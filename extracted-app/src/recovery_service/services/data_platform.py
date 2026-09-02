from __future__ import annotations

import threading
import time
import uuid
import hashlib
import json
from calendar import monthrange
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import OperationalError

from recovery_service.common.logging import get_logger
from recovery_service.api.schemas.data_platform import (
    DataPlatformComponentRunLogResponse,
    DataPlatformComponentRunResponse,
    DataPlatformComponentRunTableResponse,
    DataPlatformDashboardResponse,
    DataPlatformFolderResponse,
    DataPlatformNodeResponse,
    DataPlatformNodeRunResponse,
    DataPlatformRunResponse,
    DataPlatformScheduleResponse,
    DataPlatformVersionResponse,
    DataPlatformWorkflowResponse,
)
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    DataAutomationBatch,
    DataPlatformFolder,
    DataPlatformChangeTriggerState,
    DataPlatformComponentRunLog,
    DataPlatformComponentRun,
    DataPlatformComponentRunTable,
    DataPlatformNode,
    DataPlatformNodeRun,
    DataPlatformWorkflow,
    DataPlatformWorkflowRun,
    DataPlatformWorkflowVersion,
    DatabaseConnectionProfile,
    RecoveryTask,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.auth import AuthContext
from recovery_service.services.doris_encryption import (
    create_sm4_batch_task,
    dispatch_queued_sm4_jobs_once,
    get_sm4_batch_task,
    run_sm4_task_definition,
    run_sm4_task_snapshot,
    sm4_task_definition_snapshot,
)
from recovery_service.services.doris_sm3_mapping import (
    run_sm3_task_definition_sync,
    run_sm3_task_snapshot_sync,
    sm3_task_definition_snapshot,
    get_sm3_task_status_sync,
)
from recovery_service.services.doris_sql_etl import execute_doris_sql
from recovery_service.services.data_sync import execute_data_sync
from recovery_service.services.data_change_trigger import (
    finalize_change_trigger_run,
    probe_change_trigger_now,
    retire_change_triggers,
    run_due_change_triggers,
    synchronize_change_triggers,
)

_SCHEDULER_STOP = threading.Event()
_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_LOCK = threading.Lock()
_VERSION_SCHEDULE_FIELDS = {
    "schedule_enabled",
    "schedule_type",
    "run_time",
    "day_of_month",
    "day_of_week",
    "interval_minutes",
}
_VERSION_DESIGN_FIELDS = {"nodes", "edges", "business_metadata"}
_COMPONENT_TASK_TYPES = {"doris_sql", "data_sync", "change_trigger"}
_CHANGE_TRIGGER_OPERATORS = {
    "row_count": {"changed", "increased", "increase_by", "increase_percent", "greater_than", "equals"},
    "max": {"changed", "increased", "increase_by", "increase_percent", "greater_than", "equals"},
    "min": {"changed", "increased", "increase_by", "increase_percent", "greater_than", "equals"},
    "schema_signature": {"changed", "equals"},
    "table_exists": {"changed", "equals", "became_true"},
    "scalar_sql": {"changed", "increased", "increase_by", "increase_percent", "greater_than", "equals", "became_true"},
}
_CHANGE_TRIGGER_THRESHOLD_OPERATORS = {"increase_by", "increase_percent", "greater_than", "equals"}
logger = get_logger(__name__)


def dashboard() -> DataPlatformDashboardResponse:
    session = get_sync_session_factory()()
    try:
        return DataPlatformDashboardResponse(
            node_count=session.scalar(select(func.count()).select_from(DataPlatformNode)) or 0,
            workflow_count=session.scalar(select(func.count()).select_from(DataPlatformWorkflow)) or 0,
            online_version_count=session.scalar(
                select(func.count()).select_from(DataPlatformWorkflowVersion).where(DataPlatformWorkflowVersion.status == "online")
            )
            or 0,
            running_count=session.scalar(
                select(func.count()).select_from(DataPlatformWorkflowRun).where(DataPlatformWorkflowRun.status == "running")
            )
            or 0,
            failed_count=session.scalar(
                select(func.count()).select_from(DataPlatformWorkflowRun).where(DataPlatformWorkflowRun.status == "failed")
            )
            or 0,
        )
    finally:
        session.close()


def create_node(*, name: str, node_type: str, description: str | None, config: dict, actor: AuthContext | None) -> DataPlatformNodeResponse:
    now = app_now()
    clean_config = _normalize_component_task_config(node_type, config or {})
    node = DataPlatformNode(
        id=uuid.uuid4(),
        name=name.strip(),
        revision=1,
        node_type=node_type,
        description=(description or "").strip() or None,
        config=clean_config,
        status="active",
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        session.add(node)
        session.commit()
        session.refresh(node)
        return _node_to_response(node)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def backfill_data_platform_release_metadata() -> None:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(select(DataPlatformWorkflowVersion)).scalars().all()
        changed = False
        for version in rows:
            try:
                with session.begin_nested():
                    nodes = _freeze_component_task_nodes(
                        session,
                        version.nodes or [],
                        preserve_existing=True,
                        tolerate_missing=True,
                    )
                    release = version.release_snapshot or _build_release_snapshot(
                        nodes,
                        version.edges or [],
                        _version_schedule_payload(version),
                    )
                    content_hash = _execution_content_hash(release)
                    if version.nodes != nodes:
                        version.nodes = nodes
                        changed = True
                    if version.release_snapshot != release:
                        version.release_snapshot = release
                        changed = True
                    if version.execution_content_hash != content_hash:
                        version.execution_content_hash = content_hash
                        changed = True
                    if version.status == "online":
                        synchronize_change_triggers(session, version)
            except Exception as exc:
                logger.warning(
                    "data platform release metadata backfill skipped",
                    version_id=str(version.id),
                    error=str(exc),
                )
        if changed:
            session.commit()
        else:
            session.flush()
            session.commit()
    finally:
        session.close()


def list_nodes() -> list[DataPlatformNodeResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataPlatformNode).order_by(desc(DataPlatformNode.updated_at)).limit(300)
        try:
            rows = session.execute(stmt).scalars().all()
        except OperationalError as exc:
            if not _is_mysql_out_of_sort_memory(exc):
                raise
            logger.warning("data platform node list ordered query fallback", error=str(exc))
            session.rollback()
            rows = _list_nodes_after_sort_memory_error(session)
        return [_node_to_response(row) for row in rows]
    finally:
        session.close()


def _is_mysql_out_of_sort_memory(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ()) or getattr(exc, "args", ())
    return bool(args and args[0] == 1038)


def _list_nodes_after_sort_memory_error(session) -> list[DataPlatformNode]:
    try:
        ids = session.execute(
            select(DataPlatformNode.id).order_by(desc(DataPlatformNode.updated_at)).limit(300)
        ).scalars().all()
        if not ids:
            return []
        rows = session.execute(select(DataPlatformNode).where(DataPlatformNode.id.in_(ids))).scalars().all()
        by_id = {row.id: row for row in rows}
        return [by_id[node_id] for node_id in ids if node_id in by_id]
    except OperationalError as exc:
        if not _is_mysql_out_of_sort_memory(exc):
            raise
        logger.warning("data platform node list unordered fallback", error=str(exc))
        session.rollback()
        return session.execute(select(DataPlatformNode).limit(300)).scalars().all()


def update_node(node_id: uuid.UUID, updates: dict[str, Any], actor: AuthContext | None) -> DataPlatformNodeResponse:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node:
            raise KeyError("Task node does not exist.")
        next_type = updates.get("node_type") or node.node_type
        if "config" in updates and updates["config"] is not None:
            if next_type == "change_trigger":
                published = {
                    key: value
                    for key, value in dict(node.config or {}).items()
                    if key.startswith("published_") or key in {"system_workflow_id", "standalone_trigger"}
                }
                updates["config"] = {**updates["config"], **published}
            updates["config"] = _normalize_component_task_config(next_type, updates["config"])
        changed = False
        for field in ("name", "node_type", "description", "config", "status"):
            if field in updates and updates[field] is not None:
                value = updates[field]
                if field in {"name", "description"} and isinstance(value, str):
                    value = value.strip()
                if field != "status" and getattr(node, field) != value:
                    changed = True
                setattr(node, field, value)
        if changed:
            node.revision = int(node.revision or 1) + 1
        node.updated_at = app_now()
        session.commit()
        session.refresh(node)
        return _node_to_response(node)
    finally:
        session.close()


def delete_node(node_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformNodeResponse:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node:
            raise KeyError("Task node does not exist.")
        response = _node_to_response(node)
        if node.node_type == "change_trigger":
            published_version_id = (node.config or {}).get("published_workflow_version_id")
            if published_version_id:
                retire_change_triggers(session, uuid.UUID(str(published_version_id)))
            workflow_id = (node.config or {}).get("system_workflow_id")
            if workflow_id:
                workflow = session.get(DataPlatformWorkflow, uuid.UUID(str(workflow_id)))
                if workflow:
                    workflow.status = "archived"
                    workflow.updated_at = app_now()
        session.delete(node)
        session.commit()
        return response
    finally:
        session.close()


class _DataSyncComponentRunRecorder:
    def __init__(self, component_run_id: uuid.UUID, node_id: uuid.UUID):
        self.component_run_id = component_run_id
        self.node_id = node_id
        self._lock = threading.Lock()
        self._table_ids: dict[str, uuid.UUID] = {}
        self._has_table_events = False

    def handle(self, event: str, payload: dict[str, Any]) -> None:
        if event == "run_prepared":
            self._prepare_tables(payload)
        elif event == "run_log":
            self._append_log(None, payload.get("log") or {})
        elif event == "table_started":
            self._mark_table(payload.get("mapping") or {}, "running", None)
        elif event == "table_log":
            table_id = self._ensure_table_id(payload.get("mapping") or {})
            self._append_log(table_id, payload.get("log") or {})
        elif event == "table_finished":
            self._finish_table(payload.get("mapping") or {}, payload.get("result") or {}, "succeeded")
        elif event == "table_failed":
            self._finish_table(payload.get("mapping") or {}, payload.get("result") or {}, "failed")
        elif event == "table_skipped":
            self._finish_table(payload.get("mapping") or {}, payload.get("result") or {}, "skipped")

    def sync_from_result(self, result: dict[str, Any]) -> None:
        if self._has_table_events:
            return
        for item in result.get("table_results") or []:
            mapping = {
                "mapping_id": str(item.get("table_id") or ""),
                "source_catalog": item.get("source_catalog"),
                "source_schema": item.get("source_schema"),
                "source_table": item.get("source_table"),
                "target_database": item.get("target_database"),
                "target_table": item.get("target_table"),
                "sync_method": item.get("sync_method"),
                "write_mode": item.get("write_mode"),
                "schema_policy": item.get("schema_policy"),
            }
            status = str(item.get("status") or "succeeded")
            self._finish_table(mapping, item, status)
            table_id = self._ensure_table_id(mapping)
            for log in item.get("logs") or []:
                self._append_log(table_id, log)

    def _prepare_tables(self, payload: dict[str, Any]) -> None:
        self._has_table_events = True
        for mapping in payload.get("mappings") or []:
            self._ensure_table_id(mapping)
        self._refresh_summary()

    def _mapping_key(self, mapping: dict[str, Any]) -> str:
        mapping_id = str(mapping.get("mapping_id") or mapping.get("id") or mapping.get("table_id") or "").strip()
        if mapping_id:
            return mapping_id
        return "|".join(
            str(mapping.get(field) or "")
            for field in ("source_catalog", "source_schema", "source_table", "target_database", "target_table")
        )

    def _ensure_table_id(self, mapping: dict[str, Any]) -> uuid.UUID | None:
        key = self._mapping_key(mapping)
        if not key:
            return None
        with self._lock:
            cached = self._table_ids.get(key)
        if cached:
            return cached
        session = get_sync_session_factory()()
        try:
            row = session.execute(
                select(DataPlatformComponentRunTable).where(
                    DataPlatformComponentRunTable.component_run_id == self.component_run_id,
                    DataPlatformComponentRunTable.mapping_id == key,
                )
            ).scalars().first()
            if not row:
                now = app_now()
                row = DataPlatformComponentRunTable(
                    id=uuid.uuid4(),
                    component_run_id=self.component_run_id,
                    node_id=self.node_id,
                    mapping_id=key,
                    source_catalog=mapping.get("source_catalog"),
                    source_schema=mapping.get("source_schema"),
                    source_table=mapping.get("source_table"),
                    target_database=mapping.get("target_database"),
                    target_table=mapping.get("target_table"),
                    sync_method=mapping.get("sync_method"),
                    write_mode=mapping.get("write_mode"),
                    schema_policy=mapping.get("schema_policy"),
                    status="queued",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.commit()
            with self._lock:
                self._table_ids[key] = row.id
            return row.id
        finally:
            session.close()

    def _mark_table(self, mapping: dict[str, Any], status: str, message: str | None) -> None:
        self._has_table_events = True
        table_id = self._ensure_table_id(mapping)
        if not table_id:
            return
        session = get_sync_session_factory()()
        try:
            row = session.get(DataPlatformComponentRunTable, table_id)
            if row:
                now = app_now()
                row.status = status
                row.message = message
                row.started_at = row.started_at or now
                row.updated_at = now
                session.commit()
            self._refresh_summary(session)
        finally:
            session.close()

    def _finish_table(self, mapping: dict[str, Any], result: dict[str, Any], status: str) -> None:
        self._has_table_events = True
        table_id = self._ensure_table_id(mapping)
        if not table_id:
            return
        session = get_sync_session_factory()()
        try:
            row = session.get(DataPlatformComponentRunTable, table_id)
            if row:
                now = app_now()
                row.status = status if status in {"queued", "running", "succeeded", "failed", "skipped"} else "failed"
                row.message = result.get("message")
                row.loaded_rows = int(result.get("loaded_rows") or 0)
                row.duration_ms = int(result.get("duration_ms") or 0)
                row.result_summary = _compact_table_result(result)
                row.started_at = row.started_at or now
                row.finished_at = now if row.status in {"succeeded", "failed", "skipped"} else row.finished_at
                row.updated_at = now
                session.commit()
            self._refresh_summary(session)
        finally:
            session.close()

    def _append_log(self, table_run_id: uuid.UUID | None, log: dict[str, Any]) -> None:
        if not log:
            return
        session = get_sync_session_factory()()
        try:
            created_raw = log.get("created_at") or log.get("time")
            created_at = _parse_datetime_or_now(created_raw)
            session.add(
                DataPlatformComponentRunLog(
                    id=uuid.uuid4(),
                    component_run_id=self.component_run_id,
                    table_run_id=table_run_id,
                    level=str(log.get("level") or "INFO")[:16],
                    stage=str(log.get("stage") or "")[:64] or None,
                    message=str(log.get("message") or ""),
                    payload=log.get("payload") or log.get("detail") or {},
                    created_at=created_at,
                )
            )
            run = session.get(DataPlatformComponentRun, self.component_run_id)
            if run:
                run.updated_at = app_now()
            session.commit()
        finally:
            session.close()

    def _refresh_summary(self, session=None) -> None:
        owns_session = session is None
        if owns_session:
            session = get_sync_session_factory()()
        try:
            rows = session.execute(
                select(DataPlatformComponentRunTable).where(
                    DataPlatformComponentRunTable.component_run_id == self.component_run_id
                )
            ).scalars().all()
            run = session.get(DataPlatformComponentRun, self.component_run_id)
            if not run:
                return
            summary = _component_run_live_summary(run.result or {}, rows)
            run.result = summary
            run.updated_at = app_now()
            session.commit()
        finally:
            if owns_session:
                session.close()


def _parse_datetime_or_now(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return app_now()


def _compact_table_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result or {})
    compact.pop("logs", None)
    if isinstance(compact.get("stream_load"), list):
        compact["stream_load"] = compact["stream_load"][-3:]
    return compact


def _component_run_live_summary(base: dict[str, Any], table_rows: list[DataPlatformComponentRunTable]) -> dict[str, Any]:
    summary = _compact_component_run_result(base or {})
    table_summaries = [_table_run_summary(row) for row in table_rows]
    success_count = sum(1 for row in table_rows if row.status == "succeeded")
    failed_count = sum(1 for row in table_rows if row.status == "failed")
    skipped_count = sum(1 for row in table_rows if row.status == "skipped")
    running_count = sum(1 for row in table_rows if row.status == "running")
    queued_count = sum(1 for row in table_rows if row.status == "queued")
    summary.update(
        {
            "table_count": len(table_rows),
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "running_count": running_count,
            "queued_count": queued_count,
            "loaded_rows": sum(int(row.loaded_rows or 0) for row in table_rows if row.status == "succeeded"),
            "table_results": table_summaries,
        }
    )
    return summary


def _compact_component_run_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result or {})
    compact["logs"] = list(compact.get("logs") or [])[:5]
    compact["table_results"] = [_compact_table_result(item) for item in compact.get("table_results") or []]
    return compact


def _table_run_summary(row: DataPlatformComponentRunTable) -> dict[str, Any]:
    return {
        "table_run_id": str(row.id),
        "table_id": row.mapping_id,
        "source_catalog": row.source_catalog,
        "source_schema": row.source_schema,
        "source_table": row.source_table,
        "target_database": row.target_database,
        "target_table": row.target_table,
        "sync_method": row.sync_method,
        "write_mode": row.write_mode,
        "schema_policy": row.schema_policy,
        "status": row.status,
        "message": row.message,
        "loaded_rows": int(row.loaded_rows or 0),
        "duration_ms": int(row.duration_ms or 0),
    }


def submit_component_task_run(
    node_id: uuid.UUID,
    overrides: dict[str, Any] | None = None,
    actor: AuthContext | None = None,
) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active":
            raise KeyError("Component task does not exist.")
        if node.node_type != "data_sync":
            session.close()
            return run_component_task_once(node_id, overrides, actor)

        from recovery_service.services.assistant_execution import sync_config_for_batch
        frozen_config = sync_config_for_batch(session, (overrides or {}).get("pipeline_batch_id"), node.id, node.config or {})
        config = _normalize_component_task_config(node.node_type, frozen_config)
        if isinstance(overrides, dict) and overrides.get("selected_tables") is not None:
            config["selected_tables"] = list(overrides.get("selected_tables") or [])
        selected_items = list(config.get("selected_tables") or [])
        now = app_now()
        component_run = DataPlatformComponentRun(
            id=uuid.uuid4(),
            node_id=node.id,
            node_type=node.node_type,
            node_name=node.name,
            node_revision=int(node.revision or 1),
            trigger_type="manual",
            selected_items=selected_items or None,
            status="queued",
            message="Data sync task has been queued.",
            result={
                "status": "queued",
                "message": "Data sync task has been queued.",
                "selected_tables": selected_items,
                "runtime_overrides": {
                    "pipeline_batch_id": str(overrides.get("pipeline_batch_id")) if isinstance(overrides, dict) and overrides.get("pipeline_batch_id") else None,
                    "restored_target": dict(overrides.get("restored_target") or {}) if isinstance(overrides, dict) else {},
                },
                "logs": [
                    {
                        "level": "INFO",
                        "stage": "queued",
                        "message": "Data sync task has been queued.",
                        "created_at": now.isoformat(),
                    }
                ],
            },
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_at=now,
            updated_at=now,
        )
        session.add(component_run)
        session.commit()

        from recovery_service.settings import get_settings
        from recovery_service.workers.celery_app import celery_app

        settings = get_settings()
        task = celery_app.send_task(
            "data_platform.component_task_run",
            args=[str(component_run.id)],
            queue=settings.celery_data_sync_queue,
        )
        component_run.result = {**(component_run.result or {}), "celery_task_id": str(task.id)}
        component_run.updated_at = app_now()
        session.commit()
        return {
            "status": "queued",
            "message": "Data sync task has been queued.",
            "component_run_id": str(component_run.id),
            "run_id": str(component_run.id),
            "selected_tables": selected_items,
            "celery_task_id": str(task.id),
        }
    finally:
        session.close()


def _mark_component_run_failed(
    session,
    component_run: DataPlatformComponentRun,
    message: str,
    selected_items: list[str] | None = None,
) -> None:
    finished = app_now()
    component_run.status = "failed"
    component_run.message = message
    component_run.result = _compact_component_run_result(
        {
            "status": "failed",
            "message": message,
            "selected_tables": list(selected_items or component_run.selected_items or []),
            "success_count": 0,
            "failed_count": 1,
            "loaded_rows": 0,
            "duration_ms": 0,
            "table_results": [],
            "logs": [
                {
                    "level": "ERROR",
                    "stage": "failed",
                    "message": message,
                    "created_at": finished.isoformat(),
                }
            ],
            "component_run_id": str(component_run.id),
            "run_id": str(component_run.id),
        }
    )
    component_run.finished_at = finished
    component_run.updated_at = finished
    session.commit()


def run_queued_component_task(component_run_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        component_run = session.get(DataPlatformComponentRun, component_run_id)
        if not component_run:
            raise KeyError("Component run does not exist.")
        node = session.get(DataPlatformNode, component_run.node_id)
        if not node or node.status != "active":
            message = "Component task does not exist."
            _mark_component_run_failed(session, component_run, message)
            raise KeyError(message)
        if node.node_type != "data_sync":
            message = "Only data sync component runs can be queued."
            _mark_component_run_failed(session, component_run, message)
            raise ValueError(message)

        try:
            runtime_overrides = dict((component_run.result or {}).get("runtime_overrides") or {})
            from recovery_service.services.assistant_execution import sync_config_for_batch
            frozen_config = sync_config_for_batch(session, runtime_overrides.get("pipeline_batch_id"), node.id, node.config or {})
            config = _normalize_component_task_config(node.node_type, frozen_config)
            restored_target = dict(runtime_overrides.get("restored_target") or {})
            restored_schema = str(restored_target.get("schema") or "").strip()
            if restored_schema:
                config["source_schema"] = restored_schema
                config["table_mappings"] = [
                    {**dict(item), "source_schema": restored_schema}
                    for item in config.get("table_mappings") or []
                ]
            if component_run.selected_items is not None:
                config["selected_tables"] = list(component_run.selected_items or [])
            source = session.get(DatabaseConnectionProfile, uuid.UUID(str(config["source_connection_id"])))
            target = session.get(DatabaseConnectionProfile, uuid.UUID(str(config["target_connection_id"])))
            if not source or not target:
                raise ValueError("Data sync source or target connection does not exist.")
            if restored_schema:
                pipeline_batch_id = runtime_overrides.get("pipeline_batch_id")
                batch = session.get(DataAutomationBatch, uuid.UUID(str(pipeline_batch_id))) if pipeline_batch_id else None
                restore_task = session.get(RecoveryTask, batch.restore_task_id) if batch and batch.restore_task_id else None
                target_options = dict(((restore_task.options or {}).get("professional_flow") or {}).get("target") or {}) if restore_task else {}
                oracle_profile_ref = (restore_task.options or {}).get("target_connection_profile") if restore_task else None
                oracle_profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(oracle_profile_ref))) if oracle_profile_ref else None
                encrypted_password = str(target_options.get("generated_user_password") or "")
                if oracle_profile and encrypted_password:
                    source = SimpleNamespace(
                        engine="oracle",
                        host=oracle_profile.host,
                        port=oracle_profile.port,
                        username=restored_schema,
                        password_enc=encrypted_password,
                        database=oracle_profile.database,
                        service_name=oracle_profile.service_name,
                        dsn=oracle_profile.dsn,
                    )
                    config["source_catalog"] = "local_oracle"
                    config["sync_method"] = "stream_load"
                    config["table_mappings"] = [
                        {**dict(item), "source_catalog": "local_oracle"}
                        for item in config.get("table_mappings") or []
                    ]
                else:
                    raise ValueError("恢复任务缺少 Oracle 目标连接快照或生成用户口令，无法直连同步。")
        except Exception as exc:
            _mark_component_run_failed(session, component_run, str(exc))
            raise

        now = app_now()
        component_run.status = "running"
        component_run.message = "Data sync task is running."
        component_run.started_at = component_run.started_at or now
        component_run.updated_at = now
        component_run.result = {
            **(component_run.result or {}),
            "status": "running",
            "message": "Data sync task is running.",
        }
        session.commit()

        selected_items = list(config.get("selected_tables") or [])
        recorder = _DataSyncComponentRunRecorder(component_run.id, node.id)
        try:
            result = execute_data_sync(source, target, config, event_hook=recorder.handle)
            result = dict(result or {})
            recorder.sync_from_result(result)
            result["component_run_id"] = str(component_run.id)
            result["run_id"] = str(component_run.id)
            result["selected_tables"] = selected_items
            config_patch = result.get("config_patch") if isinstance(result, dict) else None
            finished = app_now()
            component_run.status = _component_run_status(result.get("status"))
            component_run.message = result.get("message")
            component_run.result = _compact_component_run_result(result)
            component_run.finished_at = finished
            component_run.updated_at = finished
            if isinstance(config_patch, dict):
                updated_config = dict(node.config or {})
                updated_config.update(config_patch)
                node.config = updated_config
                node.updated_at = finished
            session.commit()
            return result
        except Exception as exc:
            finished = app_now()
            error_result = {
                "status": "failed",
                "message": str(exc),
                "selected_tables": selected_items,
                "success_count": 0,
                "failed_count": 1,
                "loaded_rows": 0,
                "duration_ms": 0,
                "table_results": [],
                "logs": [
                    {
                        "level": "ERROR",
                        "stage": "failed",
                        "message": str(exc),
                        "created_at": finished.isoformat(),
                    }
                ],
                "component_run_id": str(component_run.id),
                "run_id": str(component_run.id),
            }
            component_run.status = "failed"
            component_run.message = str(exc)
            component_run.result = _compact_component_run_result(error_result)
            component_run.finished_at = finished
            component_run.updated_at = finished
            recorder.handle(
                "run_log",
                {
                    "log": {
                        "level": "ERROR",
                        "stage": "failed",
                        "message": str(exc),
                        "created_at": finished.isoformat(),
                    }
                },
            )
            session.commit()
            raise
    finally:
        session.close()


def run_component_task_once(
    node_id: uuid.UUID,
    overrides: dict[str, Any] | None = None,
    actor: AuthContext | None = None,
) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active":
            raise KeyError("Component task does not exist.")
        config = _normalize_component_task_config(node.node_type, node.config or {})
        if isinstance(overrides, dict) and overrides.get("selected_tables") is not None:
            config["selected_tables"] = list(overrides.get("selected_tables") or [])
        if node.node_type == "doris_sql":
            profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(config["connection_id"])))
            if not profile:
                raise ValueError("Doris SQL 任务连接不存在。")
            result = execute_doris_sql(
                profile,
                database=config.get("database"),
                sql=config["sql"],
                limit=config.get("limit") or 200,
            )
            return result.model_dump()
        if node.node_type == "data_sync":
            source = session.get(
                DatabaseConnectionProfile, uuid.UUID(str(config["source_connection_id"]))
            )
            target = session.get(
                DatabaseConnectionProfile, uuid.UUID(str(config["target_connection_id"]))
            )
            if not source or not target:
                raise ValueError("数据同步任务的源连接或目标连接不存在。")
            selected_items = list(config.get("selected_tables") or [])
            now = app_now()
            component_run = DataPlatformComponentRun(
                id=uuid.uuid4(),
                node_id=node.id,
                node_type=node.node_type,
                node_name=node.name,
                node_revision=int(node.revision or 1),
                trigger_type="manual",
                selected_items=selected_items or None,
                status="running",
                message="数据同步任务执行中。",
                result={
                    "status": "running",
                    "message": "数据同步任务执行中。",
                    "selected_tables": selected_items,
                    "logs": [
                        {
                            "level": "INFO",
                            "stage": "start",
                            "message": "任务已进入后端执行。",
                            "created_at": now.isoformat(),
                        }
                    ],
                },
                created_by_user_id=_actor_uuid(actor),
                created_by_username=actor.username if actor else None,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(component_run)
            session.commit()
            recorder = _DataSyncComponentRunRecorder(component_run.id, node.id)
            try:
                result = execute_data_sync(source, target, config, event_hook=recorder.handle)
                result = dict(result or {})
                recorder.sync_from_result(result)
                result["component_run_id"] = str(component_run.id)
                result["run_id"] = str(component_run.id)
                result["selected_tables"] = selected_items
                config_patch = result.get("config_patch") if isinstance(result, dict) else None
                finished = app_now()
                component_run.status = _component_run_status(result.get("status"))
                component_run.message = result.get("message")
                component_run.result = _compact_component_run_result(result)
                component_run.finished_at = finished
                component_run.updated_at = finished
                if isinstance(config_patch, dict):
                    updated_config = dict(node.config or {})
                    updated_config.update(config_patch)
                    node.config = updated_config
                    node.updated_at = finished
                session.commit()
                return result
            except Exception as exc:
                finished = app_now()
                error_result = {
                    "status": "failed",
                    "message": str(exc),
                    "selected_tables": selected_items,
                    "success_count": 0,
                    "failed_count": 1,
                    "loaded_rows": 0,
                    "duration_ms": 0,
                    "table_results": [],
                    "logs": [
                        {
                            "level": "ERROR",
                            "stage": "failed",
                            "message": str(exc),
                            "created_at": finished.isoformat(),
                        }
                    ],
                    "component_run_id": str(component_run.id),
                    "run_id": str(component_run.id),
                }
                component_run.status = "failed"
                component_run.message = str(exc)
                component_run.result = _compact_component_run_result(error_result)
                component_run.finished_at = finished
                component_run.updated_at = finished
                recorder.handle(
                    "run_log",
                    {
                        "log": {
                            "level": "ERROR",
                            "stage": "failed",
                            "message": str(exc),
                            "created_at": finished.isoformat(),
                        }
                    },
                )
                session.commit()
                raise
        if node.node_type == "change_trigger":
            return run_change_trigger_task_once(node_id, actor=None)
        raise ValueError("Unsupported component task type.")
    finally:
        session.close()


def validate_change_trigger_task(node_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        snapshot = _build_standalone_trigger_snapshot(session, node)
        return {
            "valid": True,
            "task_id": str(node.id),
            "revision": int(node.revision or 1),
            "node_count": len(snapshot["nodes"]) - 1,
            "edge_count": len(snapshot["edges"]),
            "message": "触发器监控配置和内部流程校验通过。",
        }
    finally:
        session.close()


def publish_change_trigger_task(node_id: uuid.UUID, actor: AuthContext | None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        snapshot = _build_standalone_trigger_snapshot(session, node)
        config = dict(node.config or {})
        workflow = _ensure_trigger_system_workflow(session, node, actor)
        old_version_id = config.get("published_workflow_version_id")
        old_enabled = False
        if old_version_id:
            old_states = session.execute(
                select(DataPlatformChangeTriggerState).where(
                    DataPlatformChangeTriggerState.version_id == uuid.UUID(str(old_version_id))
                )
            ).scalars().all()
            old_enabled = any(item.enabled for item in old_states)
            retire_change_triggers(session, uuid.UUID(str(old_version_id)))
            old_version = session.get(DataPlatformWorkflowVersion, uuid.UUID(str(old_version_id)))
            if old_version:
                old_version.status = "offline"
                old_version.offline_at = app_now()
                old_version.updated_at = app_now()
        version = DataPlatformWorkflowVersion(
            id=uuid.uuid4(),
            workflow_id=workflow.id,
            version_no=_next_version_no(session, workflow.id, "prod"),
            channel="prod",
            status="online",
            nodes=snapshot["nodes"],
            edges=snapshot["edges"],
            release_snapshot=snapshot,
            execution_content_hash=_execution_content_hash(snapshot),
            schedule_enabled=False,
            schedule_type="interval",
            interval_minutes=max(1, int(config.get("probe_interval_minutes") or 5)),
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            updated_by_username=actor.username if actor else None,
            published_at=app_now(),
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(version)
        session.flush()
        synchronize_change_triggers(session, version)
        state = session.execute(
            select(DataPlatformChangeTriggerState).where(
                DataPlatformChangeTriggerState.version_id == version.id,
                DataPlatformChangeTriggerState.node_key == "monitor",
            )
        ).scalar_one()
        state.enabled = old_enabled
        state.state = "active" if old_enabled else "paused"
        state.next_probe_at = app_now() if old_enabled else None
        state.message = "触发器新版本已发布并继续运行。" if old_enabled else "触发器已发布，等待启用。"
        state.updated_at = app_now()
        config.update(
            {
                "standalone_trigger": True,
                "system_workflow_id": str(workflow.id),
                "published_revision": int(node.revision or 1),
                "published_workflow_version_id": str(version.id),
                "published_version_no": version.version_no,
                "published_at": app_now().isoformat(),
                "published_snapshot": snapshot,
            }
        )
        node.config = config
        node.updated_at = app_now()
        workflow.updated_at = app_now()
        session.commit()
        session.refresh(node)
        session.refresh(state)
        return {
            "task": _node_to_response(node).model_dump(),
            "deployment": _standalone_trigger_status_payload(node, state),
            "message": f"触发器 V{version.version_no} 已发布。",
        }
    finally:
        session.close()


def get_change_trigger_task_status(node_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        state = _standalone_trigger_state(session, node)
        return _standalone_trigger_status_payload(node, state)
    finally:
        session.close()


def set_change_trigger_task_enabled(node_id: uuid.UUID, *, enabled: bool) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        state = _standalone_trigger_state(session, node, required=True)
        state.enabled = bool(enabled)
        state.state = "active" if enabled else "paused"
        state.next_probe_at = app_now() if enabled else None
        state.message = "触发器已启用。" if enabled else "触发器已暂停。"
        state.updated_at = app_now()
        session.commit()
        session.refresh(state)
        return _standalone_trigger_status_payload(node, state)
    finally:
        session.close()


def probe_change_trigger_task_now(node_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        state = _standalone_trigger_state(session, node, required=True)
        trigger_id = state.id
    finally:
        session.close()
    return probe_change_trigger_now(trigger_id, run_version)


def run_change_trigger_task_once(node_id: uuid.UUID, actor: AuthContext | None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        node = session.get(DataPlatformNode, node_id)
        if not node or node.status != "active" or node.node_type != "change_trigger":
            raise KeyError("数据变化触发器任务不存在。")
        snapshot = _build_standalone_trigger_snapshot(session, node)
        workflow = _ensure_trigger_system_workflow(session, node, actor)
        version = DataPlatformWorkflowVersion(
            id=uuid.uuid4(),
            workflow_id=workflow.id,
            version_no=_next_version_no(session, workflow.id, "dev"),
            channel="dev",
            status="draft",
            nodes=snapshot["nodes"],
            edges=snapshot["edges"],
            release_snapshot=snapshot,
            execution_content_hash=_execution_content_hash(snapshot),
            schedule_enabled=False,
            schedule_type="interval",
            interval_minutes=max(1, int((node.config or {}).get("probe_interval_minutes") or 5)),
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            updated_by_username=actor.username if actor else None,
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(version)
        session.commit()
        version_id = version.id
    finally:
        session.close()
    run = run_version(
        version_id,
        trigger_type="trigger_test",
        actor=actor,
        trigger_context={"dry_run": True, "applied_value": {}},
    )
    return {"run_id": str(run.run_id), "state": run.status, "message": "触发器内部流程已提交运行。"}


def create_folder(*, name: str, parent_id: uuid.UUID | None, actor: AuthContext | None) -> DataPlatformFolderResponse:
    now = app_now()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Folder name is required.")
    folder = DataPlatformFolder(
        id=uuid.uuid4(),
        name=clean_name,
        parent_id=parent_id,
        status="active",
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        if parent_id:
            parent = session.get(DataPlatformFolder, parent_id)
            if not parent or parent.status == "archived":
                raise ValueError("Parent folder does not exist.")
        session.add(folder)
        session.commit()
        session.refresh(folder)
        return _folder_to_response(folder)
    finally:
        session.close()


def list_folders() -> list[DataPlatformFolderResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DataPlatformFolder)
            .where(DataPlatformFolder.status != "archived")
            .order_by(DataPlatformFolder.name)
            .limit(300)
        ).scalars().all()
        return [_folder_to_response(row) for row in rows]
    finally:
        session.close()


def update_folder(folder_id: uuid.UUID, updates: dict[str, Any], actor: AuthContext | None) -> DataPlatformFolderResponse:
    session = get_sync_session_factory()()
    try:
        folder = session.get(DataPlatformFolder, folder_id)
        if not folder:
            raise KeyError("Folder does not exist.")
        if "name" in updates and updates["name"] is not None:
            clean_name = str(updates["name"]).strip()
            if not clean_name:
                raise ValueError("Folder name is required.")
            folder.name = clean_name
        if "parent_id" in updates:
            parent_id = updates["parent_id"]
            if parent_id == folder.id:
                raise ValueError("A folder cannot be moved into itself.")
            if parent_id:
                parent = session.get(DataPlatformFolder, parent_id)
                if not parent or parent.status == "archived":
                    raise ValueError("Target folder does not exist.")
                cursor = parent
                visited: set[uuid.UUID] = set()
                while cursor and cursor.id not in visited:
                    if cursor.id == folder.id:
                        raise ValueError("A folder cannot be moved into its descendant.")
                    visited.add(cursor.id)
                    cursor = session.get(DataPlatformFolder, cursor.parent_id) if cursor.parent_id else None
            folder.parent_id = parent_id
        if "status" in updates and updates["status"] is not None:
            folder.status = updates["status"]
        folder.updated_at = app_now()
        session.commit()
        session.refresh(folder)
        return _folder_to_response(folder)
    finally:
        session.close()


def archive_folder(folder_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformFolderResponse:
    session = get_sync_session_factory()()
    try:
        folder = session.get(DataPlatformFolder, folder_id)
        if not folder or folder.status == "archived":
            raise KeyError("Folder does not exist.")
        child_count = session.scalar(
            select(func.count()).select_from(DataPlatformFolder).where(
                DataPlatformFolder.parent_id == folder_id,
                DataPlatformFolder.status != "archived",
            )
        ) or 0
        workflow_count = session.scalar(
            select(func.count()).select_from(DataPlatformWorkflow).where(
                DataPlatformWorkflow.folder_id == folder_id,
                DataPlatformWorkflow.status != "archived",
            )
        ) or 0
        if child_count or workflow_count:
            raise ValueError("Folder is not empty. Move or delete its subfolders and tasks first.")
        folder.status = "archived"
        folder.updated_at = app_now()
        session.commit()
        session.refresh(folder)
        return _folder_to_response(folder)
    finally:
        session.close()


def create_workflow(*, name: str, description: str | None, folder_id: uuid.UUID | None, business_metadata: dict | None = None, actor: AuthContext | None) -> DataPlatformWorkflowResponse:
    now = app_now()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Workflow name is required.")
    workflow = DataPlatformWorkflow(
        id=uuid.uuid4(),
        folder_id=folder_id,
        name=clean_name,
        description=(description or "").strip() or None,
        business_metadata=deepcopy(business_metadata or {}),
        status="active",
        created_by_user_id=_actor_uuid(actor),
        created_by_username=actor.username if actor else None,
        created_at=now,
        updated_at=now,
    )
    session = get_sync_session_factory()()
    try:
        if folder_id:
            folder = session.get(DataPlatformFolder, folder_id)
            if not folder or folder.status == "archived":
                raise ValueError("Target folder does not exist.")
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        return _workflow_to_response(session, workflow)
    finally:
        session.close()


def list_workflows() -> list[DataPlatformWorkflowResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DataPlatformWorkflow)
            .where(DataPlatformWorkflow.status == "active")
            .order_by(desc(DataPlatformWorkflow.updated_at))
            .limit(300)
        ).scalars().all()
        return [_workflow_to_response(session, row) for row in rows]
    finally:
        session.close()


def update_workflow(workflow_id: uuid.UUID, updates: dict[str, Any], actor: AuthContext | None) -> DataPlatformWorkflowResponse:
    session = get_sync_session_factory()()
    try:
        workflow = session.get(DataPlatformWorkflow, workflow_id)
        if not workflow:
            raise KeyError("Workflow does not exist.")
        if "name" in updates and updates["name"] is not None:
            clean_name = str(updates["name"]).strip()
            if not clean_name:
                raise ValueError("Workflow name is required.")
            workflow.name = clean_name
        if "description" in updates:
            workflow.description = str(updates["description"] or "").strip() or None
        if "folder_id" in updates:
            folder_id = updates["folder_id"]
            if folder_id:
                folder = session.get(DataPlatformFolder, folder_id)
                if not folder or folder.status == "archived":
                    raise ValueError("Target folder does not exist.")
            workflow.folder_id = folder_id
        if "status" in updates and updates["status"] is not None:
            workflow.status = updates["status"]
        if "business_metadata" in updates and updates["business_metadata"] is not None:
            workflow.business_metadata = deepcopy(updates["business_metadata"])
        workflow.updated_at = app_now()
        session.commit()
        session.refresh(workflow)
        return _workflow_to_response(session, workflow)
    finally:
        session.close()


def copy_workflow(
    workflow_id: uuid.UUID,
    *,
    name: str | None,
    folder_id: uuid.UUID | None,
    actor: AuthContext | None,
) -> DataPlatformWorkflowResponse:
    session = get_sync_session_factory()()
    try:
        source = session.get(DataPlatformWorkflow, workflow_id)
        if not source or source.status == "archived":
            raise KeyError("Workflow does not exist.")
        if folder_id:
            folder = session.get(DataPlatformFolder, folder_id)
            if not folder or folder.status == "archived":
                raise ValueError("Target folder does not exist.")
        clean_name = (name or f"{source.name} 副本").strip()
        if not clean_name:
            raise ValueError("Workflow name is required.")
        now = app_now()
        copied = DataPlatformWorkflow(
            id=uuid.uuid4(),
            folder_id=folder_id,
            name=clean_name,
            description=source.description,
            business_metadata=deepcopy(source.business_metadata or {}),
            status="active",
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_at=now,
            updated_at=now,
        )
        session.add(copied)
        source_versions = session.execute(
            select(DataPlatformWorkflowVersion)
            .where(DataPlatformWorkflowVersion.workflow_id == workflow_id)
            .order_by(
                desc(DataPlatformWorkflowVersion.channel == "dev"),
                desc(DataPlatformWorkflowVersion.version_no),
            )
        ).scalars().all()
        if source_versions:
            source_version = source_versions[0]
            copied_nodes = deepcopy(source_version.nodes or [])
            for node in copied_nodes:
                config = dict(node.get("config") or {})
                config.pop("task_definition_snapshot", None)
                config.pop("task_definition_revision", None)
                node["config"] = config
            copied_nodes = _freeze_component_task_nodes(
                session,
                copied_nodes,
                preserve_existing=False,
                tolerate_missing=True,
            )
            copied_edges = deepcopy(source_version.edges or [])
            copied_metadata = deepcopy(source_version.business_metadata or source.business_metadata or {})
            copied_release = _build_release_snapshot(copied_nodes, copied_edges, {**_version_schedule_payload(source_version), "business_metadata": copied_metadata})
            session.add(
                DataPlatformWorkflowVersion(
                    id=uuid.uuid4(),
                    workflow_id=copied.id,
                    version_no=1,
                    channel="dev",
                    status="draft",
                    nodes=copied_nodes,
                    edges=copied_edges,
                    business_metadata=copied_metadata,
                    release_snapshot=copied_release,
                    execution_content_hash=_execution_content_hash(copied_release),
                    schedule_enabled=False,
                    schedule_type=source_version.schedule_type,
                    run_time=source_version.run_time,
                    day_of_month=source_version.day_of_month,
                    day_of_week=source_version.day_of_week,
                    interval_minutes=source_version.interval_minutes,
                    created_by_user_id=_actor_uuid(actor),
                    created_by_username=actor.username if actor else None,
                    updated_by_username=actor.username if actor else None,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
        session.refresh(copied)
        return _workflow_to_response(session, copied)
    finally:
        session.close()


def archive_workflow(workflow_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformWorkflowResponse:
    session = get_sync_session_factory()()
    try:
        workflow = session.get(DataPlatformWorkflow, workflow_id)
        if not workflow:
            raise KeyError("Workflow does not exist.")
        now = app_now()
        workflow.status = "archived"
        workflow.updated_at = now
        versions = session.execute(
            select(DataPlatformWorkflowVersion).where(DataPlatformWorkflowVersion.workflow_id == workflow_id)
        ).scalars().all()
        for version in versions:
            if version.status == "online":
                version.status = "offline"
                version.offline_at = now
            version.schedule_enabled = False
            version.next_run_at = None
            retire_change_triggers(session, version.id)
            version.updated_by_username = actor.username if actor else None
            version.updated_at = now
        session.commit()
        session.refresh(workflow)
        return _workflow_to_response(session, workflow)
    finally:
        session.close()


def create_version(workflow_id: uuid.UUID, body: dict[str, Any], actor: AuthContext | None) -> DataPlatformVersionResponse:
    session = get_sync_session_factory()()
    try:
        workflow = session.get(DataPlatformWorkflow, workflow_id)
        if not workflow:
            raise KeyError("Workflow does not exist.")
        channel = body.get("channel") or "dev"
        version_no = _next_version_no(session, workflow_id, channel)
        nodes = _freeze_component_task_nodes(session, _normalize_nodes(body.get("nodes") or []), preserve_existing=True)
        edges = _normalize_edges(body.get("edges") or [])
        business_metadata = deepcopy(body.get("business_metadata") if body.get("business_metadata") is not None else (workflow.business_metadata or {}))
        release_snapshot = _build_release_snapshot(nodes, edges, {**body, "business_metadata": business_metadata})
        version = DataPlatformWorkflowVersion(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            version_no=version_no,
            channel=channel,
            status="draft" if channel == "dev" else "submitted",
            nodes=nodes,
            edges=edges,
            business_metadata=business_metadata,
            release_snapshot=release_snapshot,
            execution_content_hash=_execution_content_hash(release_snapshot),
            schedule_enabled=bool(body.get("schedule_enabled") or False),
            schedule_type=body.get("schedule_type") or "daily",
            run_time=body.get("run_time") or "02:00",
            day_of_month=body.get("day_of_month"),
            day_of_week=body.get("day_of_week"),
            interval_minutes=body.get("interval_minutes"),
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            updated_by_username=actor.username if actor else None,
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return _version_to_response(version)
    finally:
        session.close()


def list_versions(workflow_id: uuid.UUID | None = None) -> list[DataPlatformVersionResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataPlatformWorkflowVersion).order_by(desc(DataPlatformWorkflowVersion.updated_at)).limit(300)
        if workflow_id:
            stmt = stmt.where(DataPlatformWorkflowVersion.workflow_id == workflow_id)
        rows = session.execute(stmt).scalars().all()
        return [_version_to_response(row) for row in rows]
    finally:
        session.close()


def update_version(version_id: uuid.UUID, updates: dict[str, Any], actor: AuthContext | None) -> DataPlatformVersionResponse:
    session = get_sync_session_factory()()
    try:
        version = session.get(DataPlatformWorkflowVersion, version_id)
        if not version:
            raise KeyError("Workflow version does not exist.")
        requested_fields = {field for field, value in updates.items() if value is not None}
        requested_design_fields = requested_fields & _VERSION_DESIGN_FIELDS
        requested_schedule_fields = requested_fields & _VERSION_SCHEDULE_FIELDS
        if requested_design_fields and not (actor and actor.has_permission("dataPlatform:design")):
            raise PermissionError("Permission required: dataPlatform:design")
        if requested_schedule_fields and not (
            actor and (actor.has_permission("dataPlatform:design") or actor.has_permission("dataPlatform:publish"))
        ):
            raise PermissionError("Permission required: dataPlatform:publish")
        if version.status == "online" and requested_fields - _VERSION_SCHEDULE_FIELDS:
            raise ValueError("上线版本允许修改调度规则，但流程节点和连线必须从开发版重新提交。")
        for field in _VERSION_SCHEDULE_FIELDS:
            if field in updates and updates[field] is not None:
                setattr(version, field, updates[field])
        if version.status == "online":
            version.next_run_at = _next_schedule_time(version, app_now()) if version.schedule_enabled else None
        if "nodes" in updates and updates["nodes"] is not None:
            version.nodes = _freeze_component_task_nodes(session, _normalize_nodes(updates["nodes"]), preserve_existing=True)
        if "edges" in updates and updates["edges"] is not None:
            version.edges = _normalize_edges(updates["edges"])
        if "business_metadata" in updates and updates["business_metadata"] is not None:
            version.business_metadata = deepcopy(updates["business_metadata"])
        version.release_snapshot = _build_release_snapshot(version.nodes or [], version.edges or [], {**_version_schedule_payload(version), "business_metadata": version.business_metadata or {}})
        version.execution_content_hash = _execution_content_hash(version.release_snapshot)
        version.updated_by_username = actor.username if actor else None
        version.updated_at = app_now()
        session.commit()
        session.refresh(version)
        return _version_to_response(version)
    finally:
        session.close()


def submit_version(version_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformVersionResponse:
    session = get_sync_session_factory()()
    try:
        source = session.get(DataPlatformWorkflowVersion, version_id)
        if not source:
            raise KeyError("Workflow version does not exist.")
        frozen_nodes = _freeze_component_task_nodes(session, source.nodes or [], preserve_existing=True)
        _validate_component_task_bindings(frozen_nodes)
        release_snapshot = _build_release_snapshot(frozen_nodes, source.edges or [], {**_version_schedule_payload(source), "business_metadata": source.business_metadata or {}})
        content_hash = _execution_content_hash(release_snapshot)
        existing = session.execute(
            select(DataPlatformWorkflowVersion)
            .where(
                DataPlatformWorkflowVersion.workflow_id == source.workflow_id,
                DataPlatformWorkflowVersion.channel == "prod",
                DataPlatformWorkflowVersion.execution_content_hash == content_hash,
            )
            .order_by(desc(DataPlatformWorkflowVersion.version_no))
            .limit(1)
        ).scalar_one_or_none()
        if existing:
            return _version_to_response(existing)
        version = DataPlatformWorkflowVersion(
            id=uuid.uuid4(),
            workflow_id=source.workflow_id,
            version_no=_next_version_no(session, source.workflow_id, "prod"),
            channel="prod",
            status="submitted",
            nodes=frozen_nodes,
            edges=source.edges or [],
            business_metadata=deepcopy(source.business_metadata or {}),
            release_snapshot=release_snapshot,
            execution_content_hash=content_hash,
            schedule_enabled=source.schedule_enabled,
            schedule_type=source.schedule_type,
            run_time=source.run_time,
            day_of_month=source.day_of_month,
            day_of_week=source.day_of_week,
            interval_minutes=source.interval_minutes,
            submitted_at=app_now(),
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            updated_by_username=actor.username if actor else None,
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return _version_to_response(version)
    finally:
        session.close()


def publish_version(version_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformVersionResponse:
    session = get_sync_session_factory()()
    try:
        version = session.get(DataPlatformWorkflowVersion, version_id)
        if not version:
            raise KeyError("Workflow version does not exist.")
        if version.channel != "prod":
            raise ValueError("Only submitted prod versions can be published.")
        if not version.nodes:
            raise ValueError("A workflow needs at least one node before publish.")
        _validate_component_task_bindings(version.nodes or [])
        now = app_now()
        online_rows = session.execute(
            select(DataPlatformWorkflowVersion).where(
                DataPlatformWorkflowVersion.workflow_id == version.workflow_id,
                DataPlatformWorkflowVersion.status == "online",
                DataPlatformWorkflowVersion.id != version.id,
            )
        ).scalars().all()
        for row in online_rows:
            row.status = "offline"
            row.schedule_enabled = False
            row.next_run_at = None
            row.offline_at = now
            row.updated_at = now
            retire_change_triggers(session, row.id)
        version.status = "online"
        version.published_at = now
        version.offline_at = None
        version.updated_by_username = actor.username if actor else None
        version.updated_at = now
        version.next_run_at = _next_schedule_time(version, now) if version.schedule_enabled else None
        synchronize_change_triggers(session, version)
        session.commit()
        session.refresh(version)
        return _version_to_response(version)
    finally:
        session.close()


def offline_version(version_id: uuid.UUID, actor: AuthContext | None) -> DataPlatformVersionResponse:
    session = get_sync_session_factory()()
    try:
        version = session.get(DataPlatformWorkflowVersion, version_id)
        if not version:
            raise KeyError("Workflow version does not exist.")
        version.status = "offline"
        version.schedule_enabled = False
        version.next_run_at = None
        retire_change_triggers(session, version.id)
        version.offline_at = app_now()
        version.updated_by_username = actor.username if actor else None
        version.updated_at = app_now()
        session.commit()
        session.refresh(version)
        return _version_to_response(version)
    finally:
        session.close()


def run_version(
    version_id: uuid.UUID,
    *,
    trigger_type: str = "manual",
    actor: AuthContext | None = None,
    trigger_context: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
) -> DataPlatformRunResponse:
    session = get_sync_session_factory()()
    try:
        version = session.get(DataPlatformWorkflowVersion, version_id)
        if not version:
            raise KeyError("Workflow version does not exist.")
        if version.status not in {"draft", "submitted", "online"}:
            raise ValueError("This version is offline and cannot run.")
        _validate_component_task_bindings(version.nodes or [])
        if _has_active_workflow_run(session, version.id):
            raise ValueError("This workflow version already has an active run. Please wait for it to finish before submitting again.")
        run = DataPlatformWorkflowRun(
            id=run_id or uuid.uuid4(),
            workflow_id=version.workflow_id,
            version_id=version.id,
            version_no=version.version_no,
            channel=version.channel,
            trigger_type=trigger_type,
            trigger_context=trigger_context or None,
            status="queued",
            message="Waiting for workflow executor.",
            total_count=len(version.nodes or []),
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        try:
            _enqueue_workflow_run(run.id)
        except Exception as exc:
            now = app_now()
            run.status = "failed"
            run.message = f"Failed to submit workflow executor task: {exc}"
            run.failed_count = max(run.failed_count or 0, 1)
            run.finished_at = now
            run.updated_at = now
            finalize_change_trigger_run(session, run)
            session.commit()
            raise RuntimeError(run.message) from exc
        return _run_to_response(run)
    finally:
        session.close()


def _enqueue_workflow_run(run_id: uuid.UUID) -> str | None:
    from recovery_service.settings import get_settings
    from recovery_service.workers.celery_app import celery_app

    result = celery_app.send_task(
        "data_platform.workflow_run",
        args=[str(run_id)],
        queue=get_settings().celery_data_platform_queue,
    )
    return getattr(result, "id", None)


def run_queued_workflow(run_id: uuid.UUID) -> None:
    _execute_run(run_id)


def list_runs(workflow_id: uuid.UUID | None = None, limit: int = 100) -> list[DataPlatformRunResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataPlatformWorkflowRun).order_by(desc(DataPlatformWorkflowRun.created_at)).limit(limit)
        if workflow_id:
            stmt = stmt.where(DataPlatformWorkflowRun.workflow_id == workflow_id)
        rows = session.execute(stmt).scalars().all()
        return [_run_to_response(row) for row in rows]
    finally:
        session.close()


def list_node_runs(run_id: uuid.UUID) -> list[DataPlatformNodeRunResponse]:
    session = get_sync_session_factory()()
    try:
        rows = session.execute(
            select(DataPlatformNodeRun).where(DataPlatformNodeRun.run_id == run_id).order_by(DataPlatformNodeRun.created_at)
        ).scalars().all()
        return [_node_run_to_response(row) for row in rows]
    finally:
        session.close()


def list_component_runs(node_id: uuid.UUID, limit: int = 20) -> list[DataPlatformComponentRunResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = (
            select(DataPlatformComponentRun)
            .where(DataPlatformComponentRun.node_id == node_id)
            .order_by(desc(DataPlatformComponentRun.created_at))
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        run_ids = [row.id for row in rows]
        table_map: dict[uuid.UUID, list[DataPlatformComponentRunTable]] = {run_id: [] for run_id in run_ids}
        if run_ids:
            table_rows = session.execute(
                select(DataPlatformComponentRunTable)
                .where(DataPlatformComponentRunTable.component_run_id.in_(run_ids))
                .order_by(DataPlatformComponentRunTable.created_at, DataPlatformComponentRunTable.id)
            ).scalars().all()
            for table_row in table_rows:
                table_map.setdefault(table_row.component_run_id, []).append(table_row)
        for row in rows:
            setattr(row, "_table_runs", table_map.get(row.id, []))
        return [_component_run_to_response(row) for row in rows]
    finally:
        session.close()


def list_component_run_tables(
    node_id: uuid.UUID,
    component_run_id: uuid.UUID,
) -> list[DataPlatformComponentRunTableResponse]:
    session = get_sync_session_factory()()
    try:
        run = session.get(DataPlatformComponentRun, component_run_id)
        if not run or run.node_id != node_id:
            raise KeyError("Component run does not exist.")
        rows = session.execute(
            select(DataPlatformComponentRunTable)
            .where(DataPlatformComponentRunTable.component_run_id == component_run_id)
            .order_by(DataPlatformComponentRunTable.created_at, DataPlatformComponentRunTable.id)
        ).scalars().all()
        return [_component_run_table_to_response(row) for row in rows]
    finally:
        session.close()


def list_component_run_table_logs(
    node_id: uuid.UUID,
    component_run_id: uuid.UUID,
    table_run_id: uuid.UUID,
    limit: int = 500,
) -> list[DataPlatformComponentRunLogResponse]:
    session = get_sync_session_factory()()
    try:
        run = session.get(DataPlatformComponentRun, component_run_id)
        table = session.get(DataPlatformComponentRunTable, table_run_id)
        if not run or run.node_id != node_id or not table or table.component_run_id != component_run_id:
            raise KeyError("Component run table does not exist.")
        rows = session.execute(
            select(DataPlatformComponentRunLog)
            .where(
                DataPlatformComponentRunLog.component_run_id == component_run_id,
                DataPlatformComponentRunLog.table_run_id == table_run_id,
            )
            .order_by(DataPlatformComponentRunLog.created_at, DataPlatformComponentRunLog.id)
            .limit(limit)
        ).scalars().all()
        return [_component_run_log_to_response(row) for row in rows]
    finally:
        session.close()


def start_data_platform_scheduler() -> None:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(target=_scheduler_loop, daemon=True)
        _SCHEDULER_THREAD.start()


def stop_data_platform_scheduler() -> None:
    _SCHEDULER_STOP.set()
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        _SCHEDULER_THREAD.join(timeout=5)


def _scheduler_loop() -> None:
    while not _SCHEDULER_STOP.is_set():
        try:
            _run_due_versions()
        except Exception as exc:
            logger.error("data platform time scheduler tick failed", error=str(exc), exc_info=True)
        try:
            run_due_change_triggers(run_version)
        except Exception as exc:
            logger.error("data platform change scheduler tick failed", error=str(exc), exc_info=True)
        _SCHEDULER_STOP.wait(30)


def _mark_interrupted_runs_on_startup() -> None:
    now = app_now()
    session = get_sync_session_factory()()
    try:
        runs = session.execute(
            select(DataPlatformWorkflowRun).where(DataPlatformWorkflowRun.status.in_(["queued", "running"]))
        ).scalars().all()
        for run in runs:
            run.status = "failed"
            run.message = "Workflow run was interrupted by service restart; please submit it again."
            run.failed_count = max(run.failed_count or 0, 1)
            run.finished_at = now
            run.updated_at = now
            finalize_change_trigger_run(session, run)
        nodes = session.execute(
            select(DataPlatformNodeRun).where(DataPlatformNodeRun.status.in_(["queued", "running"]))
        ).scalars().all()
        for node in nodes:
            node.status = "failed"
            node.message = "Node run was interrupted by service restart; please submit it again."
            node.result = _node_error_result(node.result, "interrupted by service restart")
            node.finished_at = now
            node.updated_at = now
        session.commit()
    finally:
        session.close()


def list_schedules(*, include_disabled: bool = True) -> list[DataPlatformScheduleResponse]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataPlatformWorkflowVersion).where(
            DataPlatformWorkflowVersion.channel == "prod",
            DataPlatformWorkflowVersion.status == "online",
        )
        if not include_disabled:
            stmt = stmt.where(DataPlatformWorkflowVersion.schedule_enabled == True)  # noqa: E712
        versions = session.execute(
            stmt.order_by(
                desc(DataPlatformWorkflowVersion.schedule_enabled),
                DataPlatformWorkflowVersion.next_run_at,
                desc(DataPlatformWorkflowVersion.updated_at),
            ).limit(300)
        ).scalars().all()
        if not versions:
            return []

        workflow_ids = {version.workflow_id for version in versions}
        workflows = {
            row.id: row
            for row in session.execute(
                select(DataPlatformWorkflow).where(
                    DataPlatformWorkflow.id.in_(workflow_ids),
                    DataPlatformWorkflow.status != "archived",
                )
            ).scalars().all()
        }
        folders = {
            row.id: row
            for row in session.execute(
                select(DataPlatformFolder).where(DataPlatformFolder.status != "archived")
            ).scalars().all()
        }
        version_ids = {version.id for version in versions}
        latest_runs: dict[uuid.UUID, DataPlatformWorkflowRun] = {}
        runs = session.execute(
            select(DataPlatformWorkflowRun)
            .where(DataPlatformWorkflowRun.version_id.in_(version_ids))
            .order_by(desc(DataPlatformWorkflowRun.created_at))
        ).scalars().all()
        for run in runs:
            latest_runs.setdefault(run.version_id, run)

        result: list[DataPlatformScheduleResponse] = []
        for version in versions:
            workflow = workflows.get(version.workflow_id)
            if not workflow:
                continue
            latest_run = latest_runs.get(version.id)
            if not version.schedule_enabled:
                schedule_state = "disabled"
            elif latest_run and latest_run.status in {"queued", "running"}:
                schedule_state = latest_run.status
            elif not version.next_run_at:
                schedule_state = "abnormal"
            else:
                schedule_state = "waiting"
            result.append(
                DataPlatformScheduleResponse(
                    version_id=version.id,
                    workflow_id=workflow.id,
                    workflow_name=workflow.name,
                    folder_id=workflow.folder_id,
                    folder_path=_folder_path(folders, workflow.folder_id),
                    version_no=version.version_no,
                    version_status=version.status,  # type: ignore[arg-type]
                    schedule_enabled=version.schedule_enabled,
                    schedule_state=schedule_state,  # type: ignore[arg-type]
                    schedule_type=version.schedule_type,  # type: ignore[arg-type]
                    run_time=version.run_time,
                    day_of_month=version.day_of_month,
                    day_of_week=version.day_of_week,
                    interval_minutes=version.interval_minutes,
                    last_run_at=version.last_run_at,
                    next_run_at=version.next_run_at,
                    latest_run_id=latest_run.id if latest_run else None,
                    latest_run_status=latest_run.status if latest_run else None,  # type: ignore[arg-type]
                    latest_run_trigger_type=latest_run.trigger_type if latest_run else None,
                    latest_run_created_at=latest_run.created_at if latest_run else None,
                    latest_run_finished_at=latest_run.finished_at if latest_run else None,
                    updated_by_username=version.updated_by_username,
                    updated_at=version.updated_at,
                )
            )
        return result
    finally:
        session.close()


def _run_due_versions() -> None:
    now = app_now()
    session = get_sync_session_factory()()
    due: list[uuid.UUID] = []
    try:
        rows = session.execute(
            select(DataPlatformWorkflowVersion)
            .where(
                DataPlatformWorkflowVersion.channel == "prod",
                DataPlatformWorkflowVersion.status == "online",
                DataPlatformWorkflowVersion.schedule_enabled == True,  # noqa: E712
                DataPlatformWorkflowVersion.next_run_at <= now,
            )
            .order_by(DataPlatformWorkflowVersion.next_run_at)
            .limit(20)
        ).scalars().all()
        for version in rows:
            if _has_active_workflow_run(session, version.id):
                version.next_run_at = _next_schedule_time(version, now)
                version.updated_at = now
                continue
            due.append(version.id)
            version.last_run_at = now
            version.next_run_at = _next_schedule_time(version, now)
            version.updated_at = now
        session.commit()
    finally:
        session.close()
    for version_id in due:
        run_version(version_id, trigger_type="schedule", actor=None)


def _has_active_workflow_run(session, version_id: uuid.UUID) -> bool:
    existing = session.scalar(
        select(func.count())
        .select_from(DataPlatformWorkflowRun)
        .where(
            DataPlatformWorkflowRun.version_id == version_id,
            DataPlatformWorkflowRun.status.in_(["queued", "running"]),
        )
    )
    return bool(existing)


def _execute_run(run_id: uuid.UUID) -> None:
    session = get_sync_session_factory()()
    try:
        run = session.get(DataPlatformWorkflowRun, run_id)
        if not run:
            return
        if run.status != "queued":
            logger.warning(
                "data platform workflow task ignored because run is not queued",
                run_id=str(run_id),
                status=run.status,
            )
            return
        version = session.get(DataPlatformWorkflowVersion, run.version_id)
        if not version:
            run.status = "failed"
            run.message = "Workflow version no longer exists."
            run.finished_at = app_now()
            session.commit()
            return
        release = version.release_snapshot or {}
        if (run.trigger_context or {}).get("assistant_plan_id"):
            from recovery_service.services.assistant_execution import release_for_run
            release = release_for_run(session, run, release)
        node_specs = _normalize_nodes(release.get("nodes") or version.nodes or [])
        edge_specs = _normalize_edges(release.get("edges") or version.edges or [])
        order = _topological_order(node_specs, edge_specs)
        upstream = _upstream_map(edge_specs)
        node_types = {str(item["key"]): str(item.get("node_type") or "manual") for item in node_specs}
        selected_nodes: set[str] | None = None
        if run.trigger_type == "data_change":
            selected_trigger = str((run.trigger_context or {}).get("node_key") or "")
            selected_nodes = _reachable_nodes(selected_trigger, edge_specs)
        run.status = "running"
        run.message = "Workflow is running."
        run.started_at = app_now()
        run.updated_at = app_now()
        session.commit()
        results: dict[str, str] = {}
        for spec in order:
            key = str(spec["key"])
            node_run = DataPlatformNodeRun(
                id=uuid.uuid4(),
                run_id=run.id,
                workflow_id=run.workflow_id,
                version_id=run.version_id,
                node_key=key,
                node_name=spec.get("name") or key,
                node_type=spec.get("node_type") or "manual",
                status="queued",
                upstream_keys=upstream.get(key, []),
                created_at=app_now(),
                updated_at=app_now(),
            )
            session.add(node_run)
            session.commit()
            if selected_nodes is not None and key not in selected_nodes:
                node_run.status = "skipped"
                node_run.message = "Skipped because this node is outside the selected change-trigger branch."
                node_run.finished_at = app_now()
                node_run.updated_at = app_now()
                results[key] = "skipped"
                session.commit()
                continue
            if run.trigger_type in {"data_change", "trigger_test"} and spec.get("node_type") == "change_trigger":
                if run.trigger_type == "trigger_test":
                    node_run.status = "succeeded"
                    node_run.message = "触发器试运行已跳过变化判断，从内部流程开始执行。"
                    node_run.result = {"dry_run": True}
                    node_run.finished_at = app_now()
                    node_run.updated_at = app_now()
                    results[key] = "succeeded"
                    session.commit()
                    continue
                selected_trigger = str((run.trigger_context or {}).get("node_key") or "")
                if key != selected_trigger:
                    node_run.status = "skipped"
                    node_run.message = "Skipped because another change trigger started this run."
                    node_run.finished_at = app_now()
                    node_run.updated_at = app_now()
                    results[key] = "skipped"
                    session.commit()
                    continue
            selected_trigger = str((run.trigger_context or {}).get("node_key") or "")
            blocked = [
                item
                for item in upstream.get(key, [])
                if results.get(item) != "succeeded"
                and not (
                    run.trigger_type == "data_change"
                    and node_types.get(item) == "change_trigger"
                    and item != selected_trigger
                )
            ]
            if blocked:
                node_run.status = "skipped"
                node_run.message = "Skipped because upstream node did not succeed."
                node_run.finished_at = app_now()
                node_run.updated_at = app_now()
                results[key] = "skipped"
                session.commit()
                continue
            node_run.status = "running"
            node_run.message = "Running node."
            node_run.started_at = app_now()
            node_run.updated_at = app_now()
            session.commit()
            try:
                result = _execute_node(session, spec, node_run=node_run, run=run)
                node_run.status = "succeeded"
                node_run.message = result.get("message") or "Node finished."
                node_run.result = result
                results[key] = "succeeded"
            except Exception as exc:
                node_run.status = "failed"
                node_run.message = str(exc)
                node_run.result = _node_error_result(node_run.result, str(exc))
                results[key] = "failed"
            node_run.finished_at = app_now()
            node_run.updated_at = app_now()
            session.commit()
        run = session.get(DataPlatformWorkflowRun, run_id)
        if not run:
            return
        run.success_count = sum(1 for item in results.values() if item == "succeeded")
        run.failed_count = sum(1 for item in results.values() if item == "failed")
        run.skipped_count = sum(1 for item in results.values() if item == "skipped")
        if run.failed_count:
            run.status = "failed" if not run.success_count else "partial"
        elif run.skipped_count and run.trigger_type != "data_change":
            run.status = "partial"
        else:
            run.status = "succeeded"
        run.message = f"Finished: success {run.success_count}, failed {run.failed_count}, skipped {run.skipped_count}."
        run.finished_at = app_now()
        run.updated_at = app_now()
        finalize_change_trigger_run(session, run)
        session.commit()
    except Exception as exc:
        session.rollback()
        now = app_now()
        try:
            run = session.get(DataPlatformWorkflowRun, run_id)
            if run:
                run.status = "failed"
                run.message = f"Workflow executor failed: {exc}"
                run.failed_count = max(run.failed_count or 0, 1)
                run.finished_at = now
                run.updated_at = now
                active_nodes = session.execute(
                    select(DataPlatformNodeRun).where(
                        DataPlatformNodeRun.run_id == run_id,
                        DataPlatformNodeRun.status.in_(["queued", "running"]),
                    )
                ).scalars().all()
                for node in active_nodes:
                    node.status = "failed"
                    node.message = f"Workflow executor failed: {exc}"
                    node.result = _node_error_result(node.result, str(exc))
                    node.finished_at = now
                    node.updated_at = now
                finalize_change_trigger_run(session, run)
                session.commit()
        except Exception:
            session.rollback()
    finally:
        session.close()


def _execute_node(
    session,
    spec: dict[str, Any],
    *,
    node_run: DataPlatformNodeRun | None = None,
    run: DataPlatformWorkflowRun | None = None,
) -> dict[str, Any]:
    node_type = spec.get("node_type") or "manual"
    config = dict(spec.get("config") or {})
    if node_type == "manual":
        return {"message": "Manual/check node completed."}
    if node_type == "change_trigger":
        return {"message": "数据变化触发条件已满足，本节点作为触发入口完成。"}
    if node_type == "data_sync":
        source_id = config.get("source_connection_id")
        target_id = config.get("target_connection_id")
        if not source_id or not target_id:
            raise ValueError("数据同步节点必须配置源连接和目标连接。")
        source = session.get(DatabaseConnectionProfile, uuid.UUID(str(source_id)))
        target = session.get(DatabaseConnectionProfile, uuid.UUID(str(target_id)))
        if not source or not target:
            raise ValueError("数据同步节点的源连接或目标连接不存在。")
        runtime_config = deepcopy(config)
        context = dict(run.trigger_context or {}) if run else {}
        watermark_condition_id = runtime_config.get("watermark_condition_id")
        if watermark_condition_id:
            applied = context.get("applied_value") or {}
            runtime_config["watermark_value"] = applied.get(str(watermark_condition_id))
        elif runtime_config.get("watermark_column"):
            applied = context.get("applied_value") or {}
            if applied:
                runtime_config["watermark_value"] = (
                    applied.get("condition_1")
                    if "condition_1" in applied
                    else next(iter(applied.values()))
                )
        return execute_data_sync(source, target, runtime_config)
    if node_type == "sm3_mapping":
        task_definition_id = config.get("task_definition_id")
        if not task_definition_id:
            raise ValueError("SM3 node needs a saved task definition.")
        snapshot = config.get("task_definition_snapshot")
        task = (
            run_sm3_task_snapshot_sync(snapshot, actor=None)
            if isinstance(snapshot, dict) and snapshot
            else run_sm3_task_definition_sync(uuid.UUID(str(task_definition_id)), actor=None)
        )
        final_status = _wait_for_sm3_task(task.task_id, timeout_seconds=_sm4_wait_timeout(config))
        if final_status.state != "succeeded":
            raise ValueError(f"SM3 task {final_status.task_id} finished with state {final_status.state}: {final_status.message}")
        return {
            "message": final_status.message,
            "task_id": str(final_status.task_id),
            "state": final_status.state,
        }
    if node_type == "sm4_batch":
        task_definition_id = config.get("task_definition_id")
        if task_definition_id:
            snapshot = config.get("task_definition_snapshot")
            task = (
                run_sm4_task_snapshot(snapshot, actor=None)
                if isinstance(snapshot, dict) and snapshot
                else run_sm4_task_definition(uuid.UUID(str(task_definition_id)), actor=None)
            )
            _record_node_batch_link(session, node_run, task.batch_id, task.state, "SM4 batch submitted and waiting for completion.")
            dispatch_queued_sm4_jobs_once()
            final_status = _wait_for_sm4_batch(task.batch_id, timeout_seconds=_sm4_wait_timeout(config))
            if final_status.state != "succeeded":
                raise ValueError(
                    f"SM4 batch {final_status.batch_id} finished with state {final_status.state}: {final_status.message}"
                )
            return {
                "message": final_status.message,
                "batch_id": str(final_status.batch_id),
                "state": final_status.state,
                "total_count": final_status.total_count,
                "success_count": final_status.success_count,
                "failed_count": final_status.failed_count,
            }
        connection_id = config.get("connection_id")
        database = (config.get("database") or "").strip()
        tables = config.get("tables") or []
        if not connection_id or not database or not tables:
            raise ValueError("SM4 node needs connection_id, database and tables.")
        profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(connection_id)))
        if not profile:
            raise ValueError("SM4 node connection does not exist.")
        task = create_sm4_batch_task(
            profile,
            database=database,
            tables=tables,
            table_strategy=config.get("table_strategy") or "drop_recreate",
            target_suffix=config.get("target_suffix"),
            actor=None,
        )
        _record_node_batch_link(session, node_run, task.batch_id, task.state, "SM4 batch submitted and waiting for completion.")
        dispatch_queued_sm4_jobs_once()
        final_status = _wait_for_sm4_batch(task.batch_id, timeout_seconds=_sm4_wait_timeout(config))
        if final_status.state != "succeeded":
            raise ValueError(f"SM4 batch {final_status.batch_id} finished with state {final_status.state}: {final_status.message}")
        return {
            "message": final_status.message,
            "batch_id": str(final_status.batch_id),
            "state": final_status.state,
            "total_count": final_status.total_count,
            "success_count": final_status.success_count,
            "failed_count": final_status.failed_count,
        }
    if node_type == "doris_sql":
        connection_id = config.get("connection_id")
        sql = (config.get("sql") or "").strip()
        database = (config.get("database") or "").strip() or None
        if not connection_id or not sql:
            raise ValueError("Doris SQL node needs connection_id and sql.")
        try:
            limit = max(1, min(5000, int(config.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        profile = session.get(DatabaseConnectionProfile, uuid.UUID(str(connection_id)))
        if not profile:
            raise ValueError("Doris SQL node connection does not exist.")
        result = execute_doris_sql(
            profile,
            database=database,
            sql=sql,
            limit=limit,
        )
        columns = [
            column.model_dump() if hasattr(column, "model_dump") else dict(column)
            for column in (result.columns or [])
        ]
        return {
            "message": result.message,
            "sql_type": result.sql_type,
            "row_count": result.row_count,
            "affected_rows": result.affected_rows,
            "duration_ms": result.duration_ms,
            "columns": columns,
            "rows": (result.rows or [])[:50],
        }
    raise ValueError(f"Unsupported node type: {node_type}")


def _record_node_batch_link(
    session,
    node_run: DataPlatformNodeRun | None,
    batch_id: uuid.UUID,
    state: str,
    message: str,
) -> None:
    if not node_run:
        return
    node_run.result = {
        **(node_run.result or {}),
        "batch_id": str(batch_id),
        "state": state,
        "message": message,
    }
    node_run.message = message
    node_run.updated_at = app_now()
    session.commit()


def _node_error_result(existing: dict[str, Any] | None, error: str) -> dict[str, Any]:
    return {**(existing or {}), "error": error}


def _sm4_wait_timeout(config: dict[str, Any]) -> int:
    try:
        return max(60, int(config.get("wait_timeout_seconds") or 24 * 60 * 60))
    except (TypeError, ValueError):
        return 24 * 60 * 60


def _wait_for_sm3_task(task_id: uuid.UUID, *, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = get_sm3_task_status_sync(task_id)
        if status.state in {"succeeded", "failed", "cancelled"}:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"SM3 task {task_id} did not finish within {timeout_seconds} seconds.")
        time.sleep(2)


def _wait_for_sm4_batch(batch_id: uuid.UUID, *, timeout_seconds: int) -> Any:
    deadline = time.monotonic() + timeout_seconds
    terminal_states = {"succeeded", "partial", "failed", "stopped", "cancelled"}
    while True:
        status = get_sm4_batch_task(batch_id)
        if status.state in terminal_states:
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"SM4 batch {batch_id} did not finish within {timeout_seconds} seconds.")
        time.sleep(5)


def _topological_order(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {str(item["key"]): item for item in nodes}
    incoming = {key: set() for key in by_key}
    outgoing: dict[str, set[str]] = {key: set() for key in by_key}
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        if source in by_key and target in by_key and source != target:
            incoming[target].add(source)
            outgoing[source].add(target)
    ready = [key for key, deps in incoming.items() if not deps]
    ordered: list[dict[str, Any]] = []
    while ready:
        key = ready.pop(0)
        ordered.append(by_key[key])
        for target in sorted(outgoing.get(key, [])):
            incoming[target].discard(key)
            if not incoming[target] and target not in ready and by_key[target] not in ordered:
                ready.append(target)
    if len(ordered) != len(nodes):
        raise ValueError("Workflow edges contain a cycle.")
    return ordered


def _upstream_map(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for edge in edges:
        result.setdefault(str(edge.get("target")), []).append(str(edge.get("source")))
    return result


def _normalize_nodes(items: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        value = item.model_dump() if hasattr(item, "model_dump") else dict(item or {})
        key = str(value.get("key") or f"node_{index + 1}").strip()
        if not key or key in seen:
            raise ValueError("Workflow node keys must be unique.")
        seen.add(key)
        result.append(
            {
                "key": key,
                "node_id": str(value["node_id"]) if value.get("node_id") else None,
                "name": (value.get("name") or key).strip(),
                "node_type": value.get("node_type") or "manual",
                "config": value.get("config") or {},
                "x": value.get("x"),
                "y": value.get("y"),
            }
        )
    return result


def _reachable_nodes(source: str, edges: list[dict[str, Any]]) -> set[str]:
    if not source:
        return set()
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(str(edge.get("source")), []).append(str(edge.get("target")))
    reached = {source}
    pending = [source]
    while pending:
        current = pending.pop()
        for target in outgoing.get(current, []):
            if target not in reached:
                reached.add(target)
                pending.append(target)
    return reached


def _freeze_component_task_nodes(
    session: Session,
    nodes: list[dict[str, Any]],
    *,
    preserve_existing: bool,
    tolerate_missing: bool = False,
) -> list[dict[str, Any]]:
    frozen = deepcopy(nodes or [])
    for node in frozen:
        node_type = node.get("node_type")
        if node_type not in {"sm3_mapping", "sm4_batch", *_COMPONENT_TASK_TYPES}:
            continue
        config = dict(node.get("config") or {})
        existing = config.get("task_definition_snapshot")
        if preserve_existing and isinstance(existing, dict) and existing:
            config["task_definition_revision"] = int(existing.get("revision") or 1)
            config["task_definition_name"] = existing.get("name") or config.get("task_definition_name")
            node["config"] = config
            continue
        task_id = config.get("task_definition_id")
        if not task_id:
            continue
        try:
            parsed_task_id = uuid.UUID(str(task_id))
        except (TypeError, ValueError):
            if tolerate_missing:
                continue
            raise ValueError(f"Invalid {node_type} task definition id: {task_id}")
        if node_type in _COMPONENT_TASK_TYPES:
            task = session.get(DataPlatformNode, parsed_task_id)
            if not task or task.status != "active" or task.node_type != node_type:
                if tolerate_missing:
                    continue
                raise ValueError(f"{node_type} task definition does not exist: {task_id}")
            snapshot = _component_task_snapshot(task)
        elif node_type == "sm4_batch":
            from recovery_service.core.models.task import DorisSm4TaskDefinition

            task = session.get(DorisSm4TaskDefinition, parsed_task_id)
            if not task or task.archived_at is not None:
                if tolerate_missing:
                    continue
                raise ValueError(f"SM4 task definition does not exist: {task_id}")
            snapshot = sm4_task_definition_snapshot(task)
        else:
            from recovery_service.core.models.task import DorisSm3TaskDefinition

            task = session.get(DorisSm3TaskDefinition, parsed_task_id)
            if not task or task.archived_at is not None:
                if tolerate_missing:
                    continue
                raise ValueError(f"SM3 task definition does not exist: {task_id}")
            snapshot = sm3_task_definition_snapshot(task)
        config["task_definition_name"] = snapshot["name"]
        config["task_definition_revision"] = snapshot["revision"]
        config["task_definition_snapshot"] = snapshot
        if node_type in _COMPONENT_TASK_TYPES:
            config = {
                **deepcopy(snapshot.get("config") or {}),
                "task_definition_id": str(task.id),
                "task_definition_name": snapshot["name"],
                "task_definition_revision": snapshot["revision"],
                "task_definition_snapshot": snapshot,
            }
        node["name"] = snapshot["name"]
        node["config"] = config
    return frozen


def _build_standalone_trigger_snapshot(session, node: DataPlatformNode) -> dict[str, Any]:
    config = _normalize_component_task_config("change_trigger", node.config or {})
    action_nodes = _normalize_nodes(config.get("action_nodes") or [])
    action_edges = _normalize_edges(config.get("action_edges") or [])
    if not action_nodes:
        raise ValueError("触发器内部流程至少需要一个执行节点。")
    allowed_types = {"sm3_mapping", "sm4_batch", "doris_sql", "data_sync"}
    invalid_types = sorted(
        {str(item.get("node_type") or "") for item in action_nodes if item.get("node_type") not in allowed_types}
    )
    if invalid_types:
        raise ValueError(f"触发器内部不支持以下节点类型：{', '.join(invalid_types)}。")
    action_edges = _standalone_trigger_edges(
        action_nodes,
        action_edges,
        graph_schema_version=int(config.get("graph_schema_version") or 1),
    )
    for item in action_nodes:
        if not (item.get("config") or {}).get("task_definition_id"):
            raise ValueError(f"触发器节点“{item.get('name') or item.get('key')}”尚未选择已保存任务。")
    frozen_actions = _freeze_component_task_nodes(
        session,
        action_nodes,
        preserve_existing=False,
    )
    monitor_config = {
        key: deepcopy(value)
        for key, value in config.items()
        if key not in {"action_nodes", "action_edges", "published_snapshot"}
        and not key.startswith("published_")
        and key != "system_workflow_id"
    }
    monitor_config["standalone_trigger"] = True
    monitor_config["standalone_deployment_monitor"] = True
    monitor_config["trigger_task_id"] = str(node.id)
    monitor_config["trigger_task_revision"] = int(node.revision or 1)
    monitor = {
        "key": "monitor",
        "node_id": str(node.id),
        "name": node.name,
        "node_type": "change_trigger",
        "config": monitor_config,
        "x": 60,
        "y": 180,
    }
    snapshot = {
        "nodes": [monitor, *frozen_actions],
        "edges": action_edges,
        "schedule": {
            "schedule_type": "interval",
            "run_time": "00:00",
            "day_of_month": None,
            "day_of_week": None,
            "interval_minutes": max(1, int(config.get("probe_interval_minutes") or 5)),
        },
    }
    _topological_order(snapshot["nodes"], snapshot["edges"])
    return snapshot


def _standalone_trigger_edges(
    action_nodes: list[dict[str, Any]],
    action_edges: list[dict[str, Any]],
    *,
    graph_schema_version: int,
) -> list[dict[str, Any]]:
    action_keys = {str(item["key"]) for item in action_nodes}
    edges = _normalize_edges(action_edges)
    if graph_schema_version < 2:
        action_only_edges = [
            edge
            for edge in edges
            if str(edge.get("source")) in action_keys and str(edge.get("target")) in action_keys
        ]
        incoming = {key: 0 for key in action_keys}
        for edge in action_only_edges:
            incoming[str(edge["target"])] += 1
        roots = [key for key, count in incoming.items() if count == 0]
        edges = [{"source": "monitor", "target": key} for key in roots] + action_only_edges
    _validate_standalone_trigger_action_graph(action_nodes, edges)
    return edges


def _validate_standalone_trigger_action_graph(
    action_nodes: list[dict[str, Any]],
    action_edges: list[dict[str, Any]],
) -> None:
    if not action_nodes:
        return
    action_keys = {str(item["key"]) for item in action_nodes}
    all_keys = {"monitor", *action_keys}
    invalid_edges = [
        edge
        for edge in action_edges
        if str(edge.get("source")) not in all_keys or str(edge.get("target")) not in all_keys
    ]
    if invalid_edges:
        raise ValueError("触发器内部连线引用了不存在的节点。")
    if any(str(edge.get("target")) == "monitor" for edge in action_edges):
        raise ValueError("监控节点必须作为流程首节点，不能连接上游节点。")
    if not any(str(edge.get("source")) == "monitor" for edge in action_edges):
        raise ValueError("监控节点至少需要连接一个下游执行节点。")
    edge_pairs = [
        (str(edge.get("source")), str(edge.get("target")))
        for edge in action_edges
    ]
    if len(edge_pairs) != len(set(edge_pairs)):
        raise ValueError("触发器内部存在重复连线。")
    graph_nodes = [{"key": "monitor"}, *action_nodes]
    try:
        _topological_order(graph_nodes, action_edges)
    except ValueError as exc:
        raise ValueError("触发器内部流程不能形成环路。") from exc
    reachable = _reachable_nodes("monitor", action_edges)
    orphan_keys = action_keys - reachable
    if orphan_keys:
        names = [
            str(item.get("name") or item.get("key"))
            for item in action_nodes
            if str(item.get("key")) in orphan_keys
        ]
        raise ValueError(f"存在游离节点：{', '.join(names)}，请连接到监控流程后再保存。")


def _ensure_trigger_system_workflow(
    session,
    node: DataPlatformNode,
    actor: AuthContext | None,
) -> DataPlatformWorkflow:
    config = dict(node.config or {})
    workflow = None
    if config.get("system_workflow_id"):
        workflow = session.get(DataPlatformWorkflow, uuid.UUID(str(config["system_workflow_id"])))
    if workflow is None:
        workflow = DataPlatformWorkflow(
            id=uuid.uuid4(),
            folder_id=None,
            name=f"[触发器] {node.name}",
            description=f"数据变化触发器 {node.id} 的系统执行流程",
            status="system",
            created_by_user_id=_actor_uuid(actor),
            created_by_username=actor.username if actor else None,
            created_at=app_now(),
            updated_at=app_now(),
        )
        session.add(workflow)
        session.flush()
        config["system_workflow_id"] = str(workflow.id)
        node.config = config
    else:
        workflow.name = f"[触发器] {node.name}"
        workflow.updated_at = app_now()
    return workflow


def _standalone_trigger_state(
    session,
    node: DataPlatformNode,
    *,
    required: bool = False,
) -> DataPlatformChangeTriggerState | None:
    version_id = (node.config or {}).get("published_workflow_version_id")
    if not version_id:
        if required:
            raise ValueError("触发器尚未发布。")
        return None
    state = session.execute(
        select(DataPlatformChangeTriggerState).where(
            DataPlatformChangeTriggerState.version_id == uuid.UUID(str(version_id)),
            DataPlatformChangeTriggerState.node_key == "monitor",
        )
    ).scalar_one_or_none()
    if required and state is None:
        raise ValueError("触发器发布状态不存在，请重新发布。")
    return state


def _standalone_trigger_status_payload(
    node: DataPlatformNode,
    state: DataPlatformChangeTriggerState | None,
) -> dict[str, Any]:
    config = dict(node.config or {})
    return {
        "task_id": str(node.id),
        "task_name": node.name,
        "draft_revision": int(node.revision or 1),
        "published_revision": config.get("published_revision"),
        "published_version_no": config.get("published_version_no"),
        "published_at": config.get("published_at"),
        "workflow_id": str(state.workflow_id) if state else config.get("system_workflow_id"),
        "workflow_version_id": str(state.version_id) if state else config.get("published_workflow_version_id"),
        "trigger_id": str(state.id) if state else None,
        "published": bool(state),
        "enabled": bool(state.enabled) if state else False,
        "state": state.state if state else "draft",
        "observed_value": state.observed_value if state else None,
        "pending_value": state.pending_value if state else None,
        "applied_value": state.applied_value if state else None,
        "pending_run_id": str(state.pending_run_id) if state and state.pending_run_id else None,
        "last_probe_at": state.last_probe_at if state else None,
        "next_probe_at": state.next_probe_at if state else None,
        "last_trigger_at": state.last_trigger_at if state else None,
        "last_success_at": state.last_success_at if state else None,
        "message": state.message if state else "尚未发布。",
    }


def _build_release_snapshot(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], schedule: dict[str, Any]) -> dict[str, Any]:
    _validate_workflow_graph(nodes, edges)
    _validate_change_trigger_graph(nodes, edges)
    return {
        "nodes": deepcopy(nodes or []),
        "edges": deepcopy(edges or []),
        "business_metadata": deepcopy(schedule.get("business_metadata") or {}),
        "schedule": {
            "schedule_type": schedule.get("schedule_type") or "daily",
            "run_time": schedule.get("run_time") or "02:00",
            "day_of_month": schedule.get("day_of_month"),
            "day_of_week": schedule.get("day_of_week"),
            "interval_minutes": schedule.get("interval_minutes"),
        },
    }


def _validate_workflow_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    node_keys = {str(item.get("key")) for item in nodes}
    invalid_edges = [
        edge
        for edge in edges
        if str(edge.get("source")) not in node_keys or str(edge.get("target")) not in node_keys
    ]
    if invalid_edges:
        raise ValueError("流程连线引用了不存在的节点。")
    if any(str(edge.get("source")) == str(edge.get("target")) for edge in edges):
        raise ValueError("流程节点不能连接自身。")
    edge_pairs = [(str(edge.get("source")), str(edge.get("target"))) for edge in edges]
    if len(edge_pairs) != len(set(edge_pairs)):
        raise ValueError("流程中存在重复连线。")
    if len(node_keys) > 1:
        standalone_keys = {
            str(item.get("key"))
            for item in nodes
            if item.get("node_type") == "change_trigger"
            and (
                (item.get("config") or {}).get("standalone_trigger")
                or (item.get("config") or {}).get("published_workflow_version_id")
                or ((item.get("config") or {}).get("task_definition_snapshot") or {}).get("config", {}).get("standalone_trigger")
            )
        }
        connected_keys = {key for pair in edge_pairs for key in pair}
        orphan_keys = node_keys - connected_keys - standalone_keys
        if orphan_keys:
            names = [
                str(item.get("name") or item.get("key"))
                for item in nodes
                if str(item.get("key")) in orphan_keys
            ]
            raise ValueError(f"存在游离节点：{', '.join(names)}，请完成连线后再保存。")
    try:
        _topological_order(nodes, edges)
    except ValueError as exc:
        raise ValueError("流程连线不能形成环路。") from exc


def _validate_change_trigger_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    trigger_keys = {str(item.get("key")) for item in nodes if item.get("node_type") == "change_trigger"}
    if not trigger_keys:
        return
    trigger_nodes = {str(item.get("key")): item for item in nodes if item.get("node_type") == "change_trigger"}
    incoming = {key: 0 for key in trigger_keys}
    outgoing = {key: 0 for key in trigger_keys}
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        if target in incoming:
            incoming[target] += 1
        if source in outgoing:
            outgoing[source] += 1
    standalone_keys = {
        key
        for key, item in trigger_nodes.items()
        if (item.get("config") or {}).get("standalone_trigger")
        or (item.get("config") or {}).get("published_workflow_version_id")
        or ((item.get("config") or {}).get("task_definition_snapshot") or {}).get("config", {}).get("standalone_trigger")
    }
    invalid_standalone = [
        key for key in standalone_keys if incoming.get(key, 0) or outgoing.get(key, 0)
    ]
    if invalid_standalone:
        raise ValueError(
            f"自包含数据变化触发器不能在离线画布连接外部上下游节点：{', '.join(invalid_standalone)}"
        )
    invalid_incoming = [key for key, count in incoming.items() if count and key not in standalone_keys]
    invalid_outgoing = [key for key, count in outgoing.items() if not count and key not in standalone_keys]
    if invalid_incoming:
        raise ValueError(f"数据变化触发器必须作为流程入口，不能有上游节点：{', '.join(invalid_incoming)}")
    if invalid_outgoing:
        raise ValueError(f"数据变化触发器至少需要连接一个下游执行组件：{', '.join(invalid_outgoing)}")


def _validate_component_task_bindings(nodes: list[dict[str, Any]]) -> None:
    unbound: list[str] = []
    for node in nodes or []:
        node_type = node.get("node_type")
        if node_type not in _COMPONENT_TASK_TYPES:
            continue
        config = dict(node.get("config") or {})
        if config.get("task_definition_id"):
            continue
        try:
            _normalize_component_task_config(str(node_type), config)
        except (TypeError, ValueError):
            unbound.append(str(node.get("name") or node.get("key") or node_type))
    if unbound:
        raise ValueError(f"以下离线节点尚未选择已保存任务：{', '.join(unbound)}。")


def _execution_content_hash(release_snapshot: dict[str, Any]) -> str:
    normalized = _canonical_execution_value(release_snapshot)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_execution_value(value: Any, key: str | None = None) -> Any:
    ignored_keys = {
        "x",
        "y",
        "name",
        "connection_name",
        "task_definition_name",
        "task_definition_id",
        "task_definition_revision",
        "revision",
        "node_id",
        "note",
        "sm4_key_version_id",
        "sm4_key_fingerprint",
        "jar_filename",
        "observed_value",
        "pending_value",
        "applied_value",
        "next_probe_at",
        "last_probe_at",
        "last_run_at",
        "next_run_at",
        "batch_id",
        "run_id",
    }
    if isinstance(value, dict):
        return {
            item_key: _canonical_execution_value(item_value, item_key)
            for item_key, item_value in sorted(value.items())
            if item_key not in ignored_keys
        }
    if isinstance(value, list):
        return [_canonical_execution_value(item, key) for item in value]
    return value


def _version_schedule_payload(version: DataPlatformWorkflowVersion) -> dict[str, Any]:
    return {
        "schedule_type": version.schedule_type,
        "run_time": version.run_time,
        "day_of_month": version.day_of_month,
        "day_of_week": version.day_of_week,
        "interval_minutes": version.interval_minutes,
    }


def _normalize_edges(items: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        value = item.model_dump() if hasattr(item, "model_dump") else dict(item or {})
        source = str(value.get("source") or "").strip()
        target = str(value.get("target") or "").strip()
        if source and target:
            result.append({"source": source, "target": target})
    return result


def _next_version_no(session, workflow_id: uuid.UUID, channel: str) -> int:
    current = session.scalar(
        select(func.max(DataPlatformWorkflowVersion.version_no)).where(
            DataPlatformWorkflowVersion.workflow_id == workflow_id,
            DataPlatformWorkflowVersion.channel == channel,
        )
    )
    return int(current or 0) + 1


def _next_schedule_time(version: DataPlatformWorkflowVersion, after: datetime) -> datetime:
    schedule_type = version.schedule_type or "daily"
    run_time = version.run_time or "02:00"
    hour, minute = _parse_time(run_time)
    if schedule_type == "interval":
        return after + timedelta(minutes=max(1, int(version.interval_minutes or 60)))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule_type == "daily":
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if schedule_type == "weekly":
        target = max(1, min(7, int(version.day_of_week or 1))) - 1
        days = (target - candidate.weekday()) % 7
        candidate += timedelta(days=days)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate
    day = max(1, min(31, int(version.day_of_month or 1)))
    year, month = candidate.year, candidate.month
    while True:
        last_day = monthrange(year, month)[1]
        candidate = candidate.replace(year=year, month=month, day=min(day, last_day))
        if candidate > after:
            return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1


def _parse_time(value: str) -> tuple[int, int]:
    parts = (value or "02:00").split(":", 1)
    hour = max(0, min(23, int(parts[0] or 2)))
    minute = max(0, min(59, int(parts[1] if len(parts) > 1 else 0)))
    return hour, minute


def _actor_uuid(actor: AuthContext | None) -> uuid.UUID | None:
    if not actor or not actor.user_id:
        return None
    try:
        return uuid.UUID(actor.user_id)
    except ValueError:
        return None


def _folder_path(folders: dict[uuid.UUID, DataPlatformFolder], folder_id: uuid.UUID | None) -> str | None:
    if not folder_id:
        return None
    names: list[str] = []
    visited: set[uuid.UUID] = set()
    current_id = folder_id
    while current_id and current_id not in visited:
        visited.add(current_id)
        folder = folders.get(current_id)
        if not folder:
            break
        names.append(folder.name)
        current_id = folder.parent_id
    return " / ".join(reversed(names)) or None


def _node_to_response(node: DataPlatformNode) -> DataPlatformNodeResponse:
    return DataPlatformNodeResponse(
        node_id=node.id,
        name=node.name,
        revision=int(node.revision or 1),
        node_type=node.node_type,  # type: ignore[arg-type]
        description=node.description,
        config=node.config or {},
        status=node.status,
        created_by_username=node.created_by_username,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def _component_task_snapshot(node: DataPlatformNode) -> dict[str, Any]:
    return {
        "task_definition_id": str(node.id),
        "revision": int(node.revision or 1),
        "name": node.name,
        "task_type": node.node_type,
        "description": node.description,
        "config": deepcopy(node.config or {}),
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


def _normalize_component_task_config(node_type: str, config: dict[str, Any]) -> dict[str, Any]:
    clean = deepcopy(config or {})
    if node_type not in _COMPONENT_TASK_TYPES:
        return clean
    if node_type == "doris_sql":
        if not clean.get("connection_id"):
            raise ValueError("Doris SQL 任务必须选择 Doris 连接。")
        if not str(clean.get("sql") or "").strip():
            raise ValueError("Doris SQL 任务必须填写 SQL。")
        clean["database"] = str(clean.get("database") or "").strip() or None
        clean["sql"] = str(clean.get("sql") or "").strip()
        clean["limit"] = max(1, min(5000, int(clean.get("limit") or 200)))
        clean.pop("confirm_dangerous", None)
        return clean
    if node_type == "data_sync":
        if clean.get("table_mappings") or clean.get("source_catalog") or clean.get("source_schema"):
            source_engine = str(clean.get("source_engine") or "doris").strip().lower()
            required = {
                "source_connection_id": "源连接",
                "source_catalog": "源 Catalog",
                "source_schema": "源 Schema",
                "target_database": "目标库",
            }
            if source_engine == "doris":
                clean["target_connection_id"] = clean.get("target_connection_id") or clean.get("source_connection_id")
            missing = [label for field, label in required.items() if not str(clean.get(field) or "").strip()]
            if not str(clean.get("target_connection_id") or "").strip():
                missing.append("目标 Doris 连接")
            if missing:
                raise ValueError(f"数据同步任务缺少：{', '.join(missing)}。")
            write_mode = str(clean.get("write_mode") or "append")
            if write_mode not in {"append", "truncate_insert"}:
                raise ValueError("数据同步写入策略只支持追加或清空后写入。")
            sync_method = str(clean.get("sync_method") or "auto").strip()
            if sync_method not in {"auto", "insert_select", "stream_load"}:
                raise ValueError("data sync method must be auto, insert_select, or stream_load.")
            if source_engine != "doris" and sync_method == "insert_select":
                raise ValueError("MySQL 源连接不能使用 Catalog 联邦查询，请选择 Stream Load 或自动选择。")
            schema_policy = str(clean.get("schema_policy") or "target").strip()
            if schema_policy not in {"source", "target"}:
                raise ValueError("data sync schema policy must be source or target.")
            mappings = list(clean.get("table_mappings") or [])
            if not mappings:
                raise ValueError("数据同步任务至少需要一条表映射。")
            clean["write_mode"] = write_mode
            clean["sync_method"] = sync_method
            clean["schema_policy"] = schema_policy
            clean["batch_size"] = max(100, min(50000, int(clean.get("batch_size") or 1000)))
            clean["field_delimiter"] = str(clean.get("field_delimiter") or ",")
            clean["line_delimiter"] = str(clean.get("line_delimiter") or "\\n")
            clean["max_filter_ratio"] = str(clean.get("max_filter_ratio") if clean.get("max_filter_ratio") is not None else "1")
            clean["strict_mode"] = bool(clean.get("strict_mode", False))
            clean["continue_on_error"] = bool(clean.get("continue_on_error", True))
            clean["table_parallelism"] = max(1, min(128, int(clean.get("table_parallelism") or 1)))
            clean["stream_load_http_port"] = int(clean.get("stream_load_http_port") or 8030)
            clean["stream_load_timeout_seconds"] = max(30, min(7200, int(clean.get("stream_load_timeout_seconds") or 300)))
            clean["table_mappings"] = mappings
            return clean
        required = {
            "source_connection_id": "源连接",
            "source_database": "源数据库",
            "source_table": "源表",
            "target_connection_id": "目标连接",
            "target_database": "目标数据库",
            "target_table": "目标表",
        }
        missing = [label for field, label in required.items() if not str(clean.get(field) or "").strip()]
        if missing:
            raise ValueError(f"数据同步任务缺少：{', '.join(missing)}。")
        write_mode = str(clean.get("write_mode") or "full_replace")
        if write_mode not in {"full_replace", "append", "incremental_append", "primary_key_merge"}:
            raise ValueError("数据同步写入模式不合法。")
        if write_mode in {"incremental_append", "primary_key_merge"} and not clean.get("watermark_column"):
            raise ValueError("增量同步任务必须配置水位字段。")
        clean["write_mode"] = write_mode
        clean["batch_size"] = max(100, min(10000, int(clean.get("batch_size") or 1000)))
        clean["column_mapping"] = list(clean.get("column_mapping") or [])
        clean["primary_keys"] = list(clean.get("primary_keys") or [])
        return clean
    conditions = list(clean.get("conditions") or [])
    if not conditions:
        raise ValueError("数据变化触发器至少需要一个探测条件。")
    source_type = str(clean.get("source_type") or "direct")
    if source_type == "direct" and not clean.get("connection_id"):
        raise ValueError("直接探测触发器必须选择 Doris 连接。")
    database = str(clean.get("database") or "").strip()
    if source_type == "direct" and not database:
        raise ValueError("直接探测触发器必须选择监控数据库。")
    normalized_conditions: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions):
        item = dict(condition or {})
        metric_type = str(item.get("metric_type") or "row_count").strip()
        if metric_type not in _CHANGE_TRIGGER_OPERATORS:
            raise ValueError(f"不支持的触发器判定指标：{metric_type}")
        operator = str(item.get("operator") or "changed").strip()
        if operator not in _CHANGE_TRIGGER_OPERATORS[metric_type]:
            raise ValueError(f"判定指标 {metric_type} 不支持比较规则 {operator}。")
        table = str(item.get("table") or "").strip()
        column = str(item.get("column") or "").strip()
        sql = str(item.get("sql") or "").strip()
        threshold = item.get("threshold")
        if metric_type != "scalar_sql" and not table:
            raise ValueError(f"判定指标 {metric_type} 必须选择监控表。")
        if metric_type in {"max", "min"} and not column:
            raise ValueError(f"判定指标 {metric_type} 必须选择指标字段。")
        if metric_type == "scalar_sql" and not sql:
            raise ValueError("标量 SQL 判定指标必须填写查询语句。")
        if operator in _CHANGE_TRIGGER_THRESHOLD_OPERATORS and (threshold is None or str(threshold).strip() == ""):
            raise ValueError(f"比较规则 {operator} 必须填写阈值。")
        item["id"] = str(item.get("id") or f"condition_{index + 1}")
        item["metric_type"] = metric_type
        item["operator"] = operator
        item["table"] = table or None
        item["column"] = column if metric_type in {"max", "min"} else None
        item["sql"] = sql if metric_type == "scalar_sql" else None
        item["threshold"] = threshold if operator in _CHANGE_TRIGGER_THRESHOLD_OPERATORS else None
        normalized_conditions.append(item)
    clean["source_type"] = source_type
    clean["database"] = database or None
    clean["conditions"] = normalized_conditions
    clean["probe_interval_minutes"] = max(1, int(clean.get("probe_interval_minutes") or 5))
    clean["consecutive_matches"] = max(1, int(clean.get("consecutive_matches") or 1))
    clean["minimum_trigger_interval_minutes"] = max(
        0, int(clean.get("minimum_trigger_interval_minutes") or 0)
    )
    clean["retry_interval_minutes"] = max(1, int(clean.get("retry_interval_minutes") or 5))
    clean["overlap_policy"] = str(clean.get("overlap_policy") or "merge")
    if clean["overlap_policy"] not in {"merge", "queue", "skip"}:
        raise ValueError("触发器重叠策略不合法。")
    clean["action_nodes"] = _normalize_nodes(list(clean.get("action_nodes") or []))
    clean["action_edges"] = _normalize_edges(list(clean.get("action_edges") or []))
    clean["graph_schema_version"] = max(1, int(clean.get("graph_schema_version") or 1))
    if clean["graph_schema_version"] >= 2 and clean["action_nodes"]:
        _validate_standalone_trigger_action_graph(clean["action_nodes"], clean["action_edges"])
    return clean


def _folder_to_response(folder: DataPlatformFolder) -> DataPlatformFolderResponse:
    return DataPlatformFolderResponse(
        folder_id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        status=folder.status,
        created_by_username=folder.created_by_username,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


def _workflow_to_response(session, workflow: DataPlatformWorkflow) -> DataPlatformWorkflowResponse:
    versions = session.execute(
        select(DataPlatformWorkflowVersion)
        .where(DataPlatformWorkflowVersion.workflow_id == workflow.id)
        .order_by(desc(DataPlatformWorkflowVersion.version_no))
    ).scalars().all()
    latest_dev = next((item for item in versions if item.channel == "dev"), None)
    latest_prod = next((item for item in versions if item.channel == "prod"), None)
    online = next((item for item in versions if item.status == "online"), None)
    return DataPlatformWorkflowResponse(
        workflow_id=workflow.id,
        folder_id=workflow.folder_id,
        name=workflow.name,
        description=workflow.description,
        status=workflow.status,
        business_metadata=workflow.business_metadata or {},
        latest_dev_version_id=latest_dev.id if latest_dev else None,
        latest_prod_version_id=latest_prod.id if latest_prod else None,
        online_version_id=online.id if online else None,
        created_by_username=workflow.created_by_username,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _version_to_response(version: DataPlatformWorkflowVersion) -> DataPlatformVersionResponse:
    return DataPlatformVersionResponse(
        version_id=version.id,
        workflow_id=version.workflow_id,
        version_no=version.version_no,
        channel=version.channel,  # type: ignore[arg-type]
        status=version.status,  # type: ignore[arg-type]
        nodes=version.nodes or [],
        edges=version.edges or [],
        business_metadata=version.business_metadata or {},
        release_snapshot=version.release_snapshot,
        execution_content_hash=version.execution_content_hash,
        schedule_enabled=version.schedule_enabled,
        schedule_type=version.schedule_type,  # type: ignore[arg-type]
        run_time=version.run_time,
        day_of_month=version.day_of_month,
        day_of_week=version.day_of_week,
        interval_minutes=version.interval_minutes,
        last_run_at=version.last_run_at,
        next_run_at=version.next_run_at,
        submitted_at=version.submitted_at,
        published_at=version.published_at,
        offline_at=version.offline_at,
        created_by_username=version.created_by_username,
        updated_by_username=version.updated_by_username,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _run_to_response(run: DataPlatformWorkflowRun) -> DataPlatformRunResponse:
    return DataPlatformRunResponse(
        run_id=run.id,
        workflow_id=run.workflow_id,
        version_id=run.version_id,
        version_no=run.version_no,
        channel=run.channel,  # type: ignore[arg-type]
        trigger_type=run.trigger_type,
        trigger_context=run.trigger_context,
        status=run.status,  # type: ignore[arg-type]
        message=run.message,
        total_count=run.total_count,
        success_count=run.success_count,
        failed_count=run.failed_count,
        skipped_count=run.skipped_count,
        created_by_username=run.created_by_username,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.updated_at,
    )


def _node_run_to_response(row: DataPlatformNodeRun) -> DataPlatformNodeRunResponse:
    return DataPlatformNodeRunResponse(
        node_run_id=row.id,
        run_id=row.run_id,
        node_key=row.node_key,
        node_name=row.node_name,
        node_type=row.node_type,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        message=row.message,
        upstream_keys=row.upstream_keys or [],
        result=row.result or {},
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


def _component_run_to_response(row: DataPlatformComponentRun) -> DataPlatformComponentRunResponse:
    selected = row.selected_items if isinstance(row.selected_items, list) else None
    table_runs = getattr(row, "_table_runs", []) or []
    return DataPlatformComponentRunResponse(
        component_run_id=row.id,
        node_id=row.node_id,
        node_type=row.node_type,  # type: ignore[arg-type]
        node_name=row.node_name,
        node_revision=int(row.node_revision or 1),
        trigger_type=row.trigger_type,
        selected_items=[str(item) for item in selected] if selected is not None else None,
        status=row.status,  # type: ignore[arg-type]
        message=row.message,
        result=row.result or {},
        created_by_username=row.created_by_username,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        table_runs=[_component_run_table_to_response(item) for item in table_runs],
    )


def _component_run_table_to_response(row: DataPlatformComponentRunTable) -> DataPlatformComponentRunTableResponse:
    return DataPlatformComponentRunTableResponse(
        table_run_id=row.id,
        component_run_id=row.component_run_id,
        node_id=row.node_id,
        mapping_id=row.mapping_id,
        source_catalog=row.source_catalog,
        source_schema=row.source_schema,
        source_table=row.source_table,
        target_database=row.target_database,
        target_table=row.target_table,
        sync_method=row.sync_method,
        write_mode=row.write_mode,
        schema_policy=row.schema_policy,
        status=row.status,  # type: ignore[arg-type]
        message=row.message,
        loaded_rows=int(row.loaded_rows or 0),
        duration_ms=int(row.duration_ms or 0),
        result_summary=row.result_summary or {},
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


def _component_run_log_to_response(row: DataPlatformComponentRunLog) -> DataPlatformComponentRunLogResponse:
    return DataPlatformComponentRunLogResponse(
        log_id=row.id,
        component_run_id=row.component_run_id,
        table_run_id=row.table_run_id,
        level=row.level,
        stage=row.stage,
        message=row.message,
        payload=row.payload or {},
        created_at=row.created_at,
    )


def _component_run_status(value: Any) -> str:
    clean = str(value or "failed").strip()
    if clean in {"queued", "running", "succeeded", "failed", "partial", "cancelled"}:
        return clean
    return "succeeded" if clean == "success" else "failed"
