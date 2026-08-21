from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ProviderConnection, User
from app.models import Session as SessionRow
from app.security import decrypt_token, hash_session_token, note_unrequested_scopes
from app.services.github_client import GitHubClient

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
    with GitHubClient(decrypt_token(connection.access_token_enc)) as client:
        yield client


GitHubDep = Annotated[GitHubClient, Depends(get_github_client)]
