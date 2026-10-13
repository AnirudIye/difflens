# Honor the reviewed repository's ruff configuration

Date: 2026-08-25. Status: approved in discussion; this document records it.

## Problem

Both linter adapters deliberately refuse the reviewed repository's own
configuration: ruff runs `--isolated --select E9,F,B,S`, ESLint runs
`--no-config-lookup --config <bundled>`. So DiffLens flags things the
repository's own toolchain would never flag (rules the project never enabled)
and misses the project's own selections and ignores. The owner asked for the
analyzers to run through the repo's lint config instead.

## Decision

**Ruff honors the repository's config. ESLint does not.** The asymmetry is a
security boundary, not an oversight: ruff config is inert TOML that ruff reads
as data, while ESLint 9 flat config is an executable JavaScript module whose
plugins are arbitrary npm packages. Honoring the repo's ESLint config means
executing the reviewed repository's code inside the worker, which holds the
Fernet key, the database URL, and the server AI key. That is the exact
arbitrary-code-execution vector reproduced and closed on Day 8, and this
deployment has no sandbox to contain it (the worker is co-located on Render's
free tier; same-UID children can read the worker's environment and reach the
database). Revisiting requires per-review isolation infrastructure first.

## Behavior

### The switch: a root config

If the reviewed tree's **root** carries a ruff config — `ruff.toml`,
`.ruff.toml`, or `pyproject.toml` with a `[tool.ruff]` table — the adapter
runs ruff in **repository-config mode**:

- drop `--isolated` and `--select`; keep `--output-format json`, `--no-cache`,
  and the `--` end-of-options separator (the argv-injection fix is
  mode-independent);
- add `--no-fix` (a repo config can set `fix = true`, and a reviewer must
  never mutate the tree it is reviewing) and `--force-exclude` (the repo's
  `exclude` lists apply even to explicitly passed paths, matching what that
  repo's own CI prints);
- ruff's own hierarchical discovery then applies the root config plus any
  nested configs, exactly as it would in that repository's CI.

No root config → today's command, byte for byte. Root-only detection is
deliberate: it guarantees no per-file upward config search ever escapes the
workspace (the root config is the backstop). A monorepo with only nested
configs and no root config keeps the bundled rules; accepted v1 edge.

Detection reads `pyproject.toml` with `tomllib` and requires a `tool.ruff`
table (ruff itself skips a pyproject without one and would walk past the
workspace root). If the file is unparseable but its text contains
`[tool.ruff`, it counts as present: the repo clearly meant to configure ruff,
and the honest outcome is the visible failure path below, not silently
linting with our rules while their CI fails.

### Both review kinds

- **Repository snapshots**: the whole tree is in the workspace; nothing extra
  to do.
- **Pull request reviews**: the workspace holds only changed files, so worker
  ingestion additionally fetches the three root config names at the pinned
  head SHA into the workspace (three contents calls; 404 means absent and is
  caught, never escalated to the review-failure path). The PR diff index is
  built from the diff text, so the fetched config never appears as a reviewed
  file. A PR that itself modifies the config is naturally covered: the fetch
  is pinned to head.

### Failure falls back, visibly

If the repository-config run exits ≥ 2 (broken config, `extend` pointing at
a file that does not exist or will not parse, `required-version` mismatch),
the adapter logs the full stderr server-side (structlog redaction applies),
then **reruns once in bundled mode** and flags the fallback. Hard-skipping
ruff entirely would make a repo with a broken config strictly worse off than
today. Timeouts do not retry (time budget) and surface as today's skip.

### Provenance is visible

- `ReviewStats.ruff_config_source`: `"bundled"` (default) or `"repository"`.
- `ReviewStats.ruff_repo_config_failed`: fallback happened.
- Summary notes: "ruff ran with the repository's own ruff configuration." /
  "The repository's ruff configuration could not be used; ruff ran with
  DiffLens's default rules instead."
- `pipeline_version` markers: `ruff_config=repository` /
  `ruff_config=repository_failed`.

The adapter exposes the outcome as instance attributes read by the pipeline
after the run, the same pattern as `SecretsAnalyzer.suppressed`.

### Hardening that rides along

The linter subprocesses run with an allowlisted environment (PATH, the
Windows process-boot variables, TEMP/TMP, locale). Motivation: a repo config's
`extend` can point at any readable path, including the child's own
`/proc/self/environ`; a scrubbed environment makes that read worthless. The
allowlist applies to both linters uniformly — neither ever needed secrets.
Skip-reason strings already stay server-side (only analyzer names reach
`pipeline_version` and the API payload), so no page-side sanitization is
needed.

## Accepted trade-offs

- **A PR author can now silence ruff rules for their own PR** by adding or
  editing the repo's ruff config in that PR. This is the direct meaning of
  "honor the repo's config": the config change is visible in the reviewed
  diff, the provenance note names whose rules ran, and detect-secrets (which
  reads no ruff config) still covers hardcoded credentials regardless. The
  Day 8 argv-injection fix is unaffected — file *names* still cannot become
  flags in either mode.
- **Foreign rule codes** (families outside E9/F/B/S that a repo enables) map
  through `RUFF_DEFAULT` = medium/maintainability/medium unless already in
  the tables. A style-heavy config makes a medium-heavy report; that is the
  repo's own standard, and the diversity cap bounds the volume.
- **Their config, their ruff version — almost.** We run our pinned ruff
  0.16.3 against their config; a repo pinning a different ruff in
  `required-version` gets the visible fallback rather than that version.
- **`.secrets.baseline` is out of scope**: detect-secrets keeps its bundled
  configuration. ESLint sandboxing is out of scope, as above.

## Tests

- Adapter honors a root `ruff.toml` (a rule outside their `select` stops
  being reported; a planted violation inside it still is), honors
  `per-file-ignores`, and detects `pyproject.toml` only with a `tool.ruff`
  table.
- No config → the exact bundled argv (asserted), so golden fixtures and the
  snapshot-precision suite stay green untouched.
- Broken config (`extend` at a missing file) → bundled fallback, flag set,
  findings still produced.
- A canary secret in the parent environment never reaches either linter
  child (asserted on the captured env of both).
- PR ingestion writes a fetched config into the workspace and survives 404s.
- Pipeline/summary: the provenance sentence appears exactly when it should;
  `pipeline_version` carries the marker.
- Regression tests are mutation-graded: revert the adapter change and watch
  them fail.
