"""Bring-your-own-key endpoints and per-user provider resolution.

The plaintext key must never appear in any response or persisted column;
only a last-four hint comes back.
"""

import json

from sqlalchemy import select

from app import security
from app.ai.factory import resolve_provider
from app.ai.gemini_provider import GeminiProvider
from app.ai.mock import MockProvider
from app.models import UserAIKey

GEMINI_KEY = "AIzaFakeTestKey1234"


def test_ai_key_requires_auth(client):
    assert client.get("/settings/ai-key").status_code == 401
    assert (
        client.put(
            "/settings/ai-key", json={"provider": "gemini", "api_key": GEMINI_KEY}
        ).status_code
        == 401
    )
    assert client.delete("/settings/ai-key").status_code == 401


def test_ai_key_starts_unconfigured(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.get("/settings/ai-key")
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "provider": None,
        "model": None,
        "key_hint": None,
        "key_invalid": False,
    }


def test_put_stores_the_key_encrypted_and_returns_only_a_hint(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")

    response = client.put("/settings/ai-key", json={"provider": "gemini", "api_key": GEMINI_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["provider"] == "gemini"
    assert body["key_hint"] == GEMINI_KEY[-4:]
    # The full key never travels back
    assert GEMINI_KEY not in json.dumps(body)

    row = db.execute(select(UserAIKey).where(UserAIKey.user_id == user.id)).scalar_one()
    assert row.key_enc != GEMINI_KEY
    assert GEMINI_KEY not in row.key_enc
    assert security.decrypt_token(row.key_enc) == GEMINI_KEY


def test_put_upserts_and_can_switch_provider(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    assert (
        client.put(
            "/settings/ai-key", json={"provider": "gemini", "api_key": GEMINI_KEY}
        ).status_code
        == 200
    )

    second = client.put(
        "/settings/ai-key",
        json={
            "provider": "anthropic",
            "api_key": "sk-ant-fake-key-5678",
            "model": "claude-sonnet-5",
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-sonnet-5"
    assert body["key_hint"] == "5678"

    rows = list(db.execute(select(UserAIKey).where(UserAIKey.user_id == user.id)).scalars())
    assert len(rows) == 1
    assert rows[0].provider == "anthropic"


def test_key_is_stripped_before_storage(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    response = client.put(
        "/settings/ai-key", json={"provider": "gemini", "api_key": f"  {GEMINI_KEY}\n"}
    )
    assert response.status_code == 200
    assert response.json()["key_hint"] == GEMINI_KEY[-4:]
    row = db.execute(select(UserAIKey).where(UserAIKey.user_id == user.id)).scalar_one()
    assert security.decrypt_token(row.key_enc) == GEMINI_KEY


def test_delete_removes_the_key_and_is_idempotent(client, db, make_user_with_session):
    make_user_with_session("alice")
    client.put("/settings/ai-key", json={"provider": "gemini", "api_key": GEMINI_KEY})

    first = client.delete("/settings/ai-key")
    assert first.status_code == 200
    assert first.json()["configured"] is False
    assert db.execute(select(UserAIKey)).first() is None

    assert client.delete("/settings/ai-key").status_code == 200


def test_put_rejects_unknown_provider_and_short_keys(client, make_user_with_session):
    make_user_with_session("alice")
    assert (
        client.put(
            "/settings/ai-key", json={"provider": "openai", "api_key": GEMINI_KEY}
        ).status_code
        == 422
    )
    assert (
        client.put("/settings/ai-key", json={"provider": "gemini", "api_key": "short"}).status_code
        == 422
    )
    assert (
        client.put(
            "/settings/ai-key", json={"provider": "gemini", "api_key": "          "}
        ).status_code
        == 422
    )


def test_undecryptable_key_is_flagged_not_hidden(client, db, make_user_with_session):
    # A row stored under a rotated TOKEN_ENCRYPTION_KEY: the UI must be able
    # to tell the user to paste the key again, not show it as healthy
    user, _ = make_user_with_session("alice")
    db.add(UserAIKey(user_id=user.id, provider="gemini", key_enc="not-a-fernet-token"))
    db.flush()

    body = client.get("/settings/ai-key").json()
    assert body["configured"] is True
    assert body["key_invalid"] is True
    assert body["key_hint"] is None


def test_put_survives_a_concurrent_first_save(client, db, make_user_with_session, monkeypatch):
    from app.routers import ai_settings

    user, _ = make_user_with_session("alice")
    real_load = ai_settings._load
    calls = {"n": 0}

    def racing_load(db_, user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # A racing request wins the insert between our SELECT and commit
            db.add(
                UserAIKey(
                    user_id=user_id,
                    provider="gemini",
                    key_enc=security.encrypt_token("AIzaWinnerKey0001"),
                )
            )
            db.flush()
            return None
        return real_load(db_, user_id)

    monkeypatch.setattr(ai_settings, "_load", racing_load)

    response = client.put(
        "/settings/ai-key", json={"provider": "anthropic", "api_key": "sk-ant-second-9876"}
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "anthropic"
    assert response.json()["key_hint"] == "9876"


def test_users_only_see_their_own_key(client, make_user_with_session):
    make_user_with_session("alice")
    client.put("/settings/ai-key", json={"provider": "gemini", "api_key": GEMINI_KEY})

    make_user_with_session("bob")
    assert client.get("/settings/ai-key").json()["configured"] is False


# --- resolution ---


def test_resolve_prefers_the_users_own_key(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    client.put(
        "/settings/ai-key",
        json={"provider": "gemini", "api_key": GEMINI_KEY, "model": "gemini-2.5-pro"},
    )

    mode, provider, source = resolve_provider(db, user.id)
    assert mode == "cheap"
    assert source == "user"
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-2.5-pro"


def test_resolve_falls_back_to_server_config(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    mode, provider, source = resolve_provider(db, user.id)
    assert mode == "cheap"
    assert source == "server"
    assert isinstance(provider, MockProvider)


def test_resolve_decrypt_failure_is_a_user_key_error(client, db, make_user_with_session):
    import pytest

    from app.ai.errors import UserAIKeyError

    user, _ = make_user_with_session("alice")
    db.add(UserAIKey(user_id=user.id, provider="gemini", key_enc="not-a-fernet-token"))
    db.flush()

    with pytest.raises(UserAIKeyError):
        resolve_provider(db, user.id)


def test_resolve_reports_none_when_ai_is_off(client, db, make_user_with_session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "off")
    user, _ = make_user_with_session("alice")
    mode, provider, source = resolve_provider(db, user.id)
    assert mode == "deterministic_only"
    assert provider is None
    assert source == "none"
