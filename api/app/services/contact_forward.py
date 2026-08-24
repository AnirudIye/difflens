"""Best-effort email forwarding for contact messages.

Storage is truth, forwarding is a doorbell, same shape as the queue: the row
is committed before this module is asked to do anything, and nothing that
happens here can fail the request. With RESEND_API_KEY or CONTACT_FORWARD_TO
unset the message simply stays in Postgres, readable in the database
console, and nothing is lost.

Resend is called over plain REST with httpx, no SDK, matching how the AI
providers are called.
"""

import httpx
import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ContactMessage

log = structlog.get_logger()

RESEND_URL = "https://api.resend.com/emails"

# The onboarding sender works without a verified domain, which fits the
# whole free-tier setup: the operator sets two env vars and owns no DNS.
FROM_ADDRESS = "DiffLens <onboarding@resend.dev>"

REQUEST_TIMEOUT = httpx.Timeout(10, connect=5)


def forward(db: Session, message: ContactMessage) -> None:
    """Send one stored contact message on by email, if forwarding is configured.

    On success `forwarded` flips to true and is committed. On any failure the
    warning below is the whole consequence: the sender already has their 200
    and the row already exists with forwarded still false, so an operator can
    see exactly which messages never went out.
    """
    if not (settings.resend_api_key and settings.contact_forward_to):
        return
    body = (
        f"Name: {message.name or '(not given)'}\n"
        f"Email: {message.email or '(not given)'}\n\n"
        f"{message.message}\n"
    )
    try:
        response = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": FROM_ADDRESS,
                "to": [settings.contact_forward_to],
                "subject": f"DiffLens contact: {message.subject or 'no subject'}",
                "text": body,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:  # any failure at all, and none may fail the request
        log.warning("contact_forward_failed", message_id=str(message.id), error=str(exc))
        return
    message.forwarded = True
    db.commit()
