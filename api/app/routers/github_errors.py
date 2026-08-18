"""Map GitHubClient failures onto the API's error envelope.

Shared by every router that talks to GitHub mid-request so the same failure
always looks the same to the frontend.
"""

import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import github_reconnect_required
from app.models import ProviderConnection, User
from app.services.github_client import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimited,
    GitHubTransient,
)


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "not_found", "message": message})


def mark_token_invalid(db: Session, user: User) -> None:
    # The failed call may have left a partial sync pending; drop it before flagging
    db.rollback()
    connection = db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.provider == "github",
        )
    ).scalar_one_or_none()
    if connection is not None:
        connection.token_invalid = True
        db.commit()


def github_failure(db: Session, user: User, exc: GitHubError, missing: str) -> HTTPException:
    if isinstance(exc, GitHubAuthError):
        mark_token_invalid(db, user)
        return github_reconnect_required()
    if isinstance(exc, GitHubRateLimited):
        retry_after = 60 if exc.reset_at is None else max(exc.reset_at - int(time.time()), 1)
        return HTTPException(
            status_code=503,
            detail={
                "code": "github_rate_limited",
                "message": "GitHub API rate limit exceeded, try again later",
            },
            headers={"Retry-After": str(retry_after)},
        )
    if isinstance(exc, GitHubTransient):
        return HTTPException(
            status_code=502,
            detail={"code": "github_unavailable", "message": "GitHub did not respond as expected"},
        )
    # GitHubNotFound: the resource vanished or access was revoked on GitHub's
    # side, indistinguishable from an id that never existed
    return not_found(missing)
