from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from recovery_service.api.deps import get_current_actor
from recovery_service.api.v1.harness_assistant import router
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    Base, AssistantPlan, DataAutomationBatch, DataAutomationPipeline, DataPlatformNode,
    DataPlatformWorkflowVersion, DataPlatformComponentRun, DataPlatformWorkflowRun,
    DorisSm4TaskDefinition, DorisSm4BatchJob, RecoveryTask, DataAsset, DataLineageEdge,
)
from recovery_service.services.auth import AuthContext
from recovery_service.services import harness_assistant as plans, assistant_execution as execute, data_automation as auto

ACTOR = AuthContext(None, "admin-test", "admin", "bearer", {})


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for mod in (plans, execute, auto):
        monkeypatch.setattr(mod, "get_sync_session_factory", lambda: factory)
    with factory() as s:
        restore = RecoveryTask(remote_host="oracle", remote_user="root", remote_directory="/dmp",
            remote_password_enc="DO_NOT_EXPOSE", target_connection="oracle:1521/PDB", target_admin_user="SYSTEM",
            target_admin_password_enc="DO_NOT_EXPOSE", options={"professional_flow": {}}, state="succeeded")
        node = DataPlatformNode(name="ODS同步", node_type="data_sync", status="active", config={
            "table_mappings": [{"source_table": "A", "target_database": "ODS", "target_table": "A"}],
            "write_mode": "truncate_insert"})
        version = DataPlatformWorkflowVersion(workflow_id=uuid.uuid4(), version_no=2, channel="prod", status="online",
            release_snapshot={"nodes": [{"key": "sql1", "node_type": "doris_sql", "config": {"sql": "insert into DWD.A select * from ODS.A"}}], "edges": []})
        sm4 = DorisSm4TaskDefinition(name="全库加密", connection_id=uuid.uuid4(), database="ALL_DB", revision=3,
            tables=[{"table_name":"A", "columns":["PHONE"]}, {"table_name":"B", "columns":["ID_CARD"]}],
            table_strategy="auto_create", target_suffix="_SEC")
        s.add_all([restore, node, version, sm4]); s.flush()
        pipeline = DataAutomationPipeline(name="自然资源DWD", restore_template_task_id=restore.id,
            data_sync_node_id=node.id, standard_workflow_version_id=version.id, sm4_task_definition_id=sm4.id,
            standard_target={"database":"DWD", "table_name":"A"}, stable_wait_seconds=10)
        s.add(pipeline); s.commit()
        ids = SimpleNamespace(pipeline=pipeline.id, node=node.id, version=version.id, sm4=sm4.id, template=restore.id)
    files=[{"relative_path":"test.dmp", "remote_path":"/dmp/test.dmp", "size_bytes":1024, "modified_epoch":100}]
    monkeypatch.setattr(auto, "_list_template_dmp_files", lambda *_: deepcopy(files))
    send=Mock(return_value=SimpleNamespace(id="celery-test"))
    monkeypatch.setattr("recovery_service.workers.celery_app.celery_app.send_task", send)
    yield SimpleNamespace(factory=factory, ids=ids, files=files, send=send)
    engine.dispose()


def prepare(env):
    return plans.prepare_plan(env.ids.pipeline, "test.dmp", actor=ACTOR)


def confirmed(env):
    plan = prepare(env)
    result = plans.confirm_plan(plan["plan_id"], plan["plan_hash"], ACTOR)
    with env.factory() as s:
        b=s.get(DataAutomationBatch, uuid.UUID(result["batch_id"]))
        b.source_observed_at=app_now()-timedelta(seconds=20); s.commit()
    return uuid.UUID(result["batch_id"]), plan


def test_plan_has_full_task_scope_no_execution_no_secrets(env):
    p=prepare(env)
    assert [t["table_name"] for t in p["sm4"]["tables"]]==["A","B"]
    assert p["sm4"]["revision"]==3
    assert "DO_NOT_EXPOSE" not in str(p) + str(plans.catalog())
    env.send.assert_not_called()
    with env.factory() as s:
        assert s.scalar(select(func.count()).select_from(DataAutomationBatch)) == 0


@pytest.mark.parametrize("name",["../test.dmp","/dmp/test.dmp","*.dmp","x\\test.dmp","missing.dmp"])
def test_explicit_file_must_resolve_in_template_directory(env,name):
    with pytest.raises(ValueError):plans.prepare_plan(env.ids.pipeline,name)


def test_confirm_idempotent_and_changed_plan_rejected(env):
    p=prepare(env)
    with pytest.raises(ValueError):plans.confirm_plan(p["plan_id"],"x"*64,ACTOR)
    a=plans.confirm_plan(p["plan_id"],p["plan_hash"],ACTOR)
    b=plans.confirm_plan(p["plan_id"],p["plan_hash"],ACTOR)
    assert a["batch_id"]==b["batch_id"]
    env.send.assert_not_called()
    p2=prepare(env)
    with pytest.raises(ValueError):plans.confirm_plan(p2["plan_id"],p2["plan_hash"],ACTOR)


@pytest.mark.parametrize("change",["sm4","file","version"])
def test_stale_plan_cannot_be_confirmed(env,change):
    p=prepare(env)
    with env.factory() as s:
        if change=="sm4":s.get(DorisSm4TaskDefinition,env.ids.sm4).tables=[{"table_name":"C","columns":["SECRET"]}]
        if change=="version":s.get(DataPlatformWorkflowVersion,env.ids.version).status="offline"
        s.commit()
    if change=="file":env.files[0]["size_bytes"]+=1
    with pytest.raises(ValueError):plans.confirm_plan(p["plan_id"],p["plan_hash"],ACTOR)


def test_frozen_sync_workflow_and_sm4_survive_edits(env):
    batch_id,p=confirmed(env)
    with env.factory() as s:
        s.get(DataPlatformNode,env.ids.node).config={"new":"not approved"}
        s.get(DorisSm4TaskDefinition,env.ids.sm4).tables=[{"table_name":"C","columns":["SECRET"]}]
        b=s.get(DataAutomationBatch,batch_id)
        b.standard_run_id=uuid.uuid4();s.commit()
        frozen=execute.sync_config_for_batch(s,batch_id,env.ids.node,{})
        assert frozen["table_mappings"][0]["target_database"]=="ODS"
        run=SimpleNamespace(id=b.standard_run_id,version_id=env.ids.version,trigger_context={"assistant_plan_id":p["plan_id"]})
        assert execute.release_for_run(s,run,{})["nodes"][0]["key"]=="sql1"
        assert len(b.context["assistant_snapshot"]["sm4"]["tables"])==2


def test_four_stages_and_full_database_encryption(env,monkeypatch):
    batch_id,p=confirmed(env)
    def tick():
        with env.factory() as s:
            b=s.get(DataAutomationBatch,batch_id); execute.advance(s,b);s.commit();return b
    b=tick();assert b.state=="restore_queued";env.send.assert_called_once()
    with env.factory() as s:
        task=s.get(RecoveryTask,b.restore_task_id);task.state="succeeded";task.metadata_snapshot={"schema":"ACTUAL_RESTORED"};s.commit()
    b=tick();assert b.state=="assistant_sync_ready"
    def sync(node_id,overrides,actor=None):
        assert overrides["restored_target"]["schema"]=="ACTUAL_RESTORED"
        with env.factory() as s:
            run=DataPlatformComponentRun(node_id=node_id,node_type="data_sync",node_name="ODS",status="succeeded")
            s.add(run);s.commit();return {"run_id":str(run.id)}
    monkeypatch.setattr("recovery_service.services.data_platform.submit_component_task_run",sync)
    monkeypatch.setattr(auto,"_record_sync_assets",lambda *_:[])
    monkeypatch.setattr(auto,"_record_standard_assets",lambda *_:[])
    monkeypatch.setattr(auto,"_record_secured_assets",lambda *_:None)
    b=tick();assert b.state=="sync_queued"
    b=tick();assert b.state=="assistant_standard_ready"
    def standard(version_id,**kw):
        with env.factory() as s:
            run=DataPlatformWorkflowRun(id=kw["run_id"],workflow_id=uuid.uuid4(),version_id=version_id,status="succeeded")
            s.add(run);s.commit();return SimpleNamespace(run_id=run.id)
    monkeypatch.setattr("recovery_service.services.data_platform.run_version",standard)
    b=tick();assert b.state=="standardize_queued"
    b=tick();assert b.state=="assistant_encryption_ready"
    captured=[]
    def sm4(snapshot,actor=None):
        captured.append(snapshot)
        with env.factory() as s:
            job=DorisSm4BatchJob(connection_id=uuid.UUID(snapshot["connection_id"]),database=snapshot["database"],
                tables=snapshot["tables"],state="succeeded",table_strategy=snapshot["table_strategy"])
            s.add(job);s.commit();return SimpleNamespace(batch_id=job.id)
    monkeypatch.setattr("recovery_service.services.doris_encryption.run_sm4_task_snapshot",sm4)
    b=tick();assert b.state=="encrypting"
    assert captured[0]["tables"]==p["sm4"]["tables"]
    assert captured[0]["database"]=="ALL_DB"  # Not guessed from ODS/DWD lineage.
    assert "key_seed" not in str(captured)
    b=tick();assert b.state=="completed"
    tick();assert len(captured)==1


def test_dispatch_uncertainty_never_auto_repeats(env,monkeypatch):
    batch_id,p=confirmed(env)
    with env.factory() as s:
        b=s.get(DataAutomationBatch,batch_id);b.state="assistant_encryption_ready";s.commit()
    submit=Mock(side_effect=RuntimeError("lost response"))
    monkeypatch.setattr("recovery_service.services.doris_encryption.run_sm4_task_snapshot",submit)
    for _ in range(3):
        with env.factory() as s:
            b=s.get(DataAutomationBatch,batch_id);execute.advance(s,b);s.commit()
    assert submit.call_count==1
    with pytest.raises(ValueError):execute.resume(p["plan_id"],True)
    with pytest.raises(ValueError):auto.resume_batch(batch_id)


def test_failure_does_not_submit_downstream(env):
    batch_id,p=confirmed(env)
    with env.factory() as s:
        b=s.get(DataAutomationBatch,batch_id);execute.advance(s,b);s.commit()
        task=s.get(RecoveryTask,b.restore_task_id);task.state="failed";s.commit()
        execute.advance(s,b);s.commit();assert b.state=="failed" and b.sync_run_id is None
    with pytest.raises(ValueError):execute.resume(p["plan_id"],False)
    execute.resume(p["plan_id"],True)


def test_routes_require_admin_and_explicit_confirm(env):
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_current_actor]=lambda:AuthContext(None,"reader","user","bearer",{})
    client=TestClient(app)
    assert client.get("/assistant/catalog").status_code==403
    assert client.post("/assistant/plans",json={"pipeline_id":str(env.ids.pipeline),"file_name":"test.dmp"}).status_code==403
    app.dependency_overrides[get_current_actor]=lambda:ACTOR
    result=client.post("/assistant/plans",json={"pipeline_id":str(env.ids.pipeline),"file_name":"test.dmp"})
    assert result.status_code==200,result.text
    assert result.json()["batch_id"] is None
    env.send.assert_not_called()


def test_missing_harness_does_not_fake_model_execution(env,monkeypatch):
    monkeypatch.setattr(plans,"get_settings",lambda:SimpleNamespace(harness_bridge_url="",harness_bridge_token=""))
    with pytest.raises(ValueError,match="尚未配置"):plans.interpret("处理test.dmp")
    with env.factory() as s:assert s.scalar(select(func.count()).select_from(AssistantPlan))==0


def test_plan_displays_only_effective_sync_scope_and_frozen_sql(env):
    with env.factory() as s:
        node=s.get(DataPlatformNode,env.ids.node)
        node.config={"target_database":"ODS", "write_mode":"truncate_insert", "selected_tables":["A"],
                     "table_mappings":[{"source_table":"A", "write_mode":"append"},
                                       {"source_table":"B"}, {"source_table":"C", "enabled":False}]}
        s.commit()
    p=prepare(env)
    assert p["sync_tables"]==[{"source_table":"A", "target_database":"ODS", "target_table":"A", "write_mode":"truncate_insert"}]
    assert p["sql_steps"][0]["sql"]=="insert into DWD.A select * from ODS.A"
    assert "insert into" not in str(plans.catalog())


def test_changed_file_after_confirmation_does_not_restore(env):
    batch_id,p=confirmed(env)
    env.files[0]["size_bytes"]+=1
    result=auto.advance_batches()
    assert result["failed"]==1
    env.send.assert_not_called()
    assert plans.get_plan(p["plan_id"])["batch"]["state"]=="failed"
    assert plans.list_plans()[0]["state"]=="failed"


def test_legacy_paths_keep_original_config(env):
    original={"x":"original"}
    with env.factory() as s:
        assert execute.sync_config_for_batch(s,None,env.ids.node,original) is original
        assert execute.release_for_run(s,SimpleNamespace(trigger_context=None),original) is original


def test_sm4_lineage_requires_exact_connection_catalog_database(env):
    batch_id,_=confirmed(env)
    with env.factory() as s:
        task=s.get(DorisSm4TaskDefinition,env.ids.sm4)
        # Same table in another DB/catalog must never be linked as this job's source.
        for db,catalog in [("DWD",""),("ALL_DB","external")]:
            auto._upsert_asset(s,connection_id=task.connection_id,catalog=catalog,database=db,
                table_name="A",layer="standard",domain="test",columns=[],batch_id=batch_id)
        good=auto._upsert_asset(s,connection_id=task.connection_id,catalog="",database="ALL_DB",
            table_name="B",layer="raw",domain="test",columns=[],batch_id=batch_id)
        job=DorisSm4BatchJob(connection_id=task.connection_id,database="ALL_DB",tables=task.tables,
            table_strategy="auto_create",state="succeeded",results=[
                {"table_name":"A","target_table":"A_SEC","state":"succeeded"},
                {"table_name":"B","target_table":"B_SEC","state":"succeeded","columns":["ID_CARD"]}])
        s.add(job);s.flush()
        b=s.get(DataAutomationBatch,batch_id)
        execute._record_full_task_secured(s,SimpleNamespace(business_domain="test"),b,job);s.commit()
        edges=s.scalars(select(DataLineageEdge).where(DataLineageEdge.batch_id==batch_id)).all()
        assert len(edges)==1 and edges[0].source_asset_id==good.id
        assert len(b.context["secured_asset_ids"])==2


@pytest.mark.parametrize("model_result",[
    {"pipeline_id":str(uuid.uuid4()),"file_name":"test.dmp"},
    {"reply":"请明确选择路径和文件"},
])
def test_model_unknown_or_ambiguous_candidate_never_dispatches(env,monkeypatch,model_result):
    monkeypatch.setattr(plans,"get_settings",lambda:SimpleNamespace(harness_bridge_url="http://fixture",harness_bridge_token="dummy"))
    client=Mock()
    client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    client.post.return_value.json.return_value=model_result
    monkeypatch.setattr(plans.httpx,"Client",lambda **kw:client)
    if model_result.get("pipeline_id"):
        with pytest.raises(ValueError,match="不存在"):plans.interpret("处理test.dmp",actor=ACTOR)
    else:
        assert plans.interpret("更新DWD",actor=ACTOR)["plan"] is None
    env.send.assert_not_called()
    with env.factory() as s:
        assert s.scalar(select(func.count()).select_from(AssistantPlan))==0


def test_valid_model_suggestion_only_creates_draft_and_sends_safe_metadata(env,monkeypatch):
    monkeypatch.setattr(plans,"get_settings",lambda:SimpleNamespace(harness_bridge_url="http://fixture",harness_bridge_token="dummy"))
    client=Mock()
    client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    client.post.return_value.json.return_value={"pipeline_id":str(env.ids.pipeline),"file_name":"test.dmp"}
    monkeypatch.setattr(plans.httpx,"Client",lambda **kw:client)
    result=plans.interpret("将test.dmp更新到自然资源DWD，再执行全库加密",actor=ACTOR)
    assert result["plan"]["state"]=="draft" and result["plan"]["batch_id"] is None
    assert len(result["plan"]["sm4"]["tables"])==2
    payload=str(client.post.call_args.kwargs["json"])
    assert "DO_NOT_EXPOSE" not in payload and "insert into" not in payload and "PHONE" not in payload
    env.send.assert_not_called()
