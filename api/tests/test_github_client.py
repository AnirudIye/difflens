"""GitHubClient.get_file_content: the worker's window onto head-SHA file state."""

import httpx

from app.services.github_client import GitHubClient

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
