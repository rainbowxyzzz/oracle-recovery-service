from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from recovery_service.common.time import app_now
from recovery_service.core.models.task import ApiKeyCredential, User
from recovery_service.services.permissions import all_permissions, normalize_permissions
from recovery_service.settings import get_settings

_TOKEN_ALGORITHM = "HS256"
_PASSWORD_ITERATIONS = 260000
_PASSWORD_PREFIX = "pbkdf2_sha256"


@dataclass(frozen=True)
class AuthContext:
    user_id: str | None
    username: str | None
    role: str
    auth_type: str
    permissions: dict[str, list[str]]
    api_key_id: str | None = None
    api_key_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def has_permission(self, action: str) -> bool:
        if self.is_admin:
            return True
        return action in set(self.permissions.get("actions") or [])


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return "$".join(
        [
            _PASSWORD_PREFIX,
            str(_PASSWORD_ITERATIONS),
            _b64encode(salt),
            _b64encode(digest),
        ]
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        prefix, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if prefix != _PASSWORD_PREFIX:
            return False
        iterations = int(iterations_text)
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_access_token(user: User) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": int(expires_at.timestamp()),
        "nonce": secrets.token_hex(8),
    }
    header = {"typ": "JWT", "alg": _TOKEN_ALGORITHM}
    signing_input = f"{_json_b64(header)}.{_json_b64(payload)}"
    signature = _sign(signing_input, settings.secret_key)
    return f"{signing_input}.{signature}", expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _sign(signing_input, settings.secret_key)
    if not hmac.compare_digest(parts[2], expected):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64decode(parts[1]).decode("utf-8"))
    if int(payload.get("exp") or 0) < int(datetime.utcnow().timestamp()):
        raise ValueError("Token expired")
    return payload


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username.strip()))
    user = result.scalar_one_or_none()
    if not user or user.status != "active":
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = app_now()
    await db.commit()
    await db.refresh(user)
    return user


async def actor_from_token(db: AsyncSession, token: str) -> AuthContext:
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise ValueError("Invalid token")
    try:
        parsed_user_id = uuid.UUID(str(user_id))
    except ValueError as exc:
        raise ValueError("Invalid token") from exc
    user = await db.get(User, parsed_user_id)
    if not user or user.status != "active":
        raise ValueError("User is disabled or missing")
    permissions = normalize_permissions(user.permissions or {}, admin=user.role == "admin")
    return AuthContext(
        user_id=str(user.id),
        username=user.username,
        role=user.role,
        auth_type="user",
        permissions=permissions,
    )


async def actor_from_api_key(db: AsyncSession, api_key: str) -> AuthContext | None:
    key_hash = hash_api_key(api_key)
    result = await db.execute(select(ApiKeyCredential).where(ApiKeyCredential.key_hash == key_hash))
    credential = result.scalar_one_or_none()
    if not credential or credential.status != "active":
        return None
    credential.last_used_at = app_now()
    await db.commit()
    return AuthContext(
        user_id=None,
        username=credential.name,
        role="api",
        auth_type="api-key",
        permissions=normalize_permissions(credential.permissions or {}),
        api_key_id=str(credential.id),
        api_key_name=credential.name,
    )


def generate_api_key() -> str:
    return "ors_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


async def ensure_default_admin(db: AsyncSession) -> None:
    result = await db.execute(select(func.count()).select_from(User))
    if int(result.scalar_one() or 0) > 0:
        return
    settings = get_settings()
    username = settings.default_admin_username.strip() or "admin"
    password = settings.default_admin_password or "admin123"
    display_name = settings.default_admin_display_name.strip() or "系统管理员"
    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            display_name=display_name,
            role="admin",
            status="active",
            permissions=all_permissions(),
        )
    )
    await db.commit()


def _json_b64(value: dict[str, Any]) -> str:
    return _b64encode(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _sign(signing_input: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
