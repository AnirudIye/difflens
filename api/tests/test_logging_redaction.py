"""The log redaction processor, tested against how secrets actually escape.

Redacting by key name alone catches only the fields someone remembered to
name suspiciously. The leaks that happen in practice arrive inside an
innocuous string: a formatted exception carrying a URL, a provider error
quoting the request it rejected. Both axes are covered here.
"""

import pytest

from app.logging_setup import REDACTED, redact_sensitive, scrub

GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"  # GitHub's own docs example
ANTHROPIC_KEY = "sk-ant-api03-" + "A1b2C3d4E5" * 4
OPENAI_KEY = "sk-proj-" + "Zz9Yy8Xx7W" * 4
GOOGLE_KEY = "AIza" + "SyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6"
FERNET = "gAAAAABm" + "Qw3rTy5Ui8Op1As2Df4Gh6Jk9Lz0Xc7Vb"


def process(**event):
    return redact_sensitive(None, "info", dict(event))


@pytest.mark.parametrize(
    "secret",
    [GITHUB_TOKEN, ANTHROPIC_KEY, OPENAI_KEY, GOOGLE_KEY, FERNET],
    ids=["github", "anthropic", "openai", "google", "fernet"],
)
def test_credential_shapes_are_scrubbed_out_of_any_string(secret):
    event = process(event=f"provider rejected the request using {secret} at 401")
    assert secret not in event["event"]
    assert REDACTED in event["event"]


def test_the_surrounding_text_survives():
    # A log line that redacts itself into uselessness is its own outage
    event = process(error=f"GitHubAuthError: 401 Unauthorized for token {GITHUB_TOKEN}")
    assert event["error"].startswith("GitHubAuthError: 401 Unauthorized for token ")


def test_a_credential_in_a_url_query_string_is_scrubbed():
    event = process(error="ReadTimeout: GET https://api.vendor.test/v1/models?key=whatever-it-was")
    assert "whatever-it-was" not in event["error"]
    assert "https://api.vendor.test/v1/models?key=" in event["error"]


def test_an_oauth_code_in_a_url_is_scrubbed():
    event = process(event="redirecting to /auth/github/callback?code=abc123def&state=xyz789")
    assert "abc123def" not in event["event"]
    assert "xyz789" not in event["event"]


def test_a_bearer_header_is_scrubbed():
    event = process(headers_dump="Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in event["headers_dump"]


def test_sensitive_key_names_are_blanked_whatever_the_value():
    event = process(access_token="anything at all", session_secret="s", api_key="k", cookie="c")
    assert event["access_token"] == REDACTED
    assert event["session_secret"] == REDACTED
    assert event["api_key"] == REDACTED
    assert event["cookie"] == REDACTED


def test_nested_structures_are_walked():
    event = process(
        payload={
            "outer": {"inner": [f"leaked {GITHUB_TOKEN}", {"access_token": "nested"}]},
            "safe": "octocat/alpha",
        }
    )
    inner = event["payload"]["outer"]["inner"]
    assert GITHUB_TOKEN not in inner[0]
    assert inner[1]["access_token"] == REDACTED
    assert event["payload"]["safe"] == "octocat/alpha"


def test_ordinary_fields_are_left_alone():
    event = process(event="review_completed", findings=11, repo="octocat/alpha", sha="a" * 40)
    assert event == {
        "event": "review_completed",
        "findings": 11,
        "repo": "octocat/alpha",
        "sha": "a" * 40,
    }


def test_a_sha_is_not_mistaken_for_a_secret():
    # Commit shas are logged constantly and are not credentials. A pattern
    # broad enough to catch them would make every review log unreadable.
    assert scrub("head_sha=" + "0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd") == (
        "head_sha=" + "0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd"
    )


def test_deep_nesting_terminates():
    deep: dict = {"level": "bottom"}
    for _ in range(50):
        deep = {"nest": deep}
    process(payload=deep)  # must return rather than recurse to a stack overflow


def test_a_traceback_carrying_a_secret_is_redacted_before_it_is_written():
    """The end-to-end version, and the reason format_exc_info is in the chain.

    log.exception() hands the renderer an exc_info tuple that redaction
    cannot see into, so the traceback would reach the log unscrubbed. The
    worker logs exactly this way when a review attempt fails.
    """
    import io

    import structlog

    from app.config import settings
    from app.logging_setup import setup_logging

    setup_logging("production")
    try:
        stream = io.StringIO()
        log = structlog.wrap_logger(structlog.PrintLogger(file=stream))
        try:
            raise RuntimeError(f"401 Unauthorized for https://api.vendor.test/v1?key={GOOGLE_KEY}")
        except RuntimeError:
            log.exception("job_crashed")
        written = stream.getvalue()
    finally:
        setup_logging(settings.environment)

    assert "job_crashed" in written, "the log line itself must survive"
    assert "RuntimeError" in written, "the traceback must survive"
    assert GOOGLE_KEY not in written
    assert REDACTED in written
