"""Per-finding feedback: one verdict per user per finding, upserted.

The unique constraint on (finding_id, user_id) makes the upsert the whole
concurrency story; racing saves resolve by commit order (the UI serializes
per-finding clicks, so a user cannot race themselves).
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.deps import CurrentUser, DbSession
from app.models import Feedback, Finding, Review
from app.routers.github_errors import not_found

router = APIRouter(prefix="/findings")

MISSING = "Finding not found"


class PutFeedbackRequest(BaseModel):
    verdict: Literal["useful", "not_useful", "dismissed"]


def _require_owned_finding(db: DbSession, user_id: UUID, finding_id: UUID) -> None:
    row = db.execute(
        select(Finding.id)
        .join(Review, Review.id == Finding.review_id)
        .where(Finding.id == finding_id, Review.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        # A foreign finding must look exactly like one that never existed
        raise not_found(MISSING)


@router.put("/{finding_id}/feedback")
def put_feedback(
    finding_id: UUID, body: PutFeedbackRequest, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    _require_owned_finding(db, user.id, finding_id)
    stmt = pg_insert(Feedback).values(finding_id=finding_id, user_id=user.id, verdict=body.verdict)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_feedback_finding_id_user_id",
        set_={"verdict": stmt.excluded.verdict, "updated_at": func.now()},
    )
    db.execute(stmt)
    db.commit()
    return {"verdict": body.verdict}


@router.delete("/{finding_id}/feedback")
def delete_feedback(finding_id: UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _require_owned_finding(db, user.id, finding_id)
    db.execute(
        delete(Feedback).where(Feedback.finding_id == finding_id, Feedback.user_id == user.id)
    )
    db.commit()
    return {"verdict": None}
