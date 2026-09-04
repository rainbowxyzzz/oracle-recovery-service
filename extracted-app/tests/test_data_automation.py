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
    DataDatabaseLayer,
    DataLineageEdge,
    DataLineageEvent,
    RecoveryTask,
)
from recovery_service.services.data_automation import (
    build_reverse_encryption_plan,
    classify_asset,
    create_blueprint,
    create_classification_rule,
    create_lineage_edge,
    create_pipeline,
    ingest_openlineage_event,
    list_database_layers,
    lineage_overview,
    match_batch_blueprint,
    openmetadata_lineage_projection,
    register_asset,
    scan_pipeline,
    schema_signature,
    trace_lineage,
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
