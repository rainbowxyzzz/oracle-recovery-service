from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pymysql
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from recovery_service.api.schemas.resource_provisioning import (
    RESOURCE_PERMISSION_OPTIONS,
    ResourcePermissionBatchCreateRequest,
    ResourcePermissionBatchResponse,
    ResourcePermissionRowResponse,
    ResourcePermissionStepResponse,
)
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now, to_app_naive
from recovery_service.core.models.task import (
    DatabaseConnectionProfile,
    ResourcePermissionBatch,
    ResourcePermissionRow,
    ResourcePermissionStepLog,
    ResourceProvisioningBatch,
    ResourceProvisioningRow,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.services.resource_provisioning import (
    _external_success,
    _invalidate_youdata_token,
    _is_youdata_auth_failure,
    _redact_sensitive,
    _refresh_youdata_token,
    _resolve_youdata_token,
    _response_summary,
    _uses_youdata_user_password,
)
from recovery_service.settings import get_settings

_STEPS = ("validate", "lookup_resource", "import_permissions", "complete")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,127}$")
_MOBILE_RE = re.compile(r"^1[3-9][0-9]{9}$")
_PERMISSION_API_SUFFIX = "/api/dash/role/importDataPermissions"
_ROLE_DELETE_API_SUFFIX = "/api/dash/role/ext/delete"
_API_ADD_SUFFIX = "/api/dash/dataConnection/apiAdd"


class ResourcePermissionStepError(ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None, *, state: str = "failed") -> None:
        super().__init__(message)
        self.details = details or {}
        self.state = state


def derive_permission_api_url(api_add_url: str) -> str:
    parts = urlsplit(str(api_add_url).strip())
    normalized_path = parts.path.rstrip("/")
    if parts.scheme not in {"http", "https"} or not parts.netloc or not normalized_path.endswith(_API_ADD_SUFFIX):
        raise ValueError("来源开通接口地址必须以 /api/dash/dataConnection/apiAdd 结尾。")
    prefix = normalized_path[: -len(_API_ADD_SUFFIX)]
    return urlunsplit((parts.scheme, parts.netloc, f"{prefix}{_PERMISSION_API_SUFFIX}", "", ""))


def validate_permission_api_url(value: str) -> str:
    parts = urlsplit(str(value).strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or not parts.path.rstrip("/").endswith(_PERMISSION_API_SUFFIX)
    ):
        raise ValueError("权限接口地址必须以 /api/dash/role/importDataPermissions 结尾。")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def derive_role_delete_api_url(permission_api_url: str) -> str:
    normalized = validate_permission_api_url(permission_api_url)
    parts = urlsplit(normalized)
    prefix = parts.path[: -len(_PERMISSION_API_SUFFIX)]
    return urlunsplit((parts.scheme, parts.netloc, f"{prefix}{_ROLE_DELETE_API_SUFFIX}", "", ""))


async def create_permission_batch(
    db: AsyncSession,
    body: ResourcePermissionBatchCreateRequest,
    actor: AuthContext,
) -> ResourcePermissionBatch:
    source = await db.get(ResourceProvisioningBatch, body.source_batch_id)
    if not source:
        raise ValueError("来源开通批次不存在。")
    lookup_profile = await db.get(DatabaseConnectionProfile, body.lookup_connection_id)
    if not lookup_profile or lookup_profile.engine != "doris":
        raise ValueError("请选择有效的 Doris 资源查询连接。")
    source_rows = (
        await db.execute(
            select(ResourceProvisioningRow)
            .where(
                ResourceProvisioningRow.batch_id == source.id,
                ResourceProvisioningRow.state == "succeeded",
            )
            .order_by(ResourceProvisioningRow.row_no.asc())
        )
    ).scalars().all()
    if not source_rows:
        raise ValueError("来源开通批次没有可授权的成功行。")
    expire_at = to_app_naive(body.expire_at)
    if expire_at <= app_now():
        raise ValueError("授权到期时间必须晚于当前时间。")
    permission_api_url = validate_permission_api_url(str(body.permission_api_url))
    batch = ResourcePermissionBatch(
        source_batch_id=source.id,
        source_filename=source.filename,
        lookup_connection_id=lookup_profile.id,
        lookup_connection_name=lookup_profile.name,
        lookup_database=body.lookup_database,
        lookup_table=body.lookup_table,
        lookup_name_column=body.lookup_name_column,
        lookup_id_column=body.lookup_id_column,
        permission_api_url=permission_api_url,
        api_token_enc=source.api_token_enc,
        youdata_login_name=source.youdata_login_name,
        youdata_password_enc=source.youdata_password_enc,
        youdata_token_url=source.youdata_token_url,
        project_id=body.project_id,
        paths=list(body.paths),
        expire_at=expire_at,
        permissions=list(body.permissions),
        parallelism=min(body.parallelism, get_settings().resource_provisioning_max_parallelism),
        lookup_timeout_seconds=body.lookup_timeout_seconds,
        lookup_interval_seconds=body.lookup_interval_seconds,
        state="pending",
        total_count=len(source_rows),
        created_by_user_id=uuid.UUID(actor.user_id) if actor.user_id else None,
        created_by_username=actor.username,
        created_by_auth_type=actor.auth_type,
    )
    db.add(batch)
    await db.flush()
    for source_row in source_rows:
        db.add(
            ResourcePermissionRow(
                batch_id=batch.id,
                source_row_id=source_row.id,
                row_no=source_row.row_no,
                person_name=source_row.person_name,
                department_name=source_row.department_name,
                mobile=source_row.mobile,
                database_name=source_row.database_name,
                state="pending",
            )
        )
    await db.commit()
    await db.refresh(batch)
    return batch


async def list_permission_batches(db: AsyncSession, limit: int = 50) -> list[ResourcePermissionBatchResponse]:
    batches = (
        await db.execute(
            select(ResourcePermissionBatch)
            .order_by(ResourcePermissionBatch.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    ).scalars().all()
    return [_batch_response(item, []) for item in batches]


async def get_permission_batch(db: AsyncSession, batch_id: uuid.UUID) -> ResourcePermissionBatchResponse:
    batch = await db.get(ResourcePermissionBatch, batch_id)
    if not batch:
        raise ValueError("数据连接授权批次不存在。")
    rows = (
        await db.execute(
            select(ResourcePermissionRow)
            .where(ResourcePermissionRow.batch_id == batch_id)
            .order_by(ResourcePermissionRow.row_no.asc())
        )
    ).scalars().all()
    row_ids = [row.id for row in rows]
    steps = []
    if row_ids:
        steps = (
            await db.execute(
                select(ResourcePermissionStepLog)
                .where(ResourcePermissionStepLog.row_id.in_(row_ids))
                .order_by(ResourcePermissionStepLog.row_id.asc(), ResourcePermissionStepLog.id.asc())
            )
        ).scalars().all()
    by_row: dict[uuid.UUID, list[ResourcePermissionStepLog]] = {}
    for step in steps:
        by_row.setdefault(step.row_id, []).append(step)
    return _batch_response(batch, [(row, by_row.get(row.id, [])) for row in rows])


async def prepare_permission_retry(db: AsyncSession, batch_id: uuid.UUID) -> ResourcePermissionBatch:
    batch = await db.get(ResourcePermissionBatch, batch_id)
    if not batch:
        raise ValueError("数据连接授权批次不存在。")
    rows = (
        await db.execute(select(ResourcePermissionRow).where(ResourcePermissionRow.batch_id == batch_id))
    ).scalars().all()
    retry_rows = [row for row in rows if row.state in {"failed", "conflict"}]
    if not retry_rows:
        raise ValueError("当前授权批次没有可重试的失败行。")
    for row in retry_rows:
        row.state = "pending"
        row.current_step = None
        row.error_message = None
        row.finished_at = None
    batch.state = "pending"
    batch.failed_count = 0
    batch.finished_at = None
    batch.message = f"已提交 {len(retry_rows)} 行授权重试。"
    await db.commit()
    await db.refresh(batch)
    return batch


def run_permission_batch(batch_id: uuid.UUID) -> dict[str, Any]:
    factory = get_sync_session_factory()
    with factory() as db:
        batch = db.get(ResourcePermissionBatch, batch_id)
        if not batch:
            raise ValueError("数据连接授权批次不存在。")
        batch.state = "running"
        batch.started_at = batch.started_at or app_now()
        batch.finished_at = None
        db.commit()
        row_ids = list(
            db.scalars(
                select(ResourcePermissionRow.id).where(
                    ResourcePermissionRow.batch_id == batch_id,
                    ResourcePermissionRow.state == "pending",
                )
            )
        )
        parallelism = max(
            1,
            min(batch.parallelism, get_settings().resource_provisioning_max_parallelism),
        )

    with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="resource-permission") as executor:
        futures = {executor.submit(_run_permission_row, row_id): row_id for row_id in row_ids}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                _mark_permission_row_failed(futures[future], f"工作线程异常：{exc}")

    with factory() as db:
        batch = db.get(ResourcePermissionBatch, batch_id)
        assert batch is not None
        rows = list(db.scalars(select(ResourcePermissionRow).where(ResourcePermissionRow.batch_id == batch_id)))
        success_count = sum(1 for row in rows if row.state == "succeeded")
        failed_count = sum(1 for row in rows if row.state in {"failed", "conflict"})
        batch.success_count = success_count
        batch.failed_count = failed_count
        batch.state = "succeeded" if failed_count == 0 else ("failed" if success_count == 0 else "partial")
        batch.message = f"授权完成：成功 {success_count} 行，失败 {failed_count} 行。"
        batch.finished_at = app_now()
        db.commit()
        return {
            "batch_id": str(batch_id),
            "state": batch.state,
            "success_count": success_count,
            "failed_count": failed_count,
        }


def _run_permission_row(row_id: uuid.UUID) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.scalar(
            select(ResourcePermissionRow)
            .where(ResourcePermissionRow.id == row_id)
            .with_for_update()
        )
        if not row or row.state != "pending":
            return
        row.state = "running"
        row.started_at = row.started_at or app_now()
        row.finished_at = None
        db.commit()
    try:
        for step in _STEPS:
            if _permission_step_already_done(row_id, step):
                continue
            _execute_permission_step(row_id, step)
        with factory() as db:
            row = db.get(ResourcePermissionRow, row_id)
            assert row is not None
            row.state = "succeeded"
            row.current_step = "complete"
            row.message = "数据连接资源权限已导入。"
            row.error_message = None
            row.finished_at = app_now()
            db.commit()
    except Exception as exc:
        state = exc.state if isinstance(exc, ResourcePermissionStepError) else "failed"
        _mark_permission_row_failed(row_id, str(exc), state=state)


def _mark_permission_row_failed(row_id: uuid.UUID, message: str, *, state: str = "failed") -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourcePermissionRow, row_id)
        if not row:
            return
        row.state = state if state in {"failed", "conflict"} else "failed"
        row.error_message = message
        row.finished_at = app_now()
        db.commit()


def _execute_permission_step(row_id: uuid.UUID, step: str) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourcePermissionRow, row_id)
        assert row is not None
        batch = db.get(ResourcePermissionBatch, row.batch_id)
        assert batch is not None
        attempt = int(
            db.scalar(
                select(func.max(ResourcePermissionStepLog.attempt)).where(
                    ResourcePermissionStepLog.row_id == row_id,
                    ResourcePermissionStepLog.step == step,
                )
            )
            or 0
        ) + 1
        log = ResourcePermissionStepLog(
            batch_id=batch.id,
            row_id=row.id,
            step=step,
            attempt=attempt,
            state="running",
            started_at=app_now(),
        )
        row.current_step = step
        db.add(log)
        db.commit()
        db.refresh(log)
        started = time.monotonic()
        try:
            state, message, details = _perform_permission_step(db, batch, row, step)
            log.state = state
            log.message = message
            log.sql_text = details.get("sql_text")
            log.request_summary = details.get("request_summary")
            log.response_summary = details.get("response_summary")
            if details.get("resource_id") is not None:
                row.resource_id = int(details["resource_id"])
            if details.get("role_id") is not None:
                row.role_id = int(details["role_id"])
                row.role_delete_state = "active"
                row.role_delete_message = None
        except Exception as exc:
            details = exc.details if isinstance(exc, ResourcePermissionStepError) else {}
            log.state = exc.state if isinstance(exc, ResourcePermissionStepError) else "failed"
            log.error_message = str(exc)
            log.sql_text = details.get("sql_text")
            log.request_summary = details.get("request_summary")
            log.response_summary = details.get("response_summary")
            raise
        finally:
            log.duration_ms = int((time.monotonic() - started) * 1000)
            log.finished_at = app_now()
            db.commit()


def _perform_permission_step(
    db: Session,
    batch: ResourcePermissionBatch,
    row: ResourcePermissionRow,
    step: str,
) -> tuple[str, str, dict[str, Any]]:
    if step == "validate":
        _validate_runtime(batch, row)
        return "succeeded", "授权人员、查询配置和接口配置校验通过。", {}
    if step == "lookup_resource":
        profile = db.get(DatabaseConnectionProfile, batch.lookup_connection_id)
        if not profile or profile.engine != "doris":
            raise ResourcePermissionStepError("Doris 资源查询连接不存在或类型不正确。")
        return _lookup_resource_id(batch, row, profile)
    if step == "import_permissions":
        if not row.resource_id:
            raise ResourcePermissionStepError("资源 ID 为空，无法导入权限。")
        return _import_data_permissions(batch, row)
    if step == "complete":
        return "succeeded", "该行数据连接授权全部完成。", {}
    raise ResourcePermissionStepError(f"不支持的授权执行步骤：{step}")


def _lookup_resource_id(
    batch: ResourcePermissionBatch,
    row: ResourcePermissionRow,
    profile: DatabaseConnectionProfile,
) -> tuple[str, str, dict[str, Any]]:
    sql = (
        f"SELECT {_quoted_identifier(batch.lookup_id_column)} "
        f"FROM {_quoted_identifier(batch.lookup_database)}.{_quoted_identifier(batch.lookup_table)} "
        f"WHERE {_quoted_identifier(batch.lookup_name_column)} = %s LIMIT 2"
    )
    log_sql = f"{sql}; -- parameters: [{row.database_name}]"
    request_summary = {
        "connectionId": str(batch.lookup_connection_id),
        "connectionName": batch.lookup_connection_name,
        "database": batch.lookup_database,
        "table": batch.lookup_table,
        "nameColumn": batch.lookup_name_column,
        "idColumn": batch.lookup_id_column,
        "name": row.database_name,
        "timeoutSeconds": batch.lookup_timeout_seconds,
        "intervalSeconds": batch.lookup_interval_seconds,
    }
    deadline = time.monotonic() + batch.lookup_timeout_seconds
    attempts = 0
    last_error: str | None = None
    while True:
        attempts += 1
        try:
            values = _query_resource_ids(profile, sql, row.database_name)
            last_error = None
        except Exception as exc:
            values = []
            last_error = str(exc)
        if len(values) > 1:
            raise ResourcePermissionStepError(
                f"资源名称 {row.database_name} 匹配到多条记录，无法确定唯一 ID。",
                {
                    "sql_text": log_sql,
                    "request_summary": request_summary,
                    "response_summary": {"attempts": attempts, "matchedCount": len(values)},
                },
                state="conflict",
            )
        if len(values) == 1:
            try:
                resource_id = int(values[0])
            except (TypeError, ValueError) as exc:
                raise ResourcePermissionStepError(
                    f"资源 ID 不是有效整数：{values[0]}",
                    {
                        "sql_text": log_sql,
                        "request_summary": request_summary,
                        "response_summary": {"attempts": attempts, "matchedCount": 1, "resourceId": values[0]},
                    },
                ) from exc
            if resource_id <= 0:
                raise ResourcePermissionStepError(
                    f"资源 ID 必须为正整数：{resource_id}",
                    {
                        "sql_text": log_sql,
                        "request_summary": request_summary,
                        "response_summary": {"attempts": attempts, "matchedCount": 1, "resourceId": resource_id},
                    },
                )
            return "succeeded", f"已查询到数据连接资源 ID：{resource_id}。", {
                "sql_text": log_sql,
                "request_summary": request_summary,
                "response_summary": {"attempts": attempts, "matchedCount": 1, "resourceId": resource_id},
                "resource_id": resource_id,
            }
        if time.monotonic() >= deadline:
            message = f"在 {batch.lookup_timeout_seconds} 秒内未查询到数据连接资源：{row.database_name}。"
            if last_error:
                message = f"资源查询持续失败：{last_error}"
            raise ResourcePermissionStepError(
                message,
                {
                    "sql_text": log_sql,
                    "request_summary": request_summary,
                    "response_summary": {
                        "attempts": attempts,
                        "matchedCount": 0,
                        "lastError": last_error,
                    },
                },
            )
        time.sleep(batch.lookup_interval_seconds)


def _query_resource_ids(profile: DatabaseConnectionProfile, sql: str, resource_name: str) -> list[Any]:
    password = decrypt_secret(profile.password_enc, get_settings().credential_encryption_key)
    connection = pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=15,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, (resource_name,))
            return [item[0] for item in cursor.fetchall()]
    finally:
        connection.close()


def _import_data_permissions(
    batch: ResourcePermissionBatch,
    row: ResourcePermissionRow,
) -> tuple[str, str, dict[str, Any]]:
    youdata_token, token_source = _resolve_youdata_token(batch)
    payload = _permission_payload(batch, row, youdata_token)
    request_summary = _permission_request_summary(batch, payload, token_source)
    response = _post_permission_api(batch.permission_api_url, payload, request_summary)
    raw_response = _response_summary(response)
    response_summary = _redact_sensitive(raw_response)
    token_refreshed = False

    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response):
        initial_auth_failure = response_summary
        try:
            youdata_token, token_source = _refresh_youdata_token(batch, youdata_token)
        except Exception as exc:
            details = exc.details if hasattr(exc, "details") else {}
            raise ResourcePermissionStepError(
                f"有数 Token 失效且重新登录失败：{exc}",
                {
                    "request_summary": details.get("request_summary") or request_summary,
                    "response_summary": {
                        "initialAuthFailure": initial_auth_failure,
                        "tokenRefresh": details.get("response_summary") or {},
                    },
                },
            ) from exc
        payload["token"] = youdata_token
        request_summary = _permission_request_summary(batch, payload, token_source)
        response = _post_permission_api(batch.permission_api_url, payload, request_summary)
        raw_response = _response_summary(response)
        response_summary = _redact_sensitive(raw_response)
        response_summary["initialAuthFailure"] = initial_auth_failure
        response_summary["youdataTokenRefreshed"] = True
        token_refreshed = True

    response_summary["youdataTokenSource"] = token_source
    details = {"request_summary": request_summary, "response_summary": response_summary}
    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response):
        _invalidate_youdata_token(batch, youdata_token)
    if response.status_code < 200 or response.status_code >= 300:
        raise ResourcePermissionStepError(
            f"权限接口 HTTP {response.status_code}：{json.dumps(response_summary, ensure_ascii=False)[:1000]}",
            details,
        )
    if _external_success(raw_response):
        role_id = _extract_role_id(raw_response)
        details["role_id"] = role_id
        source_label = {
            "generated": "首次生成",
            "memory": "内存复用",
            "refreshed": "失效后刷新",
            "refreshed_by_peer": "复用并发刷新结果",
            "legacy_manual_token": "历史手工 Token",
        }.get(token_source, token_source)
        refresh_label = "，已完成一次失效刷新" if token_refreshed else ""
        return "succeeded", f"数据连接权限导入成功，角色 ID：{role_id}；Token 来源：{source_label}{refresh_label}。", details
    message = json.dumps(response_summary, ensure_ascii=False)[:1000]
    if "已存在" in message or "already exist" in message.lower():
        role_id = _extract_role_id(raw_response)
        details["role_id"] = role_id
        return "skipped", f"数据连接权限已存在，角色 ID：{role_id}。", details
    raise ResourcePermissionStepError(f"权限接口业务返回未确认成功：{message}", details)


def _extract_role_id(response_summary: dict[str, Any]) -> int:
    value = response_summary.get("result")
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ResourcePermissionStepError(
            "权限接口已返回成功状态，但 result 不是有效角色 ID。",
            {"response_summary": _redact_sensitive(response_summary)},
        )
    try:
        role_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ResourcePermissionStepError(
            "权限接口已返回成功状态，但 result 不是有效角色 ID。",
            {"response_summary": _redact_sensitive(response_summary)},
        ) from exc
    if role_id <= 0:
        raise ResourcePermissionStepError(
            "权限接口已返回成功状态，但 result 不是正整数角色 ID。",
            {"response_summary": _redact_sensitive(response_summary)},
        )
    return role_id


def _permission_payload(
    batch: ResourcePermissionBatch,
    row: ResourcePermissionRow,
    token: str,
) -> dict[str, Any]:
    return {
        "token": token,
        "projectId": batch.project_id,
        "uniqueId": row.mobile,
        "userExpireMap": {row.mobile: batch.expire_at.strftime("%Y-%m-%d %H:%M:%S")},
        "roleName": row.database_name,
        "path": list(batch.paths or []),
        "type": 0,
        "importResourceTypes": ["DATA_CONNECTION"],
        "resourcePermissions": [
            {
                "resourceType": "DATA_CONNECTION",
                "resourceId": row.resource_id,
                "permissions": list(batch.permissions or []),
                "isFolder": 0,
            }
        ],
    }


def _permission_request_summary(
    batch: ResourcePermissionBatch,
    payload: dict[str, Any],
    token_source: str,
) -> dict[str, Any]:
    summary = _redact_sensitive(payload)
    summary["permissionApiUrl"] = batch.permission_api_url
    if _uses_youdata_user_password(batch):
        summary["youdataAuth"] = {
            "mode": "userPassword",
            "tokenUrl": batch.youdata_token_url,
            "email": batch.youdata_login_name,
            "password": "******",
            "tokenSource": token_source,
        }
    else:
        summary["youdataAuth"] = {"mode": "legacyManualToken", "tokenSource": token_source}
    return summary


def _post_permission_api(
    api_url: str,
    payload: dict[str, Any],
    request_summary: dict[str, Any],
) -> httpx.Response:
    try:
        return httpx.post(api_url, json=payload, timeout=httpx.Timeout(60.0, connect=15.0))
    except httpx.HTTPError as exc:
        raise ResourcePermissionStepError(
            f"数据连接权限接口调用失败：{exc}",
            {
                "request_summary": request_summary,
                "response_summary": {"error": str(exc)},
            },
        ) from exc


def _post_role_delete_api(
    api_url: str,
    payload: dict[str, Any],
    request_summary: dict[str, Any],
) -> httpx.Response:
    try:
        return httpx.post(api_url, json=payload, timeout=httpx.Timeout(60.0, connect=15.0))
    except httpx.HTTPError as exc:
        raise ResourcePermissionStepError(
            f"角色删除接口调用失败：{exc}",
            {
                "request_summary": request_summary,
                "response_summary": {"error": str(exc)},
            },
        ) from exc


def _delete_role(
    batch: ResourcePermissionBatch,
    role_id: int,
) -> tuple[str, str, dict[str, Any]]:
    youdata_token, token_source = _resolve_youdata_token(batch)
    api_url = derive_role_delete_api_url(batch.permission_api_url)
    payload = {"token": youdata_token, "roleId": int(role_id)}
    request_summary = _role_delete_request_summary(api_url, role_id, token_source)
    response = _post_role_delete_api(api_url, payload, request_summary)
    raw_response = _response_summary(response)
    response_summary = _redact_sensitive(raw_response)
    token_refreshed = False

    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response):
        initial_auth_failure = response_summary
        try:
            youdata_token, token_source = _refresh_youdata_token(batch, youdata_token)
        except Exception as exc:
            details = exc.details if hasattr(exc, "details") else {}
            raise ResourcePermissionStepError(
                f"角色删除 Token 失效且重新登录失败：{exc}",
                {
                    "request_summary": details.get("request_summary") or request_summary,
                    "response_summary": {
                        "initialAuthFailure": initial_auth_failure,
                        "tokenRefresh": details.get("response_summary") or {},
                    },
                },
            ) from exc
        payload["token"] = youdata_token
        token_source = "refreshed"
        request_summary = _role_delete_request_summary(api_url, role_id, token_source)
        response = _post_role_delete_api(api_url, payload, request_summary)
        raw_response = _response_summary(response)
        response_summary = _redact_sensitive(raw_response)
        response_summary["initialAuthFailure"] = initial_auth_failure
        response_summary["youdataTokenRefreshed"] = True
        token_refreshed = True

    response_summary["youdataTokenSource"] = token_source
    details = {"request_summary": request_summary, "response_summary": response_summary}
    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response):
        _invalidate_youdata_token(batch, youdata_token)
    if response.status_code < 200 or response.status_code >= 300:
        raise ResourcePermissionStepError(
            f"角色删除接口 HTTP {response.status_code}：{json.dumps(response_summary, ensure_ascii=False)[:1000]}",
            details,
        )
    if not _external_success(raw_response):
        raise ResourcePermissionStepError(
            f"角色删除接口业务返回未确认成功：{json.dumps(response_summary, ensure_ascii=False)[:1000]}",
            details,
        )
    refresh_label = "，已完成一次失效刷新" if token_refreshed else ""
    return "succeeded", f"角色 {role_id} 删除成功{refresh_label}。", details


def _role_delete_request_summary(api_url: str, role_id: int, token_source: str) -> dict[str, Any]:
    return {
        "roleDeleteApiUrl": api_url,
        "token": "******",
        "roleId": int(role_id),
        "youdataTokenSource": token_source,
    }


def delete_permission_role(batch_id: uuid.UUID, row_id: uuid.UUID) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourcePermissionRow, row_id)
        if not row or row.batch_id != batch_id:
            raise ValueError("授权行不存在。")
        batch = db.get(ResourcePermissionBatch, batch_id)
        if not batch:
            raise ValueError("数据连接授权批次不存在。")
        if row.role_id is None:
            raise ValueError("该授权行没有可删除的角色 ID。")
        if row.role_delete_state == "deleted":
            raise ValueError("该角色已经删除，不能重复操作。")
        if row.role_delete_state == "deleting":
            raise ValueError("该角色正在删除，请勿重复操作。")
        attempt = int(
            db.scalar(
                select(func.max(ResourcePermissionStepLog.attempt)).where(
                    ResourcePermissionStepLog.row_id == row_id,
                    ResourcePermissionStepLog.step == "delete_role",
                )
            )
            or 0
        ) + 1
        log = ResourcePermissionStepLog(
            batch_id=batch.id,
            row_id=row.id,
            step="delete_role",
            attempt=attempt,
            state="running",
            started_at=app_now(),
        )
        row.role_delete_state = "deleting"
        row.role_delete_message = None
        db.add(log)
        db.commit()
        db.refresh(log)
        started = time.monotonic()
        role_id = int(row.role_id)
    try:
        state, message, details = _delete_role(batch, role_id)
        with factory() as db:
            row = db.get(ResourcePermissionRow, row_id)
            log = db.get(ResourcePermissionStepLog, log.id)
            assert row is not None and log is not None
            log.state = state
            log.message = message
            log.request_summary = details.get("request_summary")
            log.response_summary = details.get("response_summary")
            row.role_delete_state = "deleted"
            row.role_delete_message = message
            row.role_deleted_at = app_now()
            log.duration_ms = int((time.monotonic() - started) * 1000)
            log.finished_at = app_now()
            db.commit()
    except Exception as exc:
        details = exc.details if isinstance(exc, ResourcePermissionStepError) else {}
        with factory() as db:
            row = db.get(ResourcePermissionRow, row_id)
            log = db.get(ResourcePermissionStepLog, log.id)
            if row is not None:
                row.role_delete_state = "delete_failed"
                row.role_delete_message = str(exc)
            if log is not None:
                log.state = "failed"
                log.error_message = str(exc)
                log.request_summary = details.get("request_summary")
                log.response_summary = details.get("response_summary")
                log.duration_ms = int((time.monotonic() - started) * 1000)
                log.finished_at = app_now()
            db.commit()
        raise


def _validate_runtime(batch: ResourcePermissionBatch, row: ResourcePermissionRow) -> None:
    if not _MOBILE_RE.fullmatch(row.mobile):
        raise ResourcePermissionStepError("手机号格式不合法。")
    if not row.database_name:
        raise ResourcePermissionStepError("资源名称不能为空。")
    for value in (
        batch.lookup_database,
        batch.lookup_table,
        batch.lookup_name_column,
        batch.lookup_id_column,
    ):
        if not _SAFE_IDENTIFIER_RE.fullmatch(value or ""):
            raise ResourcePermissionStepError("资源查询库、表或字段名称不合法。")
    validate_permission_api_url(batch.permission_api_url)
    if batch.expire_at <= app_now():
        raise ResourcePermissionStepError("授权到期时间已经失效。")
    permissions = list(batch.permissions or [])
    if not permissions or any(item not in RESOURCE_PERMISSION_OPTIONS for item in permissions):
        raise ResourcePermissionStepError("数据连接权限集合为空或包含不支持的权限。")


def _permission_step_already_done(row_id: uuid.UUID, step: str) -> bool:
    factory = get_sync_session_factory()
    with factory() as db:
        return bool(
            db.scalar(
                select(func.count()).select_from(ResourcePermissionStepLog).where(
                    ResourcePermissionStepLog.row_id == row_id,
                    ResourcePermissionStepLog.step == step,
                    ResourcePermissionStepLog.state.in_(("succeeded", "skipped")),
                )
            )
        )


def _quoted_identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER_RE.fullmatch(value or ""):
        raise ResourcePermissionStepError(f"不安全的 Doris 标识符：{value}")
    return f"`{value}`"


def _batch_response(batch: ResourcePermissionBatch, rows_with_steps) -> ResourcePermissionBatchResponse:
    rows = []
    for row, steps in rows_with_steps:
        rows.append(
            ResourcePermissionRowResponse(
                id=row.id,
                source_row_id=row.source_row_id,
                row_no=row.row_no,
                person_name=row.person_name,
                department_name=row.department_name,
                mobile=row.mobile,
                database_name=row.database_name,
                resource_id=row.resource_id,
                role_id=row.role_id,
                role_delete_state=row.role_delete_state,
                role_delete_message=row.role_delete_message,
                role_deleted_at=row.role_deleted_at,
                state=row.state,
                current_step=row.current_step,
                message=row.message,
                error_message=row.error_message,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                steps=[
                    ResourcePermissionStepResponse(
                        id=step.id,
                        step=step.step,
                        attempt=step.attempt,
                        state=step.state,
                        sql_text=step.sql_text,
                        request_summary=step.request_summary,
                        response_summary=step.response_summary,
                        message=step.message,
                        error_message=step.error_message,
                        duration_ms=step.duration_ms,
                        started_at=step.started_at,
                        finished_at=step.finished_at,
                    )
                    for step in steps
                ],
            )
        )
    return ResourcePermissionBatchResponse(
        id=batch.id,
        source_batch_id=batch.source_batch_id,
        source_filename=batch.source_filename,
        lookup_connection_id=batch.lookup_connection_id,
        lookup_connection_name=batch.lookup_connection_name,
        lookup_database=batch.lookup_database,
        lookup_table=batch.lookup_table,
        lookup_name_column=batch.lookup_name_column,
        lookup_id_column=batch.lookup_id_column,
        permission_api_url=batch.permission_api_url,
        youdata_login_name=batch.youdata_login_name,
        token_strategy="youdata_user_password" if _uses_youdata_user_password(batch) else "legacy_manual_token",
        project_id=batch.project_id,
        paths=list(batch.paths or []),
        expire_at=batch.expire_at,
        permissions=list(batch.permissions or []),
        parallelism=batch.parallelism,
        lookup_timeout_seconds=batch.lookup_timeout_seconds,
        lookup_interval_seconds=batch.lookup_interval_seconds,
        state=batch.state,
        total_count=batch.total_count,
        success_count=batch.success_count,
        failed_count=batch.failed_count,
        message=batch.message,
        created_by_username=batch.created_by_username,
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        rows=rows,
    )
