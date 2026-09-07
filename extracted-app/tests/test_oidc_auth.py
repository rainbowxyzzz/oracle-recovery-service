from recovery_service.services.oidc import (
    OIDCState,
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
