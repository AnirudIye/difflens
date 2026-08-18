from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession, GitHubDep
from app.models import PullRequest, Repository, User, UserRepository
from app.routers.github_errors import github_failure, not_found
from app.services import repo_service
from app.services.github_client import GitHubError

router = APIRouter(prefix="/repositories")

MISSING = "Repository not found"


def _not_found() -> HTTPException:
    return not_found(MISSING)


def _github_failure(db: Session, user: User, exc: GitHubError) -> HTTPException:
    return github_failure(db, user, exc, MISSING)


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
