"""Adapter over the detect-secrets Python API for reviewable files of any language.

Two things decide what reaches the report: how strong the detector is, and
what kind of file it fired in. A pull request review could be careless about
both, because `touches_change` meant every match sat on a line the author had
just written. A repository snapshot has no such line, so precision has to come
from somewhere else (see app/analysis/paths.py for the full story).

The rule: a detector that matches a real token shape reports everywhere,
because a live AWS key checked into a test fixture is still a live AWS key. A
detector that guesses from a keyword or from entropy reports only in
production code, because fixtures and `.env.example` files exist precisely to
hold things that look like credentials. Suppressed matches are counted and
the reviewer is told how many, never dropped in silence.
"""

from pathlib import Path

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import default_settings

from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.validator import is_reviewable, touches_change
from app.analysis.models import Confidence, Finding, Severity
from app.analysis.paths import is_credential_bearing

_DOWNGRADE: dict[Confidence, Confidence] = {"high": "medium", "medium": "low", "low": "low"}

# Detectors that match a documented token shape. A hit is evidence about the
# string itself, so it is worth reporting wherever it appears.
HIGH_SIGNAL_DETECTORS = {
    "AWSKeyDetector",
    "PrivateKeyDetector",
    "JwtTokenDetector",
    "StripeDetector",
    "GitHubTokenDetector",
    "SlackDetector",
    "SendGridDetector",
    "NpmDetector",
    "SquareOAuthDetector",
    "TwilioKeyDetector",
    "MailchimpDetector",
}

# Detectors that guess. BasicAuthDetector is here despite matching a shape
# because the shape it matches, user:password@host, is what every placeholder
# connection string in every .env.example looks like.
_LOW_SIGNAL_SEVERITY: dict[str, Severity] = {
    "BasicAuthDetector": "high",
    "KeywordDetector": "medium",
    "Base64HighEntropyString": "medium",
    "HexHighEntropyString": "medium",
}

ENTROPY_DETECTORS = {"Base64HighEntropyString", "HexHighEntropyString"}

# Words a project uses to say "this is not the real value". Checked only for
# the guessing detectors: a string that announces itself as a placeholder is
# one, and a real key never asks to be ignored.
PLACEHOLDER_WORDS = (
    "placeholder",
    "example",
    "sample",
    "dummy",
    "changeme",
    "change-me",
    "replace-me",
    "replaceme",
    "your-",
    "your_",
    "xxxx",
    "redacted",
    "todo",
    "notasecret",
    "fake",
)

# A credential lives in a literal, and a short one is not a credential. The
# keyword detector fires on `if (typeof currentPassword !== "string")` purely
# for the identifier; the only quoted thing on that line is the word "string".
# So the test is not "is there a literal" but "is there a literal long enough
# to be a secret and not an obvious type name".
_QUOTES = "'\"`"
MIN_CREDENTIAL_LITERAL = 8
NON_CREDENTIAL_LITERALS = {
    "string",
    "number",
    "boolean",
    "object",
    "function",
    "undefined",
    "symbol",
    "bigint",
    "base64",
    "utf-8",
    "utf8",
    "password",
    "secret",
    "token",
}

# Hosts that mean "this machine", so a user:password@host default here is a
# local development convenience rather than a leak.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "example.com", "host.docker.internal")


def _quoted_literals(line: str) -> list[str]:
    out: list[str] = []
    quote = ""
    buf: list[str] = []
    for char in line:
        if quote:
            if char == quote:
                out.append("".join(buf))
                quote, buf = "", []
            else:
                buf.append(char)
        elif char in _QUOTES:
            quote = char
    return out


def has_credential_literal(line: str) -> bool:
    for literal in _quoted_literals(line):
        if len(literal) < MIN_CREDENTIAL_LITERAL:
            continue
        if literal.strip().lower() in NON_CREDENTIAL_LITERALS:
            continue
        return True
    return False


def looks_like_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS)


def points_at_local_host(line: str) -> bool:
    lowered = line.lower()
    return any(host in lowered for host in LOCAL_HOSTS)


# An encoding alphabet is every symbol exactly once, which is what makes it an
# alphabet. A random secret of the same length almost certainly repeats one:
# drawing 24 symbols from 64 with no repeat has probability well under 1%. So
# "long, and no character twice" separates the base32 constant in every TOTP
# implementation from an actual key, without ever looking at a real secret.
MIN_ALPHABET_RUN = 16


def _tokens(line: str) -> list[str]:
    token: list[str] = []
    out: list[str] = []
    for char in line:
        if char.isalnum() or char in "+/-_":
            token.append(char)
        elif token:
            out.append("".join(token))
            token = []
    if token:
        out.append("".join(token))
    return out


def looks_like_encoding_alphabet(line: str) -> bool:
    for token in _tokens(line):
        if len(token) >= MIN_ALPHABET_RUN and len(set(token)) == len(token):
            return True
    return False


def severity_for(detector: str, credential_bearing: bool) -> Severity | None:
    """The severity to report at, or None to suppress this match entirely."""
    if detector in HIGH_SIGNAL_DETECTORS:
        # Still reported in a fixture, one notch down: worth a look, not an alarm
        return "medium" if credential_bearing else "critical"
    if credential_bearing:
        return None
    return _LOW_SIGNAL_SEVERITY.get(detector, "medium")


def _line_text(workspace: Path, path: str, line: int, cache: dict[str, list[str]]) -> str:
    if path not in cache:
        try:
            text = (workspace / path).read_text(encoding="utf-8", errors="replace")
            cache[path] = text.splitlines()
        except OSError:
            cache[path] = []
    lines = cache[path]
    return lines[line - 1] if 0 < line <= len(lines) else ""


class SecretsAnalyzer:
    name = "detect-secrets"

    def __init__(self) -> None:
        # Read by the pipeline after analyze(), so the summary can say how
        # many matches were held back and why
        self.suppressed = 0

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        by_scan_path = {
            str(workspace / path): path
            for path in sorted(index.files)
            if is_reviewable(index, workspace, path)
        }
        if not by_scan_path:
            return []

        collection = SecretsCollection()
        with default_settings():
            for scan_path in by_scan_path:
                collection.scan_file(scan_path)

        detector_classes = get_mapping_from_secret_type_to_class()
        line_cache: dict[str, list[str]] = {}
        findings: list[Finding] = []
        suppressed = 0

        for scanned, secret in collection:
            path = by_scan_path[scanned]
            line = secret.line_number
            plugin = detector_classes.get(secret.type)
            detector = plugin.__name__ if plugin else secret.type

            severity = severity_for(detector, is_credential_bearing(path))
            if severity is None:
                suppressed += 1
                continue
            text = _line_text(workspace, path, line, line_cache)
            if detector in ENTROPY_DETECTORS and looks_like_encoding_alphabet(text):
                suppressed += 1
                continue
            if detector not in HIGH_SIGNAL_DETECTORS:
                # Only the guessing detectors get these. A structured match is
                # evidence about the string itself and is never talked out of.
                if not has_credential_literal(text) or looks_like_placeholder(text):
                    suppressed += 1
                    continue
                if detector == "BasicAuthDetector" and points_at_local_host(text):
                    suppressed += 1
                    continue

            confidence: Confidence = "high" if detector in HIGH_SIGNAL_DETECTORS else "medium"
            # In a diff review, a match away from the changed lines is less
            # likely to be something this pull request introduced. A snapshot
            # has no changed lines, so this never fires there.
            if not touches_change(index, path, line, line):
                confidence = _DOWNGRADE[confidence]

            findings.append(
                Finding(
                    file_path=path,
                    start_line=line,
                    end_line=line,
                    severity=severity,
                    category="security",
                    confidence=confidence,
                    source="deterministic",
                    title=f"Potential {secret.type} committed",
                    # never echo the matched value into the review output
                    explanation=(
                        f"Line {line} of {path} matches the {secret.type} pattern. "
                        "The matched value is deliberately omitted from this report."
                    ),
                    recommendation="Rotate this credential and purge it from git history",
                    rule_id=detector,
                    tool="detect-secrets",
                )
            )

        self.suppressed = suppressed
        findings.sort(key=lambda f: (f.file_path, f.start_line or 0, f.rule_id or ""))
        return findings
