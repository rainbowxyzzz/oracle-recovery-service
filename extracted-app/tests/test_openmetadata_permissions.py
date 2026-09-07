from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest

from recovery_service.services import openmetadata_permissions as permissions


def _settings(**overrides):
    values = {
        "openmetadata_url": "http://openmetadata.test:8585",
        "openmetadata_api_url": "http://openmetadata.test:8585/api",
        "openmetadata_api_token": "secret-token",
        "openmetadata_request_timeout_seconds": 20,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    @property
    def is_error(self):
        return self.status_code >= 400

    def json(self):
        return self._data


class _Client:
    calls: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        _Client.calls = self.calls

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        if path.startswith("/v1/users/name/"):
            return _Response({"id": "user-1", "name": "catalog_viewer"})
        if path == "/v1/roles":
            return _Response({"data": [{"id": "role-1", "name": "DataConsumer"}]})
        if path == "/v1/teams":
            return _Response({"data": [{"id": "team-1", "name": "LineageReaders"}]})
        raise AssertionError(path)

    def put(self, path, **kwargs):
        self.calls.append(("PUT", path, kwargs))
        return _Response({"id": "user-1", "name": "catalog_viewer"})


def test_sync_user_resolves_native_role_and_team_and_never_returns_token():
    with patch.object(permissions, "get_settings", return_value=_settings()), patch.object(permissions.httpx, "Client", _Client):
        result = permissions.sync_user(
            openmetadata_username="catalog_viewer",
            role_names=["DataConsumer"],
            team_names=["LineageReaders"],
        )

    update_call = next(call for call in _Client.calls if call[0] == "PUT")
    assert update_call[1] == "/v1/users"
    assert update_call[2]["json"] == {
        "name": "catalog_viewer",
        "displayName": "catalog_viewer",
        "email": "catalog_viewer@openmetadata.local",
        "isBot": False,
        "isAdmin": False,
        "roles": ["role-1"],
        "teams": ["team-1"],
    }
    assert result["openmetadata_user_id"] == "user-1"
    assert "secret-token" not in str(result)


def test_sync_user_rejects_missing_native_permission_object():
    class MissingClient(_Client):
        def get(self, path, **kwargs):
            response = super().get(path, **kwargs)
            if path == "/v1/roles":
                return _Response({"data": []})
            return response

    with (
        patch.object(permissions, "get_settings", return_value=_settings()),
        patch.object(permissions.httpx, "Client", MissingClient),
        pytest.raises(permissions.OpenMetadataAuthorizationError, match="角色"),
    ):
        permissions.sync_user(openmetadata_username="catalog_viewer", role_names=["DataConsumer"], team_names=[])


def test_catalog_is_pending_without_api_token():
    with patch.object(permissions, "get_settings", return_value=_settings(openmetadata_api_token="")):
        result = permissions.authorization_catalog()
    assert result == {"configured": True, "token_configured": False, "roles": [], "teams": []}
