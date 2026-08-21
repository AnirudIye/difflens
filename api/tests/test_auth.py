from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import structlog
from sqlalchemy import select

from app import logging_setup, security
from app.models import ProviderConnection, User
from app.models import Session as SessionRow

RAW_GITHUB_TOKEN = "gho_raw_token_from_exchange"
GITHUB_USER = {
    "id": 424242,
    "login": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://avatars.example.com/octocat.png",
}


@pytest.fixture
def github_scope(request):
    """The scope GitHub reports back on the exchange.

    Empty is the normal case, because the authorize URL asks for none. A test
    that cares overrides it with indirect parametrization rather than cloning
    the whole transport stub.
    """
    return getattr(request, "param", "")


@pytest.fixture
def mock_github(monkeypatch, github_scope):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            if b"rejected-code" in request.content:
                # GitHub answers 200 with an error body, not an error status
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(
                200, json={"access_token": RAW_GITHUB_TOKEN, "scope": github_scope}
            )
        if request.url.host == "api.github.com" and request.url.path == "/user":
            assert request.headers["Authorization"] == f"Bearer {RAW_GITHUB_TOKEN}"
            return httpx.Response(200, json=GITHUB_USER)
        return httpx.Response(404)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    # TestClient subclassed httpx.Client before this patch, so only the app's
    # outbound GitHub calls are rerouted.
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport))


def _start_login(client) -> str:
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code in (302, 307)
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def test_me_without_cookie_is_401(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_with_garbage_cookie_is_401(client):
    client.cookies.set("session", "definitely-not-a-minted-token")
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_login_redirects_to_github_without_scope(client):
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code in (302, 307)

    location = urlsplit(response.headers["location"])
    assert location.scheme == "https"
    assert location.netloc == "github.com"
    assert location.path == "/login/oauth/authorize"

    params = parse_qs(location.query)
    assert "scope" not in params
    assert params["client_id"] == ["test-client-id"]
    assert params["redirect_uri"] == ["http://localhost:3000/api/backend/auth/github/callback"]
    assert params["state"][0]
    assert response.cookies.get("oauth_state")


def test_callback_with_mismatched_state_goes_back_to_login(client):
    _start_login(client)
    response = client.get(
        "/auth/github/callback",
        params={"code": "irrelevant", "state": "some-other-state"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=expired"


def test_callback_with_tampered_state_cookie_goes_back_to_login(client):
    state = _start_login(client)
    signed = client.cookies.get("oauth_state")
    assert signed
    # The cookie ends in the hex HMAC, so mangling the tail breaks the signature
    tampered = signed[:-4] + ("0000" if signed[-4:] != "0000" else "1111")
    client.cookies.delete("oauth_state")
    client.cookies.set("oauth_state", tampered)

    response = client.get(
        "/auth/github/callback",
        params={"code": "irrelevant", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=expired"


def test_full_login_flow(client, db, mock_github):
    state = _start_login(client)
    response = client.get(
        "/auth/github/callback",
        params={"code": "good-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    session_token = client.cookies.get("session")
    assert session_token

    user = db.execute(select(User).where(User.github_id == GITHUB_USER["id"])).scalar_one()
    assert user.login == "octocat"

    connection = db.execute(
        select(ProviderConnection).where(ProviderConnection.user_id == user.id)
    ).scalar_one()
    assert connection.provider == "github"
    assert RAW_GITHUB_TOKEN not in connection.access_token_enc
    assert security.decrypt_token(connection.access_token_enc) == RAW_GITHUB_TOKEN

    me = client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["login"] == "octocat"
    assert body["github_connected"] is True

    assert client.post("/auth/logout").status_code == 204

    # Re-present the old token to prove the server-side session row is gone,
    # not just the cookie
    client.cookies.set("session", session_token)
    assert client.get("/auth/me").status_code == 401


def test_expired_session_is_401(client, db):
    user = User(github_id=999001, login="expired-user")
    db.add(user)
    db.flush()
    token = security.mint_session(db, user.id)
    db.flush()

    row = db.execute(select(SessionRow).where(SessionRow.user_id == user.id)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()

    client.cookies.set("session", token)
    assert client.get("/auth/me").status_code == 401


# --- the ways a callback can arrive without a usable code ---


def test_declining_on_github_lands_on_the_sign_in_page(client):
    """Saying no on the consent screen is a decision, not a malformed request.

    GitHub sends error=access_denied and no code at all. A required `code`
    parameter answered that with a 422 body rendered as raw JSON in the
    address bar, with no way back into the product.
    """
    _start_login(client)
    response = client.get(
        "/auth/github/callback",
        params={"error": "access_denied", "error_description": "The user has denied"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=cancelled"


def test_a_callback_with_no_code_at_all_lands_on_the_sign_in_page(client):
    response = client.get("/auth/github/callback", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=github"


def test_github_refusing_the_code_lands_on_the_sign_in_page(client, mock_github):
    """The token exchange failing is nobody's fault and nothing the user can
    fix except by trying again, so it says so on a page."""
    state = _start_login(client)
    response = client.get(
        "/auth/github/callback",
        params={"code": "rejected-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=github"
    assert client.cookies.get("session") is None


def test_the_state_cookie_is_cleared_on_every_failure(client):
    _start_login(client)
    response = client.get(
        "/auth/github/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert 'oauth_state=""' in response.headers.get("set-cookie", "")


@pytest.mark.parametrize("github_scope", ["repo"], indirect=True)
def test_a_token_carrying_scopes_we_never_asked_for_is_not_silent(client, db, mock_github):
    """Sending no scope does not guarantee getting a token without one.

    An OAuth App authorization is a union of every scope the user has ever
    granted this client, so the token can come back carrying more than the
    authorize URL asked for, and this one asks for nothing at all. Sign in
    still succeeds, because refusing would lock out the account the check
    exists to protect, but it stops being invisible: the column was written
    and read by nothing before this.
    """
    state = _start_login(client)
    with structlog.testing.capture_logs() as logs:
        response = client.get(
            "/auth/github/callback",
            params={"code": "good-code", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"

    warned = [e for e in logs if e["event"] == "github_token_carries_unrequested_scopes"]
    assert warned, "a token arrived with an unrequested scope and nothing said so"
    assert warned[0]["scopes"] == "repo"
    assert warned[0]["log_level"] == "warning"
    # seen_at exists only to tell the two call sites apart in the logs, and
    # user_id is the field that makes the warning actionable at all
    assert warned[0]["seen_at"] == "callback"
    connection = db.execute(select(ProviderConnection)).scalar_one()
    assert warned[0]["user_id"] == str(connection.user_id)
    assert connection.scopes == "repo"


def test_the_ordinary_empty_scope_says_nothing(client, db, mock_github):
    """The normal case is silent, or the warning would mean nothing."""
    state = _start_login(client)
    with structlog.testing.capture_logs() as logs:
        client.get(
            "/auth/github/callback",
            params={"code": "good-code", "state": state},
            follow_redirects=False,
        )

    assert not [e for e in logs if e["event"] == "github_token_carries_unrequested_scopes"]
    connection = db.execute(select(ProviderConnection)).scalar_one()
    assert connection.scopes == ""


def test_the_scope_warning_survives_redaction():
    """capture_logs sees the event before the processors, production does not.

    The two tests above assert on structlog's captured dict, which bypasses
    the redaction chain entirely, so on their own they say nothing about what
    lands in Render's logs. The event name contains "token" and the payload is
    a credential-adjacent string, either of which a redactor could plausibly
    blank. Run the real processor over the real event shape instead.
    """
    event = logging_setup.redact_sensitive(
        None,
        "warning",
        {
            "event": "github_token_carries_unrequested_scopes",
            "scopes": "repo",
            "user_id": "b4f0a1de-0000-4000-8000-000000000000",
            "seen_at": "token_use",
        },
    )

    assert event["scopes"] == "repo"
    assert event["event"] == "github_token_carries_unrequested_scopes"
    assert event["seen_at"] == "token_use"
    # The field most plausibly at risk from a future PII rule keyed on "user"
    assert event["user_id"] == "b4f0a1de-0000-4000-8000-000000000000"


def test_an_elevated_token_is_noticed_every_time_it_is_used(
    client, db, github, make_user_with_session
):
    """The callback sees the grant once; the session outlives it.

    A scope can widen on GitHub's side without DiffLens issuing a new token,
    and nothing re-runs the callback for a signed-in user. Checking only at
    sign-in would leave an elevated token in use indefinitely with nothing
    said, so get_github_client carries the same check.
    """
    user, _token = make_user_with_session("alice")
    connection = db.execute(
        select(ProviderConnection).where(ProviderConnection.user_id == user.id)
    ).scalar_one()
    connection.scopes = "repo"
    db.flush()

    with structlog.testing.capture_logs() as logs:
        response = client.get("/repositories")

    assert response.status_code == 200
    warned = [e for e in logs if e["event"] == "github_token_carries_unrequested_scopes"]
    assert warned, "an elevated token was used and nothing said so"
    assert warned[0]["seen_at"] == "token_use"
    assert warned[0]["scopes"] == "repo"
    assert warned[0]["user_id"] == str(user.id)


def test_an_ordinary_token_stays_quiet_when_used(client, db, github, make_user_with_session):
    """The empty scope is the normal case and must not warn, or the warning
    stops meaning anything on the path that runs most often."""
    make_user_with_session("bob")

    with structlog.testing.capture_logs() as logs:
        response = client.get("/repositories")

    assert response.status_code == 200
    assert not [e for e in logs if e["event"] == "github_token_carries_unrequested_scopes"]
