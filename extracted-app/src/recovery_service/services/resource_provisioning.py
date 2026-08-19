from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
import pymysql
from pypinyin import Style, lazy_pinyin
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from recovery_service.api.schemas.resource_provisioning import (
    ResourceProvisioningBatchCreateRequest,
    ResourceProvisioningBatchResponse,
    ResourceProvisioningPreviewResponse,
    ResourceProvisioningPreviewRow,
    ResourceProvisioningRowResponse,
    ResourceProvisioningStepResponse,
)
from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    DatabaseConnectionProfile,
    ResourceProvisioningBatch,
    ResourceProvisioningRow,
    ResourceProvisioningStepLog,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.settings import get_settings

_MOBILE_RE = re.compile(r"^1[3-9][0-9]{9}$")
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_DATABASE_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,127}$")
_STEPS = ("validate", "create_user", "create_database", "grant_database", "register_connection", "complete")
_SENSITIVE_KEY_MARKERS = ("password", "passwd", "token", "authorization", "secret")
_YOUDATA_TOKEN_CACHE: dict[tuple[str, str], str] = {}
_YOUDATA_TOKEN_LOCK = threading.Lock()
_YOUDATA_AUTH_FAILURE_KEYWORDS = (
    "请登录",
    "未登录",
    "登录失效",
    "登录过期",
    "token失效",
    "token已失效",
    "token过期",
    "token已过期",
    "unauthorized",
    "invalid token",
    "token expired",
    "not logged in",
    "login required",
)


class ResourceProvisioningStepError(ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def preview_file(filename: str, content: bytes) -> ResourceProvisioningPreviewResponse:
    if not content:
        raise ValueError("上传文件不能为空。")
    rows = _parse_rows(filename, content)
    if not rows:
        raise ValueError("文件中没有可处理的数据行。")
    preview_rows: list[ResourceProvisioningPreviewRow] = []
    for row in rows:
        issues: list[str] = []
        person_name = row["person_name"]
        department_name = row["department_name"]
        mobile = row["mobile"]
        username = generate_username(person_name, mobile)
        database_name = f"{department_name}_{person_name}" if department_name and person_name else ""
        if not person_name:
            issues.append("姓名不能为空。")
        if not department_name:
            issues.append("部门不能为空。")
        if not _MOBILE_RE.fullmatch(mobile):
            issues.append("手机号必须是 11 位中国大陆手机号。")
        if not _USERNAME_RE.fullmatch(username):
            issues.append("生成的用户名格式不合法，请在提交前修正。")
        if not _DATABASE_RE.fullmatch(database_name):
            issues.append("生成的数据库名格式不合法，请在提交前修正。")
        preview_rows.append(
            ResourceProvisioningPreviewRow(
                row_no=row["row_no"],
                person_name=person_name,
                department_name=department_name,
                mobile=mobile,
                db_username=username,
                database_name=database_name,
                valid=not issues,
                issues=issues,
            )
        )
    _mark_duplicates(preview_rows, "db_username", "批次内用户名重复。")
    _mark_duplicates(preview_rows, "database_name", "批次内数据库名重复。")
    invalid_count = sum(1 for row in preview_rows if not row.valid)
    return ResourceProvisioningPreviewResponse(
        filename=filename,
        total_count=len(preview_rows),
        valid_count=len(preview_rows) - invalid_count,
        invalid_count=invalid_count,
        rows=preview_rows,
    )


def generate_username(person_name: str, mobile: str) -> str:
    parts = lazy_pinyin(person_name.strip(), style=Style.NORMAL, errors=lambda value: list(value))
    name_part = re.sub(r"[^a-z0-9_]", "", "".join(parts).lower())
    mobile_part = re.sub(r"[^0-9]", "", mobile)
    return f"{name_part}{mobile_part}"


async def create_batch(
    db: AsyncSession,
    body: ResourceProvisioningBatchCreateRequest,
    actor: AuthContext,
) -> ResourceProvisioningBatch:
    profile = await db.get(DatabaseConnectionProfile, body.connection_id)
    if not profile or profile.engine != "doris":
        raise ValueError("请选择有效的 Doris 数据连接。")
    _validate_submission_rows(body.rows)
    settings = get_settings()
    youdata_login_name = body.youdata_login_name or None
    youdata_password = body.youdata_password.get_secret_value() if body.youdata_password else ""
    legacy_token = body.api_token.get_secret_value() if body.api_token else ""
    youdata_token_url = _derive_youdata_token_url(str(body.api_url)) if youdata_login_name else None
    batch = ResourceProvisioningBatch(
        filename=body.filename.strip() or "batch.xlsx",
        connection_id=profile.id,
        connection_name=profile.name,
        api_url=str(body.api_url),
        api_token_enc=(
            encrypt_secret(legacy_token, settings.credential_encryption_key)
            if legacy_token
            else ""
        ),
        youdata_login_name=youdata_login_name,
        youdata_password_enc=(
            encrypt_secret(youdata_password, settings.credential_encryption_key)
            if youdata_password
            else None
        ),
        youdata_token_url=youdata_token_url,
        user_password_enc=encrypt_secret(body.user_password.get_secret_value(), settings.credential_encryption_key),
        project_id=body.project_id,
        paths=[item.strip() for item in body.paths if item.strip()],
        server=body.server.strip(),
        port=body.port,
        parallelism=min(body.parallelism, settings.resource_provisioning_max_parallelism),
        state="pending",
        total_count=len(body.rows),
        created_by_user_id=uuid.UUID(actor.user_id) if actor.user_id else None,
        created_by_username=actor.username,
        created_by_auth_type=actor.auth_type,
    )
    db.add(batch)
    await db.flush()
    for item in body.rows:
        db.add(
            ResourceProvisioningRow(
                batch_id=batch.id,
                row_no=item.row_no,
                person_name=item.person_name,
                department_name=item.department_name,
                mobile=item.mobile,
                db_username=item.db_username,
                database_name=item.database_name,
                state="pending",
            )
        )
    await db.commit()
    await db.refresh(batch)
    return batch


async def list_batches(db: AsyncSession, limit: int = 50) -> list[ResourceProvisioningBatchResponse]:
    batches = (
        await db.execute(
            select(ResourceProvisioningBatch)
            .order_by(ResourceProvisioningBatch.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    ).scalars().all()
    return [_batch_response(item, []) for item in batches]


async def get_batch(db: AsyncSession, batch_id: uuid.UUID) -> ResourceProvisioningBatchResponse:
    batch = await db.get(ResourceProvisioningBatch, batch_id)
    if not batch:
        raise ValueError("开通批次不存在。")
    rows = (
        await db.execute(
            select(ResourceProvisioningRow)
            .where(ResourceProvisioningRow.batch_id == batch_id)
            .order_by(ResourceProvisioningRow.row_no.asc())
        )
    ).scalars().all()
    row_ids = [item.id for item in rows]
    steps = []
    if row_ids:
        steps = (
            await db.execute(
                select(ResourceProvisioningStepLog)
                .where(ResourceProvisioningStepLog.row_id.in_(row_ids))
                .order_by(ResourceProvisioningStepLog.row_id.asc(), ResourceProvisioningStepLog.id.asc())
            )
        ).scalars().all()
    by_row: dict[uuid.UUID, list[ResourceProvisioningStepLog]] = {}
    for item in steps:
        by_row.setdefault(item.row_id, []).append(item)
    return _batch_response(batch, [(row, by_row.get(row.id, [])) for row in rows])


async def prepare_retry(db: AsyncSession, batch_id: uuid.UUID) -> ResourceProvisioningBatch:
    batch = await db.get(ResourceProvisioningBatch, batch_id)
    if not batch:
        raise ValueError("开通批次不存在。")
    rows = (
        await db.execute(select(ResourceProvisioningRow).where(ResourceProvisioningRow.batch_id == batch_id))
    ).scalars().all()
    retry_rows = [row for row in rows if row.state in {"failed", "conflict"}]
    if not retry_rows:
        raise ValueError("当前批次没有可重试的失败行。")
    for row in retry_rows:
        row.state = "pending"
        row.current_step = None
        row.error_message = None
        row.finished_at = None
    batch.state = "pending"
    batch.failed_count = 0
    batch.finished_at = None
    batch.message = f"已提交 {len(retry_rows)} 行重试。"
    await db.commit()
    await db.refresh(batch)
    return batch


def run_batch(batch_id: uuid.UUID) -> dict[str, Any]:
    factory = get_sync_session_factory()
    with factory() as db:
        batch = db.get(ResourceProvisioningBatch, batch_id)
        if not batch:
            raise ValueError("开通批次不存在。")
        batch.state = "running"
        batch.started_at = batch.started_at or app_now()
        batch.finished_at = None
        db.commit()
        row_ids = list(
            db.scalars(
                select(ResourceProvisioningRow.id).where(
                    ResourceProvisioningRow.batch_id == batch_id,
                    ResourceProvisioningRow.state == "pending",
                )
            )
        )
        parallelism = max(1, min(batch.parallelism, get_settings().resource_provisioning_max_parallelism))

    with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="resource-provisioning") as executor:
        futures = {executor.submit(_run_row, row_id): row_id for row_id in row_ids}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                _mark_row_failed(futures[future], f"工作线程异常：{exc}")

    with factory() as db:
        batch = db.get(ResourceProvisioningBatch, batch_id)
        assert batch is not None
        rows = list(db.scalars(select(ResourceProvisioningRow).where(ResourceProvisioningRow.batch_id == batch_id)))
        success_count = sum(1 for row in rows if row.state == "succeeded")
        failed_count = sum(1 for row in rows if row.state in {"failed", "conflict"})
        batch.success_count = success_count
        batch.failed_count = failed_count
        batch.state = "succeeded" if failed_count == 0 else ("failed" if success_count == 0 else "partial")
        batch.message = f"开通完成：成功 {success_count} 行，失败 {failed_count} 行。"
        batch.finished_at = app_now()
        db.commit()
        return {"batch_id": str(batch_id), "state": batch.state, "success_count": success_count, "failed_count": failed_count}


def _run_row(row_id: uuid.UUID) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourceProvisioningRow, row_id)
        if not row or row.state != "pending":
            return
        row.state = "running"
        row.started_at = row.started_at or app_now()
        row.finished_at = None
        db.commit()
    try:
        for step in _STEPS:
            if _step_already_done(row_id, step):
                continue
            _execute_step(row_id, step)
        with factory() as db:
            row = db.get(ResourceProvisioningRow, row_id)
            assert row is not None
            row.state = "succeeded"
            row.current_step = "complete"
            row.message = "Doris 资源与外部数据连接均已开通。"
            row.error_message = None
            row.finished_at = app_now()
            db.commit()
    except Exception as exc:
        _mark_row_failed(row_id, str(exc))


def _mark_row_failed(row_id: uuid.UUID, message: str) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourceProvisioningRow, row_id)
        if not row:
            return
        row.state = "failed"
        row.error_message = message
        row.finished_at = app_now()
        db.commit()


def _execute_step(row_id: uuid.UUID, step: str) -> None:
    factory = get_sync_session_factory()
    with factory() as db:
        row = db.get(ResourceProvisioningRow, row_id)
        assert row is not None
        batch = db.get(ResourceProvisioningBatch, row.batch_id)
        assert batch is not None
        attempt = int(
            db.scalar(
                select(func.max(ResourceProvisioningStepLog.attempt)).where(
                    ResourceProvisioningStepLog.row_id == row_id,
                    ResourceProvisioningStepLog.step == step,
                )
            )
            or 0
        ) + 1
        log = ResourceProvisioningStepLog(
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
            state, message, details = _perform_step(db, batch, row, step)
            log.state = state
            log.message = message
            log.sql_text = details.get("sql_text")
            log.request_summary = details.get("request_summary")
            log.response_summary = details.get("response_summary")
        except Exception as exc:
            details = exc.details if isinstance(exc, ResourceProvisioningStepError) else {}
            log.state = "failed"
            log.error_message = str(exc)
            log.sql_text = details.get("sql_text")
            log.request_summary = details.get("request_summary")
            log.response_summary = details.get("response_summary")
            raise
        finally:
            log.duration_ms = int((time.monotonic() - started) * 1000)
            log.finished_at = app_now()
            db.commit()


def _perform_step(
    db: Session,
    batch: ResourceProvisioningBatch,
    row: ResourceProvisioningRow,
    step: str,
) -> tuple[str, str, dict[str, Any]]:
    if step == "validate":
        _validate_runtime_row(row)
        return "succeeded", "输入和生成名称校验通过。", {}
    if step == "complete":
        return "succeeded", "该行全部步骤完成。", {}

    profile = db.get(DatabaseConnectionProfile, batch.connection_id)
    if not profile:
        raise ValueError("Doris 数据连接不存在。")
    settings = get_settings()
    admin_password = decrypt_secret(profile.password_enc, settings.credential_encryption_key)
    user_password = decrypt_secret(batch.user_password_enc, settings.credential_encryption_key)

    if step == "register_connection":
        return _register_external_connection(batch, row, user_password)

    conn = pymysql.connect(
        host=profile.host,
        port=profile.port or 9030,
        user=profile.username,
        password=admin_password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=15,
        read_timeout=60,
        write_timeout=60,
    )
    try:
        with conn.cursor() as cur:
            user = _quoted_user(row.db_username)
            database = _quoted_identifier(row.database_name)
            if step == "create_user":
                if _doris_user_exists(cur, row.db_username):
                    return "skipped", "Doris 用户已存在，保留现有密码。", {
                        "sql_text": f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY ******"
                    }
                parameterized_user = _quoted_user(row.db_username, escape_percent=True)
                cur.execute(f"CREATE USER {parameterized_user} IDENTIFIED BY %s", (user_password,))
                return "succeeded", "Doris 用户创建成功。", {
                    "sql_text": f"CREATE USER {user} IDENTIFIED BY ******"
                }
            if step == "create_database":
                if _doris_database_exists(cur, row.database_name):
                    return "skipped", "Doris 数据库已存在。", {
                        "sql_text": f"CREATE DATABASE IF NOT EXISTS {database}"
                    }
                cur.execute(f"CREATE DATABASE {database}")
                return "succeeded", "Doris 数据库创建成功。", {"sql_text": f"CREATE DATABASE {database}"}
            if step == "grant_database":
                sql = f"GRANT ALL PRIVILEGES ON {database}.* TO {user}"
                try:
                    cur.execute(sql)
                except Exception:
                    sql = f"GRANT ALL ON {database}.* TO {user}"
                    cur.execute(sql)
                return "succeeded", "数据库权限授予成功。", {"sql_text": sql}
    finally:
        conn.close()
    raise ValueError(f"不支持的执行步骤：{step}")


def _register_external_connection(
    batch: ResourceProvisioningBatch,
    row: ResourceProvisioningRow,
    user_password: str,
) -> tuple[str, str, dict[str, Any]]:
    youdata_token, token_source = _resolve_youdata_token(batch)
    payload = {
        "name": row.database_name,
        "projectId": batch.project_id,
        "type": 124,
        "paths": batch.paths,
        "server": batch.server,
        "port": str(batch.port),
        "userName": row.db_username,
        "password": user_password,
        "token": youdata_token,
        "defaultSchemaName": row.database_name,
        "skipTest": False,
        "parameters": {
            "authType": "ldap",
            "dorisCatalog": "internal",
            "queryQueueSetting": {"totalQueueLength": 40, "highQueueLength": 1},
            "nullSafeEqual": False,
            "driver": "mysql-connector-5.1.49",
        },
    }
    request_summary = _registration_request_summary(payload, batch, token_source)
    response = _post_external_connection(batch.api_url, payload, request_summary)
    raw_response_data = _response_summary(response)
    response_data = _redact_sensitive(raw_response_data)
    token_refreshed = False

    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response_data):
        initial_auth_failure = response_data
        try:
            youdata_token, token_source = _refresh_youdata_token(batch, youdata_token)
        except ResourceProvisioningStepError as exc:
            refresh_response = exc.details.get("response_summary") or {}
            raise ResourceProvisioningStepError(
                f"有数 Token 失效且重新登录失败：{exc}",
                {
                    "request_summary": exc.details.get("request_summary") or request_summary,
                    "response_summary": {
                        "initialAuthFailure": initial_auth_failure,
                        "tokenRefresh": refresh_response,
                    },
                },
            ) from exc
        payload["token"] = youdata_token
        request_summary = _registration_request_summary(payload, batch, token_source)
        response = _post_external_connection(batch.api_url, payload, request_summary)
        raw_response_data = _response_summary(response)
        response_data = _redact_sensitive(raw_response_data)
        response_data["initialAuthFailure"] = initial_auth_failure
        response_data["youdataTokenRefreshed"] = True
        token_refreshed = True

    response_data["youdataTokenSource"] = token_source
    details = {
        "request_summary": request_summary,
        "response_summary": response_data,
    }
    if _uses_youdata_user_password(batch) and _is_youdata_auth_failure(response.status_code, raw_response_data):
        _invalidate_youdata_token(batch, youdata_token)
    if response.status_code < 200 or response.status_code >= 300:
        message = json.dumps(response_data, ensure_ascii=False)[:1000]
        raise ResourceProvisioningStepError(
            f"外部接口 HTTP {response.status_code}：{message}",
            details,
        )
    if _external_success(response_data):
        source_label = {
            "generated": "首次生成",
            "memory": "内存复用",
            "refreshed": "失效后刷新",
            "refreshed_by_peer": "复用并发刷新结果",
            "legacy_manual_token": "历史手工 Token",
        }.get(token_source, token_source)
        refresh_label = "，已完成一次失效刷新" if token_refreshed else ""
        return "succeeded", f"外部数据连接注册及连接测试成功；Token 来源：{source_label}{refresh_label}。", details
    message = json.dumps(response_data, ensure_ascii=False)[:1000]
    if "已存在" in message or "already exist" in message.lower():
        return "skipped", "外部数据连接已存在。", details
    raise ResourceProvisioningStepError(f"外部接口业务返回未确认成功：{message}", details)


def _post_external_connection(
    api_url: str,
    payload: dict[str, Any],
    request_summary: dict[str, Any],
) -> httpx.Response:
    try:
        return httpx.post(api_url, json=payload, timeout=httpx.Timeout(60.0, connect=15.0))
    except httpx.HTTPError as exc:
        raise ResourceProvisioningStepError(
            f"外部数据连接接口调用失败：{exc}",
            {
                "request_summary": request_summary,
                "response_summary": {"error": str(exc)},
            },
        ) from exc


def _uses_youdata_user_password(batch: ResourceProvisioningBatch) -> bool:
    return bool((getattr(batch, "youdata_login_name", None) or "").strip())


def _derive_youdata_token_url(api_url: str) -> str:
    parts = urlsplit(str(api_url).strip())
    api_suffix = "/api/dash/dataConnection/apiAdd"
    if parts.scheme not in {"http", "https"} or not parts.netloc or not parts.path.rstrip("/").endswith(api_suffix):
        raise ValueError("外部接口地址必须以 /api/dash/dataConnection/apiAdd 结尾。")
    normalized_path = parts.path.rstrip("/")
    prefix = normalized_path[: -len(api_suffix)]
    token_path = f"{prefix}/api/dash/util/genToken"
    return urlunsplit((parts.scheme, parts.netloc, token_path, "", ""))


def _youdata_cache_key(batch: ResourceProvisioningBatch) -> tuple[str, str]:
    token_url = str(getattr(batch, "youdata_token_url", None) or _derive_youdata_token_url(batch.api_url))
    login_name = str(getattr(batch, "youdata_login_name", None) or "").strip()
    if not login_name:
        raise ResourceProvisioningStepError("有数登录账号为空，无法自动生成 Token。")
    return token_url.rstrip("/"), login_name


def _resolve_youdata_token(batch: ResourceProvisioningBatch) -> tuple[str, str]:
    settings = get_settings()
    if not _uses_youdata_user_password(batch):
        legacy_token = decrypt_secret(getattr(batch, "api_token_enc", "") or "", settings.credential_encryption_key)
        if not legacy_token:
            raise ResourceProvisioningStepError("历史批次 Token 为空，无法注册外部数据连接。")
        return legacy_token, "legacy_manual_token"

    key = _youdata_cache_key(batch)
    with _YOUDATA_TOKEN_LOCK:
        cached = _YOUDATA_TOKEN_CACHE.get(key)
        if cached:
            return cached, "memory"
        token = _generate_youdata_token(batch)
        _YOUDATA_TOKEN_CACHE[key] = token
        return token, "generated"


def _refresh_youdata_token(batch: ResourceProvisioningBatch, stale_token: str) -> tuple[str, str]:
    key = _youdata_cache_key(batch)
    with _YOUDATA_TOKEN_LOCK:
        current = _YOUDATA_TOKEN_CACHE.get(key)
        if current and current != stale_token:
            return current, "refreshed_by_peer"
        token = _generate_youdata_token(batch)
        _YOUDATA_TOKEN_CACHE[key] = token
        return token, "refreshed"


def _invalidate_youdata_token(batch: ResourceProvisioningBatch, stale_token: str) -> None:
    if not _uses_youdata_user_password(batch):
        return
    key = _youdata_cache_key(batch)
    with _YOUDATA_TOKEN_LOCK:
        if _YOUDATA_TOKEN_CACHE.get(key) == stale_token:
            _YOUDATA_TOKEN_CACHE.pop(key, None)


def _generate_youdata_token(batch: ResourceProvisioningBatch) -> str:
    token_url, login_name = _youdata_cache_key(batch)
    encrypted_password = getattr(batch, "youdata_password_enc", None) or ""
    password = decrypt_secret(encrypted_password, get_settings().credential_encryption_key)
    if not password:
        raise ResourceProvisioningStepError("有数登录密码为空，无法自动生成 Token。")
    payload = {
        "tokenType": "userPassword",
        "email": login_name,
        "password": password,
    }
    request_summary = {
        "youdataAuth": {
            "tokenUrl": token_url,
            "tokenType": "userPassword",
            "email": login_name,
            "password": "******",
        }
    }
    try:
        response = httpx.post(token_url, json=payload, timeout=httpx.Timeout(30.0, connect=15.0))
    except httpx.HTTPError as exc:
        raise ResourceProvisioningStepError(
            f"有数登录接口调用失败：{exc}",
            {
                "request_summary": request_summary,
                "response_summary": {"error": str(exc)},
            },
        ) from exc
    raw_response = _response_summary(response)
    response_summary = _redact_youdata_login_response(raw_response)
    details = {
        "request_summary": request_summary,
        "response_summary": response_summary,
    }
    result = raw_response.get("result")
    if response.status_code < 200 or response.status_code >= 300:
        raise ResourceProvisioningStepError(
            f"有数登录接口 HTTP {response.status_code}：{json.dumps(response_summary, ensure_ascii=False)[:1000]}",
            details,
        )
    if raw_response.get("code") not in {200, "200"} or not isinstance(result, str) or not result.strip():
        raise ResourceProvisioningStepError(
            f"有数登录失败：{json.dumps(response_summary, ensure_ascii=False)[:1000]}",
            details,
        )
    return result.strip()


def _registration_request_summary(
    payload: dict[str, Any],
    batch: ResourceProvisioningBatch,
    token_source: str,
) -> dict[str, Any]:
    summary = _redact_sensitive(payload)
    if _uses_youdata_user_password(batch):
        token_url, login_name = _youdata_cache_key(batch)
        summary["youdataAuth"] = {
            "mode": "userPassword",
            "tokenUrl": token_url,
            "email": login_name,
            "password": "******",
            "tokenSource": token_source,
        }
    else:
        summary["youdataAuth"] = {
            "mode": "legacyManualToken",
            "tokenSource": token_source,
        }
    return summary


def _redact_youdata_login_response(value: dict[str, Any]) -> dict[str, Any]:
    summary = _redact_sensitive(value)
    if "result" in summary:
        summary["result"] = "******"
    return summary


def _is_youdata_auth_failure(status_code: int, value: dict[str, Any]) -> bool:
    if status_code in {401, 403}:
        return True
    if value.get("code") in {401, 403, "401", "403"}:
        return True
    text = json.dumps(value, ensure_ascii=False).lower().replace(" ", "")
    return any(keyword.replace(" ", "") in text for keyword in _YOUDATA_AUTH_FAILURE_KEYWORDS)


def _clear_youdata_token_cache() -> None:
    with _YOUDATA_TOKEN_LOCK:
        _YOUDATA_TOKEN_CACHE.clear()


def _response_summary(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"data": value}
    except ValueError:
        return {"text": response.text[:2000], "httpStatus": response.status_code}


def _external_success(value: dict[str, Any]) -> bool:
    if value.get("success") is True:
        return True
    code = value.get("code")
    if code in {0, 200, "0", "200"}:
        return True
    return str(value.get("status") or "").lower() in {"success", "ok", "succeeded"}


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "******" if _is_sensitive_key(key) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _step_already_done(row_id: uuid.UUID, step: str) -> bool:
    factory = get_sync_session_factory()
    with factory() as db:
        return bool(
            db.scalar(
                select(func.count()).select_from(ResourceProvisioningStepLog).where(
                    ResourceProvisioningStepLog.row_id == row_id,
                    ResourceProvisioningStepLog.step == step,
                    ResourceProvisioningStepLog.state.in_(("succeeded", "skipped")),
                )
            )
        )


def _doris_user_exists(cur, username: str) -> bool:
    try:
        cur.execute(f"SHOW GRANTS FOR {_quoted_user(username)}")
        cur.fetchall()
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "not exist" in text or "unknown user" in text or "cannot find" in text:
            return False
        raise


def _doris_database_exists(cur, database_name: str) -> bool:
    cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (database_name,))
    return cur.fetchone() is not None


def _validate_submission_rows(rows) -> None:
    usernames: set[str] = set()
    databases: set[str] = set()
    for item in rows:
        if not _MOBILE_RE.fullmatch(item.mobile):
            raise ValueError(f"第 {item.row_no} 行手机号格式不合法。")
        if not _USERNAME_RE.fullmatch(item.db_username):
            raise ValueError(f"第 {item.row_no} 行用户名格式不合法。")
        if not _DATABASE_RE.fullmatch(item.database_name):
            raise ValueError(f"第 {item.row_no} 行数据库名格式不合法。")
        if item.db_username in usernames:
            raise ValueError(f"第 {item.row_no} 行用户名在批次内重复。")
        if item.database_name in databases:
            raise ValueError(f"第 {item.row_no} 行数据库名在批次内重复。")
        usernames.add(item.db_username)
        databases.add(item.database_name)


def _validate_runtime_row(row: ResourceProvisioningRow) -> None:
    if not row.person_name or not row.department_name:
        raise ValueError("姓名和部门不能为空。")
    if not _MOBILE_RE.fullmatch(row.mobile):
        raise ValueError("手机号格式不合法。")
    if not _USERNAME_RE.fullmatch(row.db_username):
        raise ValueError("用户名格式不合法。")
    if not _DATABASE_RE.fullmatch(row.database_name):
        raise ValueError("数据库名格式不合法。")


def _batch_response(batch: ResourceProvisioningBatch, rows_with_steps) -> ResourceProvisioningBatchResponse:
    rows = []
    for row, steps in rows_with_steps:
        rows.append(
            ResourceProvisioningRowResponse(
                id=row.id,
                row_no=row.row_no,
                person_name=row.person_name,
                department_name=row.department_name,
                mobile=row.mobile,
                db_username=row.db_username,
                database_name=row.database_name,
                state=row.state,
                current_step=row.current_step,
                message=row.message,
                error_message=row.error_message,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                steps=[
                    ResourceProvisioningStepResponse(
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
    return ResourceProvisioningBatchResponse(
        id=batch.id,
        filename=batch.filename,
        connection_id=batch.connection_id,
        connection_name=batch.connection_name,
        api_url=batch.api_url,
        youdata_token_url=getattr(batch, "youdata_token_url", None),
        youdata_login_name=getattr(batch, "youdata_login_name", None),
        token_strategy=(
            "youdata_user_password"
            if _uses_youdata_user_password(batch)
            else "legacy_manual_token"
        ),
        project_id=batch.project_id,
        paths=list(batch.paths or []),
        server=batch.server,
        port=batch.port,
        parallelism=batch.parallelism,
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


def _mark_duplicates(rows: list[ResourceProvisioningPreviewRow], field: str, message: str) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(getattr(row, field) or "")
        if value:
            counts[value] = counts.get(value, 0) + 1
    for row in rows:
        if counts.get(str(getattr(row, field) or ""), 0) > 1:
            row.valid = False
            row.issues.append(message)


def _parse_rows(filename: str, content: bytes) -> list[dict[str, Any]]:
    raw_rows = _read_table_rows(filename, content)
    if not raw_rows:
        return []
    aliases = {
        "person_name": {"姓名", "名字", "人员姓名"},
        "department_name": {"部门", "部门名称", "处室", "处室名称"},
        "mobile": {"手机号", "手机号码", "联系电话", "电话"},
    }
    header: dict[str, int] = {}
    for index, cell in enumerate(raw_rows[0][1]):
        normalized = _normalize_header(cell)
        for field, names in aliases.items():
            if normalized in {_normalize_header(name) for name in names}:
                header.setdefault(field, index)
    if set(header) != set(aliases):
        raise ValueError("Excel 表头必须包含：姓名、部门、手机号。")
    rows = []
    for row_no, values in raw_rows[1:]:
        item = {
            "row_no": row_no,
            "person_name": _value_at(values, header["person_name"]),
            "department_name": _value_at(values, header["department_name"]),
            "mobile": _value_at(values, header["mobile"]),
        }
        if any(item[field] for field in ("person_name", "department_name", "mobile")):
            rows.append(item)
    return rows


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
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for item in root.findall("main:si", ns):
                    shared.append("".join(text.text or "" for text in item.findall(".//main:t", ns)))
            candidates = [name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
            if not candidates:
                raise ValueError("Excel 中没有可读取的工作表。")
            root = ET.fromstring(zf.read(sorted(candidates)[0]))
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError("Excel 文件格式损坏或无法解析。") from exc
    rows: list[tuple[int, list[str]]] = []
    for fallback_index, row in enumerate(root.findall(".//main:sheetData/main:row", ns), start=1):
        row_no = int(row.attrib.get("r") or fallback_index)
        values: dict[int, str] = {}
        for cell in row.findall("main:c", ns):
            column_index = _column_index(cell.attrib.get("r", ""))
            value_node = cell.find("main:v", ns)
            inline_nodes = cell.findall("main:is/main:t", ns)
            value = ""
            if inline_nodes:
                value = "".join(node.text or "" for node in inline_nodes)
            elif value_node is not None:
                raw = value_node.text or ""
                value = shared[int(raw)] if cell.attrib.get("t") == "s" and raw.isdigit() and int(raw) < len(shared) else raw
            values[column_index] = value.strip()
        if values:
            rows.append((row_no, [values.get(index, "") for index in range(max(values) + 1)]))
    return rows


def _column_index(ref: str) -> int:
    value = 0
    for char in "".join(ch for ch in ref if ch.isalpha()):
        value = value * 26 + ord(char.upper()) - ord("A") + 1
    return max(value - 1, 0)


def _normalize_header(value: str) -> str:
    return str(value or "").strip().replace(" ", "").replace("\u3000", "").lower()


def _value_at(values: list[str], index: int) -> str:
    return str(values[index] if index < len(values) else "").strip()


def _quoted_identifier(value: str) -> str:
    if not _DATABASE_RE.fullmatch(value):
        raise ValueError(f"数据库名格式不合法：{value}")
    return f"`{value}`"


def _quoted_user(value: str, *, escape_percent: bool = False) -> str:
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError(f"用户名格式不合法：{value}")
    host = "%%" if escape_percent else "%"
    return f"'{value}'@'{host}'"
