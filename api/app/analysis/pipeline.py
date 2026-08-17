"""Runs a ReviewJob through the parse, analyze, dedup, and summarize stages."""

import importlib.metadata
import platform
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

from app.analysis.analyzers.base import run_analyzers
from app.analysis.analyzers.ruff_adapter import RuffAnalyzer
from app.analysis.analyzers.secrets_adapter import SecretsAnalyzer
from app.analysis.analyzers.test_detector import TestDetector
from app.analysis.dedup import dedupe
from app.analysis.diffs.parser import build_diff_index
from app.analysis.models import Finding, ReviewJob, ReviewResult, ReviewStats

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


class AnalysisError(Exception):
    """The diff itself could not be parsed; nothing downstream can run."""


@contextmanager
def _timed(stats: ReviewStats, stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        stats.stage_durations_ms[stage] = (time.perf_counter() - start) * 1000


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summarize(findings: list[Finding]) -> str:
    if not findings:
        return "No findings. The changed code passes all deterministic checks."
    counts = Counter(finding.severity for finding in findings)
    breakdown = ", ".join(f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts[s])
    files = len({finding.file_path for finding in findings})
    return f"{_plural(len(findings), 'finding')} across {_plural(files, 'file')} ({breakdown})"


def _tool_versions() -> dict[str, str]:
    try:
        ruff = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        # a broken version lookup must never sink a review
        ruff = ""
    return {
        "ruff": ruff or "unknown",
        # detect_secrets 1.5.0 ships no __version__ attribute
        "detect-secrets": importlib.metadata.version("detect-secrets"),
        "python": platform.python_version(),
    }


def run_review(job: ReviewJob) -> ReviewResult:
    if job.mode != "deterministic_only":
        raise NotImplementedError("AI layer arrives Day 6")

    stats = ReviewStats()

    with _timed(stats, "parse"):
        try:
            index = build_diff_index(job.diff_text)
        except Exception as exc:
            raise AnalysisError(f"could not parse diff: {exc}") from exc

    with _timed(stats, "analyze"):
        analyzers = [RuffAnalyzer(), SecretsAnalyzer(), TestDetector()]
        findings, stats.analyzers_run, stats.analyzers_skipped = run_analyzers(
            analyzers, job.workspace, index
        )
    stats.findings_before_dedup = len(findings)

    with _timed(stats, "dedup"):
        findings, stats.truncated = dedupe(findings, job.workspace)
    stats.findings_after_dedup = len(findings)

    with _timed(stats, "summarize"):
        summary = _summarize(findings)

    stats.tool_versions = _tool_versions()
    return ReviewResult(summary=summary, findings=findings, stats=stats)
