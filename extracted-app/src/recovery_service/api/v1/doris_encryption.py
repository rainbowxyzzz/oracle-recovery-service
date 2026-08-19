import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.doris_encryption import (
    DorisSm4AutoSnapshotRequest,
    DorisSm4AutoSnapshotResponse,
    DorisSm4AutoSnapshotTaskListResponse,
    DorisSm4AutoSnapshotTaskUpdateRequest,
    DorisSm4BatchExecuteResponse,
    DorisSm4BatchListResponse,
    DorisSm4BatchRequest,
    DorisSm4BatchStatus,
    DorisEncryptionCatalogRequest,
    DorisEncryptionCatalogResponse,
    DorisEncryptionDatabaseListResponse,
    DorisEncryptionExecuteRequest,
    DorisEncryptionExecuteResponse,
    DorisEncryptionTaskStatus,
    DorisSm4FunctionDeploymentListResponse,
    DorisSm4FunctionRefreshRequest,
    DorisSm4FunctionRefreshResponse,
    DorisSm4KeyVersionListResponse,
    DorisSm4ScheduleCreateRequest,
    DorisSm4ScheduleLifecycleRequest,
    DorisSm4ScheduleListResponse,
    DorisSm4ScheduleResponse,
    DorisSm4ScheduleUpdateRequest,
    DorisSm4TaskDefinitionCreateRequest,
    DorisSm4TaskDefinitionListResponse,
    DorisSm4TaskDefinitionResponse,
    DorisSm4TaskReferenceResponse,
    DorisSm4TaskDefinitionUpdateRequest,
    DorisSm4TaskLogEntry,
    DorisSm4TaskLogResponse,
)
from recovery_service.services.database_connections import get_profile
from recovery_service.services.doris_encryption import (
    archive_sm4_schedule,
    archive_sm4_task_definition,
    create_sm4_auto_snapshot_task,
    create_sm4_batch_task,
    create_sm4_schedule,
    create_sm4_task_definition,
    create_encryption_task,
    delete_sm4_auto_snapshot_task,
    delete_sm4_schedule,
    get_encryption_task,
    get_sm4_batch_task,
    get_sm4_task_definition_references,
    list_sm4_batch_logs,
    list_sm4_batch_tasks,
    list_sm4_auto_snapshot_tasks,
    list_doris_encryption_catalog,
    list_doris_source_databases,
    list_sm4_schedules,
    list_sm4_task_definitions,
    restore_sm4_schedule,
    resume_sm4_schedule,
    run_sm4_schedule_now,
    run_sm4_auto_snapshot_task_now,
    run_sm4_task_definition,
    stop_sm4_batch_task,
    stop_sm4_schedule,
    update_sm4_schedule,
    update_sm4_auto_snapshot_task_interval,
    update_sm4_task_definition,
)
from recovery_service.services.doris_sm4_function import (
    list_sm4_function_deployments,
    refresh_sm4_functions,
    sm4_jar_path,
)
from recovery_service.services.file_decryption import decrypt_file_content
from recovery_service.services.sm4_key_versions import (
    get_sm4_key_seed,
    get_sm4_key_seed_for_batch,
    list_sm4_key_versions,
    sm4_key_fingerprint,
)
from recovery_service.services.auth import AuthContext
from recovery_service.services.audit import record_audit
from recovery_service.settings import get_settings

router = APIRouter(prefix="/doris-encryption", tags=["doris-encryption"])


@router.post("/file-decrypt")
async def decrypt_sm4_file(
    request: Request,
    file: UploadFile = File(...),
    columns: str = Form(...),
    sm4_key: str | None = Form(default=None),
    key_id: uuid.UUID | None = Form(default=None),
    batch_id: uuid.UUID | None = Form(default=None),
    output_format: str = Form(default="preserve"),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
    db: AsyncSession = Depends(get_db),
):
    try:
        content = await file.read()
        if not content:
            raise ValueError("上传文件不能为空。")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("文件不能超过 20MB。")
        decrypt_columns = _parse_column_list(columns)
        resolved_key_id = key_id
        resolved_fingerprint: str | None = None
        if key_id:
            resolved_key = get_sm4_key_seed(key_id)
        elif batch_id:
            resolved_key, resolved_key_id, resolved_fingerprint = get_sm4_key_seed_for_batch(batch_id)
        else:
            resolved_key = (sm4_key or "").strip()
        if not resolved_key:
            raise ValueError("请选择 SM4 密钥版本、历史加密批次，或手动输入 SM4 密钥种子。")
        result = await asyncio.to_thread(
            decrypt_file_content,
            filename=file.filename or "decrypt_input",
            content=content,
            decrypt_columns=decrypt_columns,
            sm4_key=resolved_key,
            output_format=output_format,
        )
        key_fingerprint = resolved_fingerprint or sm4_key_fingerprint(resolved_key)
        await record_audit(
            db,
            actor,
            action="decrypt_sm4_file",
            module="doris-encryption",
            target_type="file",
            target_name=file.filename,
            payload={
                "columns": decrypt_columns,
                "output_format": output_format,
                "row_count": result.row_count,
                "decrypted_count": result.decrypted_count,
                "failed_count": result.failed_count,
                "key_id": str(resolved_key_id) if resolved_key_id else None,
                "batch_id": str(batch_id) if batch_id else None,
                "key_fingerprint": key_fingerprint,
            },
            request=request,
        )
        return {
            "filename": result.filename,
            "format": result.format,
            "content_type": result.content_type,
            "content": result.content,
            "row_count": result.row_count,
            "decrypted_count": result.decrypted_count,
            "failed_count": result.failed_count,
            "errors": result.errors,
            "columns": result.columns,
        }
    except ValueError as exc:
        await record_audit(
            db,
            actor,
            action="decrypt_sm4_file",
            module="doris-encryption",
            target_type="file",
            target_name=file.filename,
            status="failed",
            payload={
                "columns": columns,
                "output_format": output_format,
                "key_id": str(key_id) if key_id else None,
                "batch_id": str(batch_id) if batch_id else None,
            },
            error_message=str(exc),
            request=request,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await record_audit(
            db,
            actor,
            action="decrypt_sm4_file",
            module="doris-encryption",
            target_type="file",
            target_name=file.filename,
            status="failed",
            payload={
                "columns": columns,
                "output_format": output_format,
                "key_id": str(key_id) if key_id else None,
                "batch_id": str(batch_id) if batch_id else None,
            },
            error_message=str(exc),
            request=request,
        )
        raise HTTPException(status_code=400, detail=f"SM4 文件解密失败：{exc}") from exc


def _parse_column_list(value: str) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("解密列参数必须是列名数组或逗号分隔文本。")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


@router.get("/function-jars/{filename}", include_in_schema=False)
async def download_sm4_function_jar(filename: str):
    try:
        path = sm4_jar_path(filename)
        return FileResponse(path, media_type="application/java-archive", filename=path.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sm4-keys", response_model=DorisSm4KeyVersionListResponse)
async def list_doris_sm4_keys(
    status: str = Query(default="active"),
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        return DorisSm4KeyVersionListResponse(keys=await asyncio.to_thread(list_sm4_key_versions, status=status, limit=limit))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SM4 key versions load failed: {exc}") from exc


async def _doris_profile(db: AsyncSession, connection_id: uuid.UUID):
    profile = await get_profile(db, connection_id)
    if profile.engine != "doris":
        raise HTTPException(status_code=422, detail="请选择 Doris 类型的数据连接。")
    return profile


@router.get("/databases", response_model=DorisEncryptionDatabaseListResponse)
async def list_doris_encryption_databases(
    connection_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        profile = await _doris_profile(db, connection_id)
        databases = await asyncio.to_thread(list_doris_source_databases, profile)
        return DorisEncryptionDatabaseListResponse(connection_id=connection_id, databases=databases)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 源数据库列表加载失败：{exc}") from exc


@router.post("/catalog", response_model=DorisEncryptionCatalogResponse)
async def catalog_doris_encryption(
    body: DorisEncryptionCatalogRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        return await asyncio.to_thread(
            list_doris_encryption_catalog,
            profile,
            database=body.database,
            keywords=body.keywords,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 加密字段扫描失败：{exc}") from exc


@router.post("/execute", response_model=DorisEncryptionExecuteResponse)
async def execute_doris_encryption(
    body: DorisEncryptionExecuteRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        task = create_encryption_task(
            profile,
            database=body.database,
            table_name=body.table_name,
            columns=body.columns,
            backup_suffix=body.backup_suffix,
            table_mode=body.table_mode,
        )
        return DorisEncryptionExecuteResponse(
            task_id=task.task_id,
            state="running",
            message="Doris 表加密任务已提交。",
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris 表加密任务提交失败：{exc}") from exc


@router.post("/batches", response_model=DorisSm4BatchExecuteResponse)
async def submit_doris_sm4_batch(
    body: DorisSm4BatchRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        task = await asyncio.to_thread(
            create_sm4_batch_task,
            profile,
            database=body.database,
            tables=[item.model_dump() for item in body.tables],
            table_strategy=body.table_strategy,
            target_suffix=body.target_suffix,
            schedule_id=body.schedule_id,
            key_id=body.key_id,
            execution_window_enabled=body.execution_window_enabled,
            execution_window_start=body.execution_window_start,
            execution_window_end=body.execution_window_end,
            allow_running_cross_window=body.allow_running_cross_window,
            actor=actor,
        )
        return DorisSm4BatchExecuteResponse(
            batch_id=task.batch_id,
            state="queued",
            message="Doris SM4 批次加密任务已提交。",
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 批次加密任务提交失败：{exc}") from exc


@router.post("/auto-snapshot-tasks", response_model=DorisSm4AutoSnapshotResponse)
async def submit_doris_sm4_auto_snapshot_task(
    body: DorisSm4AutoSnapshotRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        return await asyncio.to_thread(
            create_sm4_auto_snapshot_task,
            profile,
            name=body.name,
            include_databases=body.include_databases,
            exclude_databases=body.exclude_databases,
            exclude_tables=body.exclude_tables,
            keywords=body.keywords,
            table_strategy=body.table_strategy,
            target_suffix=body.target_suffix,
            execution_window_enabled=body.execution_window_enabled,
            execution_window_start=body.execution_window_start,
            execution_window_end=body.execution_window_end,
            allow_running_cross_window=body.allow_running_cross_window,
            scan_interval_minutes=body.scan_interval_minutes,
            actor=actor,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 自动快照任务提交失败：{exc}") from exc


@router.get("/auto-snapshot-tasks", response_model=DorisSm4AutoSnapshotTaskListResponse)
async def list_doris_sm4_auto_snapshot_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    tasks = await asyncio.to_thread(list_sm4_auto_snapshot_tasks, limit=limit)
    return DorisSm4AutoSnapshotTaskListResponse(tasks=tasks)


@router.patch("/auto-snapshot-tasks/{task_id}", response_model=DorisSm4AutoSnapshotTaskListResponse)
async def update_doris_sm4_auto_snapshot_task(
    task_id: uuid.UUID,
    body: DorisSm4AutoSnapshotTaskUpdateRequest,
    _: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    task = await asyncio.to_thread(update_sm4_auto_snapshot_task_interval, task_id, scan_interval_minutes=body.scan_interval_minutes)
    return DorisSm4AutoSnapshotTaskListResponse(tasks=[task])


@router.delete("/auto-snapshot-tasks/{task_id}", response_model=DorisSm4AutoSnapshotTaskListResponse)
async def delete_doris_sm4_auto_snapshot_task(
    task_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        task = await asyncio.to_thread(delete_sm4_auto_snapshot_task, task_id)
        return DorisSm4AutoSnapshotTaskListResponse(tasks=[task])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/auto-snapshot-tasks/{task_id}/scan", response_model=DorisSm4AutoSnapshotResponse)
async def scan_doris_sm4_auto_snapshot_task_now(
    task_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        return await asyncio.to_thread(run_sm4_auto_snapshot_task_now, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 自动快照立刻巡检失败：{exc}") from exc


@router.get("/batches", response_model=DorisSm4BatchListResponse)
async def list_doris_sm4_batches(
    connection_id: uuid.UUID | None = Query(default=None),
    database: str | None = Query(default=None),
    schedule_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    tasks = await asyncio.to_thread(
        list_sm4_batch_tasks,
        connection_id=connection_id,
        database=database,
        schedule_id=schedule_id,
        limit=limit,
    )
    return DorisSm4BatchListResponse(batches=tasks)


@router.get("/batches/{batch_id}", response_model=DorisSm4BatchStatus)
async def get_doris_sm4_batch(
    batch_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        return await asyncio.to_thread(get_sm4_batch_task, batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/batches/{batch_id}/logs", response_model=DorisSm4TaskLogResponse)
async def get_doris_sm4_batch_logs(
    batch_id: uuid.UUID,
    limit: int = Query(default=300, ge=1, le=1000),
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    logs = await asyncio.to_thread(list_sm4_batch_logs, batch_id, limit=limit)
    return DorisSm4TaskLogResponse(batch_id=batch_id, logs=[DorisSm4TaskLogEntry.model_validate(item) for item in logs])


@router.post("/batches/{batch_id}/stop", response_model=DorisSm4BatchStatus)
async def stop_doris_sm4_batch(
    batch_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        return await asyncio.to_thread(stop_sm4_batch_task, batch_id, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/task-definitions", response_model=DorisSm4TaskDefinitionListResponse)
async def list_doris_sm4_task_definitions(
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    return DorisSm4TaskDefinitionListResponse(tasks=await asyncio.to_thread(list_sm4_task_definitions))


@router.post("/task-definitions", response_model=DorisSm4TaskDefinitionResponse)
async def create_doris_sm4_task_definition(
    body: DorisSm4TaskDefinitionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        result = await asyncio.to_thread(
            create_sm4_task_definition,
            profile,
            name=body.name,
            database=body.database,
            tables=[item.model_dump() for item in body.tables],
            table_strategy=body.table_strategy,
            target_suffix=body.target_suffix,
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="create_sm4_task_definition",
            module="doris-encryption",
            target_type="doris_sm4_task_definition",
            target_id=str(result.task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/task-definitions/{task_id}/references", response_model=DorisSm4TaskReferenceResponse)
async def get_doris_sm4_task_definition_references(
    task_id: uuid.UUID,
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        return await asyncio.to_thread(get_sm4_task_definition_references, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/task-definitions/{task_id}", response_model=DorisSm4TaskDefinitionResponse)
async def update_doris_sm4_task_definition(
    task_id: uuid.UUID,
    body: DorisSm4TaskDefinitionUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        updates = body.model_dump(exclude_unset=True)
        if body.connection_id:
            profile = await _doris_profile(db, body.connection_id)
            updates["connection_name"] = profile.name
        result = await asyncio.to_thread(update_sm4_task_definition, task_id, updates=updates, actor=actor)
        await record_audit(
            db,
            actor,
            action="update_sm4_task_definition",
            module="doris-encryption",
            target_type="doris_sm4_task_definition",
            target_id=str(task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/task-definitions/{task_id}", response_model=DorisSm4TaskDefinitionResponse)
async def delete_doris_sm4_task_definition(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(archive_sm4_task_definition, task_id, actor)
        await record_audit(
            db,
            actor,
            action="archive_sm4_task_definition",
            module="doris-encryption",
            target_type="doris_sm4_task_definition",
            target_id=str(task_id),
            target_name=result.name,
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/task-definitions/{task_id}/run", response_model=DorisSm4BatchExecuteResponse)
async def run_doris_sm4_task_definition(
    task_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        task = await asyncio.to_thread(run_sm4_task_definition, task_id, actor)
        return DorisSm4BatchExecuteResponse(batch_id=task.batch_id, state="queued", message="Doris SM4 任务已提交执行。")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules", response_model=DorisSm4ScheduleResponse)
async def create_doris_sm4_schedule(
    body: DorisSm4ScheduleCreateRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        return await asyncio.to_thread(
            create_sm4_schedule,
            profile,
            name=body.name,
            database=body.database,
            tables=[item.model_dump() for item in body.tables],
            table_strategy=body.table_strategy,
            target_suffix=body.target_suffix,
            schedule_type=body.schedule_type,
            run_time=body.run_time,
            day_of_month=body.day_of_month,
            day_of_week=body.day_of_week,
            interval_minutes=body.interval_minutes,
            enabled=body.enabled,
            actor=actor,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 定时计划创建失败：{exc}") from exc


@router.get("/schedules", response_model=DorisSm4ScheduleListResponse)
async def list_doris_sm4_schedules(
    status: str = Query(default="normal"),
    _: AuthContext = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        return DorisSm4ScheduleListResponse(schedules=await asyncio.to_thread(list_sm4_schedules, status))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/schedules/{schedule_id}", response_model=DorisSm4ScheduleResponse)
async def update_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    body: DorisSm4ScheduleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        updates = body.model_dump(exclude_unset=True)
        if body.connection_id:
            profile = await _doris_profile(db, body.connection_id)
            updates["connection_name"] = profile.name
        return await asyncio.to_thread(update_sm4_schedule, schedule_id, updates=updates)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/stop", response_model=DorisSm4ScheduleResponse)
async def stop_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    body: DorisSm4ScheduleLifecycleRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(stop_sm4_schedule, schedule_id, actor, body.reason if body else None)
        await record_audit(
            db,
            actor,
            action="stop_sm4_schedule",
            module="doris-encryption",
            target_type="doris_sm4_schedule",
            target_id=str(schedule_id),
            target_name=result.name,
            payload={"status": result.status, "reason": body.reason if body else None},
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/resume", response_model=DorisSm4ScheduleResponse)
async def resume_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(resume_sm4_schedule, schedule_id, actor)
        await record_audit(
            db,
            actor,
            action="resume_sm4_schedule",
            module="doris-encryption",
            target_type="doris_sm4_schedule",
            target_id=str(schedule_id),
            target_name=result.name,
            payload={"status": result.status, "next_run_at": result.next_run_at.isoformat() if result.next_run_at else None},
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/archive", response_model=DorisSm4ScheduleResponse)
async def archive_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    body: DorisSm4ScheduleLifecycleRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(archive_sm4_schedule, schedule_id, actor, body.reason if body else None)
        await record_audit(
            db,
            actor,
            action="archive_sm4_schedule",
            module="doris-encryption",
            target_type="doris_sm4_schedule",
            target_id=str(schedule_id),
            target_name=result.name,
            payload={"status": result.status, "reason": body.reason if body else None},
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/restore", response_model=DorisSm4ScheduleResponse)
async def restore_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(restore_sm4_schedule, schedule_id, actor)
        await record_audit(
            db,
            actor,
            action="restore_sm4_schedule",
            module="doris-encryption",
            target_type="doris_sm4_schedule",
            target_id=str(schedule_id),
            target_name=result.name,
            payload={"status": result.status},
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/schedules/{schedule_id}", response_model=DorisSm4ScheduleResponse)
async def delete_doris_sm4_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    body: DorisSm4ScheduleLifecycleRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        result = await asyncio.to_thread(delete_sm4_schedule, schedule_id, actor, body.reason if body else None)
        await record_audit(
            db,
            actor,
            action="delete_sm4_schedule",
            module="doris-encryption",
            target_type="doris_sm4_schedule",
            target_id=str(schedule_id),
            target_name=result.name,
            payload={"status": result.status, "reason": body.reason if body else None},
            request=request,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/schedules/{schedule_id}/run", response_model=DorisSm4BatchExecuteResponse)
async def run_doris_sm4_schedule_now(
    schedule_id: uuid.UUID,
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        task = await asyncio.to_thread(run_sm4_schedule_now, schedule_id, actor)
        return DorisSm4BatchExecuteResponse(
            batch_id=task.batch_id,
            state="queued",
            message="Doris SM4 定时计划已手动触发。",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/functions/refresh", response_model=DorisSm4FunctionRefreshResponse)
async def refresh_doris_sm4_functions(
    body: DorisSm4FunctionRefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("dorisEncrypt:execute")),
):
    try:
        profile = await _doris_profile(db, body.connection_id)
        settings = get_settings()
        public_base_url = settings.doris_sm4_udf_public_base_url.strip()
        if not public_base_url:
            public_base_url = str(request.base_url).rstrip("/") + "/api/v1/doris-encryption/function-jars"
        sm4_key = None if body.key_mode == "random" else body.sm4_key
        return await asyncio.to_thread(
            refresh_sm4_functions,
            profile,
            sm4_key=sm4_key,
            key_mode=body.key_mode,
            public_base_url=public_base_url,
            function_name=body.function_name,
            include_system_databases=body.include_system_databases,
            databases=body.databases,
            actor=actor,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 函数刷新失败：{exc}") from exc


@router.get("/functions/deployments", response_model=DorisSm4FunctionDeploymentListResponse)
async def list_doris_sm4_function_deployments(
    connection_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        await _doris_profile(db, connection_id)
        deployments = await asyncio.to_thread(list_sm4_function_deployments, connection_id=connection_id)
        return DorisSm4FunctionDeploymentListResponse(connection_id=connection_id, deployments=deployments)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Doris SM4 函数部署状态加载失败：{exc}") from exc


@router.get("/tasks/{task_id}", response_model=DorisEncryptionTaskStatus)
async def get_doris_encryption_task(
    task_id: uuid.UUID,
    _: None = Depends(require_permission("dorisEncrypt:read")),
):
    try:
        return get_encryption_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
