import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.doris_sm3_mapping import (
    DorisSm3CatalogRequest,
    DorisSm3CatalogResponse,
    DorisSm3ExecuteRequest,
    DorisSm3ExecuteResponse,
    DorisSm3JobListResponse,
    DorisSm3QueueStatusResponse,
    DorisSm3TaskDefinitionCreateRequest,
    DorisSm3TaskDefinitionListResponse,
    DorisSm3TaskDefinitionResponse,
    DorisSm3TaskDefinitionUpdateRequest,
    DorisSm3TaskLogResponse,
    DorisSm3TaskStatus,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.database_connections import get_profile
from recovery_service.services.doris_sm3_mapping import (
    cancel_sm3_mapping_task,
    archive_sm3_task_definition,
    create_sm3_mapping_task,
    create_sm3_task_definition,
    get_sm3_mapping_task,
    get_sm3_queue_status,
    list_sm3_mapping_task_logs,
    list_sm3_mapping_tasks,
    list_doris_sm3_catalog,
    list_sm3_task_definitions,
    run_sm3_task_definition,
    update_sm3_task_definition,
)

router = APIRouter(prefix="/doris-sm3", tags=["doris-sm3"])


async def _doris_profile(db: AsyncSession, connection_id: uuid.UUID):
    profile = await get_profile(db, connection_id)
    if profile.engine != "doris":
        raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据连接。")
    return profile


@router.post("/catalog", response_model=DorisSm3CatalogResponse)
async def catalog_doris_sm3(
    body: DorisSm3CatalogRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSm3:read")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        return await asyncio.to_thread(
            list_doris_sm3_catalog,
            profile,
            database=body.database,
            keywords=body.keywords,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM3 字段扫描失败：{exc}") from exc


@router.post("/execute", response_model=DorisSm3ExecuteResponse)
async def execute_doris_sm3(
    body: DorisSm3ExecuteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        task = await create_sm3_mapping_task(
            db,
            profile,
            database=body.database,
            table_name=body.table_name,
            columns=body.columns,
            mapping_database=body.mapping_database,
            mapping_tables=body.mapping_tables,
            field_mapping_database=body.field_mapping_database,
            field_mapping_table=body.field_mapping_table,
            output_suffix=body.output_suffix,
            table_mode=body.table_mode,
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="submit_sm3_task",
            module="doris-sm3",
            target_type="task",
            target_id=str(task.task_id),
            target_name=f"{body.database}.{body.table_name}",
            payload={"columns_count": len(body.columns), "table_mode": body.table_mode},
            request=request,
        )
        return DorisSm3ExecuteResponse(
            task_id=task.task_id,
            state="queued",
            message="Doris SM3 映射脱敏任务已进入队列。",
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM3 映射脱敏任务提交失败：{exc}") from exc


@router.get("/tasks", response_model=DorisSm3JobListResponse)
async def list_doris_sm3_tasks(
    connection_id: uuid.UUID | None = Query(default=None),
    database: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSm3:read")),
):
    return await list_sm3_mapping_tasks(db, connection_id=connection_id, database=database, limit=limit)


@router.get("/task-definitions", response_model=DorisSm3TaskDefinitionListResponse)
async def list_doris_sm3_task_definitions(
    _: AuthContext = Depends(require_permission("dorisSm3:read")),
):
    return DorisSm3TaskDefinitionListResponse(tasks=await asyncio.to_thread(list_sm3_task_definitions))


@router.post("/task-definitions", response_model=DorisSm3TaskDefinitionResponse)
async def create_doris_sm3_task_definition(
    body: DorisSm3TaskDefinitionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        result = await asyncio.to_thread(
            create_sm3_task_definition,
            profile,
            name=body.name,
            database=body.database,
            table_name=body.table_name,
            columns=body.columns,
            mapping_database=body.mapping_database,
            mapping_tables=body.mapping_tables,
            field_mapping_database=body.field_mapping_database,
            field_mapping_table=body.field_mapping_table,
            output_suffix=body.output_suffix,
            table_mode=body.table_mode,
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="create_sm3_task_definition",
            module="doris-sm3",
            target_type="doris_sm3_task_definition",
            target_id=str(result.task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/task-definitions/{task_id}", response_model=DorisSm3TaskDefinitionResponse)
async def update_doris_sm3_task_definition(
    task_id: uuid.UUID,
    body: DorisSm3TaskDefinitionUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:execute")),
):
    try:
        updates = body.model_dump(exclude_unset=True)
        if "connection_id" in updates and updates["connection_id"] is not None:
            profile = await _doris_profile(db, updates["connection_id"])
            updates["connection_name"] = profile.name
        result = await asyncio.to_thread(update_sm3_task_definition, task_id, updates=updates, actor=actor)
        await record_audit(
            db,
            actor,
            action="update_sm3_task_definition",
            module="doris-sm3",
            target_type="doris_sm3_task_definition",
            target_id=str(result.task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/task-definitions/{task_id}", response_model=DorisSm3TaskDefinitionResponse)
async def delete_doris_sm3_task_definition(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:execute")),
):
    try:
        result = await asyncio.to_thread(archive_sm3_task_definition, task_id, actor)
        await record_audit(
            db,
            actor,
            action="archive_sm3_task_definition",
            module="doris-sm3",
            target_type="doris_sm3_task_definition",
            target_id=str(result.task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/task-definitions/{task_id}/run", response_model=DorisSm3TaskStatus)
async def run_doris_sm3_task_definition(
    task_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dorisSm3:execute")),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await run_sm3_task_definition(db, task_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/queue", response_model=DorisSm3QueueStatusResponse)
async def get_doris_sm3_queue_status(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSm3:read")),
):
    return await get_sm3_queue_status(db)


@router.get("/tasks/{task_id}", response_model=DorisSm3TaskStatus)
async def get_doris_sm3_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSm3:read")),
):
    try:
        return await get_sm3_mapping_task(db, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/logs", response_model=DorisSm3TaskLogResponse)
async def get_doris_sm3_task_logs(
    task_id: uuid.UUID,
    limit: int = Query(default=500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisSm3Logs:read")),
):
    return await list_sm3_mapping_task_logs(db, task_id, limit=limit)


@router.post("/tasks/{task_id}/cancel", response_model=DorisSm3TaskStatus)
async def cancel_doris_sm3_task(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisSm3:cancel")),
):
    try:
        result = await cancel_sm3_mapping_task(db, task_id, actor=actor)
        await record_audit(
            db,
            actor,
            action="cancel_sm3_task",
            module="doris-sm3",
            target_type="task",
            target_id=str(task_id),
            target_name=f"{result.database}.{result.table_name}",
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
