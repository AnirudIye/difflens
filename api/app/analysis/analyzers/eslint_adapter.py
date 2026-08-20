"""Adapter that shells out to ESLint and maps its JSON output onto Findings.

The same shape as the ruff adapter, with one extra problem: ESLint does not
ship with the analyzer the way ruff does. It lives in eslint-runtime/ beside
the Python package, installed by npm, and is reached by running node against
its entrypoint directly rather than through npm or npx, both of which would
resolve config and packages relative to the workspace being reviewed. That
workspace is attacker-authored.

If node or that runtime is missing, this analyzer raises and run_analyzers
records it under analyzers_skipped. Nothing else about the review changes,
which is the point: TypeScript support is an addition to a Python reviewer,
not a dependency of it.
"""

import json
import shutil
import subprocess
from pathlib import Path

from app.analysis.analyzers.mappings import map_eslint
from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.validator import is_reviewable, touches_change
from app.analysis.models import Finding

ESLINT_TIMEOUT_S = 90

# What the bundled config has a parser for. .vue and .svelte need their own
# parsers and are deliberately out of scope.
ESLINT_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")

# app/analysis/analyzers/ -> app/analysis/ -> app/ -> api/, which is /srv in
# the container, where the Dockerfile puts the same directory
RUNTIME_DIR = Path(__file__).resolve().parents[3] / "eslint-runtime"

# Reported when a file will not parse at all; ESLint gives those no rule id
PARSE_ERROR_RULE = "parse-error"


class ESLintUnavailable(RuntimeError):
    """Node or the bundled ESLint install is not present."""


def _eslint_entrypoint(runtime: Path) -> Path:
    return runtime / "node_modules" / "eslint" / "bin" / "eslint.js"


def _resolve(runtime: Path | None) -> Path:
    # Read at call time, never as a default argument: a default is bound when
    # the function is defined, which puts the location out of reach of tests
    # and of anything that relocates the runtime after import
    return runtime if runtime is not None else RUNTIME_DIR


def eslint_is_available(runtime: Path | None = None) -> bool:
    root = _resolve(runtime)
    return shutil.which("node") is not None and _eslint_entrypoint(root).is_file()


def eslint_version(runtime: Path | None = None) -> str:
    """Read the installed version off disk rather than asking the binary.

    A subprocess per review to learn a number that is fixed at image build
    time is not worth the 200ms, and this runs on every review.
    """
    root = _resolve(runtime)
    if not eslint_is_available(root):
        return "unavailable"
    manifest = root / "node_modules" / "eslint" / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except Exception:
        # a broken version lookup must never sink a review
        return "unknown"


class ESLintAnalyzer:
    name = "eslint"

    def __init__(self, runtime: Path | None = None) -> None:
        self.runtime = _resolve(runtime)

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        files = [
            path
            for path in sorted(index.files)
            if path.endswith(ESLINT_SUFFIXES) and is_reviewable(index, workspace, path)
        ]
        if not files:
            # Checked before node is: a Python-only pull request must not be
            # told that a JavaScript linter is missing
            return []

        node = shutil.which("node")
        entrypoint = _eslint_entrypoint(self.runtime)
        if node is None or not entrypoint.is_file():
            raise ESLintUnavailable(
                f"node {'found' if node else 'missing'}; "
                f"eslint at {entrypoint} {'found' if entrypoint.is_file() else 'missing'}"
            )

        command = [
            node,
            str(entrypoint),
            # The reviewed repository's own config and plugins are untrusted
            # input. --no-config-lookup is this adapter's --isolated.
            "--no-config-lookup",
            "--config",
            str(self.runtime / "eslint.config.mjs"),
            "--format",
            "json",
            # A changed file inside an ignored directory is skipped quietly
            # rather than reported as a warning carrying no rule
            "--no-warn-ignored",
            *files,
        ]
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ESLINT_TIMEOUT_S,
        )
        # 0 clean, 1 lint problems found, 2 ESLint itself failed
        if result.returncode not in (0, 1):
            raise RuntimeError(f"eslint exited {result.returncode}: {result.stderr.strip()[:500]}")

        return findings_from(result.stdout, workspace, index)


def _relative_path(file_path: str, root: Path) -> str:
    path = Path(file_path)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            # Outside the workspace, so not a file this review is about
            return ""
    return path.as_posix()


def findings_from(stdout: str, workspace: Path, index: DiffIndex) -> list[Finding]:
    root = workspace.resolve()
    findings: list[Finding] = []
    for entry in json.loads(stdout or "[]"):
        path = _relative_path(entry["filePath"], root)
        if not path:
            continue
        for message in entry.get("messages", []):
            finding = _finding_from(message, path, index)
            if finding is not None:
                findings.append(finding)
    return findings


def _finding_from(message: dict, path: str, index: DiffIndex) -> Finding | None:
    # A file that will not parse produces one message with no rule id
    fatal = bool(message.get("fatal"))
    rule_id = message.get("ruleId") or (PARSE_ERROR_RULE if fatal else None)
    if rule_id is None:
        # Neither a rule nor a parse failure means ESLint is talking about its
        # own configuration, not about the code under review
        return None

    start = message.get("line")
    if start is None:
        return None
    end = message.get("endLine") or start
    if not touches_change(index, path, start, end):
        return None

    severity, category, confidence = map_eslint(rule_id)
    text = (message.get("message") or "").strip()
    return Finding(
        file_path=path,
        start_line=start,
        end_line=end,
        severity=severity,
        category=category,
        confidence=confidence,
        source="deterministic",
        title=f"{rule_id}: {text}",
        explanation=text,
        rule_id=rule_id,
        tool="eslint",
    )
