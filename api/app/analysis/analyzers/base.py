"""Analyzer protocol and the harness that runs analyzers in isolation."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from app.analysis.diffs.parser import DiffIndex
from app.analysis.models import Finding

# Above this many characters of file arguments, subprocess analyzers scan the
# workspace root instead of splatting paths onto argv: Windows caps a command
# line around 32K characters, and a repository snapshot can hold thousands of
# paths. Output-side filtering keeps the findings identical either way.
ANALYZER_ARGV_CHAR_CAP = 100_000

# What a linter subprocess needs to boot, and nothing else. The worker's
# environment holds real secrets (the Fernet key, the database URL, AI keys),
# and a repository's ruff config can point `extend` at any readable path,
# including the child's own /proc/self/environ; an allowlisted environment
# makes that read worthless. Every entry is a path, a locale, or a Windows
# process-boot variable; none is a secret.
CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)


def child_env() -> dict[str, str]:
    """Allowlisted environment for linter subprocesses."""
    return {key: value for key, value in os.environ.items() if key.upper() in CHILD_ENV_ALLOWLIST}


class Analyzer(Protocol):
    name: str

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]: ...


def run_analyzers(
    analyzers: list[Analyzer],
    workspace: Path,
    index: DiffIndex,
    timeout_s: float = 60,
) -> tuple[list[Finding], list[str], dict[str, str]]:
    """Run every analyzer, isolating failures so one bad tool cannot sink the review."""
    findings: list[Finding] = []
    analyzers_run: list[str] = []
    analyzers_skipped: dict[str, str] = {}

    pool = ThreadPoolExecutor(max_workers=max(len(analyzers), 1))
    futures = [
        (analyzer.name, pool.submit(analyzer.analyze, workspace, index)) for analyzer in analyzers
    ]
    for name, future in futures:
        try:
            findings.extend(future.result(timeout=timeout_s))
            analyzers_run.append(name)
        except TimeoutError:
            analyzers_skipped[name] = f"timed out after {timeout_s:g}s"
        except Exception as exc:
            analyzers_skipped[name] = f"{type(exc).__name__}: {exc}"
    # ponytail: a hung analyzer thread is abandoned, not killed; fine while every
    # analyzer is subprocess- or file-bound, revisit if one ever holds a lock
    pool.shutdown(wait=False, cancel_futures=True)
    return findings, analyzers_run, analyzers_skipped
