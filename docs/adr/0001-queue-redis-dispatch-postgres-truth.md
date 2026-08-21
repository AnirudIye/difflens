# 0001. Redis dispatches, Postgres is the truth

Status: Accepted
Date: 2026-08-21

## Context

A review takes minutes, so it cannot run inside the request. Two constraints shaped what replaced
it. Upstash's free tier allows 10,000 Redis commands a day, so idle cost matters. And
`render.yaml` provisions one free `type: web` service: `api/start.sh` runs the worker as a 5s
restart loop inside that container while uvicorn holds the foreground and the
`healthCheckPath: /health` check. That container spins down after about 15 minutes idle
(`docs/DEPLOYMENT.md`, section 9), and `.github/workflows/keep-warm.yml` pings it only once
`API_ORIGIN` is set, so job state must survive the worker dying mid-review. Budget: 10 days, one
developer (`docs/SCOPE.md`).

## Decision

Redis is a doorbell. Postgres owns job state.

`api/app/queue.py` is 58 lines: `notify()` `LPUSH`es a job id onto `difflens:review_jobs`,
`wait_for_job()` `BRPOP`s with `BRPOP_TIMEOUT_S = 25`. `notify()` swallows `redis.RedisError` and
rings after the commit (`api/app/routers/reviews.py:83` and `:142`,
`api/app/routers/demo.py:107`, `api/app/demo/seed.py:46`). It is not the only Redis here:
`api/app/rate_limit.py` spends `INCR` plus `EXPIRE` per limited request under its own
`difflens:rl` prefix. An idle worker costs 86,400 / 25 = 3,456 commands a day against the cap, a
started review at least three more: arithmetic from constants we chose, never metered against
Upstash.

Every `review_jobs` transition is one guarded UPDATE. `claim_job` matches id, `status == "queued"`
and `run_after <= now`, then sets `running` and `attempts + 1`; no row matched means another worker
won. Later owner transitions add `locked_by == worker_id`: `record_heartbeat`,
`requeue_for_retry`, `fail_job` and `cancel_job` through `_transition_owned` in
`api/worker/jobs.py`, and `_persist_success` (`api/worker/runner.py:140`), which gates the findings
inserts so a reclaimed worker's results never land.

Recovery needs no Redis. The worker beats every `HEARTBEAT_INTERVAL_S = 10` seconds; `sweep()`
requeues a `running` job whose `heartbeat_at` is older than `STALE_AFTER_S = 120` seconds while
`attempts < max_attempts` (default 3, `api/alembic/versions/0001_initial.py`), and fails it with
`CRASH_MESSAGE` otherwise. Retries wait in `run_after` (30 seconds, doubling) rather than ringing.
With Redis unreachable, `api/worker/loop.py` falls back to `jobs.next_due_job_id` (indexed by
`ix_jobs_dequeue`), so dispatch becomes 25 second polling at zero Redis commands.

## Alternatives considered

**Celery, arq, RQ, Dramatiq.** All four make the broker the record of what work exists, putting job
state in the tier with the daily cap. Celery loses earlier: its Redis transport polls at kombu's
documented 1 second default, which alone approaches the cap. Documented defaults, not measurements.

**Postgres only, polled faster than the doorbell.** Not evaluated at the time, and the strongest
competitor. `next_due_job_id` is one indexed SELECT; polling it every 2 seconds costs roughly
43,000 cheap queries a day, zero Redis commands, and a 2 second start. We only compared Redis
against a 25 second poll, which is `BRPOP_TIMEOUT_S`, a constant we picked ourselves.
`docs/DEPLOYMENT.md:107` also measures a 32 second cold `/health`, so on a cold container the
latency argument does not hold. Redis buys a sub-second start on a warm container.

## Consequences

- **A malformed `REDIS_URL` is not survivable.** `render.yaml:37-38` promises it degrades to
  polling, but every `get_redis()` call sits outside the `except redis.RedisError` guards
  (`loop.py:73`, `reviews.py:83`, `:142`, `rate_limit.py:134`, `:188`) and `from_url` raises
  `ValueError` on an unrecognised scheme. Unreachable degrades; malformed crash-loops the worker
  and 500s review creation.
- **Retries are expensive, and interruptions consume them.** `_persist_success` is the only
  findings write and runs last, so a job that fails after the AI call re-runs everything, model
  call included. `attempts` increments at claim time and nothing drains an in-flight job, so a
  deploy or a spin-down burns one of three attempts.
- **Recovery is slow and lumpy.** Nothing acts until a heartbeat is 120 seconds stale, and
  `SWEEP_INTERVAL_S = 30` is a minimum gap, not a period: it is checked once per loop iteration,
  and the iteration blocks up to 25 seconds on `BRPOP`. An idle worker sweeps about every 50
  seconds; a busy one not at all. The due select has no LIMIT or dedupe, so each completion
  re-rings the backlog.
- **Only `uq_reviews_pr_sha_live` is translated.** `insert_review` catches `IntegrityError` around
  the `Review` flush and raises `ReviewAlreadyExists`, which routers turn into 409. The job insert
  sits outside that handler and `api/app/main.py` registers none, so a
  `uq_jobs_one_live_per_review` violation surfaces as a 500.
- **Postgres is metered too, and this decision never counted it.** A beat every 10 seconds during a
  review and a sweep about every 50 seconds idle keep Neon's compute awake;
  `docs/DEPLOYMENT.md:131` notes it otherwise autosuspends.

The guarded claim would already take a second worker; only the single container prevents it.
