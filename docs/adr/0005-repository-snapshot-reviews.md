# 0005. A repository snapshot is a review target, not a synthetic pull request

Status: Accepted
Date: 2026-08-24

The first post-sprint decision. `docs/SCOPE.md` froze v1 on pull requests and its change policy
held; this landed three days after Day 10, through the pipeline the sprint built.

## Context

Until now the product reviewed one shape: a pull request pinned to immutable base and head SHAs.
The obvious second ask, "review my whole repository", is the same pipeline with three of its
inputs missing: no base commit, no diff, and no compare payload naming which files to fetch. The
constraints have not moved: $0 infrastructure, the worker sharing one Render free instance (512MB
of memory) with the API, GitHub at 5,000 requests/hour on the user's empty-scope token (ADR 0002),
and the AI stage the only part that spends money per run (ADR 0004).

The public demo had already proved the load-bearing trick: content presented as all-added runs the
whole pipeline unchanged, because analyzers pick their files from the diff index and
`touches_change` then passes everywhere.

## Decision

**A review targets exactly one of two things, and the schema says so.** Migration 0006 makes
`reviews.pull_request_id` and `base_sha` nullable and adds `repository_id`.
`ck_reviews_one_target` enforces the XOR (`(pull_request_id IS NULL) != (repository_id IS NULL)`)
and `ck_reviews_pr_has_base` lets only repository reviews omit a base. `CreateReviewRequest`
(`api/app/routers/reviews.py`) repeats the XOR at the API edge, and the review payload carries
`target` plus nullable `pull_request` and `repository` blocks, so the page never infers the shape.

**The second partial unique index is correctness, not optimization.** Postgres treats NULLs as
distinct in unique indexes, so the moment `pull_request_id` goes nullable,
`uq_reviews_pr_sha_live` fails open for repo reviews: every (NULL, head_sha) row is distinct from
every other, and unlimited live reviews of one repository at one commit would insert cleanly.
`uq_reviews_repo_sha_live`, predicated on `repository_id IS NOT NULL` plus the live statuses,
closes that, and doubles as the structural cap on concurrent repo jobs: the property
`docs/THREAT_MODEL.md` already leans on for the PR index, load-bearing here because the rate
limiter fails open (gap 2). `_insert_queued` in `api/app/services/review_service.py` translates a
violation of either live index into the same 409, classifying on the constraint name Postgres
reports rather than on a second query.

**Ingestion is one tarball, and the ceilings refuse rather than truncate.** `download_tarball`
streams `/repos/{full_name}/tarball/{sha}` to disk, following the 302 to codeload and aborting
past `MAX_TARBALL_BYTES` (100MB compressed). `extract_snapshot` (`api/worker/snapshot.py`) walks
the archive member by member, never `extractall`: regular files only, symlinks skipped and never
resolved, GitHub's single top-level prefix stripped, vendored trees and `.git` never written,
every destination resolved and checked against the workspace root, files over 500KB skipped (the
same `MAX_FILE_BYTES` the validator already enforces), and hard ceilings of 200,000 members
scanned, 20,000 files extracted, and 200MB written. Past a ceiling the whole review fails with
`REPO_TOO_LARGE_MESSAGE` instead of reviewing what fit, because a half-extracted repository
produces a confidently wrong "no findings" for the missing half. (The 100MB download abort maps to
the same message, which names only the extraction ceilings.) The tarball is deleted before the
analyzers run; disk is the scarcest thing on this tier after memory.

**The snapshot index is flags, not a synthesized mega-diff.** `build_snapshot_index` makes one
`FileDiff` per file with `all_changed=True`, which `touches_change` honors directly. No diff text
is materialized and no set of every line number is held, so the marginal memory cost of a
20,000-file snapshot is one small object per file. `run_review` branches on
`target == "repository"` for exactly three things: this index, dropping `TestDetector` (with
every line counted as changed it would flag every repository without a test file), and raised
analyzer ceilings (300s harness, ruff 300s, ESLint 270s so the subprocess dies with a real error
before the harness abandons its thread).

**The AI stage is chunked, and whose key it is decides how much runs.**
`api/app/analysis/repo_review.py` renders reviewable files whole, with line numbers, packed in
deterministic order (production code first, test-pattern paths last, path-sorted within each
bucket) into chunks under `AI_CHUNK_CHAR_BUDGET` (160,000 characters, deliberately under the PR
path's proven 200,000 single-call bound). The worker sets the cap from `ai_source`: a user's own
key runs up to `MAX_AI_CHUNKS` (40); anything else, the shared keyless tier, gets
`KEYLESS_AI_CHUNKS` (1), one call, the same order of cost as a maximal PR review, so repo reviews
cannot starve the free key. Chunks are paced 7 seconds apart, under Gemini free's 10/minute. Each
chunk retries one transient once (30s later), then degrades and is counted; three consecutive
failures stop the stage. Only config errors (a bad key, a bad model id) abort, because they fail
every chunk identically. Containment is the point: a transient on chunk 39 must never bubble into
the job retry ladder and re-bill chunks 1 through 38 of a user's key. Every chunk's output goes
through the same `parse_candidates` and `validate_candidates` chain as a PR review, and coverage
is written down: `ai_coverage=covered/total`, `ai_capped=keyless`, and `ai_chunks_failed=N` land
in `pipeline_version`, and `_repo_ai_note` turns them into sentences in the summary.

## Alternatives considered

**A synthetic pull request row**, so nothing downstream changes. Rejected on the demo's own
evidence: one special row leaks obligations everywhere (`is_demo` is checked in the routers,
asserted in the worker, excluded in joins). A synthetic PR would need a reserved `github_number`
per repository, would sit in a table the creation path refetches from GitHub
(`refetch_open_pull` refuses anything not open, and this PR does not exist there), and would
still need a faked `base_sha` to satisfy the old NOT NULL, at which point the worker branches
anyway. The union puts the difference in the schema, where two CHECKs enforce it, instead of in
conventions around a lie.

**Per-file contents calls instead of the tarball.** The PR path fetches changed files one
contents call each, bounded by the 300-file compare cap. A 20,000-file snapshot at one call per
file is four hours of the user's entire 5,000/hour quota; the tarball is one request. The
contents API also inlines nothing over 1MB, which the tarball does not care about.

**A synthesized all-added mega-diff** through `build_diff_index`, the demo's literal mechanism.
Loses on memory alone: up to 200MB of extracted content rendered into a diff string plus a
changed-line set per file, parsed by unidiff, on the same 512MB instance that is running uvicorn
and the analyzers. The flag on `FileDiff` buys identical semantics for one boolean.

**Truncate at the ceilings instead of refusing.** Reviewing the first N files would report "no
findings" about the rest with a straight face. Refusal is honest, and the error message names the
limits.

**No tier split.** One shared cap either lets any signed-in user with a big repository spend 40
paced calls of the operator's free-tier key, or holds BYOK users to one chunk for no reason. The
split is decided by `ai_source`, which `resolve_provider` already returned for blame routing.

## Consequences

- **Cross-chunk blindness is accepted.** Each chunk sees only its own files, so a bug whose
  evidence spans two chunks is invisible to the AI. Whole-file packing bounds this (a file is
  never split) but does not remove it; the deterministic analyzers, which are per-file anyway, do
  not care.
- **Containment does not survive losing the job.** There is no per-chunk resume: findings persist
  only in `_persist_success`, so a worker reclaimed by the sweep mid-stage (a spin-down, ADR
  0001) re-runs every chunk on the next attempt, and a full 40-chunk pass spends at least 273
  seconds just pacing. ADR 0001's "retries are expensive" consequence, multiplied by 40.
- **The 7-second pace is provider-agnostic.** It is Gemini free's ceiling applied to everyone: a
  BYOK Anthropic or OpenAI user waits behind a rate limit their quota does not have. The sleep
  also runs before the cancellation check, so a cancel can wait 7 seconds to be noticed.
- **"keyless" means "not your key", not "no key".** The operator's own server-side key is capped
  at one chunk too, and the capped note tells every such user to add their own key in Settings.
- **There is no reviews list.** The only handle on a finished repo review is
  `latest_repo_review` on `GET /repositories/{id}`; supersede an old one and the row survives but
  nothing serves it.
- **Extraction reports statistics nobody reads.** `extract_snapshot` counts the files it skipped
  for size and `_run_repository_review` discards the return value. No finding is lost by it: those
  files exceed the same `MAX_FILE_BYTES` the validator enforces, so they would be unreviewable
  even if extracted. What is missing is the sentence saying so. The AI-stage equivalent
  (`ai_files_skipped_large`) is counted into the coverage total; the extraction one is not
  surfaced at all.
