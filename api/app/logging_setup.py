import structlog
from structlog.typing import EventDict

SENSITIVE_FRAGMENTS = ("token", "secret", "authorization", "cookie", "password")


def redact_sensitive(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        lowered = key.lower()
        if any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS):
            event_dict[key] = "[redacted]"
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
            redact_sensitive,
            renderer,
        ]
    )
