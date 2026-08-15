# DiffLens

DiffLens is an AI code review platform for GitHub pull requests. It pairs deterministic static analysis (ruff, detect-secrets, a missing-tests heuristic) with an AI reviewer built on Anthropic Claude, then verifies every file and line the AI cites against the exact commit it reviewed, so hallucinated findings never reach you. Sign in with GitHub OAuth (read-only, public repos), pick a pull request, and an async pipeline built on FastAPI and Next.js fetches the diff, runs the analyzers, dedupes the results, and renders findings with severity, category, confidence, and a concrete recommendation.

## What it does

- Signs in with GitHub OAuth using a deliberately empty scope: read-only, public repos only. DiffLens never asks for write access.
- Pins every review to immutable base and head SHAs, so the review describes one exact snapshot even if the branch moves afterward.
- Runs deterministic analyzers: ruff for Python lint, detect-secrets for leaked credentials, and a missing-tests heuristic. ESLint is a stretch goal.
- Runs an AI reviewer through a provider abstraction with a mock mode, so the whole pipeline works locally and in CI without an API key.
- Validates every AI-cited file and line against the reviewed snapshot and discards locations that do not exist. Hallucinations get filtered, not rendered.
- Treats PR content as untrusted input to the AI reviewer; prompt-injection defense is part of the pipeline design, not a patch.
- Dedupes findings across analyzers and the AI with content-based fingerprints.
- Persists each finding with severity, category, confidence, source, and recommendation, and lets you mark findings as helpful or wrong.

## Architecture

```mermaid
flowchart LR
    B[Browser] --> V["Next.js on Vercel"]
    V -->|"/api/* rewrite proxy"| A["FastAPI on Render"]
    A --> P[("Postgres on Neon")]
    A --> R[("Redis on Upstash")]
    R -->|BRPOP dispatch| W["Worker, co-located process"]
    W --> P
    W --> G["GitHub API"]
    W --> AN["Analyzer pipeline: ruff, detect-secrets, missing-tests"]
    W --> C["Claude via Anthropic API"]
```

Everything runs on free tiers: Vercel for the Next.js 15 frontend, Render for the FastAPI API with the worker supervised in the same container, Neon for Postgres, Upstash for the Redis dispatch queue. Postgres holds job state as the source of truth; Redis is just the doorbell. Total infrastructure cost: $0.

## Status

Day 1 of a 10-day build.

- [x] Day 1: repo scaffold, CI, scope and architecture docs
- [ ] Day 2: database schema and GitHub OAuth
- [ ] Day 3: GitHub integration and first deploy
- [ ] Day 4: deterministic analyzers
- [ ] Day 5: worker and queue (descope checkpoint)
- [ ] Day 6: AI review layer
- [ ] Day 7: review UI
- [ ] Day 8: tests, security pass, threat model
- [ ] Day 9: demo mode and hardening
- [ ] Day 10: portfolio polish

## Local development

Prerequisites: Docker Desktop, Node 20+, and [uv](https://docs.astral.sh/uv/).

First, copy the env template and fill in what you need (mock AI mode needs no keys):

```
copy .env.example .env
```

Start Postgres and Redis:

```
docker compose up -d
```

Run the API:

```
cd api
uv sync
uv run uvicorn app.main:app --reload
```

Run the frontend in a second terminal:

```
cd web
npm install
npm run dev
```

The databases live in Docker; the API and web app run natively for fast reloads. `docker compose --profile full up -d` additionally builds and runs the API in a container if you want to exercise that path.

## License

MIT. See [LICENSE](LICENSE).

Last updated: 2026-08-15
