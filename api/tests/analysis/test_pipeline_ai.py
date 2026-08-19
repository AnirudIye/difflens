"""Cheap mode: deterministic analyzers plus the AI reviewer through one pipeline.

Everything runs against MockProvider (or an inline stub), so these tests are
offline and deterministic like the rest of the analysis suite.
"""

import json

import pytest

from app.ai.mock import MockProvider
from app.analysis.ai_review import AIRequest, AIResponse
from app.analysis.pipeline import run_review
from tests.analysis.test_pipeline import golden, make_job, normalize


def ai_candidate(**overrides) -> dict:
    base = {
        "file_path": "app/report.py",
        "start_line": 15,
        "end_line": 15,
        "severity": "medium",
        # 'performance' cannot merge with the security/correctness findings
        # nearby, so this one must survive as a standalone AI finding
        "category": "performance",
        "confidence": "medium",
        "title": "String concatenation rebuilds the query every call",
        "explanation": "Consider a prepared statement.",
        "recommendation": "Use parameterized queries.",
    }
    return {**base, **overrides}


def test_cheap_mode_requires_a_provider():
    with pytest.raises(ValueError, match="provider"):
        run_review(make_job("python_buggy", mode="cheap"))


def test_deterministic_only_never_consults_the_provider():
    class ExplodingProvider:
        def review(self, request: AIRequest) -> AIResponse:
            raise AssertionError("deterministic_only must not call the provider")

    result = run_review(make_job("python_buggy"), provider=ExplodingProvider())
    assert result.stats.ai_model is None


def test_cheap_mode_validates_merges_and_counts():
    provider = MockProvider(
        candidates=[
            ai_candidate(),  # standalone: survives as source "ai"
            ai_candidate(  # overlaps ruff S602 at line 10, same category: merges
                start_line=10,
                end_line=10,
                severity="high",
                category="security",
                title="shell=True builds a command from user input",
                explanation="The target string reaches a shell unsanitized.",
            ),
            ai_candidate(file_path="app/invented.py"),  # hallucinated file
            ai_candidate(start_line=999, end_line=999),  # hallucinated line
        ]
    )
    result = run_review(make_job("python_buggy", mode="cheap"), provider=provider)

    # Every deterministic golden finding is still present (line 10 upgraded)
    assert len(result.findings) == len(golden("python_buggy")) + 1
    by_line = {(f.file_path, f.start_line): f for f in result.findings}
    merged = by_line[("app/report.py", 10)]
    assert merged.source == "hybrid"
    # dedup folds the AI explanation into the deterministic finding
    assert "reaches a shell unsanitized" in (merged.explanation or "")
    standalone = by_line[("app/report.py", 15)]
    # Two findings share line 15; the AI one is the performance finding
    ai_findings = [f for f in result.findings if f.source == "ai"]
    assert len(ai_findings) == 1
    assert ai_findings[0].category == "performance"
    assert standalone is not None

    stats = result.stats
    assert stats.ai_model == "mock"
    assert stats.ai_candidates == 4
    assert stats.ai_discarded["unreviewable_file"] == 1
    assert stats.ai_discarded["bad_location"] == 1
    assert stats.ai_refused is False
    assert stats.ai_parse_failed is False
    assert "ai" in stats.stage_durations_ms
    assert stats.tool_versions["ai"] == "mock"


def test_refusal_degrades_to_deterministic_findings():
    result = run_review(make_job("python_buggy", mode="cheap"), provider=MockProvider(refused=True))
    assert normalize(result) == golden("python_buggy")
    assert result.stats.ai_refused is True
    assert result.stats.ai_candidates == 0


def test_unparseable_output_degrades_to_deterministic_findings():
    class GarbageProvider:
        def review(self, request: AIRequest) -> AIResponse:
            return AIResponse(raw_text="Looks fine to me!", refused=False, model="mock")

    result = run_review(make_job("python_buggy", mode="cheap"), provider=GarbageProvider())
    assert normalize(result) == golden("python_buggy")
    assert result.stats.ai_parse_failed is True


def test_cheap_mode_with_mock_is_deterministic():
    def one_run() -> bytes:
        provider = MockProvider(candidates=[ai_candidate()])
        result = run_review(make_job("python_buggy", mode="cheap"), provider=provider)
        return json.dumps(normalize(result), sort_keys=True).encode()

    assert one_run() == one_run()


def test_refusal_is_visible_in_the_summary():
    result = run_review(make_job("python_buggy", mode="cheap"), provider=MockProvider(refused=True))
    assert "AI reviewer declined" in result.summary


def test_unusable_output_is_visible_in_the_summary():
    class GarbageProvider:
        def review(self, request: AIRequest) -> AIResponse:
            return AIResponse(raw_text="not json", refused=False, model="mock")

    result = run_review(make_job("python_buggy", mode="cheap"), provider=GarbageProvider())
    assert "output was unusable" in result.summary


def test_truncated_output_is_marked_and_noted():
    class TruncatedProvider:
        def review(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                raw_text='{"findings": [{"file_pa', refused=False, model="mock", truncated=True
            )

    result = run_review(make_job("python_buggy", mode="cheap"), provider=TruncatedProvider())
    assert result.stats.ai_truncated is True
    assert result.stats.ai_parse_failed is True
    assert "cut short" in result.summary
    assert normalize(result) == golden("python_buggy")


def test_oversized_diff_skips_the_ai_stage_honestly():
    from app.analysis.ai_review import MAX_DIFF_CHARS

    class ExplodingProvider:
        def review(self, request: AIRequest) -> AIResponse:
            raise AssertionError("an oversized diff must never reach the provider")

    pad_lines = MAX_DIFF_CHARS // 5 + 100
    big_file_diff = (
        "diff --git a/big.txt b/big.txt\n"
        "--- /dev/null\n"
        "+++ b/big.txt\n"
        f"@@ -0,0 +1,{pad_lines} @@\n" + "+pad\n" * pad_lines
    )
    job = make_job("python_buggy", mode="cheap")
    job = job.model_copy(update={"diff_text": job.diff_text + big_file_diff})

    result = run_review(job, provider=ExplodingProvider())

    assert result.stats.ai_skipped == "diff_too_large"
    assert result.stats.ai_model is None
    assert "too large for the AI reviewer" in result.summary
    # Deterministic findings still landed
    assert len(result.findings) == len(golden("python_buggy"))


def test_deterministic_summary_never_carries_an_ai_note():
    result = run_review(make_job("python_buggy"))
    assert "AI reviewer" not in result.summary


def test_nonce_is_random_per_review():
    import re

    nonces: list[str] = []

    class RecordingProvider:
        def review(self, request: AIRequest) -> AIResponse:
            match = re.search(r"<untrusted-([0-9a-f]+)>", request.user)
            assert match is not None
            nonces.append(match.group(1))
            return AIResponse(raw_text='{"findings": []}', refused=False, model="mock")

    run_review(make_job("python_buggy", mode="cheap"), provider=RecordingProvider())
    run_review(make_job("python_buggy", mode="cheap"), provider=RecordingProvider())
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]
    assert all(len(nonce) == 16 for nonce in nonces)  # secrets.token_hex(8)


def test_prompt_reaches_the_provider_fenced():
    seen: dict = {}

    class RecordingProvider:
        def review(self, request: AIRequest) -> AIResponse:
            seen["request"] = request
            return AIResponse(raw_text='{"findings": []}', refused=False, model="mock")

    run_review(make_job("python_buggy", mode="cheap"), provider=RecordingProvider())
    request = seen["request"]
    assert "<untrusted-" in request.user and "</untrusted-" in request.user
    assert "fixture python_buggy" in request.user  # the PR title rode inside
    assert request.output_schema["properties"]["findings"]  # schema went along
