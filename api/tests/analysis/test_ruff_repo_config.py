"""The repository's own ruff configuration governs the ruff stage.

A tree whose root carries a ruff config (ruff.toml, .ruff.toml, or a
pyproject.toml with a [tool.ruff] table) is linted the way its own CI would
lint it: the repo's selects, ignores, per-file-ignores and excludes apply.
Without one, the bundled command runs exactly as before. Design record:
docs/superpowers/specs/2026-08-25-honor-repo-ruff-config-design.md.
"""

import subprocess
from pathlib import Path

from app.analysis.analyzers.ruff_adapter import RuffAnalyzer, has_root_ruff_config
from app.analysis.diffs.snapshot import build_snapshot_index
from app.analysis.models import ReviewJob
from app.analysis.pipeline import run_review


def put(workspace: Path, path: str, text: str) -> None:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# F401 (unused import) plus S105 (hardcoded password) under the bundled select
BUGGY = 'import os\npassword = "hunter2"\n'


def test_root_ruff_toml_is_config(tmp_path):
    put(tmp_path, "ruff.toml", "")
    assert has_root_ruff_config(tmp_path)


def test_root_dot_ruff_toml_is_config(tmp_path):
    put(tmp_path, ".ruff.toml", "")
    assert has_root_ruff_config(tmp_path)


def test_pyproject_with_tool_ruff_table_is_config(tmp_path):
    put(tmp_path, "pyproject.toml", '[tool.ruff.lint]\nselect = ["F401"]\n')
    assert has_root_ruff_config(tmp_path)


def test_pyproject_without_tool_ruff_is_not_config(tmp_path):
    # ruff itself skips a pyproject with no [tool.ruff] table and keeps
    # walking upward, which is exactly the escape root-only detection exists
    # to prevent; such a tree must stay in bundled mode
    put(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    assert not has_root_ruff_config(tmp_path)


def test_nested_config_alone_is_not_config(tmp_path):
    put(tmp_path, "pkg/ruff.toml", "")
    assert not has_root_ruff_config(tmp_path)


def test_empty_tree_is_not_config(tmp_path):
    assert not has_root_ruff_config(tmp_path)


def test_unparseable_pyproject_naming_tool_ruff_counts_as_config(tmp_path):
    # The repo clearly meant to configure ruff; the honest outcome is the
    # visible repo-config failure path, not silently linting with our rules
    # while their own CI fails
    put(tmp_path, "pyproject.toml", "[tool.ruff.lint\nselect = [")
    assert has_root_ruff_config(tmp_path)


def test_repo_select_governs_which_rules_run(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", '[lint]\nselect = ["F401"]\n')

    findings = RuffAnalyzer().analyze(tmp_path, build_snapshot_index(tmp_path))
    codes = {f.rule_id for f in findings}

    assert "F401" in codes
    # the bundled select would report the hardcoded password; the repo's
    # config never enabled S, and the repo's config is what governs now
    assert "S105" not in codes


def test_repo_per_file_ignores_are_honored(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "legacy/old.py", BUGGY)
    put(
        tmp_path,
        "ruff.toml",
        '[lint]\nselect = ["F401", "S105"]\n[lint.per-file-ignores]\n"legacy/*" = ["S105"]\n',
    )

    findings = RuffAnalyzer().analyze(tmp_path, build_snapshot_index(tmp_path))
    by_file = {(f.file_path, f.rule_id) for f in findings}

    assert ("app.py", "S105") in by_file
    assert ("legacy/old.py", "S105") not in by_file
    assert ("legacy/old.py", "F401") in by_file


def test_repo_exclude_applies_to_explicitly_passed_files(tmp_path):
    # The adapter passes explicit paths, which ruff exempts from excludes
    # unless --force-exclude; the repo's CI behaviour is that excluded means
    # excluded, so repo-config mode must carry the flag
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "generated/g.py", BUGGY)
    put(tmp_path, "ruff.toml", 'extend-exclude = ["generated"]\n[lint]\nselect = ["F401"]\n')

    findings = RuffAnalyzer().analyze(tmp_path, build_snapshot_index(tmp_path))

    assert {f.file_path for f in findings} == {"app.py"}


def test_bundled_command_is_unchanged_without_a_config(tmp_path, monkeypatch):
    put(tmp_path, "app.py", BUGGY)
    command = _capture_command(tmp_path, monkeypatch)

    assert "--isolated" in command
    assert "--select" in command
    assert "--no-fix" not in command
    assert "--force-exclude" not in command
    assert "--" in command


def test_repo_config_command_drops_the_bundled_rules(tmp_path, monkeypatch):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", '[lint]\nselect = ["F401"]\n')
    command = _capture_command(tmp_path, monkeypatch)

    assert "--isolated" not in command
    assert "--select" not in command
    # a repo config can set fix = true, and a reviewer must never mutate the
    # tree it is reviewing
    assert "--no-fix" in command
    assert "--force-exclude" in command
    # the argv-injection separator is mode-independent
    assert "--" in command


def test_broken_repo_config_falls_back_to_bundled_rules(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", 'extend = "does-not-exist.toml"\n')

    analyzer = RuffAnalyzer()
    findings = analyzer.analyze(tmp_path, build_snapshot_index(tmp_path))

    # hard-skipping ruff would leave a repo with a broken config worse off
    # than before this feature existed; the bundled rules run instead
    assert {"F401", "S105"} <= {f.rule_id for f in findings}
    assert analyzer.repo_config_failed
    assert not analyzer.used_repo_config


def test_working_repo_config_reports_itself(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", '[lint]\nselect = ["F401"]\n')

    analyzer = RuffAnalyzer()
    analyzer.analyze(tmp_path, build_snapshot_index(tmp_path))

    assert analyzer.used_repo_config
    assert not analyzer.repo_config_failed


def test_bundled_mode_reports_no_repo_config(tmp_path):
    put(tmp_path, "app.py", BUGGY)

    analyzer = RuffAnalyzer()
    analyzer.analyze(tmp_path, build_snapshot_index(tmp_path))

    assert not analyzer.used_repo_config
    assert not analyzer.repo_config_failed


def test_secrets_never_reach_the_ruff_child_environment(tmp_path, monkeypatch):
    # A repo config's `extend` can point at any readable path, including the
    # child's own /proc/self/environ; an allowlisted environment makes that
    # read worthless
    put(tmp_path, "app.py", BUGGY)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "canary-value")
    captured: dict = {}

    class Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    RuffAnalyzer().analyze(tmp_path, build_snapshot_index(tmp_path))

    env = captured["env"]
    assert env is not None
    assert all(key.upper() != "TOKEN_ENCRYPTION_KEY" for key in env)
    assert any(key.upper() == "PATH" for key in env)


def snapshot_job(workspace: Path) -> ReviewJob:
    return ReviewJob(
        repo_full_name="octocat/footyboard",
        head_sha="b" * 40,
        diff_text="",
        workspace=workspace,
        target="repository",
    )


def test_pipeline_reports_repository_config_in_summary_and_stats(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", '[lint]\nselect = ["F401"]\n')

    result = run_review(snapshot_job(tmp_path))

    assert result.stats.ruff_config_source == "repository"
    assert not result.stats.ruff_repo_config_failed
    assert "ruff ran with the repository's own ruff configuration." in result.summary


def test_pipeline_reports_the_fallback_when_repo_config_is_broken(tmp_path):
    put(tmp_path, "app.py", BUGGY)
    put(tmp_path, "ruff.toml", 'extend = "does-not-exist.toml"\n')

    result = run_review(snapshot_job(tmp_path))

    assert result.stats.ruff_config_source == "bundled"
    assert result.stats.ruff_repo_config_failed
    assert (
        "The repository's ruff configuration could not be used; "
        "ruff ran with DiffLens's default rules instead." in result.summary
    )


def test_pipeline_stays_silent_without_a_repo_config(tmp_path):
    put(tmp_path, "app.py", BUGGY)

    result = run_review(snapshot_job(tmp_path))

    assert result.stats.ruff_config_source == "bundled"
    assert not result.stats.ruff_repo_config_failed
    assert "ruff configuration" not in result.summary


def _capture_command(tmp_path, monkeypatch) -> list[str]:
    captured: dict = {}

    class Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    RuffAnalyzer().analyze(tmp_path, build_snapshot_index(tmp_path))
    return captured["command"]
