import base64
import binascii
from pathlib import Path
from typing import Any

import httpx

# Compressed tarball ceiling. Anything bigger cannot pass the 200MB extraction
# ceiling in worker.snapshot anyway, and stopping at download keeps the free
# tier's ephemeral disk from filling first.
MAX_TARBALL_BYTES = 100 * 1024 * 1024


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


class GitHubSnapshotTooLarge(GitHubError):
    pass


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

    def get_repo(self, full_name: str) -> dict[str, Any]:
        return self._get(f"/repos/{full_name}")

    def get_branch_head(self, full_name: str, branch: str) -> str:
        """The branch's current head commit SHA.

        404 covers an empty repository and a renamed branch alike; the caller
        decides what that means.
        """
        payload = self._get(f"/repos/{full_name}/branches/{branch}")
        return payload["commit"]["sha"]

    def download_tarball(self, full_name: str, sha: str, destination: Path) -> None:
        """Stream the repository tarball at a pinned SHA to a local file.

        GitHub answers this endpoint with a 302 to codeload, so the request
        follows redirects (httpx drops the Authorization header on the
        cross-origin hop, which is what we want; the Location URL is
        self-authorizing). The payload is not JSON, so the error mapping from
        _get is replicated here rather than reused. Aborts with
        GitHubSnapshotTooLarge past MAX_TARBALL_BYTES.
        """
        try:
            with self._client.stream(
                "GET", f"/repos/{full_name}/tarball/{sha}", follow_redirects=True
            ) as response:
                if response.status_code == 401:
                    raise GitHubAuthError("GitHub rejected the access token")
                if (
                    response.status_code == 403
                    and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    reset = response.headers.get("x-ratelimit-reset", "")
                    raise GitHubRateLimited(int(reset) if reset.isdigit() else None)
                if response.status_code == 404:
                    raise GitHubNotFound(f"GitHub returned 404 for the {sha} tarball")
                if response.status_code >= 400:
                    raise GitHubTransient(f"GitHub returned {response.status_code}")
                written = 0
                with destination.open("wb") as out:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > MAX_TARBALL_BYTES:
                            raise GitHubSnapshotTooLarge(
                                f"tarball exceeded {MAX_TARBALL_BYTES} bytes"
                            )
                        out.write(chunk)
        except httpx.HTTPError as exc:
            raise GitHubTransient("GitHub request failed") from exc

    def get_file_content(self, full_name: str, path: str, ref: str) -> bytes | None:
        """One file's bytes at a pinned SHA, or None when GitHub will not inline it.

        The contents API inlines base64 up to 1MB; beyond that it answers with
        encoding "none" (and a directory answers with a list). Both mean the
        workspace simply goes without this file.
        """
        payload = self._get(f"/repos/{full_name}/contents/{path}", params={"ref": ref})
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(payload["content"])
        except (binascii.Error, ValueError):
            return None
