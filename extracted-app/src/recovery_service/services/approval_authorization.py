from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from recovery_service.api.schemas.approval_authorization import (
    ApprovalAuthorizationConfigPayload,
    ApprovalAuthorizationConfigResponse,
    ApprovalAuthorizationRunDetailResponse,
    ApprovalAuthorizationRunResponse,
    ApprovalAuthorizationStepLogResponse,
    ApprovalAuthorizationStepTestResponse,
)
from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    ApprovalAuthorizationConfig,
    ApprovalAuthorizationRun,
    ApprovalAuthorizationStepLog,
    DatabaseConnectionProfile,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.services.batch_authorization import (
    _doris_conn,
    _doris_user,
    _grant_department_database,
    _ensure_doris_user,
    _grant_source_table,
    _q,
)
from recovery_service.settings import get_settings

logger = logging.getLogger(__name__)

STEP_NAMES = {
    "login": "审批系统登录",
    "todo_list": "获取待办申请列表",
    "detail": "获取申请详情",
    "department_mapping": "查询部门数据库映射",
    "data_list": "获取授权数据列表",
    "table_schema_lookup": "查询表所属 schema",
    "auth_info_insert": "写入授权信息表",
    "internal_grant": "内部 Doris 用户与表授权",
    "youdata_token": "获取有数 token",
    "api_add": "创建有数数据连接",
    "import_permissions": "导入有数人员权限",
    "audit_status_update": "回写审批状态",
}

_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()
_RUNNING_STATES = {"created", "running"}


async def list_configs(db: AsyncSession) -> list[ApprovalAuthorizationConfigResponse]:
    rows = (
        await db.execute(
            select(ApprovalAuthorizationConfig).order_by(ApprovalAuthorizationConfig.created_at.desc())
        )
    ).scalars().all()
    return [_config_response(row) for row in rows]


async def get_config(db: AsyncSession, config_id: uuid.UUID) -> ApprovalAuthorizationConfigResponse:
    row = await db.get(ApprovalAuthorizationConfig, config_id)
    if not row:
        raise ValueError("审批流自动授权配置不存在。")
    return _config_response(row)


async def create_config(
    db: AsyncSession,
    payload: ApprovalAuthorizationConfigPayload,
    actor: AuthContext,
) -> ApprovalAuthorizationConfigResponse:
    _validate_payload_passwords(payload, require_all=True)
    row = ApprovalAuthorizationConfig(
        name=payload.name.strip(),
        status=payload.status,
        doris_connection_id=payload.doris_connection_id,
        workflow_base_url=payload.workflow_base_url,
        workflow_username=payload.workflow_username.strip(),
        workflow_password_enc=_encrypt_secret(payload.workflow_password),
        youdata_base_url=payload.youdata_base_url,
        youdata_email=payload.youdata_email.strip(),
        youdata_password_enc=_encrypt_secret(payload.youdata_password),
        default_doris_password_enc=_encrypt_secret(payload.default_doris_password),
        config=_normalized_config(payload.config),
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _config_response(row)


async def update_config(
    db: AsyncSession,
    config_id: uuid.UUID,
    payload: ApprovalAuthorizationConfigPayload,
) -> ApprovalAuthorizationConfigResponse:
    row = await db.get(ApprovalAuthorizationConfig, config_id)
    if not row:
        raise ValueError("审批流自动授权配置不存在。")
    row.name = payload.name.strip()
    row.status = payload.status
    row.doris_connection_id = payload.doris_connection_id
    row.workflow_base_url = payload.workflow_base_url
    row.workflow_username = payload.workflow_username.strip()
    row.youdata_base_url = payload.youdata_base_url
    row.youdata_email = payload.youdata_email.strip()
    if payload.workflow_password is not None:
        row.workflow_password_enc = _encrypt_secret(payload.workflow_password)
    if payload.youdata_password is not None:
        row.youdata_password_enc = _encrypt_secret(payload.youdata_password)
    if payload.default_doris_password is not None:
        row.default_doris_password_enc = _encrypt_secret(payload.default_doris_password)
    row.config = _normalized_config(payload.config)
    await db.commit()
    await db.refresh(row)
    return _config_response(row)


async def list_runs(db: AsyncSession, limit: int = 50) -> list[ApprovalAuthorizationRunResponse]:
    rows = (
        await db.execute(
            select(ApprovalAuthorizationRun).order_by(ApprovalAuthorizationRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [ApprovalAuthorizationRunResponse.model_validate(row) for row in rows]


async def get_run_detail(db: AsyncSession, run_id: uuid.UUID) -> ApprovalAuthorizationRunDetailResponse:
    run = await db.get(ApprovalAuthorizationRun, run_id)
    if not run:
        raise ValueError("审批流自动授权运行记录不存在。")
    logs = (
        await db.execute(
            select(ApprovalAuthorizationStepLog)
            .where(ApprovalAuthorizationStepLog.run_id == run_id)
            .order_by(ApprovalAuthorizationStepLog.started_at.asc(), ApprovalAuthorizationStepLog.id.asc())
        )
    ).scalars().all()
    return ApprovalAuthorizationRunDetailResponse(
        **ApprovalAuthorizationRunResponse.model_validate(run).model_dump(),
        logs=[ApprovalAuthorizationStepLogResponse.model_validate(item) for item in logs],
    )


async def test_step(
    db: AsyncSession,
    config_id: uuid.UUID,
    step_key: str,
    context: dict[str, Any],
    actor: AuthContext,
) -> ApprovalAuthorizationStepTestResponse:
    config = await db.get(ApprovalAuthorizationConfig, config_id)
    if not config:
        raise ValueError("审批流自动授权配置不存在。")
    run = ApprovalAuthorizationRun(
        config_id=config.id,
        config_name=config.name,
        state="running",
        message=f"正在测试步骤：{STEP_NAMES.get(step_key, step_key)}",
        context={"mode": "step_test", "step_key": step_key, "input": _redact(context)},
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username,
        started_at=app_now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run_id = run.id

    def _run() -> uuid.UUID:
        return _test_step_sync(run_id, config.id, step_key, dict(context or {}))

    log_id = await _to_thread(_run)
    await db.commit()
    run = await db.get(ApprovalAuthorizationRun, run_id)
    log = await db.get(ApprovalAuthorizationStepLog, log_id)
    if not run or not log:
        raise ValueError(f"步骤测试日志读取失败：run_id={run_id}, log_id={log_id}")
    return ApprovalAuthorizationStepTestResponse(
        run=ApprovalAuthorizationRunResponse.model_validate(run),
        log=ApprovalAuthorizationStepLogResponse.model_validate(log),
    )


async def create_full_run(
    db: AsyncSession,
    config_id: uuid.UUID,
    context: dict[str, Any],
    actor: AuthContext,
) -> ApprovalAuthorizationRunResponse:
    config = await db.get(ApprovalAuthorizationConfig, config_id)
    if not config:
        raise ValueError("审批流自动授权配置不存在。")
    run = ApprovalAuthorizationRun(
        config_id=config.id,
        config_name=config.name,
        state="running",
        message="审批流自动授权任务已启动。",
        context={"mode": "full", "input": _redact(context or {})},
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username,
        started_at=app_now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return ApprovalAuthorizationRunResponse.model_validate(run)


def run_full_run_task(run_id: uuid.UUID, config_id: uuid.UUID, context: dict[str, Any]) -> None:
    _run_full_sync(run_id, config_id, dict(context or {}))


async def create_auto_watch_run(
    db: AsyncSession,
    config_id: uuid.UUID,
    context: dict[str, Any],
    actor: AuthContext,
) -> ApprovalAuthorizationRunResponse:
    config = await db.get(ApprovalAuthorizationConfig, config_id)
    if not config:
        raise ValueError("审批流自动授权配置不存在。")
    existing = (
        await db.execute(
            select(ApprovalAuthorizationRun.id)
            .where(ApprovalAuthorizationRun.config_id == config_id)
            .where(ApprovalAuthorizationRun.state.in_(list(_RUNNING_STATES)))
            .limit(1)
        )
    ).scalars().first()
    if existing:
        raise ValueError(f"当前配置已有运行中的审批流自动授权任务：{existing}")
    cfg = _normalized_config(config.config or {})
    run = ApprovalAuthorizationRun(
        config_id=config.id,
        config_name=config.name,
        state="running",
        message="审批流自动监听扫描已启动。",
        context={
            "mode": "auto_watch",
            "input": _redact(context or {}),
            "watch": {
                "interval_minutes": _positive_int(cfg.get("auto_watch_interval_minutes"), 5, minimum=1, maximum=1440),
                "max_items_per_scan": _positive_int(cfg.get("auto_watch_max_items_per_scan"), 1, minimum=1, maximum=100),
            },
        },
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username,
        started_at=app_now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return ApprovalAuthorizationRunResponse.model_validate(run)


def start_approval_authorization_scheduler() -> None:
    global _SCHEDULER_THREAD
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return
    _SCHEDULER_STOP.clear()
    _SCHEDULER_THREAD = threading.Thread(
        target=_approval_authorization_scheduler_loop,
        name="approval-auth-auto-watch-scheduler",
        daemon=True,
    )
    _SCHEDULER_THREAD.start()


def stop_approval_authorization_scheduler() -> None:
    _SCHEDULER_STOP.set()


def _approval_authorization_scheduler_loop() -> None:
    while not _SCHEDULER_STOP.wait(60):
        try:
            _run_auto_watch_due_configs()
        except Exception:
            logger.exception("approval authorization auto watch scheduler tick failed")


def _run_auto_watch_due_configs() -> None:
    session = get_sync_session_factory()()
    try:
        configs = (
            session.execute(
                select(ApprovalAuthorizationConfig)
                .where(ApprovalAuthorizationConfig.status == "active")
                .order_by(ApprovalAuthorizationConfig.updated_at.asc())
            )
            .scalars()
            .all()
        )
        now = app_now()
        for config in configs:
            cfg = _normalized_config(config.config or {})
            if not _as_bool(cfg.get("auto_watch_enabled")):
                continue
            if _has_running_config_run(session, config.id):
                logger.info("approval_auth auto_watch config=%s skipped because a run is active", config.id)
                continue
            interval = _positive_int(cfg.get("auto_watch_interval_minutes"), 5, minimum=1, maximum=1440)
            last_scan_at = _parse_datetime(cfg.get("auto_watch_last_scan_at"))
            if last_scan_at and (now - last_scan_at).total_seconds() < interval * 60:
                continue
            updated_cfg = dict(config.config or {})
            updated_cfg["auto_watch_last_scan_at"] = now.isoformat(sep=" ", timespec="seconds")
            config.config = updated_cfg
            run = ApprovalAuthorizationRun(
                config_id=config.id,
                config_name=config.name,
                state="running",
                message="审批流自动监听扫描已启动。",
                context={
                    "mode": "auto_watch",
                    "watch": {
                        "interval_minutes": interval,
                        "max_items_per_scan": _positive_int(
                            cfg.get("auto_watch_max_items_per_scan"),
                            1,
                            minimum=1,
                            maximum=100,
                        ),
                        "trigger": "scheduler",
                    },
                },
                created_by_username="system",
                started_at=now,
            )
            session.add(run)
            session.commit()
            logger.info("approval_auth auto_watch started config=%s run=%s", config.id, run.id)
            _run_full_sync(run.id, config.id, {"mode": "auto_watch"})
    finally:
        session.close()


async def start_full_run(
    db: AsyncSession,
    config_id: uuid.UUID,
    context: dict[str, Any],
    actor: AuthContext,
) -> ApprovalAuthorizationRunResponse:
    run = await create_full_run(db, config_id, context, actor)
    await _to_thread(lambda: _run_full_sync(run.id, config_id, dict(context or {})))
    await db.commit()
    refreshed = await db.get(ApprovalAuthorizationRun, run.id)
    assert refreshed is not None
    return ApprovalAuthorizationRunResponse.model_validate(refreshed)


def _test_step_sync(run_id: uuid.UUID, config_id: uuid.UUID, step_key: str, context: dict[str, Any]) -> uuid.UUID:
    session = get_sync_session_factory()()
    runtime: _Runtime | None = None
    try:
        runtime = _Runtime(session, run_id, config_id)
        result = runtime.execute_step(step_key, context, context.get("apply_flow_id"))
        run = session.get(ApprovalAuthorizationRun, run_id)
        if run:
            run.state = "success"
            run.message = f"步骤测试完成：{STEP_NAMES.get(step_key, step_key)}"
            run.context = {"mode": "step_test", "step_key": step_key, "result": _redact(result)}
            run.finished_at = app_now()
            run.updated_at = run.finished_at
        session.commit()
        return runtime.last_log_id
    except Exception as exc:
        run = session.get(ApprovalAuthorizationRun, run_id)
        if run:
            run.state = "failed"
            run.message = str(exc)
            run.finished_at = app_now()
            run.updated_at = run.finished_at
            session.commit()
        if runtime and runtime.last_log_id:
            return runtime.last_log_id
        raise
    finally:
        session.close()


def _run_full_sync(run_id: uuid.UUID, config_id: uuid.UUID, context: dict[str, Any]) -> None:
    session = get_sync_session_factory()()
    try:
        runtime = _Runtime(session, run_id, config_id)
        auto_watch = str(context.get("mode") or "").strip() == "auto_watch" or _as_bool(context.get("auto_watch"))
        workflow = runtime.execute_step("login", context, None)
        context["workflow_token"] = workflow["workflow_token"]
        todo = runtime.execute_step("todo_list", context, None)
        apply_flow_ids = [str(item) for item in todo.get("apply_flow_ids") or []]
        discovered_count = len(apply_flow_ids)
        if auto_watch:
            max_items = _positive_int(
                context.get("max_apply_flow_ids") or runtime.config_dict.get("auto_watch_max_items_per_scan"),
                1,
                minimum=1,
                maximum=100,
            )
            apply_flow_ids = apply_flow_ids[:max_items]
        run = session.get(ApprovalAuthorizationRun, run_id)
        if run:
            run.total_count = len(apply_flow_ids)
            run.message = (
                f"自动监听发现 auditStatus=0/空 申请 {discovered_count} 个，本轮处理 {len(apply_flow_ids)} 个。"
                if auto_watch
                else f"待处理申请 {len(apply_flow_ids)} 个。"
            )
            run.updated_at = app_now()
            session.commit()
        success_count = 0
        failed_count = 0
        skipped_count = 0
        for apply_flow_id in apply_flow_ids:
            try:
                if auto_watch and _as_bool(runtime.config_dict.get("auto_watch_skip_status_updated")) and runtime.apply_flow_status_updated(apply_flow_id):
                    skipped_count += 1
                    runtime.log_event(
                        "auto_watch_skip",
                        "自动监听跳过",
                        "skipped",
                        f"applyFlowId={apply_flow_id} 已完成审批状态回写，本轮跳过。",
                        apply_flow_id=apply_flow_id,
                        extracted={"apply_flow_id": apply_flow_id, "reason": "audit_status_update_already_success"},
                    )
                elif auto_watch and runtime.apply_flow_import_succeeded(apply_flow_id):
                    runtime.set_current_apply_flow(apply_flow_id)
                    runtime.execute_step("audit_status_update", dict(context, apply_flow_id=apply_flow_id), apply_flow_id)
                    success_count += 1
                else:
                    _run_one_apply_flow(runtime, dict(context), apply_flow_id)
                    success_count += 1
            except Exception as exc:
                failed_count += 1
                runtime.log_event(
                    "apply_flow",
                    "申请处理失败",
                    "failed",
                    f"applyFlowId={apply_flow_id} 处理失败：{exc}",
                    apply_flow_id=apply_flow_id,
                    extracted={"apply_flow_id": apply_flow_id},
                    error=str(exc),
                )
            run = session.get(ApprovalAuthorizationRun, run_id)
            if run:
                run.success_count = success_count
                run.failed_count = failed_count
                run.skipped_count = skipped_count
                run.current_apply_flow_id = None
                run.updated_at = app_now()
                session.commit()
        run = session.get(ApprovalAuthorizationRun, run_id)
        if run:
            run.state = "success" if failed_count == 0 else ("failed" if success_count == 0 else "partial_failed")
            run.message = f"审批流自动授权完成：成功 {success_count} 个，失败 {failed_count} 个，跳过 {skipped_count} 个。"
            run.finished_at = app_now()
            run.updated_at = run.finished_at
            session.commit()
    except Exception as exc:
        run = session.get(ApprovalAuthorizationRun, run_id)
        if run:
            run.state = "failed"
            run.message = str(exc)
            run.finished_at = app_now()
            run.updated_at = run.finished_at
            session.commit()
        logger.exception("approval authorization run failed run_id=%s", run_id)
    finally:
        session.close()


def _run_one_apply_flow(runtime: "_Runtime", base_context: dict[str, Any], apply_flow_id: str) -> None:
    context = dict(base_context)
    context["apply_flow_id"] = apply_flow_id
    runtime.set_current_apply_flow(apply_flow_id)
    detail = runtime.execute_step("detail", context, apply_flow_id)
    context.update(detail)
    mapping = runtime.execute_step("department_mapping", context, apply_flow_id)
    context.update(mapping)
    data_list = runtime.execute_step("data_list", context, apply_flow_id)
    context.update(data_list)
    schema = runtime.execute_step("table_schema_lookup", context, apply_flow_id)
    context.update(schema)
    runtime.execute_step("auth_info_insert", context, apply_flow_id)
    grant = runtime.execute_step("internal_grant", context, apply_flow_id)
    context.update(grant)
    youdata = runtime.execute_step("youdata_token", context, apply_flow_id)
    context.update(youdata)
    api_add = runtime.execute_step("api_add", context, apply_flow_id)
    context.update(api_add)
    runtime.execute_step("import_permissions", context, apply_flow_id)
    if _as_bool(runtime.config_dict.get("update_audit_status_after_success")):
        runtime.execute_step("audit_status_update", context, apply_flow_id)


class _Runtime:
    def __init__(self, session: Session, run_id: uuid.UUID, config_id: uuid.UUID):
        self.session = session
        self.run_id = run_id
        self.config = session.get(ApprovalAuthorizationConfig, config_id)
        if not self.config:
            raise ValueError("审批流自动授权配置不存在。")
        self.profile = session.get(DatabaseConnectionProfile, self.config.doris_connection_id)
        if not self.profile or self.profile.engine != "doris":
            raise ValueError("请选择 Doris 类型的数据连接。")
        self.config_dict = _normalized_config(self.config.config or {})
        self.last_log_id: uuid.UUID | None = None
        self._last_http_request: dict[str, Any] | None = None
        self._last_http_response: dict[str, Any] | None = None
        self._last_sql_text: str | None = None
        self._last_sql_params: dict[str, Any] | None = None
        self._last_sql_result: dict[str, Any] | None = None

    def set_current_apply_flow(self, apply_flow_id: str | None) -> None:
        run = self.session.get(ApprovalAuthorizationRun, self.run_id)
        if run:
            run.current_apply_flow_id = apply_flow_id
            run.updated_at = app_now()
            self.session.commit()

    def execute_step(self, step_key: str, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        handlers = {
            "login": self._step_login,
            "todo_list": self._step_todo_list,
            "detail": self._step_detail,
            "department_mapping": self._step_department_mapping,
            "data_list": self._step_data_list,
            "table_schema_lookup": self._step_table_schema_lookup,
            "auth_info_insert": self._step_auth_info_insert,
            "internal_grant": self._step_internal_grant,
            "youdata_token": self._step_youdata_token,
            "api_add": self._step_api_add,
            "import_permissions": self._step_import_permissions,
            "audit_status_update": self._step_audit_status_update,
        }
        if step_key not in handlers:
            raise ValueError(f"未知步骤：{step_key}")
        self._reset_step_trace()
        try:
            return handlers[step_key](context, apply_flow_id)
        except Exception as exc:
            self.log_event(
                step_key,
                STEP_NAMES.get(step_key, step_key),
                "failed",
                f"{STEP_NAMES.get(step_key, step_key)}失败：{exc}",
                apply_flow_id=apply_flow_id or context.get("apply_flow_id"),
                request=self._last_http_request,
                response=self._last_http_response,
                extracted={"context": _redact(context)},
                sql_text=self._last_sql_text,
                sql_params=self._last_sql_params,
                sql_result=self._last_sql_result,
                error=str(exc),
            )
            raise

    def _reset_step_trace(self) -> None:
        self._last_http_request = None
        self._last_http_response = None
        self._last_sql_text = None
        self._last_sql_params = None
        self._last_sql_result = None

    def log_event(
        self,
        step_key: str,
        step_name: str,
        status: str,
        message: str,
        *,
        apply_flow_id: str | None = None,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        extracted: dict[str, Any] | None = None,
        sql_text: str | None = None,
        sql_params: dict[str, Any] | None = None,
        sql_result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> uuid.UUID:
        now = app_now()
        allow_sensitive = self._diagnostic_logging_enabled()
        request_payload = _diagnostic_redact(request) if allow_sensitive else _redact(request)
        response_payload = _diagnostic_redact(response) if allow_sensitive else _redact(response)
        extracted_payload = _diagnostic_redact(extracted) if allow_sensitive else _redact(extracted)
        sql_params_payload = _diagnostic_redact(sql_params) if allow_sensitive else _redact(sql_params)
        sql_result_payload = _diagnostic_redact(sql_result) if allow_sensitive else _redact(sql_result)
        row = ApprovalAuthorizationStepLog(
            run_id=self.run_id,
            config_id=self.config.id,
            apply_flow_id=apply_flow_id,
            step_key=step_key,
            step_name=step_name,
            status=status,
            message=message,
            request_data=request_payload,
            response_data=response_payload,
            extracted_data=extracted_payload,
            sql_text=sql_text,
            sql_params=sql_params_payload,
            sql_result=sql_result_payload,
            error_message=error,
            started_at=now,
            finished_at=now,
        )
        self.session.add(row)
        self.session.commit()
        self.last_log_id = row.id
        logger.info(
            "approval_auth step=%s status=%s applyFlowId=%s message=%s request=%s response=%s extracted=%s sql=%s sql_params=%s sql_result=%s",
            step_key,
            status,
            apply_flow_id or "-",
            message,
            _diagnostic_json(request_payload, allow_sensitive),
            _diagnostic_json(response_payload, allow_sensitive),
            _diagnostic_json(extracted_payload, allow_sensitive),
            _compact_sql(sql_text),
            _diagnostic_json(sql_params_payload, allow_sensitive),
            _diagnostic_json(sql_result_payload, allow_sensitive),
        )
        return row.id

    def _step_login(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        body = {
            "username": self.config.workflow_username,
            "password": _decrypt(self.config.workflow_password_enc),
        }
        url = self._workflow_url("login_path")
        response = self._post_json(url, body)
        token = _json_path(response, self.config_dict["workflow_token_path"])
        if not token:
            raise ValueError("审批系统登录成功响应中未读取到 token。")
        extracted = {"workflow_token": str(token), "token_path": self.config_dict["workflow_token_path"]}
        self.log_event("login", STEP_NAMES["login"], "success", "审批系统登录成功。", request={"url": url, "body": body}, response=response, extracted=extracted)
        return {"workflow_token": str(token)}

    def _step_todo_list(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        token = self._workflow_token(context)
        body = {
            "page": int(context.get("todo_page") or self.config_dict["todo_page"]),
            "rows": int(context.get("todo_rows") or self.config_dict["todo_rows"]),
        }
        url = self._workflow_url("todo_list_path")
        response = self._post_json(url, body, headers=self._workflow_headers(token))
        rows = _json_path(response, self.config_dict["todo_items_path"]) or []
        apply_ids = [
            str(item.get("id"))
            for item in rows
            if isinstance(item, dict)
            and item.get("id")
            and _todo_status_group(item.get("auditStatus")) in {"zero", "empty"}
        ]
        zero_count = sum(1 for item in rows if isinstance(item, dict) and _todo_status_group(item.get("auditStatus")) == "zero")
        empty_count = sum(1 for item in rows if isinstance(item, dict) and _todo_status_group(item.get("auditStatus")) == "empty")
        extracted = {
            "total_rows": len(rows),
            "audit_status_ready_count": len(apply_ids),
            "audit_status_zero_count": zero_count,
            "audit_status_empty_count": empty_count,
            "apply_flow_ids": apply_ids,
        }
        self.log_event("todo_list", STEP_NAMES["todo_list"], "success", "待办申请列表读取完成。", apply_flow_id=apply_flow_id, request={"url": url, "headers": self._workflow_headers(token), "body": body}, response=response, extracted=extracted)
        return {"apply_flow_ids": apply_ids, "todo_rows": rows}

    def _step_detail(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        flow_id = str(context.get("apply_flow_id") or apply_flow_id or "").strip()
        if not flow_id:
            raise ValueError("请提供 apply_flow_id。")
        token = self._workflow_token(context)
        body = {"id": flow_id}
        url = self._workflow_url("detail_path")
        response = self._post_json(url, body, headers=self._workflow_headers(token))
        detail = _json_path(response, self.config_dict["detail_data_path"]) or response
        department_raw = str(_json_path(detail, "createUserDepartment") or "")
        department = _extract_department_name(department_raw)
        query_users = _json_path(detail, "queryUserList") or []
        tels = [str(item.get("tel")).strip() for item in query_users if isinstance(item, dict) and item.get("tel")]
        query_end_time = str(_json_path(detail, "queryEndTime") or "").strip()
        creator_name = str(_json_path(detail, "createUserName") or "").strip()
        creator_mobile = str(_json_path(detail, "createUserMobile") or "").strip()
        generated_username = _generated_username(creator_name, creator_mobile, str(context.get("date_suffix") or self.config_dict["date_suffix"]))
        extracted = {
            "apply_flow_id": flow_id,
            "department_raw": department_raw,
            "department": department,
            "unique_ids": tels,
            "query_end_time": query_end_time,
            "expire_at": _expire_at(query_end_time),
            "create_user_name": creator_name,
            "create_user_mobile_last4": creator_mobile[-4:] if creator_mobile else "",
            "generated_username": generated_username,
        }
        self.log_event("detail", STEP_NAMES["detail"], "success", "申请详情读取完成。", apply_flow_id=flow_id, request={"url": url, "headers": self._workflow_headers(token), "body": body}, response=response, extracted=extracted)
        return {"detail": detail, **extracted}

    def _step_department_mapping(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        department = str(context.get("department") or "").strip()
        if not department:
            raise ValueError("缺少部门名称，无法查询单位与数据库映射表。")
        sql = (
            f"SELECT {_q(self.config_dict['mapping_database_column'])} AS database_name "
            f"FROM {_q(self.config_dict['mapping_database'])}.{_q(self.config_dict['mapping_table'])} "
            f"WHERE {_q(self.config_dict['mapping_department_column'])} = %s"
        )
        rows = self._query(sql, (department,))
        if len(rows) != 1:
            raise ValueError(f"部门 {department} 映射结果不是 1 条，实际 {len(rows)} 条。")
        database_name = str(rows[0].get("database_name") or "").strip()
        extracted = {"department": department, "database_name": database_name, "row_count": len(rows)}
        self.log_event("department_mapping", STEP_NAMES["department_mapping"], "success", "部门数据库映射查询完成。", apply_flow_id=apply_flow_id, extracted=extracted, sql_text=sql, sql_params={"department": department}, sql_result={"rows": rows})
        return extracted

    def _step_data_list(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        flow_id = str(context.get("apply_flow_id") or apply_flow_id or "").strip()
        token = self._workflow_token(context)
        body = {
            "page": int(context.get("data_list_page") or 1),
            "rows": int(context.get("data_list_rows") or self.config_dict["data_list_rows"]),
            "applyFlowId": flow_id,
        }
        url = self._workflow_url("data_list_path")
        response = self._post_json(url, body, headers=self._workflow_headers(token))
        rows = _json_path(response, self.config_dict["data_list_result_path"]) or []
        items = [
            {"datatitle": str(item.get("dataTitle") or item.get("datatitle") or "").strip(), "dataLevel": str(item.get("dataLevel") or item.get("datalevel") or "").strip()}
            for item in rows
            if isinstance(item, dict)
        ]
        items = [item for item in items if item["datatitle"]]
        extracted = {"apply_flow_id": flow_id, "item_count": len(items), "items": items}
        self.log_event("data_list", STEP_NAMES["data_list"], "success", "授权数据列表读取完成。", apply_flow_id=flow_id, request={"url": url, "headers": self._workflow_headers(token), "body": body}, response=response, extracted=extracted)
        return {"data_items": items}

    def _step_table_schema_lookup(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        items = context.get("data_items") or []
        titles = list(dict.fromkeys(str(item.get("datatitle") or "").strip() for item in items if item.get("datatitle")))
        if not titles:
            raise ValueError("没有可查询 schema 的 datatitle。")
        placeholders = ",".join(["%s"] * len(titles))
        schema_like = "DWD_%"
        sql = f"SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS table_name FROM information_schema.tables WHERE TABLE_SCHEMA LIKE %s AND TABLE_NAME IN ({placeholders})"
        rows = self._query(sql, (schema_like, *titles))
        by_title: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_title.setdefault(str(row.get("table_name")), []).append(row)
        records = []
        for item in items:
            title = str(item["datatitle"])
            matches = by_title.get(title) or []
            if len(matches) != 1:
                raise ValueError(f"表 {title} 的 schema 匹配结果不是 1 条，实际 {len(matches)} 条。")
            records.append({**item, "schema_name": str(matches[0].get("schema_name"))})
        extracted = {"record_count": len(records), "records": records}
        self.log_event("table_schema_lookup", STEP_NAMES["table_schema_lookup"], "success", "表 schema 查询完成。", apply_flow_id=apply_flow_id, extracted=extracted, sql_text=sql, sql_params={"schema_like": schema_like, "titles": titles}, sql_result={"rows": rows})
        return {"grant_records": records}

    def _step_auth_info_insert(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        flow_id = str(context.get("apply_flow_id") or apply_flow_id or "").strip()
        records = context.get("grant_records") or []
        if not records:
            raise ValueError("没有可写入授权信息表的记录。")
        sql = (
            f"INSERT INTO {_q(self.config_dict['auth_info_database'])}.{_q(self.config_dict['auth_info_table'])} "
            f"({_q('applyFlowId')}, {_q('datatitle')}, {_q('dataLevel')}, {_q('schema_name')}) VALUES (%s, %s, %s, %s)"
        )
        params = [(flow_id, row["datatitle"], row.get("dataLevel") or "", row["schema_name"]) for row in records]
        affected = self._execute_many(sql, params)
        extracted = {"insert_count": affected, "records": records}
        self.log_event("auth_info_insert", STEP_NAMES["auth_info_insert"], "success", "授权信息表写入完成。", apply_flow_id=flow_id, extracted=extracted, sql_text=sql, sql_params={"rows": params}, sql_result={"affected": affected})
        return extracted

    def _step_internal_grant(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        username = str(context.get("generated_username") or "").strip()
        records = context.get("grant_records") or []
        if not username:
            raise ValueError("缺少生成的授权用户。")
        password = _decrypt(self.config.default_doris_password_enc)
        database_name = str(context.get("database_name") or "").strip()
        sql_results = []
        with _doris_conn(self.profile, None) as conn:
            with conn.cursor() as cur:
                _ensure_doris_user(cur, username, password)
                if database_name:
                    _grant_department_database(cur, database_name, username)
        for record in records:
            _grant_source_table(self.profile, record["schema_name"], record["datatitle"], username)
            sql_results.append({"schema_name": record["schema_name"], "table": record["datatitle"], "user": username, "state": "granted"})
        extracted = {"username": username, "password": password, "database_name": database_name, "grant_count": len(sql_results), "grants": sql_results}
        self.log_event("internal_grant", STEP_NAMES["internal_grant"], "success", "内部 Doris 用户、基础库和表授权完成。", apply_flow_id=apply_flow_id, extracted=extracted, sql_text="CREATE USER IF NOT EXISTS ...; GRANT SELECT_PRIV ON base database ...; GRANT SELECT_PRIV ...", sql_result={"rows": sql_results})
        return {"doris_username": username, "doris_password": password}

    def _step_youdata_token(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        body = {
            "tokenType": self.config_dict["youdata_token_type"],
            "email": self.config.youdata_email,
            "password": _decrypt(self.config.youdata_password_enc),
        }
        url = self._youdata_url("youdata_token_path")
        response = self._post_json(url, body)
        token = _json_path(response, self.config_dict["youdata_token_result_path"])
        if not token:
            raise ValueError("有数 genToken 响应中未读取到 token。")
        extracted = {"youdata_token": str(token), "token_path": self.config_dict["youdata_token_result_path"]}
        self.log_event("youdata_token", STEP_NAMES["youdata_token"], "success", "有数 token 获取成功。", apply_flow_id=apply_flow_id, request={"url": url, "body": body}, response=response, extracted=extracted)
        return {"youdata_token": str(token)}

    def _step_api_add(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        token = self._youdata_token(context)
        username = str(context.get("doris_username") or context.get("generated_username") or "").strip()
        password = str(context.get("doris_password") or _decrypt(self.config.default_doris_password_enc))
        database_name = str(context.get("database_name") or "").strip()
        if not username or not database_name:
            raise ValueError("apiAdd 缺少用户名或默认库。")
        body = dict(self.config_dict["api_add_defaults"])
        if "paths" not in body and "path" in body:
            body["paths"] = body["path"]
        body.pop("path", None)
        server = str(self.profile.host or "").strip()
        if not server:
            raise ValueError("当前 Doris 数据连接缺少 host，无法提交 apiAdd server。")
        body.update(
            {
                "token": token,
                "name": str(context.get("api_add_name") or f"{database_name}_{username}"),
                "userName": username,
                "password": password,
                "defaultSchemaName": database_name,
                "server": server,
            }
        )
        url = self._youdata_url("api_add_path")
        response = self._post_json(url, body)
        resource_id = _json_path(response, self.config_dict["api_add_id_path"])
        if resource_id is None and self.config_dict["api_add_id_path"] != "result":
            resource_id = _json_path(response, "result")
        if resource_id is None:
            raise ValueError("apiAdd 响应中未读取到资源 id。")
        extracted = {
            "resource_id": resource_id,
            "database_name": database_name,
            "username": username,
            "server": server,
            "paths": body.get("paths"),
            "api_add_name": str(body.get("name") or "").strip(),
        }
        self.log_event("api_add", STEP_NAMES["api_add"], "success", "有数数据连接创建成功。", apply_flow_id=apply_flow_id, request={"url": url, "body": body}, response=response, extracted=extracted)
        return {"resource_id": resource_id, "api_add_name": str(body.get("name") or "").strip()}

    def _step_import_permissions(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        token = self._youdata_token(context)
        unique_ids = list(dict.fromkeys(str(item).strip() for item in (context.get("unique_ids") or []) if str(item).strip()))
        expire_at = _expire_at(str(context.get("query_end_time") or ""))
        resource_id = context.get("resource_id")
        if not unique_ids or resource_id is None:
            raise ValueError("importDataPermissions 缺少 uniqueIds 或 resourceId。")
        body = dict(self.config_dict["import_permissions_defaults"])
        role_name = str(context.get("api_add_name") or context.get("role_name") or "").strip()
        if not role_name:
            database_name = str(context.get("database_name") or "").strip()
            username = str(context.get("doris_username") or context.get("generated_username") or "").strip()
            if database_name and username:
                role_name = f"{database_name}_{username}"
        body.update(
            {
                "token": token,
                "roleName": role_name,
                "uniqueIds": unique_ids,
                "userExpireMap": {item: expire_at for item in unique_ids},
                "resourcePermissions": [
                    {
                        "resourceType": "DATA_CONNECTION",
                        "resourceId": resource_id,
                        "permissions": self.config_dict["youdata_permissions"],
                        "isFolder": 0,
                    }
                ],
            }
        )
        url = self._youdata_url("import_permissions_path")
        response = self._post_json(url, body)
        role_id = _json_path(response, self.config_dict["import_permissions_role_id_path"])
        extracted = {"unique_ids": unique_ids, "expire_at": expire_at, "resource_id": resource_id, "role_id": role_id, "role_name": role_name}
        self.log_event("import_permissions", STEP_NAMES["import_permissions"], "success", "有数人员权限导入完成。", apply_flow_id=apply_flow_id, request={"url": url, "body": body}, response=response, extracted=extracted)
        return {"role_id": role_id}

    def _step_audit_status_update(self, context: dict[str, Any], apply_flow_id: str | None) -> dict[str, Any]:
        flow_id = str(context.get("apply_flow_id") or apply_flow_id or "").strip()
        if not flow_id:
            raise ValueError("回写审批状态缺少 apply_flow_id。")
        token = self._workflow_token(context)
        body = dict(self.config_dict.get("audit_status_update_body_defaults") or {})
        body["id"] = flow_id
        url = self._workflow_url("audit_status_update_path")
        response = self._post_json(url, body, headers=self._workflow_headers(token))
        extracted = {"apply_flow_id": flow_id, "updated": True, "body": body}
        self.log_event(
            "audit_status_update",
            STEP_NAMES["audit_status_update"],
            "success",
            "审批状态回写完成。",
            apply_flow_id=flow_id,
            request={"url": url, "headers": self._workflow_headers(token), "body": body},
            response=response,
            extracted=extracted,
        )
        return {"audit_status_updated": True}

    def apply_flow_status_updated(self, apply_flow_id: str) -> bool:
        existing = (
            self.session.execute(
                select(ApprovalAuthorizationStepLog.id)
                .where(ApprovalAuthorizationStepLog.config_id == self.config.id)
                .where(ApprovalAuthorizationStepLog.apply_flow_id == str(apply_flow_id))
                .where(ApprovalAuthorizationStepLog.step_key == "audit_status_update")
                .where(ApprovalAuthorizationStepLog.status == "success")
                .limit(1)
            )
            .scalars()
            .first()
        )
        return existing is not None

    def apply_flow_import_succeeded(self, apply_flow_id: str) -> bool:
        existing = (
            self.session.execute(
                select(ApprovalAuthorizationStepLog.id)
                .where(ApprovalAuthorizationStepLog.config_id == self.config.id)
                .where(ApprovalAuthorizationStepLog.apply_flow_id == str(apply_flow_id))
                .where(ApprovalAuthorizationStepLog.step_key == "import_permissions")
                .where(ApprovalAuthorizationStepLog.status == "success")
                .limit(1)
            )
            .scalars()
            .first()
        )
        return existing is not None

    def _workflow_token(self, context: dict[str, Any]) -> str:
        token = str(context.get("workflow_token") or "").strip()
        if token:
            return token
        result = self._step_login(context, context.get("apply_flow_id"))
        context["workflow_token"] = result["workflow_token"]
        return result["workflow_token"]

    def _youdata_token(self, context: dict[str, Any]) -> str:
        token = str(context.get("youdata_token") or "").strip()
        if token:
            return token
        result = self._step_youdata_token(context, context.get("apply_flow_id"))
        context["youdata_token"] = result["youdata_token"]
        return result["youdata_token"]

    def _workflow_headers(self, token: str) -> dict[str, str]:
        header = str(self.config_dict["workflow_token_header"])
        prefix = str(self.config_dict["workflow_token_prefix"])
        return {header: f"{prefix}{token}"}

    def _workflow_url(self, path_key: str) -> str:
        return self.config.workflow_base_url.rstrip("/") + "/" + str(self.config_dict[path_key]).lstrip("/")

    def _youdata_url(self, path_key: str) -> str:
        return self.config.youdata_base_url.rstrip("/") + "/" + str(self.config_dict[path_key]).lstrip("/")

    def _post_json(self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        timeout = float(self.config_dict["timeout_seconds"])
        self._last_http_request = {"method": "POST", "url": url, "headers": headers or {}, "body": body}
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=body, headers=headers or {})
        try:
            payload = response.json()
        except Exception as exc:
            self._last_http_response = {"status_code": response.status_code, "text": response.text[:2000]}
            raise ValueError(f"接口 {url} 返回非 JSON：HTTP {response.status_code} {response.text[:500]}") from exc
        self._last_http_response = {"status_code": response.status_code, "body": payload}
        if response.status_code >= 400:
            raise ValueError(f"接口 {url} 返回 HTTP {response.status_code}：{payload}")
        code = payload.get("code") if isinstance(payload, dict) else None
        success = payload.get("success") if isinstance(payload, dict) else None
        if code not in (None, 1, 200, "1", "200") and success is not True:
            raise ValueError(f"接口 {url} 业务返回失败：{payload}")
        return payload

    def _diagnostic_logging_enabled(self) -> bool:
        value = self.config_dict.get("debug_log_sensitive_payloads")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        self._last_sql_text = sql
        self._last_sql_params = {"params": list(params)}
        with _doris_conn(self.profile, None) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql, params)
                    rows = list(cur.fetchall() or [])
                    self._last_sql_result = {"rows": rows, "row_count": len(rows)}
                    return rows
                except Exception as exc:
                    self._last_sql_result = {"error": str(exc)}
                    raise

    def _execute_many(self, sql: str, params: list[tuple[Any, ...]]) -> int:
        self._last_sql_text = sql
        self._last_sql_params = {"rows": params}
        with _doris_conn(self.profile, None) as conn:
            with conn.cursor() as cur:
                try:
                    cur.executemany(sql, params)
                    affected = int(cur.rowcount or 0)
                    self._last_sql_result = {"affected": affected}
                    return affected
                except Exception as exc:
                    self._last_sql_result = {"error": str(exc)}
                    raise


def _config_response(row: ApprovalAuthorizationConfig) -> ApprovalAuthorizationConfigResponse:
    return ApprovalAuthorizationConfigResponse(
        id=row.id,
        name=row.name,
        status=row.status,
        doris_connection_id=row.doris_connection_id,
        workflow_base_url=row.workflow_base_url,
        workflow_username=row.workflow_username,
        has_workflow_password=bool(row.workflow_password_enc),
        youdata_base_url=row.youdata_base_url,
        youdata_email=row.youdata_email,
        has_youdata_password=bool(row.youdata_password_enc),
        has_default_doris_password=bool(row.default_doris_password_enc),
        config=_normalized_config(row.config or {}),
        created_by_username=row.created_by_username,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _normalized_config(value: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(value or {})
    defaults = {
        "login_path": "/api/market/login",
        "todo_list_path": "/api/market/dataModelApplyFlow/getMyTodoList",
        "detail_path": "/api/market/dataModelApplyFlow/getDetail",
        "data_list_path": "/api/market/dataModelApplyData/getList",
        "workflow_token_path": "data.token",
        "workflow_token_header": "token",
        "workflow_token_prefix": "",
        "todo_items_path": "data.list",
        "audit_status_update_path": "/api/market/dataModelApplyFlow/auditStatus",
        "audit_status_update_body_defaults": {},
        "detail_data_path": "data",
        "data_list_result_path": "data.list",
        "todo_page": 1,
        "todo_rows": 1000,
        "data_list_rows": 9999,
        "mapping_database": "TESTS",
        "mapping_table": "单位与数据库映射表",
        "mapping_department_column": "部门",
        "mapping_database_column": "数据库",
        "auth_info_database": "TESTS",
        "auth_info_table": "授权信息表",
        "date_suffix": datetime.now().strftime("%m%d"),
        "youdata_token_path": "/api/dash/util/genToken",
        "youdata_token_type": "userPassword",
        "youdata_token_result_path": "result",
        "api_add_path": "/api/dash/dataConnection/apiAdd",
        "api_add_id_path": "result.id",
        "import_permissions_path": "/api/dash/role/importDataPermissions",
        "import_permissions_role_id_path": "result",
        "timeout_seconds": 60,
        "youdata_permissions": [
            "view",
            "addModel",
            "customSql",
            "sqlFetch",
            "sqlFetchCopyData",
            "sqlFetchExport",
            "sqlFetchShare",
            "updateData",
            "relationship",
        ],
        "api_add_defaults": {
            "projectId": 6,
            "type": 124,
            "paths": ["2026年7月培训项目_数据连接"],
            "server": "",
            "port": "9030",
            "skipTest": "false",
            "parameters": {"authType": "ldap", "dorisCatalog": "internal"},
            "nullSafeEqual": False,
            "driver": "mysql-connector-5.1.49",
        },
        "import_permissions_defaults": {
            "projectId": 6,
            "roleName": "",
            "path": ["2026年7月培训"],
            "type": 0,
            "importResourceTypes": ["DATA_CONNECTION"],
        },
        "debug_log_sensitive_payloads": False,
        "auto_watch_enabled": False,
        "auto_watch_interval_minutes": 5,
        "auto_watch_max_items_per_scan": 1,
        "auto_watch_skip_status_updated": True,
        "update_audit_status_after_success": True,
    }
    for key, default in defaults.items():
        cfg.setdefault(key, default)
    return cfg


def _has_running_config_run(session: Session, config_id: uuid.UUID) -> bool:
    existing = (
        session.execute(
            select(ApprovalAuthorizationRun.id)
            .where(ApprovalAuthorizationRun.config_id == config_id)
            .where(ApprovalAuthorizationRun.state.in_(list(_RUNNING_STATES)))
            .limit(1)
        )
        .scalars()
        .first()
    )
    return existing is not None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _todo_status_group(value: Any) -> str:
    if value is None:
        return "empty"
    text = str(value).strip()
    if not text:
        return "empty"
    if text == "0":
        return "zero"
    return "other"


def _positive_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _validate_payload_passwords(payload: ApprovalAuthorizationConfigPayload, *, require_all: bool) -> None:
    if require_all and not payload.workflow_password:
        raise ValueError("请填写审批系统密码。")
    if require_all and not payload.youdata_password:
        raise ValueError("请填写有数密码。")
    if require_all and not payload.default_doris_password:
        raise ValueError("请填写默认 Doris 用户密码。")


def _encrypt_secret(value: Any) -> str:
    text = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")
    return encrypt_secret(text, get_settings().credential_encryption_key)


def _decrypt(value: str) -> str:
    return decrypt_secret(value or "", get_settings().credential_encryption_key)


def _actor_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(str(actor.user_id))
    except ValueError:
        return None


async def _to_thread(func):
    import asyncio

    return await asyncio.to_thread(func)


def _json_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def _generated_username(name: str, mobile: str, suffix: str) -> str:
    last4 = re.sub(r"\D", "", mobile)[-4:] if mobile else ""
    base = "_".join(part for part in (name.strip(), last4, suffix.strip()) if part)
    return base or f"approval_user_{suffix}"


def _extract_department_name(department_raw: str) -> str:
    text = str(department_raw or "").strip()
    if not text:
        return ""
    if text.startswith("重庆市审计局/"):
        return text.split("/", 1)[1].strip()
    if "/" in text:
        return text.split("/", 1)[0].strip()
    return text


def _expire_at(query_end_time: str) -> str:
    value = str(query_end_time or "").strip()
    if not value:
        return ""
    return value.split()[0] + " 23:59:59"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(word in low for word in ("password", "token", "secret", "pwd", "authorization", "cookie")):
                result[key] = _mask(str(item))
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _diagnostic_redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(word in low for word in ("password", "secret", "pwd")):
                result[key] = _mask(str(item))
            else:
                result[key] = _diagnostic_redact(item)
        return result
    if isinstance(value, list):
        return [_diagnostic_redact(item) for item in value]
    return value


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def _compact_json(value: Any) -> str:
    if value is None:
        return "-"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:1000] + ("..." if len(text) > 1000 else "")


def _diagnostic_json(value: Any, full: bool) -> str:
    if value is None:
        return "-"
    if full:
        return json.dumps(value, ensure_ascii=False, default=str)
    return _compact_json(value)


def _compact_sql(value: str | None) -> str:
    if not value:
        return "-"
    text = " ".join(value.split())
    return text[:500] + ("..." if len(text) > 500 else "")
