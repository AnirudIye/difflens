"""The ESLint adapter: what it reports, what it refuses to read, and how it
disappears when Node is not installed.

The last one carries the most weight. ESLint is the only analyzer that is not
a Python dependency, so the interesting failure is not a wrong finding, it is
a Python review that dies because a JavaScript linter is absent.
"""

import json
import shutil
from pathlib import Path

import pytest

from app.analysis.analyzers import eslint_adapter
from app.analysis.analyzers.eslint_adapter import (
    ESLintAnalyzer,
    ESLintUnavailable,
    eslint_is_available,
    eslint_version,
    findings_from,
)
from app.analysis.analyzers.mappings import ESLINT_DEFAULT, map_eslint
from app.analysis.diffs.parser import build_diff_index
from app.analysis.models import ReviewJob
from app.analysis.pipeline import run_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

needs_eslint = pytest.mark.skipif(
    not eslint_is_available(),
    reason="node and eslint-runtime/node_modules are required; run npm ci in api/eslint-runtime",
)


def workspace_with(tmp_path: Path, files: dict[str, str], changed: dict[str, range] | None = None):
    """Write files, and build a diff that claims every line of them is new.

    Passing `changed` narrows the diff to a line range, which is how the
    "only report inside the diff" rule gets tested.
    """
    chunks = []
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        window = changed.get(rel) if changed else None
        span = window or range(1, len(lines) + 1)
        first, last = span[0], span[-1]
        body = "".join(f"+{line}\n" for line in lines[first - 1 : last])
        chunks.append(
            f"diff --git a/{rel} b/{rel}\n"
            "index 1111111..2222222 100644\n"
            f"--- a/{rel}\n"
            f"+++ b/{rel}\n"
            f"@@ -{first},0 +{first},{last - first + 1} @@\n"
            f"{body}"
        )
    return tmp_path, build_diff_index("".join(chunks))


@needs_eslint
def test_reports_the_defects_in_a_typescript_file(tmp_path):
    workspace, index = workspace_with(
        tmp_path,
        {
            "src/checkout.ts": (
                "export function rate(kind: string): number {\n"
                "  const table = { standard: 0.2, standard: 0.15 };\n"
                "  return table[kind as keyof typeof table];\n"
                "}\n"
            )
        },
    )
    findings = ESLintAnalyzer().analyze(workspace, index)

    assert [(f.rule_id, f.start_line) for f in findings] == [("no-dupe-keys", 2)]
    assert findings[0].severity == "high"
    assert findings[0].category == "correctness"
    assert findings[0].tool == "eslint"
    assert findings[0].source == "deterministic"


@needs_eslint
def test_a_defect_outside_the_diff_is_not_reported(tmp_path):
    # The line with the bug is real, but nobody in this pull request touched
    # it, and a reviewer that comments on untouched code gets muted.
    workspace, index = workspace_with(
        tmp_path,
        {
            # Exported, because an unused top-level function is itself a
            # finding and would drown out the rule this test is about
            "src/legacy.js": (
                "export function old() {\n"
                "  return eval('1 + 1');\n"
                "}\n"
                "export function fresh() {\n"
                "  return 2;\n"
                "}\n"
            )
        },
        changed={"src/legacy.js": range(4, 7)},
    )
    assert ESLintAnalyzer().analyze(workspace, index) == []


@needs_eslint
def test_a_file_that_will_not_parse_is_a_critical_finding(tmp_path):
    workspace, index = workspace_with(
        tmp_path, {"src/broken.ts": "export function half( {\n  return 1;\n"}
    )
    findings = ESLintAnalyzer().analyze(workspace, index)

    assert len(findings) == 1
    assert findings[0].rule_id == "parse-error"
    assert findings[0].severity == "critical"
    assert findings[0].category == "correctness"


@needs_eslint
def test_the_reviewed_repository_cannot_change_the_rules(tmp_path):
    """A pull request that adds an ESLint config must not disarm the reviewer.

    Everything in the workspace is attacker-authored. A config file that
    turns every rule off is the cheapest possible attack on a linter, and
    --no-config-lookup is what stops it.
    """
    workspace, index = workspace_with(
        tmp_path,
        {
            "src/sneaky.js": "export function go() {\n  return eval('2');\n}\n",
            "eslint.config.mjs": "export default [{ rules: {} }];\n",
        },
    )
    findings = ESLintAnalyzer().analyze(workspace, index)
    assert [f.rule_id for f in findings] == ["no-eval"]


@needs_eslint
@pytest.mark.parametrize(
    "path",
    ["src/cart.test.js", "src/cart.test.ts", "src/__tests__/cart.js", "tests/cart.js"],
)
def test_the_test_file_globals_do_not_become_findings(tmp_path, path):
    """describe/it/expect belong to a runner nobody installed in a workspace
    built from the contents API.

    Reporting them as undefined names would put a high-severity correctness
    finding on every test file in the pull request, burying the real ones. The
    .js cases are the ones that matter: no-undef is already off for .ts.
    """
    workspace, index = workspace_with(
        tmp_path,
        {
            path: (
                'describe("total", () => {\n'
                "  beforeEach(() => { jest.resetModules(); });\n"
                '  it("adds up", () => {\n'
                "    expect(1 + 1).toBe(2);\n"
                "  });\n"
                "});\n"
            )
        },
    )
    assert ESLintAnalyzer().analyze(workspace, index) == []


@needs_eslint
def test_typescript_syntax_is_not_reported_as_a_defect(tmp_path):
    """Base ESLint rules do not understand TypeScript.

    no-redeclare sees an overload signature as a redeclaration, and the base
    no-unused-vars does not count a type-only import as a use. Both are handed
    to the typescript-eslint versions, which do understand.
    """
    workspace, index = workspace_with(
        tmp_path,
        {
            "src/api.ts": (
                'import type { Widget } from "./widget";\n'
                "\nexport function find(id: string): Widget;\n"
                "export function find(id: number): Widget;\n"
                "export function find(id: string | number): Widget {\n"
                "  return { id } as unknown as Widget;\n"
                "}\n"
                "\nexport class Store {\n"
                "  get(id: string): Widget;\n"
                "  get(id: number): Widget;\n"
                "  get(id: string | number): Widget {\n"
                "    return find(id as string);\n"
                "  }\n"
                "}\n"
            )
        },
    )
    assert ESLintAnalyzer().analyze(workspace, index) == []


def test_a_python_only_diff_never_looks_for_node(tmp_path, monkeypatch):
    """The important one. ESLint is optional; Python review is not."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    workspace, index = workspace_with(tmp_path, {"app/main.py": "x = 1\n"})

    assert ESLintAnalyzer().analyze(workspace, index) == []


def test_a_javascript_diff_without_node_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(eslint_adapter.shutil, "which", lambda _name: None)
    workspace, index = workspace_with(tmp_path, {"src/app.js": "const x = 1;\n"})

    with pytest.raises(ESLintUnavailable, match="node missing"):
        ESLintAnalyzer().analyze(workspace, index)


def test_a_missing_runtime_says_so(tmp_path):
    workspace, index = workspace_with(tmp_path, {"src/app.js": "const x = 1;\n"})

    with pytest.raises(ESLintUnavailable, match="missing"):
        ESLintAnalyzer(runtime=tmp_path / "nowhere").analyze(workspace, index)


def test_a_missing_runtime_skips_the_analyzer_rather_than_failing_the_review(tmp_path, monkeypatch):
    """Through the pipeline, because that is where it matters.

    An ESLint install that vanished on a redeploy must cost the JavaScript
    findings and nothing else.
    """
    monkeypatch.setattr(eslint_adapter, "RUNTIME_DIR", tmp_path / "nowhere")
    result = run_review(
        ReviewJob(
            repo_full_name="difflens/fixture",
            pr_title="fixture typescript_buggy",
            base_sha="a" * 40,
            head_sha="b" * 40,
            diff_text=(FIXTURES / "typescript_buggy" / "pr.diff").read_text(encoding="utf-8"),
            workspace=FIXTURES / "typescript_buggy" / "files",
        )
    )

    assert "eslint" not in result.stats.analyzers_run
    assert "ESLintUnavailable" in result.stats.analyzers_skipped["eslint"]
    assert "ruff" in result.stats.analyzers_run
    assert result.summary  # the review still produced one


def test_findings_are_dropped_when_eslint_talks_about_itself(tmp_path):
    # A message with no rule and no parse failure is ESLint commenting on its
    # own configuration. That is our problem, not the pull request author's.
    _workspace, index = workspace_with(tmp_path, {"src/app.js": "const x = 1;\n"})
    stdout = json.dumps(
        [
            {
                "filePath": str(tmp_path / "src" / "app.js"),
                "messages": [
                    {"ruleId": None, "message": "File ignored by default.", "line": 0},
                    {"ruleId": "no-eval", "message": "eval can be harmful.", "line": 1},
                ],
            }
        ]
    )
    findings = findings_from(stdout, tmp_path, index)
    assert [f.rule_id for f in findings] == ["no-eval"]


def test_a_path_outside_the_workspace_is_ignored(tmp_path):
    _workspace, index = workspace_with(tmp_path, {"src/app.js": "const x = 1;\n"})
    stdout = json.dumps(
        [
            {
                "filePath": "/etc/passwd",
                "messages": [{"ruleId": "no-eval", "message": "nope", "line": 1}],
            }
        ]
    )
    assert findings_from(stdout, tmp_path, index) == []


def test_unmapped_rules_fall_back_rather_than_crash():
    assert map_eslint("some-rule-nobody-mapped") == ESLINT_DEFAULT
    assert map_eslint("no-eval") == ("high", "security", "high")
    assert map_eslint("no-unused-vars") == ("low", "maintainability", "high")
    assert map_eslint("parse-error") == ("critical", "correctness", "high")


def test_the_version_is_reported_either_way(tmp_path):
    assert eslint_version(tmp_path / "nowhere") == "unavailable"
    if eslint_is_available():
        assert eslint_version()[0].isdigit()
