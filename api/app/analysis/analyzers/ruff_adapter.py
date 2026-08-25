"""Adapter that shells out to ruff and maps its JSON output onto Findings."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import structlog

from app.analysis.analyzers.base import ANALYZER_ARGV_CHAR_CAP, child_env
from app.analysis.analyzers.mappings import map_ruff
from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.validator import is_reviewable, touches_change
from app.analysis.models import Finding

log = structlog.get_logger()

RUFF_SELECT = "E9,F,B,S"
RUFF_TIMEOUT_S = 60

# The root files that make a tree "a repository with its own ruff config".
# Worker ingestion fetches these names into a pull request workspace so both
# review kinds see the same switch.
RUFF_CONFIG_FILES = ("ruff.toml", ".ruff.toml", "pyproject.toml")


def has_root_ruff_config(workspace: Path) -> bool:
    """True when the workspace root carries a ruff config of its own.

    Root-only on purpose: with a root config present, ruff's per-file upward
    config search can never walk past the workspace into whatever the temp
    directory's parents hold. A pyproject.toml counts only with a [tool.ruff]
    table, because ruff itself skips one without it and keeps walking up.
    """
    if (workspace / "ruff.toml").is_file() or (workspace / ".ruff.toml").is_file():
        return True
    pyproject = workspace / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return False
    try:
        return "ruff" in tomllib.loads(text).get("tool", {})
    except Exception:
        # Unparseable, but the repo clearly meant to configure ruff: honor
        # the intent so the failure is the visible repo-config path rather
        # than our rules silently running while the repo's own CI fails
        return "[tool.ruff" in text


def _relative_path(filename: str, root: Path) -> str:
    # ruff echoes absolute paths even when handed relative ones
    path = Path(filename)
    if path.is_absolute():
        path = path.relative_to(root)
    return path.as_posix()


class RuffAnalyzer:
    name = "ruff"

    def __init__(self, timeout_s: float = RUFF_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        # Read by the pipeline after the run, the same way it reads
        # SecretsAnalyzer.suppressed, so the summary can say whose rules ran
        self.used_repo_config = False
        self.repo_config_failed = False

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        files = [
            path
            for path in sorted(index.files)
            if path.endswith(".py") and is_reviewable(index, workspace, path)
        ]
        if not files:
            return []
        if sum(len(path) + 1 for path in files) > ANALYZER_ARGV_CHAR_CAP:
            # Scan the workspace root instead; touches_change drops any
            # finding on a path outside the index, so the output is identical
            files = ["."]

        repo_config = has_root_ruff_config(workspace)
        result = self._run(workspace, files, repo_config=repo_config)
        if repo_config and result.returncode not in (0, 1):
            # The repo's config would not run (a broken table, an extend
            # pointing nowhere, a required-version we do not carry). Falling
            # back to the bundled rules keeps this repo no worse off than
            # before the feature; the flag makes the summary say so.
            log.warning("ruff_repo_config_failed", stderr=result.stderr.strip()[:2000])
            self.repo_config_failed = True
            result = self._run(workspace, files, repo_config=False)
        # exit 1 just means ruff found violations
        if result.returncode not in (0, 1):
            raise RuntimeError(f"ruff exited {result.returncode}: {result.stderr.strip()}")
        self.used_repo_config = repo_config and not self.repo_config_failed
        return self._findings_from(result.stdout, workspace.resolve(), index)

    def _run(
        self, workspace: Path, files: list[str], repo_config: bool
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "ruff", "check", "--output-format", "json"]
        if repo_config:
            # The repo configured ruff itself, so its own selects, ignores
            # and excludes govern, exactly as its own CI would run. --no-fix
            # because a config can set fix = true and a reviewer must never
            # mutate the tree it is reviewing; --force-exclude because the
            # paths are passed explicitly, which otherwise exempts them from
            # the repo's own exclude lists.
            command += ["--no-cache", "--no-fix", "--force-exclude"]
        else:
            # No config of its own: the bundled selection, and --isolated so
            # nothing outside the workspace can supply one
            command += ["--select", RUFF_SELECT, "--no-cache", "--isolated"]
        command += [
            # Everything after this is a path, never an option, in both
            # modes. A pull request can add a file whose NAME is a flag, and
            # argv does not know the difference: without the separator, a
            # file called "--ignore=S105.py" is parsed as one and the pull
            # request chooses which rules run.
            "--",
            *files,
        ]
        return subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_s,
            env=child_env(),
        )

    def _findings_from(self, stdout: str, root: Path, index: DiffIndex) -> list[Finding]:
        findings: list[Finding] = []
        for item in json.loads(stdout):
            # ruff reports syntax errors with a null code
            code = item["code"] or "E999"
            path = _relative_path(item["filename"], root)
            start = item["location"]["row"]
            end = item["end_location"]["row"]
            if not touches_change(index, path, start, end):
                continue
            severity, category, confidence = map_ruff(code)
            fix = item.get("fix")
            findings.append(
                Finding(
                    file_path=path,
                    start_line=start,
                    end_line=end,
                    severity=severity,
                    category=category,
                    confidence=confidence,
                    source="deterministic",
                    title=f"{code}: {item['message']}",
                    explanation=item["message"],
                    recommendation=fix["message"] if fix else None,
                    rule_id=code,
                    tool="ruff",
                )
            )
        return findings
