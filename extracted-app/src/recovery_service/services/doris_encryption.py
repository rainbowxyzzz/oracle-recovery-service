from __future__ import annotations

import re
from copy import deepcopy
import threading
import time
import uuid
from calendar import monthrange
from fnmatch import fnmatch
from datetime import datetime, timedelta
from typing import Any

import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from recovery_service.api.schemas.doris_encryption import (
    DEFAULT_DORIS_ENCRYPTION_KEYWORDS,
    DorisSm4AutoSnapshotResponse,
    DorisSm4AutoSnapshotTaskStatus,
    DorisSm4BatchStatus,
    DorisSm4BatchTableResult,
    DorisSm4BatchTableSpec,
    DorisSm4ScheduleResponse,
    DorisSm4TaskDefinitionResponse,
    DorisSm4TaskReference,
    DorisSm4TaskReferenceResponse,
    DorisEncryptionCatalogResponse,
    DorisEncryptionColumn,
    DorisEncryptionTable,
    DorisEncryptionTaskStatus,
    DorisEncryptionTaskStep,
)
from recovery_service.common.logging import get_logger
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    DataPlatformWorkflow,
    DataPlatformWorkflowVersion,
    DatabaseConnectionProfile,
    DorisSm3TaskLog,
    DorisSm4AutoSnapshotTask,
    DorisSm4BatchJob,
    DorisSm4Schedule,
    DorisSm4TaskDefinition,
    DorisSm4TaskDefinitionRevision,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.services.doris_mask_metadata import (
    finish_mask_task,
    latest_mask_assets_for_catalog,
    mask_asset_sort_priority,
    record_field_mappings,
    register_mask_task,
)
from recovery_service.services.sm4_key_versions import get_sm4_key_seed_for_batch, resolve_sm4_key_version_for_batch
from recovery_service.services.doris_table_ddl import rewrite_table_replication_allocation
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_DORIS_SYSTEM_DATABASES = {"__internal_schema", "information_schema", "mysql", "performance_schema", "sys"}
_CREATE_TABLE_RE = re.compile(
    r"(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)((?:`[^`]+`\.)?`[^`]+`|(?:[^\s.(]+\.)?[^\s.(]+)"
)
_COLUMN_DEF_RE = re.compile(
    r"(?im)^(\s*`(?P<name>(?:``|[^`])+)`\s+)(?P<type>[A-Za-z][A-Za-z0-9_]*(?:\s*\([^)]*\))?)(?P<rest>.*)$"
)

_TASKS: dict[uuid.UUID, DorisEncryptionTaskStatus] = {}
_TASK_LOCK = threading.Lock()
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_LOCK = threading.Lock()
logger = get_logger(__name__)


def list_doris_source_databases(profile: DatabaseConnectionProfile) -> list[str]:
    _ensure_doris_profile(profile)
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            cur.execute("SHOW DATABASES")
            rows = cur.fetchall()
    databases: list[str] = []
    for row in rows:
        name = _first_value(row).strip()
        if not name or name.lower() in _DORIS_SYSTEM_DATABASES:
            continue
        databases.append(name)
    return sorted(set(databases), key=lambda item: item.lower())


def _first_value(row: Any) -> str:
    if isinstance(row, dict):
        for value in row.values():
            if value is not None:
                return str(value)
        return ""
    if isinstance(row, (list, tuple)) and row:
        return str(row[0])
    return str(row or "")


def list_doris_encryption_catalog(
    profile: DatabaseConnectionProfile,
    *,
    database: str | None,
    keywords: list[str] | None,
) -> DorisEncryptionCatalogResponse:
    _ensure_doris_profile(profile)
    target_database = (database or profile.database or "").strip()
    if not target_database:
        raise ValueError("请先选择或填写 Doris 数据库。")
    clean_keywords = _clean_keywords(keywords)
    mask_assets = _latest_mask_assets_for_catalog(profile.id, target_database)
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
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
            DorisEncryptionColumn(
                name=column_name,
                type=str(row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or ""),
                ordinal_position=_safe_int(row.get("ORDINAL_POSITION") or row.get("ordinal_position")),
                matched_keywords=matched,
                selected=bool(matched),
            )
        )

    tables: list[DorisEncryptionTable] = []
    for name, columns in table_map.items():
        asset = mask_assets.get(name)
        tables.append(
            DorisEncryptionTable(
                name=name,
                columns=columns,
                selected_count=sum(1 for column in columns if column.selected),
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
    tables.sort(key=lambda item: (mask_asset_sort_priority(mask_assets.get(item.name)), item.name))
    return DorisEncryptionCatalogResponse(database=target_database, keywords=clean_keywords, tables=tables)


def create_encryption_task(
    profile: DatabaseConnectionProfile,
    *,
    database: str,
    table_name: str,
    columns: list[str],
    backup_suffix: str | None = None,
    table_mode: str = "replace_original",
) -> DorisEncryptionTaskStatus:
    _ensure_doris_profile(profile)
    clean_database = database.strip()
    clean_table = table_name.strip()
    clean_columns = [item.strip() for item in columns if item and item.strip()]
    clean_table_mode = table_mode if table_mode in {"replace_original", "create_suffixed"} else "replace_original"
    if not clean_database:
        raise ValueError("请填写 Doris 数据库。")
    if not clean_table:
        raise ValueError("请填写要加密的表。")
    if not clean_columns:
        raise ValueError("请至少选择一个要加密的字段。")
    task_id = uuid.uuid4()
    default_suffix_prefix = "origin" if clean_table_mode == "replace_original" else "sm4"
    suffixed_table = _suffixed_table_name(clean_table, backup_suffix, default_suffix_prefix)
    backup_table = suffixed_table if clean_table_mode == "replace_original" else None
    output_table = clean_table if clean_table_mode == "replace_original" else suffixed_table
    rename_step_title = "重命名原表" if clean_table_mode == "replace_original" else "读取原表 DDL"
    task = DorisEncryptionTaskStatus(
        task_id=task_id,
        state="running",
        message="加密任务已开始。",
        database=clean_database,
        table_name=clean_table,
        table_mode=clean_table_mode,  # type: ignore[arg-type]
        backup_table_name=backup_table,
        output_table_name=output_table,
        encrypted_columns=clean_columns,
        created_at=app_now(),
        steps=[
            DorisEncryptionTaskStep(title="预检查", state="pending"),
            DorisEncryptionTaskStep(title=rename_step_title, state="pending"),
            DorisEncryptionTaskStep(title="创建新表", state="pending"),
            DorisEncryptionTaskStep(title="写入加密数据", state="pending"),
            DorisEncryptionTaskStep(title="校验行数", state="pending"),
        ],
    )
    with _TASK_LOCK:
        _TASKS[task_id] = task
    _register_sm4_mask_assets(
        profile,
        task_id=task_id,
        database=clean_database,
        source_table=clean_table,
        output_table=output_table,
        backup_table=backup_table,
        table_mode=clean_table_mode,
        columns=clean_columns,
        status="running",
    )
    thread = threading.Thread(
        target=_run_encryption_task,
        args=(task_id, profile, clean_database, clean_table, output_table, backup_table, clean_columns, clean_table_mode),
        daemon=True,
    )
    thread.start()
    return task


def get_encryption_task(task_id: uuid.UUID) -> DorisEncryptionTaskStatus:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
    if not task:
        raise KeyError("Doris 加密任务不存在或已过期。")
    return task


def create_sm4_batch_task(
    profile: DatabaseConnectionProfile,
    **kwargs: Any,
) -> DorisSm4BatchStatus:
    from recovery_service.services.sm4_runtime_guard import sm4_database_guard

    database = str(kwargs.get("database") or "").strip()
    if not database:
        raise ValueError("请填写 Doris 数据库。")
    with sm4_database_guard(profile.id, database):
        return _create_sm4_batch_task(profile, **kwargs)


def _create_sm4_batch_task(
    profile: DatabaseConnectionProfile,
    *,
    database: str,
    tables: list[dict[str, Any]],
    table_strategy: str = "drop_recreate",
    target_suffix: str | None = None,
    schedule_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    execution_window_enabled: bool = False,
    execution_window_start: str | None = None,
    execution_window_end: str | None = None,
    allow_running_cross_window: bool = True,
    auto_snapshot: bool = False,
    auto_snapshot_config: dict[str, Any] | None = None,
    actor: AuthContext | None = None,
) -> DorisSm4BatchStatus:
    _ensure_doris_profile(profile)
    clean_database = (database or "").strip()
    if not clean_database:
        raise ValueError("请填写 Doris 数据库。")
    clean_strategy = _clean_sm4_table_strategy(table_strategy)
    clean_suffix = _clean_optional_identifier(target_suffix, "目标表后缀")
    clean_window_enabled = bool(execution_window_enabled)
    clean_window_start = _clean_execution_window_time(execution_window_start or "22:00") if clean_window_enabled else None
    clean_window_end = _clean_execution_window_time(execution_window_end or "09:00") if clean_window_enabled else None
    clean_tables = _normalize_batch_tables(tables)
    if not clean_tables:
        raise ValueError("请至少选择一张要加密的表。")

    now = app_now()
    key_version = resolve_sm4_key_version_for_batch(
        key_id=key_id,
        connection_id=profile.id,
        database=clean_database,
    )
    if not key_version:
        raise ValueError("未找到可绑定的 SM4 密钥版本，请先刷新 SM4 函数密钥后再提交加密任务。")
    try:
        from recovery_service.services.doris_sm4_function import ensure_sm4_key_version_jar

        ensure_sm4_key_version_jar(key_version.key_id, key_version.jar_filename)
    except Exception as exc:
        raise ValueError(
            f"SM4 密钥版本 {key_version.key_fingerprint} 的 JAR 不可用，自动恢复失败：{exc}"
        ) from exc
    job = DorisSm4BatchJob(
        id=uuid.uuid4(),
        schedule_id=schedule_id,
        connection_id=profile.id,
        connection_name=profile.name,
        database=clean_database,
        sm4_key_version_id=key_version.key_id if key_version else None,
        sm4_key_fingerprint=key_version.key_fingerprint if key_version else None,
        table_strategy=clean_strategy,
        target_suffix=clean_suffix,
        execution_window_enabled=clean_window_enabled,
        execution_window_start=clean_window_start,
        execution_window_end=clean_window_end,
        allow_running_cross_window=bool(allow_running_cross_window),
        auto_snapshot=bool(auto_snapshot),
        auto_snapshot_config=auto_snapshot_config or None,
        tables=clean_tables,
        results=[
            {
                "table_name": item["table_name"],
                "target_database": item.get("target_database") or clean_database,
                "target_table": _planned_target_table(item["table_name"], item.get("target_table"), clean_suffix, clean_strategy),
                "columns": item["columns"],
                "state": "queued",
            }
            for item in clean_tables
        ],
        total_count=len(clean_tables),
        success_count=0,
        failed_count=0,
        state="queued",
        message=f"SM4 批次任务已提交，共 {len(clean_tables)} 张表。",
        created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(job)
        session.commit()
        session.refresh(job)
        status = _sm4_batch_to_status(job)
    finally:
        session.close()

    _add_sm4_log(
        job.id,
        "INFO",
        "queued",
        "SM4 batch job queued.",
        connection_id=profile.id,
        database=clean_database,
        payload={
            "table_count": len(clean_tables),
            "schedule_id": str(schedule_id) if schedule_id else None,
            "sm4_key_version_id": str(key_version.key_id) if key_version else None,
            "sm4_key_fingerprint": key_version.key_fingerprint if key_version else None,
            "execution_window_enabled": clean_window_enabled,
            "execution_window_start": clean_window_start,
            "execution_window_end": clean_window_end,
            "auto_snapshot": bool(auto_snapshot),
        },
    )
    return status


def create_sm4_auto_snapshot_task(
    profile: DatabaseConnectionProfile,
    *,
    name: str | None = None,
    include_databases: list[str] | None = None,
    exclude_databases: list[str] | None = None,
    exclude_tables: list[str] | None = None,
    keywords: list[str] | None = None,
    table_strategy: str = "drop_recreate",
    target_suffix: str | None = "sm4",
    execution_window_enabled: bool = True,
    execution_window_start: str = "22:00",
    execution_window_end: str = "09:00",
    allow_running_cross_window: bool = True,
    scan_interval_minutes: int = 60,
    actor: AuthContext | None = None,
) -> DorisSm4AutoSnapshotResponse:
    _ensure_doris_profile(profile)
    clean_strategy = _clean_sm4_table_strategy(table_strategy)
    if clean_strategy != "drop_recreate":
        raise ValueError("自动快照加密固定使用删除目标表后重建策略。")
    now = app_now()
    clean_keywords = _clean_keywords(keywords)
    include_patterns = _clean_database_patterns(include_databases)
    exclude_patterns = _clean_database_patterns(exclude_databases)
    exclude_table_patterns = _clean_table_patterns(exclude_tables)
    clean_window_start = _clean_execution_window_time(execution_window_start or "22:00")
    clean_window_end = _clean_execution_window_time(execution_window_end or "09:00")
    clean_interval = max(1, min(1440, int(scan_interval_minutes or 60)))
    snapshot = _capture_doris_auto_snapshot(
        profile,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        exclude_table_patterns=exclude_table_patterns,
        keywords=clean_keywords,
    )
    clean_name = (name or "").strip() or f"SM4 自动快照 {profile.name or profile.host}"
    task = DorisSm4AutoSnapshotTask(
        id=uuid.uuid4(),
        name=clean_name[:128],
        connection_id=profile.id,
        connection_name=profile.name,
        include_databases=include_patterns,
        exclude_databases=exclude_patterns,
        exclude_tables=exclude_table_patterns,
        keywords=clean_keywords,
        table_strategy="drop_recreate",
        target_suffix=_clean_optional_identifier(target_suffix, "目标表后缀"),
        execution_window_enabled=bool(execution_window_enabled),
        execution_window_start=clean_window_start if execution_window_enabled else None,
        execution_window_end=clean_window_end if execution_window_enabled else None,
        allow_running_cross_window=bool(allow_running_cross_window),
        scan_interval_minutes=clean_interval,
        snapshot=snapshot,
        last_scan_at=now,
        next_scan_at=now + timedelta(minutes=clean_interval),
        enabled=True,
        state="active",
        message=f"已采集首次基线：{snapshot.get('database_count', 0)} 个库，{snapshot.get('table_count', 0)} 张表；暂不执行加密。",
        created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(task)
        session.commit()
        session.refresh(task)
    finally:
        session.close()
    return DorisSm4AutoSnapshotResponse(
        task_id=task.id,
        batch_ids=[],
        batches=[],
        database_count=int(snapshot.get("database_count", 0) or 0),
        table_count=int(snapshot.get("table_count", 0) or 0),
        changed_table_count=0,
        message=task.message,
    )


def list_sm4_auto_snapshot_tasks(*, limit: int = 50) -> list[DorisSm4AutoSnapshotTaskStatus]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DorisSm4AutoSnapshotTask)
            .order_by(desc(DorisSm4AutoSnapshotTask.created_at))
            .limit(limit)
        ).scalars().all()
        return [_sm4_auto_snapshot_task_to_status(row) for row in rows]
    finally:
        session.close()


def update_sm4_auto_snapshot_task_interval(task_id: uuid.UUID, *, scan_interval_minutes: int) -> DorisSm4AutoSnapshotTaskStatus:
    clean_interval = max(1, min(1440, int(scan_interval_minutes or 60)))
    now = app_now()
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4AutoSnapshotTask, task_id)
        if not task:
            raise KeyError("SM4 自动快照任务不存在。")
        task.scan_interval_minutes = clean_interval
        task.next_scan_at = now + timedelta(minutes=clean_interval)
        task.updated_at = now
        task.message = f"巡检间隔已调整为 {clean_interval} 分钟。"
        session.commit()
        session.refresh(task)
        return _sm4_auto_snapshot_task_to_status(task)
    finally:
        session.close()


def delete_sm4_auto_snapshot_task(task_id: uuid.UUID) -> DorisSm4AutoSnapshotTaskStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4AutoSnapshotTask, task_id)
        if not task:
            raise KeyError("SM4 自动快照任务不存在。")
        status = _sm4_auto_snapshot_task_to_status(task)
        session.delete(task)
        session.commit()
        return status
    finally:
        session.close()


def run_sm4_auto_snapshot_task_now(task_id: uuid.UUID) -> DorisSm4AutoSnapshotResponse:
    now = app_now()
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4AutoSnapshotTask, task_id)
        if not task:
            raise KeyError("SM4 自动快照任务不存在。")
        profile = session.get(DatabaseConnectionProfile, task.connection_id)
        if not profile:
            raise ValueError("Doris 连接不存在，无法巡检。")
        batches, changed_tables, snapshot = _scan_sm4_auto_snapshot_task(profile, task, now=now)
        interval = max(1, min(1440, int(task.scan_interval_minutes or 60)))
        task.snapshot = snapshot
        task.last_scan_at = now
        task.next_scan_at = now + timedelta(minutes=interval)
        task.updated_at = now
        if changed_tables:
            task.last_change_at = now
            task.message = f"立刻巡检发现 {len(changed_tables)} 张表发生变化，已生成 {len(batches)} 个 SM4 批次。"
        else:
            task.message = f"立刻巡检完成：{snapshot.get('database_count', 0)} 个库，{snapshot.get('table_count', 0)} 张表，未发现变化。"
        session.commit()
        return DorisSm4AutoSnapshotResponse(
            task_id=task.id,
            batch_ids=[batch.batch_id for batch in batches],
            batches=batches,
            database_count=int(snapshot.get("database_count", 0) or 0),
            table_count=int(snapshot.get("table_count", 0) or 0),
            changed_table_count=len(changed_tables),
            message=task.message,
        )
    finally:
        session.close()


def list_sm4_batch_tasks(
    *,
    connection_id: uuid.UUID | None = None,
    database: str | None = None,
    schedule_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[DorisSm4BatchStatus]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DorisSm4BatchJob).order_by(desc(DorisSm4BatchJob.created_at)).limit(limit)
        if connection_id:
            stmt = stmt.where(DorisSm4BatchJob.connection_id == connection_id)
        if database:
            stmt = stmt.where(DorisSm4BatchJob.database == database)
        if schedule_id:
            stmt = stmt.where(DorisSm4BatchJob.schedule_id == schedule_id)
        return [_sm4_batch_to_status(row) for row in session.execute(stmt).scalars().all()]
    finally:
        session.close()


def get_sm4_batch_task(batch_id: uuid.UUID) -> DorisSm4BatchStatus:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            raise KeyError("Doris SM4 批次任务不存在。")
        return _sm4_batch_to_status(job)
    finally:
        session.close()


def stop_sm4_batch_task(batch_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4BatchStatus:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            raise KeyError("Doris SM4 batch job does not exist.")
        if job.state in {"succeeded", "failed", "partial", "stopped", "cancelled"}:
            return _sm4_batch_to_status(job)
        now = app_now()
        if job.state in {"queued", "reserved"}:
            job.state = "cancelled"
            job.message = "SM4 batch job was cancelled before execution."
            job.finished_at = now
            job.updated_at = now
            results = []
            for item in job.results or []:
                updated = dict(item)
                if updated.get("state") == "queued":
                    updated["state"] = "cancelled"
                    updated["message"] = "Task was cancelled before execution."
                    updated["finished_at"] = now.isoformat()
                results.append(updated)
            job.results = results
        else:
            job.state = "stopping"
            job.message = "SM4 batch job is stopping. Running Doris SQL will be interrupted when possible."
            job.updated_at = now
            _try_stop_running_sm4_queries(session, job)
        session.commit()
        _add_sm4_log(
            job.id,
            "CANCEL" if job.state == "cancelled" else "INFO",
            "stop_requested",
            f"Stop requested by {actor.username if actor else 'api-key'}.",
            connection_id=job.connection_id,
            database=job.database,
            payload={"state": job.state},
        )
        session.refresh(job)
        return _sm4_batch_to_status(job)
    finally:
        session.close()


def stop_sm4_schedule(
    schedule_id: uuid.UUID,
    actor: AuthContext | None = None,
    reason: str | None = None,
) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 schedule does not exist.")
        if schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be changed.")
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.updated_at = app_now()
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def resume_sm4_schedule(schedule_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 schedule does not exist.")
        if schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be resumed.")
        if schedule.archived_at is not None:
            raise ValueError("Archived schedules must be restored before resuming.")
        now = app_now()
        schedule.enabled = True
        schedule.next_run_at = _next_schedule_time(
            schedule.schedule_type,
            schedule.run_time,
            day_of_month=schedule.day_of_month,
            day_of_week=schedule.day_of_week,
            interval_minutes=schedule.interval_minutes,
            after=now,
        )
        schedule.updated_at = now
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def archive_sm4_schedule(
    schedule_id: uuid.UUID,
    actor: AuthContext | None = None,
    reason: str | None = None,
) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 schedule does not exist.")
        if schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be archived.")
        now = app_now()
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.archived_at = now
        schedule.archived_by_user_id = _auth_user_uuid(actor)
        schedule.archived_by_username = actor.username if actor else None
        schedule.archived_reason = _clean_lifecycle_reason(reason)
        schedule.updated_at = now
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def restore_sm4_schedule(schedule_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 schedule does not exist.")
        if schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be restored from archive.")
        schedule.archived_at = None
        schedule.archived_by_user_id = None
        schedule.archived_by_username = None
        schedule.archived_reason = None
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.updated_at = app_now()
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def delete_sm4_schedule(
    schedule_id: uuid.UUID,
    actor: AuthContext | None = None,
    reason: str | None = None,
) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 schedule does not exist.")
        if schedule.deleted_at is not None:
            return _sm4_schedule_to_response(schedule)
        now = app_now()
        schedule.enabled = False
        schedule.next_run_at = None
        schedule.deleted_at = now
        schedule.deleted_by_user_id = _auth_user_uuid(actor)
        schedule.deleted_by_username = actor.username if actor else None
        schedule.delete_reason = _clean_lifecycle_reason(reason)
        schedule.updated_at = now
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def create_sm4_schedule(
    profile: DatabaseConnectionProfile,
    *,
    name: str,
    database: str,
    tables: list[dict[str, Any]],
    table_strategy: str,
    target_suffix: str | None,
    schedule_type: str,
    run_time: str,
    day_of_month: int | None,
    day_of_week: int | None,
    interval_minutes: int | None,
    enabled: bool,
    actor: AuthContext | None = None,
) -> DorisSm4ScheduleResponse:
    _ensure_doris_profile(profile)
    clean_name = (name or "").strip()
    clean_database = (database or "").strip()
    if not clean_name:
        raise ValueError("请填写计划名称。")
    if not clean_database:
        raise ValueError("请填写 Doris 数据库。")
    clean_tables = _normalize_batch_tables(tables)
    if not clean_tables:
        raise ValueError("请至少选择一张表后再保存计划。")
    clean_strategy = _clean_sm4_table_strategy(table_strategy)
    clean_suffix = _clean_optional_identifier(target_suffix, "目标表后缀")
    clean_schedule_type = _clean_schedule_type(schedule_type)
    clean_run_time = _clean_run_time(run_time)
    now = app_now()
    schedule = DorisSm4Schedule(
        id=uuid.uuid4(),
        name=clean_name,
        connection_id=profile.id,
        connection_name=profile.name,
        database=clean_database,
        table_strategy=clean_strategy,
        target_suffix=clean_suffix,
        tables=clean_tables,
        schedule_type=clean_schedule_type,
        run_time=clean_run_time,
        day_of_month=day_of_month,
        day_of_week=day_of_week,
        interval_minutes=interval_minutes,
        enabled=enabled,
        next_run_at=_next_schedule_time(
            clean_schedule_type,
            clean_run_time,
            day_of_month=day_of_month,
            day_of_week=day_of_week,
            interval_minutes=interval_minutes,
            after=now,
        )
        if enabled
        else None,
        created_by_user_id=uuid.UUID(actor.user_id) if actor and actor.user_id else None,
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def list_sm4_schedules(status: str = "normal") -> list[DorisSm4ScheduleResponse]:
    session = get_sync_session_factory()()
    try:
        clean_status = (status or "normal").strip().lower()
        stmt = select(DorisSm4Schedule).order_by(desc(DorisSm4Schedule.created_at)).limit(200)
        if clean_status == "normal":
            stmt = stmt.where(DorisSm4Schedule.archived_at.is_(None), DorisSm4Schedule.deleted_at.is_(None))
        elif clean_status == "active":
            stmt = stmt.where(
                DorisSm4Schedule.enabled == True,  # noqa: E712
                DorisSm4Schedule.archived_at.is_(None),
                DorisSm4Schedule.deleted_at.is_(None),
            )
        elif clean_status == "paused":
            stmt = stmt.where(
                DorisSm4Schedule.enabled == False,  # noqa: E712
                DorisSm4Schedule.archived_at.is_(None),
                DorisSm4Schedule.deleted_at.is_(None),
            )
        elif clean_status == "archived":
            stmt = stmt.where(DorisSm4Schedule.archived_at.is_not(None), DorisSm4Schedule.deleted_at.is_(None))
        elif clean_status == "deleted":
            stmt = stmt.where(DorisSm4Schedule.deleted_at.is_not(None))
        elif clean_status != "all":
            raise ValueError("Unsupported schedule status filter.")
        return [_sm4_schedule_to_response(row) for row in session.execute(stmt).scalars().all()]
    finally:
        session.close()


def update_sm4_schedule(schedule_id: uuid.UUID, *, updates: dict[str, Any]) -> DorisSm4ScheduleResponse:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 定时计划不存在。")
        if schedule and schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be edited.")
        if schedule and schedule.archived_at is not None:
            raise ValueError("Archived schedules must be restored before editing.")
        for field in (
            "name",
            "connection_id",
            "connection_name",
            "database",
            "table_strategy",
            "target_suffix",
            "schedule_type",
            "run_time",
            "day_of_month",
            "day_of_week",
            "interval_minutes",
            "enabled",
        ):
            if field not in updates:
                continue
            value = updates[field]
            if value is None and field in {"name", "table_strategy", "schedule_type", "run_time"}:
                continue
            if field == "name" and value is not None:
                value = str(value).strip()
            elif field == "database" and value is not None:
                value = str(value).strip()
            elif field == "table_strategy" and value is not None:
                value = _clean_sm4_table_strategy(str(value))
            elif field == "target_suffix":
                value = _clean_optional_identifier(value, "目标表后缀")
            elif field == "schedule_type" and value is not None:
                value = _clean_schedule_type(str(value))
            elif field == "run_time" and value is not None:
                value = _clean_run_time(str(value))
            setattr(schedule, field, value)
        if "tables" in updates and updates["tables"] is not None:
            schedule.tables = _normalize_batch_tables(updates["tables"])
        if not schedule.tables:
            raise ValueError("计划至少需要保留一张表。")
        schedule.updated_at = app_now()
        schedule.next_run_at = (
            _next_schedule_time(
                schedule.schedule_type,
                schedule.run_time,
                day_of_month=schedule.day_of_month,
                day_of_week=schedule.day_of_week,
                interval_minutes=schedule.interval_minutes,
                after=app_now(),
            )
            if schedule.enabled
            else None
        )
        session.commit()
        session.refresh(schedule)
        return _sm4_schedule_to_response(schedule)
    finally:
        session.close()


def sm4_task_definition_snapshot(task: DorisSm4TaskDefinition) -> dict[str, Any]:
    return {
        "task_definition_id": str(task.id),
        "revision": int(task.revision or 1),
        "name": task.name,
        "connection_id": str(task.connection_id),
        "connection_name": task.connection_name,
        "database": task.database,
        "tables": deepcopy(task.tables or []),
        "table_strategy": task.table_strategy,
        "target_suffix": task.target_suffix,
    }


def freeze_sm4_task_nodes(session: Session, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frozen_nodes = deepcopy(nodes or [])
    for node in frozen_nodes:
        if node.get("node_type") != "sm4_batch":
            continue
        config = dict(node.get("config") or {})
        task_id = config.get("task_definition_id")
        if not task_id:
            continue
        existing_snapshot = config.get("task_definition_snapshot")
        if isinstance(existing_snapshot, dict) and existing_snapshot:
            config["task_definition_name"] = existing_snapshot.get("name") or config.get("task_definition_name")
            config["task_definition_revision"] = int(existing_snapshot.get("revision") or 1)
            node["config"] = config
            node["name"] = existing_snapshot.get("name") or node.get("name")
            continue
        task = session.get(DorisSm4TaskDefinition, uuid.UUID(str(task_id)))
        if not task or task.archived_at is not None:
            raise ValueError(f"SM4 task definition does not exist: {task_id}")
        snapshot = sm4_task_definition_snapshot(task)
        config["task_definition_name"] = task.name
        config["task_definition_revision"] = snapshot["revision"]
        config["task_definition_snapshot"] = snapshot
        node["config"] = config
        node["name"] = task.name
    return frozen_nodes


def _ensure_sm4_revision_record(
    session: Session,
    task: DorisSm4TaskDefinition,
    snapshot: dict[str, Any],
    actor: AuthContext | None,
) -> None:
    revision = int(snapshot.get("revision") or 1)
    existing = session.scalar(
        select(DorisSm4TaskDefinitionRevision.id).where(
            DorisSm4TaskDefinitionRevision.task_definition_id == task.id,
            DorisSm4TaskDefinitionRevision.revision == revision,
        )
    )
    if existing:
        return
    session.add(
        DorisSm4TaskDefinitionRevision(
            id=uuid.uuid4(),
            task_definition_id=task.id,
            revision=revision,
            snapshot=deepcopy(snapshot),
            created_by_username=actor.username if actor else task.created_by_username,
            created_at=app_now(),
        )
    )


def _freeze_existing_production_references(
    session: Session,
    task: DorisSm4TaskDefinition,
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
            if node.get("node_type") != "sm4_batch":
                continue
            config = dict(node.get("config") or {})
            if str(config.get("task_definition_id") or "") != task_id:
                continue
            if config.get("task_definition_snapshot"):
                continue
            config["task_definition_name"] = snapshot["name"]
            config["task_definition_revision"] = snapshot["revision"]
            config["task_definition_snapshot"] = deepcopy(snapshot)
            node["config"] = config
            changed = True
        if changed:
            version.nodes = nodes
            version.updated_at = app_now()


def get_sm4_task_definition_references(task_id: uuid.UUID) -> DorisSm4TaskReferenceResponse:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM4 task definition does not exist.")
        rows: list[DorisSm4TaskReference] = []
        versions = session.execute(
            select(DataPlatformWorkflowVersion).order_by(
                DataPlatformWorkflowVersion.workflow_id,
                DataPlatformWorkflowVersion.channel,
                DataPlatformWorkflowVersion.version_no,
            )
        ).scalars().all()
        workflow_names: dict[uuid.UUID, str] = {}
        for version in versions:
            matching = []
            for node in version.nodes or []:
                config = node.get("config") or {}
                if node.get("node_type") == "sm4_batch" and str(config.get("task_definition_id") or "") == str(task_id):
                    matching.append(config)
            if not matching:
                continue
            if version.workflow_id not in workflow_names:
                workflow = session.get(DataPlatformWorkflow, version.workflow_id)
                workflow_names[version.workflow_id] = workflow.name if workflow else str(version.workflow_id)
            frozen_revisions = [
                int(item.get("task_definition_revision"))
                for item in matching
                if item.get("task_definition_revision") is not None
            ]
            rows.append(
                DorisSm4TaskReference(
                    workflow_id=version.workflow_id,
                    workflow_name=workflow_names[version.workflow_id],
                    version_id=version.id,
                    version_no=version.version_no,
                    channel=version.channel,
                    status=version.status,
                    schedule_enabled=bool(version.schedule_enabled),
                    next_run_at=version.next_run_at,
                    node_count=len(matching),
                    frozen_revision=max(frozen_revisions) if frozen_revisions else None,
                )
            )
        return DorisSm4TaskReferenceResponse(
            task_id=task.id,
            task_name=task.name,
            revision=int(task.revision or 1),
            development_count=sum(item.node_count for item in rows if item.channel == "dev"),
            production_count=sum(item.node_count for item in rows if item.channel == "prod"),
            online_count=sum(item.node_count for item in rows if item.status == "online"),
            references=rows,
        )
    finally:
        session.close()


def create_sm4_task_definition(
    profile: DatabaseConnectionProfile,
    *,
    name: str,
    database: str,
    tables: list[dict[str, Any]],
    table_strategy: str,
    target_suffix: str | None,
    actor: AuthContext | None = None,
) -> DorisSm4TaskDefinitionResponse:
    _ensure_doris_profile(profile)
    clean_name = (name or "").strip()
    clean_database = (database or "").strip()
    clean_tables = _normalize_batch_tables(tables)
    if not clean_name:
        raise ValueError("Task name is required.")
    if not clean_database:
        raise ValueError("Doris database is required.")
    if not clean_tables:
        raise ValueError("At least one table is required.")
    now = app_now()
    task = DorisSm4TaskDefinition(
        id=uuid.uuid4(),
        name=clean_name,
        revision=1,
        connection_id=profile.id,
        connection_name=profile.name,
        database=clean_database,
        tables=clean_tables,
        table_strategy=_clean_sm4_table_strategy(table_strategy),
        target_suffix=_clean_optional_identifier(target_suffix, "target suffix"),
        created_by_user_id=_auth_user_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "api-key",
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(task)
        _ensure_sm4_revision_record(session, task, sm4_task_definition_snapshot(task), actor)
        session.commit()
        session.refresh(task)
        return _sm4_task_definition_to_response(task)
    finally:
        session.close()


def list_sm4_task_definitions() -> list[DorisSm4TaskDefinitionResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DorisSm4TaskDefinition)
            .where(DorisSm4TaskDefinition.archived_at.is_(None))
            .order_by(desc(DorisSm4TaskDefinition.updated_at))
            .limit(300)
        ).scalars().all()
        return [_sm4_task_definition_to_response(row) for row in rows]
    finally:
        session.close()


def update_sm4_task_definition(
    task_id: uuid.UUID,
    *,
    updates: dict[str, Any],
    actor: AuthContext | None = None,
) -> DorisSm4TaskDefinitionResponse:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM4 task definition does not exist.")
        old_snapshot = sm4_task_definition_snapshot(task)
        _freeze_existing_production_references(session, task, old_snapshot)
        _ensure_sm4_revision_record(session, task, old_snapshot, actor)
        if "name" in updates and updates["name"] is not None:
            task.name = str(updates["name"]).strip()
        if "connection_id" in updates and updates["connection_id"] is not None:
            task.connection_id = updates["connection_id"]
        if "connection_name" in updates:
            task.connection_name = updates["connection_name"]
        if "database" in updates and updates["database"] is not None:
            task.database = str(updates["database"]).strip()
        if "tables" in updates and updates["tables"] is not None:
            task.tables = _normalize_batch_tables(updates["tables"])
        if "table_strategy" in updates and updates["table_strategy"] is not None:
            task.table_strategy = _clean_sm4_table_strategy(updates["table_strategy"])
        if "target_suffix" in updates:
            task.target_suffix = _clean_optional_identifier(updates["target_suffix"], "target suffix")
        if not task.name:
            raise ValueError("Task name is required.")
        if not task.tables:
            raise ValueError("At least one table is required.")
        updated_snapshot = sm4_task_definition_snapshot(task)
        old_content = {key: value for key, value in old_snapshot.items() if key != "revision"}
        updated_content = {key: value for key, value in updated_snapshot.items() if key != "revision"}
        if updated_content != old_content:
            task.revision = int(task.revision or 1) + 1
            task.updated_at = app_now()
            _ensure_sm4_revision_record(session, task, sm4_task_definition_snapshot(task), actor)
        session.commit()
        session.refresh(task)
        return _sm4_task_definition_to_response(task)
    finally:
        session.close()


def archive_sm4_task_definition(task_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4TaskDefinitionResponse:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM4 task definition does not exist.")
        snapshot = sm4_task_definition_snapshot(task)
        _freeze_existing_production_references(session, task, snapshot)
        _ensure_sm4_revision_record(session, task, snapshot, actor)
        task.archived_at = app_now()
        task.archived_by_username = actor.username if actor else None
        task.updated_at = app_now()
        session.commit()
        session.refresh(task)
        return _sm4_task_definition_to_response(task)
    finally:
        session.close()


def run_sm4_task_definition(task_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4BatchStatus:
    session = get_sync_session_factory()()
    try:
        task = session.get(DorisSm4TaskDefinition, task_id)
        if not task or task.archived_at is not None:
            raise KeyError("Doris SM4 task definition does not exist.")
        profile = session.get(DatabaseConnectionProfile, task.connection_id)
        if not profile:
            raise ValueError("Task connection does not exist.")
        return create_sm4_batch_task(
            profile,
            database=task.database,
            tables=task.tables,
            table_strategy=task.table_strategy,
            target_suffix=task.target_suffix,
            actor=actor,
        )
    finally:
        session.close()


def run_sm4_schedule_now(schedule_id: uuid.UUID, actor: AuthContext | None = None) -> DorisSm4BatchStatus:
    session = get_sync_session_factory()()
    try:
        schedule = session.get(DorisSm4Schedule, schedule_id)
        if not schedule:
            raise KeyError("Doris SM4 定时计划不存在。")
        if schedule and schedule.deleted_at is not None:
            raise ValueError("Deleted schedules cannot be run.")
        if schedule and schedule.archived_at is not None:
            raise ValueError("Archived schedules must be restored before running.")
        profile = session.get(DatabaseConnectionProfile, schedule.connection_id)
        if not profile:
            raise ValueError("计划关联的数据连接不存在。")
        schedule.last_run_at = app_now()
        schedule.next_run_at = (
            _next_schedule_time(
                schedule.schedule_type,
                schedule.run_time,
                day_of_month=schedule.day_of_month,
                day_of_week=schedule.day_of_week,
                interval_minutes=schedule.interval_minutes,
                after=app_now(),
            )
            if schedule.enabled
            else None
        )
        session.commit()
        return create_sm4_batch_task(
            profile,
            database=schedule.database,
            tables=schedule.tables,
            table_strategy=schedule.table_strategy,
            target_suffix=schedule.target_suffix,
            schedule_id=schedule.id,
            actor=actor,
        )
    finally:
        session.close()


def start_sm4_scheduler() -> None:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        _recover_interrupted_sm4_jobs_on_startup()
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(target=_sm4_scheduler_loop, daemon=True)
        _SCHEDULER_THREAD.start()


def stop_sm4_scheduler() -> None:
    _SCHEDULER_STOP.set()


def dispatch_queued_sm4_jobs_once() -> None:
    _dispatch_queued_sm4_jobs()


def run_sm4_batch_job(batch_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            return {"state": "failed", "message": "SM4 batch job not found."}
        profile = session.get(DatabaseConnectionProfile, job.connection_id)
        if not profile:
            job.state = "failed"
            job.message = "Doris connection profile not found."
            job.error_message = job.message
            job.finished_at = app_now()
            job.updated_at = app_now()
            session.commit()
            return {"state": "failed", "message": job.message}
        _add_sm4_log(job.id, "INFO", "worker_start", "SM4 worker picked up batch job.", connection_id=job.connection_id, database=job.database)
    finally:
        session.close()
    _run_sm4_batch_task(batch_id, profile)
    status = get_sm4_batch_task(batch_id)
    return {"state": status.state, "message": status.message}


def _run_encryption_task(
    task_id: uuid.UUID,
    profile: DatabaseConnectionProfile,
    database: str,
    table_name: str,
    output_table: str,
    backup_table: str | None,
    encrypted_columns: list[str],
    table_mode: str,
) -> None:
    renamed = False
    created_new_table = False
    source_table = table_name
    rename_original = table_mode == "replace_original"
    try:
        logger.info(
            "doris encryption task started",
            task_id=str(task_id),
            database=database,
            table_name=table_name,
            output_table=output_table,
            backup_table=backup_table,
            encrypted_columns=encrypted_columns,
            table_mode=table_mode,
        )
        _set_step(task_id, 0, "running")
        with _doris_conn(profile, database) as db:
            with db.cursor() as cur:
                table_columns = _load_table_columns(cur, database, table_name)
                logger.info(
                    "doris encryption loaded table columns",
                    task_id=str(task_id),
                    database=database,
                    table_name=table_name,
                    table_columns=table_columns,
                )
                if not table_columns:
                    raise ValueError(f"表 {table_name} 不存在或没有字段。")
                unknown_columns = [name for name in encrypted_columns if name not in table_columns]
                if unknown_columns:
                    raise ValueError(f"字段不存在：{', '.join(unknown_columns)}")
                if rename_original and backup_table and _table_exists(cur, database, backup_table):
                    raise ValueError(f"备份表 {backup_table} 已存在，请换一个备份后缀。")
                if not rename_original and _table_exists(cur, database, output_table):
                    raise ValueError(f"加密目标表 {output_table} 已存在，请换一个加密表后缀。")
                source_rows = _count_rows(cur, database, table_name)
                _update_task(task_id, source_rows=source_rows)
                _set_step(task_id, 0, "success", f"原表 {source_rows} 行，待加密 {len(encrypted_columns)} 个字段。")
                logger.info(
                    "doris encryption precheck passed",
                    task_id=str(task_id),
                    database=database,
                    table_name=table_name,
                    output_table=output_table,
                    source_rows=source_rows,
                    encrypted_columns=encrypted_columns,
                    table_mode=table_mode,
                )

                if rename_original:
                    if not backup_table:
                        raise ValueError("覆盖原表模式缺少备份表名。")
                    rename_sql = f"ALTER TABLE {_q(database)}.{_q(table_name)} RENAME {_q(backup_table)}"
                    logger.info("doris encryption rename original table", task_id=str(task_id), sql=rename_sql)
                    _set_step(task_id, 1, "running", sql=rename_sql)
                    cur.execute(rename_sql)
                    renamed = True
                    source_table = backup_table
                    _set_step(task_id, 1, "success", f"原表已重命名为 {backup_table}。", sql=rename_sql)
                    logger.info(
                        "doris encryption original table renamed",
                        task_id=str(task_id),
                        database=database,
                        table_name=table_name,
                        backup_table=backup_table,
                    )
                else:
                    _set_step(task_id, 1, "running", f"原表保持不变，将创建加密表 {output_table}。")
                    source_table = table_name
                    _set_step(task_id, 1, "success", f"原表保持不变，目标加密表为 {output_table}。")
                    logger.info(
                        "doris encryption keep original table",
                        task_id=str(task_id),
                        database=database,
                        table_name=table_name,
                        output_table=output_table,
                    )

                ddl = _show_create_table(cur, database, source_table)
                logger.info(
                    "doris encryption original ddl loaded",
                    task_id=str(task_id),
                    database=database,
                    source_table=source_table,
                    ddl=ddl,
                )
                create_sql = _replace_create_table_name(ddl, output_table)
                create_sql = _rewrite_encrypted_column_types(create_sql, encrypted_columns)
                create_sql = rewrite_table_replication_allocation(create_sql)
                logger.info(
                    "doris encryption create table sql prepared",
                    task_id=str(task_id),
                    database=database,
                    table_name=table_name,
                    output_table=output_table,
                    encrypted_columns=encrypted_columns,
                    sql=create_sql,
                )
                _set_step(task_id, 2, "running", sql=create_sql)
                cur.execute(create_sql)
                created_new_table = True
                _set_step(task_id, 2, "success", "新表已按原 DDL 创建，待加密字段已转为 varchar(2000)。", sql=create_sql)
                logger.info(
                    "doris encryption new table created",
                    task_id=str(task_id),
                    database=database,
                    table_name=output_table,
                )

                insert_sql = _build_insert_sql(database, output_table, source_table, table_columns, encrypted_columns)
                logger.info(
                    "doris encryption insert encrypted data sql prepared",
                    task_id=str(task_id),
                    database=database,
                    table_name=output_table,
                    source_table=source_table,
                    sql=insert_sql,
                )
                _set_step(task_id, 3, "running", sql=insert_sql)
                cur.execute(insert_sql)
                _set_step(task_id, 3, "success", "加密数据已写入新表。", sql=insert_sql)
                logger.info(
                    "doris encryption encrypted data inserted",
                    task_id=str(task_id),
                    database=database,
                    table_name=output_table,
                    source_table=source_table,
                )

                target_rows = _count_rows(cur, database, output_table)
                _update_task(task_id, target_rows=target_rows)
                logger.info(
                    "doris encryption row count checked",
                    task_id=str(task_id),
                    database=database,
                    table_name=table_name,
                    output_table=output_table,
                    source_rows=source_rows,
                    target_rows=target_rows,
                )
                if source_rows != target_rows:
                    raise ValueError(f"行数校验失败：原表 {source_rows} 行，新表 {target_rows} 行。")
                _set_step(task_id, 4, "success", f"行数一致：{target_rows} 行。")
        if rename_original:
            _finish_task(task_id, "succeeded", f"表 {table_name} 加密完成，原表已备份为 {backup_table}。")
        else:
            _finish_task(task_id, "succeeded", f"表 {table_name} 加密完成，原表保持不变，加密表为 {output_table}。")
        _finish_sm4_mask_assets(task_id, "succeeded", _TASKS[task_id].message)
        _record_sm4_field_mappings(
            profile,
            task_id=task_id,
            database=database,
            source_table=table_name,
            output_table=output_table,
            columns=encrypted_columns,
        )
        logger.info(
            "doris encryption task succeeded",
            task_id=str(task_id),
            database=database,
            table_name=table_name,
            output_table=output_table,
            backup_table=backup_table,
            table_mode=table_mode,
        )
    except Exception as exc:
        logger.error(
            "doris encryption task failed",
            task_id=str(task_id),
            database=database,
            table_name=table_name,
            output_table=output_table,
            backup_table=backup_table,
            renamed=renamed,
            created_new_table=created_new_table,
            table_mode=table_mode,
            error=str(exc),
            exc_info=True,
        )
        _mark_current_step_failed(task_id, str(exc))
        if renamed and not created_new_table and backup_table:
            _try_rollback_rename(profile, database, table_name, backup_table)
        _finish_task(task_id, "failed", f"表 {table_name} 加密失败：{exc}")
        _finish_sm4_mask_assets(task_id, "failed", str(exc))


def _run_sm4_batch_task(batch_id: uuid.UUID, profile: DatabaseConnectionProfile) -> None:
    from recovery_service.services.sm4_runtime_guard import sm4_database_guard

    lookup_session = get_sync_session_factory()()
    try:
        job = lookup_session.get(DorisSm4BatchJob, batch_id)
        if not job:
            return
        database = job.database
        connection_id = job.connection_id
    finally:
        lookup_session.close()
    with sm4_database_guard(connection_id, database, timeout_seconds=30):
        _run_sm4_batch_task_locked(batch_id, profile)


def _run_sm4_batch_task_locked(batch_id: uuid.UUID, profile: DatabaseConnectionProfile) -> None:
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            return
        if job.state in {"cancelled", "stopping", "stopped"}:
            if job.state != "cancelled":
                job.state = "stopped"
                job.finished_at = app_now()
                job.updated_at = app_now()
                session.commit()
            return
        _verify_sm4_batch_key_binding(profile, job)
        job.state = "running"
        job.started_at = app_now()
        job.updated_at = app_now()
        job.message = f"SM4 批次任务执行中，共 {job.total_count} 张表。"
        session.commit()
        _add_sm4_log(job.id, "INFO", "running", job.message, connection_id=job.connection_id, database=job.database)
        tables = list(job.tables or [])
        results = list(job.results or [])
        success_count = 0
        failed_count = 0
        for index, item in enumerate(tables):
            session.refresh(job)
            if job.state in {"stopping", "cancelled", "stopped"}:
                _add_sm4_log(job.id, "CANCEL", "stopped_before_table", "Batch stopped before next table.", connection_id=job.connection_id, database=job.database, table_name=item.get("table_name"))
                _stop_remaining_sm4_batch_tables(session, job, tables, results, index)
                return
            started_at = app_now()
            result = {
                "table_name": item["table_name"],
                "target_database": item.get("target_database") or job.database,
                "target_table": results[index].get("target_table") if index < len(results) else None,
                "columns": item["columns"],
                "state": "running",
                "started_at": started_at.isoformat(),
            }
            results = _replace_result(results, index, result)
            _save_sm4_batch_progress(session, job, results, success_count, failed_count)
            _add_sm4_log(job.id, "INFO", "table_start", f"Start table {item['table_name']}.", connection_id=job.connection_id, database=job.database, table_name=item["table_name"])
            try:
                table_result = _execute_sm4_batch_table(
                    profile,
                    database=job.database,
                    table_name=item["table_name"],
                    columns=item["columns"],
                    table_strategy=job.table_strategy,
                    target_suffix=job.target_suffix,
                    target_database=item.get("target_database"),
                    target_table=item.get("target_table"),
                    batch_id=batch_id,
                )
                table_result["started_at"] = started_at.isoformat()
                table_result["finished_at"] = app_now().isoformat()
                results = _replace_result(results, index, table_result)
                success_count += 1
                _add_sm4_log(job.id, "INFO", "table_success", f"Table {item['table_name']} succeeded.", connection_id=job.connection_id, database=job.database, table_name=item["table_name"], payload=table_result)
            except Exception as exc:
                session.refresh(job)
                stopped_by_user = job.state in {"stopping", "cancelled", "stopped"}
                logger.error(
                    "doris sm4 batch table failed",
                    batch_id=str(batch_id),
                    database=job.database,
                    table_name=item.get("table_name"),
                    error=str(exc),
                    exc_info=True,
                )
                result.update(
                    {
                        "state": "stopped" if stopped_by_user else "failed",
                        "message": "Task was stopped by user." if stopped_by_user else str(exc),
                        "finished_at": app_now().isoformat(),
                    }
                )
                results = _replace_result(results, index, result)
                if stopped_by_user:
                    _add_sm4_log(job.id, "CANCEL", "table_stopped", f"Table {item.get('table_name')} stopped by user.", connection_id=job.connection_id, database=job.database, table_name=item.get("table_name"), error_message=str(exc))
                    job.results = results
                    job.state = "stopped"
                    job.message = "SM4 batch job stopped by user."
                    job.finished_at = app_now()
                    job.updated_at = app_now()
                    session.commit()
                    return
                failed_count += 1
                _add_sm4_log(job.id, "ERROR", "table_failed", f"Table {item.get('table_name')} failed.", connection_id=job.connection_id, database=job.database, table_name=item.get("table_name"), error_message=str(exc))
            _save_sm4_batch_progress(session, job, results, success_count, failed_count)

        if failed_count and success_count:
            job.state = "partial"
            job.message = f"SM4 批次任务部分完成：成功 {success_count} 张，失败 {failed_count} 张。"
        elif failed_count:
            job.state = "failed"
            job.message = f"SM4 批次任务失败：{failed_count} 张表未成功。"
            job.error_message = job.message
        else:
            job.state = "succeeded"
            job.message = f"SM4 批次任务完成：成功 {success_count} 张表。"
        job.finished_at = app_now()
        job.updated_at = app_now()
        session.commit()
        _add_sm4_log(job.id, "INFO" if job.state in {"succeeded", "partial"} else "ERROR", "batch_finished", job.message, connection_id=job.connection_id, database=job.database)
    except Exception as exc:
        logger.error("doris sm4 batch failed", batch_id=str(batch_id), error=str(exc), exc_info=True)
        try:
            job = session.get(DorisSm4BatchJob, batch_id)
            if job:
                job.state = "failed"
                job.message = f"SM4 批次任务失败：{exc}"
                job.error_message = str(exc)
                job.finished_at = app_now()
                job.updated_at = app_now()
                session.commit()
                _add_sm4_log(job.id, "ERROR", "batch_failed", job.message, connection_id=job.connection_id, database=job.database, error_message=str(exc))
        except Exception:
            session.rollback()
    finally:
        session.close()


def _verify_sm4_batch_key_binding(profile: DatabaseConnectionProfile, job: DorisSm4BatchJob) -> None:
    try:
        key_seed, key_id, fingerprint = get_sm4_key_seed_for_batch(job.id)
        from recovery_service.services.doris_sm4_function import sm4_encrypt_to_base64

        plaintext = f"oracle-recovery-sm4-batch-check-{fingerprint}"
        expected = sm4_encrypt_to_base64(plaintext, key_seed)
        with _doris_conn(profile, job.database) as db:
            with db.cursor() as cur:
                cur.execute("SELECT CQ_SM4_ENCRYPT(%s)", (plaintext,))
                actual = _first_value(cur.fetchone()).strip()
        if actual != expected:
            raise ValueError(
                "Doris CQ_SM4_ENCRYPT 与批次绑定密钥不一致，"
                f"批次密钥={fingerprint}，请刷新数据库 {job.database} 的 SM4 函数后重试。"
            )
        _add_sm4_log(
            job.id,
            "INFO",
            "key_function_preflight",
            f"SM4 key/function preflight passed for {fingerprint}.",
            connection_id=job.connection_id,
            database=job.database,
            payload={"sm4_key_version_id": str(key_id), "sm4_key_fingerprint": fingerprint},
        )
    except Exception as exc:
        raise ValueError(
            f"SM4 密钥函数预检失败，尚未执行任何目标表删除或重建操作：{exc}"
        ) from exc


def _execute_sm4_batch_table(
    profile: DatabaseConnectionProfile,
    *,
    database: str,
    table_name: str,
    columns: list[str],
    table_strategy: str,
    target_suffix: str | None,
    target_database: str | None,
    target_table: str | None,
    batch_id: uuid.UUID,
) -> dict[str, Any]:
    clean_table = table_name.strip()
    clean_target_database = (target_database or database).strip()
    clean_columns = [item.strip() for item in columns if item and item.strip()]
    if not clean_columns:
        raise ValueError(f"表 {clean_table} 未选择加密字段。")
    metadata_task_id = uuid.uuid5(batch_id, f"{database}.{clean_table}->{clean_target_database}")
    with _doris_conn(profile, database) as db:
        with db.cursor() as cur:
            db_session_id = _current_doris_session_id(db, cur)
            _save_current_sm4_session(batch_id, clean_table, db_session_id)
            table_columns = _load_table_columns(cur, database, clean_table)
            if not table_columns:
                raise ValueError(f"表 {clean_table} 不存在或没有字段。")
            unknown_columns = [name for name in clean_columns if name not in table_columns]
            if unknown_columns:
                raise ValueError(f"表 {clean_table} 字段不存在：{', '.join(unknown_columns)}")
            output_table = _resolve_sm4_target_table(
                cur,
                database=clean_target_database,
                source_table=clean_table,
                requested_target=target_table,
                target_suffix=target_suffix,
                table_strategy=table_strategy,
            )
            if clean_target_database == database and output_table == clean_table:
                raise ValueError("批次策略不允许源表和目标加密表同名，避免自读自写。")
            source_rows = _count_rows(cur, database, clean_table)
            created_table = False
            if table_strategy == "drop_recreate" and _table_exists(cur, clean_target_database, output_table):
                drop_sql = f"DROP TABLE {_q(clean_target_database)}.{_q(output_table)}"
                logger.info("doris sm4 batch drop target", batch_id=str(batch_id), sql=drop_sql)
                _execute_sm4_logged_sql(
                    cur,
                    drop_sql,
                    batch_id=batch_id,
                    profile=profile,
                    database=database,
                    table_name=clean_table,
                    stage="drop_target",
                    sql_type="DROP_TABLE",
                    db_session_id=db_session_id,
                )
            if table_strategy in {"drop_recreate", "auto_create"} or not _table_exists(cur, clean_target_database, output_table):
                ddl = _show_create_table(cur, database, clean_table)
                create_sql = _replace_create_table_name(ddl, output_table, database=clean_target_database)
                create_sql = _rewrite_encrypted_column_types(create_sql, clean_columns)
                create_sql = rewrite_table_replication_allocation(create_sql)
                logger.info("doris sm4 batch create target", batch_id=str(batch_id), sql=create_sql)
                _execute_sm4_logged_sql(
                    cur,
                    create_sql,
                    batch_id=batch_id,
                    profile=profile,
                    database=database,
                    table_name=clean_table,
                    stage="create_target",
                    sql_type="CREATE_TABLE",
                    db_session_id=db_session_id,
                )
                created_table = True
            insert_sql = _build_insert_sql(database, clean_target_database, output_table, clean_table, table_columns, clean_columns)
            logger.info("doris sm4 batch insert encrypted data", batch_id=str(batch_id), sql=insert_sql)
            _execute_sm4_logged_sql(
                cur,
                insert_sql,
                batch_id=batch_id,
                profile=profile,
                database=database,
                table_name=clean_table,
                stage="insert_encrypted_data",
                sql_type="INSERT_SELECT",
                db_session_id=db_session_id,
            )
            target_rows = _count_rows(cur, clean_target_database, output_table)

    _register_sm4_mask_assets(
        profile,
        task_id=metadata_task_id,
        database=database,
        source_table=clean_table,
        output_table=output_table,
        backup_table=None,
        table_mode=table_strategy,
        columns=clean_columns,
        status="succeeded",
    )
    _finish_sm4_mask_assets(metadata_task_id, "succeeded", f"表 {clean_table} 已写入加密表 {output_table}。")
    _record_sm4_field_mappings(
        profile,
        task_id=metadata_task_id,
        database=database,
        source_table=clean_table,
        output_table=output_table,
        columns=clean_columns,
    )
    expected_message = "追加完成" if table_strategy == "append_existing" and not created_table else "加密表已重建并写入"
    return {
        "table_name": clean_table,
        "target_database": clean_target_database,
        "target_table": output_table,
        "columns": clean_columns,
        "state": "succeeded",
        "message": f"{expected_message}，源表 {source_rows} 行，目标表当前 {target_rows} 行。",
        "source_rows": source_rows,
        "target_rows": target_rows,
    }


def _sm4_scheduler_loop() -> None:
    while not _SCHEDULER_STOP.wait(5):
        try:
            _run_due_sm4_schedules()
            _dispatch_queued_sm4_jobs()
        except Exception as exc:
            logger.error("doris sm4 scheduler tick failed", error=str(exc), exc_info=True)


def _recover_interrupted_sm4_jobs_on_startup() -> None:
    settings = get_settings()
    now = app_now()
    session = get_sync_session_factory()()
    try:
        reserved_jobs = session.execute(
            select(DorisSm4BatchJob).where(DorisSm4BatchJob.state == "reserved")
        ).scalars().all()
        for job in reserved_jobs:
            job.state = "queued"
            job.message = "SM4 reserved job was returned to queue after service startup recovery."
            job.updated_at = now
            _add_sm4_log(job.id, "WARN", "startup_reserved_recovered", job.message, connection_id=job.connection_id, database=job.database)
        if settings.sm4_recover_active_jobs_on_startup:
            active_jobs = session.execute(
                select(DorisSm4BatchJob).where(DorisSm4BatchJob.state.in_(["running", "stopping"]))
            ).scalars().all()
            for job in active_jobs:
                job.state = "failed"
                job.message = "SM4 batch job was interrupted by service restart; please submit it again."
                job.error_message = job.message
                job.finished_at = now
                job.updated_at = now
                job.results = [_interrupt_running_sm4_table_result(item, job.message, now) for item in (job.results or [])]
                job.failed_count = job.total_count or len(job.results or [])
                job.success_count = 0
                _add_sm4_log(job.id, "ERROR", "startup_running_interrupted", job.message, connection_id=job.connection_id, database=job.database)
        session.commit()
    finally:
        session.close()


def run_sm4_task_snapshot(snapshot: dict[str, Any], actor: AuthContext | None = None) -> DorisSm4BatchStatus:
    session = get_sync_session_factory()()
    try:
        connection_id = snapshot.get("connection_id")
        if not connection_id:
            raise ValueError("SM4 task snapshot has no connection_id.")
        profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(connection_id)))
        if not profile:
            raise ValueError("Task snapshot connection does not exist.")
        return create_sm4_batch_task(
            profile,
            database=str(snapshot.get("database") or ""),
            tables=deepcopy(snapshot.get("tables") or []),
            table_strategy=str(snapshot.get("table_strategy") or "drop_recreate"),
            target_suffix=snapshot.get("target_suffix"),
            actor=actor,
        )
    finally:
        session.close()


def _dispatch_queued_sm4_jobs() -> None:
    from recovery_service.services.sm4_runtime_guard import sm4_dispatch_guard

    try:
        with sm4_dispatch_guard():
            _dispatch_queued_sm4_jobs_locked()
    except TimeoutError:
        return


def _dispatch_queued_sm4_jobs_locked() -> None:
    settings = get_settings()
    max_global = max(1, settings.sm4_worker_concurrency)
    max_connection = max(1, settings.sm4_connection_concurrency)
    max_database = max(1, settings.sm4_database_concurrency)
    session = get_sync_session_factory()()
    try:
        _reset_stale_reserved_sm4_jobs(session)
        active_jobs = list(
            session.execute(
                select(DorisSm4BatchJob)
                .where(DorisSm4BatchJob.state.in_(["reserved", "running", "stopping"]))
                .order_by(DorisSm4BatchJob.updated_at)
            )
            .scalars()
            .all()
        )
        available = max_global - len(active_jobs)
        if available <= 0:
            return
        queued_jobs = list(
            session.execute(
                select(DorisSm4BatchJob)
                .where(DorisSm4BatchJob.state == "queued")
                .order_by(DorisSm4BatchJob.created_at)
                .limit(50)
            )
            .scalars()
            .all()
        )
        for job in queued_jobs:
            if available <= 0:
                break
            if job.execution_window_enabled and not _is_now_in_execution_window(app_now(), job.execution_window_start, job.execution_window_end):
                window_reason = f"SM4 batch job waiting for execution window {job.execution_window_start or '22:00'}-{job.execution_window_end or '09:00'}."
                if job.message != window_reason:
                    job.message = window_reason
                    job.updated_at = app_now()
                continue
            blocked_reason = _sm4_dispatch_block_reason(
                job,
                active_jobs,
                max_connection=max_connection,
                max_database=max_database,
            )
            if blocked_reason:
                if job.message != blocked_reason:
                    job.message = blocked_reason
                    job.updated_at = app_now()
                continue
            reserved_at = app_now()
            claimed = session.execute(
                update(DorisSm4BatchJob)
                .where(DorisSm4BatchJob.id == job.id)
                .where(DorisSm4BatchJob.state == "queued")
                .values(
                    state="reserved",
                    message="SM4 batch job reserved by scheduler and waiting for worker.",
                    updated_at=reserved_at,
                )
            )
            session.commit()
            if claimed.rowcount != 1:
                continue
            job.state = "reserved"
            job.message = "SM4 batch job reserved by scheduler and waiting for worker."
            job.updated_at = reserved_at
            try:
                celery_task_id = _enqueue_sm4_worker(job.id)
                _add_sm4_log(
                    job.id,
                    "INFO",
                    "dispatched",
                    "SM4 batch job dispatched to worker.",
                    connection_id=job.connection_id,
                    database=job.database,
                    payload={"celery_task_id": celery_task_id},
                )
                active_jobs.append(job)
                available -= 1
            except Exception as exc:
                job.state = "queued"
                job.message = f"SM4 worker dispatch failed: {exc}"
                job.updated_at = app_now()
                session.commit()
                _add_sm4_log(job.id, "ERROR", "dispatch_failed", job.message, connection_id=job.connection_id, database=job.database, error_message=str(exc))
    finally:
        session.commit()
        session.close()


def _reset_stale_reserved_sm4_jobs(session: Session) -> None:
    stale_before = app_now() - timedelta(minutes=10)
    jobs = session.execute(
        select(DorisSm4BatchJob)
        .where(DorisSm4BatchJob.state == "reserved", DorisSm4BatchJob.updated_at < stale_before)
        .limit(20)
    ).scalars().all()
    for job in jobs:
        job.state = "queued"
        job.message = "SM4 reserved job timed out and was returned to queue."
        job.updated_at = app_now()
        _add_sm4_log(job.id, "WARN", "reserved_timeout", job.message, connection_id=job.connection_id, database=job.database)


def _sm4_dispatch_block_reason(
    job: DorisSm4BatchJob,
    active_jobs: list[DorisSm4BatchJob],
    *,
    max_connection: int,
    max_database: int,
) -> str | None:
    connection_active = [item for item in active_jobs if item.connection_id == job.connection_id]
    if len(connection_active) >= max_connection:
        blockers = ", ".join(f"{str(item.id)[:8]}:{item.state}" for item in connection_active[:5])
        return f"SM4 batch job waiting: connection concurrency limit reached. blockers={blockers}"
    database_active = [item for item in connection_active if item.database == job.database]
    if len(database_active) >= max_database:
        return "SM4 batch job waiting: database concurrency limit reached."
    job_tables = {str(item.get("table_name")) for item in job.tables or [] if item.get("table_name")}
    for active in database_active:
        active_tables = {str(item.get("table_name")) for item in active.tables or [] if item.get("table_name")}
        if job_tables & active_tables:
            return "SM4 batch job waiting: table lock is held by another running job."
    return None


def _interrupt_running_sm4_table_result(item: dict[str, Any], message: str, finished_at) -> dict[str, Any]:
    if item.get("state") in {"succeeded", "failed", "stopped", "cancelled"}:
        return item
    updated = dict(item)
    updated["state"] = "failed"
    updated["message"] = message
    updated["finished_at"] = finished_at.isoformat() if hasattr(finished_at, "isoformat") else str(finished_at)
    return updated


def _enqueue_sm4_worker(batch_id: uuid.UUID) -> str | None:
    from recovery_service.workers.celery_app import celery_app

    settings = get_settings()
    result = celery_app.send_task("doris.sm4_batch", args=[str(batch_id)], queue=settings.celery_sm4_queue)
    return result.id


def _run_due_sm4_auto_snapshot_tasks() -> None:
    now = app_now()
    session = get_sync_session_factory()()
    try:
        tasks = session.execute(
            select(DorisSm4AutoSnapshotTask)
            .where(
                DorisSm4AutoSnapshotTask.enabled == True,  # noqa: E712
                DorisSm4AutoSnapshotTask.next_scan_at <= now,
            )
            .order_by(DorisSm4AutoSnapshotTask.next_scan_at)
            .limit(10)
        ).scalars().all()
        for task in tasks:
            interval = max(1, min(1440, int(task.scan_interval_minutes or 60)))
            if task.execution_window_enabled and not _is_now_in_execution_window(now, task.execution_window_start, task.execution_window_end):
                task.message = f"等待执行窗口 {task.execution_window_start or '22:00'}-{task.execution_window_end or '09:00'} 后再巡检。"
                task.next_scan_at = now + timedelta(minutes=5)
                task.updated_at = now
                continue
            profile = session.get(DatabaseConnectionProfile, task.connection_id)
            if not profile:
                task.message = "Doris 连接不存在，自动快照巡检已跳过。"
                task.next_scan_at = now + timedelta(minutes=interval)
                task.updated_at = now
                continue
            try:
                batches, changed_tables, snapshot = _scan_sm4_auto_snapshot_task(profile, task, now=now)
                task.snapshot = snapshot
                task.last_scan_at = now
                task.next_scan_at = now + timedelta(minutes=interval)
                task.updated_at = now
                if changed_tables:
                    task.last_change_at = now
                    task.message = f"巡检发现 {len(changed_tables)} 张表发生变化，已生成 {len(batches)} 个 SM4 批次。"
                else:
                    task.message = f"巡检完成：{snapshot.get('database_count', 0)} 个库，{snapshot.get('table_count', 0)} 张表，未发现变化。"
            except Exception as exc:
                logger.error("doris sm4 auto snapshot scan failed", task_id=str(task.id), error=str(exc), exc_info=True)
                task.message = f"自动快照巡检失败：{exc}"
                task.next_scan_at = now + timedelta(minutes=interval)
                task.updated_at = now
        session.commit()
    finally:
        session.close()


def _scan_sm4_auto_snapshot_task(
    profile: DatabaseConnectionProfile,
    task: DorisSm4AutoSnapshotTask,
    *,
    now: datetime,
) -> tuple[list[DorisSm4BatchStatus], list[dict[str, Any]], dict[str, Any]]:
    snapshot = _capture_doris_auto_snapshot(
        profile,
        include_patterns=list(task.include_databases or []),
        exclude_patterns=list(task.exclude_databases or []),
        exclude_table_patterns=list(getattr(task, "exclude_tables", None) or []),
        keywords=list(task.keywords or []),
    )
    changed_tables = _auto_snapshot_changed_tables(task.snapshot or {}, snapshot)
    batches = _create_sm4_auto_snapshot_batches(profile, task, changed_tables, snapshot)
    return batches, changed_tables, snapshot


def _create_sm4_auto_snapshot_batches(
    profile: DatabaseConnectionProfile,
    task: DorisSm4AutoSnapshotTask,
    changed_tables: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[DorisSm4BatchStatus]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    reasons: dict[str, str] = {}
    for item in changed_tables:
        database = str(item.get("database") or "").strip()
        table_name = str(item.get("table_name") or "").strip()
        columns = [str(column).strip() for column in item.get("columns") or [] if str(column).strip()]
        if not database or not table_name or not columns:
            continue
        grouped.setdefault(database, []).append({"table_name": table_name, "columns": columns})
        reasons[f"{database}.{table_name}"] = str(item.get("reason") or "changed")
    batches: list[DorisSm4BatchStatus] = []
    for database, tables in grouped.items():
        batch = create_sm4_batch_task(
            profile,
            database=database,
            tables=tables,
            table_strategy="drop_recreate",
            target_suffix=task.target_suffix,
            execution_window_enabled=task.execution_window_enabled,
            execution_window_start=task.execution_window_start,
            execution_window_end=task.execution_window_end,
            allow_running_cross_window=task.allow_running_cross_window,
            auto_snapshot=True,
            auto_snapshot_config={
                "source": "auto_snapshot_task",
                "task_id": str(task.id),
                "task_name": task.name,
                "scan_at": snapshot.get("captured_at"),
                "change_reasons": reasons,
            },
            actor=None,
        )
        batches.append(batch)
    return batches


def _run_due_sm4_schedules() -> None:
    now = app_now()
    session = get_sync_session_factory()()
    due: list[tuple[DorisSm4Schedule, DatabaseConnectionProfile]] = []
    try:
        schedules = session.execute(
            select(DorisSm4Schedule)
            .where(
                DorisSm4Schedule.enabled == True,  # noqa: E712
                DorisSm4Schedule.next_run_at <= now,
                DorisSm4Schedule.archived_at.is_(None),
                DorisSm4Schedule.deleted_at.is_(None),
            )
            .order_by(DorisSm4Schedule.next_run_at)
            .limit(20)
        ).scalars().all()
        for schedule in schedules:
            profile = session.get(DatabaseConnectionProfile, schedule.connection_id)
            if not profile:
                schedule.last_run_at = now
                schedule.next_run_at = _next_schedule_time(
                    schedule.schedule_type,
                    schedule.run_time,
                    day_of_month=schedule.day_of_month,
                    day_of_week=schedule.day_of_week,
                    interval_minutes=schedule.interval_minutes,
                    after=now,
                )
                continue
            due.append((schedule, profile))
            schedule.last_run_at = now
            schedule.next_run_at = _next_schedule_time(
                schedule.schedule_type,
                schedule.run_time,
                day_of_month=schedule.day_of_month,
                day_of_week=schedule.day_of_week,
                interval_minutes=schedule.interval_minutes,
                after=now,
            )
        session.commit()
        for schedule, profile in due:
            create_sm4_batch_task(
                profile,
                database=schedule.database,
                tables=schedule.tables,
                table_strategy=schedule.table_strategy,
                target_suffix=schedule.target_suffix,
                schedule_id=schedule.id,
                actor=None,
            )
    finally:
        session.close()


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


def _ensure_doris_profile(profile: DatabaseConnectionProfile) -> None:
    if profile.engine != "doris":
        raise ValueError("请选择 Doris 类型的数据连接。")


def _clean_keywords(keywords: list[str] | None) -> list[str]:
    result: list[str] = []
    for item in keywords or DEFAULT_DORIS_ENCRYPTION_KEYWORDS:
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return result or DEFAULT_DORIS_ENCRYPTION_KEYWORDS.copy()


def _matched_keywords(column_name: str, keywords: list[str]) -> list[str]:
    lowered = column_name.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def _latest_mask_assets_for_catalog(connection_id: uuid.UUID, database: str):
    session = get_sync_session_factory()()
    try:
        return latest_mask_assets_for_catalog(session, connection_id=connection_id, database=database)
    finally:
        session.close()


def _register_sm4_mask_assets(
    profile: DatabaseConnectionProfile,
    *,
    task_id: uuid.UUID,
    database: str,
    source_table: str,
    output_table: str | None,
    backup_table: str | None,
    table_mode: str,
    columns: list[str],
    status: str,
) -> None:
    session = get_sync_session_factory()()
    try:
        register_mask_task(
            session,
            task_id=task_id,
            profile=profile,
            database=database,
            source_table=source_table,
            output_table=output_table,
            backup_table=backup_table,
            algorithm="SM4",
            table_mode=table_mode,
            columns=columns,
            status=status,
        )
    finally:
        session.close()


def _finish_sm4_mask_assets(task_id: uuid.UUID, status: str, message: str | None = None) -> None:
    session = get_sync_session_factory()()
    try:
        finish_mask_task(session, task_id=task_id, status=status, message=message)
    finally:
        session.close()


def _record_sm4_field_mappings(
    profile: DatabaseConnectionProfile,
    *,
    task_id: uuid.UUID,
    database: str,
    source_table: str,
    output_table: str,
    columns: list[str],
) -> None:
    session = get_sync_session_factory()()
    try:
        record_field_mappings(
            session,
            task_id=task_id,
            profile=profile,
            source_database=database,
            source_table=source_table,
            masked_database=database,
            masked_table=output_table,
            columns=columns,
            algorithm="SM4",
            mapping_database=None,
            mapping_tables={},
        )
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


def _replace_create_table_name(ddl: str, new_table_name: str, database: str | None = None) -> str:
    new_name = f"{_q(database)}.{_q(new_table_name)}" if database else _q(new_table_name)
    replacement = r"\1" + new_name
    updated = _CREATE_TABLE_RE.sub(replacement, ddl, count=1)
    if updated == ddl:
        raise ValueError("无法替换 DDL 中的表名。")
    return updated


def _rewrite_encrypted_column_types(ddl: str, encrypted_columns: list[str]) -> str:
    encrypted = set(encrypted_columns)
    if not encrypted:
        return ddl
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        column_name = match.group("name").replace("``", "`")
        if column_name not in encrypted:
            return match.group(0)
        found.add(column_name)
        logger.info(
            "doris encryption rewrite encrypted column type",
            column_name=column_name,
            original_type=match.group("type"),
            target_type="varchar(2000)",
        )
        return f"{match.group(1)}varchar(2000){match.group('rest')}"

    updated = _COLUMN_DEF_RE.sub(replace, ddl)
    missing = sorted(encrypted - found)
    if missing:
        raise ValueError(f"无法在 DDL 中定位待加密字段：{', '.join(missing)}")
    return updated


def _build_insert_sql(
    source_database: str,
    target_database: str,
    table_name: str,
    source_table: str,
    table_columns: list[str],
    encrypted_columns: list[str],
) -> str:
    encrypted = set(encrypted_columns)
    insert_columns = ", ".join(_q(column) for column in table_columns)
    select_columns = ", ".join(
        f"CQ_SM4_ENCRYPT({_q(column)}) AS {_q(column)}" if column in encrypted else _q(column)
        for column in table_columns
    )
    return (
        f"INSERT INTO {_q(target_database)}.{_q(table_name)} ({insert_columns}) "
        f"SELECT {select_columns} FROM {_q(source_database)}.{_q(source_table)}"
    )


def _suffixed_table_name(table_name: str, suffix: str | None, default_prefix: str) -> str:
    clean_suffix = (suffix or "").strip().strip("_")
    if clean_suffix:
        if not _IDENT_RE.match(clean_suffix):
            raise ValueError("备份后缀只能包含中文、字母、数字和下划线。")
        return _fit_table_name(f"{table_name}_{clean_suffix}")
    return _fit_table_name(f"{table_name}_{default_prefix}_{app_now().strftime('%Y%m%d_%H%M%S')}")


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


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _clean_sm4_table_strategy(value: str) -> str:
    if value in {"append_existing", "drop_recreate", "auto_create"}:
        return value
    raise ValueError("SM4 表策略只能是追加到已有表、删除后新建或自动新建。")


def _clean_schedule_type(value: str) -> str:
    if value in {"daily", "weekly", "monthly", "interval"}:
        return value
    raise ValueError("定时类型只能是每日、每周、每月或间隔执行。")


def _clean_run_time(value: str | None) -> str:
    text = (value or "02:00").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("执行时间格式应为 HH:MM。")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("执行时间格式应为 HH:MM。")
    return f"{hour:02d}:{minute:02d}"


def _clean_optional_identifier(value: str | None, label: str) -> str | None:
    clean = (value or "").strip().strip("_")
    if not clean:
        return None
    if not _IDENT_RE.match(clean):
        raise ValueError(f"{label}只能包含中文、字母、数字和下划线。")
    return clean


def _clean_execution_window_time(value: str) -> str:
    clean = str(value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", clean)
    if not match:
        raise ValueError("执行窗口时间必须使用 HH:MM 格式。")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("执行窗口时间必须在 00:00 到 23:59 之间。")
    return f"{hour:02d}:{minute:02d}"


def _time_to_minutes(value: str) -> int:
    clean = _clean_execution_window_time(value)
    hour, minute = clean.split(":", 1)
    return int(hour) * 60 + int(minute)


def _is_now_in_execution_window(now: datetime, start: str | None, end: str | None) -> bool:
    start_minutes = _time_to_minutes(start or "22:00")
    end_minutes = _time_to_minutes(end or "09:00")
    current_minutes = now.hour * 60 + now.minute
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def _clean_database_patterns(values: list[str] | None) -> list[str]:
    patterns: list[str] = []
    for value in values or []:
        clean = str(value or "").strip()
        if clean and clean not in patterns:
            patterns.append(clean)
    return patterns


def _clean_table_patterns(values: list[str] | None) -> list[str]:
    return _clean_database_patterns(values)


def _database_allowed_for_auto_snapshot(name: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    if include_patterns and not _database_matches_any_pattern(name, include_patterns):
        return False
    if exclude_patterns and _database_matches_any_pattern(name, exclude_patterns):
        return False
    return True


def _table_excluded_for_auto_snapshot(name: str, exclude_patterns: list[str]) -> bool:
    return bool(exclude_patterns and _database_matches_any_pattern(name, exclude_patterns))


def _database_matches_any_pattern(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    for pattern in patterns:
        clean = pattern.strip()
        if not clean:
            continue
        lowered_pattern = clean.lower()
        wildcard = lowered_pattern if any(char in lowered_pattern for char in "*?[]") else f"*{lowered_pattern}*"
        if fnmatch(lowered, wildcard):
            return True
    return False


def _capture_doris_auto_snapshot(
    profile: DatabaseConnectionProfile,
    *,
    include_patterns: list[str],
    exclude_patterns: list[str],
    exclude_table_patterns: list[str],
    keywords: list[str],
) -> dict[str, Any]:
    clean_keywords = _clean_keywords(keywords)
    databases = [
        name
        for name in list_doris_source_databases(profile)
        if _database_allowed_for_auto_snapshot(name, include_patterns, exclude_patterns)
    ]
    snapshot: dict[str, Any] = {
        "captured_at": app_now().isoformat(),
        "database_count": len(databases),
        "table_count": 0,
        "databases": {},
    }
    with _doris_conn(profile, None) as db:
        with db.cursor() as cur:
            for database in databases:
                cur.execute(
                    """
                    SELECT TABLE_NAME, TABLE_ROWS
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_NAME
                    """,
                    (database,),
                )
                table_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, ORDINAL_POSITION
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """,
                    (database,),
                )
                column_rows = cur.fetchall()
                tables = _build_auto_snapshot_tables(table_rows, column_rows, clean_keywords, exclude_table_patterns)
                snapshot["databases"][database] = {"tables": tables}
                snapshot["table_count"] += len(tables)
    return snapshot


def _build_auto_snapshot_tables(
    table_rows: list[dict[str, Any]],
    column_rows: list[dict[str, Any]],
    keywords: list[str],
    exclude_table_patterns: list[str],
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for row in table_rows:
        table_name = str(row.get("TABLE_NAME") or row.get("table_name") or "").strip()
        if not table_name:
            continue
        if _table_excluded_for_auto_snapshot(table_name, exclude_table_patterns):
            continue
        tables[table_name] = {
            "row_count": _safe_int(row.get("TABLE_ROWS") or row.get("table_rows")),
            "columns": [],
            "selected_columns": [],
        }
    for row in column_rows:
        table_name = str(row.get("TABLE_NAME") or row.get("table_name") or "").strip()
        column_name = str(row.get("COLUMN_NAME") or row.get("column_name") or "").strip()
        if not table_name or not column_name or table_name not in tables:
            continue
        column_type = str(row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or "")
        column = {
            "name": column_name,
            "type": column_type,
            "ordinal_position": _safe_int(row.get("ORDINAL_POSITION") or row.get("ordinal_position")),
        }
        tables[table_name]["columns"].append(column)
        if _matched_keywords(column_name, keywords):
            tables[table_name]["selected_columns"].append(column_name)
    return tables


def _auto_snapshot_changed_tables(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    previous_tables = _flatten_auto_snapshot_tables(previous or {})
    current_tables = _flatten_auto_snapshot_tables(current)
    changed: list[dict[str, Any]] = []
    for key, current_table in current_tables.items():
        previous_table = previous_tables.get(key)
        reason = _auto_snapshot_change_reason(previous_table, current_table)
        selected_columns = list(current_table.get("selected_columns") or [])
        if reason and selected_columns:
            changed.append(
                {
                    "database": current_table["database"],
                    "table_name": current_table["table_name"],
                    "columns": selected_columns,
                    "reason": reason,
                }
            )
    return changed


def _flatten_auto_snapshot_tables(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    flattened: dict[str, dict[str, Any]] = {}
    for database, db_payload in (snapshot.get("databases") or {}).items():
        for table_name, table_payload in (db_payload.get("tables") or {}).items():
            key = f"{database}.{table_name}"
            flattened[key] = {
                **(table_payload or {}),
                "database": database,
                "table_name": table_name,
            }
    return flattened


def _auto_snapshot_change_reason(previous: dict[str, Any] | None, current: dict[str, Any]) -> str | None:
    if not previous:
        return "new_table"
    if _safe_int(previous.get("row_count")) != _safe_int(current.get("row_count")):
        return "row_count_changed"
    if _auto_snapshot_column_signature(previous) != _auto_snapshot_column_signature(current):
        return "columns_changed"
    return None


def _auto_snapshot_column_signature(table_payload: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [
        (
            str(column.get("name") or ""),
            str(column.get("type") or ""),
            _safe_int(column.get("ordinal_position")),
        )
        for column in (table_payload.get("columns") or [])
    ]


def _normalize_batch_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tables or []:
        table_name = str(item.get("table_name") or "").strip()
        if not table_name or table_name in seen:
            continue
        if not _IDENT_RE.match(table_name):
            raise ValueError(f"表名 {table_name} 不合法。")
        columns = []
        for column in item.get("columns") or []:
            clean_column = str(column or "").strip()
            if clean_column and clean_column not in columns:
                columns.append(clean_column)
        if not columns:
            raise ValueError(f"表 {table_name} 未选择加密字段。")
        target_database = _clean_optional_identifier(item.get("target_database"), "结果数据库")
        target_table = _clean_optional_identifier(item.get("target_table"), "目标表名")
        result.append({"table_name": table_name, "columns": columns, "target_database": target_database, "target_table": target_table})
        seen.add(table_name)
    return result


def _planned_target_table(table_name: str, target_table: str | None, target_suffix: str | None, table_strategy: str) -> str:
    if target_table:
        return target_table
    if table_strategy == "auto_create":
        return _suffixed_table_name(table_name, target_suffix, "sm4")
    suffix = target_suffix or "sm4"
    return _fit_table_name(f"{table_name}_{suffix}")


def _resolve_sm4_target_table(
    cur,
    *,
    database: str,
    source_table: str,
    requested_target: str | None,
    target_suffix: str | None,
    table_strategy: str,
) -> str:
    target = _planned_target_table(source_table, requested_target, target_suffix, table_strategy)
    if table_strategy != "auto_create":
        return target
    if not _table_exists(cur, database, target):
        return target
    base = _fit_table_name(target, reserve=3)
    for index in range(1, 100):
        candidate = _fit_table_name(f"{base}_{index}")
        if not _table_exists(cur, database, candidate):
            return candidate
    raise ValueError(f"无法为 {source_table} 生成可用的自动加密表名。")


def _fit_table_name(name: str, reserve: int = 0) -> str:
    limit = max(1, 64 - reserve)
    if len(name) <= limit:
        return name
    return name[:limit].rstrip("_")


def _replace_result(results: list[dict[str, Any]], index: int, value: dict[str, Any]) -> list[dict[str, Any]]:
    updated = list(results)
    while len(updated) <= index:
        updated.append({})
    updated[index] = value
    return updated


def _stop_remaining_sm4_batch_tables(
    session,
    job: DorisSm4BatchJob,
    tables: list[dict[str, Any]],
    results: list[dict[str, Any]],
    start_index: int,
) -> None:
    now = app_now()
    stop_state = "cancelled" if job.state == "cancelled" else "stopped"
    updated_results = list(results)
    for index in range(start_index, len(tables)):
        item = tables[index]
        result = dict(updated_results[index]) if index < len(updated_results) else {}
        if result.get("state") not in {"succeeded", "failed", "stopped", "cancelled"}:
            result.update(
                {
                    "table_name": item["table_name"],
                    "target_database": item.get("target_database") or job.database,
                    "target_table": result.get("target_table"),
                    "columns": item["columns"],
                    "state": stop_state,
                    "message": "Task was not started because the batch was stopped.",
                    "finished_at": now.isoformat(),
                }
            )
            updated_results = _replace_result(updated_results, index, result)
    job.results = updated_results
    job.state = stop_state
    job.message = "SM4 batch job stopped by user."
    job.finished_at = now
    job.updated_at = now
    session.commit()


def _current_doris_session_id(db, cur) -> str | None:
    try:
        thread_id = db.thread_id()
        if thread_id:
            return str(thread_id)
    except Exception:
        pass
    try:
        cur.execute("SELECT CONNECTION_ID() AS connection_id")
        row = cur.fetchone() or {}
        value = row.get("connection_id") or row.get("CONNECTION_ID()")
        return str(value) if value else None
    except Exception:
        return None


def _save_current_sm4_session(batch_id: uuid.UUID, table_name: str, db_session_id: str | None) -> None:
    if not db_session_id:
        return
    session = get_sync_session_factory()()
    try:
        job = session.get(DorisSm4BatchJob, batch_id)
        if not job:
            return
        results = []
        changed = False
        for item in job.results or []:
            updated = dict(item)
            if updated.get("table_name") == table_name and updated.get("state") == "running":
                updated["db_session_id"] = db_session_id
                changed = True
            results.append(updated)
        if changed:
            job.results = results
            job.updated_at = app_now()
            session.commit()
    finally:
        session.close()


def list_sm4_batch_logs(batch_id: uuid.UUID, *, limit: int = 300) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DorisSm3TaskLog)
            .where(DorisSm3TaskLog.task_id == batch_id)
            .order_by(DorisSm3TaskLog.created_at, DorisSm3TaskLog.id)
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "level": row.level,
                "stage": row.stage,
                "message": row.message,
                "sql_type": row.sql_type,
                "sql_text": row.sql_text,
                "database_engine": row.database_engine,
                "connection_id": row.connection_id,
                "database_name": row.database_name,
                "table_name": row.table_name,
                "db_session_id": row.db_session_id,
                "duration_ms": row.duration_ms,
                "affected_rows": row.affected_rows,
                "error_message": row.error_message,
                "payload": row.payload or {},
                "created_at": row.created_at,
            }
            for row in rows
        ]
    finally:
        session.close()


def _add_sm4_log(
    task_id: uuid.UUID,
    level: str,
    stage: str,
    message: str,
    *,
    connection_id: uuid.UUID | None = None,
    database: str | None = None,
    table_name: str | None = None,
    sql_text: str | None = None,
    sql_type: str | None = None,
    db_session_id: str | None = None,
    duration_ms: int | None = None,
    affected_rows: int | None = None,
    error_message: str | None = None,
    payload: dict | None = None,
) -> None:
    session = get_sync_session_factory()()
    try:
        session.add(
            DorisSm3TaskLog(
                task_id=task_id,
                level=level,
                stage=stage,
                message=message,
                sql_text=sql_text,
                sql_type=sql_type,
                database_engine="doris",
                connection_id=connection_id,
                database_name=database,
                table_name=table_name,
                db_session_id=db_session_id,
                duration_ms=duration_ms,
                affected_rows=affected_rows,
                error_message=error_message,
                payload=payload or {"algorithm": "SM4"},
            )
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _execute_sm4_logged_sql(
    cur,
    sql: str,
    *,
    batch_id: uuid.UUID,
    profile: DatabaseConnectionProfile,
    database: str,
    table_name: str,
    stage: str,
    sql_type: str,
    db_session_id: str | None,
) -> None:
    _add_sm4_log(
        batch_id,
        "SQL",
        stage,
        "SQL execution started.",
        connection_id=profile.id,
        database=database,
        table_name=table_name,
        sql_text=sql,
        sql_type=sql_type,
        db_session_id=db_session_id,
    )
    started = time.monotonic()
    try:
        cur.execute(sql)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        _add_sm4_log(
            batch_id,
            "ERROR",
            stage,
            "SQL execution failed.",
            connection_id=profile.id,
            database=database,
            table_name=table_name,
            sql_text=sql,
            sql_type=sql_type,
            db_session_id=db_session_id,
            duration_ms=duration_ms,
            error_message=str(exc),
        )
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    _add_sm4_log(
        batch_id,
        "SQL",
        stage,
        "SQL execution succeeded.",
        connection_id=profile.id,
        database=database,
        table_name=table_name,
        sql_text=sql,
        sql_type=sql_type,
        db_session_id=db_session_id,
        duration_ms=duration_ms,
    )


def _try_stop_running_sm4_queries(session, job: DorisSm4BatchJob) -> None:
    profile = session.get(DatabaseConnectionProfile, job.connection_id)
    if not profile:
        return
    results = []
    changed = False
    for item in job.results or []:
        updated = dict(item)
        db_session_id = updated.get("db_session_id")
        if updated.get("state") == "running" and db_session_id:
            kill_message = _try_kill_doris_query(profile, job.database, str(db_session_id))
            updated["stop_requested_at"] = app_now().isoformat()
            updated["stop_message"] = kill_message
            _add_sm4_log(
                job.id,
                "CANCEL",
                "kill_query",
                kill_message,
                connection_id=job.connection_id,
                database=job.database,
                table_name=updated.get("table_name"),
                db_session_id=str(db_session_id),
            )
            changed = True
        results.append(updated)
    if changed:
        job.results = results


def _try_kill_doris_query(profile: DatabaseConnectionProfile, database: str, db_session_id: str) -> str:
    safe_session_id = _safe_int(db_session_id)
    if safe_session_id <= 0:
        return f"Invalid Doris session id: {db_session_id}"
    errors: list[str] = []
    try:
        with _doris_conn(profile, database) as db:
            with db.cursor() as cur:
                for sql in (
                    f"KILL QUERY {safe_session_id}",
                    f"KILL CONNECTION {safe_session_id}",
                    f"KILL {safe_session_id}",
                    f"CANCEL QUERY {safe_session_id}",
                ):
                    try:
                        cur.execute(sql)
                        return f"Sent stop command: {sql}"
                    except Exception as exc:
                        errors.append(f"{sql}: {exc}")
    except Exception as exc:
        errors.append(str(exc))
    return "Failed to stop Doris query. " + " | ".join(errors)


def _save_sm4_batch_progress(
    session,
    job: DorisSm4BatchJob,
    results: list[dict[str, Any]],
    success_count: int,
    failed_count: int,
) -> None:
    job.results = results
    job.success_count = success_count
    job.failed_count = failed_count
    job.updated_at = app_now()
    job.message = f"SM4 批次执行中：成功 {success_count} 张，失败 {failed_count} 张。"
    session.commit()


def _sm4_batch_to_status(job: DorisSm4BatchJob) -> DorisSm4BatchStatus:
    return DorisSm4BatchStatus(
        batch_id=job.id,
        schedule_id=job.schedule_id,
        connection_id=job.connection_id,
        connection_name=job.connection_name,
        database=job.database,
        sm4_key_version_id=job.sm4_key_version_id,
        sm4_key_fingerprint=job.sm4_key_fingerprint,
        table_strategy=job.table_strategy,  # type: ignore[arg-type]
        target_suffix=job.target_suffix,
        execution_window_enabled=job.execution_window_enabled,
        execution_window_start=job.execution_window_start,
        execution_window_end=job.execution_window_end,
        allow_running_cross_window=job.allow_running_cross_window,
        auto_snapshot=job.auto_snapshot,
        auto_snapshot_config=job.auto_snapshot_config,
        state=job.state,  # type: ignore[arg-type]
        message=job.message,
        total_count=job.total_count,
        success_count=job.success_count,
        failed_count=job.failed_count,
        tables=[DorisSm4BatchTableSpec.model_validate(item) for item in (job.tables or [])],
        results=[DorisSm4BatchTableResult.model_validate(item) for item in (job.results or [])],
        created_by_username=job.created_by_username,
        created_by_auth_type=job.created_by_auth_type,
        created_at=job.created_at,
        started_at=job.started_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
    )


def _sm4_auto_snapshot_task_to_status(task: DorisSm4AutoSnapshotTask) -> DorisSm4AutoSnapshotTaskStatus:
    snapshot = task.snapshot or {}
    return DorisSm4AutoSnapshotTaskStatus(
        task_id=task.id,
        name=task.name,
        connection_id=task.connection_id,
        connection_name=task.connection_name,
        include_databases=list(task.include_databases or []),
        exclude_databases=list(task.exclude_databases or []),
        exclude_tables=list(getattr(task, "exclude_tables", None) or []),
        keywords=list(task.keywords or []),
        target_suffix=task.target_suffix,
        execution_window_enabled=task.execution_window_enabled,
        execution_window_start=task.execution_window_start,
        execution_window_end=task.execution_window_end,
        allow_running_cross_window=task.allow_running_cross_window,
        scan_interval_minutes=task.scan_interval_minutes,
        database_count=int(snapshot.get("database_count", 0) or 0),
        table_count=int(snapshot.get("table_count", 0) or 0),
        last_scan_at=task.last_scan_at,
        next_scan_at=task.next_scan_at,
        last_change_at=task.last_change_at,
        enabled=task.enabled,
        state=task.state,
        message=task.message,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _sm4_schedule_to_response(schedule: DorisSm4Schedule) -> DorisSm4ScheduleResponse:
    return DorisSm4ScheduleResponse(
        schedule_id=schedule.id,
        status=_sm4_schedule_status(schedule),  # type: ignore[arg-type]
        name=schedule.name,
        connection_id=schedule.connection_id,
        connection_name=schedule.connection_name,
        database=schedule.database,
        tables=[DorisSm4BatchTableSpec.model_validate(item) for item in (schedule.tables or [])],
        table_strategy=schedule.table_strategy,  # type: ignore[arg-type]
        target_suffix=schedule.target_suffix,
        schedule_type=schedule.schedule_type,  # type: ignore[arg-type]
        run_time=schedule.run_time,
        day_of_month=schedule.day_of_month,
        day_of_week=schedule.day_of_week,
        interval_minutes=schedule.interval_minutes,
        enabled=schedule.enabled,
        created_by_username=schedule.created_by_username,
        created_by_auth_type=schedule.created_by_auth_type,
        last_run_at=schedule.last_run_at,
        next_run_at=schedule.next_run_at,
        archived_at=schedule.archived_at,
        archived_by_username=schedule.archived_by_username,
        deleted_at=schedule.deleted_at,
        deleted_by_username=schedule.deleted_by_username,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def _sm4_task_definition_to_response(task: DorisSm4TaskDefinition) -> DorisSm4TaskDefinitionResponse:
    return DorisSm4TaskDefinitionResponse(
        task_id=task.id,
        name=task.name,
        revision=int(task.revision or 1),
        connection_id=task.connection_id,
        connection_name=task.connection_name,
        database=task.database,
        tables=[DorisSm4BatchTableSpec.model_validate(item) for item in (task.tables or [])],
        table_strategy=task.table_strategy,  # type: ignore[arg-type]
        target_suffix=task.target_suffix,
        created_by_username=task.created_by_username,
        created_by_auth_type=task.created_by_auth_type,
        archived_at=task.archived_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _sm4_schedule_status(schedule: DorisSm4Schedule) -> str:
    if schedule.deleted_at is not None:
        return "deleted"
    if schedule.archived_at is not None:
        return "archived"
    if schedule.enabled:
        return "active"
    return "paused"


def _auth_user_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(actor.user_id)
    except ValueError:
        return None


def _clean_lifecycle_reason(reason: str | None) -> str | None:
    clean = (reason or "").strip()
    return clean[:1000] if clean else None


def _next_schedule_time(
    schedule_type: str,
    run_time: str,
    *,
    day_of_month: int | None,
    day_of_week: int | None,
    interval_minutes: int | None,
    after: datetime,
) -> datetime:
    if schedule_type == "interval":
        return after + timedelta(minutes=interval_minutes or 60)
    hour, minute = [int(part) for part in _clean_run_time(run_time).split(":")]
    if schedule_type == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if schedule_type == "weekly":
        target_weekday = max(1, min(7, day_of_week or 1)) - 1
        days = (target_weekday - after.weekday()) % 7
        candidate = (after + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate
    target_day = max(1, min(31, day_of_month or 1))
    year = after.year
    month = after.month
    for _ in range(14):
        last_day = monthrange(year, month)[1]
        day = min(target_day, last_day)
        candidate = after.replace(year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > after:
            return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1
    return after + timedelta(days=30)


def _update_task(task_id: uuid.UUID, **changes: Any) -> None:
    with _TASK_LOCK:
        task = _TASKS[task_id]
        data = task.model_dump()
        data.update(changes)
        _TASKS[task_id] = DorisEncryptionTaskStatus.model_validate(data)


def _set_step(
    task_id: uuid.UUID,
    index: int,
    state: str,
    message: str | None = None,
    sql: str | None = None,
) -> None:
    with _TASK_LOCK:
        task = _TASKS[task_id]
        steps = list(task.steps)
        current = steps[index]
        steps[index] = DorisEncryptionTaskStep(
            title=current.title,
            state=state,  # type: ignore[arg-type]
            message=message if message is not None else current.message,
            sql=sql if sql is not None else current.sql,
        )
        data = task.model_dump()
        data["steps"] = [step.model_dump() for step in steps]
        _TASKS[task_id] = DorisEncryptionTaskStatus.model_validate(data)


def _mark_current_step_failed(task_id: uuid.UUID, message: str) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        steps = list(task.steps)
        for index, step in enumerate(steps):
            if step.state == "running":
                steps[index] = DorisEncryptionTaskStep(
                    title=step.title,
                    state="failed",
                    message=message,
                    sql=step.sql,
                )
                break
        data = task.model_dump()
        data["steps"] = [step.model_dump() for step in steps]
        _TASKS[task_id] = DorisEncryptionTaskStatus.model_validate(data)


def _finish_task(task_id: uuid.UUID, state: str, message: str) -> None:
    with _TASK_LOCK:
        task = _TASKS[task_id]
        data = task.model_dump()
        data.update({"state": state, "message": message, "finished_at": app_now()})
        _TASKS[task_id] = DorisEncryptionTaskStatus.model_validate(data)
