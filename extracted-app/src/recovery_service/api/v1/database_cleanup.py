from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.database_cleanup import (
    CleanupBatchExecuteRequest,
    CleanupBatchExecutionResult,
    CleanupBatchPlan,
    CleanupBatchTargetRequest,
    CleanupCatalog,
    CleanupConnection,
    CleanupExecuteRequest,
    CleanupExecutionResult,
    CleanupPlan,
    CleanupRequest,
    CleanupStatus,
    CleanupTargetRequest,
)
from recovery_service.services.database_cleanup import (
    build_cleanup_plan,
    build_cleanup_batch_plan,
    cleanup_defaults,
    discover_catalog,
    execute_cleanup,
    execute_cleanup_batch,
    test_connection,
)
from recovery_service.services.database_connections import get_profile, profile_to_cleanup_connection

router = APIRouter(prefix="/database-cleanup", tags=["database-cleanup"])


async def _resolve_connection(body: CleanupRequest, db: AsyncSession) -> CleanupConnection:
    if body.connection_id:
        try:
            profile = await get_profile(db, body.connection_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return profile_to_cleanup_connection(profile)
    if body.connection:
        return body.connection
    raise HTTPException(status_code=422, detail="请先选择数据库连接。")


@router.get("/defaults")
async def get_cleanup_defaults(_: None = Depends(require_permission("cleanup:read"))):
    return cleanup_defaults()


@router.post("/test", response_model=CleanupStatus)
async def test_database_connection(
    body: CleanupRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:read")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await test_connection(conn)
    except Exception as e:
        return CleanupStatus(ok=False, message=f"连接失败：{e}", details={})


@router.post("/catalog", response_model=CleanupCatalog)
async def get_database_catalog(
    body: CleanupRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:read")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await discover_catalog(conn)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取库表失败：{e}") from e


@router.post("/plan", response_model=CleanupPlan)
async def create_cleanup_plan(
    body: CleanupTargetRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:read")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await build_cleanup_plan(
            conn,
            body.target_name,
            drop_storage=body.drop_storage,
            cleanup_files=body.cleanup_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"生成清理计划失败：{e}") from e


@router.post("/batch-plan", response_model=CleanupBatchPlan)
async def create_cleanup_batch_plan(
    body: CleanupBatchTargetRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:read")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await build_cleanup_batch_plan(
            conn,
            body.target_names,
            drop_storage=body.drop_storage,
            cleanup_files=body.cleanup_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"生成批量清理计划失败：{e}") from e


@router.post("/execute", response_model=CleanupExecutionResult)
async def execute_cleanup_plan(
    body: CleanupExecuteRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:execute")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await execute_cleanup(
            conn,
            body.target_name,
            confirmation=body.confirmation,
            drop_storage=body.drop_storage,
            cleanup_files=body.cleanup_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"执行清理失败：{e}") from e
@router.post("/batch-execute", response_model=CleanupBatchExecutionResult)
async def execute_cleanup_batch_plan(
    body: CleanupBatchExecuteRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("cleanup:execute")),
):
    try:
        conn = await _resolve_connection(body, db)
        return await execute_cleanup_batch(
            conn,
            body.target_names,
            acknowledged=body.acknowledged,
            drop_storage=body.drop_storage,
            cleanup_files=body.cleanup_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"执行批量清理失败：{e}") from e
