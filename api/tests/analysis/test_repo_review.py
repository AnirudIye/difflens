"""The repository snapshot pipeline: index, chunk planning, the AI stage loop.

Everything here is pure and offline: temp workspaces, scripted providers, and
recorded sleeps. The AI stage's cost containment (per-chunk retry, degrade,
consecutive-failure stop) is the load-bearing behavior under test.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.analysis.repo_review as repo_review
from app.ai.errors import AIProviderConfigError
from app.analysis.ai_review import AIRequest, AIResponse
from app.analysis.diffs.snapshot import build_snapshot_index
from app.analysis.diffs.validator import touches_change
from app.analysis.models import ReviewJob, ReviewStats
from app.analysis.repo_review import (
    AI_CHUNK_PACE_S,
    CHUNK_RETRY_DELAY_S,
    AnalysisStopped,
    build_repo_prompt,
    plan_chunks,
    run_repo_ai_stage,
)


def snapshot_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def make_repo_job(workspace: Path, **overrides) -> ReviewJob:
    job = ReviewJob(
        repo_full_name="octocat/alpha",
        head_sha="b" * 40,
        diff_text="",
        workspace=workspace,
        target="repository",
        mode="cheap",
    )
    return job.model_copy(update=overrides) if overrides else job


def candidate(**overrides) -> dict:
    base = {
        "file_path": "a.py",
        "start_line": 1,
        "end_line": 1,
        "severity": "medium",
        "category": "correctness",
        "confidence": "medium",
        "title": "Suspicious constant",
        "explanation": None,
        "recommendation": None,
    }
    return {**base, **overrides}


def ok(candidates: list[dict] | None = None, **overrides) -> AIResponse:
    return AIResponse(
        raw_text=json.dumps({"findings": candidates or []}), model="scripted", **overrides
    )


class ScriptedProvider:
    """Answers each call with the next scripted item; exceptions are raised."""

    def __init__(self, *script: AIResponse | BaseException) -> None:
        self.script = list(script)
        self.requests: list[AIRequest] = []

    def review(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        assert self.script, "the provider was called more times than the test scripted"
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record every time.sleep the stage asks for, without sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr(repo_review, "time", SimpleNamespace(sleep=recorded.append))
    return recorded


def _small_budget(monkeypatch, workspace: Path, paths: list[str], per_chunk: int) -> None:
    """Shrink the chunk budget so exactly per_chunk of these files fit together."""
    rendered = [len(repo_review._render_file(path, workspace)) for path in paths]
    assert len(set(rendered)) == 1, "budget math needs equally sized files"
    monkeypatch.setattr(repo_review, "AI_CHUNK_CHAR_BUDGET", rendered[0] * per_chunk + 1)


def _stage_input(tmp_path, files: dict[str, str]):
    workspace = snapshot_workspace(tmp_path, files)
    return workspace, build_snapshot_index(workspace), ReviewStats()


# --- build_snapshot_index and touches_change ---


def test_snapshot_index_marks_every_file_all_changed(tmp_path):
    workspace = snapshot_workspace(
        tmp_path, {"z.py": "x = 1\n", "a.py": "y = 2\n", "src/deep/mod.py": "z = 3\n"}
    )

    index = build_snapshot_index(workspace)

    assert list(index.files) == ["a.py", "src/deep/mod.py", "z.py"]  # posix, sorted
    assert all(diff.all_changed for diff in index.files.values())
    assert all(diff.status == "added" for diff in index.files.values())


def test_touches_change_is_true_anywhere_for_all_changed_files(tmp_path):
    workspace = snapshot_workspace(tmp_path, {"a.py": "x = 1\n"})
    index = build_snapshot_index(workspace)

    assert touches_change(index, "a.py", 1, 1) is True
    assert touches_change(index, "a.py", 10**9, 10**9) is True
    assert touches_change(index, "missing.py", 1, 1) is False


# --- plan_chunks ---


def test_plan_chunks_orders_production_code_before_tests(tmp_path):
    workspace, index, stats = _stage_input(
        tmp_path, {"tests/test_a.py": "x = 1\n", "b.py": "y = 2\n", "a.py": "z = 3\n"}
    )
    to_run, beyond = plan_chunks(make_repo_job(workspace), index, stats)

    assert beyond == []
    assert [chunk.paths for chunk in to_run] == [["a.py", "b.py", "tests/test_a.py"]]


def test_plan_chunks_packs_whole_files_first_fit_under_the_budget(tmp_path, monkeypatch):
    workspace, index, stats = _stage_input(
        tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n", "c.py": "z = 3\n"}
    )
    _small_budget(monkeypatch, workspace, ["a.py", "b.py", "c.py"], per_chunk=2)

    to_run, beyond = plan_chunks(make_repo_job(workspace), index, stats)

    assert beyond == []
    assert [chunk.paths for chunk in to_run] == [["a.py", "b.py"], ["c.py"]]
    assert stats.ai_chunks_planned == 2


def test_plan_chunks_excludes_a_file_bigger_than_the_budget(tmp_path, monkeypatch):
    workspace, index, stats = _stage_input(
        tmp_path, {"a.py": "x = 1\n", "big.py": "line = 1\n" * 40}
    )
    _small_budget(monkeypatch, workspace, ["a.py"], per_chunk=1)

    to_run, beyond = plan_chunks(make_repo_job(workspace), index, stats)

    assert [chunk.paths for chunk in to_run] == [["a.py"]]
    assert beyond == []
    assert stats.ai_files_skipped_large == 1
    assert stats.ai_files_total == 2  # the skipped file still counts toward total


def test_plan_chunks_counts_beyond_cap_files_in_the_total(tmp_path, monkeypatch):
    workspace, index, stats = _stage_input(
        tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n", "c.py": "z = 3\n"}
    )
    _small_budget(monkeypatch, workspace, ["a.py", "b.py", "c.py"], per_chunk=1)

    to_run, beyond = plan_chunks(make_repo_job(workspace, ai_chunk_cap=1), index, stats)

    assert [chunk.paths for chunk in to_run] == [["a.py"]]
    assert [chunk.paths for chunk in beyond] == [["b.py"], ["c.py"]]
    assert stats.ai_files_total == 3
    assert stats.ai_chunks_planned == 3


# --- run_repo_ai_stage ---


def test_keyless_cap_runs_one_chunk_and_marks_capped(tmp_path, monkeypatch, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _small_budget(monkeypatch, workspace, ["a.py", "b.py"], per_chunk=1)
    job = make_repo_job(workspace, ai_chunk_cap=1, ai_cap_reason="keyless")
    provider = ScriptedProvider(ok())

    run_repo_ai_stage(job, provider, index, stats)

    assert len(provider.requests) == 1
    assert stats.ai_capped is True
    assert stats.ai_files_covered == 1
    assert stats.ai_files_total == 2


def test_uncut_keyless_review_is_not_marked_capped(tmp_path, sleeps):
    # One chunk planned, cap one: the cap cut nothing, so no capped flag
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    job = make_repo_job(workspace, ai_chunk_cap=1, ai_cap_reason="keyless")

    run_repo_ai_stage(job, ScriptedProvider(ok()), index, stats)

    assert stats.ai_capped is False
    assert stats.ai_files_covered == stats.ai_files_total == 1


def test_byok_runs_every_chunk(tmp_path, monkeypatch, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _small_budget(monkeypatch, workspace, ["a.py", "b.py"], per_chunk=1)
    job = make_repo_job(workspace, ai_chunk_cap=None, ai_cap_reason=None)
    provider = ScriptedProvider(ok(), ok())

    run_repo_ai_stage(job, provider, index, stats)

    assert len(provider.requests) == 2
    assert stats.ai_capped is False
    assert stats.ai_files_covered == stats.ai_files_total == 2


def test_the_hard_ceiling_cutting_a_byok_repo_is_not_keyless_capped(tmp_path, monkeypatch, sleeps):
    """ai_capped means "your missing key did this"; a repository bigger than
    MAX_AI_CHUNKS is a size limit, reported by the coverage numbers instead."""
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _small_budget(monkeypatch, workspace, ["a.py", "b.py"], per_chunk=1)
    monkeypatch.setattr(repo_review, "MAX_AI_CHUNKS", 1)
    job = make_repo_job(workspace, ai_chunk_cap=None, ai_cap_reason=None)
    provider = ScriptedProvider(ok())

    run_repo_ai_stage(job, provider, index, stats)

    assert len(provider.requests) == 1
    assert stats.ai_capped is False
    assert stats.ai_files_covered == 1
    assert stats.ai_files_total == 2


def test_transient_error_retries_once_then_succeeds(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    provider = ScriptedProvider(RuntimeError("provider hiccup"), ok([candidate()]))

    findings = run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    assert len(provider.requests) == 2
    assert sleeps == [CHUNK_RETRY_DELAY_S]
    assert stats.ai_chunks_failed == 0
    assert stats.ai_chunks_run == 1
    assert len(findings) == 1


def test_failing_chunk_degrades_and_the_loop_continues(tmp_path, monkeypatch, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _small_budget(monkeypatch, workspace, ["a.py", "b.py"], per_chunk=1)
    provider = ScriptedProvider(RuntimeError("down"), RuntimeError("still down"), ok())

    run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    assert len(provider.requests) == 3  # chunk one twice, chunk two once
    assert stats.ai_chunks_failed == 1
    assert stats.ai_files_covered == 1  # only chunk two's file counts as read


def test_three_consecutive_failures_abandon_the_remaining_chunks(tmp_path, monkeypatch, sleeps):
    files = {f"{name}.py": "x = 1\n" for name in "abcde"}
    workspace, index, stats = _stage_input(tmp_path, files)
    _small_budget(monkeypatch, workspace, sorted(files), per_chunk=1)
    provider = ScriptedProvider(*[RuntimeError("hard down")] * 6)

    run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    # Three chunks each burned both attempts, then the stage stopped spending
    assert len(provider.requests) == 6
    assert stats.ai_chunks_failed == 5  # three failed plus two abandoned
    assert stats.ai_files_covered == 0


def test_refusal_counts_the_chunk_failed(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    refusal = AIResponse(raw_text="", refused=True, model="scripted")

    findings = run_repo_ai_stage(make_repo_job(workspace), ScriptedProvider(refusal), index, stats)

    assert findings == []
    assert stats.ai_chunks_failed == 1


def test_unparseable_output_counts_the_chunk_failed(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    garbage = AIResponse(raw_text="Looks fine to me!", model="scripted")

    findings = run_repo_ai_stage(make_repo_job(workspace), ScriptedProvider(garbage), index, stats)

    assert findings == []
    assert stats.ai_chunks_failed == 1


def test_truncated_response_keeps_its_parsed_findings(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    provider = ScriptedProvider(ok([candidate()], truncated=True))

    findings = run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    assert stats.ai_truncated is True
    assert len(findings) == 1
    assert stats.ai_chunks_failed == 0


def test_pacing_sleep_runs_between_chunks(tmp_path, monkeypatch, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    _small_budget(monkeypatch, workspace, ["a.py", "b.py"], per_chunk=1)

    run_repo_ai_stage(make_repo_job(workspace), ScriptedProvider(ok(), ok()), index, stats)

    assert sleeps == [AI_CHUNK_PACE_S]  # before the second chunk, never the first


def test_a_candidate_citing_a_nonexistent_line_is_discarded(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    provider = ScriptedProvider(ok([candidate(start_line=999, end_line=999)]))

    findings = run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    assert findings == []
    assert stats.ai_discarded["bad_location"] == 1
    assert stats.ai_chunks_failed == 0  # the chunk ran; one candidate died


def test_config_error_propagates_immediately(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    provider = ScriptedProvider(AIProviderConfigError("bad key"))

    with pytest.raises(AIProviderConfigError):
        run_repo_ai_stage(make_repo_job(workspace), provider, index, stats)

    assert len(provider.requests) == 1  # no retry burned a second call


def test_stop_check_cancellation_raises_between_chunks(tmp_path, sleeps):
    workspace, index, stats = _stage_input(tmp_path, {"a.py": "x = 1\n"})
    provider = ScriptedProvider()

    with pytest.raises(AnalysisStopped) as excinfo:
        run_repo_ai_stage(
            make_repo_job(workspace), provider, index, stats, stop_check=lambda: "cancelled"
        )

    assert excinfo.value.outcome == "cancelled"
    assert provider.requests == []  # cancelled before any money was spent


# --- build_repo_prompt ---


def test_repo_prompt_is_fenced_and_speaks_snapshot(tmp_path):
    workspace = snapshot_workspace(tmp_path, {"a.py": "x = 1\n"})
    job = make_repo_job(workspace)
    chunk_text = repo_review._render_file("a.py", workspace)

    system, user = build_repo_prompt(job, "cafe1234", chunk_text)

    assert user.count("<untrusted-cafe1234>") == 1
    assert user.count("</untrusted-cafe1234>") == 1
    assert "octocat/alpha" in user
    assert "x = 1" in user
    assert "pull request" not in system.lower()
    assert "pull request" not in user.lower()


# --- the pipeline with target="repository" ---


CLEAN_SOURCE = "def add(a, b):\n    return a + b\n"


def test_repo_mode_runs_no_test_detector(tmp_path):
    from app.analysis.pipeline import run_review

    # Source files and not one test: PR mode would flag missing tests, and at
    # repository scope that would fire on every repo without a test file
    workspace = snapshot_workspace(tmp_path, {"src/app.py": CLEAN_SOURCE})

    result = run_review(make_repo_job(workspace, mode="deterministic_only"))

    assert "missing-tests" not in result.stats.analyzers_run
    assert "missing-tests" not in result.stats.analyzers_skipped
    assert not any(finding.tool == "missing-tests" for finding in result.findings)


def test_clean_repo_review_gets_the_snapshot_summary(tmp_path):
    from app.analysis.pipeline import run_review

    workspace = snapshot_workspace(tmp_path, {"src/app.py": CLEAN_SOURCE})

    result = run_review(make_repo_job(workspace, mode="deterministic_only"))

    assert result.findings == []
    assert result.summary == "No findings. This repository came back clean at this commit."


def test_truncated_findings_get_the_cap_sentence(tmp_path, monkeypatch):
    import app.analysis.pipeline as pipeline
    from app.analysis.dedup import MAX_FINDINGS

    workspace = snapshot_workspace(tmp_path, {"src/app.py": CLEAN_SOURCE})
    real_dedupe = pipeline.dedupe
    monkeypatch.setattr(
        pipeline, "dedupe", lambda findings, ws: (real_dedupe(findings, ws)[0], True)
    )

    result = pipeline.run_review(make_repo_job(workspace, mode="deterministic_only"))

    assert MAX_FINDINGS == 100
    assert (
        f"More than {MAX_FINDINGS} findings were found; the "
        f"{MAX_FINDINGS} shown are the most severe, spread across rules "
        "and files so no single one crowds out the rest." in result.summary
    )


def test_keyless_cap_note_is_exact():
    from app.analysis.pipeline import _repo_ai_note

    stats = ReviewStats(
        ai_model="gemini-3.6-flash", ai_capped=True, ai_files_covered=3, ai_files_total=10
    )

    assert _repo_ai_note(stats) == (
        "The AI reviewer read 3 of 10 reviewable files; without your own AI "
        "key, AI coverage is capped. Add your own AI key in Settings to lift "
        "the cap. The deterministic analyzers checked every reviewable file."
    )


def test_byok_partial_coverage_note_is_exact():
    from app.analysis.pipeline import _repo_ai_note

    stats = ReviewStats(ai_model="gemini-3.6-flash", ai_files_covered=5, ai_files_total=9)

    assert _repo_ai_note(stats) == (
        "The AI reviewer read 5 of 9 reviewable files; this repository is "
        "larger than one review can cover. The deterministic analyzers "
        "checked every reviewable file."
    )


def test_chunk_failure_note_is_exact():
    from app.analysis.pipeline import _repo_ai_note

    stats = ReviewStats(
        ai_model="gemini-3.6-flash",
        ai_files_covered=7,
        ai_files_total=7,
        ai_chunks_failed=2,
        ai_chunks_planned=7,
    )

    assert _repo_ai_note(stats) == (
        "The AI reviewer could not finish 2 of 7 passes over this repository; "
        "AI findings may be incomplete."
    )


def test_mock_provider_note_says_no_ai_ran():
    from app.analysis.pipeline import _repo_ai_note

    stats = ReviewStats(ai_model="mock", ai_files_covered=0, ai_files_total=4)

    assert _repo_ai_note(stats) == (
        "No AI reviewer is configured, so only deterministic checks ran."
    )


def test_full_coverage_gets_no_note():
    from app.analysis.pipeline import _repo_ai_note

    stats = ReviewStats(ai_model="gemini-3.6-flash", ai_files_covered=4, ai_files_total=4)

    assert _repo_ai_note(stats) is None


# --- analyzer argv fallback at snapshot scale ---


def test_ruff_argv_fallback_produces_identical_findings(tmp_path, monkeypatch):
    import app.analysis.analyzers.ruff_adapter as ruff_adapter
    from app.analysis.analyzers.ruff_adapter import RuffAnalyzer

    workspace = snapshot_workspace(
        tmp_path, {"flagged.py": "import os\n", "clean.py": CLEAN_SOURCE}
    )
    index = build_snapshot_index(workspace)

    listed = RuffAnalyzer().analyze(workspace, index)
    # The adapter reads the cap from its own module namespace at call time
    monkeypatch.setattr(ruff_adapter, "ANALYZER_ARGV_CHAR_CAP", 1)
    scanned = RuffAnalyzer().analyze(workspace, index)

    assert [f.model_dump() for f in listed] == [f.model_dump() for f in scanned]
    assert listed, "the fixture violation vanished; the comparison proves nothing"
    assert any(f.rule_id == "F401" for f in listed)


def test_a_changed_file_the_pipeline_never_saw_is_named_in_the_summary(tmp_path):
    """A review that skipped files still printed "the changed code passes all
    deterministic checks" underneath, which is a claim about code it never
    read."""
    from app.analysis.models import ReviewJob as AnalysisJob
    from app.analysis.pipeline import run_review

    (tmp_path / "kept.py").write_text("value = 1\n", encoding="utf-8")
    diff = (
        "diff --git a/kept.py b/kept.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/kept.py\n"
        "@@ -0,0 +1 @@\n"
        "+value = 1\n"
    )

    result = run_review(
        AnalysisJob(
            repo_full_name="octocat/alpha",
            pr_title="a change",
            base_sha="b" * 40,
            head_sha="h" * 40,
            diff_text=diff,
            workspace=tmp_path,
            files_not_reviewed=2,
        )
    )

    assert "2 changed files could not be reviewed" in result.summary
