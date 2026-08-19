import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_current_actor, get_db, require_permission
from recovery_service.api.schemas.data_platform import (
    DataPlatformDashboardResponse,
    DataPlatformChangeProbeListResponse,
    DataPlatformChangeTriggerBaselineRequest,
    DataPlatformChangeTriggerEnableRequest,
    DataPlatformChangeTriggerListResponse,
    DataPlatformChangeTriggerResponse,
    DataPlatformChangeTriggerUpdateRequest,
    DataPlatformComponentRunListResponse,
    DataPlatformFolderCreateRequest,
    DataPlatformFolderListResponse,
    DataPlatformFolderResponse,
    DataPlatformFolderUpdateRequest,
    DataPlatformNodeCreateRequest,
    DataPlatformNodeListResponse,
    DataPlatformNodeResponse,
    DataPlatformNodeRunRequest,
    DataPlatformNodeRunListResponse,
    DataPlatformNodeUpdateRequest,
    DataPlatformRunListResponse,
    DataPlatformRunResponse,
    DataPlatformScheduleListResponse,
    DataPlatformVersionCreateRequest,
    DataPlatformVersionListResponse,
    DataPlatformVersionResponse,
    DataPlatformVersionUpdateRequest,
    DataPlatformWorkflowCreateRequest,
    DataPlatformWorkflowCopyRequest,
    DataPlatformWorkflowListResponse,
    DataPlatformWorkflowResponse,
    DataPlatformWorkflowUpdateRequest,
    DataSyncRecognizeRequest,
)
from recovery_service.api.schemas.doris_sql_etl import DorisSqlObjectListResponse
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.database_connections import get_profile
from recovery_service.services.data_change_trigger import (
    list_change_probes,
    list_change_triggers,
    probe_change_trigger_now,
    rebuild_change_trigger_baseline,
    update_change_trigger_state,
)
from recovery_service.services.data_platform import (
    archive_folder,
    archive_workflow,
    copy_workflow,
    create_folder,
    create_node,
    create_version,
    create_workflow,
    dashboard,
    delete_node,
    list_folders,
    list_component_runs,
    list_node_runs,
    list_nodes,
    list_runs,
    list_schedules,
    list_versions,
    list_workflows,
    offline_version,
    publish_version,
    run_version,
    submit_version,
    update_node,
    submit_component_task_run,
    get_change_trigger_task_status,
    probe_change_trigger_task_now,
    publish_change_trigger_task,
    run_change_trigger_task_once,
    set_change_trigger_task_enabled,
    validate_change_trigger_task,
    update_folder,
    update_version,
    update_workflow,
)
from recovery_service.services.data_sync import (
    list_data_sync_source_catalogs,
    list_data_sync_source_databases,
    recognize_data_sync_mappings,
)
from recovery_service.services.doris_sql_etl import (
    list_doris_columns,
    list_doris_tables,
)

router = APIRouter(prefix="/data-platform", tags=["data-platform"])


@router.get("/metadata/databases", response_model=DorisSqlObjectListResponse)
async def get_data_platform_doris_databases(
    connection_id: uuid.UUID,
    catalog: str = Query(default="internal"),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_data_sync_source_databases, profile, catalog=catalog)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 数据库读取失败：{exc}") from exc


@router.get("/metadata/catalogs", response_model=DorisSqlObjectListResponse)
async def get_data_platform_doris_catalogs(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(list_data_sync_source_catalogs, profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris Catalog 读取失败：{exc}") from exc


@router.get("/metadata/tables", response_model=DorisSqlObjectListResponse)
async def get_data_platform_doris_tables(
    connection_id: uuid.UUID,
    database: str,
    catalog: str = Query(default="internal"),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(
            list_doris_tables,
            profile,
            catalog=catalog,
            database=database,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 表读取失败：{exc}") from exc


@router.get("/metadata/columns", response_model=DorisSqlObjectListResponse)
async def get_data_platform_doris_columns(
    connection_id: uuid.UUID,
    database: str,
    table: str,
    catalog: str = Query(default="internal"),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await asyncio.to_thread(
            list_doris_columns,
            profile,
            catalog=catalog,
            database=database,
            table=table,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 字段读取失败：{exc}") from exc


@router.post("/data-sync/recognize")
async def recognize_data_platform_data_sync(
    body: DataSyncRecognizeRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        source = await get_profile(db, body.source_connection_id)
        if source.engine != "doris" and not body.target_connection_id:
            raise ValueError("MySQL 源连接必须显式选择目标 Doris 连接。")
        target = await get_profile(db, body.target_connection_id or body.source_connection_id)
        return await asyncio.to_thread(
            recognize_data_sync_mappings,
            source,
            target,
            source_catalog=body.source_catalog,
            source_schema=body.source_schema,
            target_database=body.target_database,
            schema_policy=body.schema_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"数据同步映射识别失败：{exc}") from exc


@router.post("/nodes/{node_id}/trigger/validate")
async def validate_data_change_trigger_task(
    node_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        return await asyncio.to_thread(validate_change_trigger_task, node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/nodes/{node_id}/trigger/publish")
async def publish_data_change_trigger_task(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        result = await asyncio.to_thread(publish_change_trigger_task, node_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="publish_data_change_trigger_task",
        module="data-platform",
        target_type="data_platform_node",
        target_id=str(node_id),
        request=request,
    )
    return result


@router.get("/nodes/{node_id}/trigger/status")
async def get_data_change_trigger_task_status(
    node_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dataPlatform:monitor")),
):
    try:
        return await asyncio.to_thread(get_change_trigger_task_status, node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/nodes/{node_id}/trigger/status")
async def update_data_change_trigger_task_status(
    node_id: uuid.UUID,
    body: DataPlatformChangeTriggerEnableRequest,
    _: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        return await asyncio.to_thread(
            set_change_trigger_task_enabled,
            node_id,
            enabled=body.enabled,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/nodes/{node_id}/trigger/probe")
async def probe_data_change_trigger_task(
    node_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dataPlatform:execute")),
):
    try:
        return await asyncio.to_thread(probe_change_trigger_task_now, node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/nodes/{node_id}/trigger/run-once")
async def run_data_change_trigger_task_once(
    node_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dataPlatform:execute")),
):
    try:
        return await asyncio.to_thread(run_change_trigger_task_once, node_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/change-triggers", response_model=DataPlatformChangeTriggerListResponse)
async def list_data_platform_change_triggers(
    version_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _: AuthContext = Depends(require_permission("dataPlatform:monitor")),
):
    return DataPlatformChangeTriggerListResponse(
        triggers=await asyncio.to_thread(list_change_triggers, version_id=version_id, limit=limit)
    )


@router.patch("/change-triggers/{trigger_id}", response_model=DataPlatformChangeTriggerResponse)
async def update_data_platform_change_trigger(
    trigger_id: uuid.UUID,
    body: DataPlatformChangeTriggerUpdateRequest,
    _: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        return await asyncio.to_thread(update_change_trigger_state, trigger_id, enabled=body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/change-triggers/{trigger_id}/probe", response_model=DataPlatformChangeTriggerResponse)
async def probe_data_platform_change_trigger(
    trigger_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dataPlatform:execute")),
):
    try:
        return await asyncio.to_thread(probe_change_trigger_now, trigger_id, run_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/change-triggers/{trigger_id}/baseline", response_model=DataPlatformChangeTriggerResponse)
async def rebuild_data_platform_change_trigger_baseline(
    trigger_id: uuid.UUID,
    body: DataPlatformChangeTriggerBaselineRequest,
    _: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        return await asyncio.to_thread(rebuild_change_trigger_baseline, trigger_id, confirm=body.confirm)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/change-triggers/{trigger_id}/probes", response_model=DataPlatformChangeProbeListResponse)
async def list_data_platform_change_probes(
    trigger_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthContext = Depends(require_permission("dataPlatform:monitor")),
):
    return DataPlatformChangeProbeListResponse(
        probes=await asyncio.to_thread(list_change_probes, trigger_id=trigger_id, limit=limit)
    )


@router.get("/dashboard", response_model=DataPlatformDashboardResponse)
async def get_data_platform_dashboard(_: AuthContext = Depends(require_permission("dataPlatform:read"))):
    return await asyncio.to_thread(dashboard)


@router.get("/nodes", response_model=DataPlatformNodeListResponse)
async def list_data_platform_nodes(_: AuthContext = Depends(require_permission("dataPlatform:read"))):
    return DataPlatformNodeListResponse(nodes=await asyncio.to_thread(list_nodes))


@router.post("/nodes", response_model=DataPlatformNodeResponse)
async def create_data_platform_node(
    body: DataPlatformNodeCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(
            create_node,
            name=body.name,
            node_type=body.node_type,
            description=body.description,
            config=body.config,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="create_data_platform_node",
        module="data-platform",
        target_type="data_platform_node",
        target_id=str(result.node_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.patch("/nodes/{node_id}", response_model=DataPlatformNodeResponse)
async def update_data_platform_node(
    node_id: uuid.UUID,
    body: DataPlatformNodeUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(update_node, node_id, body.model_dump(exclude_unset=True), actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="update_data_platform_node",
        module="data-platform",
        target_type="data_platform_node",
        target_id=str(node_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.post("/nodes/{node_id}/run")
async def run_data_platform_component_task(
    node_id: uuid.UUID,
    request: Request,
    body: DataPlatformNodeRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:execute")),
):
    try:
        result = await asyncio.to_thread(
            submit_component_task_run,
            node_id,
            body.model_dump(exclude_none=True) if body else None,
            actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="run_data_platform_component_task",
        module="data-platform",
        target_type="data_platform_node",
        target_id=str(node_id),
        request=request,
    )
    return result


@router.get("/nodes/{node_id}/component-runs", response_model=DataPlatformComponentRunListResponse)
async def list_data_platform_component_runs(
    node_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    _: AuthContext = Depends(require_permission("dataPlatform:read")),
):
    return DataPlatformComponentRunListResponse(
        runs=await asyncio.to_thread(list_component_runs, node_id, limit)
    )


@router.delete("/nodes/{node_id}", response_model=DataPlatformNodeResponse)
async def delete_data_platform_node(
    node_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(delete_node, node_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="delete_data_platform_node",
        module="data-platform",
        target_type="data_platform_node",
        target_id=str(node_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.get("/folders", response_model=DataPlatformFolderListResponse)
async def list_data_platform_folders(_: AuthContext = Depends(require_permission("dataPlatform:read"))):
    return DataPlatformFolderListResponse(folders=await asyncio.to_thread(list_folders))


@router.post("/folders", response_model=DataPlatformFolderResponse)
async def create_data_platform_folder(
    body: DataPlatformFolderCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(create_folder, name=body.name, parent_id=body.parent_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="create_data_platform_folder",
        module="data-platform",
        target_type="data_platform_folder",
        target_id=str(result.folder_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.patch("/folders/{folder_id}", response_model=DataPlatformFolderResponse)
async def update_data_platform_folder(
    folder_id: uuid.UUID,
    body: DataPlatformFolderUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(update_folder, folder_id, body.model_dump(exclude_unset=True), actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="update_data_platform_folder",
        module="data-platform",
        target_type="data_platform_folder",
        target_id=str(folder_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.delete("/folders/{folder_id}", response_model=DataPlatformFolderResponse)
async def delete_data_platform_folder(
    folder_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(archive_folder, folder_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="archive_data_platform_folder",
        module="data-platform",
        target_type="data_platform_folder",
        target_id=str(folder_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.get("/workflows", response_model=DataPlatformWorkflowListResponse)
async def list_data_platform_workflows(_: AuthContext = Depends(require_permission("dataPlatform:read"))):
    return DataPlatformWorkflowListResponse(workflows=await asyncio.to_thread(list_workflows))


@router.post("/workflows", response_model=DataPlatformWorkflowResponse)
async def create_data_platform_workflow(
    body: DataPlatformWorkflowCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(create_workflow, name=body.name, description=body.description, folder_id=body.folder_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="create_data_platform_workflow",
        module="data-platform",
        target_type="data_platform_workflow",
        target_id=str(result.workflow_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.patch("/workflows/{workflow_id}", response_model=DataPlatformWorkflowResponse)
async def update_data_platform_workflow(
    workflow_id: uuid.UUID,
    body: DataPlatformWorkflowUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(update_workflow, workflow_id, body.model_dump(exclude_unset=True), actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="update_data_platform_workflow",
        module="data-platform",
        target_type="data_platform_workflow",
        target_id=str(workflow_id),
        target_name=result.name,
        request=request,
    )
    return result


@router.post("/workflows/{workflow_id}/copy", response_model=DataPlatformWorkflowResponse)
async def copy_data_platform_workflow(
    workflow_id: uuid.UUID,
    body: DataPlatformWorkflowCopyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(
            copy_workflow,
            workflow_id,
            name=body.name,
            folder_id=body.folder_id,
            actor=actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="copy_data_platform_workflow",
        module="data-platform",
        target_type="data_platform_workflow",
        target_id=str(result.workflow_id),
        target_name=result.name,
        payload={"source_workflow_id": str(workflow_id)},
        request=request,
    )
    return result


@router.delete("/workflows/{workflow_id}", response_model=DataPlatformWorkflowResponse)
async def delete_data_platform_workflow(
    workflow_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(archive_workflow, workflow_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="archive_data_platform_workflow",
        module="data-platform",
        target_type="data_platform_workflow",
        target_id=str(workflow_id),
        target_name=result.name,
        payload={"status": result.status},
        request=request,
    )
    return result


@router.get("/versions", response_model=DataPlatformVersionListResponse)
async def list_data_platform_versions(
    workflow_id: uuid.UUID | None = Query(default=None),
    _: AuthContext = Depends(require_permission("dataPlatform:read")),
):
    return DataPlatformVersionListResponse(versions=await asyncio.to_thread(list_versions, workflow_id))


@router.get("/schedules", response_model=DataPlatformScheduleListResponse)
async def list_data_platform_schedules(
    include_disabled: bool = Query(default=True),
    _: AuthContext = Depends(require_permission("dataPlatform:read")),
):
    return DataPlatformScheduleListResponse(
        schedules=await asyncio.to_thread(list_schedules, include_disabled=include_disabled)
    )


@router.post("/workflows/{workflow_id}/versions", response_model=DataPlatformVersionResponse)
async def create_data_platform_version(
    workflow_id: uuid.UUID,
    body: DataPlatformVersionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:design")),
):
    try:
        result = await asyncio.to_thread(create_version, workflow_id, body.model_dump(), actor)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="create_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow",
        target_id=str(workflow_id),
        payload={"version_id": str(result.version_id), "channel": result.channel},
        request=request,
    )
    return result


@router.patch("/versions/{version_id}", response_model=DataPlatformVersionResponse)
async def update_data_platform_version(
    version_id: uuid.UUID,
    body: DataPlatformVersionUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(get_current_actor),
):
    try:
        result = await asyncio.to_thread(update_version, version_id, body.model_dump(exclude_unset=True), actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="update_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow_version",
        target_id=str(version_id),
        payload={"status": result.status, "channel": result.channel},
        request=request,
    )
    return result


@router.post("/versions/{version_id}/submit", response_model=DataPlatformVersionResponse)
async def submit_data_platform_version(
    version_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        result = await asyncio.to_thread(submit_version, version_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="submit_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow_version",
        target_id=str(version_id),
        payload={"prod_version_id": str(result.version_id)},
        request=request,
    )
    return result


@router.post("/versions/{version_id}/publish", response_model=DataPlatformVersionResponse)
async def publish_data_platform_version(
    version_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        result = await asyncio.to_thread(publish_version, version_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="publish_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow_version",
        target_id=str(version_id),
        payload={"status": result.status, "next_run_at": result.next_run_at.isoformat() if result.next_run_at else None},
        request=request,
    )
    return result


@router.post("/versions/{version_id}/offline", response_model=DataPlatformVersionResponse)
async def offline_data_platform_version(
    version_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:publish")),
):
    try:
        result = await asyncio.to_thread(offline_version, version_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="offline_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow_version",
        target_id=str(version_id),
        payload={"status": result.status},
        request=request,
    )
    return result


@router.post("/versions/{version_id}/run", response_model=DataPlatformRunResponse)
async def run_data_platform_version(
    version_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dataPlatform:execute")),
):
    try:
        result = await asyncio.to_thread(run_version, version_id, trigger_type="manual", actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        db,
        actor,
        action="run_data_platform_version",
        module="data-platform",
        target_type="data_platform_workflow_version",
        target_id=str(version_id),
        payload={"run_id": str(result.run_id)},
        request=request,
    )
    return result


@router.get("/runs", response_model=DataPlatformRunListResponse)
async def list_data_platform_runs(
    workflow_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    _: AuthContext = Depends(require_permission("dataPlatform:monitor")),
):
    return DataPlatformRunListResponse(runs=await asyncio.to_thread(list_runs, workflow_id, limit))


@router.get("/runs/{run_id}/nodes", response_model=DataPlatformNodeRunListResponse)
async def list_data_platform_node_runs(
    run_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dataPlatform:monitor")),
):
    return DataPlatformNodeRunListResponse(nodes=await asyncio.to_thread(list_node_runs, run_id))
