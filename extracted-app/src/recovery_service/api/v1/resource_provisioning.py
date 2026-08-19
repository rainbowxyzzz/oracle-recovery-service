import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.resource_provisioning import (
    ResourcePermissionBatchCreateRequest,
    ResourcePermissionBatchListResponse,
    ResourcePermissionBatchResponse,
    ResourceProvisioningBatchCreateRequest,
    ResourceProvisioningBatchListResponse,
    ResourceProvisioningBatchResponse,
    ResourceProvisioningPreviewResponse,
)
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext
from recovery_service.services.resource_permissions import (
    ResourcePermissionStepError,
    create_permission_batch,
    delete_permission_role,
    get_permission_batch,
    list_permission_batches,
    prepare_permission_retry,
)
from recovery_service.services.resource_provisioning import (
    create_batch,
    get_batch,
    list_batches,
    prepare_retry,
    preview_file,
)
from recovery_service.settings import get_settings
from recovery_service.workers.tasks.resource_provisioning import (
    run_resource_permission_batch,
    run_resource_provisioning_batch,
)

router = APIRouter(prefix="/resource-provisioning", tags=["resource-provisioning"])


@router.post("/preview", response_model=ResourceProvisioningPreviewResponse)
async def preview_resource_provisioning_file(
    file: UploadFile = File(...),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:read")),
):
    del actor
    try:
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Excel 文件不能超过 10MB。")
        return preview_file(file.filename or "batch.xlsx", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/batches", response_model=ResourceProvisioningBatchResponse)
async def submit_resource_provisioning_batch(
    body: ResourceProvisioningBatchCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:execute")),
):
    try:
        batch = await create_batch(db, body, actor)
        run_resource_provisioning_batch.apply_async(
            args=[str(batch.id)],
            queue=get_settings().celery_resource_provisioning_queue,
        )
        await record_audit(
            db,
            actor,
            action="resource_provisioning.submit",
            module="resource-provisioning",
            target_type="resource_provisioning_batch",
            target_id=str(batch.id),
            target_name=batch.filename,
            payload={
                "connection_id": str(batch.connection_id),
                "project_id": batch.project_id,
                "parallelism": batch.parallelism,
                "total_count": batch.total_count,
                "api_url": batch.api_url,
                "youdata_login_name": batch.youdata_login_name,
                "youdata_token_url": batch.youdata_token_url,
                "token_strategy": (
                    "youdata_user_password"
                    if batch.youdata_login_name
                    else "legacy_manual_token"
                ),
            },
            request=request,
        )
        return await get_batch(db, batch.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"开通任务提交失败：{exc}") from exc


@router.get("/batches", response_model=ResourceProvisioningBatchListResponse)
async def list_resource_provisioning_batches(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:read")),
):
    del actor
    return ResourceProvisioningBatchListResponse(items=await list_batches(db, limit))


@router.get("/batches/{batch_id}", response_model=ResourceProvisioningBatchResponse)
async def get_resource_provisioning_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:read")),
):
    del actor
    try:
        return await get_batch(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/batches/{batch_id}/retry", response_model=ResourceProvisioningBatchResponse)
async def retry_resource_provisioning_batch(
    batch_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:execute")),
):
    try:
        batch = await prepare_retry(db, batch_id)
        run_resource_provisioning_batch.apply_async(
            args=[str(batch.id)],
            queue=get_settings().celery_resource_provisioning_queue,
        )
        await record_audit(
            db,
            actor,
            action="resource_provisioning.retry",
            module="resource-provisioning",
            target_type="resource_provisioning_batch",
            target_id=str(batch.id),
            target_name=batch.filename,
            payload={"failed_only": True},
            request=request,
        )
        return await get_batch(db, batch.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"重试任务提交失败：{exc}") from exc


@router.post("/permission-batches", response_model=ResourcePermissionBatchResponse)
async def submit_resource_permission_batch(
    body: ResourcePermissionBatchCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:execute")),
):
    try:
        batch = await create_permission_batch(db, body, actor)
        run_resource_permission_batch.apply_async(
            args=[str(batch.id)],
            queue=get_settings().celery_resource_provisioning_queue,
        )
        await record_audit(
            db,
            actor,
            action="resource_provisioning.permission_submit",
            module="resource-provisioning",
            target_type="resource_permission_batch",
            target_id=str(batch.id),
            target_name=batch.source_filename,
            payload={
                "source_batch_id": str(batch.source_batch_id),
                "lookup_connection_id": str(batch.lookup_connection_id),
                "lookup_target": (
                    f"{batch.lookup_database}.{batch.lookup_table}"
                    f"({batch.lookup_name_column},{batch.lookup_id_column})"
                ),
                "permission_api_url": batch.permission_api_url,
                "project_id": batch.project_id,
                "parallelism": batch.parallelism,
                "total_count": batch.total_count,
                "expire_at": batch.expire_at.isoformat(sep=" "),
            },
            request=request,
        )
        return await get_permission_batch(db, batch.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"数据连接授权任务提交失败：{exc}") from exc


@router.get("/permission-batches", response_model=ResourcePermissionBatchListResponse)
async def list_resource_permission_batches(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:read")),
):
    del actor
    return ResourcePermissionBatchListResponse(items=await list_permission_batches(db, limit))


@router.get("/permission-batches/{batch_id}", response_model=ResourcePermissionBatchResponse)
async def get_resource_permission_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:read")),
):
    del actor
    try:
        return await get_permission_batch(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/permission-batches/{batch_id}/retry", response_model=ResourcePermissionBatchResponse)
async def retry_resource_permission_batch(
    batch_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:execute")),
):
    try:
        batch = await prepare_permission_retry(db, batch_id)
        run_resource_permission_batch.apply_async(
            args=[str(batch.id)],
            queue=get_settings().celery_resource_provisioning_queue,
        )
        await record_audit(
            db,
            actor,
            action="resource_provisioning.permission_retry",
            module="resource-provisioning",
            target_type="resource_permission_batch",
            target_id=str(batch.id),
            target_name=batch.source_filename,
            payload={"failed_only": True},
            request=request,
        )
        return await get_permission_batch(db, batch.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"数据连接授权重试提交失败：{exc}") from exc


@router.post(
    "/permission-batches/{batch_id}/rows/{row_id}/delete-role",
    response_model=ResourcePermissionBatchResponse,
)
async def delete_resource_permission_role(
    batch_id: uuid.UUID,
    row_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_permission("resourceProvisioning:execute")),
):
    try:
        await asyncio.to_thread(delete_permission_role, batch_id, row_id)
        # The delete operation commits through a separate synchronous session. Clear any
        # request-session transaction/identity-map state before returning the fresh row state.
        await db.rollback()
        batch = await get_permission_batch(db, batch_id)
        await record_audit(
            db,
            actor,
            action="resource_provisioning.permission_role_delete",
            module="resource-provisioning",
            target_type="resource_permission_row",
            target_id=str(row_id),
            target_name=batch.source_filename,
            payload={"batch_id": str(batch_id), "row_id": str(row_id)},
            request=request,
        )
        return batch
    except ResourcePermissionStepError as exc:
        raise HTTPException(status_code=502, detail=f"删除有数角色失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"删除有数角色失败：{exc}") from exc
