# Architecture

This document is the shape of the system. The decisions that produced that
shape, with the alternatives that lost and the costs accepted, are recorded
separately as ADRs in [adr/](adr/):

- [0001](adr/0001-queue-redis-dispatch-postgres-truth.md): Redis dispatches, Postgres is the truth
- [0002](adr/0002-github-oauth-empty-scope.md): GitHub OAuth with an empty scope
- [0003](adr/0003-session-via-next-rewrite-proxy.md): The browser only ever talks to one origin
- [0004](adr/0004-provider-abstraction-and-output-validation.md): Treat the AI provider and its output as untrusted
- [0005](adr/0005-repository-snapshot-reviews.md): A repository snapshot is a review target, not a synthetic pull request
- [0006](adr/0006-snapshot-precision.md): Precision at repository scale is a filter problem, not a tuning problem

## Requirements

Functional:

- Sign in with GitHub OAuth (read-only, public repos)
- Show a complete review to a visitor with no account at all (the public demo)
- List repositories and open PRs, pick one, run a review
- Review a whole repository at its default branch head, no pull request
  needed (post-sprint, ADR 0005)
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

## What changes for a repository review

A repository snapshot review (post-sprint, ADR 0005) is the same queue, the same job state
machine, and the same pipeline with a few steps swapped:

1. Creation pins the default branch's current head instead of a PR's pair of SHAs. There is no
   base commit: `ck_reviews_one_target` makes every review target exactly one of a pull request
   or a repository, and `ck_reviews_pr_has_base` lets only repository reviews omit a base. An
   empty repository or a vanished default branch is refused as a 409, not a 500. A second partial
   unique index, `uq_reviews_repo_sha_live`, permits one live review per user per (repository,
   commit); it exists for correctness, not symmetry, because Postgres treats NULLs as distinct in
   unique indexes and the PR index fails open once `pull_request_id` is nullable. Both live
   indexes carry `user_id` as of migration 0008; the storage section below says why.
2. Step 5 becomes one tarball instead of per-file contents calls: the worker streams the
   repository archive at the pinned SHA (100MB download ceiling), then extracts it defensively:
   regular files only, symlinks skipped, path-escape entries dropped, vendored trees and `.git`
   never written, hard ceilings of 20,000 files / 200MB extracted / 200,000 members. Past a
   ceiling the review refuses honestly rather than reviewing a truncated tree. The tarball is
   deleted before analysis.
3. There is no diff to parse. The pipeline builds a snapshot index instead: one entry per file
   flagged all-changed, which the validation chain honors directly, so the analyzers and the
   citation checks run unchanged without a repo-sized diff string in memory. The missing-tests
   heuristic is dropped (with every line counted as changed it would flag every repository
   without a test file), and analyzer timeouts are raised for repository scale.
4. The AI stage runs in chunks: reviewable files are rendered with line numbers and packed
   whole, in deterministic order, into chunks under a character budget. A user's own AI key runs
   up to 40 chunks, paced 7 seconds apart; without one the review gets a single chunk, so repo
   reviews cannot starve the shared free key. A failed chunk retries once, then degrades and is
   counted; chunk failures never trigger job retries, so a transient on the last chunk cannot
   re-bill a user's key for all the earlier ones.
5. What actually ran is recorded in `pipeline_version` and surfaced in the summary:
   `ai_coverage=covered/total` on every repository review, `ai_capped=keyless` when the keyless
   cap bit, `ai_chunks_failed=N` when chunks degraded, `analyzers_skipped=...` when a tool could
   not run, and `findings_truncated` (the 100-finding cap), which is now surfaced in the summary
   for both targets.

The only handle on a finished repository review is `latest_repo_review` on
`GET /repositories/{id}`; there is still no reviews list.

## The public demo

`/demo` shows a finished review to a visitor with no account, and lets them run it again. It is the same pipeline, not a recording of one: steps 2, 3, 4, 6, 8, 9, and 10 above are byte for byte the code a signed-in review runs.

Only two things differ, and both are decided in one branch in `worker/runner.py` that is entered before the GitHub token is ever looked up:

- **Step 5 does not happen.** The diff and the file contents come from `app/demo/sample/`, which ships in the image because the Dockerfile copies `app/`. (`api/tests/` does not ship, which is why the demo sample could not simply reuse the regression fixtures.) The diff is synthesized from those files at load time rather than stored beside them, so there is only one copy of the content and no way for the two to drift.
- **Step 7 replays a recorded response** instead of calling a provider, so a demo run costs nothing however often the button is pressed. The recorded candidates still go through step 8's validation chain, so three of them merge with analyzer findings into `hybrid` results through production code rather than being labelled that way.

The demo user holds no `provider_connections` row, so the demo path has no token to misuse even if that branch were somehow entered wrongly. Scope is a column: `repositories.is_demo`, with a partial unique index permitting exactly one demo repository, and the public routes query from it rather than from an id in the URL. Concurrency needs no new machinery either: `uq_reviews_pr_sha_live` already permits one live review per user per (pull request, head sha), and every demo review belongs to the single synthetic demo user, reviewing one pull request at one commit, so anonymous visitors cannot produce more than one demo job at a time no matter how the rate limiter behaves.

## Storage

Postgres on Neon. One line per table:

- `users`: GitHub identity, one row per person (including the demo user, at the reserved `github_id` 0)
- `sessions`: server-side session store backing the auth cookie
- `provider_connections`: encrypted OAuth tokens, per user per provider
- `repositories`: GitHub repos seen so far, deduped by GitHub id; `is_demo` marks the one the public demo reviews
- `user_repositories`: which user can see which repo
- `pull_requests`: PR metadata per repository
- `reviews`: one review of one target at one head SHA: a pull request (base and head) or a repository snapshot (head only); two CHECK constraints enforce exactly one target and let only repository reviews omit a base
- `review_jobs`: the job state machine (queued, running, completed, failed, cancelled) plus heartbeats
- `findings`: the product: severity, category, confidence, source, location, fingerprint, recommendation
- `feedback`: per-user, per-finding verdicts

Three partial unique indexes carry the concurrency model:

- one live (non-terminal) review per (user_id, pull_request_id, head_sha): one user never has the same snapshot under review twice at once
- one live review per (user_id, repository_id, head_sha), the repository-review sibling. It is correctness, not symmetry: Postgres treats NULLs as distinct in unique indexes, so the PR index fails open for reviews whose pull_request_id is NULL
- one live job per review: a review can be retried, never doubled

Both review indexes gained `user_id` in migration 0008 (2026-08-24). Keyed on the target alone they blocked strangers rather than duplicates: a completed review counts as live and only its owner can supersede it, so one account's finished review of a public repository or pull request refused every other account that same commit permanently. The second user got a 409 that carried no review id (a foreign review's id is deliberately withheld), naming findings they are not permitted to read (a foreign review 404s), and retrying never cleared it, because the blocking review was already terminal. On a product whose subject is public repositories, two people reviewing one commit is ordinary rather than exotic. Per-user scoping keeps everything the concurrency model actually rests on: a double click is still idempotent, and anonymous work through `/demo` is still capped at one live job, because every demo review belongs to the one demo user.

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
