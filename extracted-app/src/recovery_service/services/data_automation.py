from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import re
import threading
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select

from recovery_service.common.security import decrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.domain import RemoteHost
from recovery_service.core.models.task import (
    DataAsset,
    DataAutomationBatch,
    DataAutomationBlueprint,
    DataAutomationEvent,
    DataAutomationPipeline,
    DataClassificationRule,
    DataLineageEdge,
    DataPlatformComponentRun,
    DataPlatformComponentRunTable,
    DataPlatformNode,
    DataPlatformWorkflowRun,
    DataPlatformWorkflowVersion,
    DorisSm4BatchJob,
    DorisSm4TaskDefinition,
    RecoveryTask,
    DatabaseConnectionProfile,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.infrastructure.ssh.async_client import AsyncSSHClient
from recovery_service.services.auth import AuthContext
from recovery_service.settings import get_settings

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_THREAD: threading.Thread | None = None
_TERMINAL_BATCH_STATES = {"completed", "blocked", "failed", "partial", "cancelled"}


def schema_signature(columns: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "name": str(item.get("name") or item.get("column_name") or "").strip().casefold(),
            "type": _normalize_type(item.get("type") or item.get("data_type")),
            "nullable": bool(item.get("nullable", True)),
            "key": bool(item.get("key") or item.get("is_primary_key")),
            "ordinal": int(item.get("ordinal") or item.get("ordinal_position") or index + 1),
        }
        for index, item in enumerate(columns or [])
    ]
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_pipeline(data: dict[str, Any], actor: AuthContext | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = DataAutomationPipeline(
            name=_required(data.get("name"), "流水线名称")[:128],
            status=str(data.get("status") or "active"),
            auto_watch_enabled=bool(data.get("auto_watch_enabled", False)),
            watch_interval_minutes=_bounded_int(data.get("watch_interval_minutes"), 5, 1, 1440),
            stable_wait_seconds=_bounded_int(data.get("stable_wait_seconds"), 60, 10, 86400),
            file_pattern=str(data.get("file_pattern") or "*.dmp")[:255],
            restore_template_task_id=_uuid_or_none(data.get("restore_template_task_id")),
            data_sync_node_id=_uuid_or_none(data.get("data_sync_node_id")),
            standard_workflow_version_id=_uuid_or_none(data.get("standard_workflow_version_id")),
            sm4_task_definition_id=_uuid_or_none(data.get("sm4_task_definition_id")),
            business_domain=_optional(data.get("business_domain"), 128),
            standard_target=dict(data.get("standard_target") or {}),
            config=dict(data.get("config") or {}),
            created_by_username=actor.username if actor else None,
        )
        _validate_pipeline_references(session, row)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _pipeline_dict(row)
    finally:
        session.close()


def update_pipeline(pipeline_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = session.get(DataAutomationPipeline, pipeline_id)
        if not row:
            raise KeyError("数据自动化流水线不存在。")
        for field in ("name", "status", "file_pattern", "business_domain"):
            if field in data:
                setattr(row, field, _optional(data[field], 255 if field == "file_pattern" else 128) or ("*.dmp" if field == "file_pattern" else ""))
        for field in ("auto_watch_enabled",):
            if field in data:
                setattr(row, field, bool(data[field]))
        if "watch_interval_minutes" in data:
            row.watch_interval_minutes = _bounded_int(data["watch_interval_minutes"], 5, 1, 1440)
        if "stable_wait_seconds" in data:
            row.stable_wait_seconds = _bounded_int(data["stable_wait_seconds"], 60, 10, 86400)
        for field in ("restore_template_task_id", "data_sync_node_id", "standard_workflow_version_id", "sm4_task_definition_id"):
            if field in data:
                setattr(row, field, _uuid_or_none(data[field]))
        if "standard_target" in data:
            row.standard_target = dict(data["standard_target"] or {})
        if "config" in data:
            row.config = dict(data["config"] or {})
        row.updated_at = app_now()
        _validate_pipeline_references(session, row)
        session.commit()
        session.refresh(row)
        return _pipeline_dict(row)
    finally:
        session.close()


def list_pipelines() -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        return [_pipeline_dict(row) for row in session.scalars(select(DataAutomationPipeline).order_by(DataAutomationPipeline.name)).all()]
    finally:
        session.close()


def list_batches(pipeline_id: uuid.UUID | None = None, limit: int = 100) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataAutomationBatch).order_by(desc(DataAutomationBatch.created_at)).limit(max(1, min(limit, 500)))
        if pipeline_id:
            stmt = stmt.where(DataAutomationBatch.pipeline_id == pipeline_id)
        return [_batch_dict(row) for row in session.scalars(stmt).all()]
    finally:
        session.close()


def get_batch(batch_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = session.get(DataAutomationBatch, batch_id)
        if not row:
            raise KeyError("数据批次不存在。")
        events = session.scalars(select(DataAutomationEvent).where(DataAutomationEvent.batch_id == batch_id).order_by(DataAutomationEvent.created_at)).all()
        result = _batch_dict(row)
        result["events"] = [_event_dict(item) for item in events]
        return result
    finally:
        session.close()


def create_blueprint(pipeline_id: uuid.UUID, data: dict[str, Any], actor: AuthContext | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        pipeline = session.get(DataAutomationPipeline, pipeline_id)
        if not pipeline:
            raise KeyError("数据自动化流水线不存在。")
        latest = session.scalar(select(DataAutomationBlueprint.version_no).where(DataAutomationBlueprint.pipeline_id == pipeline_id).order_by(desc(DataAutomationBlueprint.version_no)).limit(1)) or 0
        contract = dict(data.get("schema_contract") or {})
        signature = str(data.get("schema_signature") or "").strip() or (schema_signature(contract.get("columns") or []) if contract.get("columns") else None)
        row = DataAutomationBlueprint(
            pipeline_id=pipeline_id,
            version_no=int(latest) + 1,
            name=_required(data.get("name"), "蓝图名称")[:128],
            source_rule=dict(data.get("source_rule") or {}),
            schema_signature=signature,
            schema_contract=contract,
            execution_snapshot=dict(data.get("execution_snapshot") or {}),
            auto_execute=bool(data.get("auto_execute", False)),
            created_by_username=actor.username if actor else None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _blueprint_dict(row)
    finally:
        session.close()


def list_blueprints(pipeline_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        stmt = select(DataAutomationBlueprint).order_by(desc(DataAutomationBlueprint.created_at))
        if pipeline_id:
            stmt = stmt.where(DataAutomationBlueprint.pipeline_id == pipeline_id)
        return [_blueprint_dict(row) for row in session.scalars(stmt).all()]
    finally:
        session.close()


def match_batch_blueprint(batch_id: uuid.UUID, schema_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        batch = session.get(DataAutomationBatch, batch_id)
        if not batch:
            raise KeyError("数据批次不存在。")
        contract = dict(schema_contract or (batch.context or {}).get("schema_contract") or {})
        columns = list(contract.get("columns") or [])
        signature = schema_signature(columns) if columns else batch.schema_signature
        if not signature:
            raise ValueError("批次尚无可用于路径匹配的 Schema 结构。")
        batch.schema_signature = signature
        candidates = session.scalars(select(DataAutomationBlueprint).where(DataAutomationBlueprint.pipeline_id == batch.pipeline_id, DataAutomationBlueprint.status == "active").order_by(desc(DataAutomationBlueprint.version_no))).all()
        scored = []
        for blueprint in candidates:
            score, reason = _blueprint_score(signature, columns, blueprint)
            scored.append((score, blueprint, reason))
        scored.sort(key=lambda item: (item[0], item[1].version_no), reverse=True)
        if not scored:
            batch.state = "awaiting_confirmation"; batch.match_confidence = 0.0; batch.match_reason = "没有可用的历史蓝图。"
            session.commit()
            return {"batch_id": str(batch.id), "matched": False, "confidence": 0.0, "level": "low", "reason": batch.match_reason, "candidates": []}
        score, blueprint, reason = scored[0]
        level = "high" if score >= 0.95 else ("medium" if score >= 0.75 else "low")
        batch.match_confidence = score; batch.match_reason = reason
        if level == "high":
            batch.blueprint_id = blueprint.id; batch.blueprint_version = blueprint.version_no
            batch.state = "path_matched" if blueprint.auto_execute else "awaiting_confirmation"
        else:
            batch.state = "awaiting_confirmation" if level == "medium" else "blocked"
        _event(session, batch, "blueprint_matched", "success" if level == "high" else "warning", reason, {"blueprint_id": str(blueprint.id), "version": blueprint.version_no, "confidence": score, "level": level})
        session.commit()
        return {"batch_id": str(batch.id), "matched": level == "high", "blueprint_id": str(blueprint.id), "blueprint_version": blueprint.version_no, "confidence": score, "level": level, "reason": reason, "auto_execute": blueprint.auto_execute, "candidates": [{"blueprint_id": str(item[1].id), "version": item[1].version_no, "confidence": item[0], "reason": item[2]} for item in scored[:10]]}
    finally:
        session.close()


def confirm_batch_blueprint(batch_id: uuid.UUID, blueprint_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        batch = session.get(DataAutomationBatch, batch_id); blueprint = session.get(DataAutomationBlueprint, blueprint_id)
        if not batch or not blueprint or blueprint.pipeline_id != batch.pipeline_id:
            raise KeyError("批次或蓝图不存在。")
        batch.blueprint_id = blueprint.id; batch.blueprint_version = blueprint.version_no; batch.state = "path_matched"; batch.match_reason = "管理员已确认处理蓝图。"; batch.error_message = None
        _event(session, batch, "blueprint_confirmed", "success", batch.match_reason, {"blueprint_id": str(blueprint.id), "version": blueprint.version_no})
        session.commit()
        return _batch_dict(batch)
    finally:
        session.close()


def scan_pipeline(pipeline_id: uuid.UUID, *, dispatch: bool = True) -> dict[str, Any]:
    session = get_sync_session_factory()()
    created: list[str] = []
    queued: list[str] = []
    try:
        pipeline = session.get(DataAutomationPipeline, pipeline_id)
        if not pipeline:
            raise KeyError("数据自动化流水线不存在。")
        template = session.get(RecoveryTask, pipeline.restore_template_task_id) if pipeline.restore_template_task_id else None
        if not template:
            raise ValueError("流水线尚未绑定有效的 Oracle 恢复模板任务。")
        files = _list_template_dmp_files(template, pipeline.file_pattern)
        now = app_now()
        for group in _group_dump_files(files):
            fingerprint = _source_fingerprint(group)
            batch = session.scalar(select(DataAutomationBatch).where(DataAutomationBatch.pipeline_id == pipeline.id, DataAutomationBatch.source_fingerprint == fingerprint))
            if batch is None:
                batch = DataAutomationBatch(
                    pipeline_id=pipeline.id,
                    state="stabilizing",
                    source_path=str(group[0]["remote_path"]),
                    source_files=group,
                    source_fingerprint=fingerprint,
                    source_observed_at=now,
                    message="已发现 DMP 文件组，等待文件稳定。",
                )
                session.add(batch)
                session.flush()
                _event(session, batch, "discovered", "success", "发现新的 DMP 文件组。", {"files": group})
                created.append(str(batch.id))
            elif batch.state == "stabilizing" and (now - batch.source_observed_at).total_seconds() >= int(pipeline.stable_wait_seconds or 60):
                batch.source_stable_at = now
                if dispatch and bool((pipeline.config or {}).get("auto_restore", True)):
                    _queue_restore(session, pipeline, batch, template)
                    queued.append(str(batch.id))
                else:
                    batch.state = "discovered"
                    batch.message = "文件已稳定，等待人工启动恢复。"
                    _event(session, batch, "file_stable", "success", batch.message)
        pipeline.last_scan_at = now
        pipeline.next_scan_at = now + timedelta(minutes=int(pipeline.watch_interval_minutes or 5))
        session.commit()
        return {"pipeline_id": str(pipeline.id), "found_groups": len(_group_dump_files(files)), "created_batch_ids": created, "queued_batch_ids": queued, "last_scan_at": now}
    finally:
        session.close()


def resume_batch(batch_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        batch = session.get(DataAutomationBatch, batch_id)
        if not batch:
            raise KeyError("数据批次不存在。")
        pipeline = session.get(DataAutomationPipeline, batch.pipeline_id)
        template = session.get(RecoveryTask, pipeline.restore_template_task_id) if pipeline and pipeline.restore_template_task_id else None
        if not pipeline or not template:
            raise ValueError("流水线或恢复模板不存在。")
        resume_stage = batch.resume_from_stage or batch.state
        if resume_stage in {"restore_queued", "restoring", "discovered"}:
            batch.restore_task_id = None
            _queue_restore(session, pipeline, batch, template)
        elif resume_stage in {"sync_queued", "syncing"}:
            if not pipeline.data_sync_node_id or not batch.restored_target:
                raise ValueError("缺少可重试的数据同步组件或恢复目标。")
            from recovery_service.services.data_platform import submit_component_task_run
            result = submit_component_task_run(
                pipeline.data_sync_node_id,
                {"pipeline_batch_id": str(batch.id), "restored_target": batch.restored_target},
            )
            batch.sync_run_id = uuid.UUID(result["run_id"])
            batch.state = "sync_queued"
            batch.resume_from_stage = None
            batch.error_message = None
            batch.message = "数据同步重试任务已排队。"
            _event(session, batch, "sync_requeued", "success", batch.message, result)
        elif resume_stage in {"standardize_queued", "standardizing"}:
            if not pipeline.standard_workflow_version_id:
                raise ValueError("缺少可重试的标准化工作流版本。")
            from recovery_service.services.data_platform import run_version
            result = run_version(
                pipeline.standard_workflow_version_id,
                trigger_type="data_automation_retry",
                trigger_context={"pipeline_id": str(pipeline.id), "batch_id": str(batch.id), "restored_target": batch.restored_target, "raw_target": batch.raw_target},
            )
            batch.standard_run_id = result.run_id
            batch.state = "standardize_queued"
            batch.resume_from_stage = None
            batch.error_message = None
            batch.message = "标准化重试流程已排队。"
            _event(session, batch, "standardize_requeued", "success", batch.message, {"run_id": str(result.run_id)})
        else:
            batch.state = resume_stage
            batch.resume_from_stage = None
            batch.error_message = None
            batch.message = "已请求从断点继续。"
        session.commit()
        return _batch_dict(batch)
    finally:
        session.close()


def advance_batches() -> dict[str, int]:
    session = get_sync_session_factory()()
    advanced = failed = 0
    try:
        batches = session.scalars(select(DataAutomationBatch).where(DataAutomationBatch.state.not_in(_TERMINAL_BATCH_STATES)).order_by(DataAutomationBatch.updated_at).limit(100)).all()
        for batch in batches:
            try:
                if _advance_one(session, batch):
                    advanced += 1
            except Exception as exc:
                failed_stage = batch.state
                batch.state = "failed"
                batch.resume_from_stage = batch.resume_from_stage or failed_stage
                batch.error_message = str(exc)
                batch.message = "流水线阶段推进失败。"
                _event(session, batch, "stage_failed", "failed", str(exc))
                failed += 1
            session.commit()
        return {"advanced": advanced, "failed": failed}
    finally:
        session.close()


def register_asset(data: dict[str, Any], batch_id: uuid.UUID | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        columns = list(data.get("columns") or (data.get("schema_contract") or {}).get("columns") or [])
        signature = str(data.get("schema_signature") or "").strip() or schema_signature(columns)
        identity = {
            "connection_id": _uuid_or_none(data.get("connection_id")), "catalog": str(data.get("catalog") or ""),
            "database": _required(data.get("database"), "数据库"), "table_name": _required(data.get("table_name"), "表名"),
            "layer": str(data.get("layer") or "raw"),
        }
        row = session.scalar(select(DataAsset).where(
            DataAsset.connection_id == identity["connection_id"], DataAsset.catalog == identity["catalog"],
            DataAsset.database == identity["database"], DataAsset.table_name == identity["table_name"], DataAsset.layer == identity["layer"],
        ))
        if row is None:
            row = DataAsset(**identity, engine=str(data.get("engine") or "doris"), connection_name=_optional(data.get("connection_name"), 128), business_domain=_optional(data.get("business_domain"), 128), schema_signature=signature, schema_contract={"columns": columns}, first_batch_id=batch_id, last_batch_id=batch_id)
            session.add(row)
        else:
            row.schema_signature = signature
            row.schema_contract = {"columns": columns}
            row.last_batch_id = batch_id or row.last_batch_id
            row.updated_at = app_now()
        session.commit()
        session.refresh(row)
        return _asset_dict(row)
    finally:
        session.close()


def list_assets(limit: int = 200) -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        return [_asset_dict(row) for row in session.scalars(select(DataAsset).order_by(desc(DataAsset.updated_at)).limit(max(1, min(limit, 1000)))).all()]
    finally:
        session.close()


def lineage_overview(
    *,
    search: str | None = None,
    layer: str | None = None,
    batch_id: uuid.UUID | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return a bounded, read-only catalog for the standalone lineage center."""
    session = get_sync_session_factory()()
    try:
        asset_rows = session.scalars(
            select(DataAsset).order_by(desc(DataAsset.updated_at)).limit(max(1, min(limit, 1000)))
        ).all()
        normalized_search = str(search or "").strip().casefold()
        batch_asset_ids: set[uuid.UUID] = set()
        if batch_id:
            for edge in session.scalars(select(DataLineageEdge).where(DataLineageEdge.batch_id == batch_id)).all():
                batch_asset_ids.update((edge.source_asset_id, edge.target_asset_id))

        def matches(row: DataAsset) -> bool:
            if layer and row.layer != layer:
                return False
            if batch_id and row.first_batch_id != batch_id and row.last_batch_id != batch_id and row.id not in batch_asset_ids:
                return False
            if not normalized_search:
                return True
            columns = (row.schema_contract or {}).get("columns") or []
            searchable = [
                row.engine, row.catalog, row.database, row.table_name, row.business_domain,
                row.connection_name, row.schema_signature,
                *[item.get("name") or item.get("column_name") for item in columns],
            ]
            return any(normalized_search in str(value or "").casefold() for value in searchable)

        filtered_assets = [row for row in asset_rows if matches(row)]
        asset_ids = {row.id for row in filtered_assets}
        edge_rows: list[DataLineageEdge] = []
        if asset_ids:
            edge_stmt = select(DataLineageEdge).where(
                DataLineageEdge.source_asset_id.in_(asset_ids),
                DataLineageEdge.target_asset_id.in_(asset_ids),
            ).order_by(desc(DataLineageEdge.created_at))
            if batch_id:
                edge_stmt = edge_stmt.where(DataLineageEdge.batch_id == batch_id)
            edge_rows = session.scalars(edge_stmt.limit(5000)).all()
        serialized_edges = [_lineage_dict(row) for row in edge_rows]
        return {
            "assets": [_asset_dict(row) for row in filtered_assets],
            "edges": serialized_edges,
            "summary": {
                "asset_count": len(filtered_assets),
                "edge_count": len(edge_rows),
                "field_edge_count": sum(1 for row in edge_rows if row.source_field or row.target_field),
                "review_count": sum(1 for row in edge_rows if row.review_required),
                "sm4_edge_count": sum(1 for row in edge_rows if "SM4" in str(row.expression or "").upper()),
            },
            "truncated": len(asset_rows) >= max(1, min(limit, 1000)) or len(edge_rows) >= 5000,
        }
    finally:
        session.close()


def create_lineage_edge(data: dict[str, Any]) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        kind = str(data.get("transformation_type") or "direct")
        row = DataLineageEdge(
            batch_id=_uuid_or_none(data.get("batch_id")), source_asset_id=uuid.UUID(str(data["source_asset_id"])),
            source_field=_optional(data.get("source_field"), 255), target_asset_id=uuid.UUID(str(data["target_asset_id"])),
            target_field=_optional(data.get("target_field"), 255), transformation_type=kind,
            expression=_optional(data.get("expression"), 100000), workflow_version_id=_uuid_or_none(data.get("workflow_version_id")),
            node_key=_optional(data.get("node_key"), 128), evidence=dict(data.get("evidence") or {}),
            confidence=float(data.get("confidence", 1.0)), review_required=bool(data.get("review_required", kind not in {"direct", "rename", "cast"})),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _lineage_dict(row)
    finally:
        session.close()


def trace_lineage(asset_id: uuid.UUID, *, direction: str = "upstream", max_depth: int = 8, batch_id: uuid.UUID | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        frontier = {asset_id}; seen_assets = {asset_id}; edges: list[DataLineageEdge] = []
        for _ in range(max(1, min(max_depth, 20))):
            if not frontier:
                break
            column = DataLineageEdge.target_asset_id if direction == "upstream" else DataLineageEdge.source_asset_id
            stmt = select(DataLineageEdge).where(column.in_(frontier))
            if batch_id:
                stmt = stmt.where(DataLineageEdge.batch_id == batch_id)
            rows = session.scalars(stmt).all()
            next_assets = set()
            for row in rows:
                edges.append(row)
                other = row.source_asset_id if direction == "upstream" else row.target_asset_id
                if other not in seen_assets:
                    seen_assets.add(other); next_assets.add(other)
            frontier = next_assets
        assets = session.scalars(select(DataAsset).where(DataAsset.id.in_(seen_assets))).all()
        return {"root_asset_id": str(asset_id), "direction": direction, "batch_id": str(batch_id) if batch_id else None, "assets": [_asset_dict(row) for row in assets], "edges": [_lineage_dict(row) for row in edges]}
    finally:
        session.close()


def create_classification_rule(data: dict[str, Any], actor: AuthContext | None = None) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        row = DataClassificationRule(name=_required(data.get("name"), "规则名称")[:128], priority=int(data.get("priority", 100)), match_config=dict(data.get("match_config") or {}), classification=str(data.get("classification") or "internal"), protection_action=str(data.get("protection_action") or "review"), auto_apply=bool(data.get("auto_apply", False)), created_by_username=actor.username if actor else None)
        session.add(row); session.commit(); session.refresh(row)
        return _classification_rule_dict(row)
    finally:
        session.close()


def list_classification_rules() -> list[dict[str, Any]]:
    session = get_sync_session_factory()()
    try:
        return [_classification_rule_dict(row) for row in session.scalars(select(DataClassificationRule).order_by(DataClassificationRule.priority, DataClassificationRule.name)).all()]
    finally:
        session.close()


def classify_asset(asset_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        asset = session.get(DataAsset, asset_id)
        if not asset:
            raise KeyError("数据资产不存在。")
        rules = session.scalars(select(DataClassificationRule).where(DataClassificationRule.status == "active").order_by(DataClassificationRule.priority)).all()
        results = []
        for column in (asset.schema_contract or {}).get("columns") or []:
            field = str(column.get("name") or column.get("column_name") or "")
            matched = next((rule for rule in rules if _classification_matches(rule.match_config or {}, asset, field, column)), None)
            results.append({"field": field, "classification": matched.classification if matched else "internal", "protection_action": matched.protection_action if matched else "none", "rule_id": str(matched.id) if matched else None, "auto_apply": bool(matched.auto_apply) if matched else False})
        asset.classification_summary = {"fields": results, "classified_at": app_now().isoformat()}
        session.commit()
        return {"asset_id": str(asset.id), **asset.classification_summary}
    finally:
        session.close()


def build_reverse_encryption_plan(standard_asset_id: uuid.UUID) -> dict[str, Any]:
    session = get_sync_session_factory()()
    try:
        standard = session.get(DataAsset, standard_asset_id)
        if not standard:
            raise KeyError("标准资产不存在。")
        sensitive = {item["field"]: item for item in (standard.classification_summary or {}).get("fields") or [] if item.get("protection_action") == "sm4"}
        edges = session.scalars(select(DataLineageEdge).where(DataLineageEdge.target_asset_id == standard_asset_id, DataLineageEdge.target_field.in_(list(sensitive) or ["__none__"]))).all()
        suggestions = []
        for edge in edges:
            source = session.get(DataAsset, edge.source_asset_id)
            safe = edge.transformation_type in {"direct", "rename", "cast"} and not edge.review_required
            suggestions.append({"source_asset_id": str(edge.source_asset_id), "source_database": source.database if source else None, "source_table": source.table_name if source else None, "source_field": edge.source_field, "standard_field": edge.target_field, "transformation_type": edge.transformation_type, "auto_eligible": safe, "review_required": not safe, "reason": "直接可证明血缘" if safe else "复杂转换或血缘待确认"})
        return {"standard_asset_id": str(standard_asset_id), "sensitive_fields": list(sensitive), "suggestions": suggestions, "auto_eligible_count": sum(1 for item in suggestions if item["auto_eligible"])}
    finally:
        session.close()


def execute_reverse_encryption_plan(
    standard_asset_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    confirm: bool,
    actor: AuthContext | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("反向加密会写入业务表，必须显式确认后执行。")
    plan = build_reverse_encryption_plan(standard_asset_id)
    eligible = [item for item in plan["suggestions"] if item.get("auto_eligible")]
    if not eligible:
        raise ValueError("没有具备可证明直接血缘的自动加密字段。")
    session = get_sync_session_factory()()
    try:
        pipeline = session.get(DataAutomationPipeline, pipeline_id)
        definition = session.get(DorisSm4TaskDefinition, pipeline.sm4_task_definition_id) if pipeline and pipeline.sm4_task_definition_id else None
        if not pipeline or not definition:
            raise ValueError("流水线尚未绑定 SM4 任务定义。")
        if any(str(item.get("source_database") or "").casefold() != definition.database.casefold() for item in eligible):
            raise ValueError("血缘源库与绑定的 SM4 任务定义数据库不一致，已阻止跨库误加密。")
        profile = session.get(DatabaseConnectionProfile, definition.connection_id)
        if not profile:
            raise ValueError("SM4 任务定义的 Doris 连接不存在。")
        grouped: dict[str, set[str]] = {}
        for item in eligible:
            grouped.setdefault(str(item["source_table"]), set()).add(str(item["source_field"]))
        tables = [{"table_name": table, "columns": sorted(columns)} for table, columns in grouped.items()]
        from recovery_service.services.doris_encryption import create_sm4_batch_task
        result = create_sm4_batch_task(
            profile,
            database=definition.database,
            tables=tables,
            table_strategy=definition.table_strategy,
            target_suffix=definition.target_suffix,
            actor=actor,
        )
        return {
            "standard_asset_id": str(standard_asset_id),
            "pipeline_id": str(pipeline_id),
            "batch_id": str(result.batch_id),
            "tables": tables,
            "status": result.state,
        }
    finally:
        session.close()


def start_data_automation_scheduler() -> None:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(target=_scheduler_loop, name="data-automation-scheduler", daemon=True)
        _SCHEDULER_THREAD.start()


def stop_data_automation_scheduler() -> None:
    _SCHEDULER_STOP.set()
    thread = _SCHEDULER_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=5)


def _scheduler_loop() -> None:
    while not _SCHEDULER_STOP.wait(30):
        try:
            advance_batches()
            session = get_sync_session_factory()()
            try:
                now = app_now()
                ids = session.scalars(select(DataAutomationPipeline.id).where(DataAutomationPipeline.status == "active", DataAutomationPipeline.auto_watch_enabled.is_(True), (DataAutomationPipeline.next_scan_at.is_(None) | (DataAutomationPipeline.next_scan_at <= now)))).all()
            finally:
                session.close()
            for pipeline_id in ids:
                try: scan_pipeline(pipeline_id)
                except Exception:  # scheduler isolation; API/manual scan exposes detail
                    continue
        except Exception:
            continue


def _advance_one(session, batch: DataAutomationBatch) -> bool:
    pipeline = session.get(DataAutomationPipeline, batch.pipeline_id)
    if not pipeline:
        raise KeyError("流水线定义已不存在。")
    if batch.restore_task_id and batch.state in {"restore_queued", "restoring"}:
        task = session.get(RecoveryTask, batch.restore_task_id)
        if not task: raise KeyError("关联恢复任务不存在。")
        if task.state in {"created", "policy_running", "importing", "correcting", "validating", "stopping"}:
            batch.state = "restoring"; batch.message = f"Oracle 恢复任务正在执行：{task.state}"; return False
        if task.state not in {"succeeded", "succeeded_with_warnings"}:
            batch.state = "failed"; batch.resume_from_stage = "restore_queued"; batch.error_message = task.error_message or f"恢复任务状态：{task.state}"; _event(session, batch, "restore_failed", "failed", batch.error_message); return True
        metadata = task.metadata_snapshot or {}
        target_schema = metadata.get("schema") or metadata.get("username") or (metadata.get("target") or {}).get("schema")
        if not target_schema:
            batch.state = "blocked"; batch.resume_from_stage = "asset_registered"; batch.error_message = "恢复成功但没有明确的实际目标 Schema。"; _event(session, batch, "target_missing", "blocked", batch.error_message); return True
        batch.restored_target = {"connection": task.target_connection, "schema": target_schema, "metadata": metadata}
        batch.state = "restored"; batch.message = f"Oracle 已恢复到 {target_schema}。"; _event(session, batch, "restore_succeeded", "success", batch.message, batch.restored_target)
        if pipeline.data_sync_node_id:
            from recovery_service.services.data_platform import submit_component_task_run
            result = submit_component_task_run(pipeline.data_sync_node_id, {"pipeline_batch_id": str(batch.id), "restored_target": batch.restored_target})
            batch.sync_run_id = uuid.UUID(result["run_id"]); batch.state = "sync_queued"; batch.message = "数据同步任务已排队。"; _event(session, batch, "sync_queued", "success", batch.message, result)
        else:
            batch.state = "raw_ready"; batch.message = "未绑定数据同步任务，等待登记原始资产。"
        return True
    if batch.sync_run_id and batch.state in {"sync_queued", "syncing"}:
        run = session.get(DataPlatformComponentRun, batch.sync_run_id)
        if not run: raise KeyError("关联数据同步运行不存在。")
        if run.status in {"queued", "running"}: batch.state = "syncing"; batch.message = run.message; return False
        if run.status not in {"succeeded", "success"}: batch.state = "failed"; batch.resume_from_stage = "sync_queued"; batch.error_message = run.message or "数据同步失败。"; _event(session, batch, "sync_failed", "failed", batch.error_message); return True
        asset_ids = _record_sync_assets(session, pipeline, batch, run)
        batch.raw_target = {**dict(run.result or {}), "asset_ids": [str(item) for item in asset_ids]}; batch.state = "raw_ready"; batch.message = "Doris 原始层同步完成并已登记数据资产与血缘。"; _event(session, batch, "sync_succeeded", "success", batch.message, batch.raw_target)
        if pipeline.standard_workflow_version_id:
            from recovery_service.services.data_platform import run_version
            result = run_version(pipeline.standard_workflow_version_id, trigger_type="data_automation", trigger_context={"pipeline_id": str(pipeline.id), "batch_id": str(batch.id), "restored_target": batch.restored_target, "raw_target": batch.raw_target})
            batch.standard_run_id = result.run_id; batch.state = "standardize_queued"; batch.message = "标准化离线流程已排队。"; _event(session, batch, "standardize_queued", "success", batch.message, {"run_id": str(result.run_id)})
        return True
    if batch.standard_run_id and batch.state in {"standardize_queued", "standardizing"}:
        run = session.get(DataPlatformWorkflowRun, batch.standard_run_id)
        if not run: raise KeyError("关联标准化运行不存在。")
        if run.status in {"queued", "running"}: batch.state = "standardizing"; batch.message = run.message; return False
        if run.status != "succeeded": batch.state = "failed"; batch.resume_from_stage = "standardize_queued"; batch.error_message = run.message or "标准化流程失败。"; _event(session, batch, "standardize_failed", "failed", batch.error_message); return True
        standard_assets = _record_standard_assets(session, pipeline, batch)
        batch.standard_target = {**dict(pipeline.standard_target or {}), "asset_ids": [str(item.id) for item in standard_assets]}
        for asset in standard_assets:
            _classify_asset_in_session(session, asset)
        batch.state = "standard_ready"; batch.message = "标准层流程执行完成，血缘与分级结果已固化。"; _event(session, batch, "standardize_succeeded", "success", batch.message, batch.standard_target)
        if pipeline.sm4_task_definition_id and bool((pipeline.config or {}).get("auto_encryption_enabled")):
            eligible_assets = [asset for asset in standard_assets if _reverse_plan_in_session(session, asset.id)]
            if len(eligible_assets) == 1:
                batch.context = {**dict(batch.context or {}), "encryption_standard_asset_id": str(eligible_assets[0].id)}
                batch.state = "encryption_ready"; batch.message = "反向 SM4 加密计划已生成，等待安全提交。"; _event(session, batch, "encryption_planned", "success", batch.message, {"standard_asset_id": str(eligible_assets[0].id)})
            elif len(eligible_assets) > 1:
                batch.state = "blocked"; batch.resume_from_stage = "standard_ready"; batch.error_message = "存在多个可加密标准资产，需要人工确认目标。"
        return True
    if batch.state == "encryption_ready":
        asset_id = _uuid_or_none((batch.context or {}).get("encryption_standard_asset_id"))
        if not asset_id: raise ValueError("加密计划缺少标准资产。")
        execution = execute_reverse_encryption_plan(asset_id, pipeline.id, confirm=True)
        batch.encryption_batch_id = uuid.UUID(execution["batch_id"]); batch.state = "encrypting"; batch.message = "反向 SM4 加密任务已排队。"; _event(session, batch, "encryption_queued", "success", batch.message, execution); return True
    if batch.encryption_batch_id and batch.state == "encrypting":
        job = session.get(DorisSm4BatchJob, batch.encryption_batch_id)
        if not job: raise KeyError("关联 SM4 加密批次不存在。")
        if job.state in {"queued", "running"}: return False
        if job.state not in {"succeeded", "success"}:
            batch.state = "failed"; batch.resume_from_stage = "standard_ready"; batch.error_message = job.message or "SM4 加密任务失败。"; _event(session, batch, "encryption_failed", "failed", batch.error_message); return True
        _record_secured_assets(session, pipeline, batch, job)
        batch.state = "completed"; batch.finished_at = app_now(); batch.message = "自动恢复、同步、标准化、分级与加密全链路已完成。"; _event(session, batch, "pipeline_completed", "success", batch.message); return True
    if batch.state == "standard_ready":
        batch.state = "completed"; batch.finished_at = app_now(); batch.message = "自动恢复、同步、标准化、血缘与分级链路已完成。"; _event(session, batch, "pipeline_completed", "success", batch.message); return True
    return False


def _upsert_asset(session, *, connection_id, catalog, database, table_name, layer, domain, columns, batch_id):
    row = session.scalar(select(DataAsset).where(
        DataAsset.connection_id == connection_id, DataAsset.catalog == (catalog or ""),
        DataAsset.database == database, DataAsset.table_name == table_name, DataAsset.layer == layer,
    ))
    contract = {"columns": list(columns or [])}
    if row is None:
        row = DataAsset(connection_id=connection_id, catalog=catalog or "", database=database, table_name=table_name, layer=layer, engine="doris" if layer != "restored" else "oracle", business_domain=domain, schema_signature=schema_signature(contract["columns"]), schema_contract=contract, first_batch_id=batch_id, last_batch_id=batch_id)
        session.add(row); session.flush()
    else:
        row.schema_contract = contract; row.schema_signature = schema_signature(contract["columns"]); row.last_batch_id = batch_id; row.updated_at = app_now()
    return row


def _record_sync_assets(session, pipeline, batch, run):
    table_runs = session.scalars(select(DataPlatformComponentRunTable).where(DataPlatformComponentRunTable.component_run_id == run.id, DataPlatformComponentRunTable.status == "succeeded")).all()
    node = session.get(DataPlatformNode, run.node_id)
    config = dict(node.config or {}) if node else {}
    source_connection_id = _uuid_or_none(config.get("source_connection_id"))
    target_connection_id = _uuid_or_none(config.get("target_connection_id"))
    results = []
    for item in table_runs:
        mapping = next((dict(value) for value in config.get("table_mappings") or [] if str(value.get("id") or value.get("source_table")) == str(item.mapping_id or item.source_table)), {})
        schema_columns = list(mapping.get("source_columns") or mapping.get("columns") or [])
        column_mappings = list(mapping.get("column_mappings") or [])
        source = _upsert_asset(session, connection_id=source_connection_id, catalog=item.source_catalog or "", database=item.source_schema or str((batch.restored_target or {}).get("schema") or ""), table_name=item.source_table or "unknown", layer="restored", domain=pipeline.business_domain, columns=schema_columns, batch_id=batch.id)
        target = _upsert_asset(session, connection_id=target_connection_id, catalog="", database=item.target_database or "", table_name=item.target_table or "unknown", layer="raw", domain=pipeline.business_domain, columns=schema_columns, batch_id=batch.id)
        if not session.scalar(select(DataLineageEdge.id).where(DataLineageEdge.batch_id == batch.id, DataLineageEdge.source_asset_id == source.id, DataLineageEdge.target_asset_id == target.id)):
            session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, target_asset_id=target.id, transformation_type="direct", evidence={"component_run_id": str(run.id), "table_run_id": str(item.id)}, confidence=1.0, review_required=False))
        for column in column_mappings:
            source_field = str(column.get("source_name") or column.get("source") or column.get("source_column") or column.get("name") or "")
            target_field = str(column.get("target_name") or column.get("target") or column.get("target_column") or source_field)
            if source_field:
                session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, source_field=source_field, target_asset_id=target.id, target_field=target_field, transformation_type="direct" if source_field == target_field else "rename", evidence={"component_run_id": str(run.id)}, confidence=1.0, review_required=False))
        results.append(target.id)
    return results


def _record_standard_assets(session, pipeline, batch):
    target = dict(pipeline.standard_target or {})
    definitions = list(target.get("assets") or ([target] if target.get("database") and target.get("table_name") else []))
    raw_ids = [_uuid_or_none(value) for value in (batch.raw_target or {}).get("asset_ids") or []]
    raw_assets = [session.get(DataAsset, value) for value in raw_ids if value]
    assets = []
    for definition in definitions:
        asset = _upsert_asset(session, connection_id=_uuid_or_none(definition.get("connection_id")), catalog=str(definition.get("catalog") or ""), database=str(definition["database"]), table_name=str(definition["table_name"]), layer="standard", domain=pipeline.business_domain, columns=list(definition.get("columns") or []), batch_id=batch.id)
        assets.append(asset)
        contracts = list(definition.get("lineage") or [])
        if contracts:
            for edge in contracts:
                source = next((item for item in raw_assets if item and item.table_name.casefold() == str(edge.get("source_table") or "").casefold()), raw_assets[0] if raw_assets else None)
                if source:
                    kind = str(edge.get("transformation_type") or "direct")
                    session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, source_field=_optional(edge.get("source_field"), 255), target_asset_id=asset.id, target_field=_optional(edge.get("target_field"), 255), transformation_type=kind, expression=_optional(edge.get("expression"), 100000), workflow_version_id=pipeline.standard_workflow_version_id, evidence={"contract": "standard_target"}, confidence=float(edge.get("confidence", 1.0)), review_required=kind not in {"direct", "rename", "cast"}))
        elif len(raw_assets) == 1:
            session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=raw_assets[0].id, target_asset_id=asset.id, transformation_type="direct", workflow_version_id=pipeline.standard_workflow_version_id, evidence={"contract": "single_input_default"}, confidence=1.0, review_required=False))
    return assets


def _record_secured_assets(session, pipeline, batch, job):
    raw_ids = [_uuid_or_none(value) for value in (batch.raw_target or {}).get("asset_ids") or []]
    raw_assets = [session.get(DataAsset, value) for value in raw_ids if value]
    secured_ids = []
    for result in job.results or []:
        if str(result.get("state") or "").lower() not in {"succeeded", "success"}:
            continue
        source = next((item for item in raw_assets if item and item.table_name.casefold() == str(result.get("table_name") or "").casefold()), None)
        if not source:
            continue
        target = _upsert_asset(
            session,
            connection_id=job.connection_id,
            catalog="",
            database=str(result.get("target_database") or job.database),
            table_name=str(result.get("target_table") or ""),
            layer="secured",
            domain=pipeline.business_domain,
            columns=list((source.schema_contract or {}).get("columns") or []),
            batch_id=batch.id,
        )
        secured_ids.append(str(target.id))
        encrypted = {str(value).casefold() for value in result.get("columns") or []}
        if not session.scalar(select(DataLineageEdge.id).where(DataLineageEdge.batch_id == batch.id, DataLineageEdge.source_asset_id == source.id, DataLineageEdge.target_asset_id == target.id, DataLineageEdge.source_field.is_(None))):
            session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, target_asset_id=target.id, transformation_type="expression", expression="SM4 secured table", evidence={"sm4_batch_id": str(job.id), "sm4_key_fingerprint": job.sm4_key_fingerprint}, confidence=1.0, review_required=False))
        for column in (source.schema_contract or {}).get("columns") or []:
            field = str(column.get("name") or column.get("column_name") or "")
            if not field:
                continue
            exists = session.scalar(select(DataLineageEdge.id).where(DataLineageEdge.batch_id == batch.id, DataLineageEdge.source_asset_id == source.id, DataLineageEdge.source_field == field, DataLineageEdge.target_asset_id == target.id, DataLineageEdge.target_field == field))
            if exists:
                continue
            is_encrypted = field.casefold() in encrypted
            session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, source_field=field, target_asset_id=target.id, target_field=field, transformation_type="expression" if is_encrypted else "direct", expression="SM4_ENCRYPT" if is_encrypted else None, evidence={"sm4_batch_id": str(job.id), "sm4_key_fingerprint": job.sm4_key_fingerprint}, confidence=1.0, review_required=False))
    batch.context = {**dict(batch.context or {}), "secured_asset_ids": secured_ids}
    return secured_ids


def _classify_asset_in_session(session, asset):
    rules = session.scalars(select(DataClassificationRule).where(DataClassificationRule.status == "active").order_by(DataClassificationRule.priority)).all()
    fields = []
    for column in (asset.schema_contract or {}).get("columns") or []:
        field = str(column.get("name") or column.get("column_name") or column.get("target") or "")
        matched = next((rule for rule in rules if _classification_matches(rule.match_config or {}, asset, field, column)), None)
        fields.append({"field": field, "classification": matched.classification if matched else "internal", "protection_action": matched.protection_action if matched else "none", "rule_id": str(matched.id) if matched else None, "auto_apply": bool(matched.auto_apply) if matched else False})
    asset.classification_summary = {"fields": fields, "classified_at": app_now().isoformat()}


def _reverse_plan_in_session(session, standard_asset_id):
    standard = session.get(DataAsset, standard_asset_id)
    sensitive = {item.get("field") for item in (standard.classification_summary or {}).get("fields") or [] if item.get("protection_action") == "sm4" and item.get("auto_apply")}
    if not sensitive: return False
    return bool(session.scalar(select(DataLineageEdge.id).where(DataLineageEdge.target_asset_id == standard_asset_id, DataLineageEdge.target_field.in_(sensitive), DataLineageEdge.review_required.is_(False))))


def _queue_restore(session, pipeline: DataAutomationPipeline, batch: DataAutomationBatch, template: RecoveryTask) -> None:
    options = json.loads(json.dumps(template.options or {}))
    professional = options.get("professional_flow") or {}
    source_name = _dump_pattern([item["relative_path"] for item in batch.source_files])
    professional.setdefault("import_source", {})["mode"] = "direct"
    professional["import_source"]["manual_dumpfile"] = source_name
    options["professional_flow"] = professional
    options["auto_confirm"] = True
    options["data_automation"] = {"pipeline_id": str(pipeline.id), "batch_id": str(batch.id), "source_fingerprint": batch.source_fingerprint}
    task = RecoveryTask(remote_host=template.remote_host, remote_port=template.remote_port, remote_user=template.remote_user, remote_password_enc=template.remote_password_enc, remote_directory=template.remote_directory, target_connection=template.target_connection, target_admin_user=template.target_admin_user, target_admin_password_enc=template.target_admin_password_enc, options=options, state="created")
    session.add(task); session.flush()
    batch.restore_task_id = task.id; batch.state = "restore_queued"; batch.resume_from_stage = None; batch.error_message = None; batch.message = f"Oracle 恢复任务已创建：{source_name}"; _event(session, batch, "restore_queued", "success", batch.message, {"task_id": str(task.id), "dumpfile": source_name})
    session.commit()
    from recovery_service.workers.celery_app import celery_app
    celery_app.send_task("recovery.run_task", args=[str(task.id)], kwargs={"volume_group_index": 0}, queue=get_settings().celery_oracle_queue)


def _list_template_dmp_files(template: RecoveryTask, pattern: str) -> list[dict[str, Any]]:
    options = template.options or {}; professional = options.get("professional_flow") or {}; source = professional.get("source") or {}
    host = RemoteHost(host=str(source.get("host") or template.remote_host), port=int(source.get("port") or template.remote_port or 22), username=str(source.get("user") or template.remote_user), password=decrypt_secret(str(source.get("password") or template.remote_password_enc), get_settings().credential_encryption_key))
    directory = str(source.get("directory") or template.remote_directory)
    async def read():
        client = AsyncSSHClient(host)
        try: return await client.list_files_recursive(directory, max_files=10000)
        finally: await client.close()
    files = asyncio.run(read())
    clean_pattern = pattern or "*.dmp"
    return [{**item, "remote_path": item.get("remote_path") or f"{directory.rstrip('/')}/{item['relative_path']}"} for item in files if item["relative_path"].lower().endswith(".dmp") and fnmatch.fnmatch(item["relative_path"], clean_pattern)]


def _group_dump_files(files: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        key = re.sub(r"(?i)([_-])\d{2,4}(?=\.dmp$)", r"\1%U", item["relative_path"])
        groups.setdefault(key, []).append(item)
    return [sorted(items, key=lambda value: value["relative_path"]) for _key, items in sorted(groups.items())]


def _dump_pattern(names: list[str]) -> str:
    if len(names) == 1: return names[0]
    pattern = re.sub(r"(?i)([_-])\d{2,4}(?=\.dmp$)", r"\1%U", names[0])
    return pattern if "%U" in pattern else names[0]


def _source_fingerprint(group: list[dict[str, Any]]) -> str:
    value = json.dumps([{key: item.get(key) for key in ("relative_path", "size_bytes", "modified_epoch")} for item in group], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_pipeline_references(session, row: DataAutomationPipeline) -> None:
    if row.restore_template_task_id and not session.get(RecoveryTask, row.restore_template_task_id): raise ValueError("恢复模板任务不存在。")
    if row.data_sync_node_id:
        from recovery_service.core.models.task import DataPlatformNode
        node = session.get(DataPlatformNode, row.data_sync_node_id)
        if not node or node.node_type != "data_sync": raise ValueError("数据同步组件不存在或类型不正确。")
    if row.standard_workflow_version_id and not session.get(DataPlatformWorkflowVersion, row.standard_workflow_version_id): raise ValueError("标准化工作流版本不存在。")
    if row.sm4_task_definition_id and not session.get(DorisSm4TaskDefinition, row.sm4_task_definition_id): raise ValueError("SM4 任务定义不存在。")


def _event(session, batch, event_type, status, message, payload=None):
    session.add(DataAutomationEvent(batch_id=batch.id, pipeline_id=batch.pipeline_id, stage=batch.state, event_type=event_type, status=status, message=message, payload=payload or {}))


def _classification_matches(config, asset, field, column):
    field_pattern = str(config.get("field_pattern") or "*")
    if not fnmatch.fnmatch(field.casefold(), field_pattern.casefold()): return False
    if config.get("business_domain") and str(config["business_domain"]).casefold() != str(asset.business_domain or "").casefold(): return False
    types = [str(item).casefold() for item in config.get("data_types") or []]
    return not types or _normalize_type(column.get("type") or column.get("data_type")) in types


def _blueprint_score(signature: str, columns: list[dict[str, Any]], blueprint: DataAutomationBlueprint) -> tuple[float, str]:
    if blueprint.schema_signature == signature:
        return 1.0, "Schema 指纹与历史蓝图完全一致。"
    expected = list((blueprint.schema_contract or {}).get("columns") or [])
    if not columns or not expected:
        return 0.0, "缺少可比较的字段合同。"
    actual_map = {str(item.get("name") or item.get("column_name") or "").casefold(): _normalize_type(item.get("type") or item.get("data_type")) for item in columns}
    expected_map = {str(item.get("name") or item.get("column_name") or "").casefold(): _normalize_type(item.get("type") or item.get("data_type")) for item in expected}
    common = set(actual_map) & set(expected_map)
    name_score = len(common) / max(len(set(actual_map) | set(expected_map)), 1)
    type_score = sum(1 for name in common if actual_map[name] == expected_map[name]) / max(len(expected_map), 1)
    score = round(name_score * 0.6 + type_score * 0.4, 4)
    return score, f"字段名称相似度 {name_score:.1%}，类型一致度 {type_score:.1%}。"


def _normalize_type(value): return re.sub(r"\s+", "", str(value or "unknown").strip().casefold())
def _required(value, label):
    clean = str(value or "").strip()
    if not clean: raise ValueError(f"{label}不能为空。")
    return clean
def _optional(value, limit):
    clean = str(value or "").strip()
    return clean[:limit] if clean else None
def _uuid_or_none(value): return uuid.UUID(str(value)) if value else None
def _bounded_int(value, default, minimum, maximum): return max(minimum, min(maximum, int(value if value is not None else default)))
def _pipeline_dict(row): return {"pipeline_id": str(row.id), "name": row.name, "status": row.status, "auto_watch_enabled": row.auto_watch_enabled, "watch_interval_minutes": row.watch_interval_minutes, "stable_wait_seconds": row.stable_wait_seconds, "file_pattern": row.file_pattern, "restore_template_task_id": str(row.restore_template_task_id) if row.restore_template_task_id else None, "data_sync_node_id": str(row.data_sync_node_id) if row.data_sync_node_id else None, "standard_workflow_version_id": str(row.standard_workflow_version_id) if row.standard_workflow_version_id else None, "sm4_task_definition_id": str(row.sm4_task_definition_id) if row.sm4_task_definition_id else None, "business_domain": row.business_domain, "standard_target": row.standard_target or {}, "config": row.config or {}, "last_scan_at": row.last_scan_at, "next_scan_at": row.next_scan_at, "created_at": row.created_at, "updated_at": row.updated_at}
def _batch_dict(row): return {"batch_id": str(row.id), "pipeline_id": str(row.pipeline_id), "blueprint_id": str(row.blueprint_id) if row.blueprint_id else None, "blueprint_version": row.blueprint_version, "state": row.state, "resume_from_stage": row.resume_from_stage, "source_path": row.source_path, "source_files": row.source_files or [], "source_fingerprint": row.source_fingerprint, "restore_task_id": str(row.restore_task_id) if row.restore_task_id else None, "sync_run_id": str(row.sync_run_id) if row.sync_run_id else None, "standard_run_id": str(row.standard_run_id) if row.standard_run_id else None, "encryption_batch_id": str(row.encryption_batch_id) if row.encryption_batch_id else None, "restored_target": row.restored_target or {}, "raw_target": row.raw_target or {}, "standard_target": row.standard_target or {}, "schema_signature": row.schema_signature, "match_confidence": row.match_confidence, "match_reason": row.match_reason, "message": row.message, "error_message": row.error_message, "created_at": row.created_at, "updated_at": row.updated_at, "finished_at": row.finished_at}
def _event_dict(row): return {"event_id": str(row.id), "stage": row.stage, "event_type": row.event_type, "status": row.status, "message": row.message, "payload": row.payload or {}, "created_at": row.created_at}
def _blueprint_dict(row): return {"blueprint_id": str(row.id), "pipeline_id": str(row.pipeline_id), "version_no": row.version_no, "name": row.name, "status": row.status, "source_rule": row.source_rule or {}, "schema_signature": row.schema_signature, "schema_contract": row.schema_contract or {}, "execution_snapshot": row.execution_snapshot or {}, "auto_execute": row.auto_execute, "created_at": row.created_at}
def _asset_dict(row): return {"asset_id": str(row.id), "connection_id": str(row.connection_id) if row.connection_id else None, "connection_name": row.connection_name, "engine": row.engine, "catalog": row.catalog, "database": row.database, "table_name": row.table_name, "layer": row.layer, "business_domain": row.business_domain, "schema_signature": row.schema_signature, "schema_contract": row.schema_contract or {}, "classification_summary": row.classification_summary or {}, "first_batch_id": str(row.first_batch_id) if row.first_batch_id else None, "last_batch_id": str(row.last_batch_id) if row.last_batch_id else None, "created_at": row.created_at, "updated_at": row.updated_at}
def _lineage_dict(row): return {"edge_id": str(row.id), "batch_id": str(row.batch_id) if row.batch_id else None, "source_asset_id": str(row.source_asset_id), "source_field": row.source_field, "target_asset_id": str(row.target_asset_id), "target_field": row.target_field, "transformation_type": row.transformation_type, "expression": row.expression, "workflow_version_id": str(row.workflow_version_id) if row.workflow_version_id else None, "node_key": row.node_key, "evidence": row.evidence or {}, "confidence": row.confidence, "review_required": row.review_required, "created_at": row.created_at}
def _classification_rule_dict(row): return {"rule_id": str(row.id), "name": row.name, "status": row.status, "priority": row.priority, "match_config": row.match_config or {}, "classification": row.classification, "protection_action": row.protection_action, "auto_apply": row.auto_apply, "version_no": row.version_no, "created_at": row.created_at}
