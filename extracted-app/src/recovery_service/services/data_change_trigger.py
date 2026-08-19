from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Callable

import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from recovery_service.common.logging import get_logger
from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    DataPlatformChangeProbe,
    DataPlatformChangeTriggerState,
    DataPlatformNode,
    DataPlatformWorkflowRun,
    DataPlatformWorkflowVersion,
    DatabaseConnectionProfile,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.settings import get_settings

_READ_ONLY_SQL_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
logger = get_logger(__name__)


def synchronize_change_triggers(session: Session, version: DataPlatformWorkflowVersion) -> None:
    nodes = list((version.release_snapshot or {}).get("nodes") or version.nodes or [])
    trigger_nodes = [
        item
        for item in nodes
        if item.get("node_type") == "change_trigger" and not _is_standalone_trigger_reference(item)
    ]
    existing = {
        row.node_key: row
        for row in session.execute(
            select(DataPlatformChangeTriggerState).where(DataPlatformChangeTriggerState.version_id == version.id)
        ).scalars()
    }
    now = app_now()
    for node in trigger_nodes:
        key = str(node.get("key") or "").strip()
        if not key:
            continue
        config = _normalize_trigger_config(dict(node.get("config") or {}), nodes)
        row = existing.pop(key, None)
        if row is None:
            row = DataPlatformChangeTriggerState(
                id=uuid.uuid4(),
                workflow_id=version.workflow_id,
                version_id=version.id,
                node_key=key,
                node_name=str(node.get("name") or key)[:128],
                enabled=True,
                state="active",
                config=config,
                next_probe_at=now,
                message="等待首次数据变化探测。",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.node_name = str(node.get("name") or key)[:128]
            row.config = config
            if row.state == "retired":
                row.enabled = True
                row.state = "active"
            row.next_probe_at = (row.next_probe_at or now) if row.enabled else None
            row.updated_at = now
    for row in existing.values():
        row.enabled = False
        row.state = "retired"
        row.next_probe_at = None
        row.message = "触发节点已从当前发布内容移除。"
        row.updated_at = now


def retire_change_triggers(session: Session, version_id: uuid.UUID) -> None:
    rows = session.execute(
        select(DataPlatformChangeTriggerState).where(DataPlatformChangeTriggerState.version_id == version_id)
    ).scalars().all()
    for row in rows:
        row.enabled = False
        row.state = "retired"
        row.next_probe_at = None
        row.updated_at = app_now()


def _is_standalone_trigger_reference(node: dict[str, Any]) -> bool:
    config = dict(node.get("config") or {})
    if config.get("standalone_deployment_monitor"):
        return False
    snapshot_config = dict((config.get("task_definition_snapshot") or {}).get("config") or {})
    return bool(
        config.get("standalone_trigger")
        or config.get("published_workflow_version_id")
        or snapshot_config.get("standalone_trigger")
        or snapshot_config.get("published_workflow_version_id")
    )


def _standalone_task_missing(session: Session, row: DataPlatformChangeTriggerState) -> bool:
    task_id = (row.config or {}).get("trigger_task_id")
    if not task_id:
        return False
    try:
        task = session.get(DataPlatformNode, uuid.UUID(str(task_id)))
    except (TypeError, ValueError):
        return True
    return not task or task.status != "active" or task.node_type != "change_trigger"


def run_due_change_triggers(run_workflow: Callable[..., Any]) -> None:
    now = app_now()
    session = get_sync_session_factory()()
    due_ids: list[uuid.UUID] = []
    try:
        rows = session.execute(
            select(DataPlatformChangeTriggerState)
            .where(
                DataPlatformChangeTriggerState.enabled == True,  # noqa: E712
                DataPlatformChangeTriggerState.next_probe_at <= now,
            )
            .order_by(DataPlatformChangeTriggerState.next_probe_at)
            .limit(20)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        for row in rows:
            due_ids.append(row.id)
            interval = _interval_minutes(row.config)
            row.next_probe_at = now + timedelta(minutes=interval)
            row.updated_at = now
        session.commit()
    finally:
        session.close()
    for trigger_id in due_ids:
        try:
            _probe_and_maybe_run(trigger_id, run_workflow=run_workflow, manual=False)
        except Exception as exc:
            logger.error(
                "data change trigger probe failed",
                trigger_id=str(trigger_id),
                error=str(exc),
                exc_info=True,
            )
            _mark_probe_failure(trigger_id, exc)


def probe_change_trigger_now(trigger_id: uuid.UUID, run_workflow: Callable[..., Any]) -> dict[str, Any]:
    return _probe_and_maybe_run(trigger_id, run_workflow=run_workflow, manual=True)


def _mark_probe_failure(trigger_id: uuid.UUID, exc: Exception) -> None:
    session = get_sync_session_factory()()
    try:
        row = session.get(DataPlatformChangeTriggerState, trigger_id)
        if not row:
            return
        if not row.enabled or row.state == "retired" or _standalone_task_missing(session, row):
            row.enabled = False
            row.state = "retired"
            row.next_probe_at = None
            row.updated_at = app_now()
            session.commit()
            return
        retry_minutes = max(1, int((row.config or {}).get("retry_interval_minutes") or 5))
        row.state = "probe_failed"
        row.next_probe_at = app_now() + timedelta(minutes=retry_minutes)
        row.message = f"数据变化探测失败，{retry_minutes} 分钟后重试：{exc}"
        row.updated_at = app_now()
        session.add(
            DataPlatformChangeProbe(
                id=uuid.uuid4(),
                trigger_state_id=row.id,
                workflow_id=row.workflow_id,
                version_id=row.version_id,
                node_key=row.node_key,
                previous_value=row.applied_value,
                current_value=None,
                condition_results=[],
                matched=False,
                status="failed",
                message=str(exc),
                created_at=app_now(),
            )
        )
        session.commit()
    finally:
        session.close()


def list_change_triggers(*, version_id: uuid.UUID | None = None, limit: int = 200) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataPlatformChangeTriggerState).order_by(desc(DataPlatformChangeTriggerState.updated_at)).limit(limit)
        if version_id:
            stmt = stmt.where(DataPlatformChangeTriggerState.version_id == version_id)
        return [_trigger_payload(row) for row in session.execute(stmt).scalars().all()]
    finally:
        session.close()


def list_change_probes(*, trigger_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DataPlatformChangeProbe)
            .where(DataPlatformChangeProbe.trigger_state_id == trigger_id)
            .order_by(desc(DataPlatformChangeProbe.created_at), desc(DataPlatformChangeProbe.id))
            .limit(limit)
        ).scalars().all()
        return [_probe_payload(row) for row in rows]
    finally:
        session.close()


def update_change_trigger_state(trigger_id: uuid.UUID, *, enabled: bool) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = session.get(DataPlatformChangeTriggerState, trigger_id)
        if not row:
            raise KeyError("数据变化触发器不存在。")
        row.enabled = bool(enabled)
        row.state = "active" if enabled else "paused"
        row.next_probe_at = app_now() if enabled else None
        row.message = "触发器已恢复。" if enabled else "触发器已暂停。"
        row.updated_at = app_now()
        session.commit()
        session.refresh(row)
        return _trigger_payload(row)
    finally:
        session.close()


def rebuild_change_trigger_baseline(trigger_id: uuid.UUID, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise ValueError("重建基线需要明确确认。")
    session = get_sync_session_factory()()
    try:
        row = session.get(DataPlatformChangeTriggerState, trigger_id)
        if not row:
            raise KeyError("数据变化触发器不存在。")
        _reconcile_pending_run(session, row)
        if row.pending_run_id:
            raise ValueError("触发器仍有关联运行未结束，不能重建基线。")
        current, _ = _capture_values(session, row)
        row.observed_value = current
        row.applied_value = current
        row.pending_value = None
        row.pending_queue = []
        row.pending_run_id = None
        row.consecutive_matches = 0
        row.last_probe_at = app_now()
        row.next_probe_at = app_now() + timedelta(minutes=_interval_minutes(row.config))
        row.state = "active"
        row.message = "基线已按当前数据重新建立。"
        row.updated_at = app_now()
        session.commit()
        session.refresh(row)
        return _trigger_payload(row)
    finally:
        session.close()


def finalize_change_trigger_run(session: Session, run: DataPlatformWorkflowRun) -> None:
    context = dict(run.trigger_context or {})
    trigger_id = context.get("trigger_state_id")
    if run.trigger_type != "data_change" or not trigger_id:
        return
    row = session.get(DataPlatformChangeTriggerState, uuid.UUID(str(trigger_id)))
    if not row or str(row.pending_run_id or "") != str(run.id):
        return
    probe_id = context.get("probe_id")
    probe = session.get(DataPlatformChangeProbe, uuid.UUID(str(probe_id))) if probe_id else None
    if run.status == "succeeded":
        applied_value = context.get("pending_value")
        row.applied_value = applied_value
        if row.pending_value == applied_value:
            row.pending_value = None
        else:
            row.next_probe_at = app_now()
        row.pending_run_id = None
        if row.pending_value is None and row.pending_queue:
            queue = list(row.pending_queue or [])
            row.pending_value = queue.pop(0)
            row.pending_queue = queue
            row.next_probe_at = app_now()
        row.last_success_at = app_now()
        row.state = (
            "paused"
            if not row.enabled
            else ("active" if row.pending_value is None else "pending_retry")
        )
        if not row.enabled:
            row.next_probe_at = None
        row.message = (
            f"变化触发运行 {run.id} 成功，已推进应用水位。"
            if row.pending_value is None
            else f"变化触发运行 {run.id} 成功；运行期间发现了更新变化，将继续处理。"
        )
        if probe:
            probe.status = "applied"
            probe.message = row.message
    else:
        row.pending_run_id = None
        row.state = "pending_retry" if row.enabled else "paused"
        row.next_probe_at = (
            app_now() + timedelta(minutes=max(1, int(row.config.get("retry_interval_minutes") or 5)))
            if row.enabled
            else None
        )
        row.message = f"变化触发运行 {run.id} 未成功，保留 pending_value 等待重试。"
        if probe:
            probe.status = "failed"
            probe.message = row.message
    row.updated_at = app_now()


def _probe_and_maybe_run(trigger_id: uuid.UUID, *, run_workflow: Callable[..., Any], manual: bool) -> dict[str, Any]:
    session = get_sync_session_factory()()
    run_request: dict[str, Any] | None = None
    try:
        row = session.execute(
            select(DataPlatformChangeTriggerState)
            .where(DataPlatformChangeTriggerState.id == trigger_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not row:
            raise KeyError("数据变化触发器不存在。")
        if (not row.enabled or row.state == "retired" or _standalone_task_missing(session, row)) and not manual:
            row.enabled = False
            row.state = "retired"
            row.next_probe_at = None
            row.message = "触发器源任务已删除或停用，巡检状态已退役。"
            row.updated_at = app_now()
            session.commit()
            return _trigger_payload(row)
        if row.state == "dispatching":
            return _trigger_payload(row)
        _reconcile_pending_run(session, row)
        current, metric_details = _capture_values(session, row)
        previous = row.applied_value
        matched, results = _evaluate_conditions(row.config, previous, current)
        now = app_now()
        row.observed_value = current
        row.last_probe_at = now
        row.next_probe_at = now + timedelta(minutes=_interval_minutes(row.config))
        row.consecutive_matches = row.consecutive_matches + 1 if matched else 0
        probe = DataPlatformChangeProbe(
            id=uuid.uuid4(),
            trigger_state_id=row.id,
            workflow_id=row.workflow_id,
            version_id=row.version_id,
            node_key=row.node_key,
            previous_value=previous,
            current_value=current,
            condition_results=results + metric_details,
            matched=matched,
            status="observed",
            message="探测完成。",
            created_at=now,
        )
        session.add(probe)

        first_policy = str(row.config.get("first_run_policy") or "baseline_only")
        if row.applied_value is None:
            if first_policy in {"baseline_only", "manual_confirm"}:
                row.applied_value = current if first_policy == "baseline_only" else None
                row.state = "active" if first_policy == "baseline_only" else "waiting_baseline_confirm"
                row.message = "首次探测仅建立基线。" if first_policy == "baseline_only" else "首次探测等待人工确认基线。"
                probe.status = "baseline"
                session.commit()
                return _trigger_payload(row)
            matched = True
            row.consecutive_matches = max(row.consecutive_matches, int(row.config.get("consecutive_matches") or 1))

        if row.pending_value is not None and not row.pending_run_id:
            matched = True
            current = row.observed_value or row.pending_value
            row.consecutive_matches = max(row.consecutive_matches, int(row.config.get("consecutive_matches") or 1))

        threshold = max(1, int(row.config.get("consecutive_matches") or 1))
        if matched and row.consecutive_matches >= threshold:
            if row.pending_run_id:
                overlap_policy = str(row.config.get("overlap_policy") or "merge")
                if overlap_policy == "skip":
                    probe.status = "skipped_overlap"
                    row.message = "上一次运行未完成，本次变化按跳过策略记录但不排队。"
                elif overlap_policy == "queue":
                    queue = list(row.pending_queue or [])
                    if current not in queue:
                        queue.append(current)
                    row.pending_queue = queue[-100:]
                    probe.status = "queued_overlap"
                    row.message = f"上一次运行未完成，本次变化已排队；队列长度 {len(row.pending_queue)}。"
                else:
                    row.pending_value = current
                    probe.status = "merged"
                    row.message = "已合并到当前待处理变化，等待在途运行完成。"
            elif not _inside_execution_window(now, row.config):
                row.pending_value = current
                probe.status = "pending_window"
                row.state = "pending_window"
                row.message = "变化已确认，等待执行窗口。"
            elif _within_debounce(now, row):
                row.pending_value = current
                probe.status = "debounced"
                row.state = "pending_debounce"
                row.message = "变化已确认，等待最短触发间隔。"
            else:
                row.pending_value = current
                planned_run_id = uuid.uuid4()
                run_request = {
                    "version_id": row.version_id,
                    "run_id": planned_run_id,
                    "trigger_context": {
                        "trigger_state_id": str(row.id),
                        "probe_id": str(probe.id),
                        "node_key": row.node_key,
                        "pending_value": current,
                        "applied_value": row.applied_value or {},
                    },
                }
                probe.status = "triggering"
                probe.run_id = planned_run_id
                row.pending_run_id = planned_run_id
                row.last_trigger_at = now
                row.state = "dispatching"
                row.message = f"正在创建变化触发运行 {planned_run_id}。"
        else:
            row.message = "探测完成，条件未满足。"
        row.updated_at = now
        session.commit()
        if not run_request:
            session.refresh(row)
            return _trigger_payload(row)
    finally:
        session.close()

    assert run_request is not None
    try:
        run = run_workflow(
            run_request["version_id"],
            trigger_type="data_change",
            actor=None,
            trigger_context=run_request["trigger_context"],
            run_id=run_request["run_id"],
        )
    except Exception as exc:
        session = get_sync_session_factory()()
        try:
            row = session.get(DataPlatformChangeTriggerState, trigger_id)
            if row:
                row.pending_run_id = None
                row.state = "pending_retry"
                row.message = f"触发工作流失败，保留变化等待重试：{exc}"
                row.updated_at = app_now()
                session.commit()
                return _trigger_payload(row)
        finally:
            session.close()
        raise

    session = get_sync_session_factory()()
    try:
        row = session.get(DataPlatformChangeTriggerState, trigger_id)
        probe = session.get(DataPlatformChangeProbe, uuid.UUID(run_request["trigger_context"]["probe_id"]))
        persisted_run = session.get(DataPlatformWorkflowRun, run.run_id)
        run_is_active = bool(persisted_run and persisted_run.status in {"queued", "running"})
        owns_pending_run = bool(row and str(row.pending_run_id or "") == str(run.run_id))
        if owns_pending_run and run_is_active:
            row.state = "running"
            row.message = f"已触发工作流运行 {run.run_id}。"
            row.updated_at = app_now()
        if probe and probe.status == "triggering" and run_is_active:
            probe.run_id = run.run_id
            probe.status = "triggered"
            probe.message = f"已关联工作流运行 {run.run_id}。"
        session.commit()
        return _trigger_payload(row) if row else {"run_id": str(run.run_id)}
    finally:
        session.close()


def _reconcile_pending_run(session: Session, row: DataPlatformChangeTriggerState) -> None:
    if not row.pending_run_id:
        return
    run = session.get(DataPlatformWorkflowRun, row.pending_run_id)
    if run and run.status in {"queued", "running"}:
        return
    if run:
        finalize_change_trigger_run(session, run)
    else:
        row.pending_run_id = None
        row.state = "pending_retry"
        row.message = "关联运行不存在，保留 pending_value 等待重试。"


def _capture_values(session: Session, row: DataPlatformChangeTriggerState) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = row.config or {}
    connection_id = config.get("connection_id")
    if not connection_id:
        raise ValueError("变化触发器没有配置 Doris 连接。")
    profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(connection_id)))
    if not profile or profile.engine != "doris":
        raise ValueError("变化触发器的 Doris 连接不存在。")
    database = _ident(config.get("database"), "监控数据库")
    conditions = list(config.get("conditions") or [])
    if not conditions:
        raise ValueError("变化触发器至少需要一个监控条件。")
    values: dict[str, Any] = {}
    details: list[dict[str, Any]] = []
    with closing(_doris_conn(profile, database)) as db, db.cursor() as cur:
        for index, condition in enumerate(conditions):
            condition_id = str(condition.get("id") or f"condition_{index + 1}")
            metric_type = str(condition.get("metric_type") or "row_count")
            value = _read_metric(cur, database, condition, metric_type)
            values[condition_id] = value
            details.append({"condition_id": condition_id, "metric_type": metric_type, "value": value})
    return values, details


def _read_metric(cur, database: str, condition: dict[str, Any], metric_type: str) -> Any:
    table = _optional_ident(condition.get("table"), "监控表")
    column = _optional_ident(condition.get("column"), "监控字段")
    if metric_type == "table_exists":
        cur.execute(
            "SELECT COUNT(*) AS value FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (database, table),
        )
        return bool(int((cur.fetchone() or {}).get("value") or 0))
    if metric_type == "schema_signature":
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, ORDINAL_POSITION, COLUMN_KEY, IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database, table),
        )
        payload = [dict(item) for item in cur.fetchall()]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    if metric_type == "scalar_sql":
        sql = str(condition.get("sql") or "").strip().rstrip(";")
        if not _READ_ONLY_SQL_RE.match(sql) or ";" in sql:
            raise ValueError("标量 SQL 只允许单条 SELECT / WITH 查询。")
        cur.execute(sql)
        row = cur.fetchone() or {}
        return _jsonable(next(iter(row.values()), None))
    if not table:
        raise ValueError(f"监控指标 {metric_type} 必须配置表。")
    if metric_type == "row_count":
        sql = f"SELECT COUNT(*) AS value FROM {_q(database)}.{_q(table)}"
    elif metric_type in {"max", "min"}:
        if not column:
            raise ValueError(f"监控指标 {metric_type} 必须配置字段。")
        sql = f"SELECT {metric_type.upper()}({_q(column)}) AS value FROM {_q(database)}.{_q(table)}"
    else:
        raise ValueError(f"不支持的监控指标：{metric_type}")
    cur.execute(sql)
    return _jsonable((cur.fetchone() or {}).get("value"))


def _evaluate_conditions(config: dict[str, Any], previous: dict[str, Any] | None, current: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    logic = str(config.get("condition_logic") or "AND").upper()
    matches: list[bool] = []
    results: list[dict[str, Any]] = []
    previous = previous or {}
    for index, condition in enumerate(config.get("conditions") or []):
        condition_id = str(condition.get("id") or f"condition_{index + 1}")
        old = previous.get(condition_id)
        new = current.get(condition_id)
        operator = str(condition.get("operator") or "changed")
        threshold = condition.get("threshold")
        matched = _compare(old, new, operator, threshold)
        matches.append(matched)
        results.append(
            {
                "condition_id": condition_id,
                "operator": operator,
                "previous": old,
                "current": new,
                "threshold": threshold,
                "matched": matched,
            }
        )
    return (all(matches) if logic == "AND" else any(matches)), results


def _compare(old: Any, new: Any, operator: str, threshold: Any) -> bool:
    if operator == "changed":
        return old is not None and new != old
    if operator == "increased":
        return old is not None and _number(new) > _number(old)
    if operator == "increase_by":
        return old is not None and _number(new) - _number(old) >= _number(threshold)
    if operator == "increase_percent":
        if old is None or _number(old) == 0:
            return False
        return ((_number(new) - _number(old)) / abs(_number(old))) * 100 >= _number(threshold)
    if operator == "greater_than":
        return _number(new) > _number(threshold)
    if operator == "equals":
        return new == threshold
    if operator == "became_true":
        return bool(new) and not bool(old)
    raise ValueError(f"不支持的条件操作符：{operator}")


def _normalize_trigger_config(config: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(config)
    if result.get("source_type") == "component":
        source_key = str(result.get("source_node_key") or "")
        source = next((item for item in nodes if str(item.get("key")) == source_key), None)
        if not source:
            raise ValueError(f"变化触发器引用的组件不存在：{source_key}")
        source_config = dict(source.get("config") or {})
        if source.get("node_type") == "data_sync":
            if not result.get("connection_id"):
                result["connection_id"] = source_config.get("source_connection_id")
            if not result.get("database"):
                result["database"] = source_config.get("source_database")
            for condition in result.get("conditions") or []:
                if not condition.get("table"):
                    condition["table"] = source_config.get("source_table")
        else:
            snapshot = source_config.get("task_definition_snapshot") or source_config.get("task_revision_snapshot") or {}
            if not result.get("connection_id"):
                result["connection_id"] = snapshot.get("connection_id") or source_config.get("connection_id")
            if not result.get("database"):
                result["database"] = snapshot.get("database") or source_config.get("database")
            default_table = snapshot.get("table_name")
            if not default_table and snapshot.get("tables"):
                default_table = snapshot["tables"][0].get("table_name")
            for condition in result.get("conditions") or []:
                if not condition.get("table"):
                    condition["table"] = default_table
    result["probe_interval_minutes"] = _interval_minutes(result)
    return result


def _inside_execution_window(now: datetime, config: dict[str, Any]) -> bool:
    if not config.get("execution_window_enabled"):
        return True
    start = str(config.get("execution_window_start") or "00:00")
    end = str(config.get("execution_window_end") or "23:59")
    current = now.hour * 60 + now.minute
    start_minutes = _minutes(start)
    end_minutes = _minutes(end)
    return start_minutes <= current <= end_minutes if start_minutes <= end_minutes else current >= start_minutes or current <= end_minutes


def _within_debounce(now: datetime, row: DataPlatformChangeTriggerState) -> bool:
    minimum = max(0, int(row.config.get("minimum_trigger_interval_minutes") or 0))
    return bool(row.last_trigger_at and now < row.last_trigger_at + timedelta(minutes=minimum))


def _interval_minutes(config: dict[str, Any]) -> int:
    return max(1, min(1440, int(config.get("probe_interval_minutes") or 5)))


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"条件比较值不是数字：{value}") from exc


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


def _trigger_payload(row: DataPlatformChangeTriggerState) -> dict[str, Any]:
    return {
        "trigger_id": row.id,
        "workflow_id": row.workflow_id,
        "version_id": row.version_id,
        "node_key": row.node_key,
        "node_name": row.node_name,
        "enabled": row.enabled,
        "state": row.state,
        "config": row.config or {},
        "observed_value": row.observed_value,
        "pending_value": row.pending_value,
        "pending_queue": list(row.pending_queue or []),
        "applied_value": row.applied_value,
        "consecutive_matches": row.consecutive_matches,
        "pending_run_id": row.pending_run_id,
        "last_probe_at": row.last_probe_at,
        "next_probe_at": row.next_probe_at,
        "last_trigger_at": row.last_trigger_at,
        "last_success_at": row.last_success_at,
        "message": row.message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _probe_payload(row: DataPlatformChangeProbe) -> dict[str, Any]:
    return {
        "probe_id": row.id,
        "trigger_id": row.trigger_state_id,
        "previous_value": row.previous_value,
        "current_value": row.current_value,
        "condition_results": row.condition_results or [],
        "matched": row.matched,
        "status": row.status,
        "run_id": row.run_id,
        "message": row.message,
        "created_at": row.created_at,
    }


def _ident(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not clean or not _IDENT_RE.match(clean):
        raise ValueError(f"{label}不合法。")
    return clean


def _optional_ident(value: Any, label: str) -> str | None:
    clean = str(value or "").strip()
    return _ident(clean, label) if clean else None


def _q(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
