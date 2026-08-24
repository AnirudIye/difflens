from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import PullRequest, Repository, User, UserRepository
from app.services.github_client import GitHubClient


def list_user_repositories(db: Session, user: User) -> list[Repository]:
    return list(
        db.execute(
            select(Repository)
            .join(UserRepository, UserRepository.repository_id == Repository.id)
            .where(UserRepository.user_id == user.id)
            .order_by(Repository.last_synced_at.desc(), Repository.full_name)
        ).scalars()
    )


def link_repositories(db: Session, user: User, repository_ids: list) -> None:
    """Link a user to repositories, tolerating rows that already exist.

    ON CONFLICT DO NOTHING rather than ORM inserts, because the caller decides
    what is missing from a snapshot it read earlier. Two requests syncing the
    same account at once (which is what a first sign-in does, one call per
    open tab) both compute the same missing set and race to insert the same
    primary key; the loser used to escape as a raw 500 on the first page a new
    account ever sees.
    """
    if not repository_ids:
        return
    db.execute(
        pg_insert(UserRepository)
        .values([{"user_id": user.id, "repository_id": repo_id} for repo_id in repository_ids])
        .on_conflict_do_nothing(index_elements=["user_id", "repository_id"])
    )


def sync_user_repositories(db: Session, user: User, client: GitHubClient) -> list[Repository]:
    payload = client.list_repos()
    now = datetime.now(UTC)

    existing = {
        repo.github_id: repo
        for repo in db.execute(
            select(Repository).where(Repository.github_id.in_([item["id"] for item in payload]))
        ).scalars()
    }
    linked = set(
        db.execute(
            select(UserRepository.repository_id).where(UserRepository.user_id == user.id)
        ).scalars()
    )

    synced: list[Repository] = []
    for item in payload:
        if item.get("private"):
            # The empty OAuth scope means GitHub should never hand us one of
            # these, and /privacy states as fact that private repositories are
            # not touched. Filtering here rather than trusting that is the
            # difference between a policy sentence and an enforced one: a
            # scope granted in some other session, or an account GitHub
            # decides to be generous about, would otherwise be listed and
            # reviewable, and a repo review ships whole-file contents to an
            # AI provider.
            continue
        repo = existing.get(item["id"])
        if repo is None:
            repo = Repository(github_id=item["id"], full_name=item["full_name"])
            db.add(repo)
        repo.owner = item["owner"]["login"]
        repo.name = item["name"]
        repo.full_name = item["full_name"]
        repo.private = item["private"]
        repo.default_branch = item.get("default_branch")
        repo.html_url = item.get("html_url")
        repo.last_synced_at = now
        repo.updated_at = now
        synced.append(repo)

    # Flush so new rows carry their database-generated ids before linking
    db.flush()
    link_repositories(db, user, [repo.id for repo in synced if repo.id not in linked])
    db.commit()
    return list_user_repositories(db, user)


def list_pull_requests(
    db: Session, repository: Repository, client: GitHubClient
) -> list[PullRequest]:
    payload = client.list_open_pulls(repository.full_name)
    now = datetime.now(UTC)

    existing = {
        pull.github_number: pull
        for pull in db.execute(
            select(PullRequest).where(PullRequest.repository_id == repository.id)
        ).scalars()
    }

    # ponytail: rows for PRs that later close on GitHub keep their last synced state;
    # review creation refetches the PR before pinning, so nothing acts on stale rows
    rows: list[PullRequest] = []
    for item in payload:
        pull = existing.get(item["number"])
        if pull is None:
            pull = PullRequest(repository_id=repository.id, github_number=item["number"])
            db.add(pull)
        apply_pull_payload(pull, item, now)
        rows.append(pull)

    db.commit()
    return rows


def apply_pull_payload(pull: PullRequest, item: dict, now: datetime) -> None:
    """Copy one GitHub pull payload onto its row. Shared by sync and review creation."""
    pull.github_id = item["id"]
    pull.title = item["title"]
    pull.author_login = (item.get("user") or {}).get("login")
    pull.state = item["state"]
    pull.base_ref = item["base"]["ref"]
    pull.head_ref = item["head"]["ref"]
    pull.base_sha = item["base"]["sha"]
    pull.head_sha = item["head"]["sha"]
    pull.html_url = item["html_url"]
    pull.github_updated_at = _github_time(item.get("updated_at"))
    pull.updated_at = now


def _github_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
