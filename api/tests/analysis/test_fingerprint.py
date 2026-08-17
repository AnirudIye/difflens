from app.analysis.dedup import fingerprint
from app.analysis.models import Finding


def make_finding(**overrides):
    base = dict(
        file_path="src/app.py",
        start_line=5,
        end_line=5,
        severity="medium",
        category="correctness",
        confidence="high",
        source="deterministic",
        title="unused variable",
        rule_id="F841",
        tool="ruff",
    )
    base.update(overrides)
    return Finding(**base)


def test_stable_across_line_number_drift():
    lines_before = [f"filler {i}" for i in range(4)] + ["x = compute(y)"]
    lines_after = [f"other {i}" for i in range(14)] + ["x = compute(y)"]
    fp_before = fingerprint(make_finding(start_line=5), lines_before)
    fp_after = fingerprint(make_finding(start_line=15), lines_after)
    assert fp_before == fp_after


def test_whitespace_insensitive():
    fp_indented = fingerprint(make_finding(start_line=1), ["    x = compute(y)"])
    fp_spaced = fingerprint(make_finding(start_line=1), ["x  =  compute(y)"])
    assert fp_indented == fp_spaced


def test_occurrence_disambiguates_identical_lines():
    lines = ["cursor.execute(q)", "other()", "cursor.execute(q)"]
    fp_first = fingerprint(make_finding(start_line=1), lines)
    fp_second = fingerprint(make_finding(start_line=3), lines)
    assert fp_first != fp_second


def test_rule_scoped():
    lines = ["import os"]
    fp_a = fingerprint(make_finding(start_line=1, rule_id="F401"), lines)
    fp_b = fingerprint(make_finding(start_line=1, rule_id="E402"), lines)
    assert fp_a != fp_b


def test_ai_findings_use_category_as_rule():
    lines = ["password = input()"]
    ai = dict(tool=None, rule_id=None, source="ai", start_line=1)
    fp_sec = fingerprint(make_finding(**ai, category="security"), lines)
    fp_corr = fingerprint(make_finding(**ai, category="correctness"), lines)
    assert fp_sec != fp_corr


def test_file_level_ignores_content():
    finding = make_finding(start_line=None, end_line=None)
    fp_empty = fingerprint(finding, [])
    fp_lines = fingerprint(finding, ["anything", "at all"])
    assert fp_empty == fp_lines
    assert len(fp_empty) == 16
    int(fp_empty, 16)
