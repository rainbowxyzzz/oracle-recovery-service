"""Only assistant-owned batches use this four-stage execution path."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

from sqlalchemy import select, update

from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    AssistantPlan, DataAutomationBatch, DataPlatformComponentRun, DataPlatformWorkflowRun,
    DataPlatformWorkflowVersion, DorisSm4BatchJob, RecoveryTask, DataAsset, DataLineageEdge,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services import data_automation as auto
from recovery_service.services.harness_assistant import _files, restore_hash
from recovery_service.services.auth import AuthContext


def sync_config_for_batch(session, batch_id, node_id, fallback):
    batch = session.get(DataAutomationBatch, uuid.UUID(str(batch_id))) if batch_id else None
    snapshot = (batch.context or {}).get("assistant_snapshot") if batch else None
    if snapshot:
        if snapshot["data_sync_node_id"] != str(node_id):
            raise ValueError("助手批次绑定的同步任务不匹配。")
        return deepcopy(snapshot["sync_config"])
    return fallback


def release_for_run(session, run, fallback):
    plan_id = (run.trigger_context or {}).get("assistant_plan_id")
    if not plan_id:
        return fallback
    plan = session.get(AssistantPlan, uuid.UUID(plan_id))
    batch = session.get(DataAutomationBatch, plan.batch_id) if plan and plan.batch_id else None
    if not batch or batch.standard_run_id != run.id or plan.snapshot["standard_workflow_version_id"] != str(run.version_id):
        raise ValueError("助手标准化运行与已确认计划不匹配。")
    return deepcopy(plan.snapshot["workflow_release"])


def _event(session, batch, stage, message, status="success"):
    batch.message = message
    batch.updated_at = app_now()
    auto._event(session, batch, stage, status, message)


def _claim(session, batch, next_state, *, commit=True):
    original = batch.state
    claimed = session.execute(update(DataAutomationBatch).where(
        DataAutomationBatch.id == batch.id, DataAutomationBatch.state == original
    ).values(state=next_state, updated_at=app_now()).execution_options(synchronize_session=False))
    if commit:
        session.commit()
    if claimed.rowcount != 1:
        session.refresh(batch)
        return False
    session.refresh(batch)
    return True


def advance(session, batch):
    snap = (batch.context or {})["assistant_snapshot"]
    pipeline = SimpleNamespace(id=batch.pipeline_id, standard_target=snap["standard_target"],
        standard_workflow_version_id=uuid.UUID(snap["standard_workflow_version_id"]), business_domain=snap["business_domain"])
    state = batch.state
    actor = AuthContext(None, batch.context.get("actor_username"), "admin", "assistant", {})
    if state.startswith("assistant_submitting_"):
        # No automatic re-dispatch: the previous process may have submitted successfully.
        return False
    if state == "assistant_waiting_file":
        if (app_now() - batch.source_observed_at).total_seconds() < snap["stable_wait_seconds"]:
            return False
        template = session.get(RecoveryTask, uuid.UUID(snap["restore_template_task_id"]))
        if not template or restore_hash(template) != snap["restore_hash"]:
            raise ValueError("恢复模板已变更，已阻断；请重新核对模板。")
        files = _files(template, snap["file_name"])
        if auto._source_fingerprint(files) != batch.source_fingerprint:
            raise ValueError("DMP 在确认后发生变化，已阻断。")
        if not _claim(session, batch, "assistant_submitting_restore"):
            return False
        try:
            # This existing helper records task ID and commits before dispatch.
            auto._queue_restore(session, pipeline, batch, template)
        except Exception:
            session.refresh(batch)
            batch.state = "assistant_submitting_restore"
            _event(session, batch, "dispatch_unknown", "恢复派发结果未知，请核对关联任务；不会自动重复派发。", "warning")
        return True
    if state in {"restore_queued", "restoring"}:
        task = session.get(RecoveryTask, batch.restore_task_id)
        if not task:
            raise ValueError("关联恢复任务不存在。")
        if task.state in {"created", "policy_running", "importing", "correcting", "validating", "stopping"}:
            return False
        if task.state not in {"succeeded", "succeeded_with_warnings"}:
            return _failed(session, batch, "restore", "Oracle 恢复未成功，请查看恢复任务日志。")
        metadata = task.metadata_snapshot or {}
        schema = metadata.get("schema") or metadata.get("username") or (metadata.get("target") or {}).get("schema")
        if not schema:
            return _failed(session, batch, "restore", "Oracle 未返回真实目标 Schema，禁止继续同步。")
        if not _claim(session, batch, "assistant_sync_ready", commit=False):
            return False
        batch.restored_target = {"connection": task.target_connection, "schema": schema}
        _event(session, batch, "restore_succeeded", "Oracle 还原成功，已记录真实 Schema。")
        return True
    if state == "assistant_sync_ready":
        if not _claim(session, batch, "assistant_submitting_sync"):
            return False
        try:
            from recovery_service.services.data_platform import submit_component_task_run
            result = submit_component_task_run(uuid.UUID(snap["data_sync_node_id"]),
                {"pipeline_batch_id": str(batch.id), "restored_target": batch.restored_target}, actor=actor)
            batch.sync_run_id = uuid.UUID(result["run_id"])
            batch.state = "sync_queued"
            _event(session, batch, "sync_queued", "已提交 Oracle→Doris ODS 同步。")
        except Exception:
            _event(session, batch, "dispatch_unknown", "同步提交失败或结果未知，请核对组件运行记录；不会自动重试写入。", "warning")
        return True
    if state in {"sync_queued", "syncing"}:
        run = session.get(DataPlatformComponentRun, batch.sync_run_id)
        if not run:
            raise ValueError("同步运行不存在。")
        if run.status in {"queued", "running"}:
            return False
        if run.status not in {"succeeded", "success"}:
            return _failed(session, batch, "sync", "ODS 同步未全部成功，禁止更新 DWD。")
        if not _claim(session, batch, "assistant_standard_ready", commit=False):
            return False
        ids = auto._record_sync_assets(session, pipeline, batch, run)
        batch.raw_target = {"asset_ids": [str(x) for x in ids]}
        _event(session, batch, "sync_succeeded", "ODS 同步成功，已登记血缘。")
        return True
    if state == "assistant_standard_ready":
        version = session.get(DataPlatformWorkflowVersion, uuid.UUID(snap["standard_workflow_version_id"]))
        if not version or version.status != "online":
            return _failed(session, batch, "standard", "已确认的 SQL 生产版本已下线，禁止执行。")
        if not _claim(session, batch, "assistant_submitting_standard"):
            return False
        batch.standard_run_id = uuid.uuid4()
        session.commit()
        try:
            from recovery_service.services.data_platform import run_version
            run_version(version.id, trigger_type="assistant", run_id=batch.standard_run_id, actor=actor,
                        trigger_context={"assistant_plan_id": batch.context["assistant_plan_id"], "batch_id": str(batch.id)})
            batch.state = "standardize_queued"
            _event(session, batch, "standardize_queued", "已提交冻结生产版本的 ODS→DWD SQL。")
        except Exception:
            _event(session, batch, "dispatch_unknown", "标准化派发结果待核对，请查看关联运行；不会自动重复提交。", "warning")
        return True
    if state in {"standardize_queued", "standardizing"}:
        run = session.get(DataPlatformWorkflowRun, batch.standard_run_id)
        if not run:
            raise ValueError("标准化运行不存在。")
        if run.status in {"queued", "running"}:
            return False
        if run.status != "succeeded":
            return _failed(session, batch, "standard", "DWD SQL 未成功，不提交全库加密。")
        if not _claim(session, batch, "assistant_encryption_ready", commit=False):
            return False
        assets = auto._record_standard_assets(session, pipeline, batch)
        batch.standard_target = {**snap["standard_target"], "asset_ids": [str(a.id) for a in assets]}
        _event(session, batch, "standardize_succeeded", "DWD 标准化成功，下一步按已确认全库任务加密。")
        return True
    if state == "assistant_encryption_ready":
        if not _claim(session, batch, "assistant_submitting_encryption"):
            return False
        try:
            from recovery_service.services.doris_encryption import run_sm4_task_snapshot
            result = run_sm4_task_snapshot(deepcopy(snap["sm4"]), actor=actor)
            batch.encryption_batch_id = result.batch_id
            batch.state = "encrypting"
            _event(session, batch, "encryption_queued", "已按全库任务快照提交 SM4；密钥由原加密模块绑定当前有效版本。")
        except Exception:
            _event(session, batch, "dispatch_unknown", "SM4 提交失败或结果未知，请核对密钥预检与加密批次；不会自动重复加密。", "warning")
        return True
    if state == "encrypting":
        job = session.get(DorisSm4BatchJob, batch.encryption_batch_id)
        if not job:
            raise ValueError("SM4 批次不存在。")
        if job.state in {"queued", "reserved", "running", "stopping"}:
            return False
        if job.state not in {"succeeded", "success"}:
            return _failed(session, batch, "encryption", "全库加密未全部成功，请核对表级结果。")
        if not _claim(session, batch, "completed", commit=False):
            return False
        _record_full_task_secured(session, pipeline, batch, job)
        batch.finished_at = app_now()
        _event(session, batch, "assistant_completed", "DMP 还原、ODS 同步、DWD 更新、全库任务加密均已成功。")
        return True
    return False


def _record_full_task_secured(session, pipeline, batch, job):
    # The full task may include tables outside this batch's ODS/DWD scope.
    secured = []
    for result in job.results or []:
        if result.get("state") not in {"succeeded", "success"} or not result.get("target_table"):
            continue
        source = session.scalar(select(DataAsset).where(DataAsset.connection_id == job.connection_id,
            DataAsset.catalog == "",
            DataAsset.database == job.database, DataAsset.table_name == result.get("table_name"),
            DataAsset.layer.in_(["raw", "standard"])).order_by(DataAsset.updated_at.desc()).limit(1))
        target = auto._upsert_asset(session, connection_id=job.connection_id, catalog="",
            database=result.get("target_database") or job.database, table_name=result["target_table"],
            layer="secured", domain=pipeline.business_domain,
            columns=(source.schema_contract or {}).get("columns", []) if source else [], batch_id=batch.id)
        secured.append(str(target.id))
        if source:
            session.add(DataLineageEdge(batch_id=batch.id, source_asset_id=source.id, target_asset_id=target.id,
                transformation_type="expression", expression="SM4_ENCRYPT",
                evidence={"sm4_batch_id": str(job.id), "sm4_key_fingerprint": job.sm4_key_fingerprint,
                          "encrypted_fields": result.get("columns", [])}, confidence=1.0, review_required=False))
    batch.context = {**batch.context, "secured_asset_ids": secured}


def _failed(session, batch, stage, message):
    batch.state = "failed"
    batch.resume_from_stage = stage
    batch.error_message = message
    _event(session, batch, "assistant_failed", message, "failed")
    return True


def resume(plan_id, acknowledge_partial_writes, actor=None):
    if not acknowledge_partial_writes:
        raise ValueError("请先核对失败阶段的部分写入，并明确确认允许重试。")
    with get_sync_session_factory()() as session:
        plan = session.get(AssistantPlan, uuid.UUID(str(plan_id)))
        batch = session.scalar(select(DataAutomationBatch).where(DataAutomationBatch.id == plan.batch_id).with_for_update()) if plan and plan.batch_id else None
        if not batch or batch.state != "failed":
            raise ValueError("仅允许继续已明确失败的阶段；提交结果未知时须先核对，不能直接重试。")
        ready = {"restore": "assistant_waiting_file", "sync": "assistant_sync_ready",
                 "standard": "assistant_standard_ready", "encryption": "assistant_encryption_ready"}
        if batch.resume_from_stage not in ready:
            raise ValueError("该错误需要重新规划或人工核对。")
        stage = batch.resume_from_stage
        auto._event(session, batch, "assistant_retry", "warning", "管理员已核对部分写入，授权重试失败阶段。",
                    {"actor_username": actor.username if actor else None,
                     "restore_task_id": str(batch.restore_task_id), "sync_run_id": str(batch.sync_run_id),
                     "standard_run_id": str(batch.standard_run_id), "encryption_batch_id": str(batch.encryption_batch_id)})
        batch.state = ready[stage]
        batch.error_message = None
        if actor:
            batch.context = {**batch.context, "actor_username": actor.username}
        session.commit()
    return {"state": "resuming"}
