from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import desc, select

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.api_orchestration import (
    ConnectorPayload,
    InvokePayload,
    SqlApiPayload,
    WorkflowPayload,
)
from recovery_service.core.models.task import (
    ApiOrchestrationConnector,
    ApiOrchestrationRun,
    ApiOrchestrationSqlApi,
    ApiOrchestrationWorkflow,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.services.api_orchestration import (
    connector_dict,
    connector_snapshot,
    invoke_connector,
    invoke_sql_api,
    run_dict,
    run_input_data,
    save_connector,
    save_sql_api,
    save_workflow,
    sql_api_dict,
    sql_api_snapshot,
    submit_run,
    workflow_dict,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext

router = APIRouter(prefix="/api-orchestration", tags=["api-orchestration"])


async def _sync_call(callback: Callable[..., Any], *args: Any) -> Any:
    def run() -> Any:
        with get_sync_session_factory()() as session:
            return callback(session, *args)

    return await asyncio.to_thread(run)


def _get(session, model, item_id: uuid.UUID, label: str):
    item = session.get(model, item_id)
    if not item:
        raise ValueError(f"{label}不存在。")
    return item


def _connector_payload(body: ConnectorPayload) -> dict[str, Any]:
    data = body.model_dump()
    secret = data.get("auth_secret")
    data["auth_secret"] = secret.get_secret_value() if secret is not None else None
    return data


def _list_items(session, model, serializer, limit: int):
    rows = session.scalars(select(model).order_by(desc(model.updated_at)).limit(limit)).all()
    return [serializer(row) for row in rows]


def _delete_item(session, model, item_id: uuid.UUID, label: str):
    item = _get(session, model, item_id, label)
    if model in {ApiOrchestrationConnector, ApiOrchestrationSqlApi}:
        field = "connector_id" if model is ApiOrchestrationConnector else "sql_api_id"
        workflows = session.scalars(select(ApiOrchestrationWorkflow)).all()
        if any(
            str((node.get("config") or {}).get(field) or "") == str(item_id)
            for workflow in workflows
            for node in (workflow.nodes or [])
        ):
            raise ValueError(f"{label}仍被流程引用，不能删除。")
    session.delete(item)
    session.commit()
    return {"id": str(item_id), "name": getattr(item, "name", "")}


def _test_connector(session, item_id: uuid.UUID, input_data: dict[str, Any]):
    return invoke_connector(_get(session, ApiOrchestrationConnector, item_id, "连接器"), input_data)


def _invoke_sql_by_slug(session, slug: str, params: dict[str, Any], allow_write: bool):
    item = session.scalar(select(ApiOrchestrationSqlApi).where(ApiOrchestrationSqlApi.slug == slug))
    if not item:
        raise ValueError("SQL API 不存在。")
    return invoke_sql_api(session, item, params, allow_write=allow_write)


def _publish_workflow(session, item_id: uuid.UUID, allow_sql_write: bool):
    item = _get(session, ApiOrchestrationWorkflow, item_id, "流程")
    nodes = copy.deepcopy(item.nodes or [])
    for node in nodes:
        if node.get("type") == "http":
            config = node.setdefault("config", {})
            connector_id = config.get("connector_id")
            try:
                connector = session.get(ApiOrchestrationConnector, uuid.UUID(str(connector_id)))
            except (TypeError, ValueError):
                connector = None
            if not connector or not connector.enabled:
                raise ValueError(f"节点 {node.get('name') or node.get('id')} 引用的连接器不存在或已停用。")
            config["connector_snapshot"] = connector_snapshot(connector)
            continue
        if node.get("type") != "sql_api":
            continue
        config = node.setdefault("config", {})
        sql_id = config.get("sql_api_id")
        try:
            sql_api = session.get(ApiOrchestrationSqlApi, uuid.UUID(str(sql_id)))
        except (TypeError, ValueError):
            sql_api = None
        if not sql_api or not sql_api.enabled:
            raise ValueError(f"节点 {node.get('name') or node.get('id')} 引用的 SQL API 不存在或已停用。")
        if sql_api.mode == "write" and not allow_sql_write:
            raise PermissionError("发布包含写 SQL 的流程需要 apiOrchestration:sqlWrite 权限。")
        config["write_authorized"] = sql_api.mode == "write"
        config["sql_api_snapshot"] = sql_api_snapshot(sql_api)
    item.published_snapshot = {"nodes": nodes, "edges": copy.deepcopy(item.edges or [])}
    item.status = "published"
    session.commit()
    session.refresh(item)
    return workflow_dict(item)


def _submit_workflow_run(session, workflow_id: uuid.UUID, input_data: dict[str, Any], username: str | None):
    workflow = _get(session, ApiOrchestrationWorkflow, workflow_id, "流程")
    return run_dict(session, submit_run(session, workflow, input_data, username))


def _list_runs(session, limit: int, workflow_id: uuid.UUID | None, state: str | None):
    statement = select(ApiOrchestrationRun)
    if workflow_id:
        statement = statement.where(ApiOrchestrationRun.workflow_id == workflow_id)
    if state:
        statement = statement.where(ApiOrchestrationRun.state == state)
    rows = session.scalars(statement.order_by(desc(ApiOrchestrationRun.created_at)).limit(limit)).all()
    return [run_dict(session, row) for row in rows]


def _get_run(session, run_id: uuid.UUID):
    return run_dict(session, _get(session, ApiOrchestrationRun, run_id, "运行记录"), detail=True)


def _retry_run(session, run_id: uuid.UUID, username: str | None):
    previous = _get(session, ApiOrchestrationRun, run_id, "运行记录")
    if previous.state not in {"failed", "succeeded"}:
        raise ValueError("只有已结束的运行记录可以重新执行。")
    workflow = _get(session, ApiOrchestrationWorkflow, previous.workflow_id, "流程")
    return run_dict(session, submit_run(
        session, workflow, run_input_data(previous), username,
        snapshot=copy.deepcopy(previous.workflow_snapshot), revision=previous.workflow_revision,
    ))


def _raise_api_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/connectors")
async def list_connectors(
    limit: int = Query(default=200, ge=1, le=500),
    _: AuthContext = Depends(require_permission("apiOrchestration:read")),
):
    return {"items": await _sync_call(_list_items, ApiOrchestrationConnector, connector_dict, limit)}


@router.post("/connectors", status_code=201)
async def create_connector(
    body: ConnectorPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        item = await _sync_call(lambda session: save_connector(session, _connector_payload(body), actor.username))
        result = connector_dict(item)
        await record_audit(db, actor, action="api_orchestration.connector_create", module="api-orchestration", target_type="connector", target_id=result["id"], target_name=result["name"], request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.put("/connectors/{item_id}")
async def update_connector(
    item_id: uuid.UUID,
    body: ConnectorPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        item = await _sync_call(lambda session: save_connector(session, _connector_payload(body), actor.username, item_id))
        result = connector_dict(item)
        await record_audit(db, actor, action="api_orchestration.connector_update", module="api-orchestration", target_type="connector", target_id=result["id"], target_name=result["name"], request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/connectors/{item_id}", status_code=204)
async def delete_connector(
    item_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        result = await _sync_call(_delete_item, ApiOrchestrationConnector, item_id, "连接器")
        await record_audit(db, actor, action="api_orchestration.connector_delete", module="api-orchestration", target_type="connector", target_id=result["id"], target_name=result["name"], request=request)
        return Response(status_code=204)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/connectors/{item_id}/test")
async def test_connector(
    item_id: uuid.UUID,
    body: InvokePayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:test")),
):
    try:
        result = await _sync_call(_test_connector, item_id, body.input)
        await record_audit(db, actor, action="api_orchestration.connector_test", module="api-orchestration", target_type="connector", target_id=str(item_id), payload={"success": result.get("success"), "status_code": result.get("transport", {}).get("status_code")}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/sql-apis")
async def list_sql_apis(
    limit: int = Query(default=200, ge=1, le=500),
    _: AuthContext = Depends(require_permission("apiOrchestration:read")),
):
    return {"items": await _sync_call(_list_items, ApiOrchestrationSqlApi, sql_api_dict, limit)}


@router.post("/sql-apis", status_code=201)
async def create_sql_api(
    body: SqlApiPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    if body.mode == "write" and not actor.has_permission("apiOrchestration:sqlWrite"):
        raise HTTPException(status_code=403, detail="Permission required: apiOrchestration:sqlWrite")
    try:
        item = await _sync_call(lambda session: save_sql_api(session, body.model_dump(), actor.username))
        result = sql_api_dict(item)
        await record_audit(db, actor, action="api_orchestration.sql_api_create", module="api-orchestration", target_type="sql_api", target_id=result["id"], target_name=result["name"], payload={"slug": result["slug"], "mode": result["mode"]}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.put("/sql-apis/{item_id}")
async def update_sql_api(
    item_id: uuid.UUID,
    body: SqlApiPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    if body.mode == "write" and not actor.has_permission("apiOrchestration:sqlWrite"):
        raise HTTPException(status_code=403, detail="Permission required: apiOrchestration:sqlWrite")
    try:
        item = await _sync_call(lambda session: save_sql_api(session, body.model_dump(), actor.username, item_id))
        result = sql_api_dict(item)
        await record_audit(db, actor, action="api_orchestration.sql_api_update", module="api-orchestration", target_type="sql_api", target_id=result["id"], target_name=result["name"], payload={"slug": result["slug"], "mode": result["mode"]}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/sql-apis/{item_id}", status_code=204)
async def delete_sql_api(
    item_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        result = await _sync_call(_delete_item, ApiOrchestrationSqlApi, item_id, "SQL API")
        await record_audit(db, actor, action="api_orchestration.sql_api_delete", module="api-orchestration", target_type="sql_api", target_id=result["id"], target_name=result["name"], request=request)
        return Response(status_code=204)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/sql/{slug}/invoke")
async def invoke_published_sql_api(
    slug: str,
    body: InvokePayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:execute")),
):
    try:
        result = await _sync_call(_invoke_sql_by_slug, slug, body.input, actor.has_permission("apiOrchestration:sqlWrite"))
        await record_audit(db, actor, action="api_orchestration.sql_api_invoke", module="api-orchestration", target_type="sql_api", target_id=slug, payload={"parameter_names": sorted(body.input), "row_count": result.get("row_count"), "affected_rows": result.get("affected_rows")}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/workflows")
async def list_workflows(
    limit: int = Query(default=200, ge=1, le=500),
    _: AuthContext = Depends(require_permission("apiOrchestration:read")),
):
    return {"items": await _sync_call(_list_items, ApiOrchestrationWorkflow, workflow_dict, limit)}


@router.post("/workflows", status_code=201)
async def create_workflow(
    body: WorkflowPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        item = await _sync_call(lambda session: save_workflow(session, body.model_dump(), actor.username))
        result = workflow_dict(item)
        await record_audit(db, actor, action="api_orchestration.workflow_create", module="api-orchestration", target_type="workflow", target_id=result["id"], target_name=result["name"], request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.put("/workflows/{item_id}")
async def update_workflow(
    item_id: uuid.UUID,
    body: WorkflowPayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        item = await _sync_call(lambda session: save_workflow(session, body.model_dump(), actor.username, item_id))
        result = workflow_dict(item)
        await record_audit(db, actor, action="api_orchestration.workflow_update", module="api-orchestration", target_type="workflow", target_id=result["id"], target_name=result["name"], request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/workflows/{item_id}", status_code=204)
async def delete_workflow(
    item_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:design")),
):
    try:
        result = await _sync_call(_delete_item, ApiOrchestrationWorkflow, item_id, "流程")
        await record_audit(db, actor, action="api_orchestration.workflow_delete", module="api-orchestration", target_type="workflow", target_id=result["id"], target_name=result["name"], request=request)
        return Response(status_code=204)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/workflows/{item_id}/publish")
async def publish_workflow(
    item_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:publish")),
):
    try:
        result = await _sync_call(_publish_workflow, item_id, actor.has_permission("apiOrchestration:sqlWrite"))
        await record_audit(db, actor, action="api_orchestration.workflow_publish", module="api-orchestration", target_type="workflow", target_id=result["id"], target_name=result["name"], payload={"revision": result["revision"]}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/workflows/{item_id}/run", status_code=202)
async def run_workflow(
    item_id: uuid.UUID,
    body: InvokePayload,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:execute")),
):
    try:
        result = await _sync_call(_submit_workflow_run, item_id, body.input, actor.username)
        await record_audit(db, actor, action="api_orchestration.workflow_run", module="api-orchestration", target_type="workflow_run", target_id=result["id"], target_name=result["workflow_name"], request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=100, ge=1, le=500),
    workflow_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    _: AuthContext = Depends(require_permission("apiOrchestration:read")),
):
    return {"items": await _sync_call(_list_runs, limit, workflow_id, state)}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("apiOrchestration:read")),
):
    try:
        return await _sync_call(_get_run, run_id)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_run(
    run_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    actor: AuthContext = Depends(require_permission("apiOrchestration:execute")),
):
    try:
        result = await _sync_call(_retry_run, run_id, actor.username)
        await record_audit(db, actor, action="api_orchestration.run_retry", module="api-orchestration", target_type="workflow_run", target_id=result["id"], payload={"previous_run_id": str(run_id)}, request=request)
        return result
    except Exception as exc:
        _raise_api_error(exc)
