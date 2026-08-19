import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_current_actor, get_db
from recovery_service.api.schemas.auth import CurrentAuthResponse, LoginRequest, LoginResponse, UserResponse
from recovery_service.core.models.task import User
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import AuthContext, authenticate_user, create_access_token
from recovery_service.services.permissions import normalize_permissions

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    username = body.username.strip()
    user = await authenticate_user(db, username, body.password.get_secret_value())
    if not user:
        await record_audit(
            db,
            None,
            action="login",
            module="auth",
            status="failed",
            payload={"username": username},
            error_message="invalid username or password",
            request=request,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token, expires_at = create_access_token(user)
    await record_audit(
        db,
        AuthContext(
            user_id=str(user.id),
            username=user.username,
            role=user.role,
            auth_type="user",
            permissions=normalize_permissions(user.permissions or {}, admin=user.role == "admin"),
        ),
        action="login",
        module="auth",
        status="success",
        request=request,
    )
    return LoginResponse(access_token=token, expires_at=expires_at, user=_user_response(user))


@router.get("/me", response_model=CurrentAuthResponse)
async def me(
    actor: AuthContext = Depends(get_current_actor),
    db: AsyncSession = Depends(get_db),
):
    user_response = None
    if actor.user_id:
        user = await db.get(User, uuid.UUID(actor.user_id))
        if user:
            user_response = _user_response(user)
    return CurrentAuthResponse(
        auth_type=actor.auth_type,  # type: ignore[arg-type]
        user=user_response,
        username=actor.username,
        role=actor.role,
        is_admin=actor.is_admin,
        permissions=normalize_permissions(actor.permissions, admin=actor.is_admin),
    )


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
