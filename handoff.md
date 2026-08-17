# DiffLens handoff

State of the project as of 2026-08-17, end of Day 4 of a 10-day build. This file follows the house handoff convention: every claim is labeled with how it was verified, because handoff docs drift and unverified claims in them have burned this household before. Trust the labels, not the prose. Labels: [ran] means executed and observed this session, [read] means confirmed by reading the current code, [assumed] means believed but not re-checked.

## What this is

An AI code review platform for GitHub pull requests. Deterministic analyzers plus an AI reviewer (not yet built) over an async pipeline, findings pinned to exact files and lines. The build plan compresses a 40-68 day spec into 10 days; the authoritative day-by-day plan lives outside the repo, but docs/SCOPE.md carries the frozen scope and descope ladder.

## Current state

- Live in production [ran]: https://difflens-zeta.vercel.app (Next.js on Vercel) proxying /api/backend/* to https://difflens-api.onrender.com (FastAPI on Render free, Docker) backed by Neon Postgres. A real GitHub login, repository sync, and PR listing were exercised in production and the resulting rows inspected in Neon.
- 78 tests pass, ruff and format clean [ran]. CI (GitHub Actions) runs lint, pyright, pytest against Postgres and Redis service containers, plus frontend lint, typecheck, and build.
- The deterministic analysis pipeline exists and is byte-stable [ran]: run_review() in api/app/analysis/pipeline.py takes a diff plus a workspace of head-state files and returns findings. Two consecutive runs produce identical output; golden fixtures under api/tests/fixtures assert exact rule ids and line numbers, and each expected finding was hand-audited against the fixture source.
- Auth is GitHub OAuth with a deliberately empty scope (public read-only) [read]. Tokens are Fernet-encrypted at rest [ran: inspected ciphertext in both dev Postgres and Neon]. Sessions are server-side rows, opaque cookie, 7-day expiry [read].
- The GitHub integration syncs repositories and open PRs with per-user ownership checks; IDOR cases are tested (foreign resource indistinguishable from nonexistent) [ran, tests].

## What does not exist yet

- No queue, no worker, no reviews API. review_jobs and reviews tables exist in the schema [read] but nothing writes to them. Day 5 builds the Redis-dispatch queue (BRPOP, Postgres rows as job-state truth, sweep for recovery) per docs/architecture.md.
- No AI layer. api/app/analysis/pipeline.py raises NotImplementedError for modes other than deterministic_only [read]. Day 6 adds the Anthropic provider, mock provider, prompt with nonce-delimited untrusted content, and the hallucination validation chain.
- No review UI beyond repo and PR listing. The Run review buttons render disabled [read].
- REDIS_URL in production is a placeholder [read: render env]; Upstash arrives Day 5.
- No rate limiting, no threat model doc, no demo mode. Days 8-9.

## Architecture facts a newcomer needs

- The analysis package (api/app/analysis/) is pure: no DB, no network, no GitHub at analysis time [read]. The worker (Day 5) will fetch the diff and head files by pinned SHAs, build a workspace, and call run_review(). Immutability comes from fetching via compare on base_sha...head_sha, never branch names [read: github_client.compare].
- One review per (pull_request_id, head_sha) is enforced by a partial unique index, not application logic [read: migration 0001]. Same pattern for one live job per review.
- The frontend never talks to Render directly. Next.js rewrites /api/backend/* server-side, so cookies are first-party and CORS does not exist anywhere [read: web/next.config.ts]. This is also why the OAuth callback is on the Vercel origin.
- One GitHub OAuth app serves both dev and prod. GitHub always accepts localhost redirect URIs for OAuth apps regardless of the registered callback (loopback exception) [ran: probed both redirect_uris against the authorize endpoint]. The registered callback is the production one. The app holds two client secrets, one per environment.
- Fingerprints hash tool, rule, path, normalized anchor-line content, and an occurrence index, never line numbers, so re-reviews survive rebases [read: dedup.py, tests].

## Local development quirks (primary dev machine)

- Postgres publishes on host port 55432, not 5432 [read: docker-compose.yml]. A native PostgreSQL 18 Windows service owns 5432 on the primary machine and wins boot races.
- The API dev process runs from the repo root with --app-dir api so it finds the root .env [read: run-api.cmd]. run-api.cmd and run-web.cmd launch both halves.
- uv is broken on the primary machine (cross-device rename failures in its installer and cache); local Python is a plain venv at api/.venv on CPython 3.12 via py -3.12 [ran]. CI uses uv on Ubuntu where it works fine, without a lockfile so far.
- Docker Desktop lives on the A: drive with WSL data at A:\Docker\wsl [ran]. When an automation sandbox is in play, launch Docker Desktop through explorer.exe, never directly from a sandboxed shell: sandboxed launches poison its unix-socket cleanup and it crash-loops (this cost two days; see the socket files under AppData/Local/Docker if it ever recurs).
- Line endings are pinned by .gitattributes: sh is LF (the Docker image depends on it), cmd and ps1 are CRLF [read].

## Deployment

docs/DEPLOYMENT.md is the runbook and was executed for real this session [ran]. Key facts: Render service difflens-api (Docker, oregon, free plan, health check /health, migrations run in start.sh on every deploy), Vercel project difflens with rootDirectory web and API_ORIGIN env, Neon project difflens on the pooled connection string with the postgresql+psycopg scheme. Render and Vercel both auto-deploy on push to main [ran: observed]. Render free spins down after idle; first request takes 30-60s.

## Where Day 5 starts

Queue module (api/app/queue.py), worker entrypoint (api/worker/), Upstash Redis provisioning, wiring POST /reviews to enqueue, the runner that fetches by pinned SHAs and calls run_review(), retry with backoff, the stale-job sweep, and cancellation. The schema is already there. docs/architecture.md section on queue design is the spec, including the BRPOP command-budget math that motivated the design.

Last updated: 2026-08-17
