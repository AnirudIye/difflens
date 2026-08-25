# 0006. Precision at repository scale is a filter problem, not a tuning problem

Status: Accepted
Date: 2026-08-24

## Context

A repository review of a real project, FootyBoard, returned exactly 100 findings: the cap. 56
critical, 3 high, 41 medium. Four were real, and all four came from the AI reviewer. Every one of
the other 96 came from the deterministic analyzers.

The causes were mundane, and each was reproduced against the bundled tooling before anything was
changed:

- A fake password in a test fixture is a `Secret Keyword` match. The repository has 95 test files.
- A placeholder connection string in `.env.example` is a `Basic Auth Credentials` match. That file
  exists to hold one.
- `const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'` is a `Base64 High Entropy String` match.
  Every TOTP implementation contains that line.
- `no-promise-executor-return` fires on `new Promise((r) => setTimeout(r, ms))`, the standard sleep
  idiom: 39 findings. `require-atomic-updates` fires on ordinary async assignment: 6 more.

## The cause behind the causes

`touches_change` was carrying precision, not just topicality.

In a pull request review every deterministic finding must sit on or near a line the author has just
written; everything else is discarded. That single predicate is what made these analyzers tolerable,
because they only ever spoke about new code. It was never described as a precision filter, so when
ADR 0005 set `all_changed=True` for snapshots and made the predicate universally true, the filter
disappeared and nothing replaced it.

ADR 0005 did note that losing `touches_change` "loses the only topicality filter on AI output". The
same sentence was true of the deterministic output and was not written down, which is where the
blast radius actually was: the AI half runs once over selected files, and the deterministic half
runs over every file in the repository.

Four amplifiers sat on top. Secret severity was hardcoded `critical` for every detector in every
file. Nothing distinguished a test from production. The detector and rule selection had never been
reviewed at repository scale. And because the report is cut at `MAX_FINDINGS` in severity order,
56 findings calling themselves critical could evict real ones before they were considered.

## Decision

**Precision comes from the file's kind and the detector's strength, since it can no longer come from
the diff.** `app/analysis/paths.py` classifies each path as test, example, generated or production.
A detector that matches a documented token shape (AWS, GitHub, Stripe, private key, JWT) reports
everywhere, because a live key committed to a fixture is still a live key; it drops from critical to
medium in a fixture, which is a reason to look twice rather than a reason to look away. A detector
that guesses from a keyword or from entropy reports only in production code.

**Severity comes from the detector.** Structured match in production is critical. Basic auth is
high. Keyword and entropy are medium. Nothing is critical merely for matching a regex.

**Three line-level heuristics kill the residue**, applied only to the guessing detectors so a
structured match is never talked out of a report:

- An encoding alphabet uses each symbol once, which is what makes it an alphabet. A random secret of
  the same length almost never does: drawing 24 symbols from 64 without repeating has probability
  well under one percent. That one property separates the base32 constant from a key.
- A credential lives in a literal, and a short one is not a credential. `typeof currentPassword !==
  'string'` quotes only the word `string`.
- A value that calls itself a placeholder is believed, and a `user:password@localhost` default is a
  local convenience rather than a leak. RFC 2606 reserves `example.com` and `example.net` for
  documentation, so those count as placeholders too.

**Two ESLint rules are off**, with the measurements in the config comments beside them.

**The finding cap orders for diversity before it truncates.** `prioritize` takes at most
`MAX_PER_RULE` and `MAX_PER_FILE` in a first pass and appends everything held back in a second, so
one noisy rule cannot occupy the whole budget while a rule with a hundred genuine hits still fills
the report when nothing competes for the room. It is an ordering, never a filter: every finding
handed in comes back out.

**Suppression is counted and stated.** The summary says how many credential-shaped matches were held
back and why. The house rule is that degradation is visible; quietly dropping 55 findings would
break it more thoroughly than the noise did.

## Measured result

Against the real FootyBoard tree, 287 files:

| | before | after |
|---|---|---|
| detect-secrets | 56, all critical | 1 medium |
| ESLint | 47 | 2 |
| deterministic total | 103 | 3 |

The old numbers were reproduced by emulating the previous rules against the same tree, and the 56
matches the original report exactly. The single surviving secret finding is a genuinely hardcoded
password in a maintenance script, which is worth a medium.

## Consequences

- **Recall in fixtures is deliberately traded away.** A weak-signal credential that is genuinely
  leaked inside a test file is no longer reported. Structured detectors still cover the cases where
  a leak is provable from the string alone, which is where the real risk lives.
- **The heuristics are heuristics.** A real secret that happens to use each character once, or one
  sitting on a line whose only literal is short, is missed. Both are rare, and both fail toward
  silence rather than toward a false alarm, which is the direction this change is choosing.
- **`paths.py` encodes convention.** A project that keeps production code in a directory called
  `fixtures` gets quieter treatment than it should.
- **The regression fixture is the durable part.** `tests/analysis/test_snapshot_precision.py`
  reviews a workspace shaped like a real project rather than a curated diff, and asserts the noise
  is gone and a planted key is still found in the same breath. The suite had 434 tests when this
  shipped and none of them could have caught it, because every fixture was a small pull request.
