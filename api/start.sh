#!/bin/sh
# Container entrypoint: migrate, then serve. Day 5 adds the supervised
# worker process to this script (API and worker share one free-tier container).
set -e

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
