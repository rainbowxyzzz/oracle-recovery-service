import json

import pytest

from recovery_service.api.v1 import auth as auth_api
from recovery_service.services.oidc import (
    OIDCState,
    build_logout_url,
    create_state,
    decode_state,
    encode_state,
    normalize_next_path,
)
from recovery_service.settings import Settings


def test_oidc_state_round_trip_and_expiry_boundary():
    settings = Settings(secret_key="test-secret", oidc_state_ttl_seconds=600)
    state = create_state(settings, "/ui?openmetadata=1")
    encoded = encode_state(state, settings.secret_key)

    decoded = decode_state(encoded, settings.secret_key)

    assert decoded == state
    assert decoded.next_path == "/ui?openmetadata=1"


def test_oidc_state_rejects_tampering_and_external_redirects():
    settings = Settings(secret_key="test-secret")
    state = OIDCState(nonce="nonce", next_path="/ui", expires_at=4102444800)
    encoded = encode_state(state, settings.secret_key)

    assert normalize_next_path("https://evil.example") == "/ui"
    assert normalize_next_path("//evil.example") == "/ui"

    try:
        decode_state(f"{encoded}x", settings.secret_key)
    except ValueError as exc:
        assert "state" in str(exc).lower()
    else:
        raise AssertionError("tampered OIDC state must be rejected")


def test_oidc_logout_url_uses_id_token_and_same_origin_ui_redirect():
    settings = Settings(
        oidc_client_id="recovery-ui",
        oidc_redirect_uri="https://recovery.example/api/v1/auth/oidc/callback",
    )

    url = build_logout_url(
        settings,
        {"end_session_endpoint": "https://id.example/realms/recovery/protocol/openid-connect/logout"},
        "id-token-value",
    )

    assert url == (
        "https://id.example/realms/recovery/protocol/openid-connect/logout?"
        "client_id=recovery-ui&post_logout_redirect_uri=https%3A%2F%2Frecovery.example%2Fui&"
        "id_token_hint=id-token-value"
    )


@pytest.mark.asyncio
async def test_oidc_logout_clears_local_cookies_and_returns_keycloak_logout_url(monkeypatch):
    settings = Settings(
        oidc_enabled=True,
        oidc_issuer_url="https://id.example/realms/recovery",
        oidc_client_id="recovery-ui",
        oidc_client_secret="test-secret",
        oidc_redirect_uri="https://recovery.example/api/v1/auth/oidc/callback",
    )

    async def fake_discover(_settings):
        return {"end_session_endpoint": "https://id.example/realms/recovery/protocol/openid-connect/logout"}

    monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_api, "discover", fake_discover)

    response = await auth_api.oidc_logout(ors_oidc_id_token="id-token-value")

    assert json.loads(response.body) == {
        "logout_url": (
            "https://id.example/realms/recovery/protocol/openid-connect/logout?"
            "client_id=recovery-ui&post_logout_redirect_uri=https%3A%2F%2Frecovery.example%2Fui&"
            "id_token_hint=id-token-value"
        ),
        "global_logout": True,
    }
    cookies = "\n".join(response.headers.getlist("set-cookie"))
    assert "ors_oidc_session=\"\"" in cookies
    assert "ors_oidc_id_token=\"\"" in cookies
    assert "ors_oidc_state=\"\"" in cookies
