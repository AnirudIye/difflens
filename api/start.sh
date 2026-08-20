#!/bin/sh
# Container entrypoint: migrate, then serve. One free-tier container carries
# both processes: the worker rides behind the API under a restart loop while
# uvicorn owns the foreground and the health check. If the worker dies it
# restarts in 5s; if Redis is unreachable it degrades to sweep-polling
# Postgres, so reviews still run, just up to 25s later.
set -e

alembic upgrade head

# Idempotent, and a no-op unless DEMO_MODE is on. Runs here rather than as an
# app startup hook so a failure cannot take the health check with it, and is
# explicitly non-fatal despite set -e: the demo is a nice-to-have and must
# never be the reason the API fails to boot.
python -m app.demo.seed || echo "demo seed failed, continuing without it" >&2

(
  while true; do
    python -m worker || echo "worker exited ($?), restarting in 5s" >&2
    sleep 5
  done
) &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
