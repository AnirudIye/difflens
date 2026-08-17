import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession, GitHubDep, github_reconnect_required
from app.models import ProviderConnection, PullRequest, Repository, User, UserRepository
from app.services import repo_service
from app.services.github_client import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimited,
    GitHubTransient,
)

router = APIRouter(prefix="/repositories")


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": "Repository not found"},
    )


def _mark_token_invalid(db: Session, user: User) -> None:
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


def _github_failure(db: Session, user: User, exc: GitHubError) -> HTTPException:
    if isinstance(exc, GitHubAuthError):
        _mark_token_invalid(db, user)
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
    # GitHubNotFound: the repo vanished or access was revoked on GitHub's side,
    # indistinguishable from an id that never existed
    return _not_found()


def _repo_item(repo: Repository) -> dict[str, Any]:
    return {
        "id": str(repo.id),
        "full_name": repo.full_name,
        "private": repo.private,
        "default_branch": repo.default_branch,
        "html_url": repo.html_url,
        "last_synced_at": repo.last_synced_at,
    }


def _pull_item(pull: PullRequest) -> dict[str, Any]:
    return {
        "id": str(pull.id),
        "number": pull.github_number,
        "title": pull.title,
        "author_login": pull.author_login,
        "state": pull.state,
        "base_ref": pull.base_ref,
        "head_ref": pull.head_ref,
        "head_sha": pull.head_sha,
        "html_url": pull.html_url,
        "github_updated_at": pull.github_updated_at,
    }


@router.get("")
def list_repositories(
    user: CurrentUser, db: DbSession, client: GitHubDep, sync: bool = True
) -> dict[str, Any]:
    if sync:
        try:
            repos = repo_service.sync_user_repositories(db, user, client)
        except GitHubError as exc:
            raise _github_failure(db, user, exc) from exc
    else:
        repos = repo_service.list_user_repositories(db, user)
    return {"items": [_repo_item(repo) for repo in repos]}


@router.get("/{repo_id}/pull-requests")
def list_pull_requests(
    repo_id: UUID, user: CurrentUser, db: DbSession, client: GitHubDep
) -> dict[str, Any]:
    repo = db.execute(
        select(Repository)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(UserRepository.user_id == user.id, Repository.id == repo_id)
    ).scalar_one_or_none()
    if repo is None:
        raise _not_found()
    try:
        pulls = repo_service.list_pull_requests(db, repo, client)
    except GitHubError as exc:
        raise _github_failure(db, user, exc) from exc
    return {"items": [_pull_item(pull) for pull in pulls]}
