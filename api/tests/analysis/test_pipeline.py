"""End-to-end regression tests: each fixture PR must produce its exact golden findings."""

import json
import subprocess
from pathlib import Path

import pytest

from app.analysis.models import ReviewJob, ReviewResult
from app.analysis.pipeline import AnalysisError, run_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ANALYZER_NAMES = {"ruff", "detect-secrets", "missing-tests"}
STAGES = {"parse", "analyze", "dedup", "summarize"}
GOLDEN_FIELDS = (
    "file_path",
    "start_line",
    "end_line",
    "severity",
    "category",
    "confidence",
    "source",
    "rule_id",
    "tool",
    "title",
)

# the canonical AWS documentation example key, assembled so the full literal
# never sits in this repo's source
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def make_job(fixture: str, **overrides) -> ReviewJob:
    root = FIXTURES / fixture
    job = ReviewJob(
        repo_full_name="difflens/fixture",
        pr_title=f"fixture {fixture}",
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff_text=(root / "pr.diff").read_text(encoding="utf-8"),
        workspace=root / "files",
    )
    return job.model_copy(update=overrides) if overrides else job


def normalize(result: ReviewResult) -> list[dict]:
    return [{field: getattr(f, field) for field in GOLDEN_FIELDS} for f in result.findings]


def golden(fixture: str) -> list[dict]:
    return json.loads((FIXTURES / fixture / "expected.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", ["python_buggy", "secrets", "clean"])
def test_fixture_matches_golden(fixture):
    result = run_review(make_job(fixture))
    assert normalize(result) == golden(fixture)


def test_clean_pr_gets_the_clean_summary():
    result = run_review(make_job("clean"))
    assert result.summary == "No findings. The changed code passes all deterministic checks."
    assert result.findings == []


def test_buggy_summary_counts_by_severity():
    result = run_review(make_job("python_buggy"))
    assert result.summary == "6 findings across 1 file (4 high, 1 medium, 1 low)"


def test_python_buggy_is_deterministic():
    runs = [
        json.dumps(normalize(run_review(make_job("python_buggy"))), sort_keys=True).encode()
        for _ in range(2)
    ]
    assert runs[0] == runs[1]


def test_stats_sanity():
    stats = run_review(make_job("python_buggy")).stats

    assert ANALYZER_NAMES <= set(stats.analyzers_run)
    assert stats.analyzers_skipped == {}
    assert set(stats.stage_durations_ms) == STAGES
    assert all(duration >= 0 for duration in stats.stage_durations_ms.values())
    assert stats.findings_before_dedup == 6
    assert stats.findings_after_dedup == 6
    assert stats.truncated is False
    assert set(stats.tool_versions) == {"ruff", "detect-secrets", "python"}


def test_secret_value_never_leaks():
    result = run_review(make_job("secrets"))
    assert AWS_KEY not in result.model_dump_json()


def test_malformed_diff_raises_analysis_error():
    # hunk header promises three lines, body delivers one
    bad = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n+too short\n"
    with pytest.raises(AnalysisError):
        run_review(make_job("clean", diff_text=bad))


@pytest.mark.parametrize("mode", ["cheap", "demo"])
def test_ai_modes_require_a_provider(mode):
    with pytest.raises(ValueError, match="provider"):
        run_review(make_job("clean", mode=mode))


def test_ruff_crash_is_skipped_not_fatal(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("ruff exploded")

    monkeypatch.setattr(subprocess, "run", boom)
    result = run_review(make_job("python_buggy"))

    assert "ruff" not in result.stats.analyzers_run
    assert "OSError" in result.stats.analyzers_skipped["ruff"]
    assert "detect-secrets" in result.stats.analyzers_run
    assert "missing-tests" in result.stats.analyzers_run
    # the version probe shares subprocess with ruff, so it degrades too
    assert result.stats.tool_versions["ruff"] == "unknown"
