from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from cryptography.fernet import InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ProviderConnection, User
from app.models import Session as SessionRow
from app.security import decrypt_token, hash_session_token, note_unrequested_scopes
from app.services.github_client import GitHubClient

log = structlog.get_logger()

DbSession = Annotated[Session, Depends(get_db)]

LAST_SEEN_BUMP_INTERVAL = timedelta(minutes=5)


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthenticated", "message": "Authentication required"},
    )


def get_current_user(request: Request, db: DbSession) -> User:
    token = request.cookies.get("session")
    if not token:
        raise _unauthenticated()
    now = datetime.now(UTC)
    row = db.execute(
        select(SessionRow, User)
        .join(User, User.id == SessionRow.user_id)
        .where(SessionRow.token_hash == hash_session_token(token), SessionRow.expires_at > now)
    ).first()
    if row is None:
        raise _unauthenticated()
    session, user = row
    # Throttled so an active user does not cost a write on every request
    if session.last_seen_at is None or now - session.last_seen_at >= LAST_SEEN_BUMP_INTERVAL:
        session.last_seen_at = now
        db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def github_reconnect_required() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "github_reconnect_required",
            "message": "GitHub access is missing or was revoked, reconnect your account",
        },
    )


def get_github_client(user: CurrentUser, db: DbSession) -> Generator[GitHubClient, None, None]:
    connection = db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == "github",
        )
    ).scalar_one_or_none()
    if connection is None or connection.token_invalid:
        raise github_reconnect_required()
    note_unrequested_scopes(connection.scopes, user.id, seen_at="token_use")
    try:
        token = decrypt_token(connection.access_token_enc)
    except InvalidToken as exc:
        # A stored token stops decrypting when TOKEN_ENCRYPTION_KEY changes,
        # and _get_fernet falls back to an EPHEMERAL key when that variable is
        # empty, so every token written since the last restart is unreadable
        # after the next one. On a free tier that sleeps, that is routine
        # rather than exotic. Reconnecting is the actual remedy and the 401
        # three lines above already says so; a raw InvalidToken here would be
        # a 500 with a traceback instead. app/ai/factory.py handles the AI-key
        # equivalent the same way.
        log.error("github_token_undecryptable", user_id=str(user.id))
        connection.token_invalid = True
        db.commit()
        raise github_reconnect_required() from exc
    with GitHubClient(token) as client:
        yield client


GitHubDep = Annotated[GitHubClient, Depends(get_github_client)]
