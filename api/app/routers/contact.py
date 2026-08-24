"""The public contact form: anyone may write, with or without an account.

Anonymity is the point, not an oversight. The legal pages promise privacy
rights requests through this form, and a deletion request may come from
someone who can no longer sign in, so the endpoint takes no session. Two
things keep an open POST from becoming a spam funnel:

- A honeypot field named `website`, rendered invisibly on the page. People
  never fill it; form bots usually fill every input they find. A submission
  that fills it gets the same success answer as everyone else and is stored
  nowhere, so the bot learns nothing from the response.
- A per-IP fixed-window rate limit, on the same validated client address the
  demo limiter uses. Spoofable, and accepted as such: the cost of a burst
  here is bounded rows in Postgres, not money.

Messages land in Postgres first and always. Forwarding by email is best
effort on top; see services/contact_forward.py.
"""

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.deps import DbSession
from app.models import ContactMessage
from app.rate_limit import ContactRateLimit
from app.services import contact_forward

log = structlog.get_logger()

router = APIRouter(prefix="/contact")

MAX_MESSAGE_CHARS = 5000
MAX_FIELD_CHARS = 200


class ContactRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    name: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    email: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    subject: str | None = Field(default=None, max_length=MAX_FIELD_CHARS)
    # The honeypot. Deliberately unconstrained: refusing an oversized value
    # with a 422 that names this field would tell a bot which input gave it
    # away, and the request body as a whole is already size-bounded upstream.
    website: str = ""

    @field_validator("message", mode="before")
    @classmethod
    def _strip_message(cls, value: object) -> object:
        # Stripped before validation so min_length refuses a message that
        # was only whitespace
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", "email", "subject", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


@router.post("")
def create_contact_message(
    body: ContactRequest, _limit: ContactRateLimit, db: DbSession
) -> dict[str, Any]:
    if body.website:
        # Same answer as success, stored nowhere. Logged without any of the
        # submitted content: it is bot output, not something to keep.
        log.info("contact_honeypot_dropped")
        return {"ok": True}
    row = ContactMessage(
        name=body.name, email=body.email, subject=body.subject, message=body.message
    )
    db.add(row)
    db.commit()
    # After the commit on purpose: the sender's message is safe before any
    # email is attempted, and a forwarding failure cannot fail the request
    contact_forward.forward(db, row)
    return {"ok": True}
