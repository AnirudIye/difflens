# Scope: DiffLens v1

Frozen on Day 1 (2026-08-15). Changes go through the change policy at the bottom, which is short because the answer is no.

## Goal

Ship a working, deployed AI code review platform for public GitHub pull requests in 10 days at $0 infrastructure cost. A recruiter should be able to click through it in two minutes; an engineer should be able to read the source and see judgment, not feature count: pinned snapshots, validated AI output, a queue that survives crashes, and trade-offs written down honestly.

## In scope v1

- GitHub OAuth sign-in: read-only, public repos only
- Repository and pull request picker
- Async review pipeline, diff fetched pinned to immutable base/head SHAs
- Deterministic analyzers: ruff, detect-secrets, missing-tests heuristic (ESLint as stretch)
- AI reviewer: Anthropic Claude behind a provider abstraction with a mock mode
- Validation of every AI-cited file/line against the reviewed snapshot; hallucinated locations discarded
- Dedupe via content-based fingerprints
- Findings persisted with severity, category, confidence, source, recommendation
- Per-finding user feedback

## Out of scope v1

- Private repos: needs a GitHub App; the OAuth `repo` scope would grant write access, which is the wrong posture for a review tool.
- Webhooks and auto-review: v1 is user-triggered; webhook ingress is GitHub App territory.
- Posting comments back to GitHub: requires write access, contradicts the read-only stance.
- Executing repository code: reviews are static only; running untrusted code is a different product with a different threat model.
- Languages beyond Python and TS/JS: every language multiplies analyzer work, and two are enough to prove the design.
- Multi-provider AI: the abstraction exists, but shipping a second provider adds test surface with zero demo value.

## The 10-day gates

1. Day 1: repo scaffold, CI green, scope and architecture docs frozen.
2. Day 2: database schema and migrations, GitHub OAuth working locally.
3. Day 3: GitHub integration (repos, PRs, diffs) and first production deploy.
4. Day 4: deterministic analyzers running against real diffs.
5. Day 5: worker and queue end to end. Hard descope checkpoint.
6. Day 6: AI review layer with validation and dedupe.
7. Day 7: review UI with findings list and detail.
8. Day 8: tests, security pass, threat model written down.
9. Day 9: demo mode and hardening.
10. Day 10: portfolio polish, README, screenshots, write-up.

## Descope ladder

If behind at the Day 5 checkpoint, cut from the top down. Order is deliberate: the top items cost the demo the least.

1. Playwright E2E tests
2. Syntax-highlighted diff UI
3. Rate limiting
4. Sonnet demo toggle
5. tree-sitter scopes
6. Feedback persistence

## Change policy

Nothing enters scope mid-sprint. Any idea that shows up after Day 1, however good, goes into the Post-sprint list below and stays there until Day 10 is done. That is the entire process.

## Post-sprint

- GitHub App + webhooks
- PR comments (posting findings back to GitHub)
- Private repos
- semgrep
- Oracle VM migration
- Feedback analytics
