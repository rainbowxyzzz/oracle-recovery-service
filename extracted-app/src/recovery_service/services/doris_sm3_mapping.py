from __future__ import annotations

import re
import uuid
import asyncio
import time
from copy import deepcopy
from urllib.parse import urlparse, urlunparse
from datetime import datetime
from typing import Any

import pymysql
import redis
from pymysql.cursors import DictCursor
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from recovery_service.api.schemas.doris_sm3_mapping import (
    DEFAULT_DORIS_SM3_KEYWORDS,
    DorisSm3CatalogResponse,
    DorisSm3Column,
    DorisSm3JobListResponse,
    DorisSm3JobResponse,
    DorisSm3QueueStatusResponse,
    DorisSm3QueueTask,
    DorisSm3TaskDefinitionResponse,
    DorisSm3TaskLogResponse,
    DorisSm3TaskLogEntry,
    DorisSm3ColumnAudit,
    DorisSm3Table,
    DorisSm3TaskStatus,
    DorisSm3TaskStep,
)
from recovery_service.common.time import app_now
from recovery_service.common.security import decrypt_secret
from recovery_service.core.models.task import (
    DatabaseConnectionProfile,
    DataPlatformWorkflowVersion,
    DorisSm3Audit,
    DorisSm3Job,
    DorisSm3TaskDefinition,
    DorisSm3TaskDefinitionRevision,
    DorisSm3TaskLog,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.services.doris_mask_metadata import (
    finish_mask_task,
    latest_mask_assets_for_catalog,
    mask_asset_sort_priority,
    register_mask_task,
)
from recovery_service.services.doris_table_ddl import rewrite_table_replication_allocation
from recovery_service.settings import get_settings
from recovery_service.workers.tasks.doris_sm3_mapping import run_doris_sm3_job

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_CREATE_TABLE_RE = re.compile(
    r"(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)((?:`[^`]+`\.)?`[^`]+`|(?:[^\s.(]+\.)?[^\s.(]+)"
)
_COLUMN_DEF_RE = re.compile(
    r"(?im)^(\s*`(?P<name>(?:``|[^`])+)`\s+)(?P<type>[A-Za-z][A-Za-z0-9_]*(?:\s*\([^)]*\))?)(?P<rest>.*)$"
)
_RUNNING_STATES = {"queued", "running", "cancelling"}
_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_DORIS_IDENTIFIER_MAX_LENGTH = 64
_DEFAULT_FIELD_MAPPING_TABLE = "doris_mask_field_mappings"
_STEP_TITLES = [
    "预检查",
    "创建映射表",
    "写入映射数据",
    "准备原表",
    "创建脱敏表",
    "写入脱敏数据",
    "校验行数",
    "写入字段关系表",
]


def _enqueue_sm3_worker(job_id: uuid.UUID) -> str:
    settings = get_settings()
    result = run_doris_sm3_job.apply_async(args=[str(job_id)], queue=settings.celery_sm3_queue)
    return result.id


def list_doris_sm3_catalog(
    profile: DatabaseConnectionProfile,
    *,
    database: str | None,
    keywords: list[str] | None,
    db: Session | None = None,
) -> DorisSm3CatalogResponse:
    _ensure_doris_profile(profile)
    target_database = (database or profile.database or "").strip()
    if not target_database:
        raise ValueError("请先选择或填写 Doris 数据库。")
    clean_keywords = _clean_keywords(keywords)
    latest = _latest_sm3_audits_for_catalog(db, profile.id, target_database)
    mask_assets = _latest_mask_assets_for_catalog(db, profile.id, target_database)
    with _doris_conn(profile, None) as doris:
        with doris.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
                """,
                (target_database,),
            )
            table_rows = cur.fetchall()
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, ORDINAL_POSITION
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (target_database,),
            )
            column_rows = cur.fetchall()

    table_map = {
        str(row.get("TABLE_NAME") or row.get("table_name")): []
        for row in table_rows
        if row.get("TABLE_NAME") or row.get("table_name")
    }
    for row in column_rows:
        table_name = str(row.get("TABLE_NAME") or row.get("table_name") or "")
        column_name = str(row.get("COLUMN_NAME") or row.get("column_name") or "")
        if not table_name or not column_name or table_name not in table_map:
            continue
        matched = _matched_keywords(column_name, clean_keywords)
        table_map[table_name].append(
            DorisSm3Column(
                name=column_name,
                type=str(row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or ""),
                ordinal_position=_safe_int(row.get("ORDINAL_POSITION") or row.get("ordinal_position")),
                matched_keywords=matched,
                selected=bool(matched),
                default_mapping_table=_default_mapping_table(column_name),
            )
        )

    tables: list[DorisSm3Table] = []
    for name, columns in table_map.items():
        audit = latest.get(name)
        asset = mask_assets.get(name)
        tables.append(
            DorisSm3Table(
                name=name,
                columns=columns,
                selected_count=sum(1 for column in columns if column.selected),
                last_sm3_at=audit.succeeded_at if audit else None,
                last_sm3_output_table=audit.output_table_name if audit else None,
                last_sm3_columns=list(audit.hashed_columns or []) if audit else [],
                mask_role=asset.role if asset else None,
                mask_status=asset.status if asset else None,
                mask_algorithm=asset.algorithm if asset else None,
                mask_source_table=asset.source_table_name if asset else None,
                mask_output_table=asset.output_table_name if asset else None,
                mask_backup_table=asset.backup_table_name if asset else None,
                mask_task_id=asset.task_id if asset else None,
                mask_updated_at=asset.updated_at if asset else None,
            )
        )
    tables.sort(
        key=lambda item: (
            mask_asset_sort_priority(mask_assets.get(item.name)),
            item.last_sm3_at is not None,
            item.last_sm3_at or datetime.min,
            item.name,
        )
    )
    return DorisSm3CatalogResponse(database=target_database, keywords=clean_keywords, tables=tables)


def create_sm3_task_definition(
    profile: DatabaseConnectionProfile,
    *,
    name: str,
    database: str,
    table_name: str,
    columns: list[str],
    mapping_database: str | None = None,
    mapping_tables: dict[str, str] | None = None,
    field_mapping_database: str | None = None,
    field_mapping_table: str | None = None,
    output_suffix: str | None = None,
    table_mode: str = "create_suffixed",
    actor: AuthContext | None = None,
) -> DorisSm3TaskDefinitionResponse:
    normalized = _normalize_sm3_task_definition_payload(
        profile,
        name=name,
        database=database,
        table_name=table_name,
        columns=columns,
        mapping_database=mapping_database,
        mapping_tables=mapping_tables,
        field_mapping_database=field_mapping_database,
        field_mapping_table=field_mapping_table,
        output_suffix=output_suffix,
        table_mode=table_mode,
    )
    now = app_now()
    task = DorisSm3TaskDefinition(
        id=uuid.uuid4(),
        revision=1,
        connection_id=profile.id,
        connection_name=profile.name,
        created_by_user_id=_auth_user_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
        **normalized,
    )
    session = get_sync_session_factory()()
    try:
        session.add(task)
        _ensure_sm3_revision_record(session, task, actor)
        session.commit()
        session.refresh(task)
        return _sm3_task_definition_to_response(task)
    finally:
        session.close()


def list_sm3_task_definitions() -> list[DorisSm3TaskDefinitionResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DorisSm3TaskDefinition)
            .where(DorisSm3TaskDefinition.archived_at.is_(None))
            .order_by(desc(DorisSm3TaskDefinition.updated_at))
            .limit(300)
        ).scalars().all()
        return [_sm3_task_definition_to_response(row) for row in rows]
    finally:
        session.close()


def update_sm3_task_definition(
    task_id: uuid.UUID,
    *,
    updates: dict[str, Any],
    actor: AuthContext | None = None,
) -> DorisSm3TaskDefinitionResponse:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm3TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM3 task definition does not exist.")
        old_snapshot = sm3_task_definition_snapshot(task)
        _freeze_existing_sm3_production_references(session, task, old_snapshot)
        _ensure_sm3_revision_record(session, task, actor)
        profile = session.get(DatabaseConnectionProfile, updates.get("connection_id") or task.connection_id)
        if not profile:
            raise ValueError("Task connection does not exist.")
        merged = {
            "name": updates.get("name", task.name),
            "database": updates.get("database", task.database),
            "table_name": updates.get("table_name", task.table_name),
            "columns": updates.get("columns", task.hashed_columns),
            "mapping_database": updates.get("mapping_database", task.mapping_database),
            "mapping_tables": updates.get("mapping_tables", task.mapping_tables),
            "field_mapping_database": updates.get("field_mapping_database", task.field_mapping_database),
            "field_mapping_table": updates.get("field_mapping_table", task.field_mapping_table),
            "output_suffix": updates.get("output_suffix", task.output_suffix),
            "table_mode": updates.get("table_mode", task.table_mode),
        }
        normalized = _normalize_sm3_task_definition_payload(profile, **merged)
        task.connection_id = profile.id
        task.connection_name = updates.get("connection_name", profile.name)
        for key, value in normalized.items():
            setattr(task, key, value)
        updated_snapshot = sm3_task_definition_snapshot(task)
        old_content = {key: value for key, value in old_snapshot.items() if key != "revision"}
        updated_content = {key: value for key, value in updated_snapshot.items() if key != "revision"}
        if old_content != updated_content:
            task.revision = int(task.revision or 1) + 1
            _ensure_sm3_revision_record(session, task, actor)
        task.updated_at = app_now()
        session.commit()
        session.refresh(task)
        return _sm3_task_definition_to_response(task)
    finally:
        session.close()


def archive_sm3_task_definition(task_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm3TaskDefinitionResponse:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm3TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM3 task definition does not exist.")
        snapshot = sm3_task_definition_snapshot(task)
        _freeze_existing_sm3_production_references(session, task, snapshot)
        _ensure_sm3_revision_record(session, task, actor)
        task.archived_at = app_now()
        task.archived_by_username = actor.username if actor else None
        task.updated_at = app_now()
        session.commit()
        session.refresh(task)
        return _sm3_task_definition_to_response(task)
    finally:
        session.close()


async def run_sm3_task_definition(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor: AuthContext | None = None,
) -> DorisSm3TaskStatus:
    task = await db.get(DorisSm3TaskDefinition, task_id)
    if not task or task.archived_at is not None:
        raise KeyError("Doris SM3 task definition does not exist.")
    profile = await db.get(DatabaseConnectionProfile, task.connection_id)
    if not profile:
        raise ValueError("Task connection does not exist.")
    return await create_sm3_mapping_task(
        db,
        profile,
        database=task.database,
        table_name=task.table_name,
        columns=list(task.hashed_columns or []),
        mapping_database=task.mapping_database,
        mapping_tables=dict(task.mapping_tables or {}),
        field_mapping_database=task.field_mapping_database,
        field_mapping_table=task.field_mapping_table,
        output_suffix=task.output_suffix,
        table_mode=task.table_mode,
        actor=actor,
    )


def run_sm3_task_definition_sync(task_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm3TaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm3TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM3 task definition does not exist.")
        profile = session.get(DatabaseConnectionProfile, task.connection_id)
        if not profile:
            raise ValueError("Task connection does not exist.")
        normalized = _normalize_sm3_task_definition_payload(
            profile,
            name=task.name,
            database=task.database,
            table_name=task.table_name,
            columns=list(task.hashed_columns or []),
            mapping_database=task.mapping_database,
            mapping_tables=dict(task.mapping_tables or {}),
            field_mapping_database=task.field_mapping_database,
            field_mapping_table=task.field_mapping_table,
            output_suffix=task.output_suffix,
            table_mode=task.table_mode,
        )
        conflict = _find_running_same_table_job_sync(session, profile.id, normalized["database"], normalized["table_name"])
        if conflict:
            raise ValueError(
                f"SM3 task already exists for {normalized['database']}.{normalized['table_name']}: "
                f"{conflict.id} ({conflict.state}). Please wait for it to finish or cancel it first."
            )
        backup_table = (
            _suffixed_table_name(normalized["table_name"], normalized["output_suffix"], "origin")
            if normalized["table_mode"] == "replace_original"
            else None
        )
        output_table = (
            normalized["table_name"]
            if normalized["table_mode"] == "replace_original"
            else _suffixed_table_name(normalized["table_name"], normalized["output_suffix"], "sm3")
        )
        job = DorisSm3Job(
            connection_id=profile.id,
            connection_name=profile.name,
            database=normalized["database"],
            table_name=normalized["table_name"],
            table_mode=normalized["table_mode"],
            backup_table_name=backup_table,
            output_table_name=output_table,
            hashed_columns=normalized["hashed_columns"],
            mapping_database=normalized["mapping_database"],
            mapping_tables=normalized["mapping_tables"],
            field_mapping_database=normalized["field_mapping_database"],
            field_mapping_table=normalized["field_mapping_table"],
            created_by_user_id=_auth_user_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_by_auth_type=actor.auth_type if actor else "api-key",
            state="queued",
            message="Task submitted and waiting for worker.",
            steps=_initial_steps(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        _add_sm3_log(
            session,
            job,
            "INFO",
            "task_created",
            "SM3 task definition submitted from offline development.",
            payload={"task_definition_id": str(task.id), "task_definition_name": task.name},
        )
        _register_sm3_mask_assets(profile, job)
        job.celery_task_id = _enqueue_sm3_worker(job.id)
        session.commit()
        session.refresh(job)
        _add_sm3_log(
            session,
            job,
            "INFO",
            "worker_queued",
            f"Task queued for worker: {job.celery_task_id}",
            payload={"celery_task_id": job.celery_task_id},
        )
        return job_to_status(job)
    finally:
        session.close()


def run_sm3_task_snapshot_sync(snapshot: dict[str, Any], actor: AuthContext | None = None) -> DorisSm3TaskStatus:
    session = get_sync_session_factory()()
    try:
        profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(snapshot.get("connection_id"))))
        if not profile:
            raise ValueError("SM3 快照连接不存在。")
        task = DorisSm3TaskDefinition(
            id=uuid.UUID(str(snapshot.get("task_definition_id") or uuid.uuid4())),
            name=str(snapshot.get("name") or "SM3 snapshot"),
            revision=int(snapshot.get("revision") or 1),
            connection_id=profile.id,
            connection_name=profile.name,
            database=str(snapshot.get("database") or ""),
            table_name=str(snapshot.get("table_name") or ""),
            table_mode=str(snapshot.get("table_mode") or "create_suffixed"),
            hashed_columns=list(snapshot.get("columns") or []),
            mapping_database=snapshot.get("mapping_database"),
            mapping_tables=dict(snapshot.get("mapping_tables") or {}),
            field_mapping_database=snapshot.get("field_mapping_database"),
            field_mapping_table=snapshot.get("field_mapping_table"),
            output_suffix=snapshot.get("output_suffix"),
        )
        normalized = _normalize_sm3_task_definition_payload(
            profile,
            name=task.name,
            database=task.database,
            table_name=task.table_name,
            columns=list(task.hashed_columns or []),
            mapping_database=task.mapping_database,
            mapping_tables=dict(task.mapping_tables or {}),
            field_mapping_database=task.field_mapping_database,
            field_mapping_table=task.field_mapping_table,
            output_suffix=task.output_suffix,
            table_mode=task.table_mode,
        )
        conflict = _find_running_same_table_job_sync(session, profile.id, normalized["database"], normalized["table_name"])
        if conflict:
            raise ValueError(f"SM3 task already exists for {normalized['database']}.{normalized['table_name']}.")
        backup_table = _suffixed_table_name(normalized["table_name"], normalized["output_suffix"], "origin") if normalized["table_mode"] == "replace_original" else None
        output_table = normalized["table_name"] if normalized["table_mode"] == "replace_original" else _suffixed_table_name(normalized["table_name"], normalized["output_suffix"], "sm3")
        job = DorisSm3Job(
            connection_id=profile.id,
            connection_name=profile.name,
            database=normalized["database"],
            table_name=normalized["table_name"],
            table_mode=normalized["table_mode"],
            backup_table_name=backup_table,
            output_table_name=output_table,
            hashed_columns=normalized["hashed_columns"],
            mapping_database=normalized["mapping_database"],
            mapping_tables=normalized["mapping_tables"],
            field_mapping_database=normalized["field_mapping_database"],
            field_mapping_table=normalized["field_mapping_table"],
            created_by_user_id=_auth_user_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_by_auth_type=actor.auth_type if actor else "api-key",
            state="queued",
            message="Task submitted from frozen SM3 revision.",
            steps=_initial_steps(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        _register_sm3_mask_assets(profile, job)
        job.celery_task_id = _enqueue_sm3_worker(job.id)
        session.commit()
        return job_to_status(job)
    finally:
        session.close()


def sm3_task_definition_snapshot(task: DorisSm3TaskDefinition) -> dict[str, Any]:
    return {
        "task_definition_id": str(task.id),
        "revision": int(task.revision or 1),
        "name": task.name,
        "connection_id": str(task.connection_id),
        "connection_name": task.connection_name,
        "database": task.database,
        "table_name": task.table_name,
        "columns": list(task.hashed_columns or []),
        "mapping_database": task.mapping_database,
        "mapping_tables": dict(task.mapping_tables or {}),
        "field_mapping_database": task.field_mapping_database,
        "field_mapping_table": task.field_mapping_table,
        "output_suffix": task.output_suffix,
        "table_mode": task.table_mode,
    }


def _ensure_sm3_revision_record(session: Session, task: DorisSm3TaskDefinition, actor: AuthContext | None) -> None:
    revision = int(task.revision or 1)
    existing = session.scalar(
        select(DorisSm3TaskDefinitionRevision.id).where(
            DorisSm3TaskDefinitionRevision.task_definition_id == task.id,
            DorisSm3TaskDefinitionRevision.revision == revision,
        )
    )
    if existing:
        return
    session.add(
        DorisSm3TaskDefinitionRevision(
            id=uuid.uuid4(),
            task_definition_id=task.id,
            revision=revision,
            snapshot=deepcopy(sm3_task_definition_snapshot(task)),
            created_by_username=actor.username if actor else task.created_by_username,
            created_at=app_now(),
        )
    )


def _freeze_existing_sm3_production_references(
    session: Session,
    task: DorisSm3TaskDefinition,
    snapshot: dict[str, Any],
) -> None:
    task_id = str(task.id)
    versions = session.execute(
        select(DataPlatformWorkflowVersion).where(DataPlatformWorkflowVersion.channel == "prod")
    ).scalars().all()
    for version in versions:
        changed = False
        nodes = deepcopy(version.nodes or [])
        for node in nodes:
            if node.get("node_type") != "sm3_mapping":
                continue
            config = dict(node.get("config") or {})
            if str(config.get("task_definition_id") or "") != task_id or config.get("task_definition_snapshot"):
                continue
            config["task_definition_name"] = snapshot["name"]
            config["task_definition_revision"] = snapshot["revision"]
            config["task_definition_snapshot"] = deepcopy(snapshot)
            node["config"] = config
            changed = True
        if changed:
            from recovery_service.services.data_platform import (
                _build_release_snapshot,
                _execution_content_hash,
                _version_schedule_payload,
            )

            version.nodes = nodes
            release = dict(version.release_snapshot or {})
            if release:
                release["nodes"] = deepcopy(nodes)
            else:
                release = _build_release_snapshot(nodes, version.edges or [], _version_schedule_payload(version))
            version.release_snapshot = release
            version.execution_content_hash = _execution_content_hash(release)
            version.updated_at = app_now()


async def create_sm3_mapping_task(
    db: AsyncSession,
    profile: DatabaseConnectionProfile,
    *,
    database: str,
    table_name: str,
    columns: list[str],
    mapping_database: str | None = None,
    mapping_tables: dict[str, str] | None = None,
    field_mapping_database: str | None = None,
    field_mapping_table: str | None = None,
    output_suffix: str | None = None,
    table_mode: str = "create_suffixed",
    actor: AuthContext | None = None,
) -> DorisSm3TaskStatus:
    _ensure_doris_profile(profile)
    clean_database = database.strip()
    clean_mapping_database = (mapping_database or "").strip() or None
    clean_field_mapping_database = (field_mapping_database or "").strip() or clean_mapping_database or clean_database
    clean_field_mapping_table = (field_mapping_table or "").strip() or _DEFAULT_FIELD_MAPPING_TABLE
    clean_table = table_name.strip()
    clean_columns = [item.strip() for item in columns if item and item.strip()]
    clean_table_mode = table_mode if table_mode in {"replace_original", "create_suffixed"} else "create_suffixed"
    if not clean_database:
        raise ValueError("请填写 Doris 数据库。")
    if not clean_table:
        raise ValueError("请填写要脱敏的表。")
    if not clean_columns:
        raise ValueError("请至少选择一个要 SM3 脱敏的字段。")
    if clean_mapping_database and not _IDENT_RE.match(clean_mapping_database):
        raise ValueError("映射表目标库名不合法，只能包含中文、字母、数字和下划线。")
    if not _IDENT_RE.match(clean_field_mapping_database):
        raise ValueError("字段关系映射库名不合法，只能包含中文、字母、数字和下划线。")
    if not _IDENT_RE.match(clean_field_mapping_table):
        raise ValueError("字段关系映射表名不合法，只能包含中文、字母、数字和下划线。")
    clean_mapping_tables = _clean_mapping_tables(clean_columns, mapping_tables or {})
    backup_table = (
        _suffixed_table_name(clean_table, output_suffix, "origin")
        if clean_table_mode == "replace_original"
        else None
    )
    output_table = clean_table if clean_table_mode == "replace_original" else _suffixed_table_name(clean_table, output_suffix, "sm3")
    _validate_table_name_length(output_table, "脱敏表名")
    if backup_table:
        _validate_table_name_length(backup_table, "备份表名")
    for mapping_table in clean_mapping_tables.values():
        _validate_table_name_length(mapping_table, "映射表名")
    _validate_table_name_length(clean_field_mapping_table, "字段关系映射表名")
    conflict = await _find_running_same_table_job(db, profile.id, clean_database, clean_table)
    if conflict:
        raise ValueError(
            f"SM3 task already exists for {clean_database}.{clean_table}: "
            f"{conflict.id} ({conflict.state}). Please wait for it to finish or cancel it first."
        )
    job = DorisSm3Job(
        connection_id=profile.id,
        connection_name=profile.name,
        database=clean_database,
        table_name=clean_table,
        table_mode=clean_table_mode,
        backup_table_name=backup_table,
        output_table_name=output_table,
        hashed_columns=clean_columns,
        mapping_database=clean_mapping_database,
        mapping_tables=clean_mapping_tables,
        field_mapping_database=clean_field_mapping_database,
        field_mapping_table=clean_field_mapping_table,
        created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        state="queued",
        message="任务已提交，等待 Worker 执行。",
        steps=_initial_steps(),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await _add_sm3_log_async(
        db,
        job,
        "INFO",
        "task_created",
        "SM3 mapping mask task submitted.",
        payload={
            "database": clean_database,
            "table_name": clean_table,
            "columns": clean_columns,
            "table_mode": clean_table_mode,
            "mapping_database": clean_mapping_database,
            "field_mapping_database": clean_field_mapping_database,
            "field_mapping_table": clean_field_mapping_table,
            "created_by_username": actor.username if actor else None,
            "created_by_auth_type": actor.auth_type if actor else "api-key",
        },
    )
    _register_sm3_mask_assets(profile, job)
    job.celery_task_id = _enqueue_sm3_worker(job.id)
    await db.commit()
    await db.refresh(job)
    await _add_sm3_log_async(
        db,
        job,
        "INFO",
        "worker_queued",
        f"Task queued for worker: {job.celery_task_id}",
        payload={"celery_task_id": job.celery_task_id},
    )
    return job_to_status(job)


async def list_sm3_mapping_tasks(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID | None = None,
    database: str | None = None,
    limit: int = 50,
) -> DorisSm3JobListResponse:
    stmt = select(DorisSm3Job).order_by(desc(DorisSm3Job.created_at)).limit(max(1, min(limit, 200)))
    if connection_id:
        stmt = stmt.where(DorisSm3Job.connection_id == connection_id)
    if database:
        stmt = stmt.where(DorisSm3Job.database == database)
    result = await db.execute(stmt)
    jobs = [job_to_response(job) for job in result.scalars().all()]
    return DorisSm3JobListResponse(tasks=jobs)


async def _find_running_same_table_job(
    db: AsyncSession,
    connection_id: uuid.UUID,
    database: str,
    table_name: str,
) -> DorisSm3Job | None:
    result = await db.execute(
        select(DorisSm3Job)
        .where(
            DorisSm3Job.connection_id == connection_id,
            DorisSm3Job.database == database,
            DorisSm3Job.table_name == table_name,
            DorisSm3Job.state.in_(list(_RUNNING_STATES)),
        )
        .order_by(DorisSm3Job.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _find_running_same_table_job_sync(
    session: Session,
    connection_id: uuid.UUID,
    database: str,
    table_name: str,
) -> DorisSm3Job | None:
    return session.execute(
        select(DorisSm3Job)
        .where(
            DorisSm3Job.connection_id == connection_id,
            DorisSm3Job.database == database,
            DorisSm3Job.table_name == table_name,
            DorisSm3Job.state.in_(list(_RUNNING_STATES)),
        )
        .order_by(DorisSm3Job.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()


async def get_sm3_mapping_task(db: AsyncSession, task_id: uuid.UUID) -> DorisSm3TaskStatus:
    job = await db.get(DorisSm3Job, task_id)
    if not job:
        raise KeyError("Doris SM3 任务不存在或已被清理。")
    return job_to_status(job)


async def list_sm3_mapping_task_logs(
    db: AsyncSession,
    task_id: uuid.UUID,
    *,
    limit: int = 500,
) -> DorisSm3TaskLogResponse:
    stmt = (
        select(DorisSm3TaskLog)
        .where(DorisSm3TaskLog.task_id == task_id)
        .order_by(DorisSm3TaskLog.id.asc())
        .limit(max(1, min(limit, 2000)))
    )
    result = await db.execute(stmt)
    logs = [DorisSm3TaskLogEntry.model_validate(log, from_attributes=True) for log in result.scalars().all()]
    return DorisSm3TaskLogResponse(task_id=task_id, logs=logs)


async def get_sm3_queue_status(db: AsyncSession) -> DorisSm3QueueStatusResponse:
    settings = get_settings()
    warnings: list[str] = []
    pending_count = 0
    active: dict[str, list[dict[str, Any]]] = {}
    reserved: dict[str, list[dict[str, Any]]] = {}
    scheduled: dict[str, list[dict[str, Any]]] = {}
    queue_name = settings.celery_sm3_queue

    try:
        pending_count = await asyncio.to_thread(_redis_pending_count, settings.celery_broker_url, queue_name)
    except Exception as exc:
        warnings.append(f"Unable to read Redis queue length: {exc}")

    try:
        inspect = run_doris_sm3_job.app.control.inspect(timeout=1.0)
        active = await asyncio.to_thread(lambda: inspect.active() or {})
        reserved = await asyncio.to_thread(lambda: inspect.reserved() or {})
        scheduled = await asyncio.to_thread(lambda: inspect.scheduled() or {})
    except Exception as exc:
        warnings.append(f"Unable to inspect Celery workers: {exc}")

    jobs_by_celery_id = await _sm3_jobs_by_celery_id(db)
    active_tasks = _celery_tasks(active, jobs_by_celery_id, "running")
    reserved_tasks = _celery_tasks(reserved, jobs_by_celery_id, "reserved")
    scheduled_tasks = _scheduled_celery_tasks(scheduled, jobs_by_celery_id)
    queued_jobs, running_jobs = await _sm3_queue_jobs(db)
    parsed = urlparse(settings.celery_broker_url)
    return DorisSm3QueueStatusResponse(
        broker="redis",
        broker_url=_safe_url(settings.celery_broker_url),
        result_backend=_safe_url(settings.celery_result_backend),
        queue_name=queue_name,
        redis_host=parsed.hostname,
        redis_port=parsed.port,
        redis_db=_safe_redis_db(parsed.path),
        pending_count=pending_count,
        active_worker_count=len(set(active) | set(reserved) | set(scheduled)),
        configured_concurrency=settings.sm3_worker_concurrency,
        prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
        active_count=len(active_tasks),
        reserved_count=len(reserved_tasks),
        scheduled_count=len(scheduled_tasks),
        active_tasks=active_tasks,
        reserved_tasks=reserved_tasks,
        scheduled_tasks=scheduled_tasks,
        running_jobs=running_jobs,
        queued_jobs=queued_jobs,
        warnings=warnings,
    )


async def cancel_sm3_mapping_task(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor: AuthContext | None = None,
) -> DorisSm3TaskStatus:
    job = await db.get(DorisSm3Job, task_id)
    if not job:
        raise KeyError("Doris SM3 task not found.")
    if job.state in _TERMINAL_STATES:
        return job_to_status(job)
    if job.celery_task_id:
        try:
            run_doris_sm3_job.app.control.revoke(job.celery_task_id, terminate=False)
        except Exception:
            pass
    job.cancel_requested = True
    if job.state == "queued":
        job.state = "cancelled"
        job.message = "Queued task was cancelled."
        job.finished_at = app_now()
    else:
        job.state = "cancelling"
        job.message = "Cancel requested. Waiting for current database step to finish."
    await db.commit()
    await db.refresh(job)
    await _add_sm3_log_async(
        db,
        job,
        "CANCEL",
        "cancel_requested",
        "SM3 task cancel requested by user.",
        payload={"cancelled_by": actor.username if actor else None, "auth_type": actor.auth_type if actor else "api-key"},
    )
    if job.state == "cancelling" and job.active_query_id:
        profile = await db.get(DatabaseConnectionProfile, job.connection_id)
        if profile:
            cancel_message = await asyncio.to_thread(_try_cancel_active_doris_query, profile, job.database, job.active_query_id)
            await _add_sm3_log_async(
                db,
                job,
                "CANCEL",
                "kill_doris_query",
                cancel_message,
                db_session_id=job.active_query_id,
            )
    if job.state == "cancelled":
        _finish_sm3_mask_assets(job.id, "cancelled", job.message)
    return job_to_status(job)


def job_to_status(job: DorisSm3Job) -> DorisSm3TaskStatus:
    return DorisSm3TaskStatus(
        task_id=job.id,
        state=job.state,  # type: ignore[arg-type]
        message=_job_display_message(job),
        database=job.database,
        table_name=job.table_name,
        table_mode=job.table_mode,  # type: ignore[arg-type]
        backup_table_name=job.backup_table_name,
        output_table_name=job.output_table_name,
        hashed_columns=list(job.hashed_columns or []),
        mapping_database=job.mapping_database,
        mapping_tables=dict(job.mapping_tables or {}),
        field_mapping_database=job.field_mapping_database,
        field_mapping_table=job.field_mapping_table,
        source_rows=job.source_rows,
        target_rows=job.target_rows,
        steps=[DorisSm3TaskStep.model_validate(_display_step(step)) for step in job.steps or []],
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def job_to_response(job: DorisSm3Job) -> DorisSm3JobResponse:
    waiting_seconds, running_seconds = _job_elapsed_seconds(job)
    return DorisSm3JobResponse(
        task_id=job.id,
        celery_task_id=job.celery_task_id,
        connection_id=job.connection_id,
        connection_name=job.connection_name,
        database=job.database,
        table_name=job.table_name,
        table_mode=job.table_mode,  # type: ignore[arg-type]
        backup_table_name=job.backup_table_name,
        output_table_name=job.output_table_name,
        hashed_columns=list(job.hashed_columns or []),
        mapping_database=job.mapping_database,
        mapping_tables=dict(job.mapping_tables or {}),
        field_mapping_database=job.field_mapping_database,
        field_mapping_table=job.field_mapping_table,
        created_by_user_id=job.created_by_user_id,
        created_by_username=job.created_by_username,
        created_by_auth_type=job.created_by_auth_type,
        state=job.state,  # type: ignore[arg-type]
        message=_job_display_message(job),
        current_step=_display_text(job.current_step),
        source_rows=job.source_rows,
        target_rows=job.target_rows,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        started_at=job.started_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
        waiting_seconds=waiting_seconds,
        running_seconds=running_seconds,
    )


def get_sm3_task_status_sync(task_id: uuid.UUID) -> DorisSm3TaskStatus:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm3Job, task_id)
        if not job:
            raise KeyError("Doris SM3 task does not exist.")
        return job_to_status(job)
    finally:
        session.close()


def _sm3_task_definition_to_response(task: DorisSm3TaskDefinition) -> DorisSm3TaskDefinitionResponse:
    return DorisSm3TaskDefinitionResponse(
        task_id=task.id,
        revision=int(task.revision or 1),
        name=task.name,
        connection_id=task.connection_id,
        connection_name=task.connection_name,
        database=task.database,
        table_name=task.table_name,
        columns=list(task.hashed_columns or []),
        mapping_database=task.mapping_database,
        mapping_tables=dict(task.mapping_tables or {}),
        field_mapping_database=task.field_mapping_database,
        field_mapping_table=task.field_mapping_table,
        output_suffix=task.output_suffix,
        table_mode=task.table_mode,  # type: ignore[arg-type]
        created_by_username=task.created_by_username,
        created_by_auth_type=task.created_by_auth_type,
        archived_at=task.archived_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _job_elapsed_seconds(job: DorisSm3Job) -> tuple[int | None, int | None]:
    now = app_now()
    waiting_seconds = None
    running_seconds = None
    if job.state == "queued" and job.created_at:
        waiting_seconds = max(0, int((now - job.created_at).total_seconds()))
    if job.started_at:
        end = job.finished_at or now
        running_seconds = max(0, int((end - job.started_at).total_seconds()))
        if job.created_at:
            waiting_seconds = max(0, int((job.started_at - job.created_at).total_seconds()))
    return waiting_seconds, running_seconds


def _redis_pending_count(redis_url: str, queue_name: str) -> int:
    client = redis.from_url(redis_url)
    total = 0
    for key in client.scan_iter(match=f"{queue_name}*"):
        try:
            if client.type(key) == b"list":
                total += int(client.llen(key))
        except Exception:
            continue
    return total


async def _sm3_jobs_by_celery_id(db: AsyncSession) -> dict[str, DorisSm3Job]:
    result = await db.execute(
        select(DorisSm3Job).where(
            DorisSm3Job.celery_task_id.is_not(None),
            DorisSm3Job.state.in_(list(_RUNNING_STATES)),
        )
    )
    return {str(job.celery_task_id): job for job in result.scalars().all() if job.celery_task_id}


async def _sm3_queue_jobs(db: AsyncSession) -> tuple[list[DorisSm3QueueTask], list[DorisSm3QueueTask]]:
    result = await db.execute(
        select(DorisSm3Job)
        .where(DorisSm3Job.state.in_(["queued", "running", "cancelling"]))
        .order_by(DorisSm3Job.created_at.asc())
        .limit(200)
    )
    queued: list[DorisSm3QueueTask] = []
    running: list[DorisSm3QueueTask] = []
    for job in result.scalars().all():
        item = _job_queue_task(job)
        if job.state == "queued":
            queued.append(item)
        else:
            running.append(item)
    return queued, running


def _celery_tasks(
    grouped: dict[str, list[dict[str, Any]]],
    jobs_by_celery_id: dict[str, DorisSm3Job],
    state: str,
) -> list[DorisSm3QueueTask]:
    items: list[DorisSm3QueueTask] = []
    for worker, tasks in (grouped or {}).items():
        for task in tasks or []:
            celery_task_id = str(task.get("id") or "")
            job = jobs_by_celery_id.get(celery_task_id)
            items.append(_celery_task_response(task, worker, job, state))
    return items


def _scheduled_celery_tasks(
    grouped: dict[str, list[dict[str, Any]]],
    jobs_by_celery_id: dict[str, DorisSm3Job],
) -> list[DorisSm3QueueTask]:
    items: list[DorisSm3QueueTask] = []
    for worker, entries in (grouped or {}).items():
        for entry in entries or []:
            task = entry.get("request") or entry
            celery_task_id = str(task.get("id") or "")
            job = jobs_by_celery_id.get(celery_task_id)
            items.append(_celery_task_response(task, worker, job, "scheduled"))
    return items


def _celery_task_response(task: dict[str, Any], worker: str, job: DorisSm3Job | None, state: str) -> DorisSm3QueueTask:
    args = task.get("args") or []
    job_id = job.id if job else _uuid_from_task_args(args)
    if job:
        item = _job_queue_task(job)
        item.worker = worker
        item.state = state
        item.celery_task_id = str(task.get("id") or item.celery_task_id or "")
        item.name = str(task.get("name") or item.name or "")
        return item
    return DorisSm3QueueTask(
        celery_task_id=str(task.get("id") or "") or None,
        job_id=job_id,
        name=str(task.get("name") or "") or None,
        worker=worker,
        state=state,
    )


def _job_queue_task(job: DorisSm3Job) -> DorisSm3QueueTask:
    waiting_seconds, running_seconds = _job_elapsed_seconds(job)
    return DorisSm3QueueTask(
        celery_task_id=job.celery_task_id,
        job_id=job.id,
        name="doris.sm3_mapping",
        state=job.state,
        database=job.database,
        table_name=job.table_name,
        submitted_at=job.created_at,
        started_at=job.started_at,
        waiting_seconds=waiting_seconds,
        running_seconds=running_seconds,
    )


def _uuid_from_task_args(args: Any) -> uuid.UUID | None:
    if isinstance(args, str):
        values = re.findall(r"[0-9a-fA-F-]{36}", args)
        args = values
    if isinstance(args, (list, tuple)) and args:
        try:
            return uuid.UUID(str(args[0]).strip("'\""))
        except Exception:
            return None
    return None


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def _safe_redis_db(path: str | None) -> int | None:
    try:
        return int((path or "").strip("/") or "0")
    except Exception:
        return None


def run_sm3_mapping_job(session: Session, job_id: uuid.UUID) -> dict[str, Any]:
    job = session.get(DorisSm3Job, job_id)
    if not job:
        return {"state": "failed", "message": "job not found"}
    _add_sm3_log(session, job, "INFO", "worker_started", "Worker started SM3 task.")
    if job.cancel_requested:
        _finish_job(session, job, "cancelled", "任务已取消。")
        return {"state": "cancelled", "message": job.message}

    profile = session.get(DatabaseConnectionProfile, job.connection_id)
    if not profile:
        _add_sm3_log(session, job, "ERROR", "load_connection", "Doris connection profile not found.", error_message="Doris connection profile not found.")
        _finish_job(session, job, "failed", "Doris 数据连接不存在。", error="Doris 数据连接不存在。")
        return {"state": "failed", "message": job.message}

    renamed = False
    created_output_table = False
    source_table = job.table_name
    mapping_database = job.mapping_database or job.database
    replace_original = job.table_mode == "replace_original"
    _start_job(session, job)
    try:
        _check_cancel(session, job)
        _set_step(session, job, 0, "running")
        _add_sm3_log(session, job, "INFO", "connect_doris", f"Connecting Doris {profile.host}:{profile.port or 9030}/{job.database}.")
        with _doris_conn(profile, job.database) as doris:
            with doris.cursor() as cur:
                _set_active_doris_connection(session, job, _load_doris_connection_id(cur))
                table_columns = _load_table_columns(cur, job.database, job.table_name)
                if not table_columns:
                    raise ValueError(f"表 {job.table_name} 不存在或没有字段。")
                unknown_columns = [name for name in job.hashed_columns if name not in table_columns]
                if unknown_columns:
                    raise ValueError(f'以下字段不存在：{", ".join(unknown_columns)}')
                if replace_original and job.backup_table_name and _table_exists(cur, job.database, job.backup_table_name):
                    raise ValueError(f"备份表 {job.backup_table_name} 已存在，请换一个后缀。")
                if not replace_original and job.output_table_name and _table_exists(cur, job.database, job.output_table_name):
                    raise ValueError(f"脱敏目标表 {job.output_table_name} 已存在，请换一个后缀。")
                source_rows = _count_rows(cur, job.database, job.table_name)
                job.source_rows = source_rows
                session.commit()
                _set_step(session, job, 0, "success", f"原表 {source_rows} 行，待 SM3 脱敏 {len(job.hashed_columns)} 个字段。")

                _check_cancel(session, job)
                ensure_mapping_database_sql = f"CREATE DATABASE IF NOT EXISTS {_q(mapping_database)}"
                mapping_create_sqls = [
                    _build_create_mapping_table_sql(mapping_database, mapping_table)
                    for mapping_table in sorted(set((job.mapping_tables or {}).values()))
                ]
                _set_step(session, job, 1, "running", sql="\n\n".join([ensure_mapping_database_sql, *mapping_create_sqls]))
                _execute_logged_sql(session, job, cur, ensure_mapping_database_sql, stage="prepare_mapping_database", sql_type="CREATE_DATABASE")
                for sql in mapping_create_sqls:
                    _execute_logged_sql(session, job, cur, sql, stage="create_mapping_table", sql_type="CREATE_TABLE")
                for mapping_table in sorted(set((job.mapping_tables or {}).values())):
                    _validate_mapping_table_model(cur, mapping_database, mapping_table)
                _set_step(session, job, 1, "success", f"映射表已准备：{mapping_database} 库 {len(mapping_create_sqls)} 张。")

                _check_cancel(session, job)
                mapping_insert_sqls = [
                    _build_mapping_insert_sql(mapping_database, job.database, job.table_name, column, job.mapping_tables[column])
                    for column in job.hashed_columns
                ]
                _set_step(session, job, 2, "running", sql="\n\n".join(mapping_insert_sqls))
                for sql in mapping_insert_sqls:
                    _execute_logged_sql(session, job, cur, sql, stage="insert_mapping_values", sql_type="INSERT_SELECT")
                _set_step(session, job, 2, "success", "字段原值与 SM3 值已写入映射表。")

                _check_cancel(session, job)
                if replace_original:
                    if not job.backup_table_name:
                        raise ValueError("覆盖原表模式缺少备份表名。")
                    rename_sql = f"ALTER TABLE {_q(job.database)}.{_q(job.table_name)} RENAME {_q(job.backup_table_name)}"
                    _set_step(session, job, 3, "running", sql=rename_sql)
                    _execute_logged_sql(session, job, cur, rename_sql, stage="rename_source_table", sql_type="ALTER_TABLE")
                    renamed = True
                    source_table = job.backup_table_name
                    _set_step(session, job, 3, "success", f"原表已重命名为 {job.backup_table_name}。", sql=rename_sql)
                else:
                    source_table = job.table_name
                    _set_step(session, job, 3, "success", f"原表保持不变，目标脱敏表为 {job.output_table_name}。")

                _check_cancel(session, job)
                ddl = _show_create_table(cur, job.database, source_table)
                create_sql = _replace_create_table_name(ddl, job.output_table_name or job.table_name)
                create_sql = _rewrite_hashed_column_types(create_sql, list(job.hashed_columns or []))
                create_sql = rewrite_table_replication_allocation(create_sql)
                _set_step(session, job, 4, "running", sql=create_sql)
                _execute_logged_sql(session, job, cur, create_sql, stage="create_masked_table", sql_type="CREATE_TABLE")
                created_output_table = True
                _set_step(session, job, 4, "success", "脱敏表已按原 DDL 创建，SM3 字段已转为 varchar(255)。", sql=create_sql)

                _check_cancel(session, job)
                insert_sql = _build_output_insert_sql(job.database, job.output_table_name or job.table_name, source_table, table_columns, list(job.hashed_columns or []))
                _set_step(session, job, 5, "running", sql=insert_sql)
                _execute_logged_sql(session, job, cur, insert_sql, stage="insert_masked_data", sql_type="INSERT_SELECT")
                _set_step(session, job, 5, "success", "SM3 脱敏数据已写入目标表。", sql=insert_sql)

                _check_cancel(session, job)
                target_rows = _count_rows(cur, job.database, job.output_table_name or job.table_name)
                job.target_rows = target_rows
                session.commit()
                if source_rows != target_rows:
                    raise ValueError(f"行数校验失败：原表 {source_rows} 行，脱敏表 {target_rows} 行。")
                _set_step(session, job, 6, "success", f"行数一致：{target_rows} 行。")

                _check_cancel(session, job)
                field_mapping_database = job.field_mapping_database or mapping_database
                field_mapping_table = job.field_mapping_table or _DEFAULT_FIELD_MAPPING_TABLE
                field_mapping_sqls = _build_field_mapping_sqls(
                    database=field_mapping_database,
                    table_name=field_mapping_table,
                    task_id=job.id,
                    source_database=job.database,
                    source_table=job.table_name,
                    masked_database=job.database,
                    masked_table=job.output_table_name or job.table_name,
                    columns=list(job.hashed_columns or []),
                    mapping_database=mapping_database,
                    mapping_tables=dict(job.mapping_tables or {}),
                )
                _set_step(session, job, 7, "running", sql="\n\n".join(field_mapping_sqls))
                for sql in field_mapping_sqls:
                    _execute_logged_sql(session, job, cur, sql, stage="write_field_mapping", sql_type="FIELD_MAPPING")
                _set_step(
                    session,
                    job,
                    7,
                    "success",
                    f"字段关系表已更新：{field_mapping_database}.{field_mapping_table}，仅保留本表本算法最新关系。",
                )
        _record_audit(session, job)
        if replace_original:
            message = f"表 {job.table_name} SM3 映射脱敏完成，原表已备份为 {job.backup_table_name}。"
        else:
            message = f"表 {job.table_name} SM3 映射脱敏完成，原表保持不变，脱敏表为 {job.output_table_name}。"
        _finish_job(session, job, "succeeded", message)
        return {"state": "succeeded", "message": message}
    except _Sm3Cancelled as exc:
        _finish_job(session, job, "cancelled", str(exc))
        return {"state": "cancelled", "message": str(exc)}
    except Exception as exc:
        session.refresh(job)
        if job.cancel_requested or job.state == "cancelling":
            _finish_job(session, job, "cancelled", "任务已取消。")
            return {"state": "cancelled", "message": "任务已取消。"}
        _mark_current_step_failed(session, job, str(exc))
        if renamed and not created_output_table and job.backup_table_name:
            _try_rollback_rename(profile, job.database, job.table_name, job.backup_table_name)
        _finish_job(session, job, "failed", f"表 {job.table_name} SM3 映射脱敏失败：{exc}", error=str(exc))
        return {"state": "failed", "message": job.message}


def _doris_conn(profile: DatabaseConnectionProfile, database: str | None):
    return pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=decrypt_secret(profile.password_enc, get_settings().credential_encryption_key),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
        connect_timeout=10,
    )


def _load_doris_connection_id(cur) -> str | None:
    try:
        cur.execute("SELECT CONNECTION_ID() AS connection_id")
        row = cur.fetchone() or {}
        value = row.get("connection_id")
        return str(value) if value is not None else None
    except Exception:
        return None


def _set_active_doris_connection(session: Session, job: DorisSm3Job, connection_id: str | None) -> None:
    if not connection_id:
        return
    job.active_query_id = connection_id
    session.commit()
    _add_sm3_log(
        session,
        job,
        "INFO",
        "doris_connection",
        f"Doris session recorded: {connection_id}",
        db_session_id=connection_id,
    )


async def _add_sm3_log_async(
    db: AsyncSession,
    job: DorisSm3Job,
    level: str,
    stage: str,
    message: str,
    *,
    sql_text: str | None = None,
    sql_type: str | None = None,
    db_session_id: str | None = None,
    duration_ms: int | None = None,
    affected_rows: int | None = None,
    error_message: str | None = None,
    payload: dict | None = None,
) -> None:
    db.add(
        DorisSm3TaskLog(
            task_id=job.id,
            level=level,
            stage=stage,
            message=message,
            sql_text=sql_text,
            sql_type=sql_type,
            connection_id=job.connection_id,
            database_name=job.database,
            table_name=job.table_name,
            db_session_id=db_session_id or job.active_query_id,
            duration_ms=duration_ms,
            affected_rows=affected_rows,
            error_message=error_message,
            payload=payload or {},
        )
    )
    await db.commit()


def _add_sm3_log(
    session: Session,
    job: DorisSm3Job,
    level: str,
    stage: str,
    message: str,
    *,
    sql_text: str | None = None,
    sql_type: str | None = None,
    db_session_id: str | None = None,
    duration_ms: int | None = None,
    affected_rows: int | None = None,
    error_message: str | None = None,
    payload: dict | None = None,
) -> None:
    session.add(
        DorisSm3TaskLog(
            task_id=job.id,
            level=level,
            stage=stage,
            message=message,
            sql_text=sql_text,
            sql_type=sql_type,
            connection_id=job.connection_id,
            database_name=job.database,
            table_name=job.table_name,
            db_session_id=db_session_id or job.active_query_id,
            duration_ms=duration_ms,
            affected_rows=affected_rows,
            error_message=error_message,
            payload=payload or {},
        )
    )
    session.commit()


def _execute_logged_sql(
    session: Session,
    job: DorisSm3Job,
    cur,
    sql: str,
    *,
    stage: str,
    sql_type: str,
) -> None:
    _add_sm3_log(
        session,
        job,
        "SQL",
        stage,
        "SQL execution started.",
        sql_text=sql,
        sql_type=sql_type,
    )
    started = time.monotonic()
    try:
        cur.execute(sql)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        _add_sm3_log(
            session,
            job,
            "ERROR",
            stage,
            "SQL execution failed.",
            sql_text=sql,
            sql_type=sql_type,
            duration_ms=duration_ms,
            error_message=str(exc),
        )
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    _add_sm3_log(
        session,
        job,
        "RESULT",
        stage,
        "SQL execution finished.",
        sql_text=sql,
        sql_type=sql_type,
        duration_ms=duration_ms,
        affected_rows=getattr(cur, "rowcount", None),
    )


def _try_cancel_active_doris_query(profile: DatabaseConnectionProfile, database: str | None, active_query_id: str) -> str:
    try:
        numeric_id = int(str(active_query_id).strip())
    except Exception:
        return f"Invalid Doris session id: {active_query_id}"
    try:
        with _doris_conn(profile, database or profile.database) as doris:
            with doris.cursor() as cur:
                for sql in (f"KILL QUERY {numeric_id}", f"KILL CONNECTION {numeric_id}"):
                    try:
                        cur.execute(sql)
                        return f"Doris cancel command sent: {sql}"
                    except Exception as exc:
                        last_error = str(exc)
                        continue
                return f"Doris cancel command failed: {last_error if 'last_error' in locals() else 'unknown error'}"
    except Exception as exc:
        return f"Failed to connect Doris for cancel: {exc}"


def _ensure_doris_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine != "doris":
        raise ValueError("请选择 Doris 类型的数据连接。")


def _clean_keywords(keywords: list[str] | None) -> list[str]:
    result: list[str] = []
    for item in keywords or DEFAULT_DORIS_SM3_KEYWORDS:
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return result or DEFAULT_DORIS_SM3_KEYWORDS.copy()


def _matched_keywords(column_name: str, keywords: list[str]) -> list[str]:
    lowered = column_name.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def _clean_mapping_tables(columns: list[str], mapping_tables: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for column in columns:
        table_name = (mapping_tables.get(column) or _default_mapping_table(column)).strip()
        if not _IDENT_RE.match(table_name):
            raise ValueError(f"字段 {column} 的映射表名不合法，只能包含中文、字母、数字和下划线。")
        result[column] = table_name
    return result


def _normalize_sm3_task_definition_payload(
    profile: DatabaseConnectionProfile,
    *,
    name: str,
    database: str,
    table_name: str,
    columns: list[str],
    mapping_database: str | None = None,
    mapping_tables: dict[str, str] | None = None,
    field_mapping_database: str | None = None,
    field_mapping_table: str | None = None,
    output_suffix: str | None = None,
    table_mode: str = "create_suffixed",
) -> dict[str, Any]:
    _ensure_doris_profile(profile)
    clean_name = (name or "").strip()
    clean_database = (database or "").strip()
    clean_table = (table_name or "").strip()
    clean_columns = [item.strip() for item in columns or [] if item and item.strip()]
    clean_mapping_database = (mapping_database or "").strip() or None
    clean_field_mapping_database = (field_mapping_database or "").strip() or clean_mapping_database or clean_database
    clean_field_mapping_table = (field_mapping_table or "").strip() or _DEFAULT_FIELD_MAPPING_TABLE
    clean_table_mode = table_mode if table_mode in {"replace_original", "create_suffixed"} else "create_suffixed"
    clean_output_suffix = (output_suffix or "").strip() or None
    if not clean_name:
        raise ValueError("Task name is required.")
    if not clean_database:
        raise ValueError("Doris database is required.")
    if not clean_table:
        raise ValueError("Doris table is required.")
    if not clean_columns:
        raise ValueError("At least one SM3 column is required.")
    for value, label in (
        (clean_database, "database"),
        (clean_table, "table name"),
        (clean_field_mapping_database, "field mapping database"),
        (clean_field_mapping_table, "field mapping table"),
    ):
        if not _IDENT_RE.match(value):
            raise ValueError(f"Invalid {label}.")
    if clean_mapping_database and not _IDENT_RE.match(clean_mapping_database):
        raise ValueError("Invalid mapping database.")
    if clean_output_suffix and not _IDENT_RE.match(clean_output_suffix):
        raise ValueError("Invalid output suffix.")
    clean_mapping_tables = _clean_mapping_tables(clean_columns, mapping_tables or {})
    output_table = clean_table if clean_table_mode == "replace_original" else _suffixed_table_name(clean_table, clean_output_suffix, "sm3")
    backup_table = _suffixed_table_name(clean_table, clean_output_suffix, "origin") if clean_table_mode == "replace_original" else None
    _validate_table_name_length(output_table, "output table")
    if backup_table:
        _validate_table_name_length(backup_table, "backup table")
    for mapping_table in clean_mapping_tables.values():
        _validate_table_name_length(mapping_table, "mapping table")
    _validate_table_name_length(clean_field_mapping_table, "field mapping table")
    return {
        "name": clean_name,
        "database": clean_database,
        "table_name": clean_table,
        "hashed_columns": clean_columns,
        "mapping_database": clean_mapping_database,
        "mapping_tables": clean_mapping_tables,
        "field_mapping_database": clean_field_mapping_database,
        "field_mapping_table": clean_field_mapping_table,
        "output_suffix": clean_output_suffix,
        "table_mode": clean_table_mode,
    }


def _default_mapping_table(column_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", column_name).strip("_")
    return f"sm3_map_{clean or 'column'}"


def _latest_sm3_audits(db: Session | None, connection_id: uuid.UUID, database: str) -> dict[str, DorisSm3Audit]:
    if db is None:
        return {}
    subq = (
        select(DorisSm3Audit.table_name, func.max(DorisSm3Audit.succeeded_at).label("latest_at"))
        .where(DorisSm3Audit.connection_id == connection_id, DorisSm3Audit.database == database)
        .group_by(DorisSm3Audit.table_name)
        .subquery()
    )
    stmt = (
        select(DorisSm3Audit)
        .join(subq, (DorisSm3Audit.table_name == subq.c.table_name) & (DorisSm3Audit.succeeded_at == subq.c.latest_at))
    )
    return {row.table_name: row for row in db.execute(stmt).scalars().all()}


def _latest_sm3_audits_for_catalog(
    db: Session | None,
    connection_id: uuid.UUID,
    database: str,
) -> dict[str, DorisSm3Audit]:
    if db is not None:
        return _latest_sm3_audits(db, connection_id, database)

    session = get_sync_session_factory()()
    try:
        return _latest_sm3_audits(session, connection_id, database)
    finally:
        session.close()


def _latest_mask_assets_for_catalog(
    db: Session | None,
    connection_id: uuid.UUID,
    database: str,
):
    if db is not None:
        return latest_mask_assets_for_catalog(db, connection_id=connection_id, database=database)
    session = get_sync_session_factory()()
    try:
        return latest_mask_assets_for_catalog(session, connection_id=connection_id, database=database)
    finally:
        session.close()


def _register_sm3_mask_assets(profile: DatabaseConnectionProfile, job: DorisSm3Job) -> None:
    session = get_sync_session_factory()()
    try:
        register_mask_task(
            session,
            task_id=job.id,
            profile=profile,
            database=job.database,
            source_table=job.table_name,
            output_table=job.output_table_name,
            backup_table=job.backup_table_name,
            algorithm="SM3",
            table_mode=job.table_mode,
            columns=list(job.hashed_columns or []),
            status=job.state,
        )
    finally:
        session.close()


def _finish_sm3_mask_assets(task_id: uuid.UUID, status: str, message: str | None = None) -> None:
    session = get_sync_session_factory()()
    try:
        finish_mask_task(session, task_id=task_id, status=status, message=message)
    finally:
        session.close()


def _load_table_columns(cur, database: str, table_name: str) -> list[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (database, table_name),
    )
    return [str(row.get("COLUMN_NAME") or row.get("column_name")) for row in cur.fetchall()]


def _table_exists(cur, database: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS total
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (database, table_name),
    )
    row = cur.fetchone() or {}
    return _safe_int(row.get("total") or row.get("COUNT(*)")) > 0


def _count_rows(cur, database: str, table_name: str) -> int:
    cur.execute(f"SELECT COUNT(*) AS total FROM {_q(database)}.{_q(table_name)}")
    row = cur.fetchone() or {}
    return _safe_int(row.get("total") or row.get("COUNT(*)"))


def _show_create_table(cur, database: str, table_name: str) -> str:
    cur.execute(f"SHOW CREATE TABLE {_q(database)}.{_q(table_name)}")
    row = cur.fetchone() or {}
    for key in ("Create Table", "Create Table ", "create table"):
        if row.get(key):
            return str(row[key])
    for value in row.values():
        text = str(value)
        if text.upper().lstrip().startswith("CREATE TABLE"):
            return text
    raise ValueError("无法获取原表 DDL。")


def _validate_mapping_table_model(cur, database: str, table_name: str) -> None:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (database, table_name),
    )
    columns = [str(row.get("COLUMN_NAME") or row.get("column_name")) for row in cur.fetchall()]
    ddl = _show_create_table(cur, database, table_name)
    normalized_ddl = re.sub(r"\s+", "", ddl.lower())
    has_unique_key = "uniquekey(`original_value`)" in normalized_ddl
    if columns != ["original_value", "sm3_value"] or not has_unique_key:
        raise ValueError(
            f"映射表 {table_name} 结构不符合要求，应只包含 original_value/sm3_value 两列，"
            "并使用 original_value 作为 UNIQUE KEY。"
        )


def _replace_create_table_name(ddl: str, new_table_name: str) -> str:
    replacement = r"\1" + _q(new_table_name)
    updated = _CREATE_TABLE_RE.sub(replacement, ddl, count=1)
    if updated == ddl:
        raise ValueError("无法替换 DDL 中的表名。")
    return updated


def _rewrite_hashed_column_types(ddl: str, hashed_columns: list[str]) -> str:
    hashed = set(hashed_columns)
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        column_name = match.group("name").replace("``", "`")
        if column_name not in hashed:
            return match.group(0)
        found.add(column_name)
        return f"{match.group(1)}varchar(255){match.group('rest')}"

    updated = _COLUMN_DEF_RE.sub(replace, ddl)
    missing = sorted(hashed - found)
    if missing:
        raise ValueError(f'无法在 DDL 中定位需要 SM3 脱敏的字段：{", ".join(missing)}')
    return updated


def _build_create_mapping_table_sql(database: str, table_name: str) -> str:
    ddl = f"""
CREATE TABLE IF NOT EXISTS {_q(database)}.{_q(table_name)} (
  `original_value` varchar(2048) NOT NULL,
  `sm3_value` varchar(255) NULL
)
UNIQUE KEY(`original_value`)
DISTRIBUTED BY HASH(`original_value`) BUCKETS 1
PROPERTIES (
  "replication_num" = "1",
  "enable_unique_key_merge_on_write" = "true"
)
""".strip()
    return rewrite_table_replication_allocation(ddl)


def _build_mapping_insert_sql(mapping_database: str, source_database: str, source_table: str, column: str, mapping_table: str) -> str:
    value_expr = f"CAST({_q(column)} AS STRING)"
    return (
        f"INSERT INTO {_q(mapping_database)}.{_q(mapping_table)} "
        "(`original_value`, `sm3_value`) "
        f"SELECT DISTINCT {value_expr}, SM3({value_expr}) "
        f"FROM {_q(source_database)}.{_q(source_table)} "
        f"WHERE {_q(column)} IS NOT NULL"
    )


def _build_field_mapping_sqls(
    *,
    database: str,
    table_name: str,
    task_id: uuid.UUID,
    source_database: str,
    source_table: str,
    masked_database: str,
    masked_table: str,
    columns: list[str],
    mapping_database: str,
    mapping_tables: dict[str, str],
) -> list[str]:
    create_database_sql = f"CREATE DATABASE IF NOT EXISTS {_q(database)}"
    create_table_sql = rewrite_table_replication_allocation(f"""
CREATE TABLE IF NOT EXISTS {_q(database)}.{_q(table_name)} (
  `source_database` varchar(255) NOT NULL,
  `source_table_name` varchar(255) NOT NULL,
  `source_column_name` varchar(255) NOT NULL,
  `algorithm` varchar(32) NOT NULL,
  `masked_database` varchar(255) NOT NULL,
  `masked_table_name` varchar(255) NOT NULL,
  `masked_column_name` varchar(255) NOT NULL,
  `mapping_database` varchar(255) NULL,
  `mapping_table_name` varchar(255) NULL,
  `mapping_original_column` varchar(255) NULL,
  `mapping_masked_column` varchar(255) NULL,
  `task_id` varchar(64) NULL,
  `updated_at` datetime NULL
)
UNIQUE KEY(`source_database`, `source_table_name`, `source_column_name`, `algorithm`)
DISTRIBUTED BY HASH(`source_database`, `source_table_name`, `source_column_name`) BUCKETS 1
PROPERTIES (
  "replication_num" = "1",
  "enable_unique_key_merge_on_write" = "true"
)
""".strip())
    delete_sql = (
        f"DELETE FROM {_q(database)}.{_q(table_name)} "
        f"WHERE `source_database` = {_s(source_database)} "
        f"AND `source_table_name` = {_s(source_table)} "
        "AND `algorithm` = 'SM3'"
    )
    rows = []
    for column in columns:
        mapping_table = mapping_tables.get(column)
        rows.append(
            "("
            f"{_s(source_database)}, "
            f"{_s(source_table)}, "
            f"{_s(column)}, "
            "'SM3', "
            f"{_s(masked_database)}, "
            f"{_s(masked_table)}, "
            f"{_s(column)}, "
            f"{_s(mapping_database)}, "
            f"{_s(mapping_table or '')}, "
            "'original_value', "
            "'sm3_value', "
            f"{_s(str(task_id))}, "
            "NOW()"
            ")"
        )
    insert_sql = (
        f"INSERT INTO {_q(database)}.{_q(table_name)} "
        "(`source_database`, `source_table_name`, `source_column_name`, `algorithm`, "
        "`masked_database`, `masked_table_name`, `masked_column_name`, `mapping_database`, "
        "`mapping_table_name`, `mapping_original_column`, `mapping_masked_column`, `task_id`, `updated_at`) "
        f"VALUES {', '.join(rows)}"
    )
    return [create_database_sql, create_table_sql, delete_sql, insert_sql]


def _build_output_insert_sql(
    database: str,
    output_table: str,
    source_table: str,
    table_columns: list[str],
    hashed_columns: list[str],
) -> str:
    hashed = set(hashed_columns)
    insert_columns = ", ".join(_q(column) for column in table_columns)
    select_columns = ", ".join(
        f"SM3(CAST({_q(column)} AS STRING)) AS {_q(column)}" if column in hashed else _q(column)
        for column in table_columns
    )
    return (
        f"INSERT INTO {_q(database)}.{_q(output_table)} ({insert_columns}) "
        f"SELECT {select_columns} FROM {_q(database)}.{_q(source_table)}"
    )


def _suffixed_table_name(table_name: str, suffix: str | None, default_prefix: str) -> str:
    clean_suffix = (suffix or "").strip().strip("_")
    if clean_suffix:
        if not _IDENT_RE.match(clean_suffix):
            raise ValueError("后缀只能包含中文、字母、数字和下划线。")
        result = f"{table_name}_{clean_suffix}"
    else:
        result = f"{table_name}_{default_prefix}_{app_now().strftime('%Y%m%d_%H%M%S')}"
    return result


def _validate_table_name_length(table_name: str, label: str) -> None:
    length = len(table_name)
    if length > _DORIS_IDENTIFIER_MAX_LENGTH:
        raise ValueError(
            f"{label}“{table_name}”长度为 {length}，超过 Doris 限制 {_DORIS_IDENTIFIER_MAX_LENGTH}。"
            "请缩短源表名或填写更短的后缀后再提交。"
        )


def _auth_user_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(actor.user_id)
    except ValueError:
        return None


def _try_rollback_rename(
    profile: DatabaseConnectionProfile,
    database: str,
    table_name: str,
    backup_table: str,
) -> None:
    try:
        with _doris_conn(profile, database) as db:
            with db.cursor() as cur:
                if not _table_exists(cur, database, table_name) and _table_exists(cur, database, backup_table):
                    cur.execute(f"ALTER TABLE {_q(database)}.{_q(backup_table)} RENAME {_q(table_name)}")
    except Exception:
        return


def _initial_steps() -> list[dict[str, Any]]:
    return [{"title": title, "state": "pending", "message": None, "sql": None} for title in _STEP_TITLES]


def _start_job(session: Session, job: DorisSm3Job) -> None:
    job.state = "running"
    job.message = "Worker 已开始执行 SM3 脱敏任务。"
    job.started_at = app_now()
    job.current_step = _STEP_TITLES[0]
    session.commit()


def _set_step(session: Session, job: DorisSm3Job, index: int, state: str, message: str | None = None, sql: str | None = None) -> None:
    steps = list(job.steps or _initial_steps())
    current = dict(steps[index])
    current.update({"state": state})
    if message is not None:
        current["message"] = message
    if sql is not None:
        current["sql"] = sql
    steps[index] = current
    job.steps = steps
    job.current_step = current.get("title")
    if message:
        job.message = message
    session.commit()
    _add_sm3_log(
        session,
        job,
        "STEP",
        f"step_{index}",
        f"Step {index} state changed to {state}.",
        sql_text=sql,
        payload={"step_index": index, "step_title": current.get("title"), "state": state},
    )


def _mark_current_step_failed(session: Session, job: DorisSm3Job, message: str) -> None:
    steps = list(job.steps or [])
    for index, step in enumerate(steps):
        if step.get("state") == "running":
            failed = dict(step)
            failed.update({"state": "failed", "message": message})
            steps[index] = failed
            break
    job.steps = steps
    session.commit()
    _add_sm3_log(session, job, "ERROR", "step_failed", message, error_message=message)


def _finish_job(session: Session, job: DorisSm3Job, state: str, message: str, error: str | None = None) -> None:
    job.state = state
    job.message = message
    job.error_message = error
    job.finished_at = app_now()
    job.current_step = None
    job.active_query_id = None
    session.commit()
    _add_sm3_log(
        session,
        job,
        "ERROR" if state == "failed" else ("CANCEL" if state == "cancelled" else "INFO"),
        "task_finished",
        f"Task finished with state: {state}.",
        error_message=error,
        payload={"state": state, "source_rows": job.source_rows, "target_rows": job.target_rows},
    )
    finish_mask_task(session, task_id=job.id, status=state, message=message)


def _record_audit(session: Session, job: DorisSm3Job) -> None:
    session.add(
        DorisSm3Audit(
            job_id=job.id,
            connection_id=job.connection_id,
            database=job.database,
            table_name=job.table_name,
            output_table_name=job.output_table_name,
            table_mode=job.table_mode,
            hashed_columns=list(job.hashed_columns or []),
            mapping_database=job.mapping_database,
            mapping_tables=dict(job.mapping_tables or {}),
            source_rows=job.source_rows,
            target_rows=job.target_rows,
        )
    )
    session.commit()


class _Sm3Cancelled(Exception):
    pass


def _check_cancel(session: Session, job: DorisSm3Job) -> None:
    session.refresh(job)
    if job.cancel_requested or job.state == "cancelling":
        raise _Sm3Cancelled("任务已取消。")


def _display_step(step: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(step)
    for key in ("title", "message"):
        fixed[key] = _display_text(fixed.get(key))
    return fixed


def _job_display_message(job: DorisSm3Job) -> str:
    text = _display_text(job.message)
    if text and not _looks_lost_text(text):
        return text
    return _fallback_job_message(job)


def _display_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    # Historical rows may contain UTF-8 bytes decoded as latin1/cp1252.
    if any(marker in text for marker in ("Ã", "Â", "è", "æ", "å", "ð")):
        for encoding in ("latin1", "cp1252"):
            try:
                repaired = text.encode(encoding).decode("utf-8")
            except Exception:
                continue
            if _looks_better_text(text, repaired):
                return repaired
    return text


def _looks_better_text(original: str, repaired: str) -> bool:
    original_score = sum(1 for ch in original if "\u4e00" <= ch <= "\u9fff")
    repaired_score = sum(1 for ch in repaired if "\u4e00" <= ch <= "\u9fff")
    return repaired_score > original_score


def _looks_lost_text(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    question_count = compact.count("?") + compact.count("？")
    if question_count >= 8 and "SM3" in compact:
        return True
    return question_count >= 4 and question_count >= max(4, len(compact) // 5)


def _fallback_job_message(job: DorisSm3Job) -> str:
    if job.state == "succeeded":
        if job.table_mode == "replace_original":
            return f"表 {job.table_name} SM3 映射脱敏完成，原表已备份为 {job.backup_table_name}。"
        return f"表 {job.table_name} SM3 映射脱敏完成，原表保持不变，脱敏表为 {job.output_table_name}。"
    if job.state == "failed":
        error = _display_text(job.error_message) or "请查看任务步骤或服务日志。"
        return f"表 {job.table_name} SM3 映射脱敏失败：{error}"
    if job.state == "cancelled":
        return "任务已取消。"
    if job.state == "cancelling":
        return "已请求取消，正在等待当前数据库步骤结束。"
    if job.state == "running":
        return "Worker 正在执行 SM3 脱敏任务。"
    return "任务已提交，等待 Worker 执行。"


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _s(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
