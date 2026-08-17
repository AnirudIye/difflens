"""Analyzer protocol and the harness that runs analyzers in isolation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from app.analysis.diffs.parser import DiffIndex
from app.analysis.models import Finding


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
