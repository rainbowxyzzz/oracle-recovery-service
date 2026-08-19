from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.db.session import get_async_session
from recovery_service.services.auth import AuthContext, actor_from_api_key, actor_from_token
from recovery_service.services.permissions import all_permissions
from recovery_service.settings import get_settings


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_async_session():
        yield session


async def get_current_actor(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    settings = get_settings()
    if settings.app_env == "development":
        return AuthContext(
            user_id=None,
            username="development",
            role="admin",
            auth_type="development",
            permissions=all_permissions(),
        )

    token = _bearer_token(authorization)
    if token:
        try:
            return await actor_from_token(db, token)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    if x_api_key:
        managed_actor = await actor_from_api_key(db, x_api_key)
        if managed_actor:
            return managed_actor
        if x_api_key == settings.secret_key:
            return AuthContext(
                user_id=None,
                username="legacy-secret-key",
                role="admin",
                auth_type="api-key",
                permissions=all_permissions(),
                api_key_name="legacy-secret-key",
            )

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


async def verify_api_key(actor: AuthContext = Depends(get_current_actor)) -> None:
    return None


async def require_admin(actor: AuthContext = Depends(get_current_actor)) -> AuthContext:
    if not actor.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin permission required")
    return actor


def require_permission(action: str):
    async def dependency(actor: AuthContext = Depends(get_current_actor)) -> AuthContext:
        if not actor.has_permission(action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {action}",
            )
        return actor

    return dependency


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
