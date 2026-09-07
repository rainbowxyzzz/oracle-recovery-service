import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_admin
from recovery_service.api.schemas.auth import (
    OpenMetadataBindingListItem,
    OpenMetadataBindingRequest,
    OpenMetadataBindingResponse,
    PermissionCatalogItem,
    ResetPasswordRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from recovery_service.common.time import app_now
from recovery_service.core.models.task import OpenMetadataUserBinding, User
from recovery_service.services import openmetadata_permissions
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext, hash_password
from recovery_service.services.permissions import normalize_permissions, permission_catalog_response

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/permissions/catalog", response_model=list[PermissionCatalogItem])
async def get_permission_catalog(_: AuthContext = Depends(require_admin)):
    return permission_catalog_response()


@router.get("", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    return [_user_response(user) for user in result.scalars().all()]


@router.get("/openmetadata/catalog")
async def get_openmetadata_catalog(_: AuthContext = Depends(require_admin)):
    try:
        return await asyncio.to_thread(openmetadata_permissions.authorization_catalog)
    except openmetadata_permissions.OpenMetadataAuthorizationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/openmetadata/bindings", response_model=list[OpenMetadataBindingListItem])
async def list_openmetadata_bindings(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_admin),
):
    result = await db.execute(
        select(OpenMetadataUserBinding, User.username)
        .join(User, User.id == OpenMetadataUserBinding.user_id)
        .order_by(User.username.asc())
    )
    return [_binding_response(binding, username=username) for binding, username in result.all()]


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    user = User(
        username=body.username.strip(),
        password_hash=hash_password(body.password.get_secret_value()),
        display_name=(body.display_name or "").strip() or None,
        role=body.role,
        status=body.status,
        permissions=normalize_permissions(body.permissions, admin=body.role == "admin"),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    await db.refresh(user)
    await record_audit(
        db,
        actor,
        action="create_user",
        module="users",
        target_type="user",
        target_id=str(user.id),
        target_name=user.username,
        payload={"role": user.role, "status": user.status, "permissions": user.permissions},
        request=request,
    )
    return _user_response(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.status == "disabled":
        await _ensure_not_last_active_admin(db, user)
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or None
    if body.role is not None:
        if user.role == "admin" and body.role != "admin":
            await _ensure_not_last_active_admin(db, user)
        user.role = body.role
    if body.status is not None:
        user.status = body.status
    if body.permissions is not None:
        user.permissions = normalize_permissions(body.permissions, admin=user.role == "admin")
    elif user.role == "admin":
        user.permissions = normalize_permissions({}, admin=True)
    await db.commit()
    await db.refresh(user)
    await record_audit(
        db,
        actor,
        action="update_user",
        module="users",
        target_type="user",
        target_id=str(user.id),
        target_name=user.username,
        payload={"role": user.role, "status": user.status, "permissions": user.permissions},
        request=request,
    )
    return _user_response(user)


@router.put("/{user_id}/openmetadata", response_model=OpenMetadataBindingResponse)
async def set_openmetadata_binding(
    user_id: uuid.UUID,
    body: OpenMetadataBindingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = body.openmetadata_username.strip()
    roles = _clean_names(body.role_names)
    teams = _clean_names(body.team_names)
    binding = await db.scalar(select(OpenMetadataUserBinding).where(OpenMetadataUserBinding.user_id == user_id))
    if binding is None:
        binding = OpenMetadataUserBinding(user_id=user_id, created_by_username=actor.username)
        db.add(binding)
    binding.openmetadata_username = username
    binding.role_names = roles
    binding.team_names = teams
    binding.status = body.status
    binding.sync_status = "pending" if body.status == "active" else "disabled"
    binding.sync_message = "等待同步" if body.status == "active" else "绑定已停用"
    await db.commit()
    await db.refresh(binding)
    if body.sync and body.status == "active":
        try:
            synced = await asyncio.to_thread(
                openmetadata_permissions.sync_user,
                openmetadata_username=username,
                role_names=roles,
                team_names=teams,
            )
            binding.sync_status = "succeeded"
            binding.sync_message = "OpenMetadata 原生角色/团队已同步"
            binding.openmetadata_user_id = synced.get("openmetadata_user_id")
            binding.last_synced_at = app_now()
        except openmetadata_permissions.OpenMetadataAuthorizationError as exc:
            binding.sync_status = "failed"
            binding.sync_message = str(exc)[:1000]
            await db.commit()
            await record_audit(
                db,
                actor,
                action="sync_openmetadata_permissions",
                module="users",
                target_type="user",
                target_id=str(user.id),
                target_name=user.username,
                status="failed",
                payload={"openmetadata_username": username, "roles": roles, "teams": teams},
                error_message=str(exc),
                request=request,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(binding)
    await record_audit(
        db,
        actor,
        action="sync_openmetadata_permissions" if body.sync else "configure_openmetadata_permissions",
        module="users",
        target_type="user",
        target_id=str(user.id),
        target_name=user.username,
        payload={"openmetadata_username": username, "roles": roles, "teams": teams, "sync": body.sync},
        request=request,
    )
    return _binding_response(binding, username=user.username)


@router.get("/{user_id}/openmetadata", response_model=OpenMetadataBindingResponse | None)
async def get_openmetadata_binding(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    binding = await db.scalar(select(OpenMetadataUserBinding).where(OpenMetadataUserBinding.user_id == user_id))
    return _binding_response(binding, username=user.username) if binding else None


@router.post("/{user_id}/openmetadata/sync", response_model=OpenMetadataBindingResponse)
async def sync_openmetadata_binding(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    binding = await db.scalar(select(OpenMetadataUserBinding).where(OpenMetadataUserBinding.user_id == user_id))
    user = await db.get(User, user_id)
    if not user or not binding:
        raise HTTPException(status_code=404, detail="OpenMetadata 用户绑定不存在")
    try:
        synced = await asyncio.to_thread(
            openmetadata_permissions.sync_user,
            openmetadata_username=binding.openmetadata_username,
            role_names=list(binding.role_names or []),
            team_names=list(binding.team_names or []),
        )
        binding.sync_status = "succeeded"
        binding.sync_message = "OpenMetadata 原生角色/团队已同步"
        binding.openmetadata_user_id = synced.get("openmetadata_user_id")
        binding.last_synced_at = app_now()
        await db.commit()
        await db.refresh(binding)
        return _binding_response(binding, username=user.username)
    except openmetadata_permissions.OpenMetadataAuthorizationError as exc:
        binding.sync_status = "failed"
        binding.sync_message = str(exc)[:1000]
        await db.commit()
        await record_audit(
            db,
            actor,
            action="sync_openmetadata_permissions",
            module="users",
            target_type="user",
            target_id=str(user.id),
            target_name=user.username,
            status="failed",
            error_message=str(exc),
            request=request,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{user_id}/reset-password", response_model=UserResponse)
async def reset_user_password(
    user_id: uuid.UUID,
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = hash_password(body.password.get_secret_value())
    await db.commit()
    await db.refresh(user)
    await record_audit(
        db,
        actor,
        action="reset_user_password",
        module="users",
        target_type="user",
        target_id=str(user.id),
        target_name=user.username,
        request=request,
    )
    return _user_response(user)


async def _ensure_not_last_active_admin(db: AsyncSession, user: User) -> None:
    if user.role != "admin" or user.status != "active":
        return
    result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.status == "active", User.id != user.id)
    )
    if int(result.scalar_one() or 0) <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能禁用或降级最后一个可用管理员")


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,  # type: ignore[arg-type]
        status=user.status,  # type: ignore[arg-type]
        permissions=normalize_permissions(user.permissions or {}, admin=user.role == "admin"),
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
    )


def _clean_names(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def _binding_response(binding: OpenMetadataUserBinding, *, username: str) -> OpenMetadataBindingResponse:
    return OpenMetadataBindingListItem(
        id=binding.id,
        user_id=binding.user_id,
        username=username,
        openmetadata_username=binding.openmetadata_username,
        role_names=list(binding.role_names or []),
        team_names=list(binding.team_names or []),
        status=binding.status,
        sync_status=binding.sync_status,
        sync_message=binding.sync_message,
        openmetadata_user_id=binding.openmetadata_user_id,
        last_synced_at=binding.last_synced_at,
    )
