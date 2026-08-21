# 0004. Treat the AI provider and its output as untrusted

Status: Accepted
Date: 2026-08-21

## Context

The AI stage is not the only part that reads attacker-authored content: the deterministic analyzers
run over the same workspace, filled from the author's head SHA by `populate_workspace` in
`api/worker/runner.py`. It is the only part that reads it as language and the only one that spends
money per run. It also cites files and lines a user will click, so a confident sentence about
`checkout/payments.py:214` is worthless if that line is not in the snapshot.

One developer, 10 days, $0 infrastructure. Free-tier Gemini is the only zero-cost path to real
reviews, so production runs it while CI runs offline without a secret. `docs/SCOPE.md` ruled
multi-provider out; the owner overturned that on Day 6 and OpenAI landed on Day 8. This ADR was
written on Day 10, after both.

## Decision

**The port is one method.** `api/app/analysis/ai_review.py` defines `review(AIRequest) -> AIResponse`
on an `AIProvider` `Protocol`, `AIResponse` carrying `raw_text`, `refused`, `model` and `truncated`:
no streaming, tool use, client-side retry or token accounting. The analysis package has no database
handle and no network access. `AIProviderConfigError` and `UserAIKeyError` are part of the contract;
`api/worker/runner.py` keys permanent versus retryable failure off them.

**Two of the three real providers skip the vendor SDK**: `GeminiProvider` POSTs to
`/v1beta/models/{model}:generateContent`, `OpenAIProvider` to `/v1/chat/completions`, both on a plain
`httpx.Client`. `AnthropicProvider` uses the SDK with `betas=["server-side-fallback-2026-07-01"]` and
`fallbacks="default"`, so a refusal there is retried server-side on a different model in the same
call. `MockProvider` is the fourth and the default. In `resolve_provider` a stored `UserAIKey` beats
config, and `AI_PROVIDER=off` returns `("deterministic_only", None)`.

**The prompt is fenced and the output is distrusted.** `build_prompt` wraps repository name, PR
title, body and diff in `<untrusted-{nonce}>`, the nonce a per-call `secrets.token_hex(8)`. Then, in
order: a diff over `MAX_DIFF_CHARS = 200_000` skips the stage; `truncated` is recorded but parsing
continues, so a cut-off list is kept as though complete; `refused` discards everything; then
`_shape_ok`, `is_reviewable`, `location_exists` and `touches_change` with `pad=CONTEXT_PAD` (3) per
candidate, `api/app/analysis/diffs/validator.py` ruling out anything deleted, generated, vendored,
oversized or binary. Discards are counted into `stats.ai_discarded`, and `_ai_note` adds a sentence
when the stage degrades.

## Alternatives considered

**One SDK per provider.** Rejected for dependency weight before any incident: one POST does not need
a vendor client. The `anthropic` 1.0.0 release on 2026-08-20 moved to httpx2 and rejects an
`httpx.Client` at construction, which is confirmation rather than the reason; the fix was the pin
`anthropic>=0.79,<1`.

**One OpenAI-compatible gateway** (OpenRouter, LiteLLM) instead of three adapters would collapse
registration to a model string. Rejected: it puts a fourth party between a stranger's diff and the
model, widening gap 10 in `docs/THREAT_MODEL.md`, and gives up the Gemini free tier, the zero-cost
path and the only provider ever run live.

**Trust the structured output.** All three pin `FINDINGS_SCHEMA` server-side, so the question is
whether `_shape_ok` earns its keep beside it. It does: the mock, and any provider added without
strict mode, bypass server-side pinning. The cost is two shape definitions synced by hand.

**Sanitize the diff.** A review tool exists to read hostile-looking code, so a content filter eats
real findings and still misses the phrasing nobody thought of.

## Consequences

- **A provider name is registered in six places with no single source of truth**: `DEFAULT_MODELS`,
  the `build_provider` branch, `provider_from_settings` and its `config.py` key field,
  `ck_user_ai_keys_provider` and its migration, the `Literal` in `api/app/routers/ai_settings.py`,
  and the union in `web/src/lib/types.ts` and `web/src/app/settings/page.tsx`. OpenAI took three
  commits across two languages, and nothing fails if a site is missed.
- **No SDK plus one method means every adapter owns the wire format.**
  `api/app/ai/openai_provider.py` hand-maintains `max_completion_tokens`, `invalid_prompt`,
  `insufficient_quota`, `content_filter` and an `_error_fields` helper for gateways that answer with
  HTML. `MAX_OUTPUT_TOKENS = 16000` is copied into all three and the timeouts never converged: 120s,
  600s, whatever the SDK defaults to.
- **The chain bounds false positives and does nothing about induced false negatives.** Every check
  runs on findings the model returned, so an injection that keeps it silent about a real bug leaves
  no discard, no marker and no note. `docs/THREAT_MODEL.md` also claims two controls that do not
  exist: the prompt says to ignore embedded instructions, not to report them, and no prompt version
  reaches `pipeline_version`.
- **Prose is the unvalidated half.** `explanation` and `recommendation` are model-authored and
  uncapped, and `dedup.py::_merge_target` grafts them onto a deterministic finding and can raise its
  confidence, so unchecked text renders under an analyzer's title.
- **The Anthropic fallback is silent**: nothing in `AIResponse` carries it, so `_ai_note` says
  nothing when another model answered, on the least-tested adapter (four tests in
  `api/tests/test_ai_providers.py` against twenty OpenAI cases) and the only one `docs/SCOPE.md`
  does not claim has been exercised live.
