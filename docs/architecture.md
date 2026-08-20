# Architecture

## Requirements

Functional:

- Sign in with GitHub OAuth (read-only, public repos)
- Show a complete review to a visitor with no account at all (the public demo)
- List repositories and open PRs, pick one, run a review
- Fetch the diff pinned to immutable base/head SHAs
- Run deterministic analyzers and an AI reviewer asynchronously
- Validate AI citations against the reviewed snapshot, dedupe, persist findings
- Render findings with severity, category, confidence, source, recommendation; collect feedback

Non-functional:

- $0 infrastructure. Free tiers only, and their limits actively shape the design (see queue section).
- Single developer, 10 days. Boring technology, few moving parts, nothing that needs babysitting.
- Reviews take minutes, not seconds. The UI polls and says so; no fake progress.

## High-level design

```
  Browser
     |
     v
  Vercel: Next.js 15 + TypeScript
     |
     |  /api/* rewrite proxy (same-origin cookies, no CORS dance)
     v
  Render free tier: one service, two processes under a supervisor
  +--------------------------------+
  |  FastAPI API (Python 3.12)     |-----> Neon: Postgres (source of truth)
  |                                |-----> Upstash: Redis (dispatch queue)
  |  Worker (same container)       |<----- BRPOP, 25s blocking
  +--------------------------------+
          |                |
          v                v
     GitHub API      AI provider (Gemini or Anthropic behind one abstraction;
                     mock by default; a user's own key from Settings wins)
```

## Data flow for one review

1. User clicks Run Review on a PR. The request is counted against a per-user rate limit first, because it is the only call in the product that spends GitHub quota, worker time, and AI tokens. The API then resolves the PR's current base and head SHAs and pins them. The review describes exactly that snapshot forever, even if the branch moves.
2. The API inserts a `reviews` row and a `review_jobs` row (status `queued`) in one transaction, then pushes the job id to Redis. Postgres is the truth; Redis is a doorbell.
3. The worker wakes from BRPOP (25 second blocking pop), claims the job with an atomic status transition to `running`, and starts heartbeating.
4. A periodic sweep re-queues jobs whose heartbeat has gone stale. A dropped Redis message or a crashed worker delays a review; it never loses one.
5. The worker fetches the diff and the file contents it needs from GitHub at the pinned SHAs.
6. Deterministic analyzers run in parallel, isolated from each other so one hung tool costs only its own findings: ruff for Python, ESLint for TypeScript and JavaScript, detect-secrets, and the missing-tests heuristic. Both linters ignore the reviewed repository's own configuration (`--isolated` and `--no-config-lookup`), since a lint config can load plugins and a plugin is executable code from a stranger.
7. The AI reviewer runs through the provider abstraction (mock by default) and its output is parsed into candidate findings.
8. Validation chain: every AI-cited file path and line number is checked against the reviewed snapshot. Citations that do not resolve are discarded and counted, never shown.
9. Findings from all sources are fingerprinted on content, deduped, and persisted.
10. The job transitions to `completed` (or `failed` with error detail, or `cancelled` if the user asked it to stop). The UI, polling review status, renders the findings.

## The public demo

`/demo` shows a finished review to a visitor with no account, and lets them run it again. It is the same pipeline, not a recording of one: steps 2, 3, 4, 6, 8, 9, and 10 above are byte for byte the code a signed-in review runs.

Only two things differ, and both are decided in one branch in `worker/runner.py` that is entered before the GitHub token is ever looked up:

- **Step 5 does not happen.** The diff and the file contents come from `app/demo/sample/`, which ships in the image because the Dockerfile copies `app/`. (`api/tests/` does not ship, which is why the demo sample could not simply reuse the regression fixtures.) The diff is synthesized from those files at load time rather than stored beside them, so there is only one copy of the content and no way for the two to drift.
- **Step 7 replays a recorded response** instead of calling a provider, so a demo run costs nothing however often the button is pressed. The recorded candidates still go through step 8's validation chain, so three of them merge with analyzer findings into `hybrid` results through production code rather than being labelled that way.

The demo user holds no `provider_connections` row, so the demo path has no token to misuse even if that branch were somehow entered wrongly. Scope is a column: `repositories.is_demo`, with a partial unique index permitting exactly one demo repository, and the public routes query from it rather than from an id in the URL. Concurrency needs no new machinery either: `uq_reviews_pr_sha_live` already permits one live review per (pull request, head sha), and the demo is one pull request at one commit, so anonymous visitors cannot produce more than one demo job at a time no matter how the rate limiter behaves.

## Storage

Postgres on Neon. One line per table:

- `users`: GitHub identity, one row per person (including the demo user, at the reserved `github_id` 0)
- `sessions`: server-side session store backing the auth cookie
- `provider_connections`: encrypted OAuth tokens, per user per provider
- `repositories`: GitHub repos seen so far, deduped by GitHub id; `is_demo` marks the one the public demo reviews
- `user_repositories`: which user can see which repo
- `pull_requests`: PR metadata per repository
- `reviews`: one review of one PR at one head SHA
- `review_jobs`: the job state machine (queued, running, completed, failed, cancelled) plus heartbeats
- `findings`: the product: severity, category, confidence, source, location, fingerprint, recommendation
- `feedback`: per-user, per-finding verdicts

Two partial unique indexes carry the concurrency model:

- one live (non-terminal) review per (pull_request_id, head_sha): the same snapshot is never reviewed twice concurrently
- one live job per review: a review can be retried, never doubled

## Queue design and trade-offs

Redis (Upstash) does dispatch only; `review_jobs` in Postgres is the source of truth for job state. The worker blocks on BRPOP with a 25 second timeout, so a fully idle worker costs about 86,400 / 25 = 3,456 Redis commands per day, roughly 3.5k worst case against Upstash's 10k/day free limit, leaving headroom for actual work.

Why not Celery or arq: both treat the broker as the truth and both hold connections or poll in ways that burn the Upstash command budget while idle. The BRPOP loop plus the Postgres state machine is under a hundred lines, and the stale-heartbeat sweep gives crash recovery that small Celery deployments usually skip anyway.

## Key trade-offs

| Choice | What it costs | Why it wins here |
|---|---|---|
| Worker co-located with the API on one Render service | CPU contention during reviews | A second free-tier service would sleep; one supervised instance stays warm for both roles |
| Empty OAuth scope, public read only | No private repos in v1 | The alternative (`repo` scope) grants write access; wrong posture, and a GitHub App is post-sprint |
| Polling UI, not websockets | Findings appear seconds late | Free tiers and a 10-day budget; polling is stateless and debuggable |
| Mock AI provider as the default | Real reviews need a flag flip and a key | CI and local dev run free, offline, and deterministic |
| Per-user AI keys (encrypted rows, not a vault) | Key rotation invalidates stored keys; users re-save | Fernet at rest with the existing TOKEN_ENCRYPTION_KEY, no new infrastructure at $0 |

## What we would revisit at scale

- Split the worker into its own service and scale it horizontally
- GitHub App instead of OAuth: finer permissions, private repos, webhooks
- Websockets or SSE for live review progress
- A real metrics stack instead of structured logs alone
