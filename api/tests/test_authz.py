"""Every endpoint, swept for the two authorization questions that matter.

1. Does it require a session at all?
2. Can one account reach another account's rows?

The table below is the answer to (1) for every route in the app, and
``test_every_endpoint_is_classified`` fails if a route is added without an
entry, so a new endpoint cannot join the app unclassified. That guard is the
point of this file: per-router IDOR tests already existed, but nothing forced
a new router to have any.

Foreign rows answer 404, never 403. A 403 would confirm the row exists, which
is an existence oracle over other people's review ids; the tests below assert
the foreign answer is byte-identical to the answer for an id nobody owns.
"""

import uuid

import pytest

from app.main import app

# Reachable without a session, each for a stated reason.
PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness probe for Render",
    ("GET", "/ready"): "readiness probe for Render",
    ("GET", "/auth/github/login"): "starts the OAuth dance, there is no session yet",
    ("GET", "/auth/github/callback"): "finishes the OAuth dance, still no session",
    ("POST", "/auth/logout"): "signing out is idempotent, and 401 here would be theatre",
    # The demo exists so a visitor with no account can see a review. Both
    # routes are scoped to Repository.is_demo by construction rather than by
    # a check, take no id, and answer 404 entirely when DEMO_MODE is off.
    ("GET", "/demo/review"): "the public demo, readable without an account",
    ("POST", "/demo/review/rerun"): "the public demo's rerun, IP rate limited",
    # Anonymous senders are the point: the legal pages promise privacy
    # rights requests through this form, and a deletion request may come
    # from someone who cannot sign in. IP rate limited, honeypot filtered.
    ("POST", "/contact"): "the contact form, writable without an account",
}

# (method, path template, JSON body) - everything that must answer 401 with no
# session. Path ids are filled with a random UUID: authentication has to be
# settled before any lookup, so an unauthenticated caller learns nothing about
# whether the id exists.
AUTHENTICATED: list[tuple[str, str, dict | None]] = [
    ("GET", "/auth/me", None),
    ("GET", "/repositories", None),
    ("GET", "/repositories/{id}/pull-requests", None),
    ("POST", "/reviews", {"pull_request_id": str(uuid.uuid4())}),
    ("GET", "/reviews/{id}", None),
    ("POST", "/reviews/{id}/cancel", None),
    ("POST", "/reviews/{id}/rerun", None),
    ("PUT", "/findings/{id}/feedback", {"verdict": "useful"}),
    ("DELETE", "/findings/{id}/feedback", None),
    ("GET", "/settings/ai-key", None),
    ("PUT", "/settings/ai-key", {"provider": "gemini", "api_key": "AIza-not-a-real-key"}),
    ("DELETE", "/settings/ai-key", None),
]


def _fill(path: str) -> str:
    return path.replace("{id}", str(uuid.uuid4()))


def _documented_endpoints() -> set[tuple[str, str]]:
    """Every (method, path) FastAPI publishes.

    The OpenAPI schema is the enumeration rather than app.routes because
    recent FastAPI wraps included routers in a private type. Anything marked
    include_in_schema=False would escape this sweep; nothing here is.
    """
    schema = app.openapi()
    return {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }


def _template(path: str) -> str:
    """Turn a concrete test path back into its OpenAPI template."""
    return (
        path.replace("/repositories/{id}/", "/repositories/{repo_id}/")
        .replace("/reviews/{id}", "/reviews/{review_id}")
        .replace("/findings/{id}/", "/findings/{finding_id}/")
    )


def test_every_endpoint_is_classified():
    classified = set(PUBLIC) | {(method, _template(path)) for method, path, _ in AUTHENTICATED}
    documented = _documented_endpoints()
    assert documented - classified == set(), "endpoint added with no authorization decision"
    assert classified - documented == set(), "table lists an endpoint that no longer exists"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    AUTHENTICATED,
    ids=[f"{method} {path}" for method, path, _ in AUTHENTICATED],
)
def test_rejects_a_missing_session(client, method, path, body):
    client.cookies.clear()
    response = client.request(method, _fill(path), json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    AUTHENTICATED,
    ids=[f"{method} {path}" for method, path, _ in AUTHENTICATED],
)
def test_rejects_a_forged_session_cookie(client, method, path, body):
    # The cookie is an opaque token hashed into a sessions row. A well-formed
    # value that was never minted must be worth exactly as much as no cookie.
    client.cookies.set("session", "not-a-token-anyone-minted")
    response = client.request(method, _fill(path), json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    AUTHENTICATED,
    ids=[f"{method} {path}" for method, path, _ in AUTHENTICATED],
)
def test_rejects_an_expired_session(client, db, make_user_with_session, method, path, body):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models import Session as SessionRow
    from app.security import hash_session_token

    _user, token = make_user_with_session("expired-user")
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == hash_session_token(token))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    response = client.request(method, _fill(path), json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# --- public endpoints, each asserted for the reason it is public ---


def test_health_and_ready_need_no_session(client):
    client.cookies.clear()
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_login_redirects_without_a_session(client):
    client.cookies.clear()
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://github.com/login/oauth/authorize")


def test_callback_sends_a_forged_state_back_to_sign_in(client):
    client.cookies.clear()
    response = client.get(
        "/auth/github/callback",
        params={"code": "abc", "state": "never-issued"},
        follow_redirects=False,
    )
    # Refused, but on a page: a browser lands on this route directly, so its
    # failures have to be somewhere a person can act from
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=expired"
    assert client.cookies.get("session") is None


def test_logout_without_a_session_is_a_no_op(client):
    client.cookies.clear()
    assert client.post("/auth/logout").status_code == 204


# --- one account reaching another account's rows ---


def _seed_alice(db, user):
    """A repository, pull request, review, and finding that belong to alice.

    Seeded directly rather than through GitHub, because the point is the
    ownership rows, and a foreign caller must not be able to tell the
    difference between "not yours" and "not there".
    """
    from app.models import (
        Finding,
        PullRequest,
        Repository,
        Review,
        UserRepository,
    )

    repo = Repository(github_id=int(uuid.uuid4().int % 10**8), full_name="octocat/private-ish")
    db.add(repo)
    db.flush()
    db.add(UserRepository(user_id=user.id, repository_id=repo.id))
    pull = PullRequest(
        repository_id=repo.id,
        github_number=7,
        title="Add the demo feature",
        state="open",
        head_sha="a" * 40,
    )
    db.add(pull)
    db.flush()
    review = Review(
        user_id=user.id,
        pull_request_id=pull.id,
        head_sha=pull.head_sha,
        base_sha="b" * 40,
        status="completed",
    )
    db.add(review)
    db.flush()
    finding = Finding(
        review_id=review.id,
        file_path="app/main.py",
        start_line=12,
        end_line=14,
        severity="high",
        category="correctness",
        source="ai",
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        title="Off by one in pagination",
    )
    db.add(finding)
    db.flush()
    return repo, pull, review, finding


@pytest.fixture
def two_accounts(client, db, make_user_with_session):
    """Alice owns everything; bob owns nothing but is fully signed in.

    Bob deliberately never calls GET /repositories, because syncing would
    grant him the same repository and dissolve the test.
    """
    alice, alice_token = make_user_with_session("alice")
    rows = _seed_alice(db, alice)
    _bob, bob_token = make_user_with_session("bob")
    return alice_token, bob_token, rows


def _owned_requests(repo, pull, review, finding) -> list[tuple[str, str, str, dict | None]]:
    """(label, method, path, body) for every route that takes an id someone owns."""
    return [
        ("repo pull requests", "GET", f"/repositories/{repo.id}/pull-requests", None),
        ("create review", "POST", "/reviews", {"pull_request_id": str(pull.id)}),
        ("read review", "GET", f"/reviews/{review.id}", None),
        ("cancel review", "POST", f"/reviews/{review.id}/cancel", None),
        ("rerun review", "POST", f"/reviews/{review.id}/rerun", None),
        ("put feedback", "PUT", f"/findings/{finding.id}/feedback", {"verdict": "useful"}),
        ("delete feedback", "DELETE", f"/findings/{finding.id}/feedback", None),
    ]


def _strip_request_id(payload: dict) -> dict:
    error = dict(payload["error"])
    error.pop("request_id", None)
    return {"error": error}


def test_foreign_ids_are_indistinguishable_from_missing_ones(client, two_accounts):
    _alice_token, bob_token, (repo, pull, review, finding) = two_accounts
    client.cookies.set("session", bob_token)

    for label, method, path, body in _owned_requests(repo, pull, review, finding):
        foreign = client.request(method, path, json=body)
        assert foreign.status_code == 404, f"{label} leaked a status other than 404"

        # Same shape of request, an id that belongs to nobody at all
        nowhere = client.request(method, _fill(_as_template(path)), json=_blank_body(body))
        assert nowhere.status_code == 404, f"{label} control request was not a 404"
        assert _strip_request_id(foreign.json()) == _strip_request_id(nowhere.json()), (
            f"{label} answers differently for a foreign id than for a missing one, "
            "which tells the caller the foreign id exists"
        )


def _as_template(path: str) -> str:
    """Swap the concrete uuid in a seeded path back out for the {id} marker."""
    parts = path.split("/")
    return "/".join("{id}" if _looks_like_uuid(part) else part for part in parts)


def _looks_like_uuid(part: str) -> bool:
    try:
        uuid.UUID(part)
    except ValueError:
        return False
    return True


def _blank_body(body: dict | None) -> dict | None:
    if body and "pull_request_id" in body:
        return {"pull_request_id": str(uuid.uuid4())}
    return body


def test_the_owner_can_still_reach_all_of_it(client, two_accounts):
    """The control for the test above: without this, a broken seed would make
    every 404 above pass for the wrong reason."""
    alice_token, _bob_token, (repo, pull, review, finding) = two_accounts
    client.cookies.set("session", alice_token)

    assert client.get(f"/reviews/{review.id}").status_code == 200
    assert (
        client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"}).status_code
        == 200
    )
    assert client.delete(f"/findings/{finding.id}/feedback").status_code == 200
    # cancel refuses a completed review on its own merits, not on ownership
    assert client.post(f"/reviews/{review.id}/cancel").status_code == 409
    assert pull.id and repo.id  # the seeded rows are real


def test_repository_list_shows_only_your_own(client, two_accounts):
    alice_token, bob_token, (repo, _pull, _review, _finding) = two_accounts

    client.cookies.set("session", alice_token)
    mine = client.get("/repositories", params={"sync": "false"}).json()["items"]
    assert [item["id"] for item in mine] == [str(repo.id)]

    client.cookies.set("session", bob_token)
    theirs = client.get("/repositories", params={"sync": "false"}).json()["items"]
    assert theirs == []


def test_stored_ai_keys_are_per_account(client, two_accounts):
    alice_token, bob_token, _rows = two_accounts

    client.cookies.set("session", alice_token)
    stored = client.put(
        "/settings/ai-key",
        json={"provider": "gemini", "api_key": "AIzaSyAliceKeyValue12345"},
    )
    assert stored.status_code == 200
    assert stored.json()["key_hint"] == "2345"

    client.cookies.set("session", bob_token)
    assert client.get("/settings/ai-key").json()["configured"] is False
    # Bob deleting "his" key must not reach across accounts
    assert client.delete("/settings/ai-key").status_code == 200

    client.cookies.set("session", alice_token)
    assert client.get("/settings/ai-key").json()["key_hint"] == "2345"
