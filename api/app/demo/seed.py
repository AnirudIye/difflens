"""Seed the demo rows. Run from start.sh on every boot; a no-op once seeded.

A one-off command would be forgotten, and Render rebuilds the container on
every deploy, so seeding runs where migrations already run and guards itself
instead of relying on anyone remembering. It is deliberately NOT a FastAPI
startup hook: writes on the request path's startup are harder to reason
about, and a failure there takes the health check down with it.

Run directly with:

    python -m app.demo.seed
"""

import sys

import structlog

from app import queue
from app.config import settings
from app.db import SessionLocal
from app.demo import service
from app.logging_setup import setup_logging

log = structlog.get_logger()


def main() -> int:
    setup_logging(settings.environment)
    if not settings.demo_mode:
        log.info("demo_seed_skipped", reason="DEMO_MODE is off")
        return 0

    db = SessionLocal()
    try:
        created = service.seed(db)
    finally:
        db.close()

    if created is None:
        log.info("demo_seed_already_present")
        return 0

    _review, job = created
    # Best effort, like every other doorbell: the worker's sweep picks up a
    # queued job whose ring was lost.
    queue.notify(queue.get_redis(), job.id)
    log.info("demo_seeded", job_id=str(job.id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
