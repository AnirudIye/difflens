"""GitHubClient's non-JSON and repo-snapshot surface: contents, branch heads,
and the tarball download with its redirect and size cap."""

import httpx
import pytest

import app.services.github_client as github_client
from app.services.github_client import (
    GitHubClient,
    GitHubNotFound,
    GitHubSnapshotTooLarge,
)
from tests.conftest import make_tarball

HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd"


def test_get_file_content_decodes_base64(github):
    github.contents[("app/greeting.py", HEAD)] = b"def greet():\n    return 'hi'\n"
    with GitHubClient("gho_test") as client:
        blob = client.get_file_content("octocat/alpha", "app/greeting.py", HEAD)
    assert blob == b"def greet():\n    return 'hi'\n"


def test_get_file_content_returns_none_when_github_wont_inline(github):
    # Files over 1MB come back with encoding "none" and empty content; the
    # worker skips them rather than sinking the review
    github.responses["/repos/octocat/alpha/contents/big.bin"] = httpx.Response(
        200, json={"encoding": "none", "content": "", "size": 5_000_000}
    )
    with GitHubClient("gho_test") as client:
        assert client.get_file_content("octocat/alpha", "big.bin", HEAD) is None


def test_get_file_content_returns_none_for_directories(github):
    # A directory listing is a JSON array, not a file payload
    github.responses["/repos/octocat/alpha/contents/app"] = httpx.Response(200, json=[])
    with GitHubClient("gho_test") as client:
        assert client.get_file_content("octocat/alpha", "app", HEAD) is None


def test_get_repo_returns_the_payload(github):
    payload = {"full_name": "octocat/alpha", "default_branch": "trunk"}
    github.repo_details["octocat/alpha"] = payload
    with GitHubClient("gho_test") as client:
        assert client.get_repo("octocat/alpha") == payload


def test_get_branch_head_returns_the_commit_sha(github):
    github.branches[("octocat/alpha", "main")] = HEAD
    with GitHubClient("gho_test") as client:
        assert client.get_branch_head("octocat/alpha", "main") == HEAD


def test_get_branch_head_maps_404_to_not_found(github):
    # An empty repository and a renamed branch both answer 404
    with GitHubClient("gho_test") as client:
        with pytest.raises(GitHubNotFound):
            client.get_branch_head("octocat/alpha", "main")


def test_download_tarball_follows_the_redirect_and_streams_to_disk(github, tmp_path):
    """GitHub 302s the tarball endpoint to codeload on another origin; the
    client must follow the hop (dropping auth, which the fake asserts) and
    stream the body into the destination file byte for byte."""
    blob = make_tarball({"a.py": b"x = 1\n"})
    github.tarballs[("octocat/alpha", HEAD)] = blob
    destination = tmp_path / "snapshot.tar.gz"

    with GitHubClient("gho_test") as client:
        client.download_tarball("octocat/alpha", HEAD, destination)

    assert destination.read_bytes() == blob
    hosts = [request.url.host for request in github.calls]
    assert "codeload.example" in hosts, "the redirect was never followed"


def test_download_tarball_refuses_a_body_over_the_cap(github, tmp_path, monkeypatch):
    blob = make_tarball({"a.py": b"x = 1\n"})
    github.tarballs[("octocat/alpha", HEAD)] = blob
    monkeypatch.setattr(github_client, "MAX_TARBALL_BYTES", len(blob) - 1)

    with GitHubClient("gho_test") as client:
        with pytest.raises(GitHubSnapshotTooLarge):
            client.download_tarball("octocat/alpha", HEAD, tmp_path / "snapshot.tar.gz")


def test_download_tarball_maps_404_to_not_found(github, tmp_path):
    destination = tmp_path / "snapshot.tar.gz"
    with GitHubClient("gho_test") as client:
        with pytest.raises(GitHubNotFound):
            client.download_tarball("octocat/alpha", HEAD, destination)
    assert not destination.exists()
