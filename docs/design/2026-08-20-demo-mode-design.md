# Demo mode design (Day 9)

Status: proposed, not approved, not implemented.
Date: 2026-08-20.

Day 9 of the sprint is demo mode plus production hardening. Gate 9 in
`docs/SCOPE.md`: an incognito visitor reaches the demo URL and reads findings
in under 60 seconds including cold start, and the deployment is reproducible
from the runbook alone.

## The problem

A visitor with no GitHub account has to see a real review. Two facts from the
current code decide the whole shape of this, and both contradict the original
plan:

1. **`tests/` is not in the production image.** `api/Dockerfile` copies
   `pyproject.toml`, `app/`, `worker/`, `alembic.ini`, `alembic/`, `start.sh`
   and the eslint runtime. Nothing else. The plan says the seed script reuses
   the regression fixtures under `api/tests/fixtures/`. In production those
   files do not exist.
2. **The worker requires a GitHub token.** `run_claimed_job` loads a
   `ProviderConnection` and fails with `RECONNECT_MESSAGE` when there is none,
   then fetches every diff and every blob from GitHub. An anonymous visitor
   has no token, so the demo needs its own source for the diff.

A third fact decides the honesty of the result:

3. **`MockProvider()` returns zero findings**, and `_ai_note()` in
   `app/analysis/pipeline.py` answers a mock model with the fixed sentence
   "No AI reviewer is configured, so only deterministic checks ran." A demo
   built literally to the plan either shows deterministic findings only, or
   shows that sentence while displaying AI findings, which would be false.
   The plan's "8 to 12 findings including one hybrid" does not come for free.

## Approach chosen

Bundle the sample inside `app/` so it ships in the image, and give the worker
one fenced demo branch that reads it. Rejected alternatives are recorded at
the bottom.

### Identity: what makes a row a demo row

Migration **0005** adds `repositories.is_demo boolean not null default false`
with a partial unique index:

```sql
create unique index uq_repositories_single_demo
    on repositories (is_demo) where is_demo;
```

Only rows with `is_demo` true are indexed and they all carry the same value,
so the index permits exactly one demo repository. Everything else derives from
that one row: the demo pull request is the one whose repository is the demo
repository, the demo reviews are the ones whose pull request is that one, and
the demo findings hang off those reviews.

The demo user is a `users` row with `github_id = 0` (real GitHub ids start at
1, so the sentinel can never collide) and login `difflens-demo`. It has **no
`ProviderConnection` row at all**. That is the point: the demo user is
structurally incapable of making a GitHub call, rather than merely not making
one. There is no token to leak, revoke, or spend quota against.

### The bundled sample

```
api/app/demo/sample/
    meta.json     repo full_name, PR number, title, author, base_sha, head_sha, html_url
    pr.diff       the unified diff, byte for byte what the parser will see
    files/        head state of every changed file, for the analyzer workspace
```

It lives under `app/` because that is what the Dockerfile copies. It carries a
Python file and a TypeScript file so that ruff, detect-secrets, TestDetector
and ESLint all contribute, which is what makes the finding list look like a
real review rather than one tool's output.

The sample is loaded by a small module, `app/demo/sample.py`, exposing the
diff text and a function that materializes `files/` into a temporary
workspace. The analysis package stays pure: it receives a workspace path
exactly as it does today and cannot tell the difference.

### The worker's demo branch

In `run_claimed_job`, after the repository is loaded and **before** the
`ProviderConnection` lookup:

```
if repository.is_demo:
    diff_text, workspace = demo sample
    provider = MockProvider(candidates=DEMO_CANDIDATES, model="demo")
else:
    existing GitHub path, unchanged
```

Placing the branch ahead of the token lookup is what guarantees the demo path
never reaches GitHub. Everything downstream is the existing code: the same
temp workspace, the same `run_review`, the same persistence, the same
ownership fences, the same cancellation checkpoints.

### Honest AI labelling

`MockProvider` gains a `model` parameter defaulting to `"mock"`, so every
existing test and every existing behaviour is untouched. The demo passes
`model="demo"` with canned candidates.

Those candidates are hand written, and they are genuinely correct bugs in the
sample. They are not injected into the output: they enter the pipeline as
provider candidates and go through the real chain, which is
`validate_candidates` against the diff index, then `dedup`, then the hybrid
merge. A candidate landing within `MERGE_LINE_PAD` of a deterministic finding
in a mergeable category really does become a `hybrid` finding, produced by
production code rather than staged. That is how the plan's "one hybrid" is
earned honestly.

`_ai_note()` gets a `demo` branch ahead of the `mock` branch, saying that the
AI stage replays a recorded reviewer response so the demo stays free and
identical on every run. The existing `mock` sentence stays exactly as it is,
because it is still true wherever a real mock runs.

The review page needs matching copy: Day 8 made `ai_model == "mock"` render a
notice pointing at Settings, which is wrong advice for a visitor who is not
signed in and has no Settings to reach.

### Abuse surface

The demo is an unauthenticated button that enqueues work, so it deserves a
real answer rather than a rate limit and a hope.

The important property is already in the schema. The partial unique index
`uq_reviews_pr_sha_live` allows one live review per (pull request, head sha)
across the statuses queued, running and completed. The demo is one pull
request at one head sha. Therefore **at most one demo job can be queued or
running at any instant, and Postgres enforces it.** Spamming the button
produces 409 `review_already_exists`, not a job flood. That holds during a
Redis outage, which matters because `rate_limit.check` deliberately fails
open: without the index, failing open on an anonymous endpoint would mean
unlimited anonymous job creation exactly when the system is already unhealthy.

Given that floor, per-IP limiting is fairness rather than safety: it stops one
visitor monopolizing the single demo slot. It reuses `rate_limit.check`, which
already accepts an arbitrary identity string, with a new `Limit` and the
client IP as identity.

The client IP arrives through Vercel's rewrite proxy, so it comes from
`X-Forwarded-For` and is therefore spoofable by anyone who wants to be. This
is acknowledged rather than solved: spoofing buys a share of one slot, which
the index caps anyway, so the limiter never has to be trustworthy for the
system to be safe. That reasoning belongs in `docs/THREAT_MODEL.md` as an
accepted gap, in the same voice as the existing fixed-window and fail-open
entries.

Storage growth is one superseded review row per completed re-run, and a
re-run cannot start until the previous one finishes. Old superseded demo
reviews are pruned to the most recent few, so the demo cannot grow the free
Neon tier without bound.

### Routes

`/api/demo/*` is public, read-only, and scoped to demo rows by construction:
every query joins through the demo repository, so there is no code path by
which a demo route can return a real user's data.

- `GET /api/demo/review` returns the current demo review with its findings.
- `POST /api/demo/review/rerun` supersedes and enqueues, IP limited.

When `DEMO_MODE` is off, every demo route answers **404**, not 403, so the
surface is invisible rather than merely closed.

`GET /api/demo/review` returning "the current demo review" rather than one
pinned by id is deliberate: `/demo` then never carries a review id in its URL,
so the link stays shareable and always shows the latest run. The page polls
the same URL across a re-run instead of chasing a new id.

`tests/test_authz.py` carries a completeness guard that fails when a route
joins the app unclassified. The demo routes must be classified there as
public, which is the guard doing its job rather than an obstacle.

### Configuration

`DEMO_MODE` defaults to off. It is currently missing from `.env.example` and
from `render.yaml`, both of which the deployment runbook already claims list
it. It must be `sync: false` in `render.yaml` like every other operator
managed variable, for the blueprint reason that cost a day on Day 8.

## The rest of Day 9

- **Keep-warm cron.** A GitHub Actions schedule every 10 minutes curling
  `/health`. Free for public repositories. This is what makes the under 60
  seconds gate reliably passable, since it removes the 30 to 60 second Render
  cold start from the visitor's path. It keeps Render awake and therefore the
  worker's BRPOP connected; it does not keep Neon awake, which wakes in about
  a second on demand and is invisible behind Render.
- **Walk `docs/DEPLOYMENT.md` against reality** and fix drift. `DEMO_MODE` is
  known drift. The runbook is graded by gate 9, which asks that deployment be
  reproducible from the runbook alone.
- **One Playwright end to end test on the demo path only.** Never the OAuth
  path. The demo path is deterministic by construction, which is what makes it
  the only honest candidate for an end to end test in this repo.
- **Adversarial multi-agent review** over the finished diff before merge, the
  method that caught the argv injection on Day 8.

## Testing

- Migration 0005 up and down, including that the partial index rejects a
  second demo repository.
- The sample loader: the diff parses, and the materialized workspace matches
  the paths the diff names. A drift between `pr.diff` and `files/` is the
  failure mode that would quietly produce a demo with no findings.
- A golden expectation for the demo review, the same shape as the existing
  fixtures, pinning the finding count, the severity split, and the presence of
  at least one `hybrid`. This is the test that fails if a future analyzer
  change silently guts the demo.
- The worker demo branch: it never constructs a `GitHubClient`, and a demo
  review completes with no `ProviderConnection` present.
- Demo routes 404 when `DEMO_MODE` is off, and never return a non demo row
  when it is on.
- The IP limiter returns 429 with the server's own sentence.
- One Playwright run of the visitor path.

## Alternatives rejected

**Store the diff and blobs in Postgres.** Removes the filesystem dependency,
but puts file content inside a migration on the free Neon tier, and the seed
still has to read that content from the sample, so it adds a copy rather than
removing one.

**Seed a demo GitHub token and review the real demo PR.** Reintroduces the
exact failure the current dev environment is already in, which is a revoked
token, and spends GitHub quota on anonymous traffic.

**Canned review where the button replays an animation.** Cheapest to build and
dishonest. The plan asks specifically for a real job through the real queue,
and this codebase's posture is that degradation is visible and never silent, so
a button that pretends to run a pipeline is the first thing an adversarial pass
would kill.
