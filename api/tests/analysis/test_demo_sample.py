"""The demo sample and the recorded response it is reviewed with.

The failure this file exists to catch: someone edits a file under
`app/demo/sample/files/`, every line below the edit shifts, and the recorded
AI candidates in `candidates.py` now cite lines that no longer hold what they
describe. The validation chain would silently discard them and the demo would
quietly lose its AI half, still looking like a healthy review. So the
discard counters are asserted to be zero rather than the finding count alone.
"""

import tempfile
from pathlib import Path

import pytest

from app.ai.mock import MockProvider
from app.analysis.ai_review import DEMO_AI_MODEL
from app.analysis.analyzers.eslint_adapter import eslint_is_available
from app.analysis.diffs.parser import build_diff_index
from app.analysis.models import ReviewJob
from app.analysis.pipeline import run_review
from app.demo import sample
from app.demo.candidates import DEMO_CANDIDATES

needs_eslint = pytest.mark.skipif(
    not eslint_is_available(),
    reason="the bundled eslint runtime is not installed",
)


def run_demo_pipeline():
    with tempfile.TemporaryDirectory(prefix="difflens-demo-test-") as tmp:
        workspace = Path(tmp)
        sample.populate_workspace(workspace)
        return run_review(
            ReviewJob(
                repo_full_name=sample.REPO_FULL_NAME,
                pr_title=sample.PR_TITLE,
                base_sha=sample.BASE_SHA,
                head_sha=sample.HEAD_SHA,
                diff_text=sample.build_diff(),
                workspace=workspace,
                mode="demo",
            ),
            provider=MockProvider(DEMO_CANDIDATES, model=DEMO_AI_MODEL),
        )


def test_sample_has_files():
    files = dict(sample.sample_files())
    assert "checkout/payments.py" in files
    assert "src/checkout.ts" in files


def test_sample_files_are_lf_and_newline_terminated():
    # The repository normalizes with `* text=auto`, so these are CRLF in a
    # Windows working tree. The demo must hash the same everywhere.
    for _path, text in sample.sample_files():
        assert "\r" not in text
        assert text.endswith("\n")


def test_build_diff_parses():
    index = build_diff_index(sample.build_diff())
    assert set(index.files) == {"checkout/payments.py", "src/checkout.ts"}


def test_diff_and_workspace_agree():
    """Every path the diff names exists in the workspace, with the same lines.

    A diff that disagreed with the workspace would fail every AI candidate on
    `location_exists` and produce a demo with no AI findings at all.
    """
    index = build_diff_index(sample.build_diff())
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        sample.populate_workspace(workspace)
        for path, text in sample.sample_files():
            assert path in index.files
            written = (workspace / path).read_text(encoding="utf-8")
            assert written == text


def test_build_diff_is_stable():
    assert sample.build_diff() == sample.build_diff()


def test_every_recorded_candidate_cites_a_real_location():
    """No candidate is discarded. This is the drift alarm."""
    result = run_demo_pipeline()
    assert result.stats.ai_candidates == len(DEMO_CANDIDATES)
    assert result.stats.ai_discarded == {
        "invalid_shape": 0,
        "unreviewable_file": 0,
        "bad_location": 0,
        "outside_change": 0,
    }


def test_demo_reports_its_own_model_not_mock():
    result = run_demo_pipeline()
    assert result.stats.ai_model == DEMO_AI_MODEL
    assert result.stats.ai_model != "mock"


def test_demo_summary_says_the_review_is_replayed():
    result = run_demo_pipeline()
    assert "replays a recorded review" in result.summary
    # The mock's sentence would be false here: this stage does return findings
    assert "No AI reviewer is configured" not in result.summary
    # The note is a sentence of its own, not a run-on from the counts
    assert ") The AI reviewer" not in result.summary
    assert "). The AI reviewer" in result.summary


def test_demo_ai_stage_did_not_degrade():
    stats = run_demo_pipeline().stats
    assert not stats.ai_refused
    assert not stats.ai_parse_failed
    assert not stats.ai_truncated
    assert stats.ai_skipped is None


@needs_eslint
def test_demo_golden_shape():
    """The demo's headline numbers, which the screenshots and the page show."""
    result = run_demo_pipeline()
    by_source: dict[str, int] = {}
    for finding in result.findings:
        by_source[finding.source] = by_source.get(finding.source, 0) + 1

    assert len(result.findings) == 13
    assert by_source == {"deterministic": 9, "hybrid": 3, "ai": 1}

    by_severity: dict[str, int] = {}
    for finding in result.findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
    assert by_severity == {"critical": 1, "high": 8, "medium": 2, "low": 2}


@needs_eslint
def test_all_four_analyzers_contribute():
    result = run_demo_pipeline()
    tools = {finding.tool for finding in result.findings}
    assert {"ruff", "detect-secrets", "eslint", "missing-tests"} <= tools


@needs_eslint
def test_hybrid_findings_land_on_the_right_lines():
    """The AI explanation must attach to the finding it describes.

    Merging picked the first mergeable candidate before this was fixed, and
    security and correctness are cross-mergeable, so the SQL injection note
    for line 11 attached itself to the mutable-default finding on line 9 and
    was rendered under its title.
    """
    result = run_demo_pipeline()
    hybrids = {
        (finding.file_path, finding.start_line)
        for finding in result.findings
        if finding.source == "hybrid"
    }
    assert hybrids == {
        ("checkout/payments.py", 11),
        ("src/checkout.ts", 20),
        ("src/checkout.ts", 24),
    }

    sql = next(
        f for f in result.findings if f.file_path == "checkout/payments.py" and f.start_line == 11
    )
    assert "S608" in sql.title
    assert sql.explanation is not None and "becomes SQL" in sql.explanation

    mutable_default = next(
        f for f in result.findings if f.file_path == "checkout/payments.py" and f.start_line == 9
    )
    assert mutable_default.source == "deterministic"


def test_ai_only_finding_keeps_its_recommendation():
    """A finding that merges loses its recommendation; one that does not, keeps it."""
    result = run_demo_pipeline()
    ai_only = [f for f in result.findings if f.source == "ai"]
    assert len(ai_only) == 1
    assert ai_only[0].file_path == "checkout/payments.py"
    assert ai_only[0].recommendation


def test_sample_loader_ignores_compiled_artefacts(tmp_path, monkeypatch):
    """A stray .pyc beside the sample must not take the demo down.

    `pip install .` byte-compiles the sample, because it is Python that
    happens to be data, and the installed copy ends up with __pycache__
    inside it. Reading that as UTF-8 raises UnicodeDecodeError, so a loader
    that globbed everything would fail on the packaged copy while working
    perfectly from the source tree.
    """
    root = tmp_path / "files"
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00\x01\x02\xff\xfe")
    (root / "stray.pyc").write_bytes(b"\x00\x01\x02\xff\xfe")
    monkeypatch.setattr(sample, "FILES_DIR", root)

    loaded = sample.sample_files()
    assert [path for path, _ in loaded] == ["pkg/mod.py"]


def test_sample_files_are_sorted_by_posix_path(tmp_path, monkeypatch):
    """Path objects compare case-insensitively on Windows and case-sensitively
    elsewhere, which would order the diff differently on the two platforms the
    demo is supposed to be identical on."""
    root = tmp_path / "files"
    (root / "b").mkdir(parents=True)
    (root / "A").mkdir(parents=True)
    (root / "A" / "one.py").write_text("a = 1\n", encoding="utf-8")
    (root / "b" / "two.py").write_text("b = 2\n", encoding="utf-8")
    monkeypatch.setattr(sample, "FILES_DIR", root)

    assert [path for path, _ in sample.sample_files()] == ["A/one.py", "b/two.py"]
