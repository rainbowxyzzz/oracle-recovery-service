import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    Base,
    DataAsset,
    DataAutomationBatch,
    DataAutomationPipeline,
    DataClassificationRule,
    DataLineageEdge,
    RecoveryTask,
)
from recovery_service.services.data_automation import (
    build_reverse_encryption_plan,
    classify_asset,
    create_blueprint,
    create_classification_rule,
    create_lineage_edge,
    create_pipeline,
    lineage_overview,
    match_batch_blueprint,
    register_asset,
    scan_pipeline,
    schema_signature,
    trace_lineage,
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
            cloned = session.get(DataAutomationBatch, uuid.UUID(first["created_batch_ids"][0]))
            assert cloned.state == "restore_queued"
        send.assert_called_once()
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
