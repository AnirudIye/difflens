import pytest

from app.analysis.dedup import MAX_FINDINGS, dedupe
from app.analysis.models import Finding


def make_finding(**overrides):
    base = dict(
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
