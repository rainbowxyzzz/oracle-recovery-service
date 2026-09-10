import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recovery_service.api.deps import get_current_actor
from recovery_service.api.v1.data_automation import router
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    Base,
    DataAsset,
    DataAutomationBatch,
    DataAutomationEvent,
    DataAutomationPipeline,
    DataAutomationStageDispatch,
    DatabaseConnectionProfile,
    DataDatabaseLayer,
    DataLineageEdge,
    DataLineageEvent,
    DataPlatformComponentRun,
    DataPlatformComponentRunTable,
    DataPlatformNode,
    DorisSm4BatchJob,
    DorisSm4TaskDefinition,
    RecoveryTask,
)
from recovery_service.services import data_automation as data_automation_service
from recovery_service.services.auth import AuthContext
from recovery_service.services.data_automation import (
    activate_security_orchestration_access,
    build_reverse_encryption_plan,
    build_security_access_plan,
    classify_asset,
    create_blueprint,
    create_classification_rule,
    create_lineage_edge,
    create_pipeline,
    dispatch_pending_stage_tasks,
    enable_security_orchestration,
    get_security_orchestration_ledger,
    get_security_orchestration_readiness,
    ingest_openlineage_event,
    lineage_overview,
    list_database_layers,
    match_batch_blueprint,
    openmetadata_lineage_projection,
    prepare_security_orchestration,
    register_asset,
    scan_pipeline,
    schema_signature,
    submit_security_orchestration_encryption,
    trace_lineage,
    update_pipeline,
    upsert_database_layer,
)


def _factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def _columns(extra=False):
    result = [
        {"name": "ID", "type": "BIGINT", "nullable": False, "key": True},
        {"name": "NAME", "type": "VARCHAR(100)", "nullable": True},
    ]
    if extra:
        result.append({"name": "PHONE", "type": "VARCHAR(32)", "nullable": True})
    return result


def test_schema_signature_is_stable_and_sensitive_to_contract() -> None:
    assert schema_signature(_columns()) == schema_signature(_columns())
    assert schema_signature(_columns()) != schema_signature(_columns(extra=True))


def test_scan_is_idempotent_and_clones_recovery_template_after_stable_wait() -> None:
    engine, factory = _factory()
    template_id = uuid.uuid4()
    with factory() as session:
        session.add(RecoveryTask(
            id=template_id, remote_host="oracle-host", remote_port=22, remote_user="root",
            remote_password_enc="encrypted", remote_directory="/dmp", target_connection="oracle:1521/ORCLPDB1",
            target_admin_user="SYSTEM", target_admin_password_enc="encrypted",
            options={"professional_flow": {"source": {"host": "oracle-host", "port": 22, "user": "root", "password": "encrypted", "directory": "/dmp"}, "import_source": {"mode": "direct"}}}, state="succeeded",
        ))
        session.commit()
    files = [{"relative_path": "AUTO_PIPE_01.dmp", "remote_path": "/dmp/AUTO_PIPE_01.dmp", "size_bytes": 1024, "modified_epoch": 100.0}]
    fake_task = SimpleNamespace(id="celery-1")
    with (
        patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory),
        patch("recovery_service.services.data_automation._list_template_dmp_files", return_value=files),
        patch("recovery_service.workers.celery_app.celery_app.send_task", return_value=fake_task) as send,
    ):
        pipeline = create_pipeline({"name": "自动 DMP", "restore_template_task_id": template_id, "stable_wait_seconds": 10, "config": {"auto_restore": True}})
        first = scan_pipeline(uuid.UUID(pipeline["pipeline_id"]))
        assert len(first["created_batch_ids"]) == 1
        with factory() as session:
            batch = session.scalar(select(DataAutomationBatch))
            batch.source_observed_at = app_now() - timedelta(seconds=11)
            session.commit()
        second = scan_pipeline(uuid.UUID(pipeline["pipeline_id"]))
        third = scan_pipeline(uuid.UUID(pipeline["pipeline_id"]))
        assert second["queued_batch_ids"] == first["created_batch_ids"]
        assert third["created_batch_ids"] == []
        with factory() as session:
            assert session.query(DataAutomationBatch).count() == 1
            assert session.query(RecoveryTask).count() == 2
            assert session.query(DataAutomationStageDispatch).count() == 1
            cloned = session.get(DataAutomationBatch, uuid.UUID(first["created_batch_ids"][0]))
            assert cloned.state == "restore_queued"
            dispatch_row = session.scalar(select(DataAutomationStageDispatch))
            assert dispatch_row.target_id == cloned.restore_task_id
            assert dispatch_row.status == "dispatched"
            assert dispatch_row.attempts == 1
        send.assert_called_once()
    engine.dispose()


def test_stage_dispatch_failure_is_retried_without_failing_business_batch() -> None:
    engine, factory = _factory()
    with factory() as session:
        pipeline = DataAutomationPipeline(name="审计案件登记安全流")
        session.add(pipeline)
        session.flush()
        task = RecoveryTask(
            remote_host="oracle-host",
            remote_port=22,
            remote_user="root",
            remote_password_enc="encrypted",
            remote_directory="/audit/dmp",
            target_connection="oracle:1521/ORCLPDB1",
            target_admin_user="SYSTEM",
            target_admin_password_enc="encrypted",
            state="created",
        )
        session.add(task)
        session.flush()
        batch = DataAutomationBatch(
            pipeline_id=pipeline.id,
            source_path="/audit/案件登记_20260909.dmp",
            source_files=[],
            source_fingerprint="audit-case-register-dispatch",
            state="restore_queued",
            restore_task_id=task.id,
        )
        session.add(batch)
        session.flush()
        dispatch = DataAutomationStageDispatch(
            batch_id=batch.id,
            pipeline_id=pipeline.id,
            stage="restore",
            target_type="recovery_task",
            target_id=task.id,
            idempotency_key=f"{batch.id}:restore:recovery_task:{task.id}",
            status="pending",
        )
        session.add(dispatch)
        session.commit()
        batch_id = batch.id
        dispatch_id = dispatch.id

    with (
        patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory),
        patch("recovery_service.workers.celery_app.celery_app.send_task", side_effect=RuntimeError("broker unavailable")),
    ):
        first = dispatch_pending_stage_tasks()

    assert first == {"dispatched": 0, "deferred": 0, "failed": 1}
    with factory() as session:
        saved_batch = session.get(DataAutomationBatch, batch_id)
        saved_dispatch = session.get(DataAutomationStageDispatch, dispatch_id)
        assert saved_batch.state == "restore_queued"
        assert saved_dispatch.status == "pending"
        assert saved_dispatch.attempts == 1
        assert "broker unavailable" in saved_dispatch.last_error
        assert data_automation_service._advance_one(session, saved_batch) is False
        assert saved_batch.state == "restore_queued"
        assert "等待可靠派发" in saved_batch.message
        saved_dispatch.next_attempt_at = app_now() - timedelta(seconds=1)
        session.commit()

    fake_task = SimpleNamespace(id="recovery-dispatch-retry")
    with (
        patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory),
        patch("recovery_service.workers.celery_app.celery_app.send_task", return_value=fake_task) as send,
    ):
        second = dispatch_pending_stage_tasks()

    assert second == {"dispatched": 1, "deferred": 0, "failed": 0}
    with factory() as session:
        saved_dispatch = session.get(DataAutomationStageDispatch, dispatch_id)
        assert saved_dispatch.status == "dispatched"
        assert saved_dispatch.attempts == 2
        assert saved_dispatch.celery_task_id == "recovery-dispatch-retry"
    assert send.call_args.kwargs["task_id"] == f"data-automation-{dispatch_id}"

    with factory() as session:
        saved_dispatch = session.get(DataAutomationStageDispatch, dispatch_id)
        saved_dispatch.status = "dispatching"
        saved_dispatch.last_attempt_at = app_now() - timedelta(minutes=3)
        saved_dispatch.celery_task_id = None
        session.commit()
    with (
        patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory),
        patch("recovery_service.workers.celery_app.celery_app.send_task", return_value=fake_task) as resend,
    ):
        third = dispatch_pending_stage_tasks()
    assert third == {"dispatched": 1, "deferred": 0, "failed": 0}
    assert resend.call_args.kwargs["task_id"] == f"data-automation-{dispatch_id}"

    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        ledger = get_security_orchestration_ledger()
    assert ledger["flows"][0]["latest_batch"]["stage_dispatches"][0]["status"] == "dispatched"
    engine.dispose()


def test_blueprint_exact_match_and_medium_match_gate() -> None:
    engine, factory = _factory()
    with factory() as session:
        pipeline = DataAutomationPipeline(name="P")
        session.add(pipeline); session.flush()
        exact_batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/a.dmp", source_files=[], source_fingerprint="a")
        changed_batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/b.dmp", source_files=[], source_fingerprint="b")
        session.add_all([exact_batch, changed_batch]); session.commit()
        pipeline_id, exact_id, changed_id = pipeline.id, exact_batch.id, changed_batch.id
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        blueprint = create_blueprint(pipeline_id, {"name": "客户标准化", "schema_contract": {"columns": _columns()}, "auto_execute": True})
        exact = match_batch_blueprint(exact_id, {"columns": _columns()})
        changed = match_batch_blueprint(changed_id, {"columns": _columns(extra=True)})
    assert exact["matched"] is True and exact["confidence"] == 1.0
    assert exact["blueprint_id"] == blueprint["blueprint_id"]
    assert changed["level"] in {"medium", "low"}
    engine.dispose()


def test_classification_lineage_and_reverse_sm4_plan() -> None:
    engine, factory = _factory()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        raw = register_asset({"engine": "doris", "database": "ODS", "table_name": "CUSTOMER_RAW", "layer": "raw", "columns": _columns(extra=True)})
        standard = register_asset({"engine": "doris", "database": "DWD", "table_name": "CUSTOMER", "layer": "standard", "columns": _columns(extra=True)})
        create_lineage_edge({"source_asset_id": raw["asset_id"], "source_field": "PHONE", "target_asset_id": standard["asset_id"], "target_field": "PHONE", "transformation_type": "rename"})
        create_lineage_edge({"source_asset_id": raw["asset_id"], "source_field": "NAME", "target_asset_id": standard["asset_id"], "target_field": "NAME", "transformation_type": "expression", "expression": "TRIM(NAME)"})
        create_classification_rule({"name": "手机号", "priority": 1, "match_config": {"field_pattern": "*phone*"}, "classification": "highly_sensitive", "protection_action": "sm4", "auto_apply": True})
        classified = classify_asset(uuid.UUID(standard["asset_id"]))
        trace = trace_lineage(uuid.UUID(standard["asset_id"]))
        plan = build_reverse_encryption_plan(uuid.UUID(standard["asset_id"]))
    assert next(item for item in classified["fields"] if item["field"] == "PHONE")["protection_action"] == "sm4"
    assert len(trace["edges"]) == 2
    assert plan["auto_eligible_count"] == 1
    assert plan["suggestions"][0]["source_field"] == "PHONE"
    engine.dispose()


def test_security_access_plan_requires_frozen_contract_and_production_lineage() -> None:
    engine, factory = _factory()
    profile_id = uuid.uuid4(); version_id = uuid.uuid4(); sm4_id = uuid.uuid4(); pipeline_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=profile_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x", database="ODS"))
        session.add(DorisSm4TaskDefinition(id=sm4_id, name="审计证据源表加密", connection_id=profile_id, database="ODS", tables=[{"table_name": "CASE_RAW", "columns": ["PHONE"]}], target_suffix="sm4", coverage_contracts=[{"standard_database": "DWD", "standard_table": "CASE_STANDARD", "standard_field": "PHONE", "source_database": "ODS", "source_table": "CASE_RAW", "source_field": "PHONE", "access_database": "ACCESS"}]))
        session.add(DataAutomationPipeline(id=pipeline_id, name="审计证据安全访问", standard_workflow_version_id=version_id, sm4_task_definition_id=sm4_id, config={"security_access_enabled": True}))
        raw = DataAsset(id=uuid.uuid4(), connection_id=profile_id, engine="doris", catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature="raw-v1", schema_contract={"columns": _columns(extra=True)})
        standard = DataAsset(id=uuid.uuid4(), connection_id=profile_id, engine="doris", catalog="", database="DWD", table_name="CASE_STANDARD", layer="standard", schema_signature="dwd-v1", schema_contract={"columns": _columns(extra=True)}, classification_summary={"fields": [{"field": "PHONE", "protection_action": "sm4", "auto_apply": True}]})
        session.add_all([raw, standard]); session.flush()
        session.add(DataLineageEdge(source_asset_id=raw.id, source_field="PHONE", target_asset_id=standard.id, target_field="PHONE", transformation_type="rename", workflow_version_id=version_id, review_required=False))
        session.commit()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        plan = build_security_access_plan(standard.id, pipeline_id)
    assert plan["ready"] is True
    assert plan["suggestions"][0]["contract"]["access_table"] == "CASE_RAW"
    engine.dispose()


def test_encrypting_batch_waits_for_reserved_sm4_job() -> None:
    engine, factory = _factory()
    with factory() as session:
        pipeline = DataAutomationPipeline(name="审计反向加密")
        session.add(pipeline)
        session.flush()
        job = DorisSm4BatchJob(
            id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            database="AUDIT_RESTORE",
            tables=[],
            state="reserved",
            message="SM4 batch job reserved by scheduler and waiting for worker.",
        )
        batch = DataAutomationBatch(
            pipeline_id=pipeline.id,
            source_path="/audit.dmp",
            source_files=[],
            source_fingerprint="audit-reserved",
            state="encrypting",
            encryption_batch_id=job.id,
        )
        session.add_all([job, batch])
        session.commit()
        assert data_automation_service._advance_one(session, batch) is False
        assert batch.state == "encrypting"
        assert batch.error_message is None
    engine.dispose()


def test_duplicate_sm4_worker_delivery_is_ignored() -> None:
    from recovery_service.services import doris_encryption

    engine, factory = _factory()
    profile_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(
            id=profile_id,
            name="审计 Doris",
            engine="doris",
            host="127.0.0.1",
            port=9030,
            username="root",
            password_enc="x",
            database="审计原始层",
        ))
        session.add(DorisSm4BatchJob(
            id=job_id,
            connection_id=profile_id,
            database="审计原始层",
            tables=[],
            state="reserved",
        ))
        session.commit()

    with patch("recovery_service.services.doris_encryption.get_sync_session_factory", return_value=factory), patch(
        "recovery_service.services.doris_encryption._run_sm4_batch_task",
    ) as execute:
        first = doris_encryption.run_sm4_batch_job(job_id)
        duplicate = doris_encryption.run_sm4_batch_job(job_id)

    assert first["state"] == "running"
    assert duplicate["state"] == "running"
    assert duplicate["message"] == "Duplicate SM4 delivery ignored."
    execute.assert_called_once()
    engine.dispose()


def test_security_orchestration_ledger_enables_only_a_ready_frozen_contract() -> None:
    engine, factory = _factory()
    profile_id = uuid.uuid4(); version_id = uuid.uuid4(); sm4_id = uuid.uuid4(); pipeline_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=profile_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x", database="ODS"))
        session.add(DorisSm4TaskDefinition(id=sm4_id, name="案件登记源表加密", connection_id=profile_id, database="ODS", tables=[{"table_name": "CASE_RAW", "columns": ["PHONE"]}], coverage_contracts=[{"standard_database": "DWD", "standard_table": "CASE_STANDARD", "standard_field": "PHONE", "source_database": "ODS", "source_table": "CASE_RAW", "source_field": "PHONE", "access_database": "ACCESS"}]))
        pipeline = DataAutomationPipeline(id=pipeline_id, name="案件登记数据安全流", standard_workflow_version_id=version_id, sm4_task_definition_id=sm4_id)
        raw = DataAsset(id=uuid.uuid4(), connection_id=profile_id, engine="doris", catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature="raw-v1", schema_contract={"columns": _columns(extra=True)})
        standard = DataAsset(id=uuid.uuid4(), connection_id=profile_id, engine="doris", catalog="", database="DWD", table_name="CASE_STANDARD", layer="standard", schema_signature="dwd-v1", schema_contract={"columns": _columns(extra=True)}, classification_summary={"fields": [{"field": "PHONE", "protection_action": "sm4", "auto_apply": True}]})
        foreign_standard = DataAsset(id=uuid.uuid4(), connection_id=profile_id, engine="doris", catalog="", database="DWD", table_name="OTHER_STANDARD", layer="standard", schema_signature="other-v1", schema_contract={"columns": _columns(extra=True)})
        session.add_all([pipeline, raw, standard, foreign_standard]); session.flush()
        batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/audit.dmp", source_files=[], source_fingerprint="orchestration-ledger", state="standard_ready", standard_target={"asset_ids": [str(standard.id)]})
        session.add(batch); session.flush()
        standard.last_batch_id = batch.id
        session.add(DataLineageEdge(source_asset_id=raw.id, source_field="PHONE", target_asset_id=standard.id, target_field="PHONE", transformation_type="direct", workflow_version_id=version_id, review_required=False))
        session.commit()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        ledger = get_security_orchestration_ledger()
        readiness = get_security_orchestration_readiness(pipeline_id)
        with pytest.raises(ValueError, match="不属于该业务数据流"):
            get_security_orchestration_readiness(pipeline_id, foreign_standard.id)
        with pytest.raises(ValueError, match="必须显式确认"):
            enable_security_orchestration(pipeline_id, standard.id, confirm=False)
        prepared = prepare_security_orchestration(pipeline_id, standard.id, confirm=True, actor=SimpleNamespace(username="audit-admin"))
        enabled = enable_security_orchestration(pipeline_id, standard.id, confirm=True, actor=SimpleNamespace(username="audit-admin"))
    assert ledger["flows"][0]["standard_asset_id"] == str(standard.id)
    assert readiness["plan"]["ready"] is True
    assert prepared["activation_batch_id"] == str(batch.id)
    assert enabled["security_orchestration_enabled"] is True
    assert enabled["activation_batch_id"] == str(batch.id)
    with factory() as session:
        saved = session.get(DataAutomationPipeline, pipeline_id)
        assert saved.config["security_access_enabled"] is True
        assert saved.config["auto_encryption_enabled"] is True
        assert saved.config["security_orchestration"]["standard_asset_id"] == str(standard.id)
        events = session.scalars(select(DataAutomationEvent.event_type).where(DataAutomationEvent.batch_id == batch.id)).all()
        assert "security_orchestration_enabled" in events and "encryption_planned" in events
        assert session.get(DataAutomationBatch, batch.id).state == "encryption_ready"
    engine.dispose()


def test_security_orchestration_routes_keep_read_and_explicit_execute_permissions() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_actor] = lambda: AuthContext(None, "audit-admin", "admin", "bearer", {})
    client = TestClient(app)
    pipeline_id = uuid.uuid4(); asset_id = uuid.uuid4()
    with (
        patch("recovery_service.api.v1.data_automation.service.get_security_orchestration_ledger", return_value={"flows": [], "summary": {}}),
        patch("recovery_service.api.v1.data_automation.service.get_security_orchestration_readiness", return_value={"plan": {"ready": True}}),
        patch("recovery_service.api.v1.data_automation.service.prepare_security_orchestration", return_value={"activation_batch_id": str(uuid.uuid4())}) as prepare,
        patch("recovery_service.api.v1.data_automation.service.submit_security_orchestration_encryption", return_value={"state": "encrypting"}) as submit,
        patch("recovery_service.api.v1.data_automation.service.activate_security_orchestration_access", return_value={"state": "completed"}) as activate,
        patch("recovery_service.api.v1.data_automation.service.enable_security_orchestration", return_value={"security_orchestration_enabled": True}) as enable,
    ):
        assert client.get("/data-automation/orchestration/ledger").status_code == 200
        assert client.get(f"/data-automation/orchestration/pipelines/{pipeline_id}/readiness?standard_asset_id={asset_id}").json()["plan"]["ready"] is True
        assert client.post(f"/data-automation/orchestration/pipelines/{pipeline_id}/prepare-security", json={"standard_asset_id": str(asset_id), "confirm": True}).status_code == 200
        assert client.post(f"/data-automation/orchestration/batches/{asset_id}/submit-encryption", json={"confirm": True}).status_code == 200
        assert client.post(f"/data-automation/orchestration/batches/{asset_id}/activate-security-access", json={"confirm": True}).status_code == 200
        response = client.post(f"/data-automation/orchestration/pipelines/{pipeline_id}/enable", json={"standard_asset_id": str(asset_id), "confirm": True})
    assert response.status_code == 200
    assert prepare.call_args.kwargs["confirm"] is True
    assert submit.call_args.kwargs["confirm"] is True and activate.call_args.kwargs["confirm"] is True
    assert enable.call_args.kwargs["confirm"] is True


def test_security_orchestration_can_submit_and_activate_the_explicit_steps() -> None:
    engine, factory = _factory()
    with factory() as session:
        pipeline = DataAutomationPipeline(name="审计安全流", config={"security_access_enabled": True})
        session.add(pipeline); session.flush()
        batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="security://audit", source_files=[], source_fingerprint="security-submit", state="encryption_ready", context={"encryption_standard_asset_id": str(uuid.uuid4())})
        session.add(batch); session.commit(); batch_id = batch.id
    queued_job_id = uuid.uuid4()
    queued_job = SimpleNamespace(id=queued_job_id, state="queued")
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory), patch(
        "recovery_service.services.data_automation._prepare_automation_encryption_job",
        return_value=(queued_job, [{"table_name": "案件登记原始表", "columns": ["联系电话"]}]),
    ), patch("recovery_service.services.data_automation.dispatch_pending_stage_tasks"):
        submitted = submit_security_orchestration_encryption(batch_id, confirm=True)
    assert submitted["state"] == "encrypting"
    with factory() as session:
        batch = session.get(DataAutomationBatch, batch_id)
        session.add(DorisSm4BatchJob(id=queued_job_id, connection_id=uuid.uuid4(), database="ODS", tables=[], state="succeeded")); session.commit()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory), patch("recovery_service.services.data_automation._record_secured_assets"):
        activated = activate_security_orchestration_access(batch_id, confirm=True)
    assert activated["state"] == "completed"
    engine.dispose()


def test_security_orchestration_creates_encryption_only_backfill_for_completed_batch() -> None:
    engine, factory = _factory()
    profile_id = uuid.uuid4(); version_id = uuid.uuid4(); sm4_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=profile_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x", database="ODS"))
        session.add(DorisSm4TaskDefinition(id=sm4_id, name="案件登记加密", connection_id=profile_id, database="ODS", tables=[{"table_name": "CASE_RAW", "columns": ["PHONE"]}], coverage_contracts=[{"standard_database": "DWD", "standard_table": "CASE_STANDARD", "standard_field": "PHONE", "source_database": "ODS", "source_table": "CASE_RAW", "source_field": "PHONE", "access_database": "ACCESS"}]))
        pipeline = DataAutomationPipeline(name="历史案件登记流", standard_workflow_version_id=version_id, sm4_task_definition_id=sm4_id)
        raw = DataAsset(engine="doris", connection_id=profile_id, catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature="raw-v1", schema_contract={"columns": _columns(extra=True)})
        standard = DataAsset(engine="doris", connection_id=profile_id, catalog="", database="DWD", table_name="CASE_STANDARD", layer="standard", schema_signature="dwd-v1", schema_contract={"columns": _columns(extra=True)}, classification_summary={"fields": [{"field": "PHONE", "protection_action": "sm4", "auto_apply": True}]})
        session.add_all([pipeline, raw, standard]); session.flush()
        completed = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/audit.dmp", source_files=[], source_fingerprint="completed-ledger", state="completed", raw_target={"asset_ids": [str(raw.id)]}, standard_target={"asset_ids": [str(standard.id)]})
        session.add(completed); session.flush(); standard.last_batch_id = completed.id
        session.add(DataLineageEdge(source_asset_id=raw.id, source_field="PHONE", target_asset_id=standard.id, target_field="PHONE", transformation_type="direct", workflow_version_id=version_id, review_required=False))
        session.commit(); pipeline_id = pipeline.id; standard_id = standard.id; completed_id = completed.id
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        result = enable_security_orchestration(pipeline_id, standard_id, confirm=True)
    assert result["activation_batch_id"] != str(completed_id)
    with factory() as session:
        backfill = session.get(DataAutomationBatch, uuid.UUID(result["activation_batch_id"]))
        assert backfill.state == "encryption_ready"
        assert backfill.context["security_orchestration_backfill"] is True
        assert backfill.raw_target["asset_ids"] == [str(raw.id)]
    engine.dispose()


def test_protected_batch_encrypts_ods_before_starting_dwd_workflow() -> None:
    engine, factory = _factory()
    profile_id = uuid.uuid4(); version_id = uuid.uuid4(); sm4_id = uuid.uuid4(); node_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=profile_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x", database="ODS"))
        session.add(DorisSm4TaskDefinition(id=sm4_id, name="案件登记 ODS 加密", connection_id=profile_id, database="ODS", tables=[{"table_name": "CASE_RAW", "columns": ["PHONE"]}], coverage_contracts=[{"standard_database": "DWD", "standard_table": "CASE_STANDARD", "standard_field": "PHONE", "source_database": "ODS", "source_table": "CASE_RAW", "source_field": "PHONE", "access_database": "ACCESS"}]))
        pipeline = DataAutomationPipeline(name="案件登记安全流", data_sync_node_id=node_id, standard_workflow_version_id=version_id, sm4_task_definition_id=sm4_id)
        raw = DataAsset(connection_id=profile_id, engine="doris", catalog="", database="ODS", table_name="CASE_RAW", layer="raw", schema_signature=schema_signature(_columns(extra=True)), schema_contract={"columns": _columns(extra=True)})
        standard = DataAsset(connection_id=profile_id, engine="doris", catalog="", database="DWD", table_name="CASE_STANDARD", layer="standard", schema_signature="dwd-v1", schema_contract={"columns": _columns(extra=True)}, classification_summary={"fields": [{"field": "PHONE", "protection_action": "sm4", "auto_apply": True}]})
        session.add_all([pipeline, raw, standard]); session.flush()
        learning = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/learning.dmp", source_files=[], source_fingerprint="learning", state="completed", raw_target={"asset_ids": [str(raw.id)]}, standard_target={"asset_ids": [str(standard.id)]})
        session.add(learning); session.flush(); standard.last_batch_id = learning.id
        session.add(DataLineageEdge(source_asset_id=raw.id, source_field="PHONE", target_asset_id=standard.id, target_field="PHONE", transformation_type="direct", workflow_version_id=version_id, review_required=False))
        session.add(DataPlatformNode(id=node_id, name="Oracle 全表同步至 ODS", node_type="data_sync", config={"source_connection_id": str(uuid.uuid4()), "target_connection_id": str(profile_id), "table_mappings": [{"id": "stale", "source_table": "OLD_TABLE"}]}))
        session.commit(); pipeline_id = pipeline.id; standard_id = standard.id
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        enable_security_orchestration(pipeline_id, standard_id, confirm=True)
    with factory() as session:
        pipeline = session.get(DataAutomationPipeline, pipeline_id)
        sync_run = DataPlatformComponentRun(node_id=node_id, node_type="data_sync", node_name="Oracle 全表同步至 ODS", status="succeeded", result={"database": "ODS", "config_patch": {"table_mappings": [{"id": "case", "source_table": "CASE_RAW", "source_columns": _columns(extra=True), "column_mappings": [{"source_name": "PHONE", "target_name": "PHONE"}]}]}})
        session.add(sync_run); session.flush()
        session.add(DataPlatformComponentRunTable(component_run_id=sync_run.id, node_id=node_id, mapping_id="case", source_schema="AUDIT", source_table="CASE_RAW", target_database="ODS", target_table="CASE_RAW", status="succeeded"))
        batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/next.dmp", source_files=[], source_fingerprint="protected-next", state="syncing", sync_run_id=sync_run.id, restored_target={"schema": "AUDIT"}, context={"flow_mode": "ods_protected", "security_orchestration": pipeline.config["security_orchestration"]})
        session.add(batch); session.commit()
        with patch("recovery_service.services.data_platform.run_version") as run_version:
            assert data_automation_service._advance_one(session, batch) is True
        assert batch.state == "encryption_ready"
        assert batch.standard_run_id is None
        assert batch.context["security_contract_validated_at"]
        run_version.assert_not_called()
    engine.dispose()


def test_protected_batch_runs_dwd_in_protected_etl_mode_only_after_sm4() -> None:
    engine, factory = _factory()
    workflow_id = uuid.uuid4(); pipeline_id = uuid.uuid4(); standard_id = uuid.uuid4(); definition_id = uuid.uuid4(); job_id = uuid.uuid4()
    snapshot = {"enabled": True, "mode": "ods_protected", "standard_asset_id": str(standard_id), "standard_schema_signature": "dwd-v1", "standard_workflow_version_id": str(workflow_id), "sm4_task_definition_id": str(definition_id), "sm4_task_revision": 1, "required_standard_fields": [], "source_assets": [], "field_contracts": []}
    with factory() as session:
        pipeline = DataAutomationPipeline(id=pipeline_id, name="案件登记安全流", standard_workflow_version_id=workflow_id, sm4_task_definition_id=definition_id, config={"security_access_enabled": True, "auto_encryption_enabled": True, "security_orchestration": snapshot})
        standard = DataAsset(id=standard_id, engine="doris", catalog="", database="DWD", table_name="CASE_STANDARD", layer="standard", schema_signature="dwd-v1", schema_contract={"columns": []})
        definition = DorisSm4TaskDefinition(id=definition_id, name="案件登记 ODS 加密", connection_id=uuid.uuid4(), database="ODS", tables=[], coverage_contracts=[])
        job = DorisSm4BatchJob(id=job_id, connection_id=definition.connection_id, database="ODS", tables=[], state="succeeded", results=[])
        batch = DataAutomationBatch(pipeline_id=pipeline_id, source_path="/next.dmp", source_files=[], source_fingerprint="protected-encrypted", state="encrypting", encryption_batch_id=job_id, raw_target={"asset_ids": []}, context={"flow_mode": "ods_protected", "security_orchestration": snapshot, "encryption_standard_asset_id": str(standard_id)})
        session.add_all([pipeline, standard, definition, job, batch]); session.commit()
        queued_run = SimpleNamespace(id=uuid.uuid4())
        with patch("recovery_service.services.data_automation._record_secured_assets"), patch("recovery_service.services.data_automation._validate_protected_batch", return_value=standard_id), patch("recovery_service.services.data_platform.prepare_workflow_run", return_value=queued_run) as prepare_run:
            assert data_automation_service._complete_security_encryption(session, pipeline, batch) is True
        assert batch.state == "standardize_queued"
        assert prepare_run.call_args.kwargs["trigger_context"]["security_access_mode"] == "protected_etl"
        assert prepare_run.call_args.kwargs["trigger_context"]["security_orchestration"] == snapshot
        events = session.scalars(select(DataAutomationEvent.event_type).where(DataAutomationEvent.batch_id == batch.id)).all()
        assert events[-2:] == ["security_access_activated", "standardize_queued"]
    engine.dispose()


def test_workflow_trigger_can_force_doris_sql_to_protected_etl_mode() -> None:
    from recovery_service.services import data_platform

    engine, factory = _factory()
    profile_id = uuid.uuid4()
    with factory() as session:
        session.add(DatabaseConnectionProfile(id=profile_id, name="审计 Doris", engine="doris", host="127.0.0.1", port=9030, username="root", password_enc="x", database="ODS"))
        session.commit()
        result = SimpleNamespace(message="ok", sql_type="SELECT", row_count=0, affected_rows=0, duration_ms=1, columns=[], rows=[])
        with patch("recovery_service.services.data_platform.execute_doris_sql", return_value=result) as execute:
            security_orchestration = {"field_contracts": [{"contract_hash": "x" * 64}]}
            data_platform._execute_node(session, {"node_type": "doris_sql", "config": {"connection_id": str(profile_id), "database": "ODS", "sql": "INSERT INTO DWD.CASE_STANDARD SELECT * FROM ODS.CASE_RAW", "security_access_mode": "trusted"}}, run=SimpleNamespace(trigger_context={"security_access_mode": "protected_etl", "security_orchestration": security_orchestration}))
        assert execute.call_args.kwargs["security_access_mode"] == "protected_etl"
        assert execute.call_args.kwargs["security_access_context"] == security_orchestration
    engine.dispose()


def test_completed_batch_enqueues_openmetadata_after_commit() -> None:
    engine, factory = _factory()
    with factory() as session:
        pipeline = DataAutomationPipeline(name="案件登记目录同步流")
        session.add(pipeline); session.flush()
        batch = DataAutomationBatch(pipeline_id=pipeline.id, source_path="/audit.dmp", source_files=[], source_fingerprint="openmetadata-final", state="standard_ready", context={"flow_mode": "learning"})
        session.add(batch); session.commit(); batch_id = batch.id
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory), patch("recovery_service.services.data_automation._enqueue_openmetadata_snapshot") as enqueue:
        result = data_automation_service.advance_batches()
    assert result == {"advanced": 1, "failed": 0}
    enqueue.assert_called_once_with(batch_id=batch_id)
    with factory() as session:
        saved = session.get(DataAutomationBatch, batch_id)
        assert saved.state == "completed"
        assert saved.context["openmetadata_sync_state"] == "queued"
        assert saved.context["openmetadata_sync_pending"] is False
    engine.dispose()


def test_new_batch_copies_the_enabled_security_snapshot() -> None:
    engine, factory = _factory()
    template_id = uuid.uuid4()
    frozen_standard_id = uuid.uuid4()
    with factory() as session:
        session.add(RecoveryTask(id=template_id, remote_host="oracle-host", remote_port=22, remote_user="root", remote_password_enc="encrypted", remote_directory="/dmp", target_connection="oracle:1521/ORCLPDB1", target_admin_user="SYSTEM", target_admin_password_enc="encrypted", options={"professional_flow": {"source": {"host": "oracle-host", "port": 22, "user": "root", "password": "encrypted", "directory": "/dmp"}, "import_source": {"mode": "direct"}}}, state="succeeded"))
        pipeline = DataAutomationPipeline(name="冻结批次快照", restore_template_task_id=template_id, config={"security_access_enabled": True, "auto_encryption_enabled": True, "security_orchestration": {"enabled": True, "mode": "ods_protected", "standard_asset_id": str(frozen_standard_id)}})
        session.add(pipeline); session.commit(); pipeline_id = pipeline.id
    files = [{"relative_path": "AUDIT_01.dmp", "remote_path": "/dmp/AUDIT_01.dmp", "size_bytes": 1024, "modified_epoch": 100.0}]
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory), patch("recovery_service.services.data_automation._list_template_dmp_files", return_value=files):
        result = scan_pipeline(pipeline_id, dispatch=False)
    with factory() as session:
        batch = session.get(DataAutomationBatch, uuid.UUID(result["created_batch_ids"][0]))
        pipeline = session.get(DataAutomationPipeline, pipeline_id)
        pipeline.config = {**pipeline.config, "security_orchestration": {**pipeline.config["security_orchestration"], "standard_asset_id": str(uuid.uuid4())}}
        session.commit()
        assert batch.context["flow_mode"] == "ods_protected"
        assert batch.context["security_orchestration"]["standard_asset_id"] == str(frozen_standard_id)
    engine.dispose()


def test_editing_a_frozen_binding_disables_security_orchestration() -> None:
    engine, factory = _factory()
    pipeline_id = uuid.uuid4()
    with factory() as session:
        session.add(DataAutomationPipeline(
            id=pipeline_id,
            name="案件登记安全流",
            standard_target={"database": "DWD", "table": "CASE_STANDARD"},
            config={
                "security_access_enabled": True,
                "auto_encryption_enabled": True,
                "security_orchestration": {"enabled": True, "mode": "ods_protected"},
            },
        ))
        session.commit()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        updated = update_pipeline(
            pipeline_id,
            {"standard_target": {"database": "DWD", "table": "CASE_STANDARD_V2"}},
        )
    assert updated["config"]["security_access_enabled"] is False
    assert updated["config"]["auto_encryption_enabled"] is False
    with factory() as session:
        config = session.get(DataAutomationPipeline, pipeline_id).config
        assert config["security_access_enabled"] is False
        assert config["auto_encryption_enabled"] is False
        assert config["security_orchestration"]["enabled"] is False
        assert config["security_orchestration"]["invalidated_reason"] == "业务流冻结引用已修改"
    engine.dispose()


def test_lineage_overview_filters_assets_and_summarizes_edges() -> None:
    engine, factory = _factory()
    batch_id = uuid.uuid4()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        raw = register_asset({"engine": "doris", "database": "ODS", "table_name": "CUSTOMER_RAW", "layer": "raw", "columns": _columns(extra=True)}, batch_id)
        standard = register_asset({"engine": "doris", "database": "DWD", "table_name": "CUSTOMER_STANDARD", "layer": "standard", "columns": _columns(extra=True)}, batch_id)
        secured = register_asset({"engine": "doris", "database": "DWD", "table_name": "CUSTOMER_SECURED", "layer": "secured", "columns": _columns(extra=True)}, batch_id)
        create_lineage_edge({"batch_id": batch_id, "source_asset_id": raw["asset_id"], "source_field": "PHONE", "target_asset_id": standard["asset_id"], "target_field": "PHONE", "transformation_type": "direct"})
        create_lineage_edge({"batch_id": batch_id, "source_asset_id": standard["asset_id"], "source_field": "PHONE", "target_asset_id": secured["asset_id"], "target_field": "PHONE", "transformation_type": "expression", "expression": "CQ_SM4_ENCRYPT(PHONE)", "review_required": True})
        overview = lineage_overview(batch_id=batch_id)
        searched = lineage_overview(search="phone")
        secured_only = lineage_overview(layer="secured")
        upstream = trace_lineage(uuid.UUID(secured["asset_id"]), batch_id=batch_id)
    assert overview["summary"] == {"asset_count": 3, "edge_count": 2, "field_edge_count": 2, "review_count": 1, "sm4_edge_count": 1}
    assert len(searched["assets"]) == 3
    assert [item["layer"] for item in secured_only["assets"]] == ["secured"]
    assert len(upstream["assets"]) == 3 and upstream["batch_id"] == str(batch_id)
    engine.dispose()


def test_openlineage_event_is_idempotent_and_creates_column_lineage() -> None:
    engine, factory = _factory()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        register_asset({"engine": "doris", "catalog": "hive", "database": "ODS", "table_name": "CUSTOMER_RAW", "layer": "raw", "columns": _columns(extra=True)})
        register_asset({"engine": "doris", "catalog": "hive", "database": "DWD", "table_name": "CUSTOMER_STANDARD", "layer": "standard", "columns": _columns(extra=True)})
        raw_name = "doris.hive.ods.customer_raw.raw"
        standard_name = "doris.hive.dwd.customer_standard.standard"
        event = {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-05T10:00:00+08:00",
            "producer": "https://example.test/lineage",
            "facets": {"apiToken": "should-not-persist"},
            "run": {"runId": "run-001", "facets": {"accessToken": "should-not-persist"}},
            "job": {"namespace": "etl", "name": "customer-standardize"},
            "inputs": [{"namespace": "urn:test", "name": raw_name}],
            "outputs": [{
                "namespace": "urn:test",
                "name": standard_name,
                "facets": {"columnLineage": {"fields": {
                    "PHONE": {"inputFields": [{"namespace": "urn:test", "name": raw_name, "field": "PHONE", "transformationType": "DIRECT"}]}
                }}}
            }],
        }
        first = ingest_openlineage_event(event)
        second = ingest_openlineage_event(event)
        projection = openmetadata_lineage_projection()
    assert first["duplicate"] is False and first["created_edge_count"] == 1
    assert second["duplicate"] is True and second["created_edge_count"] == 0
    assert len(projection["relationships"]) == 1
    assert projection["relationships"][0]["lineageDetails"]["fields"][0]["toColumn"] == "PHONE"
    assert any(item["fullyQualifiedName"] == "doris.hive.ods.customer_raw.raw" for item in projection["entities"])
    with factory() as session:
        assert session.query(DataLineageEvent).count() == 1
        persisted = session.scalar(select(DataLineageEvent))
        assert persisted.event_payload["run"]["facets"]["accessToken"] == "[REDACTED]"
        assert persisted.facets["apiToken"] == "[REDACTED]"
        assert session.query(DataLineageEdge).count() == 1
        assert session.scalar(select(DataLineageEdge)).evidence["source"] == "openlineage"
    engine.dispose()


def test_openlineage_unknown_dataset_is_audited_without_fake_asset_or_edge() -> None:
    engine, factory = _factory()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        result = ingest_openlineage_event({
            "eventType": "START",
            "eventTime": "2026-09-05T10:01:00Z",
            "run": {"runId": "run-unknown"},
            "job": {"namespace": "etl", "name": "unknown-source"},
            "inputs": [{"namespace": "urn:test", "name": "missing.database.table"}],
            "outputs": [{"namespace": "urn:test", "name": "missing.database.target"}],
        })
    assert result["created_edge_count"] == 0
    with factory() as session:
        assert session.query(DataAsset).count() == 0
        assert session.query(DataLineageEdge).count() == 0
        assert session.query(DataLineageEvent).count() == 1
    engine.dispose()


def test_database_stats_are_split_by_connection_and_business_layer_is_persistent() -> None:
    engine, factory = _factory()
    with patch("recovery_service.services.data_automation.get_sync_session_factory", return_value=factory):
        left = register_asset({"engine": "doris", "connection_id": str(uuid.uuid4()), "connection_name": "doris-left", "catalog": "hive", "database": "DWD", "table_name": "CUSTOMER", "layer": "standard", "columns": _columns()})
        right = register_asset({"engine": "doris", "connection_id": str(uuid.uuid4()), "connection_name": "doris-right", "catalog": "hive", "database": "DWD", "table_name": "CUSTOMER", "layer": "standard", "columns": _columns()})
        create_lineage_edge({"source_asset_id": left["asset_id"], "target_asset_id": right["asset_id"], "transformation_type": "direct"})
        before = lineage_overview()
        left_key = next(item["database_key"] for item in before["database_stats"] if item["connection_name"] == "doris-left")
        saved = upsert_database_layer({"engine": "doris", "connection_name": "doris-left", "catalog": "hive", "database": "DWD", "business_layer": "客户域 / DWD", "description": "客户标准库"})
        filtered = lineage_overview(database_key=left_key)
        layers = list_database_layers()
    assert len(before["database_stats"]) == 2
    assert all(item["asset_count"] == 1 for item in before["database_stats"])
    assert before["summary"]["edge_count"] == 1
    assert saved["business_layer"] == "客户域 / DWD"
    assert filtered["summary"]["asset_count"] == 1
    assert filtered["assets"][0]["database_business_layer"] == "客户域 / DWD"
    assert any(item["database_key"] == left_key and item["business_layer"] == "客户域 / DWD" for item in layers)
    with factory() as session:
        assert session.query(DataDatabaseLayer).count() == 1
    engine.dispose()
