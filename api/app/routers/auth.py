import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from app import security
from app.config import settings
from app.deps import CurrentUser, DbSession
from app.models import ProviderConnection, User
from app.models import Session as SessionRow

router = APIRouter(prefix="/auth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"


def _secure_cookies() -> bool:
    return settings.environment == "production"


@router.get("/github/login")
def github_login() -> RedirectResponse:
    if not settings.github_client_id:
        raise HTTPException(
            status_code=503,
            detail={"code": "oauth_not_configured", "message": "GitHub OAuth is not configured"},
        )
    state = secrets.token_urlsafe(32)
    # No scope parameter on purpose: empty scope means public read-only access
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.frontend_origin + "/api/backend/auth/github/callback",
            "state": state,
        }
    )
    response = RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{params}", status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        security.sign_state(state),
        max_age=security.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_secure_cookies(),
    )
    return response


def _fetch_github_identity(code: str) -> tuple[str, str, dict[str, Any]]:
    try:
        with httpx.Client(timeout=10) as client:
            token_resp = client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            token_payload = token_resp.json()
            access_token = token_payload.get("access_token", "")
            if not access_token:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "oauth_exchange_failed",
                        "message": "GitHub rejected the authorization code",
                    },
                )
            user_resp = client.get(
                GITHUB_USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "github_unavailable", "message": "GitHub did not respond as expected"},
        ) from exc
    return access_token, token_payload.get("scope") or "", user_resp.json()


@router.get("/github/callback")
def github_callback(request: Request, code: str, state: str, db: DbSession) -> RedirectResponse:
    signed_state = request.cookies.get(STATE_COOKIE, "")
    if not security.verify_state(signed_state, state):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_state",
                "message": "OAuth state is missing, expired, or does not match",
            },
        )

    access_token, scopes, gh_user = _fetch_github_identity(code)
    now = datetime.now(UTC)

    user = db.execute(select(User).where(User.github_id == gh_user["id"])).scalar_one_or_none()
    if user is None:
        user = User(github_id=gh_user["id"])
        db.add(user)
    user.login = gh_user["login"]
    user.name = gh_user.get("name")
    user.avatar_url = gh_user.get("avatar_url")
    user.updated_at = now
    db.flush()

    connection = db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == "github",
        )
    ).scalar_one_or_none()
    if connection is None:
        connection = ProviderConnection(user_id=user.id, provider="github")
        db.add(connection)
    connection.provider_account_id = gh_user["id"]
    connection.access_token_enc = security.encrypt_token(access_token)
    connection.scopes = scopes
    connection.token_invalid = False
    connection.updated_at = now

    session_token = security.mint_session(db, user.id)
    db.commit()

    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_secure_cookies(),
    )
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, db: DbSession) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(
            delete(SessionRow).where(SessionRow.token_hash == security.hash_session_token(token))
        )
        db.commit()
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    connection = db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == "github",
        )
    ).scalar_one_or_none()
    return {
        "id": str(user.id),
        "login": user.login,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "github_connected": connection is not None and not connection.token_invalid,
    }
