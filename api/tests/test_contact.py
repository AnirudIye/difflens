"""The public contact form: stored first, forwarded best effort, bot filtered.

Three properties carry the design and each gets its own tests:

- Postgres is the source of truth. A message is committed before any email
  is attempted, so a forwarding failure can lose an email but never a
  message, and an unconfigured deployment loses nothing at all.
- The honeypot answers success and stores nothing, so a bot that filled the
  hidden field learns nothing from the response.
- The per-IP limit refuses the sixth message in an hour with a sentence a
  person can read, because the frontend shows it verbatim.
"""

import httpx
import pytest
import redis
from sqlalchemy import select

from app import queue
from app.config import settings
from app.models import ContactMessage
from app.rate_limit import KEY_PREFIX
from app.services import contact_forward


@pytest.fixture(autouse=True)
def quiet_limiter(monkeypatch):
    """0 disables the limiter for every test except the ones that re-enable
    it. Without this the storage tests would all share one per-IP bucket in
    Redis (the TestClient peer is not a valid address, so every request
    counts as "unknown") and start refusing each other within the hour."""
    monkeypatch.setattr(settings, "contact_rate_limit", 0)


@pytest.fixture
def clean_contact_window():
    """Rate-limit counters live in Redis, which no database rollback touches.
    Cleared on the way in and the way out, like the demo limiter tests."""

    def clear() -> None:
        try:
            client = queue.get_redis()
            if client is None:
                return
            keys = client.keys(f"{KEY_PREFIX}:contact:*")
            if keys:
                client.delete(*keys)
        except redis.RedisError:
            pass  # the limiter fails open anyway; a cleanup that cannot run is not a failure

    clear()
    yield
    clear()


def _rows(db) -> list[ContactMessage]:
    return list(db.execute(select(ContactMessage)).scalars())


def _field_names(response) -> list[str]:
    return [item["field"] for item in response.json()["error"]["fields"]]


# --- validation, through the app's own error envelope ---


def test_a_message_is_required(client, db):
    response = client.post("/contact", json={"name": "Ada"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "message" in _field_names(response)
    assert _rows(db) == []


def test_a_whitespace_only_message_is_refused(client, db):
    response = client.post("/contact", json={"message": "   \n\t  "})
    assert response.status_code == 422
    assert "message" in _field_names(response)
    assert _rows(db) == []


def test_a_message_over_the_cap_is_refused(client, db):
    response = client.post("/contact", json={"message": "a" * 5001})
    assert response.status_code == 422
    assert "message" in _field_names(response)
    assert _rows(db) == []


@pytest.mark.parametrize("field", ["name", "email", "subject"])
def test_an_optional_field_over_the_cap_is_refused(client, db, field):
    response = client.post("/contact", json={"message": "hello", field: "a" * 201})
    assert response.status_code == 422
    assert field in _field_names(response)
    assert _rows(db) == []


def test_the_cap_boundaries_are_inclusive(client, db):
    payload = {
        "message": "a" * 5000,
        "name": "b" * 200,
        "email": "c" * 200,
        "subject": "d" * 200,
    }
    assert client.post("/contact", json=payload).status_code == 200
    assert len(_rows(db)) == 1


# --- storage ---


def test_a_message_is_stored_with_its_fields(client, db):
    response = client.post(
        "/contact",
        json={
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "subject": "Deletion request",
            "message": "Please delete my account data.",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    (row,) = _rows(db)
    assert row.name == "Ada Lovelace"
    assert row.email == "ada@example.com"
    assert row.subject == "Deletion request"
    assert row.message == "Please delete my account data."
    assert row.forwarded is False
    assert row.created_at is not None


def test_blank_optional_fields_are_stored_as_null(client, db):
    assert (
        client.post(
            "/contact",
            json={"message": "hello", "name": "  ", "email": "", "subject": None},
        ).status_code
        == 200
    )
    (row,) = _rows(db)
    assert row.name is None
    assert row.email is None
    assert row.subject is None


def test_a_message_needs_no_session(client, db):
    client.cookies.clear()
    assert client.post("/contact", json={"message": "hello"}).status_code == 200


# --- the honeypot ---


def test_the_honeypot_answers_success_and_stores_nothing(client, db):
    response = client.post(
        "/contact",
        json={"message": "totally human message", "website": "https://spam.example"},
    )
    # Byte-identical to the real success, so the bot cannot tell it was caught
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _rows(db) == []


def test_an_empty_honeypot_is_a_real_submission(client, db):
    # Browsers submit empty inputs as empty strings; that must not trip it
    assert client.post("/contact", json={"message": "hello", "website": ""}).status_code == 200
    assert len(_rows(db)) == 1


# --- forwarding: best effort, never the request's problem ---


@pytest.fixture
def forwarding_on(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "contact_forward_to", "owner@example.com")


def test_unconfigured_forwarding_never_calls_resend(client, db, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("httpx.post was called with no forwarding configured")

    monkeypatch.setattr(contact_forward.httpx, "post", explode)
    assert client.post("/contact", json={"message": "hello"}).status_code == 200
    (row,) = _rows(db)
    assert row.forwarded is False


def test_forwarding_failure_still_stores_the_message(client, db, forwarding_on, monkeypatch):
    def refuse(*args, **kwargs):
        raise httpx.ConnectError("resend is unreachable")

    monkeypatch.setattr(contact_forward.httpx, "post", refuse)
    response = client.post("/contact", json={"message": "hello"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    (row,) = _rows(db)
    assert row.message == "hello"
    assert row.forwarded is False


def test_a_resend_error_status_leaves_forwarded_false(client, db, forwarding_on, monkeypatch):
    def rejected(url, **kwargs):
        return httpx.Response(500, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(contact_forward.httpx, "post", rejected)
    assert client.post("/contact", json={"message": "hello"}).status_code == 200
    (row,) = _rows(db)
    assert row.forwarded is False


def test_forwarding_success_marks_the_row_forwarded(client, db, forwarding_on, monkeypatch):
    sent = []

    def accept(url, **kwargs):
        sent.append((url, kwargs))
        return httpx.Response(200, json={"id": "email-1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(contact_forward.httpx, "post", accept)
    response = client.post(
        "/contact",
        json={"name": "Ada", "email": "ada@example.com", "message": "hello there"},
    )
    assert response.status_code == 200
    (row,) = _rows(db)
    assert row.forwarded is True

    (call,) = sent
    url, kwargs = call
    assert url == contact_forward.RESEND_URL
    assert kwargs["headers"]["Authorization"] == "Bearer re_test_key"
    payload = kwargs["json"]
    assert payload["from"] == contact_forward.FROM_ADDRESS
    assert payload["to"] == ["owner@example.com"]
    assert payload["subject"] == "DiffLens contact: no subject"
    assert "Ada" in payload["text"]
    assert "ada@example.com" in payload["text"]
    assert "hello there" in payload["text"]


def test_the_subject_travels_into_the_email_subject(client, db, forwarding_on, monkeypatch):
    sent = []

    def accept(url, **kwargs):
        sent.append(kwargs)
        return httpx.Response(200, json={"id": "email-1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(contact_forward.httpx, "post", accept)
    assert client.post("/contact", json={"message": "hi", "subject": "Billing"}).status_code == 200
    assert sent[0]["json"]["subject"] == "DiffLens contact: Billing"


# --- the per-IP limit ---


def test_the_sixth_message_in_an_hour_is_refused(client, db, clean_contact_window, monkeypatch):
    monkeypatch.setattr(settings, "contact_rate_limit", 5)
    monkeypatch.setattr(settings, "contact_rate_limit_window_s", 3600)

    headers = {"X-Forwarded-For": "203.0.113.77"}
    for _ in range(5):
        assert (
            client.post("/contact", json={"message": "hello"}, headers=headers).status_code == 200
        )

    refused = client.post("/contact", json={"message": "hello"}, headers=headers)
    assert refused.status_code == 429
    body = refused.json()["error"]
    assert body["code"] == "rate_limited"
    # The server's own sentence, because the frontend shows it verbatim
    assert "You can send 5 messages every hour" in body["message"]
    assert int(refused.headers["Retry-After"]) >= 1
    # The refused message was not stored
    assert len(_rows(db)) == 5


def test_the_limit_counts_addresses_separately(client, db, clean_contact_window, monkeypatch):
    monkeypatch.setattr(settings, "contact_rate_limit", 1)

    first = {"X-Forwarded-For": "198.51.100.1"}
    other = {"X-Forwarded-For": "198.51.100.2"}
    assert client.post("/contact", json={"message": "one"}, headers=first).status_code == 200
    assert client.post("/contact", json={"message": "two"}, headers=first).status_code == 429
    assert client.post("/contact", json={"message": "three"}, headers=other).status_code == 200


def test_a_null_byte_is_stripped_rather_than_crashing_the_insert(client, db):
    """U+0000 cannot be stored in a Postgres text column, so an unstripped
    one turned a public form post into a 500. It is client input, and no
    message means anything by it, so it is removed and the rest is kept."""
    response = client.post(
        "/contact",
        json={"message": "before\x00after", "name": "a\x00b", "subject": "s\x00t"},
    )

    assert response.status_code == 200
    row = db.execute(select(ContactMessage)).scalars().one()
    assert row.message == "beforeafter"
    assert row.name == "ab"
    assert row.subject == "st"
