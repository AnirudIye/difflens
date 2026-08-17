from typing import Any

import httpx


class GitHubError(Exception):
    pass


class GitHubAuthError(GitHubError):
    pass


class GitHubNotFound(GitHubError):
    pass


class GitHubTransient(GitHubError):
    pass


class GitHubRateLimited(GitHubError):
    def __init__(self, reset_at: int | None) -> None:
        super().__init__("GitHub rate limit exhausted")
        self.reset_at = reset_at


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubTransient("GitHub request failed") from exc
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the access token")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "")
            raise GitHubRateLimited(int(reset) if reset.isdigit() else None)
        if response.status_code == 404:
            raise GitHubNotFound(f"GitHub returned 404 for {path}")
        if response.status_code >= 400:
            # Covers 5xx plus oddballs like secondary rate limits, all retryable from our side
            raise GitHubTransient(f"GitHub returned {response.status_code}")
        return response.json()

    def list_repos(self) -> list[dict[str, Any]]:
        return self._get(
            "/user/repos", params={"per_page": 50, "sort": "updated", "affiliation": "owner"}
        )

    def list_open_pulls(self, full_name: str) -> list[dict[str, Any]]:
        return self._get(f"/repos/{full_name}/pulls", params={"state": "open", "per_page": 50})

    def get_pull(self, full_name: str, number: int) -> dict[str, Any]:
        return self._get(f"/repos/{full_name}/pulls/{number}")

    def compare(self, full_name: str, base_sha: str, head_sha: str) -> dict[str, Any]:
        # files[] in this payload carries the per-file patch text the review worker consumes
        return self._get(f"/repos/{full_name}/compare/{base_sha}...{head_sha}")
