from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from recovery_service.settings import Settings


class OIDCConfigurationError(RuntimeError):
    """Raised when the configured identity provider cannot be used."""


@dataclass(frozen=True)
class OIDCState:
    nonce: str
    next_path: str
    expires_at: int


def oidc_is_enabled(settings: Settings) -> bool:
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer_url.strip()
        and settings.oidc_client_id.strip()
        and settings.oidc_client_secret.strip()
        and settings.oidc_redirect_uri.strip()
    )


def normalize_next_path(value: str | None) -> str:
    candidate = str(value or "/ui").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return "/ui"
    return candidate


def create_state(settings: Settings, next_path: str) -> OIDCState:
    return OIDCState(
        nonce=secrets.token_urlsafe(24),
        next_path=normalize_next_path(next_path),
        expires_at=int(datetime.now(timezone.utc).timestamp()) + settings.oidc_state_ttl_seconds,
    )


def encode_state(state: OIDCState, secret: str) -> str:
    payload = {"nonce": state.nonce, "next": state.next_path, "exp": state.expires_at}
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body, secret)
    return f"{body}.{signature}"


def decode_state(value: str, secret: str) -> OIDCState:
    try:
        body, signature = value.split(".", 1)
        if not hmac.compare_digest(signature, _sign(body, secret)):
            raise ValueError("Invalid OIDC state signature")
        payload = json.loads(_b64decode(body).decode("utf-8"))
        expires_at = int(payload["exp"])
        if expires_at < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("OIDC state expired")
        nonce = str(payload["nonce"])
        if not nonce:
            raise ValueError("OIDC state nonce missing")
        return OIDCState(
            nonce=nonce,
            next_path=normalize_next_path(str(payload.get("next") or "/ui")),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid OIDC state") from exc


async def discover(settings: Settings) -> dict[str, Any]:
    issuer = settings.oidc_issuer_url.rstrip("/")
    discovery_url = settings.oidc_discovery_url.strip() or f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=settings.oidc_request_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        document = response.json()
    required = ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
    if any(not str(document.get(key) or "").strip() for key in required):
        raise OIDCConfigurationError("OIDC discovery document is missing required endpoints")
    return document


def build_authorization_url(
    settings: Settings,
    discovery_document: dict[str, Any],
    state: str,
    nonce: str,
) -> str:
    query = {
        "client_id": settings.oidc_client_id.strip(),
        "redirect_uri": settings.oidc_redirect_uri.strip(),
        "response_type": "code",
        "scope": settings.oidc_scopes.strip() or "openid profile email",
        "state": state,
        "nonce": nonce,
    }
    return f"{str(discovery_document['authorization_endpoint']).strip()}?{urlencode(query)}"


async def exchange_code(
    settings: Settings,
    discovery_document: dict[str, Any],
    code: str,
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.oidc_client_id.strip(),
        "client_secret": settings.oidc_client_secret.strip(),
        "redirect_uri": settings.oidc_redirect_uri.strip(),
    }
    async with httpx.AsyncClient(timeout=settings.oidc_request_timeout_seconds, follow_redirects=True) as client:
        response = await client.post(str(discovery_document["token_endpoint"]), data=form)
        response.raise_for_status()
        document = response.json()
    if not str(document.get("access_token") or "").strip():
        raise OIDCConfigurationError("OIDC token response did not contain an access token")
    return document


async def fetch_userinfo(
    settings: Settings,
    discovery_document: dict[str, Any],
    access_token: str,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.oidc_request_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            str(discovery_document["userinfo_endpoint"]),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        document = response.json()
    if not isinstance(document, dict) or not document.get("sub"):
        raise OIDCConfigurationError("OIDC userinfo response did not contain a subject")
    return document


def userinfo_username(userinfo: dict[str, Any]) -> str:
    for key in ("preferred_username", "username", "email", "sub"):
        value = str(userinfo.get(key) or "").strip()
        if value:
            return value[:64]
    raise OIDCConfigurationError("OIDC userinfo did not contain a usable username")


def userinfo_display_name(userinfo: dict[str, Any], username: str) -> str:
    for key in ("name", "preferred_username", "email"):
        value = str(userinfo.get(key) or "").strip()
        if value:
            return value[:128]
    return username


def _sign(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
