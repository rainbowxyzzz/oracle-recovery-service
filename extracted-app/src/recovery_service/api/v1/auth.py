import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.api.deps import get_current_actor, get_db
from recovery_service.api.schemas.auth import (
    CurrentAuthResponse,
    LoginRequest,
    LoginResponse,
    UserResponse,
)
from recovery_service.core.models.task import User
from recovery_service.services.audit import record_audit
from recovery_service.services.auth import (
    AuthContext,
    actor_from_token,
    authenticate_user,
    create_access_token,
    decode_access_token,
    hash_password,
)
from recovery_service.services.oidc import (
    OIDCConfigurationError,
    build_authorization_url,
    create_state,
    decode_state,
    discover,
    encode_state,
    exchange_code,
    fetch_userinfo,
    normalize_next_path,
    oidc_is_enabled,
    userinfo_display_name,
    userinfo_username,
)
from recovery_service.services.permissions import normalize_permissions
from recovery_service.settings import get_settings

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


@router.get("/oidc/config")
async def oidc_config():
    settings = get_settings()
    return {
        "enabled": oidc_is_enabled(settings),
        "provider": settings.oidc_provider_name,
        "message": "已启用统一认证。" if oidc_is_enabled(settings) else "统一认证尚未配置。",
    }


@router.get("/oidc/login")
async def oidc_login(next: str = "/ui"):
    settings = get_settings()
    if not oidc_is_enabled(settings):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="统一认证尚未配置")
    try:
        state = create_state(settings, normalize_next_path(next))
        encoded_state = encode_state(state, settings.secret_key)
        document = await discover(settings)
        redirect_url = build_authorization_url(settings, document, encoded_state, state.nonce)
    except (OIDCConfigurationError, HTTPException) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(
        "ors_oidc_state",
        encoded_state,
        max_age=settings.oidc_state_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.oidc_cookie_secure,
        path="/",
    )
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    ors_oidc_state: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    if error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_description or error)
    settings = get_settings()
    if not oidc_is_enabled(settings) or not code or not state or not ors_oidc_state or state != ors_oidc_state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="统一认证回调无效")
    try:
        state_data = decode_state(state, settings.secret_key)
        document = await discover(settings)
        token_document = await exchange_code(settings, document, code)
        userinfo = await fetch_userinfo(settings, document, str(token_document["access_token"]))
        username = userinfo_username(userinfo)
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            if not settings.oidc_auto_provision_users:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"统一认证用户 {username} 尚未绑定系统账号")
            user = User(
                username=username,
                password_hash=hash_password(uuid.uuid4().hex),
                display_name=userinfo_display_name(userinfo, username),
                role="viewer",
                status="active",
                permissions={},
            )
            db.add(user)
            await db.flush()
        if user.status != "active":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="系统账号已禁用")
        user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        local_token, expires_at = create_access_token(user)
    except HTTPException:
        raise
    except (httpx.HTTPError, OIDCConfigurationError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"统一认证失败：{exc}") from exc
    redirect = RedirectResponse(url=state_data.next_path, status_code=status.HTTP_303_SEE_OTHER)
    expires_delta = max(1, int((expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()))
    redirect.set_cookie(
        "ors_oidc_session",
        local_token,
        max_age=expires_delta,
        httponly=True,
        samesite="lax",
        secure=settings.oidc_cookie_secure,
        path="/",
    )
    redirect.delete_cookie("ors_oidc_state", path="/")
    return redirect


@router.get("/oidc/session", response_model=LoginResponse)
async def oidc_session(
    response: Response,
    ors_oidc_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not ors_oidc_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No OIDC session")
    try:
        actor = await actor_from_token(db, ors_oidc_session)
        if not actor.user_id:
            raise ValueError("OIDC session user missing")
        user = await db.get(User, uuid.UUID(actor.user_id))
        payload = decode_access_token(ors_oidc_session)
        if not user:
            raise ValueError("OIDC session user missing")
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc).replace(tzinfo=None)
        return LoginResponse(access_token=ors_oidc_session, expires_at=expires_at, user=_user_response(user))
    except (ValueError, KeyError, TypeError):
        response.delete_cookie("ors_oidc_session", path="/")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OIDC session expired")


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
