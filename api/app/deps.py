from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Session as SessionRow
from app.models import User
from app.security import hash_session_token

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
