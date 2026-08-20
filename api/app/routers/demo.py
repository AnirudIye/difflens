"""The public demo: read one seeded review, and run it again. No session.

Two properties do the security work here, and neither is a check that could
be forgotten on a new endpoint:

- Scope is a column value, not an argument. Every query starts from
  `Repository.is_demo`, and no route takes an id, so there is nothing a
  caller can pass to widen what it reads. A demo route cannot return a real
  user's review because it never has one in scope.
- Concurrency is capped by the schema. `uq_reviews_pr_sha_live` permits one
  live review per pull request and commit, and the demo is one pull request
  at one commit, so at most one demo job exists at a time whatever the rate
  limiter does. That matters because the limiter fails open on a Redis
  outage, which on an anonymous endpoint would otherwise mean unlimited job
  creation exactly when the system is least healthy.

With DEMO_MODE off every route here answers 404, so the surface is invisible
rather than merely closed.
"""

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from app import queue
from app.config import settings
from app.demo import service as demo_service
from app.deps import DbSession
from app.rate_limit import DemoRateLimit
from app.routers import review_payload
from app.services import review_service

log = structlog.get_logger()

router = APIRouter(prefix="/demo")

MISSING = "The demo is not available"


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "demo_unavailable", "message": MISSING})


def require_demo_mode() -> None:
    """404 rather than 403 when the demo is off: no surface to probe."""
    if not settings.demo_mode:
        raise _not_found()


DemoEnabled = Annotated[None, Depends(require_demo_mode)]


@router.get("/review")
def get_demo_review(_gate: DemoEnabled, db: DbSession) -> dict[str, Any]:
    """The current demo review, whatever its status.

    There is no id in the path on purpose: /demo stays one shareable link
    that always shows the latest run, so a rerun does not strand anyone on
    the review it replaced.
    """
    review = demo_service.current_review(db)
    if review is None:
        raise _not_found()
    findings = review_payload.review_findings(db, review)
    job = review_service.latest_job(db, review)
    return review_payload.review_item(
        review,
        cancel_requested=job.cancel_requested if job else False,
        findings=findings,
        # Feedback is per user and the demo has no signed-in user, so every
        # verdict is null and the page renders no feedback controls.
        verdicts={},
        context=review_payload.load_pull_context(db, review),
    )


@router.post("/review/rerun", status_code=201)
def rerun_demo_review(_gate: DemoEnabled, _limit: DemoRateLimit, db: DbSession) -> dict[str, Any]:
    """Run the demo pull request again through the real queue and worker."""
    try:
        review, job = demo_service.rerun(db)
    except demo_service.DemoNotSeeded as exc:
        log.warning("demo_rerun_unseeded", error=str(exc))
        raise _not_found() from exc
    except demo_service.DemoReviewRunning:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "demo_review_running",
                "message": "A demo review is already running; watch this one finish first",
            },
        ) from None
    except review_service.ReviewAlreadyExists as exc:
        # Two visitors pressed at once and the index arbitrated. The winner's
        # review is the one to watch, and GET /demo/review returns it.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "demo_review_running",
                "message": "A demo review is already running; watch this one finish first",
            },
        ) from exc

    # After commit, so a lost doorbell can only delay the job; the worker's
    # sweep re-rings anything queued and unheard.
    queue.notify(queue.get_redis(), job.id)
    return review_payload.review_item(
        review,
        cancel_requested=False,
        findings=[],
        verdicts={},
        context=review_payload.load_pull_context(db, review),
    )
