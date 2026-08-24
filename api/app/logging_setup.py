"""Structured logging, with a redaction processor standing between the app
and anything that writes a log line.

The threat this defends against is mundane and common: a secret reaching a
log because someone formatted an exception, a URL, or a request body into an
event. Render keeps container logs; a token in one is a token disclosed.

Redaction happens on two axes, because either alone leaves a hole:

- by key, for the fields we name ourselves (``token=...``)
- by value shape, for secrets that arrive inside a string nobody inspected,
  which is how they actually escape - ``error="AuthError: 401 for
  https://host/v1?key=AIza..."`` has no suspicious key name anywhere.

Both axes are structlog processors, so they only see what the app logs
through structlog. Libraries that log through the standard library bypass
them entirely, and uvicorn's access logger is exactly that: it writes the
full request line, query string included, so a GET carrying an OAuth code
would land in the log verbatim. `_ScrubFilter` closes that path.
"""

import logging
import re

import structlog
from structlog.typing import EventDict

SENSITIVE_FRAGMENTS = ("token", "secret", "authorization", "cookie", "password", "api_key")

REDACTED = "[redacted]"

# Vendor prefixes are used rather than generic entropy tests: a false positive
# here silently destroys evidence in a log, so each pattern names a real
# credential format. Lengths are minimums, since vendors lengthen keys.
_SECRET_PATTERNS = (
    # GitHub user, OAuth, app, server, and refresh tokens, then fine-grained PATs
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI, including sk-proj-
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),  # Google AI Studio
    re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{20,}"),  # our own Fernet ciphertext
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)

# Anything after these in a query string, whatever the value looks like. This
# is the catch-all for a vendor that puts credentials in a URL.
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?token|token|client[_-]?secret|code|state)=)[^&\s\"']+"
)

# Redaction walks whatever a caller passed, and a caller can pass a graph.
# Bounded so a log line can never become a traversal.
_MAX_DEPTH = 6


def scrub(text: str) -> str:
    """Blank out credential-shaped substrings, leaving the rest readable."""
    text = _QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def _redact_value(value: object, depth: int) -> object:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(key) else _redact_value(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        # Always a list back: renderers do not care, and rebuilding the
        # original type breaks on anything with a positional constructor
        return [_redact_value(item, depth + 1) for item in value]
    return value


def redact_sensitive(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            # "event" is the message itself, and it is redacted like any other
            # value: a formatted exception is the likeliest carrier of a secret
            event_dict[key] = _redact_value(event_dict[key], 0)
    return event_dict


def setup_logging(environment: str) -> None:
    renderer = (
        structlog.processors.JSONRenderer()
        if environment == "production"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Flatten the traceback into a string first. log.exception()
            # otherwise hands the renderer an exc_info tuple that redaction
            # cannot see into, and a traceback carries the exception message
            structlog.processors.format_exc_info,
            # Last before the renderer, so nothing added by an earlier
            # processor (stack traces included) skips redaction
            redact_sensitive,
            renderer,
        ]
    )
    _install_stdlib_scrubbing()


class _ScrubFilter(logging.Filter):
    """Run the same value-shape redaction over standard-library log records.

    Attached to the loggers rather than to a handler on purpose: uvicorn
    installs its own handlers when it starts, which may be after this runs,
    but the logger objects are reachable by name at any time and a filter on
    the logger runs before the record reaches any handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: scrub(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    scrub(value) if isinstance(value, str) else value for value in record.args
                )
        return True


def _install_stdlib_scrubbing() -> None:
    scrubber = _ScrubFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, _ScrubFilter) for existing in logger.filters):
            logger.addFilter(scrubber)
