"""Adapter that shells out to ruff and maps its JSON output onto Findings."""

import json
import subprocess
import sys
from pathlib import Path

from app.analysis.analyzers.mappings import map_ruff
from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.validator import is_reviewable, touches_change
from app.analysis.models import Finding

RUFF_SELECT = "E9,F,B,S"
RUFF_TIMEOUT_S = 60


def _relative_path(filename: str, root: Path) -> str:
    # ruff echoes absolute paths even when handed relative ones
    path = Path(filename)
    if path.is_absolute():
        path = path.relative_to(root)
    return path.as_posix()


class RuffAnalyzer:
    name = "ruff"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        files = [
            path
            for path in sorted(index.files)
            if path.endswith(".py") and is_reviewable(index, workspace, path)
        ]
        if not files:
            return []

        # --isolated so the target repo's own ruff config cannot change what we scan
        command = [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "json",
            "--select",
            RUFF_SELECT,
            "--no-cache",
            "--isolated",
            # Everything after this is a path, never an option. A pull
            # request can add a file whose NAME is a flag, and argv does not
            # know the difference: without the separator, a file called
            # "--ignore=S105.py" is parsed as one and the pull request chooses which rules run.
            "--",
            *files,
        ]
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUFF_TIMEOUT_S,
        )
        # exit 1 just means ruff found violations
        if result.returncode not in (0, 1):
            raise RuntimeError(f"ruff exited {result.returncode}: {result.stderr.strip()}")

        root = workspace.resolve()
        findings: list[Finding] = []
        for item in json.loads(result.stdout):
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
