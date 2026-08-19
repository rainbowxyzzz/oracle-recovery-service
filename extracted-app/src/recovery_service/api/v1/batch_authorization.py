from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.batch_authorization import (
    BatchAuthApplyDiscoveryRequest,
    BatchAuthDepartmentResponse,
    BatchAuthDepartmentRelationRequest,
    BatchAuthDiscoveryRequest,
    BatchAuthDiscoveryResponse,
    BatchAuthExtendRequest,
    BatchAuthGrantBatchResponse,
    BatchAuthGrantPreviewResponse,
    BatchAuthInitExecuteResponse,
    BatchAuthInitImportBatchResponse,
    BatchAuthInitPreviewResponse,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.batch_authorization import (
    execute_grant_import,
    execute_init_import,
    apply_discovered_mappings,
    create_department_relation,
    delete_department_relation,
    discover_initialization_mappings,
    extend_grant_batch,
    get_grant_batch,
    get_init_batch,
    list_departments,
    list_grant_batches,
    list_init_batches,
    offline_grant_batch,
    preview_grant_import,
    preview_init_import,
)

router = APIRouter(prefix="/batch-authorization", tags=["batch-authorization"])


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = file.filename or "upload.xlsx"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="上传文件为空。")
    return filename, content


@router.get("/departments", response_model=list[BatchAuthDepartmentResponse])
async def list_batch_auth_departments(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    return await list_departments(db)


@router.post("/department-relations", response_model=BatchAuthDepartmentResponse, status_code=201)
async def create_batch_auth_department_relation(
    request: Request,
    body: BatchAuthDepartmentRelationRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:manage")),
):
    try:
        result = await create_department_relation(
            db,
            connection_id=body.connection_id,
            department_name=body.department_name,
            db_username=body.db_username,
            display_name=body.display_name,
            department_database=body.department_database,
            default_password=body.default_password or "doris@2024",
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="batch_authorization.department_relation_create",
            module="batch-authorization",
            target_type="batch_auth_department",
            target_id=str(result.id),
            target_name=result.name,
            payload={"department_database": body.department_database, "db_username": body.db_username},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"新增部门关系失败：{exc}") from exc


@router.delete("/departments/{department_id}", status_code=204)
async def delete_batch_auth_department_relation(
    request: Request,
    department_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:manage")),
):
    try:
        await delete_department_relation(db, department_id)
        await record_audit(
            db,
            actor,
            action="batch_authorization.department_relation_delete",
            module="batch-authorization",
            target_type="batch_auth_department",
            target_id=str(department_id),
            request=request,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/discovery", response_model=BatchAuthDiscoveryResponse)
async def discover_batch_auth_initialization(
    body: BatchAuthDiscoveryRequest,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        return await discover_initialization_mappings(
            db,
            connection_id=body.connection_id,
            user_prefix=body.user_prefix,
            database_prefix=body.database_prefix,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"自动发现初始化映射失败：{exc}") from exc


@router.post("/discovery/apply", response_model=BatchAuthInitExecuteResponse)
async def apply_batch_auth_discovery(
    request: Request,
    body: BatchAuthApplyDiscoveryRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:import")),
):
    try:
        result = await apply_discovered_mappings(
            db,
            connection_id=body.connection_id,
            rows=body.rows,
            default_password=body.default_password or "doris@2024",
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="batch_authorization.discovery_apply",
            module="batch-authorization",
            target_type="batch_auth_init_import",
            target_id=str(result.batch.id),
            target_name="自动发现映射",
            payload={"state": result.batch.state, "total_count": result.batch.total_count},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"应用自动发现映射失败：{exc}") from exc


@router.post("/init-imports/preview", response_model=BatchAuthInitPreviewResponse)
async def preview_batch_auth_init_import(
    connection_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        filename, content = await _read_upload(file)
        return await preview_init_import(db, connection_id=connection_id, filename=filename, content=content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"初始化导入预览失败：{exc}") from exc


@router.post("/init-imports/execute", response_model=BatchAuthInitExecuteResponse)
async def execute_batch_auth_init_import(
    request: Request,
    connection_id: uuid.UUID = Form(...),
    default_password: str = Form(default="doris@2024"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:import")),
):
    try:
        filename, content = await _read_upload(file)
        result = await execute_init_import(
            db,
            connection_id=connection_id,
            filename=filename,
            content=content,
            default_password=default_password or "doris@2024",
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="batch_authorization.init_import",
            module="batch-authorization",
            target_type="batch_auth_init_import",
            target_id=str(result.batch.id),
            target_name=filename,
            payload={"state": result.batch.state, "total_count": result.batch.total_count},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"初始化导入执行失败：{exc}") from exc


@router.get("/init-imports", response_model=list[BatchAuthInitImportBatchResponse])
async def list_batch_auth_init_imports(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    return await list_init_batches(db)


@router.get("/init-imports/{batch_id}", response_model=BatchAuthInitImportBatchResponse)
async def get_batch_auth_init_import(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        return await get_init_batch(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/grant-imports/preview", response_model=BatchAuthGrantPreviewResponse)
async def preview_batch_auth_grant_import(
    connection_id: uuid.UUID = Form(...),
    department_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        filename, content = await _read_upload(file)
        return await preview_grant_import(
            db,
            connection_id=connection_id,
            department_id=department_id,
            filename=filename,
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"授权导入预览失败：{exc}") from exc


@router.post("/grant-imports/execute", response_model=BatchAuthGrantBatchResponse)
async def execute_batch_auth_grant_import(
    request: Request,
    connection_id: uuid.UUID = Form(...),
    department_id: uuid.UUID = Form(...),
    expires_at: datetime = Form(...),
    name: str = Form(default=""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        filename, content = await _read_upload(file)
        result = await execute_grant_import(
            db,
            connection_id=connection_id,
            department_id=department_id,
            filename=filename,
            content=content,
            name=name,
            expires_at=expires_at,
            actor=actor,
        )
        await record_audit(
            db,
            actor,
            action="batch_authorization.grant_import",
            module="batch-authorization",
            target_type="batch_auth_grant_batch",
            target_id=str(result.id),
            target_name=result.name,
            payload={"state": result.state, "department": result.department_name},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"授权导入执行失败：{exc}") from exc


@router.get("/grant-batches", response_model=list[BatchAuthGrantBatchResponse])
async def list_batch_auth_grant_batches(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    return await list_grant_batches(db)


@router.get("/grant-batches/{batch_id}", response_model=BatchAuthGrantBatchResponse)
async def get_batch_auth_grant_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_permission("batchAuthorization:read")),
):
    try:
        return await get_grant_batch(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/grant-batches/{batch_id}/offline", response_model=BatchAuthGrantBatchResponse)
async def offline_batch_auth_grant_batch(
    request: Request,
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        result = await offline_grant_batch(db, batch_id)
        await record_audit(
            db,
            actor,
            action="batch_authorization.offline",
            module="batch-authorization",
            target_type="batch_auth_grant_batch",
            target_id=str(batch_id),
            target_name=result.name,
            payload={"state": result.state},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"授权批次下线失败：{exc}") from exc


@router.post("/grant-batches/{batch_id}/extend", response_model=BatchAuthGrantBatchResponse)
async def extend_batch_auth_grant_batch(
    request: Request,
    batch_id: uuid.UUID,
    body: BatchAuthExtendRequest,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("batchAuthorization:execute")),
):
    try:
        result = await extend_grant_batch(db, batch_id, body.expires_at)
        await record_audit(
            db,
            actor,
            action="batch_authorization.extend",
            module="batch-authorization",
            target_type="batch_auth_grant_batch",
            target_id=str(batch_id),
            target_name=result.name,
            payload={"expires_at": result.expires_at.isoformat()},
            request=request,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
