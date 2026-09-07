from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from recovery_service.settings import get_settings


class OpenMetadataAuthorizationError(RuntimeError):
    pass


def _api_url() -> str:
    settings = get_settings()
    configured = str(settings.openmetadata_api_url or "").strip().rstrip("/")
    if configured:
        return configured
    ui_url = str(settings.openmetadata_url or "").strip().rstrip("/")
    return f"{ui_url}/api" if ui_url else ""


def _headers() -> dict[str, str]:
    token = str(get_settings().openmetadata_api_token or "").strip()
    if not token:
        raise OpenMetadataAuthorizationError("未配置 OPENMETADATA_API_TOKEN，无法同步 OpenMetadata 权限")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def configured() -> bool:
    return bool(_api_url())


def token_configured() -> bool:
    return bool(str(get_settings().openmetadata_api_token or "").strip())


def _items(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, dict) and isinstance(document.get("data"), list):
        return [item for item in document["data"] if isinstance(item, dict)]
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    return []


def _request_error(response: httpx.Response, resource: str) -> OpenMetadataAuthorizationError:
    try:
        body = response.json()
        detail = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        detail = None
    return OpenMetadataAuthorizationError(
        f"OpenMetadata {resource} 请求失败：HTTP {response.status_code} {str(detail or response.text)[:300]}"
    )


def authorization_catalog() -> dict[str, Any]:
    """Read native roles and teams for the admin configuration page."""
    result: dict[str, Any] = {
        "configured": configured(),
        "token_configured": token_configured(),
        "roles": [],
        "teams": [],
    }
    if not configured() or not token_configured():
        return result
    with httpx.Client(base_url=_api_url(), headers=_headers(), timeout=get_settings().openmetadata_request_timeout_seconds) as client:
        roles_response = client.get("/v1/roles", params={"limit": 100})
        if roles_response.is_error:
            raise _request_error(roles_response, "角色目录")
        teams_response = client.get("/v1/teams", params={"limit": 100})
        if teams_response.is_error:
            raise _request_error(teams_response, "团队目录")
        result["roles"] = [
            {"id": item.get("id"), "name": item.get("name"), "display_name": item.get("displayName") or item.get("name")}
            for item in _items(roles_response.json())
            if item.get("id") and item.get("name")
        ]
        result["teams"] = [
            {"id": item.get("id"), "name": item.get("name"), "display_name": item.get("displayName") or item.get("name")}
            for item in _items(teams_response.json())
            if item.get("id") and item.get("name")
        ]
    return result


def sync_user(
    *,
    openmetadata_username: str,
    role_names: list[str],
    team_names: list[str],
) -> dict[str, Any]:
    """Apply the desired native Role/Team references to an existing OM user."""
    username = openmetadata_username.strip()
    if not username:
        raise OpenMetadataAuthorizationError("OpenMetadata 用户名不能为空")
    if not configured():
        raise OpenMetadataAuthorizationError("未配置 OpenMetadata API 地址")
    with httpx.Client(base_url=_api_url(), headers=_headers(), timeout=get_settings().openmetadata_request_timeout_seconds) as client:
        user_response = client.get(f"/v1/users/name/{quote(username, safe='')}", params={"fields": "teams,roles"})
        if user_response.is_error:
            if user_response.status_code == 404:
                raise OpenMetadataAuthorizationError(
                    f"OpenMetadata 用户 {username} 尚未通过统一认证登录，无法绑定原生权限"
                )
            raise _request_error(user_response, "用户")
        roles_response = client.get("/v1/roles", params={"limit": 100})
        if roles_response.is_error:
            raise _request_error(roles_response, "角色目录")
        teams_response = client.get("/v1/teams", params={"limit": 100})
        if teams_response.is_error:
            raise _request_error(teams_response, "团队目录")
        roles = {str(item.get("name")): item for item in _items(roles_response.json()) if item.get("name")}
        teams = {str(item.get("name")): item for item in _items(teams_response.json()) if item.get("name")}
        missing_roles = [name for name in role_names if name not in roles]
        missing_teams = [name for name in team_names if name not in teams]
        if missing_roles or missing_teams:
            missing = []
            if missing_roles:
                missing.append(f"角色：{', '.join(missing_roles)}")
            if missing_teams:
                missing.append(f"团队：{', '.join(missing_teams)}")
            raise OpenMetadataAuthorizationError("OpenMetadata 权限对象不存在；" + "；".join(missing))
        current_user = user_response.json()
        payload = {
            "name": current_user.get("name") or username,
            "displayName": current_user.get("displayName") or username,
            "email": current_user.get("email") or f"{username}@openmetadata.local",
            "isBot": bool(current_user.get("isBot", False)),
            "isAdmin": bool(current_user.get("isAdmin", False)),
            # OpenMetadata 1.12.6's PUT CreateUser schema accepts UUID arrays.
            # The newer PATCH schema uses typed entity references instead.
            "roles": [roles[name]["id"] for name in role_names],
            "teams": [teams[name]["id"] for name in team_names],
        }
        update_response = client.put(
            "/v1/users",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if update_response.is_error:
            raise _request_error(update_response, "用户权限")
        document = update_response.json()
    return {
        "openmetadata_user_id": document.get("id") if isinstance(document, dict) else None,
        "roles": role_names,
        "teams": team_names,
        "user": document,
    }
