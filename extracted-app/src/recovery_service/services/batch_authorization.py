from __future__ import annotations

import csv
import hashlib
import io
import re
import threading
import uuid
import zipfile
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from recovery_service.api.schemas.batch_authorization import (
    BatchAuthDepartmentDatabaseResponse,
    BatchAuthDepartmentResponse,
    BatchAuthDepartmentUserResponse,
    BatchAuthDiscoveryResponse,
    BatchAuthDiscoveryRow,
    BatchAuthGeneratedCredential,
    BatchAuthGrantBatchResponse,
    BatchAuthGrantPreviewResponse,
    BatchAuthGrantPreviewRow,
    BatchAuthGrantTableResponse,
    BatchAuthGrantUserResponse,
    BatchAuthInitExecuteResponse,
    BatchAuthInitImportBatchResponse,
    BatchAuthInitImportRowResponse,
    BatchAuthInitPreviewResponse,
    BatchAuthInitPreviewRow,
    BatchAuthPreviewIssue,
)
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now, to_app_naive
from recovery_service.core.models.task import (
    BatchAuthDepartment,
    BatchAuthDepartmentDatabase,
    BatchAuthDepartmentUser,
    BatchAuthGrantBatch,
    BatchAuthGrantTable,
    BatchAuthGrantUser,
    BatchAuthInitImportBatch,
    BatchAuthInitImportRow,
    BatchAuthPrivilegeLease,
    DatabaseConnectionProfile,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.settings import get_settings

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\u4e00-\u9fff]{0,127}$")
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()


async def list_departments(db: AsyncSession) -> list[BatchAuthDepartmentResponse]:
    departments = (
        await db.execute(
            select(BatchAuthDepartment)
            .where(BatchAuthDepartment.status == "active")
            .order_by(BatchAuthDepartment.created_at.desc())
        )
    ).scalars().all()
    if not departments:
        return []
    department_ids = [item.id for item in departments]
    users = (
        await db.execute(
            select(BatchAuthDepartmentUser).where(
                BatchAuthDepartmentUser.department_id.in_(department_ids),
                BatchAuthDepartmentUser.status == "active",
            )
        )
    ).scalars().all()
    databases = (
        await db.execute(
            select(BatchAuthDepartmentDatabase).where(
                BatchAuthDepartmentDatabase.department_id.in_(department_ids),
                BatchAuthDepartmentDatabase.status == "active",
            )
        )
    ).scalars().all()
    users_by_department: dict[uuid.UUID, list[BatchAuthDepartmentUser]] = {}
    databases_by_department: dict[uuid.UUID, list[BatchAuthDepartmentDatabase]] = {}
    for item in users:
        users_by_department.setdefault(item.department_id, []).append(item)
    for item in databases:
        databases_by_department.setdefault(item.department_id, []).append(item)
    return [
        _department_response(
            item,
            users_by_department.get(item.id, []),
            databases_by_department.get(item.id, []),
        )
        for item in departments
    ]


async def create_department_relation(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    department_name: str,
    db_username: str,
    display_name: str | None,
    department_database: str,
    default_password: str,
    actor: AuthContext | None,
) -> BatchAuthDepartmentResponse:
    profile = await _doris_profile_async(db, connection_id)
    department_name = department_name.strip()
    db_username = db_username.strip()
    department_database = department_database.strip()
    if not department_name:
        raise ValueError("部门名称不能为空。")
    if not _USERNAME_RE.fullmatch(db_username):
        raise ValueError("数据库用户名格式不合法。")
    if not _IDENT_RE.fullmatch(department_database):
        raise ValueError("部门库名称格式不合法。")
    _ensure_doris_database_and_user(profile, department_database, db_username, default_password or "doris@2024")
    department = await _ensure_department(db, department_name, actor)
    department.status = "active"
    identity = _db_user_identity(db_username)
    await _ensure_department_user(db, department.id, db_username, identity, display_name)
    await _ensure_department_database(db, department.id, connection_id, department_database)
    await db.commit()
    await db.refresh(department)
    users = (
        await db.execute(
            select(BatchAuthDepartmentUser).where(
                BatchAuthDepartmentUser.department_id == department.id,
                BatchAuthDepartmentUser.status == "active",
            )
        )
    ).scalars().all()
    databases = (
        await db.execute(
            select(BatchAuthDepartmentDatabase).where(
                BatchAuthDepartmentDatabase.department_id == department.id,
                BatchAuthDepartmentDatabase.status == "active",
            )
        )
    ).scalars().all()
    return _department_response(department, users, databases)


async def delete_department_relation(db: AsyncSession, department_id: uuid.UUID) -> None:
    department = await db.get(BatchAuthDepartment, department_id)
    if not department:
        raise ValueError("部门关系不存在。")
    department.status = "inactive"
    users = (
        await db.execute(select(BatchAuthDepartmentUser).where(BatchAuthDepartmentUser.department_id == department_id))
    ).scalars().all()
    databases = (
        await db.execute(select(BatchAuthDepartmentDatabase).where(BatchAuthDepartmentDatabase.department_id == department_id))
    ).scalars().all()
    for item in users:
        item.status = "inactive"
    for item in databases:
        item.status = "inactive"
    await db.commit()


async def preview_init_import(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> BatchAuthInitPreviewResponse:
    profile = await _doris_profile_async(db, connection_id)
    rows = _parse_init_rows(filename, content)
    return _build_init_preview(profile, rows, filename)


async def execute_init_import(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    filename: str,
    content: bytes,
    default_password: str = "doris@2024",
    actor: AuthContext | None,
) -> BatchAuthInitExecuteResponse:
    profile = await _doris_profile_async(db, connection_id)
    parsed_rows = _parse_init_rows(filename, content)
    preview = _build_init_preview(profile, parsed_rows, filename)
    if preview.invalid_count:
        raise ValueError("初始化导入存在校验失败行，请先修正 Excel。")

    batch = BatchAuthInitImportBatch(
        connection_id=connection_id,
        connection_name=profile.name,
        filename=filename,
        state="running",
        total_count=len(parsed_rows),
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "system",
    )
    db.add(batch)
    await db.flush()

    generated_credentials: list[BatchAuthGeneratedCredential] = []
    success_count = 0
    failed_count = 0
    for row in parsed_rows:
        password = row["initial_password"] or default_password
        generated = False
        identity = _db_user_identity(row["db_username"])
        detail = BatchAuthInitImportRow(
            batch_id=batch.id,
            row_no=row["row_no"],
            department_name=row["department_name"],
            db_username=row["db_username"],
            db_user_identity=identity,
            display_name=row["display_name"],
            department_database=row["department_database"],
            generated_password=generated,
            state="running",
        )
        db.add(detail)
        await db.flush()
        try:
            _ensure_doris_database_and_user(profile, row["department_database"], row["db_username"], password)
            department = await _ensure_department(db, row["department_name"], actor)
            await _ensure_department_user(db, department.id, row["db_username"], identity, row["display_name"])
            await _ensure_department_database(db, department.id, profile.id, row["department_database"])
            detail.state = "succeeded"
            detail.message = "已完成初始化。"
            success_count += 1
        except Exception as exc:
            detail.state = "failed"
            detail.error_message = str(exc)
            failed_count += 1

    batch.success_count = success_count
    batch.failed_count = failed_count
    batch.state = "succeeded" if failed_count == 0 else ("failed" if success_count == 0 else "partial")
    batch.message = f"源表授权完成：成功 {success_count} 张表，失败 {failed_count} 张表。"
    batch.finished_at = app_now()
    await db.commit()
    await db.refresh(batch)
    rows = (
        await db.execute(
            select(BatchAuthInitImportRow)
            .where(BatchAuthInitImportRow.batch_id == batch.id)
            .order_by(BatchAuthInitImportRow.row_no.asc())
        )
    ).scalars().all()
    return BatchAuthInitExecuteResponse(
        batch=_init_batch_response(batch, rows),
        generated_credentials=generated_credentials,
    )


async def list_init_batches(db: AsyncSession) -> list[BatchAuthInitImportBatchResponse]:
    batches = (
        await db.execute(select(BatchAuthInitImportBatch).order_by(BatchAuthInitImportBatch.created_at.desc()))
    ).scalars().all()
    return [_init_batch_response(item, []) for item in batches]


async def get_init_batch(db: AsyncSession, batch_id: uuid.UUID) -> BatchAuthInitImportBatchResponse:
    batch = await db.get(BatchAuthInitImportBatch, batch_id)
    if not batch:
        raise ValueError("初始化导入批次不存在。")
    await db.refresh(batch)
    rows = (
        await db.execute(
            select(BatchAuthInitImportRow)
            .where(BatchAuthInitImportRow.batch_id == batch_id)
            .order_by(BatchAuthInitImportRow.row_no.asc())
        )
    ).scalars().all()
    return _init_batch_response(batch, rows)


async def discover_initialization_mappings(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    user_prefix: str = "cqssj_",
    database_prefix: str = "DWH_",
) -> BatchAuthDiscoveryResponse:
    profile = await _doris_profile_async(db, connection_id)
    return _discover_initialization_mappings(profile, user_prefix=user_prefix, database_prefix=database_prefix)


async def apply_discovered_mappings(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    rows: list[BatchAuthDiscoveryRow],
    default_password: str,
    actor: AuthContext | None,
) -> BatchAuthInitExecuteResponse:
    profile = await _doris_profile_async(db, connection_id)
    selected = [row for row in rows if row.selected]
    if not selected:
        raise ValueError("请至少选择一条映射关系。")
    batch = BatchAuthInitImportBatch(
        connection_id=connection_id,
        connection_name=profile.name,
        filename="自动发现映射",
        state="running",
        total_count=len(selected),
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "system",
    )
    db.add(batch)
    await db.flush()
    success_count = 0
    failed_count = 0
    for index, row in enumerate(selected, start=1):
        department_name = (row.department_name or row.display_name or row.db_username).strip()
        db_username = row.db_username.strip()
        department_database = row.department_database.strip()
        identity = _db_user_identity(db_username)
        detail = BatchAuthInitImportRow(
            batch_id=batch.id,
            row_no=index,
            department_name=department_name,
            db_username=db_username,
            db_user_identity=identity,
            display_name=row.display_name,
            department_database=department_database,
            generated_password=False,
            state="running",
        )
        db.add(detail)
        await db.flush()
        try:
            if not _USERNAME_RE.fullmatch(db_username):
                raise ValueError("数据库用户名格式不合法。")
            if not _IDENT_RE.fullmatch(department_database):
                raise ValueError("部门库名称格式不合法。")
            _ensure_doris_database_and_user(profile, department_database, db_username, default_password or "doris@2024")
            department = await _ensure_department(db, department_name, actor)
            await _ensure_department_user(db, department.id, db_username, identity, row.display_name)
            await _ensure_department_database(db, department.id, profile.id, department_database)
            detail.state = "succeeded"
            detail.message = "已应用自动发现映射。"
            success_count += 1
        except Exception as exc:
            detail.state = "failed"
            detail.error_message = str(exc)
            failed_count += 1
    batch.success_count = success_count
    batch.failed_count = failed_count
    batch.state = "succeeded" if failed_count == 0 else ("failed" if success_count == 0 else "partial")
    batch.message = f"源表授权完成：成功 {success_count} 张表，失败 {failed_count} 张表。"
    batch.finished_at = app_now()
    await db.commit()
    await db.refresh(batch)
    detail_rows = (
        await db.execute(
            select(BatchAuthInitImportRow)
            .where(BatchAuthInitImportRow.batch_id == batch.id)
            .order_by(BatchAuthInitImportRow.row_no.asc())
        )
    ).scalars().all()
    return BatchAuthInitExecuteResponse(batch=_init_batch_response(batch, detail_rows), generated_credentials=[])


async def preview_grant_import(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    department_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> BatchAuthGrantPreviewResponse:
    profile = await _doris_profile_async(db, connection_id)
    department, users, databases = await _department_context(db, department_id, connection_id)
    rows = _parse_grant_rows(filename, content)
    return _build_grant_preview(profile, department, users, databases[0], rows)


async def execute_grant_import(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    department_id: uuid.UUID,
    filename: str,
    content: bytes,
    name: str,
    expires_at: datetime,
    actor: AuthContext | None,
) -> BatchAuthGrantBatchResponse:
    expires_at = _naive_utc(expires_at)
    if expires_at <= app_now():
        raise ValueError("授权到期时间必须晚于当前时间。")
    profile = await _doris_profile_async(db, connection_id)
    department, users, databases = await _department_context(db, department_id, connection_id)
    parsed_rows = _parse_grant_rows(filename, content)
    preview = _build_grant_preview(profile, department, users, databases[0], parsed_rows)
    if preview.invalid_count:
        raise ValueError("授权导入存在校验失败行，请先修正 Excel。")

    department_database = databases[0].department_database
    batch = BatchAuthGrantBatch(
        connection_id=profile.id,
        connection_name=profile.name,
        department_id=department.id,
        department_name=department.name,
        department_database=department_database,
        name=name or f"{department.name} 批量授权",
        filename=filename,
        privilege_type="SELECT",
        starts_at=app_now(),
        expires_at=expires_at,
        state="running",
        total_table_count=len(parsed_rows),
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_by_auth_type=actor.auth_type if actor else "system",
        started_at=app_now(),
    )
    db.add(batch)
    await db.flush()

    success_count = 0
    failed_count = 0
    for row in parsed_rows:
        source_database = row["source_database"]
        source_table = row["source_table"]
        publish_sql = "\n".join(_grant_source_table_sql(source_database, source_table, user.db_username) for user in users)
        offline_sql = "\n".join(_revoke_source_table_sql(source_database, source_table, user.db_username) for user in users)
        table = BatchAuthGrantTable(
            batch_id=batch.id,
            source_database=source_database,
            source_table=source_table,
            source_object_level=row.get("source_object_level") or None,
            target_database=source_database,
            target_object=source_table,
            target_object_type="source_table",
            publish_sql=publish_sql,
            offline_sql=offline_sql,
            state="running",
        )
        db.add(table)
        await db.flush()

        user_success_count = 0
        user_failed_count = 0
        for user in users:
            grant_sql = _grant_source_table_sql(source_database, source_table, user.db_username)
            revoke_sql = _revoke_source_table_sql(source_database, source_table, user.db_username)
            grant_state = "pending"
            granted_at = None
            error_message = None
            checked_at = app_now()
            privilege_existed_before = False
            granted_by_this_batch = False
            lease = None
            try:
                lease, privilege_existed_before, granted_by_this_batch = (
                    await _acquire_privilege_lease_for_grant(
                        db,
                        profile,
                        batch,
                        source_database,
                        source_table,
                        user.db_username,
                        user.db_user_identity,
                        "SELECT",
                    )
                )
                if granted_by_this_batch:
                    grant_state = "succeeded"
                    granted_at = lease.granted_at
                else:
                    grant_state = "skipped"
                    if lease.ownership_state == "system":
                        error_message = "该权限由本系统其他有效批次创建，本批次已共享租约引用。"
                    elif lease.ownership_state == "external":
                        error_message = "系统接管前用户已拥有该权限，本批次已登记引用且不会取得回收所有权。"
                    else:
                        error_message = "该权限历史归属未知，本批次已登记引用并采用保守回收策略。"
                user_success_count += 1
            except Exception as exc:
                grant_state = "failed"
                error_message = str(exc)
                user_failed_count += 1
            grant_user = BatchAuthGrantUser(
                batch_id=batch.id,
                table_id=table.id,
                lease_id=lease.id if lease else None,
                db_username=user.db_username,
                db_user_identity=user.db_user_identity,
                privilege_type="SELECT",
                grant_state=grant_state,
                revoke_state="pending" if grant_state in {"succeeded", "skipped"} else "skipped",
                privilege_existed_before=privilege_existed_before,
                granted_by_this_batch=granted_by_this_batch,
                revoke_decision=(
                    "skip_existing"
                    if lease and lease.ownership_state == "external"
                    else None
                ),
                revoke_decision_reason=(
                    "系统接管前已存在该权限；最终回收时由共享租约保护。"
                    if lease and lease.ownership_state == "external"
                    else None
                ),
                checked_before_grant_at=checked_at,
                grant_sql=grant_sql,
                revoke_sql=revoke_sql,
                granted_at=granted_at,
                error_message=error_message,
            )
            db.add(grant_user)
            if lease and grant_state in {"succeeded", "skipped"}:
                await db.flush()
                lease.active_reference_count = await _active_privilege_reference_count_async(
                    db,
                    lease,
                )
        if user_failed_count:
            table.state = "failed" if user_success_count == 0 else "partial"
            table.error_message = f"源表授权完成：成功 {user_success_count} 个用户，失败 {user_failed_count} 个用户。"
            failed_count += 1
        else:
            table.state = "succeeded"
            table.published_at = app_now()
            success_count += 1
    batch.success_table_count = success_count
    batch.failed_table_count = failed_count
    batch.state = "succeeded" if failed_count == 0 else ("failed" if success_count == 0 else "partial")
    batch.message = f"源表授权完成：成功 {success_count} 张表，失败 {failed_count} 张表。"
    batch.finished_at = app_now()
    batch_id = batch.id
    await db.commit()
    return await get_grant_batch(db, batch_id)


async def list_grant_batches(db: AsyncSession) -> list[BatchAuthGrantBatchResponse]:
    batches = (
        await db.execute(select(BatchAuthGrantBatch).order_by(BatchAuthGrantBatch.created_at.desc()))
    ).scalars().all()
    return [_grant_batch_response(item, [], []) for item in batches]


async def get_grant_batch(db: AsyncSession, batch_id: uuid.UUID) -> BatchAuthGrantBatchResponse:
    batch = await db.get(BatchAuthGrantBatch, batch_id)
    if not batch:
        raise ValueError("授权批次不存在。")
    await db.refresh(batch)
    tables = (
        await db.execute(
            select(BatchAuthGrantTable)
            .where(BatchAuthGrantTable.batch_id == batch_id)
            .order_by(BatchAuthGrantTable.created_at.asc())
        )
    ).scalars().all()
    users = (
        await db.execute(
            select(BatchAuthGrantUser)
            .where(BatchAuthGrantUser.batch_id == batch_id)
            .order_by(BatchAuthGrantUser.id.asc())
        )
    ).scalars().all()
    return _grant_batch_response(batch, tables, users)


def _privilege_lease_key_conditions(
    connection_id: uuid.UUID,
    db_user_identity: str,
    source_database: str,
    source_table: str,
    privilege_type: str,
) -> tuple[Any, ...]:
    lease_key_hash = _privilege_lease_key_hash(
        connection_id,
        db_user_identity,
        source_database,
        source_table,
        privilege_type,
    )
    return (
        BatchAuthPrivilegeLease.lease_key_hash == lease_key_hash,
    )


def _privilege_lease_key_hash(
    connection_id: uuid.UUID,
    db_user_identity: str,
    source_database: str,
    source_table: str,
    privilege_type: str,
) -> str:
    canonical = "\x1f".join(
        (
            str(connection_id),
            db_user_identity.strip().lower(),
            source_database.strip().lower(),
            source_table.strip().lower(),
            privilege_type.strip().upper(),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _acquire_privilege_lease_for_grant(
    db: AsyncSession,
    profile: DatabaseConnectionProfile,
    batch: BatchAuthGrantBatch,
    source_database: str,
    source_table: str,
    db_username: str,
    db_user_identity: str,
    privilege_type: str,
) -> tuple[BatchAuthPrivilegeLease, bool, bool]:
    lease = (
        await db.execute(
            select(BatchAuthPrivilegeLease)
            .where(
                *_privilege_lease_key_conditions(
                    batch.connection_id,
                    db_user_identity,
                    source_database,
                    source_table,
                    privilege_type,
                )
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    created = lease is None
    if lease is None:
        lease = BatchAuthPrivilegeLease(
            lease_key_hash=_privilege_lease_key_hash(
                batch.connection_id,
                db_user_identity,
                source_database,
                source_table,
                privilege_type,
            ),
            connection_id=batch.connection_id,
            db_user_identity=db_user_identity,
            source_database=source_database,
            source_table=source_table,
            privilege_type=privilege_type,
            ownership_state="unknown",
            state="initializing",
            last_checked_at=app_now(),
        )
        try:
            async with db.begin_nested():
                db.add(lease)
                await db.flush()
        except IntegrityError:
            lease = (
                await db.execute(
                    select(BatchAuthPrivilegeLease)
                    .where(
                        *_privilege_lease_key_conditions(
                            batch.connection_id,
                            db_user_identity,
                            source_database,
                            source_table,
                            privilege_type,
                        )
                    )
                    .with_for_update()
                )
            ).scalar_one()
            created = False

    try:
        privilege_exists = _source_table_privilege_exists(
            profile,
            source_database,
            source_table,
            db_username,
        )
        lease.last_checked_at = app_now()
        lease.last_error = None
        if privilege_exists:
            preserve_system_ownership = (
                not created
                and lease.state != "revoked"
                and lease.ownership_state == "system"
                and lease.owned_by_system
            )
            preserve_unknown_ownership = (
                not created
                and lease.state != "revoked"
                and lease.ownership_state == "unknown"
            )
            if not preserve_system_ownership and not preserve_unknown_ownership:
                lease.baseline_existed_before_system = True
                lease.owned_by_system = False
                lease.ownership_state = "external"
                lease.grant_owner_batch_id = None
                lease.granted_at = None
            lease.state = "active"
            lease.revoked_at = None
            return lease, True, False

        _grant_source_table(profile, source_database, source_table, db_username)
        now = app_now()
        lease.baseline_existed_before_system = False
        lease.owned_by_system = True
        lease.ownership_state = "system"
        lease.grant_owner_batch_id = batch.id
        lease.state = "active"
        lease.granted_at = now
        lease.revoked_at = None
        lease.last_checked_at = now
        return lease, False, True
    except Exception as exc:
        lease.state = "error"
        lease.last_error = str(exc)
        lease.last_checked_at = app_now()
        raise


def _privilege_reference_condition(lease: BatchAuthPrivilegeLease) -> Any:
    return or_(
        BatchAuthGrantUser.lease_id == lease.id,
        and_(
            BatchAuthGrantUser.lease_id.is_(None),
            BatchAuthGrantUser.db_user_identity == lease.db_user_identity,
            BatchAuthGrantUser.privilege_type == lease.privilege_type,
            BatchAuthGrantTable.source_database == lease.source_database,
            BatchAuthGrantTable.source_table == lease.source_table,
        ),
    )


def _active_privilege_reference_statement(
    lease: BatchAuthPrivilegeLease,
    *,
    exclude_batch_id: uuid.UUID | None = None,
) -> Any:
    conditions = [
        _privilege_reference_condition(lease),
        BatchAuthGrantUser.grant_state.in_(("succeeded", "skipped")),
        BatchAuthGrantUser.revoke_state != "succeeded",
        BatchAuthGrantTable.state.in_(("running", "succeeded", "partial")),
        BatchAuthGrantBatch.connection_id == lease.connection_id,
        BatchAuthGrantBatch.state.in_(("succeeded", "partial", "running")),
        BatchAuthGrantBatch.expires_at > app_now(),
        BatchAuthGrantBatch.offlined_at.is_(None),
    ]
    if exclude_batch_id is not None:
        conditions.append(BatchAuthGrantUser.batch_id != exclude_batch_id)
    return (
        select(func.count(BatchAuthGrantUser.id))
        .join(BatchAuthGrantTable, BatchAuthGrantTable.id == BatchAuthGrantUser.table_id)
        .join(BatchAuthGrantBatch, BatchAuthGrantBatch.id == BatchAuthGrantUser.batch_id)
        .where(*conditions)
    )


async def _active_privilege_reference_count_async(
    db: AsyncSession,
    lease: BatchAuthPrivilegeLease,
    *,
    exclude_batch_id: uuid.UUID | None = None,
) -> int:
    value = await db.scalar(
        _active_privilege_reference_statement(lease, exclude_batch_id=exclude_batch_id)
    )
    return int(value or 0)


def _active_privilege_reference_count(
    session: Session,
    lease: BatchAuthPrivilegeLease,
    *,
    exclude_batch_id: uuid.UUID | None = None,
) -> int:
    value = session.scalar(
        _active_privilege_reference_statement(lease, exclude_batch_id=exclude_batch_id)
    )
    return int(value or 0)


def _privilege_lease_revoke_plan(
    lease: BatchAuthPrivilegeLease,
    active_reference_count: int,
) -> tuple[str, str, bool]:
    if active_reference_count > 0:
        return (
            "skip_referenced",
            f"仍有 {active_reference_count} 个有效批次引用该权限，跳过回收。",
            False,
        )
    if lease.state == "revoked":
        return "skip_already_revoked", "共享权限租约已完成回收，无需重复执行。", False
    if lease.ownership_state == "system" and lease.owned_by_system:
        return "revoke", "最后一个有效引用已结束，回收本系统创建的权限。", True
    if lease.ownership_state == "external":
        return "skip_existing", "该权限在系统接管前已存在，禁止自动回收。", False
    return "skip_ownership_unknown", "该权限历史归属未知，按保守策略跳过自动回收。", False


async def _lease_for_revoke_async(
    db: AsyncSession,
    batch: BatchAuthGrantBatch,
    table: BatchAuthGrantTable,
    grant_user: BatchAuthGrantUser,
) -> BatchAuthPrivilegeLease:
    lease = None
    if grant_user.lease_id:
        lease = (
            await db.execute(
                select(BatchAuthPrivilegeLease)
                .where(BatchAuthPrivilegeLease.id == grant_user.lease_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    if lease is None:
        lease = (
            await db.execute(
                select(BatchAuthPrivilegeLease)
                .where(
                    *_privilege_lease_key_conditions(
                        batch.connection_id,
                        grant_user.db_user_identity,
                        table.source_database,
                        table.source_table,
                        grant_user.privilege_type,
                    )
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    if lease is None:
        lease = BatchAuthPrivilegeLease(
            lease_key_hash=_privilege_lease_key_hash(
                batch.connection_id,
                grant_user.db_user_identity,
                table.source_database,
                table.source_table,
                grant_user.privilege_type,
            ),
            connection_id=batch.connection_id,
            db_user_identity=grant_user.db_user_identity,
            source_database=table.source_database,
            source_table=table.source_table,
            privilege_type=grant_user.privilege_type,
            ownership_state="unknown",
            owned_by_system=False,
            state="active",
            last_checked_at=app_now(),
        )
        db.add(lease)
        await db.flush()
    grant_user.lease_id = lease.id
    return lease


def _lease_for_revoke(
    session: Session,
    batch: BatchAuthGrantBatch,
    table: BatchAuthGrantTable,
    grant_user: BatchAuthGrantUser,
) -> BatchAuthPrivilegeLease:
    lease = None
    if grant_user.lease_id:
        lease = session.execute(
            select(BatchAuthPrivilegeLease)
            .where(BatchAuthPrivilegeLease.id == grant_user.lease_id)
            .with_for_update()
        ).scalar_one_or_none()
    if lease is None:
        lease = session.execute(
            select(BatchAuthPrivilegeLease)
            .where(
                *_privilege_lease_key_conditions(
                    batch.connection_id,
                    grant_user.db_user_identity,
                    table.source_database,
                    table.source_table,
                    grant_user.privilege_type,
                )
            )
            .with_for_update()
        ).scalar_one_or_none()
    if lease is None:
        lease = BatchAuthPrivilegeLease(
            lease_key_hash=_privilege_lease_key_hash(
                batch.connection_id,
                grant_user.db_user_identity,
                table.source_database,
                table.source_table,
                grant_user.privilege_type,
            ),
            connection_id=batch.connection_id,
            db_user_identity=grant_user.db_user_identity,
            source_database=table.source_database,
            source_table=table.source_table,
            privilege_type=grant_user.privilege_type,
            ownership_state="unknown",
            owned_by_system=False,
            state="active",
            last_checked_at=app_now(),
        )
        session.add(lease)
        session.flush()
    grant_user.lease_id = lease.id
    return lease


async def offline_grant_batch(db: AsyncSession, batch_id: uuid.UUID) -> BatchAuthGrantBatchResponse:
    batch = await db.get(BatchAuthGrantBatch, batch_id)
    if not batch:
        raise ValueError("授权批次不存在。")
    profile = await _doris_profile_async(db, batch.connection_id)
    tables = (
        await db.execute(
            select(BatchAuthGrantTable)
            .where(BatchAuthGrantTable.batch_id == batch_id)
            .order_by(BatchAuthGrantTable.created_at.asc())
        )
    ).scalars().all()
    grant_users = (
        await db.execute(
            select(BatchAuthGrantUser)
            .where(BatchAuthGrantUser.batch_id == batch_id)
            .order_by(BatchAuthGrantUser.id.asc())
        )
    ).scalars().all()
    users_by_table: dict[uuid.UUID, list[BatchAuthGrantUser]] = {}
    for grant_user in grant_users:
        users_by_table.setdefault(grant_user.table_id, []).append(grant_user)
    success = 0
    failed = 0
    for table in tables:
        if table.state == "offlined":
            success += 1
            continue
        if table.state not in {"succeeded", "partial"}:
            continue
        table_failed = False
        for grant_user in users_by_table.get(table.id, []):
            if grant_user.grant_state not in {"succeeded", "skipped"} or grant_user.revoke_state == "succeeded":
                continue
            grant_user.checked_before_revoke_at = app_now()
            lease = await _lease_for_revoke_async(db, batch, table, grant_user)
            active_references = await _active_privilege_reference_count_async(
                db,
                lease,
                exclude_batch_id=batch.id,
            )
            lease.active_reference_count = active_references
            decision, reason, should_revoke = _privilege_lease_revoke_plan(
                lease,
                active_references,
            )
            grant_user.revoke_decision = decision
            grant_user.revoke_decision_reason = reason
            if not should_revoke:
                grant_user.revoke_state = "skipped"
                continue
            try:
                _revoke_source_table(profile, table.source_database, table.source_table, grant_user.db_username)
                grant_user.revoke_state = "succeeded"
                grant_user.revoked_at = app_now()
                lease.state = "revoked"
                lease.active_reference_count = 0
                lease.revoked_at = grant_user.revoked_at
                lease.last_checked_at = grant_user.revoked_at
                lease.last_error = None
            except Exception as exc:
                grant_user.revoke_state = "failed"
                grant_user.revoke_decision = "failed"
                grant_user.revoke_decision_reason = "回收 SQL 执行失败。"
                grant_user.error_message = str(exc)
                lease.state = "error"
                lease.last_error = str(exc)
                lease.last_checked_at = app_now()
                table_failed = True
        if table_failed:
            table.error_message = "部分用户源表权限回收失败，请查看用户授权明细。"
            failed += 1
        else:
            table.state = "offlined"
            table.offlined_at = app_now()
            table.error_message = "本批次权限引用已结束，共享租约已完成回收判断。"
            success += 1
    batch.offlined_at = app_now() if failed == 0 else None
    batch.state = "offlined" if failed == 0 else "partial"
    batch.message = f"手动回收完成：成功 {success} 张表，失败 {failed} 张表。"
    await db.commit()
    return await get_grant_batch(db, batch_id)


async def extend_grant_batch(db: AsyncSession, batch_id: uuid.UUID, expires_at: datetime) -> BatchAuthGrantBatchResponse:
    batch = await db.get(BatchAuthGrantBatch, batch_id)
    if not batch:
        raise ValueError("授权批次不存在。")
    expires_at = _naive_utc(expires_at)
    if expires_at <= app_now():
        raise ValueError("新的到期时间必须晚于当前时间。")
    batch.expires_at = expires_at
    if batch.state in {"expired", "offlined"}:
        batch.state = "succeeded"
        batch.offlined_at = None
    await db.commit()
    return await get_grant_batch(db, batch_id)


def start_batch_authorization_scheduler() -> None:
    global _SCHEDULER_THREAD
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return
    _SCHEDULER_STOP.clear()
    _SCHEDULER_THREAD = threading.Thread(target=_scheduler_loop, name="batch-auth-offline-scheduler", daemon=True)
    _SCHEDULER_THREAD.start()


def stop_batch_authorization_scheduler() -> None:
    _SCHEDULER_STOP.set()


def _scheduler_loop() -> None:
    while not _SCHEDULER_STOP.wait(60):
        try:
            _offline_expired_batches()
        except Exception:
            continue


def _offline_expired_batches() -> None:
    session = get_sync_session_factory()()
    try:
        batches = (
            session.execute(
                select(BatchAuthGrantBatch)
                .where(
                    BatchAuthGrantBatch.expires_at <= app_now(),
                    BatchAuthGrantBatch.state.in_(["succeeded", "partial"]),
                    BatchAuthGrantBatch.offlined_at.is_(None),
                )
                .order_by(BatchAuthGrantBatch.expires_at.asc())
                .limit(20)
            )
            .scalars()
            .all()
        )
        for batch in batches:
            profile = session.get(DatabaseConnectionProfile, batch.connection_id)
            if not profile or profile.engine != "doris":
                batch.state = "failed"
                batch.message = "Doris 数据连接不存在，无法到期下线。"
                continue
            tables = (
                session.execute(
                    select(BatchAuthGrantTable)
                    .where(BatchAuthGrantTable.batch_id == batch.id)
                    .order_by(BatchAuthGrantTable.created_at.asc())
                )
                .scalars()
                .all()
            )
            _offline_tables_with_session(session, profile, batch, tables)
        session.commit()
    finally:
        session.close()


def _offline_tables_with_session(
    session: AsyncSession | Session,
    profile: DatabaseConnectionProfile,
    batch: BatchAuthGrantBatch,
    tables: list[BatchAuthGrantTable],
) -> None:
    grant_users = (
        session.execute(
            select(BatchAuthGrantUser)
            .where(BatchAuthGrantUser.batch_id == batch.id)
            .order_by(BatchAuthGrantUser.id.asc())
        )
        .scalars()
        .all()
    )
    users_by_table: dict[uuid.UUID, list[BatchAuthGrantUser]] = {}
    for grant_user in grant_users:
        users_by_table.setdefault(grant_user.table_id, []).append(grant_user)
    success = 0
    failed = 0
    for table in tables:
        if table.state == "offlined":
            success += 1
            continue
        if table.state not in {"succeeded", "partial"}:
            continue
        table_failed = False
        for grant_user in users_by_table.get(table.id, []):
            if grant_user.grant_state not in {"succeeded", "skipped"} or grant_user.revoke_state == "succeeded":
                continue
            grant_user.checked_before_revoke_at = app_now()
            lease = _lease_for_revoke(session, batch, table, grant_user)
            active_references = _active_privilege_reference_count(
                session,
                lease,
                exclude_batch_id=batch.id,
            )
            lease.active_reference_count = active_references
            decision, reason, should_revoke = _privilege_lease_revoke_plan(
                lease,
                active_references,
            )
            grant_user.revoke_decision = decision
            grant_user.revoke_decision_reason = reason
            if not should_revoke:
                grant_user.revoke_state = "skipped"
                continue
            try:
                _revoke_source_table(profile, table.source_database, table.source_table, grant_user.db_username)
                grant_user.revoke_state = "succeeded"
                grant_user.revoked_at = app_now()
                lease.state = "revoked"
                lease.active_reference_count = 0
                lease.revoked_at = grant_user.revoked_at
                lease.last_checked_at = grant_user.revoked_at
                lease.last_error = None
            except Exception as exc:
                grant_user.revoke_state = "failed"
                grant_user.revoke_decision = "failed"
                grant_user.revoke_decision_reason = "回收 SQL 执行失败。"
                grant_user.error_message = str(exc)
                lease.state = "error"
                lease.last_error = str(exc)
                lease.last_checked_at = app_now()
                table_failed = True
        if table_failed:
            table.error_message = "部分用户源表权限回收失败，请查看用户授权明细。"
            failed += 1
        else:
            table.state = "offlined"
            table.offlined_at = app_now()
            table.error_message = "本批次权限引用已结束，共享租约已完成回收判断。"
            success += 1
    batch.offlined_at = app_now() if failed == 0 else None
    batch.state = "offlined" if failed == 0 else "partial"
    batch.message = f"到期回收完成：成功 {success} 张表，失败 {failed} 张表。"


async def _doris_profile_async(db: AsyncSession, connection_id: uuid.UUID) -> DatabaseConnectionProfile:
    profile = await db.get(DatabaseConnectionProfile, connection_id)
    if not profile:
        raise ValueError("数据连接不存在。")
    if profile.engine != "doris":
        raise ValueError("请选择 Doris 类型的数据连接。")
    return profile


async def _department_context(
    db: AsyncSession,
    department_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> tuple[BatchAuthDepartment, list[BatchAuthDepartmentUser], list[BatchAuthDepartmentDatabase]]:
    department = await db.get(BatchAuthDepartment, department_id)
    if not department or department.status != "active":
        raise ValueError("部门不存在或已停用。")
    users = (
        await db.execute(
            select(BatchAuthDepartmentUser).where(
                BatchAuthDepartmentUser.department_id == department_id,
                BatchAuthDepartmentUser.status == "active",
            )
        )
    ).scalars().all()
    if not users:
        raise ValueError("该部门尚未配置数据库用户。")
    databases = (
        await db.execute(
            select(BatchAuthDepartmentDatabase).where(
                BatchAuthDepartmentDatabase.department_id == department_id,
                BatchAuthDepartmentDatabase.connection_id == connection_id,
                BatchAuthDepartmentDatabase.status == "active",
            )
        )
    ).scalars().all()
    if not databases:
        raise ValueError("该部门尚未配置当前 Doris 连接下的部门库。")
    return department, users, databases


async def _ensure_department(db: AsyncSession, name: str, actor: AuthContext | None) -> BatchAuthDepartment:
    existing = (
        await db.execute(select(BatchAuthDepartment).where(BatchAuthDepartment.name == name))
    ).scalar_one_or_none()
    if existing:
        existing.status = "active"
        return existing
    department = BatchAuthDepartment(
        name=name,
        status="active",
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
    )
    db.add(department)
    await db.flush()
    return department


async def _ensure_department_user(
    db: AsyncSession,
    department_id: uuid.UUID,
    db_username: str,
    identity: str,
    display_name: str | None,
) -> None:
    existing = (
        await db.execute(
            select(BatchAuthDepartmentUser).where(
                BatchAuthDepartmentUser.department_id == department_id,
                BatchAuthDepartmentUser.db_user_identity == identity,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.display_name = display_name or existing.display_name
        existing.status = "active"
        return
    db.add(
        BatchAuthDepartmentUser(
            department_id=department_id,
            db_username=db_username,
            db_user_identity=identity,
            display_name=display_name,
            status="active",
        )
    )


async def _ensure_department_database(
    db: AsyncSession,
    department_id: uuid.UUID,
    connection_id: uuid.UUID,
    department_database: str,
) -> None:
    existing = (
        await db.execute(
            select(BatchAuthDepartmentDatabase).where(
                BatchAuthDepartmentDatabase.department_id == department_id,
                BatchAuthDepartmentDatabase.connection_id == connection_id,
                BatchAuthDepartmentDatabase.department_database == department_database,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.status = "active"
        return
    db.add(
        BatchAuthDepartmentDatabase(
            department_id=department_id,
            connection_id=connection_id,
            department_database=department_database,
            default_privilege="SELECT",
            status="active",
        )
    )


def _build_init_preview(profile: DatabaseConnectionProfile, rows: list[dict[str, Any]], filename: str) -> BatchAuthInitPreviewResponse:
    preview_rows: list[BatchAuthInitPreviewRow] = []
    issues: list[BatchAuthPreviewIssue] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_databases: set[tuple[str, str]] = set()
    for row in rows:
        messages: list[str] = []
        valid = True
        if not row["department_name"]:
            valid = False
            messages.append("部门名称不能为空。")
        if not _USERNAME_RE.fullmatch(row["db_username"] or ""):
            valid = False
            messages.append("数据库用户名必须以字母或下划线开头，仅允许字母、数字和下划线。")
        if not _IDENT_RE.fullmatch(row["department_database"] or ""):
            valid = False
            messages.append("部门库名称格式不合法。")
        pair = (row["department_name"], row["db_username"])
        if pair in seen_pairs:
            messages.append("同一文件内部门用户重复，执行时将复用。")
        seen_pairs.add(pair)
        db_pair = (row["department_name"], row["department_database"])
        if db_pair in seen_databases:
            messages.append("同一文件内部门库重复，执行时将复用。")
        seen_databases.add(db_pair)
        if not row["initial_password"]:
            messages.append("未填写初始密码，执行时将使用默认密码。")
        for message in messages:
            if not valid:
                issues.append(BatchAuthPreviewIssue(row_no=row["row_no"], level="error", message=message))
        preview_rows.append(
            BatchAuthInitPreviewRow(
                row_no=row["row_no"],
                department_name=row["department_name"],
                db_username=row["db_username"],
                db_user_identity=_db_user_identity(row["db_username"]),
                display_name=row["display_name"],
                department_database=row["department_database"],
                initial_password_provided=bool(row["initial_password"]),
                generated_password=False,
                valid=valid,
                messages=messages,
            )
        )
    invalid = len([item for item in preview_rows if not item.valid])
    return BatchAuthInitPreviewResponse(
        filename=filename,
        total_count=len(preview_rows),
        valid_count=len(preview_rows) - invalid,
        invalid_count=invalid,
        rows=preview_rows,
        issues=issues,
    )


def _build_grant_preview(
    profile: DatabaseConnectionProfile,
    department: BatchAuthDepartment,
    users: list[BatchAuthDepartmentUser],
    database: BatchAuthDepartmentDatabase,
    rows: list[dict[str, str]],
) -> BatchAuthGrantPreviewResponse:
    preview_rows: list[BatchAuthGrantPreviewRow] = []
    issues: list[BatchAuthPreviewIssue] = []
    seen: set[tuple[str, str]] = set()
    existing_databases = _doris_databases(profile)
    for row in rows:
        messages: list[str] = []
        valid = True
        if row["source_database"] not in existing_databases:
            valid = False
            messages.append("源数据库不存在。")
        elif not _doris_table_exists(profile, row["source_database"], row["source_table"]):
            valid = False
            messages.append("源表不存在。")
        if not _IDENT_RE.fullmatch(row["source_table"] or ""):
            valid = False
            messages.append("源表名格式不合法。")
        pair = (row["source_database"], row["source_table"])
        if pair in seen:
            valid = False
            messages.append("同一文件内源库表重复。")
        seen.add(pair)
        for message in messages:
            if not valid:
                issues.append(BatchAuthPreviewIssue(row_no=row["row_no"], level="error", message=message))
        preview_rows.append(
            BatchAuthGrantPreviewRow(
                row_no=row["row_no"],
                source_database=row["source_database"],
                source_table=row["source_table"],
                source_object_level=row.get("source_object_level") or None,
                target_database=row["source_database"],
                target_object=row["source_table"],
                valid=valid,
                messages=messages,
            )
        )
    invalid = len([item for item in preview_rows if not item.valid])
    return BatchAuthGrantPreviewResponse(
        filename=profile.name,
        department_id=department.id,
        department_name=department.name,
        department_database=database.department_database,
        user_count=len(users),
        total_count=len(preview_rows),
        valid_count=len(preview_rows) - invalid,
        invalid_count=invalid,
        rows=preview_rows,
        issues=issues,
    )


def _parse_init_rows(filename: str, content: bytes) -> list[dict[str, Any]]:
    raw_rows = _read_table_rows(filename, content)
    data = _drop_header(raw_rows, {"部门名称", "数据库用户名", "用户名称", "部门库名称"})
    rows = []
    for row_no, values in data:
        values = values + [""] * 5
        rows.append(
            {
                "row_no": row_no,
                "department_name": values[0].strip(),
                "db_username": values[1].strip(),
                "display_name": values[2].strip() or None,
                "department_database": values[3].strip(),
                "initial_password": values[4].strip(),
            }
        )
    return [row for row in rows if any([row["department_name"], row["db_username"], row["department_database"]])]


def _parse_grant_rows(filename: str, content: bytes) -> list[dict[str, str]]:
    raw_rows = _read_table_rows(filename, content)
    header = _grant_header_index(raw_rows)
    if header:
        rows = []
        for row_no, values in raw_rows[1:]:
            rows.append(
                {
                    "row_no": row_no,
                    "source_database": _value_at(values, header["source_database"]),
                    "source_table": _value_at(values, header["source_table"]),
                    "source_object_level": _value_at(values, header.get("source_object_level")),
                }
            )
        return [row for row in rows if any([row["source_database"], row["source_table"], row["source_object_level"]])]

    data = _drop_header(raw_rows, {"数据库名称", "库名", "源数据库名称", "数据表名", "表名称", "表名", "源表名称", "类型"})
    rows = []
    for row_no, values in data:
        values = values + [""] * 3
        rows.append(
            {
                "row_no": row_no,
                "source_database": values[0].strip(),
                "source_table": values[1].strip(),
                "source_object_level": values[2].strip(),
            }
        )
    return [row for row in rows if any([row["source_database"], row["source_table"], row["source_object_level"]])]


def _grant_header_index(rows: list[tuple[int, list[str]]]) -> dict[str, int] | None:
    if not rows:
        return None
    aliases = {
        "source_database": {"数据库名称", "数据库名", "库名", "源数据库名称", "源数据库", "源库名称", "源库"},
        "source_table": {"数据表名", "表名称", "表名", "源表名称", "源表名", "源表"},
        "source_object_level": {"类型", "级别", "数据级别", "表级别", "表类型"},
    }
    index: dict[str, int] = {}
    for column_index, cell in enumerate(rows[0][1]):
        clean = _normalize_header(cell)
        for field, names in aliases.items():
            if clean in {_normalize_header(name) for name in names} and field not in index:
                index[field] = column_index
    if "source_database" in index and "source_table" in index:
        return index
    return None


def _normalize_header(value: str) -> str:
    return str(value or "").strip().replace(" ", "").replace("\u3000", "").lower()


def _value_at(values: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(values):
        return ""
    return str(values[index] or "").strip()


def _read_table_rows(filename: str, content: bytes) -> list[tuple[int, list[str]]]:
    lower = filename.lower()
    if lower.endswith(".csv"):
        text = content.decode("utf-8-sig")
        return [(index + 1, [str(cell or "").strip() for cell in row]) for index, row in enumerate(csv.reader(io.StringIO(text)))]
    if lower.endswith(".xlsx"):
        return _read_xlsx_rows(content)
    raise ValueError("仅支持 .xlsx 或 .csv 文件。")


def _read_xlsx_rows(content: bytes) -> list[tuple[int, list[str]]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", ns):
                shared.append("".join(text.text or "" for text in item.findall(".//main:t", ns)))
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in zf.namelist():
            candidates = [name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
            if not candidates:
                raise ValueError("Excel 中没有可读取的工作表。")
            sheet_name = sorted(candidates)[0]
        root = ET.fromstring(zf.read(sheet_name))
    rows: list[tuple[int, list[str]]] = []
    for row_index, row in enumerate(root.findall(".//main:sheetData/main:row", ns), start=1):
        values: dict[int, str] = {}
        for cell in row.findall("main:c", ns):
            ref = cell.attrib.get("r", "")
            column_index = _column_index(ref)
            value_node = cell.find("main:v", ns)
            inline_text = cell.find("main:is/main:t", ns)
            value = ""
            if inline_text is not None:
                value = inline_text.text or ""
            elif value_node is not None:
                raw = value_node.text or ""
                if cell.attrib.get("t") == "s":
                    value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else raw
                else:
                    value = raw
            values[column_index] = value.strip()
        if values:
            max_index = max(values)
            rows.append((row_index, [values.get(i, "") for i in range(max_index + 1)]))
    return rows


def _drop_header(rows: list[tuple[int, list[str]]], header_keywords: set[str]) -> list[tuple[int, list[str]]]:
    if not rows:
        return []
    first = {cell.strip() for cell in rows[0][1] if cell.strip()}
    if first & header_keywords:
        return rows[1:]
    return rows


def _column_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(value - 1, 0)


def _ensure_doris_database_and_user(profile: DatabaseConnectionProfile, database: str, username: str, password: str) -> None:
    with _doris_conn(profile, None) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_q(database)}")
            _ensure_doris_user(cur, username, password)
            _grant_department_database(cur, database, username)


def _ensure_doris_user(cur, username: str, password: str) -> None:
    escaped_password = _sql_string(password)
    user = _doris_user(username)
    variants = [
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {escaped_password}",
        f"CREATE USER {user} IDENTIFIED BY {escaped_password}",
    ]
    for sql in variants:
        try:
            cur.execute(sql)
            return
        except Exception as exc:
            if "exists" in str(exc).lower() or "already" in str(exc).lower():
                return
            last = exc
    raise last  # type: ignore[name-defined]


def _grant_department_database(cur, database: str, username: str) -> None:
    user = _doris_user(username)
    variants = [
        f"GRANT ALL PRIVILEGES ON {_q(database)}.* TO {user}",
        f"GRANT ALL ON {_q(database)}.* TO {user}",
        f"GRANT SELECT_PRIV, LOAD_PRIV, ALTER_PRIV, CREATE_PRIV, DROP_PRIV ON {_q(database)}.* TO {user}",
        f"GRANT SELECT_PRIV ON {_q(database)}.* TO {user}",
        f"GRANT SELECT ON {_q(database)}.* TO {user}",
    ]
    last: Exception | None = None
    granted = False
    for sql in variants:
        try:
            cur.execute(sql)
            granted = True
            break
        except Exception as exc:
            last = exc
    if not granted and last:
        raise last


def _grant_source_table_sql(source_database: str, source_table: str, username: str) -> str:
    return f"GRANT SELECT_PRIV ON {_q(source_database)}.{_q(source_table)} TO {_doris_user(username)}"


def _revoke_source_table_sql(source_database: str, source_table: str, username: str) -> str:
    return f"REVOKE SELECT_PRIV ON {_q(source_database)}.{_q(source_table)} FROM {_doris_user(username)}"


def _source_table_privilege_exists(
    profile: DatabaseConnectionProfile,
    source_database: str,
    source_table: str,
    username: str,
) -> bool:
    user = _doris_user(username)
    variants = [
        f"SHOW GRANTS FOR {user}",
        f"SHOW GRANTS FOR `{username}`",
    ]
    rows: list[Any] = []
    with _doris_conn(profile, None) as conn:
        with conn.cursor() as cur:
            last_error: Exception | None = None
            for sql in variants:
                try:
                    cur.execute(sql)
                    rows = list(cur.fetchall() or [])
                    break
                except Exception as exc:
                    last_error = exc
            else:
                if last_error:
                    raise last_error
    return _grant_rows_include_source_select(rows, source_database, source_table)


def _grant_rows_include_source_select(rows: list[Any], source_database: str, source_table: str) -> bool:
    database_tokens = {_normalize_grant_token(source_database), "*"}
    table_tokens = {_normalize_grant_token(source_table), "*"}
    for row in rows:
        if isinstance(row, dict):
            values = [str(value) for value in row.values()]
        elif isinstance(row, (list, tuple)):
            values = [str(value) for value in row]
        else:
            values = [str(row)]
        text = " ".join(values).upper()
        if not any(priv in text for priv in ("SELECT_PRIV", "SELECT", "ALL PRIVILEGES", "ALL")):
            continue
        normalized = _normalize_grant_token(text)
        has_database = any(token and token in normalized for token in database_tokens)
        has_table = any(token and token in normalized for token in table_tokens)
        if has_database and has_table:
            return True
    return False


def _normalize_grant_token(value: str) -> str:
    return re.sub(r"[^0-9A-Z_\u4e00-\u9fff*]+", "_", value.upper()).strip("_")


def _grant_source_table(profile: DatabaseConnectionProfile, source_database: str, source_table: str, username: str) -> None:
    user = _doris_user(username)
    variants = [
        f"GRANT SELECT_PRIV ON {_q(source_database)}.{_q(source_table)} TO {user}",
        f"GRANT SELECT ON {_q(source_database)}.{_q(source_table)} TO {user}",
    ]
    _execute_first_success(profile, None, variants)


def _revoke_source_table(profile: DatabaseConnectionProfile, source_database: str, source_table: str, username: str) -> None:
    user = _doris_user(username)
    variants = [
        f"REVOKE SELECT_PRIV ON {_q(source_database)}.{_q(source_table)} FROM {user}",
        f"REVOKE SELECT ON {_q(source_database)}.{_q(source_table)} FROM {user}",
    ]
    _execute_first_success(profile, None, variants)


def _execute_first_success(profile: DatabaseConnectionProfile, database: str | None, variants: list[str]) -> None:
    last: Exception | None = None
    with _doris_conn(profile, database) as conn:
        with conn.cursor() as cur:
            for sql in variants:
                try:
                    cur.execute(sql)
                    return
                except Exception as exc:
                    last = exc
    if last:
        raise last


def _publish_view(profile: DatabaseConnectionProfile, source_database: str, source_table: str, target_database: str, target_object: str) -> bool:
    with _doris_conn(profile, target_database) as conn:
        with conn.cursor() as cur:
            if _target_object_exists_with_cursor(cur, target_database, target_object):
                return False
            cur.execute(
                f"CREATE VIEW {_q(target_database)}.{_q(target_object)} AS "
                f"SELECT * FROM {_q(source_database)}.{_q(source_table)}"
            )
            return True


def _drop_view(profile: DatabaseConnectionProfile, target_database: str, target_object: str) -> None:
    with _doris_conn(profile, target_database) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP VIEW IF EXISTS {_q(target_database)}.{_q(target_object)}")


def _doris_databases(profile: DatabaseConnectionProfile) -> set[str]:
    with _doris_conn(profile, None) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            return {str(next(iter(row.values()))) for row in cur.fetchall()}


def _doris_table_exists(profile: DatabaseConnectionProfile, database: str, table_name: str) -> bool:
    with _doris_conn(profile, database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                LIMIT 1
                """,
                (database, table_name),
            )
            return cur.fetchone() is not None


def _discover_initialization_mappings(
    profile: DatabaseConnectionProfile,
    *,
    user_prefix: str,
    database_prefix: str,
) -> BatchAuthDiscoveryResponse:
    available_databases = sorted([name for name in _doris_databases(profile) if name.startswith(database_prefix)])
    rows: list[BatchAuthDiscoveryRow] = []
    discovered: dict[str, dict[str, Any]] = {}
    with _doris_conn(profile, None) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW ALL GRANTS")
            grants = cur.fetchall()
    for grant in grants:
        identity = str(grant.get("UserIdentity") or "")
        username = _username_from_identity(identity)
        if not username or not _user_prefix_matches(username, user_prefix):
            continue
        comment = str(grant.get("Comment") or "").strip()
        candidates = _database_candidates_from_grant(grant, database_prefix)
        item = discovered.setdefault(
            username,
            {
                "comment": comment,
                "candidates": [],
            },
        )
        if comment and not item["comment"]:
            item["comment"] = comment
        for candidate in candidates:
            if candidate not in item["candidates"]:
                item["candidates"].append(candidate)
    for username in sorted(discovered):
        item = discovered[username]
        comment = str(item["comment"] or "").strip()
        candidates = list(item["candidates"] or [])
        database_candidates = candidates or available_databases
        default_database = _default_department_database(
            comment=comment,
            database_prefix=database_prefix,
            candidates=database_candidates,
            available_databases=available_databases,
        )
        if default_database and default_database not in database_candidates:
            database_candidates = [default_database, *database_candidates]
        department_name = comment or username
        rows.append(
            BatchAuthDiscoveryRow(
                selected=bool(default_database),
                department_name=department_name,
                db_username=username,
                db_user_identity=_db_user_identity(username),
                display_name=comment or None,
                department_database=default_database,
                database_candidates=database_candidates,
            )
        )
    return BatchAuthDiscoveryResponse(rows=rows, available_databases=available_databases)


def _username_from_identity(identity: str) -> str:
    match = re.match(r"'([^']+)'@", identity)
    if match:
        return match.group(1)
    return identity.split("@", 1)[0].strip("'")


def _user_prefix_matches(username: str, user_prefix: str) -> bool:
    prefixes = [item.strip() for item in re.split(r"[,，;；\s]+", user_prefix or "") if item.strip()]
    if not prefixes:
        return True
    return any(username.startswith(prefix) for prefix in prefixes)


def _database_candidates_from_grant(grant: dict[str, Any], database_prefix: str) -> list[str]:
    values = [
        str(grant.get("DatabasePrivs") or ""),
        str(grant.get("TablePrivs") or ""),
    ]
    found: list[str] = []
    patterns = [
        re.compile(r"internal\.'([^']+)'"),
        re.compile(r"internal\.([^:.;\s]+)"),
    ]
    for value in values:
        for pattern in patterns:
            for match in pattern.finditer(value):
                database = _clean_grant_database_name(match.group(1))
                if database.startswith(database_prefix) and database not in found:
                    found.append(database)
    return found


def _clean_grant_database_name(value: str) -> str:
    database = value.strip().strip("'`")
    if "." in database:
        database = database.split(".", 1)[0].strip().strip("'`")
    return database


def _default_department_database(
    *,
    comment: str,
    database_prefix: str,
    candidates: list[str],
    available_databases: list[str],
) -> str:
    comment = comment.strip()
    if not comment:
        return ""
    expected = f"{database_prefix}{comment}"
    pool = [*candidates, *available_databases]
    for database in pool:
        if database == expected:
            return database
    return ""


def _target_object_exists_with_cursor(cur, database: str, object_name: str) -> bool:
    cur.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        LIMIT 1
        """,
        (database, object_name),
    )
    return cur.fetchone() is not None


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


def _department_response(
    department: BatchAuthDepartment,
    users: list[BatchAuthDepartmentUser],
    databases: list[BatchAuthDepartmentDatabase],
) -> BatchAuthDepartmentResponse:
    return BatchAuthDepartmentResponse(
        id=department.id,
        name=department.name,
        description=department.description,
        status=department.status,
        users=[BatchAuthDepartmentUserResponse.model_validate(item) for item in users],
        databases=[BatchAuthDepartmentDatabaseResponse.model_validate(item) for item in databases],
        created_at=department.created_at,
        updated_at=department.updated_at,
    )


def _init_batch_response(
    batch: BatchAuthInitImportBatch,
    rows: list[BatchAuthInitImportRow],
) -> BatchAuthInitImportBatchResponse:
    return BatchAuthInitImportBatchResponse(
        id=batch.id,
        connection_id=batch.connection_id,
        connection_name=batch.connection_name,
        filename=batch.filename,
        state=batch.state,
        total_count=batch.total_count,
        success_count=batch.success_count,
        failed_count=batch.failed_count,
        message=batch.message,
        created_by_username=batch.created_by_username,
        created_at=batch.created_at,
        finished_at=batch.finished_at,
        rows=[BatchAuthInitImportRowResponse.model_validate(item) for item in rows],
    )


def _grant_batch_response(
    batch: BatchAuthGrantBatch,
    tables: list[BatchAuthGrantTable],
    users: list[BatchAuthGrantUser],
) -> BatchAuthGrantBatchResponse:
    return BatchAuthGrantBatchResponse(
        id=batch.id,
        connection_id=batch.connection_id,
        connection_name=batch.connection_name,
        department_id=batch.department_id,
        department_name=batch.department_name,
        department_database=batch.department_database,
        name=batch.name,
        filename=batch.filename,
        privilege_type=batch.privilege_type,
        starts_at=batch.starts_at,
        expires_at=batch.expires_at,
        state=batch.state,
        total_table_count=batch.total_table_count,
        success_table_count=batch.success_table_count,
        failed_table_count=batch.failed_table_count,
        message=batch.message,
        created_by_username=batch.created_by_username,
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        offlined_at=batch.offlined_at,
        tables=[BatchAuthGrantTableResponse.model_validate(item) for item in tables],
        users=[BatchAuthGrantUserResponse.model_validate(item) for item in users],
    )


def _actor_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(actor.user_id)
    except Exception:
        return None


def _naive_utc(value: datetime) -> datetime:
    return to_app_naive(value)


def _db_user_identity(username: str) -> str:
    return f"{username}@%"


def _doris_user(username: str) -> str:
    escaped = username.replace("'", "''")
    return f"'{escaped}'@'%'"


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"
