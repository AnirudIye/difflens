#!/bin/sh
# Container entrypoint: migrate, then serve. One free-tier container carries
# both processes: the worker rides behind the API under a restart loop while
# uvicorn owns the foreground and the health check. If the worker dies it
# restarts in 5s; if Redis is unreachable it degrades to sweep-polling
# Postgres, so reviews still run, just up to 25s later.
set -e

alembic upgrade head

(
  while true; do
    python -m worker || echo "worker exited ($?), restarting in 5s" >&2
    sleep 5
  done
) &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
