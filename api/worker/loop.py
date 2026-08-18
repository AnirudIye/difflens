"""The worker's main loop: wait on the doorbell, claim, run, sweep, repeat.

Redis being down only slows this loop, never stops it: wait_for_job degrades
to a sleep, and whenever the doorbell yields nothing the loop asks Postgres
directly for the oldest due job (jobs.next_due_job_id), so dispatch becomes
25s-latency polling instead of stopping. One BRPOP per 25s idle keeps the
daily command count near 3.5k, inside Upstash's free 10k
(docs/architecture.md); the fallback costs zero Redis commands.
"""

import os
import signal
import socket
import threading
import time
import uuid

import structlog

from app import queue
from app.config import settings
from app.db import SessionLocal
from app.logging_setup import setup_logging
from worker import jobs, runner

log = structlog.get_logger()


def _heartbeat_loop(job_id: uuid.UUID, worker_id: str, stop: threading.Event) -> None:
    """Beat every HEARTBEAT_INTERVAL_S on its own session until told to stop.

    The claim always precedes this thread, so a beat that finds the row no
    longer ours can only mean the sweep reclaimed the job; stop quietly. The
    runner's checkpoints and ownership fences make the reclaimed run's
    writes no-ops.
    """
    while not stop.wait(jobs.HEARTBEAT_INTERVAL_S):
        try:
            with SessionLocal() as db:
                if not jobs.record_heartbeat(db, job_id, worker_id):
                    return
        except Exception:  # a DB blip must not kill the beat; next tick retries
            log.warning("heartbeat_failed", job_id=str(job_id))


def _run_one(job_id: uuid.UUID, worker_id: str) -> None:
    with SessionLocal() as db:
        # Claim BEFORE starting the heartbeat thread: a beat can then never
        # observe (and bail on) a job whose claim has not landed yet
        job = jobs.claim_job(db, job_id, worker_id)
        if job is None:
            return
        stop_beat = threading.Event()
        beat = threading.Thread(
            target=_heartbeat_loop, args=(job.id, worker_id, stop_beat), daemon=True
        )
        beat.start()
        try:
            outcome = runner.run_claimed_job(db, job, worker_id)
            log.info("job_processed", job_id=str(job_id), outcome=outcome)
        except Exception:
            # The runner's own handling could not run: leave the row alone,
            # the stale-heartbeat sweep will retry or fail it honestly
            log.exception("job_crashed", job_id=str(job_id))
        finally:
            stop_beat.set()
            beat.join(timeout=jobs.HEARTBEAT_INTERVAL_S + 5)


def main() -> None:
    setup_logging(settings.environment)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    redis_client = queue.get_redis()
    stopping = threading.Event()

    def _handle_term(_signum, _frame) -> None:
        log.info("worker_stopping", worker_id=worker_id)
        stopping.set()

    signal.signal(signal.SIGINT, _handle_term)
    signal.signal(signal.SIGTERM, _handle_term)

    log.info("worker_started", worker_id=worker_id)
    last_sweep = 0.0
    while not stopping.is_set():
        if time.monotonic() - last_sweep >= jobs.SWEEP_INTERVAL_S:
            try:
                with SessionLocal() as db:
                    jobs.sweep(db, redis_client)
            except Exception:
                log.exception("sweep_failed")
            last_sweep = time.monotonic()

        popped = queue.wait_for_job(redis_client, timeout_s=queue.BRPOP_TIMEOUT_S)
        if stopping.is_set():
            break
        if popped is None:
            # Doorbell silent (timeout or Redis down): ask Postgres directly.
            # This is the degraded-mode dispatch path the docs promise; the
            # claim's UPDATE guard keeps it safe against racing workers.
            try:
                with SessionLocal() as db:
                    due = jobs.next_due_job_id(db)
            except Exception:
                log.exception("due_poll_failed")
                continue
            if due is not None:
                _run_one(due, worker_id)
            continue
        try:
            job_id = uuid.UUID(popped)
        except ValueError:
            log.warning("queue_garbage_dropped", payload=popped[:64])
            continue
        _run_one(job_id, worker_id)

    log.info("worker_stopped", worker_id=worker_id)
