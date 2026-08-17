import time
from pathlib import Path

from app.analysis.analyzers import test_detector
from app.analysis.analyzers.base import run_analyzers
from app.analysis.analyzers.mappings import map_ruff
from app.analysis.analyzers.ruff_adapter import RuffAnalyzer
from app.analysis.analyzers.secrets_adapter import SecretsAnalyzer
from app.analysis.diffs.parser import DiffIndex, FileDiff
from app.analysis.models import Finding


def make_index(*entries: tuple[str, set[int]]) -> DiffIndex:
    files = {path: FileDiff(path, None, "modified", changed, []) for path, changed in entries}
    return DiffIndex(files)


def put(workspace: Path, path: str, text: str) -> None:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# deliberately broken python, kept as file content so our own linters never parse it
BUGGY_SOURCE = (
    "import os\n"
    "import subprocess\n"
    "\n"
    'password = "hunter2"\n'
    "\n"
    "def helper(items=[]):\n"
    "    return undefined_name(items)\n"
    "\n"
    'subprocess.run("ls", shell=True)\n'
)

# the canonical AWS documentation example key, assembled so the full literal
# never sits in this repo's source
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def test_map_ruff_lookup_order():
    assert map_ruff("E902") == ("critical", "correctness", "high")
    assert map_ruff("F821") == ("high", "correctness", "high")
    assert map_ruff("F501") == ("medium", "correctness", "high")
    assert map_ruff("B006") == ("medium", "correctness", "high")
    assert map_ruff("B012") == ("medium", "correctness", "medium")
    assert map_ruff("S101") == ("info", "testing", "high")
    assert map_ruff("S110") == ("medium", "security", "medium")
    assert map_ruff("W291") == ("medium", "maintainability", "medium")


def test_ruff_maps_codes_and_filters_unchanged_lines(tmp_path):
    put(tmp_path, "src/app.py", BUGGY_SOURCE)
    index = make_index(("src/app.py", {4, 6, 7, 9}))

    findings = RuffAnalyzer().analyze(tmp_path, index)
    by_code = {f.rule_id: f for f in findings}

    # line 1 (unused import) is real but not part of the change
    assert "F401" not in by_code

    assert by_code["S105"].severity == "high"
    assert by_code["S105"].category == "security"
    assert by_code["S105"].confidence == "medium"
    assert by_code["S105"].start_line == 4

    assert by_code["B006"].severity == "medium"
    assert by_code["B006"].category == "correctness"
    assert by_code["B006"].recommendation is not None

    assert by_code["F821"].severity == "high"
    assert by_code["F821"].category == "correctness"
    assert by_code["F821"].start_line == 7

    assert by_code["S602"].severity == "high"
    assert by_code["S602"].category == "security"
    assert by_code["S602"].confidence == "high"

    for finding in findings:
        assert finding.tool == "ruff"
        assert finding.source == "deterministic"
        assert finding.file_path == "src/app.py"
        assert finding.title.startswith(f"{finding.rule_id}:")


def test_ruff_ignores_non_python_and_empty_index(tmp_path):
    put(tmp_path, "notes.md", "just prose\n")
    assert RuffAnalyzer().analyze(tmp_path, make_index(("notes.md", {1}))) == []
    assert RuffAnalyzer().analyze(tmp_path, DiffIndex()) == []


def test_secrets_flags_aws_key_without_leaking_it(tmp_path):
    put(tmp_path, "config/settings.py", f'aws_access_key_id = "{AWS_KEY}"\n')
    index = make_index(("config/settings.py", {1}))

    findings = SecretsAnalyzer().analyze(tmp_path, index)
    aws = [f for f in findings if f.rule_id == "AWSKeyDetector"]
    assert len(aws) == 1

    finding = aws[0]
    assert finding.severity == "critical"
    assert finding.category == "security"
    assert finding.confidence == "high"
    assert finding.start_line == 1
    assert finding.tool == "detect-secrets"
    assert finding.recommendation == "Rotate this credential and purge it from git history"

    for candidate in findings:
        for value in candidate.model_dump().values():
            assert AWS_KEY not in str(value)


def test_secrets_off_changed_line_downgrades_confidence(tmp_path):
    put(
        tmp_path,
        "config/settings.py",
        f'region = "us-east-1"\ntimeout = 30\naws_access_key_id = "{AWS_KEY}"\n',
    )
    index = make_index(("config/settings.py", {1}))

    findings = SecretsAnalyzer().analyze(tmp_path, index)
    aws = [f for f in findings if f.rule_id == "AWSKeyDetector"]
    assert len(aws) == 1
    assert aws[0].confidence == "medium"
    assert aws[0].severity == "critical"


def test_missing_tests_fires_on_untested_source_change(tmp_path):
    index = make_index(("app/service.py", {10, 11, 12}), ("app/util.py", {5}))

    findings = test_detector.TestDetector().analyze(tmp_path, index)
    assert len(findings) == 1

    finding = findings[0]
    assert finding.file_path == "app/service.py"
    assert finding.start_line is None
    assert finding.severity == "medium"
    assert finding.category == "testing"
    assert finding.tool == "missing-tests"
    assert finding.title == "Logic changes without accompanying test changes"


def test_missing_tests_quiet_when_tests_or_no_source_changed(tmp_path):
    detector = test_detector.TestDetector()

    with_tests = make_index(("app/service.py", {10}), ("tests/test_service.py", {3}))
    assert detector.analyze(tmp_path, with_tests) == []

    spec_style = make_index(("web/Button.tsx", {4}), ("web/Button.test.tsx", {9}))
    assert detector.analyze(tmp_path, spec_style) == []

    docs_only = make_index(("README.md", {1}))
    assert detector.analyze(tmp_path, docs_only) == []


class CrashingAnalyzer:
    name = "crashy"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        raise RuntimeError("boom")


class SlowAnalyzer:
    name = "slow"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        time.sleep(1)
        return []


class StubAnalyzer:
    name = "stub"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        return [
            Finding(
                file_path="a.py",
                severity="low",
                category="style",
                confidence="high",
                source="deterministic",
                title="stub finding",
            )
        ]


def test_run_analyzers_isolates_crashes(tmp_path):
    findings, ran, skipped = run_analyzers(
        [CrashingAnalyzer(), StubAnalyzer()], tmp_path, DiffIndex()
    )

    assert ran == ["stub"]
    assert [f.title for f in findings] == ["stub finding"]
    assert "RuntimeError" in skipped["crashy"]
    assert "boom" in skipped["crashy"]


def test_run_analyzers_times_out_slow_analyzer(tmp_path):
    findings, ran, skipped = run_analyzers(
        [SlowAnalyzer(), StubAnalyzer()], tmp_path, DiffIndex(), timeout_s=0.2
    )

    assert ran == ["stub"]
    assert len(findings) == 1
    assert "timed out" in skipped["slow"]
