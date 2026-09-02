"""Human-approved plans. Model output is a suggestion, never execution authority."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import PurePosixPath
import uuid

import httpx
from sqlalchemy import select

from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    AssistantPlan, DataAutomationBatch, DataAutomationPipeline, DataPlatformNode,
    DataPlatformWorkflowVersion, DorisSm4TaskDefinition, RecoveryTask,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services import data_automation as automation
from recovery_service.services.doris_encryption import sm4_task_definition_snapshot
from recovery_service.settings import get_settings


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                                    separators=(",", ":")).encode()).hexdigest()


def _id(value):
    return uuid.UUID(str(value))


def restore_hash(template):
    return digest({key: getattr(template, key) for key in (
        "remote_host", "remote_port", "remote_user", "remote_password_enc", "remote_directory",
        "target_connection", "target_admin_user", "target_admin_password_enc", "options")})


def catalog():
    with get_sync_session_factory()() as session:
        pipelines = session.scalars(select(DataAutomationPipeline).where(
            DataAutomationPipeline.status != "archived").order_by(DataAutomationPipeline.name).limit(200)).all()
        tasks = session.scalars(select(DorisSm4TaskDefinition).where(
            DorisSm4TaskDefinition.archived_at.is_(None)).order_by(DorisSm4TaskDefinition.name).limit(200)).all()
        return {"pipelines": [{"id": str(p.id), "name": p.name,
                "business_domain": p.business_domain, "standard_target": _target_names(p.standard_target),
                "sm4_task_id": str(p.sm4_task_definition_id) if p.sm4_task_definition_id else None,
                "configured": bool(p.restore_template_task_id and p.data_sync_node_id and p.standard_workflow_version_id)}
                for p in pipelines],
                "sm4_tasks": [{"id": str(t.id), "name": t.name, "database": t.database,
                               "revision": t.revision, "table_count": len(t.tables or [])} for t in tasks],
                "harness_configured": bool(get_settings().harness_bridge_url and get_settings().harness_bridge_token),
                "encryption_scope": "所选全库加密任务的全部已保存表和字段；不自动换密钥"}


def _target_names(target):
    target = target or {}
    return [{"database": x.get("database"), "table_name": x.get("table_name")}
            for x in target.get("assets", [target]) if x.get("database")]


def interpret(message, pipeline_id=None, sm4_task_id=None, actor=None):
    settings = get_settings()
    if not settings.harness_bridge_url or not settings.harness_bridge_token:
        raise ValueError("Harness 尚未配置。请配置 HARNESS_BRIDGE_URL/TOKEN；也可显式选择路径和文件生成计划。")
    data = catalog()
    # Only approved metadata, never options, connection secrets, SQL or row samples.
    payload = {"message": message, "pipelines": data["pipelines"], "sm4_tasks": data["sm4_tasks"],
               "pipeline_id": pipeline_id, "sm4_task_id": sm4_task_id}
    try:
        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
            response = client.post(settings.harness_bridge_url.rstrip("/") + "/interpret", json=payload,
                                   headers={"Authorization": "Bearer " + settings.harness_bridge_token})
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Harness 服务不可用或响应无效；未创建、未执行业务任务。") from exc
    if not isinstance(result, dict):
        raise ValueError("Harness 返回格式错误。")
    allowed = {p["id"] for p in data["pipelines"]}
    selected = str(pipeline_id or result.get("pipeline_id") or "")
    if not selected or not result.get("file_name"):
        return {"reply": str(result.get("reply") or "请明确 DMP 文件名和目标处理路径。")[:2000], "plan": None}
    if selected not in allowed:
        raise ValueError("模型选择了不存在的处理路径，已阻断。")
    plan = prepare_plan(selected, str(result["file_name"]), sm4_task_id or result.get("sm4_task_id"), actor)
    return {"reply": "已生成待确认计划，尚未执行。请核对文件、写入范围和全库加密任务。", "plan": plan}


def _load_contract(session, pipeline_id, sm4_task_id=None):
    pipeline = session.get(DataAutomationPipeline, _id(pipeline_id))
    if not pipeline or pipeline.status == "archived":
        raise ValueError("处理路径不存在或已归档。")
    template = session.get(RecoveryTask, pipeline.restore_template_task_id) if pipeline.restore_template_task_id else None
    node = session.get(DataPlatformNode, pipeline.data_sync_node_id) if pipeline.data_sync_node_id else None
    version = session.get(DataPlatformWorkflowVersion, pipeline.standard_workflow_version_id) if pipeline.standard_workflow_version_id else None
    task_id = sm4_task_id or pipeline.sm4_task_definition_id
    task = session.get(DorisSm4TaskDefinition, _id(task_id)) if task_id else None
    if not template or template.state not in {"succeeded", "succeeded_with_warnings"}:
        raise ValueError("需要绑定已成功验证的 Oracle DMP 恢复模板。")
    if not node or node.status != "active" or node.node_type != "data_sync":
        raise ValueError("需要绑定有效的数据同步组件。")
    if not version or version.channel != "prod" or version.status != "online":
        raise ValueError("需要绑定已上线的 Doris SQL 生产工作流，不能运行开发草稿。")
    release = version.release_snapshot or {"nodes": version.nodes, "edges": version.edges}
    kinds = {n.get("node_type") for n in release.get("nodes", [])}
    if "doris_sql" not in kinds or not kinds.issubset({"doris_sql", "manual"}):
        raise ValueError("标准化版本必须为 Doris SQL 工作流；不能夹带其他同步、触发或加密步骤。")
    if not task or task.archived_at or not task.tables:
        raise ValueError("请选择已保存且非空的全库 SM4 加密任务。")
    if not _target_names(pipeline.standard_target):
        raise ValueError("流水线尚未登记标准目标库表，请先配置 standard_target。")
    snapshot = {"pipeline_id": str(pipeline.id), "pipeline_name": pipeline.name,
                "restore_template_task_id": str(template.id), "restore_hash": restore_hash(template),
                "restore_target": template.target_connection,
                "data_sync_node_id": str(node.id), "sync_config": deepcopy(node.config or {}), "sync_revision": node.revision,
                "standard_workflow_version_id": str(version.id), "workflow_hash": digest(release),
                "workflow_version_no": version.version_no, "workflow_release": deepcopy(release),
                "standard_target": deepcopy(pipeline.standard_target), "business_domain": pipeline.business_domain,
                "sm4": sm4_task_definition_snapshot(task), "stable_wait_seconds": max(10, pipeline.stable_wait_seconds or 60)}
    return snapshot, template


def _files(template, file_name):
    name = file_name.strip()
    path = PurePosixPath(name)
    if not name.lower().endswith(".dmp") or path.is_absolute() or ".." in path.parts or any(c in name for c in "\\*?[]\x00\r\n"):
        raise ValueError("请使用模板目录内的明确 DMP 相对路径，不允许绝对路径或通配符。")
    files = automation._list_template_dmp_files(template, "*.dmp")
    # Exact relative path only: never silently select same basenames in other folders.
    matching = [f for f in files if f["relative_path"] == name]
    if len(matching) != 1:
        raise ValueError("未唯一找到指定 DMP，请检查模板目录和文件相对路径。")
    groups = [g for g in automation._group_dump_files(files) if any(f["relative_path"] == name for f in g)]
    return groups[0]


def prepare_plan(pipeline_id, file_name, sm4_task_id=None, actor=None):
    with get_sync_session_factory()() as session:
        snapshot, template = _load_contract(session, pipeline_id, sm4_task_id)
        files = _files(template, file_name)
        snapshot["files"] = files
        snapshot["file_name"] = file_name.strip()
        snapshot["source_fingerprint"] = automation._source_fingerprint(files)
        plan = AssistantPlan(pipeline_id=_id(pipeline_id), snapshot=snapshot, plan_hash=digest(snapshot),
                             created_by_username=actor.username if actor else None, state="draft")
        session.add(plan)
        session.commit()
        return _plan_dict(plan)


def _plan_dict(plan):
    s = plan.snapshot
    config = s["sync_config"]
    selected = {str(item) for item in config.get("selected_tables") or [] if str(item).strip()}
    mappings = config.get("table_mappings") or [config]
    sync_tables = [{"source_table": m.get("source_table"),
                    "target_database": m.get("target_database") or config.get("target_database"),
                    "target_table": m.get("target_table") or m.get("source_table"),
                    "write_mode": config.get("write_mode") or ("append" if config.get("table_mappings") else "full_replace")}
                   for m in mappings if m.get("enabled", True) and
                   (not selected or str(m.get("id") or m.get("source_table")) in selected or str(m.get("source_table")) in selected)]
    return {"plan_id": str(plan.id), "plan_hash": plan.plan_hash, "state": plan.state,
            "batch_id": str(plan.batch_id) if plan.batch_id else None, "created_at": plan.created_at,
            "pipeline_name": s["pipeline_name"], "files": s["files"], "restore_target": s["restore_target"],
            "standard_target": _target_names(s["standard_target"]),
            "workflow_version_id": s["standard_workflow_version_id"], "workflow_version_no": s["workflow_version_no"],
            "sync_tables": sync_tables, "sync_write_mode": config.get("write_mode"), "sm4": s["sm4"],
            "sql_steps": [{"name": n.get("name") or n.get("key"),
                           **{k: (n.get("config") or {}).get(k) for k in ("connection_id", "database", "sql")}}
                          for n in s["workflow_release"].get("nodes", []) if n.get("node_type") == "doris_sql"],
            "warnings": ["复用已有 SQL 与写入策略，可能清空/覆盖 ODS、DWD 或加密结果表，请在确认前核对原任务 SQL。",
                         "加密范围来自已保存全库任务，与 DMP/DWD 范围可能不同；确认意味着授权执行列出的全部加密表。",
                         "不会新建或轮换密钥，按加密批次创建时的当前有效版本绑定。"]}


def get_plan(plan_id):
    with get_sync_session_factory()() as session:
        plan = session.get(AssistantPlan, _id(plan_id))
        if not plan:
            raise KeyError("助手计划不存在。")
        result = _plan_dict(plan)
        if plan.batch_id:
            batch = session.get(DataAutomationBatch, plan.batch_id)
            result["batch"] = {k: getattr(batch, k) for k in ("state", "message", "error_message", "restore_task_id", "sync_run_id", "standard_run_id", "encryption_batch_id")}
            result["events"] = [{"stage": e["stage"], "status": e["status"], "message": e["message"], "created_at": e["created_at"]}
                                for e in automation.get_batch(batch.id)["events"]][-100:]
        return result


def list_plans():
    with get_sync_session_factory()() as session:
        return [{"plan_id": str(p.id), "state": batch_state or p.state, "batch_id": str(p.batch_id) if p.batch_id else None,
                 "pipeline_name": p.snapshot["pipeline_name"], "file_name": p.snapshot["file_name"], "created_at": p.created_at}
                for p, batch_state in session.execute(select(AssistantPlan, DataAutomationBatch.state)
                    .outerjoin(DataAutomationBatch, AssistantPlan.batch_id == DataAutomationBatch.id)
                    .order_by(AssistantPlan.created_at.desc()).limit(100)).all()]


def confirm_plan(plan_id, plan_hash, actor):
    with get_sync_session_factory()() as session:
        plan = session.scalar(select(AssistantPlan).where(AssistantPlan.id == _id(plan_id)).with_for_update())
        if not plan or plan.plan_hash != plan_hash:
            raise ValueError("计划不存在或确认哈希不匹配，请重新读取。")
        if plan.batch_id:
            return _plan_dict(plan)
        if plan.state != "draft" or app_now() - plan.created_at > timedelta(hours=24):
            raise ValueError("计划已失效，请重新规划。")
        current, template = _load_contract(session, plan.pipeline_id, plan.snapshot["sm4"]["task_definition_id"])
        expected = {k: v for k, v in plan.snapshot.items() if k not in {"files", "file_name", "source_fingerprint"}}
        if digest(current) != digest(expected):
            raise ValueError("原任务/版本/加密范围已变化，请重新生成计划确认。")
        files = _files(template, plan.snapshot["file_name"])
        if automation._source_fingerprint(files) != plan.snapshot["source_fingerprint"]:
            raise ValueError("DMP 文件已变化，请重新生成计划。")
        # Serialize plans using this route; the existing unique file key is the final fence.
        session.scalar(select(DataAutomationPipeline).where(DataAutomationPipeline.id == plan.pipeline_id).with_for_update())
        existing = session.scalar(select(DataAutomationBatch).where(
            DataAutomationBatch.pipeline_id == plan.pipeline_id,
            DataAutomationBatch.state.not_in({"completed", "failed", "blocked", "partial", "cancelled"})))
        if existing:
            raise ValueError("该处理路径已有在途批次，不能并发提交。")
        prior = session.scalar(select(DataAutomationBatch).where(DataAutomationBatch.pipeline_id == plan.pipeline_id,
                              DataAutomationBatch.source_fingerprint == plan.snapshot["source_fingerprint"]))
        if prior:
            raise ValueError(f"该文件已有批次 {prior.id}，请查看原运行，不重复提交。")
        batch = DataAutomationBatch(pipeline_id=plan.pipeline_id, state="assistant_waiting_file",
                    source_path=files[0]["remote_path"], source_files=files,
                    source_fingerprint=plan.snapshot["source_fingerprint"], source_observed_at=plan.created_at,
                    context={"assistant_plan_id": str(plan.id), "assistant_snapshot": deepcopy(plan.snapshot),
                             "actor_username": actor.username}, message="已确认，等待文件稳定性复核。")
        session.add(batch)
        session.flush()
        plan.batch_id = batch.id
        plan.state = "confirmed"
        automation._event(session, batch, "assistant_confirmed", "success", "用户已确认完整范围；后台将依次执行四阶段。")
        session.commit()
        return _plan_dict(plan)
