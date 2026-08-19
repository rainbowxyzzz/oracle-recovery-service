import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_permission
from recovery_service.api.schemas.database_cleanup import CleanupCatalog, CleanupStatus
from recovery_service.api.schemas.database_connection import (
    DatabaseConnectionCreate,
    DatabaseConnectionResponse,
    DatabaseConnectionUpdate,
)
from recovery_service.services.database_cleanup import discover_catalog, test_connection
from recovery_service.services.database_connections import (
    create_profile,
    delete_profile,
    get_profile,
    list_profiles,
    mark_test_result,
    profile_response,
    profile_to_cleanup_connection,
    update_profile,
)

router = APIRouter(prefix="/database-connections", tags=["database-connections"])


@router.get("", response_model=list[DatabaseConnectionResponse])
async def list_database_connections(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:read")),
):
    profiles = await list_profiles(db)
    return [profile_response(profile) for profile in profiles]


@router.post("", response_model=DatabaseConnectionResponse, status_code=201)
async def create_database_connection(
    body: DatabaseConnectionCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:manage")),
):
    profile = await create_profile(db, body)
    return profile_response(profile)


@router.get("/{connection_id}", response_model=DatabaseConnectionResponse)
async def get_database_connection(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:read")),
):
    try:
        return profile_response(await get_profile(db, connection_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/{connection_id}", response_model=DatabaseConnectionResponse)
async def update_database_connection(
    connection_id: uuid.UUID,
    body: DatabaseConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:manage")),
):
    try:
        profile = await update_profile(db, connection_id, body)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return profile_response(profile)


@router.delete("/{connection_id}", status_code=204)
async def delete_database_connection(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:manage")),
):
    try:
        await delete_profile(db, connection_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{connection_id}/test", response_model=CleanupStatus)
async def test_database_connection_profile(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:test")),
):
    try:
        profile = await get_profile(db, connection_id)
        conn = profile_to_cleanup_connection(profile)
        result = await test_connection(conn)
        await mark_test_result(db, profile, result.ok, result.message)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        profile = await get_profile(db, connection_id)
        message = f"连接失败：{e}"
        await mark_test_result(db, profile, False, message)
        return CleanupStatus(ok=False, message=message, details={})


@router.post("/{connection_id}/catalog", response_model=CleanupCatalog)
async def get_database_connection_catalog(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_permission("connections:catalog")),
):
    try:
        profile = await get_profile(db, connection_id)
        return await discover_catalog(profile_to_cleanup_connection(profile))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取库表失败：{e}") from e
