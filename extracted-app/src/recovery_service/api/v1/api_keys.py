import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_db, require_admin
from recovery_service.api.schemas.auth import ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyResponse, ApiKeyUpdateRequest
from recovery_service.core.models.task import ApiKeyCredential
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext, generate_api_key, hash_api_key
from recovery_service.services.permissions import normalize_permissions

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    _: AuthContext = Depends(require_admin),
):
    result = await db.execute(select(ApiKeyCredential).order_by(ApiKeyCredential.created_at.desc()))
    return [_api_key_response(item) for item in result.scalars().all()]


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    raw_key = generate_api_key()
    credential = ApiKeyCredential(
        name=body.name.strip(),
        key_prefix=raw_key[:12],
        key_hash=hash_api_key(raw_key),
        status=body.status,
        permissions=normalize_permissions(body.permissions),
        created_by_user_id=uuid.UUID(actor.user_id) if actor.user_id else None,
        created_by_username=actor.username,
    )
    db.add(credential)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="API Key 已存在，请重试") from exc
    await db.refresh(credential)
    await record_audit(
        db,
        actor,
        action="create_api_key",
        module="apiKeys",
        target_type="api-key",
        target_id=str(credential.id),
        target_name=credential.name,
        payload={"permissions": credential.permissions, "status": credential.status},
        request=request,
    )
    return ApiKeyCreateResponse(
        id=credential.id,
        name=credential.name,
        key_prefix=credential.key_prefix,
        status=credential.status,  # type: ignore[arg-type]
        permissions=normalize_permissions(credential.permissions or {}),
        created_by_username=credential.created_by_username,
        last_used_at=credential.last_used_at,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
        api_key=raw_key,
    )


@router.patch("/{api_key_id}", response_model=ApiKeyResponse)
async def update_api_key(
    api_key_id: uuid.UUID,
    body: ApiKeyUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: AuthContext = Depends(require_admin),
):
    credential = await db.get(ApiKeyCredential, api_key_id)
    if not credential:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    if body.name is not None:
        credential.name = body.name.strip()
    if body.status is not None:
        credential.status = body.status
    if body.permissions is not None:
        credential.permissions = normalize_permissions(body.permissions)
    await db.commit()
    await db.refresh(credential)
    await record_audit(
        db,
        actor,
        action="update_api_key",
        module="apiKeys",
        target_type="api-key",
        target_id=str(credential.id),
        target_name=credential.name,
        payload={"permissions": credential.permissions, "status": credential.status},
        request=request,
    )
    return _api_key_response(credential)


def _api_key_response(credential: ApiKeyCredential) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=credential.id,
        name=credential.name,
        key_prefix=credential.key_prefix,
        status=credential.status,  # type: ignore[arg-type]
        permissions=normalize_permissions(credential.permissions or {}),
        created_by_username=credential.created_by_username,
        last_used_at=credential.last_used_at,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )
