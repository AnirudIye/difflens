from typing import Any

import pytest

from app.analysis.dedup import MAX_FINDINGS, dedupe
from app.analysis.models import Finding


def make_finding(**overrides: Any) -> Finding:
    base: dict[str, Any] = dict(
        file_path="src/app.py",
        start_line=10,
        end_line=10,
        severity="medium",
        category="correctness",
        confidence="medium",
        source="deterministic",
        title="a finding",
        rule_id="F841",
        tool="ruff",
    )
    base.update(overrides)
    return Finding(**base)


@pytest.fixture
def workspace(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("\n".join(f"line {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
    return tmp_path


def test_intra_run_keeps_max_severity(workspace):
    findings = [make_finding(severity="low"), make_finding(severity="critical")]
    result, truncated = dedupe(findings, workspace)
    assert len(result) == 1
    assert result[0].severity == "critical"
    assert result[0].fingerprint != ""
    assert truncated is False


def test_ai_merges_into_deterministic(workspace):
    det = make_finding(explanation="det expl", confidence="medium")
    ai = make_finding(
        tool=None,
        rule_id=None,
        source="ai",
        start_line=12,
        end_line=12,
        confidence="high",
        explanation="ai expl",
    )
    result, _ = dedupe([det, ai], workspace)
    assert len(result) == 1
    merged = result[0]
    assert merged.source == "hybrid"
    assert merged.tool == "ruff"
    assert merged.explanation is not None
    assert "det expl" in merged.explanation
    assert "ai expl" in merged.explanation
    assert merged.confidence == "high"


def test_ai_outside_line_pad_stays_separate(workspace):
    det = make_finding()
    ai = make_finding(tool=None, rule_id=None, source="ai", start_line=20, end_line=20)
    result, _ = dedupe([det, ai], workspace)
    assert len(result) == 2


def test_category_gate(workspace):
    det = make_finding(category="correctness")
    ai_style = make_finding(tool=None, rule_id=None, source="ai", category="style", start_line=11)
    result, _ = dedupe([det, ai_style], workspace)
    assert len(result) == 2

    det = make_finding(category="correctness")
    ai_sec = make_finding(tool=None, rule_id=None, source="ai", category="security", start_line=11)
    result, _ = dedupe([det, ai_sec], workspace)
    assert len(result) == 1
    assert result[0].source == "hybrid"


def test_ordering(workspace):
    findings = [
        make_finding(file_path="a.py", severity="info", start_line=1, rule_id="r1"),
        make_finding(file_path="z.py", severity="critical", start_line=5, rule_id="r2"),
        make_finding(file_path="a.py", severity="critical", start_line=None, rule_id="r3"),
        make_finding(file_path="a.py", severity="critical", start_line=3, rule_id="r4"),
    ]
    result, _ = dedupe(findings, workspace)
    ordered = [(f.severity, f.file_path, f.start_line) for f in result]
    assert ordered == [
        ("critical", "a.py", 3),
        ("critical", "a.py", None),
        ("critical", "z.py", 5),
        ("info", "a.py", 1),
    ]


def test_cap_and_truncated_flag(workspace):
    findings = [make_finding(file_path=f"m{i}.py") for i in range(150)]
    result, truncated = dedupe(findings, workspace)
    assert len(result) == MAX_FINDINGS
    assert truncated is True


def test_ai_merges_into_the_nearest_candidate_not_the_first(workspace):
    """An AI note must land on the finding it describes.

    Taking the first mergeable candidate put the model's explanation on
    whichever analyzer happened to run first: security and correctness are
    cross-mergeable, so a note about line 12 attached itself to a correctness
    finding on line 10 and was rendered under its title.
    """
    far = make_finding(start_line=10, end_line=10, category="correctness", title="far")
    near = make_finding(start_line=12, end_line=12, category="security", title="near")
    ai = make_finding(
        tool=None,
        rule_id=None,
        source="ai",
        start_line=12,
        end_line=12,
        category="security",
        title="ai note",
        explanation="describes line 12",
    )
    result, _ = dedupe([far, near, ai], workspace)
    by_title = {f.title: f for f in result}
    assert by_title["near"].source == "hybrid"
    assert by_title["near"].explanation is not None
    assert "describes line 12" in by_title["near"].explanation
    assert by_title["far"].source == "deterministic"


def test_a_range_containing_the_cited_line_beats_a_nearer_start_line(workspace):
    """Analyzers report real multi-line spans, and the span is what counts.

    ruff anchors an S608 at the start of a SQL statement while the
    interpolation the model describes can be several lines further down.
    Scored from start lines alone, that S608 reads as five lines away and
    loses to a closer point finding, so the SQL explanation is rendered
    under the wrong finding's title.
    """
    spanning = make_finding(
        start_line=7, end_line=12, category="security", title="spans the statement"
    )
    nearer_start = make_finding(
        start_line=14, end_line=14, category="security", title="point on line 14"
    )
    ai = make_finding(
        tool=None,
        rule_id=None,
        source="ai",
        start_line=12,
        end_line=12,
        category="security",
        title="ai note",
        explanation="about the statement",
    )
    result, _ = dedupe([spanning, nearer_start, ai], workspace)
    by_title = {f.title: f for f in result}
    # By start line the span is 5 away and the point is 2; by range the span
    # contains line 12 and the point does not.
    assert by_title["spans the statement"].source == "hybrid"
    assert by_title["point on line 14"].source == "deterministic"


def test_an_ambiguous_merge_is_declined_rather_than_guessed(workspace):
    """Two equally close candidates means the line numbers do not say which.

    This is the shape that misattributes explanations: a hardcoded credential
    on the same line as a SQL statement, both security, both touching the
    line the model cited. Merging either way risks rendering a note about SQL
    injection under "hardcoded credential", so neither is merged and the AI
    finding keeps its own title.
    """
    spanning = make_finding(
        start_line=7, end_line=12, category="security", title="spans the statement"
    )
    point = make_finding(start_line=12, end_line=12, category="security", title="credential")
    ai = make_finding(
        tool=None,
        rule_id=None,
        source="ai",
        start_line=12,
        end_line=12,
        category="security",
        title="ai note",
        explanation="about the statement",
    )
    result, _ = dedupe([spanning, point, ai], workspace)
    by_title = {f.title: f for f in result}
    assert by_title["spans the statement"].source == "deterministic"
    assert by_title["credential"].source == "deterministic"
    assert by_title["ai note"].source == "ai"
    assert by_title["ai note"].explanation == "about the statement"
