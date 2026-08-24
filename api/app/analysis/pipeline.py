"""Runs a ReviewJob through the parse, analyze, AI, dedup, and summarize stages."""

import importlib.metadata
import platform
import secrets
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from app.analysis.ai_review import (
    DEMO_AI_MODEL,
    FINDINGS_SCHEMA,
    MAX_DIFF_CHARS,
    AIProvider,
    AIRequest,
    build_prompt,
    parse_candidates,
    validate_candidates,
)
from app.analysis.analyzers.base import run_analyzers
from app.analysis.analyzers.eslint_adapter import ESLintAnalyzer, eslint_version
from app.analysis.analyzers.ruff_adapter import RuffAnalyzer
from app.analysis.analyzers.secrets_adapter import SecretsAnalyzer
from app.analysis.analyzers.test_detector import TestDetector
from app.analysis.dedup import MAX_FINDINGS, dedupe
from app.analysis.diffs.parser import DiffIndex, build_diff_index
from app.analysis.diffs.snapshot import build_snapshot_index
from app.analysis.models import Finding, ReviewJob, ReviewResult, ReviewStats
from app.analysis.repo_review import run_repo_ai_stage

log = structlog.get_logger()

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

# A repository snapshot lints every file, not a 300-file diff; the harness
# and subprocess ceilings scale with it. ESLint's own timeout stays below the
# harness's so the subprocess dies with a real error instead of the harness
# abandoning its thread.
REPO_ANALYZER_TIMEOUT_S = 300
REPO_RUFF_TIMEOUT_S = 300
REPO_ESLINT_TIMEOUT_S = 270


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


def _summarize(
    findings: list[Finding], target: str = "pull_request", ran_any_analyzer: bool = True
) -> str:
    if not findings:
        if not ran_any_analyzer:
            # Zero findings because nothing ran is not a clean result, and
            # saying "passes all checks" when no check completed is the most
            # dangerous sentence this product could print.
            return (
                "No findings, but no analyzer finished, so nothing was actually "
                "checked. Run the review again."
            )
        if target == "repository":
            return "No findings. This repository came back clean at this commit."
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
        # Always present, even when it is "unavailable": which tools could
        # have run is as much a part of a review's provenance as which did
        "eslint": eslint_version(),
        "python": platform.python_version(),
    }


def _run_ai_stage(
    job: ReviewJob,
    provider: AIProvider,
    index: DiffIndex,
    workspace: Path,
    stats: ReviewStats,
) -> list[Finding]:
    """One provider call, then the hallucination chain over its output."""
    if len(job.diff_text) > MAX_DIFF_CHARS:
        # An unbounded diff means an unbounded prompt and an unbounded bill;
        # skip honestly and say so rather than truncating silently
        stats.ai_skipped = "diff_too_large"
        return []
    nonce = secrets.token_hex(8)
    system, user = build_prompt(job, nonce)
    response = provider.review(AIRequest(system=system, user=user, output_schema=FINDINGS_SCHEMA))
    stats.ai_model = response.model
    stats.ai_truncated = response.truncated
    if response.refused:
        stats.ai_refused = True
        return []
    candidates = parse_candidates(response.raw_text)
    if candidates is None:
        stats.ai_parse_failed = True
        return []
    stats.ai_candidates = len(candidates)
    findings, stats.ai_discarded = validate_candidates(candidates, index, workspace)
    return findings


def _config_failure_note(ai_source: str) -> str:
    """Names the failure and the one person who can clear it."""
    if ai_source == "user":
        return (
            "Your AI key was rejected, so only the deterministic checks ran. "
            "Check the key in Settings and run the review again."
        )
    return (
        "The AI reviewer is misconfigured, so only the deterministic checks "
        "ran. This one is for the operator to fix, not you."
    )


def _ai_note(stats: ReviewStats, ai_source: str = "server") -> str | None:
    """One honest sentence when the AI stage degraded; the user must be able
    to tell a clean AI pass from a suppressed one."""
    if stats.ai_config_failed:
        # First: a rejected key explains every other symptom below it, and
        # saying "no AI reviewer is configured" to someone who configured one
        # sends them to the wrong place
        return _config_failure_note(ai_source)
    if stats.ai_skipped == "diff_too_large":
        return "This diff is too large for the AI reviewer; only deterministic checks ran."
    if stats.ai_refused:
        return "The AI reviewer declined this diff; only deterministic checks ran."
    if stats.ai_parse_failed and stats.ai_truncated:
        return "The AI reviewer's output was cut short and unusable; only deterministic checks ran."
    if stats.ai_parse_failed:
        return "The AI reviewer's output was unusable; only deterministic checks ran."
    if stats.ai_truncated:
        return "The AI reviewer's output was cut short; its findings may be incomplete."
    if stats.ai_model == DEMO_AI_MODEL:
        # The public demo replays a recorded response. It is not a live
        # model and must not be presented as one, but it is also not the
        # empty mock below: it does return findings, so the mock's sentence
        # would be false here.
        return (
            "The AI reviewer in this demo replays a recorded review, so it costs "
            "nothing and returns the same findings every time."
        )
    if stats.ai_model == "mock":
        # Last, so a stubbed refusal or garbage response still reports the
        # specific failure. The mock answers with no findings and no error,
        # which would otherwise be indistinguishable from a clean AI pass
        return "No AI reviewer is configured, so only deterministic checks ran."
    return None


def _repo_ai_note(stats: ReviewStats, ai_source: str = "server") -> str | None:
    """Honest coverage sentences for a repository snapshot's AI stage."""
    if stats.ai_config_failed:
        return _config_failure_note(ai_source)
    if stats.ai_model == "mock":
        # The mock never fails a chunk, so unlike _ai_note this check can
        # come first: there is no specific failure for it to shadow
        return "No AI reviewer is configured, so only deterministic checks ran."
    notes: list[str] = []
    covered, total = stats.ai_files_covered, stats.ai_files_total
    if stats.ai_capped:
        notes.append(
            f"The AI reviewer read {covered} of {total} reviewable files; without "
            "your own AI key, AI coverage is capped. Add your own AI key in "
            "Settings to lift the cap. The deterministic analyzers checked every "
            "reviewable file."
        )
    elif covered < total and not stats.ai_chunks_failed:
        notes.append(
            f"The AI reviewer read {covered} of {total} reviewable files; this "
            "repository is larger than one review can cover. The deterministic "
            "analyzers checked every reviewable file."
        )
    if stats.ai_chunks_failed:
        notes.append(
            f"The AI reviewer could not finish {stats.ai_chunks_failed} of "
            f"{stats.ai_chunks_planned} passes over this repository; AI findings "
            "may be incomplete."
        )
    if stats.ai_truncated:
        notes.append("The AI reviewer's output was cut short; its findings may be incomplete.")
    return " ".join(notes) or None


def run_review(
    job: ReviewJob,
    provider: AIProvider | None = None,
    stop_check: Callable[[], str | None] | None = None,
    ai_config_errors: tuple[type[BaseException], ...] = (),
) -> ReviewResult:
    if job.mode != "deterministic_only" and provider is None:
        raise ValueError(f"mode {job.mode!r} requires an AI provider")
    snapshot = job.target == "repository"

    stats = ReviewStats()

    with _timed(stats, "parse"):
        if snapshot:
            index = build_snapshot_index(job.workspace)
        else:
            try:
                index = build_diff_index(job.diff_text)
            except Exception as exc:
                raise AnalysisError(f"could not parse diff: {exc}") from exc

    with _timed(stats, "analyze"):
        if snapshot:
            # No TestDetector: "changed logic without changed tests" is
            # meaningless when everything counts as changed, and it would
            # false-positive on every repository without a test file
            analyzers = [
                RuffAnalyzer(timeout_s=REPO_RUFF_TIMEOUT_S),
                SecretsAnalyzer(),
                ESLintAnalyzer(timeout_s=REPO_ESLINT_TIMEOUT_S),
            ]
            timeout_s = REPO_ANALYZER_TIMEOUT_S
        else:
            analyzers = [RuffAnalyzer(), SecretsAnalyzer(), TestDetector(), ESLintAnalyzer()]
            timeout_s = 60
        findings, stats.analyzers_run, stats.analyzers_skipped = run_analyzers(
            analyzers, job.workspace, index, timeout_s=timeout_s
        )

    if job.mode != "deterministic_only" and provider is not None:
        with _timed(stats, "ai"):
            try:
                if snapshot:
                    findings.extend(
                        run_repo_ai_stage(
                            job,
                            provider,
                            index,
                            stats,
                            stop_check=stop_check,
                            config_errors=ai_config_errors,
                        )
                    )
                else:
                    findings.extend(_run_ai_stage(job, provider, index, job.workspace, stats))
            except ai_config_errors as exc:
                # A rejected key or a bad model id is permanent, but it is not
                # a reason to throw away a review. The analyzers have already
                # run, and their findings are exactly as valid as they were a
                # moment ago; discarding them would hand someone with a stale
                # key nothing at all, when the product has a first-class state
                # for "the AI half did not run". The summary says what
                # happened and who has to fix it.
                stats.ai_config_failed = True
                log.warning("ai_stage_config_error", error=f"{type(exc).__name__}: {exc}")
    stats.findings_before_dedup = len(findings)

    with _timed(stats, "dedup"):
        findings, stats.truncated = dedupe(findings, job.workspace)
    stats.findings_after_dedup = len(findings)

    with _timed(stats, "summarize"):
        summary = _summarize(findings, job.target, ran_any_analyzer=bool(stats.analyzers_run))
        notes: list[str] = []
        if job.mode != "deterministic_only":
            note = (
                _repo_ai_note(stats, job.ai_source) if snapshot else _ai_note(stats, job.ai_source)
            )
            if note:
                notes.append(note)
        if stats.truncated:
            # Computed since day one and never surfaced; at repository scale
            # hitting the cap is routine rather than exotic, so it speaks now
            notes.append(
                f"More than {MAX_FINDINGS} findings were found; only the "
                f"{MAX_FINDINGS} most severe are shown."
            )
        for note in notes:
            # The counts sentence has no full stop of its own, so without
            # this the note runs straight on from it: "...2 low) The AI
            # reviewer...". Added here rather than in _summarize so the
            # summary reads the same with or without a note.
            joiner = " " if summary.endswith((".", "!", "?")) else ". "
            summary = f"{summary}{joiner}{note}"

    stats.tool_versions = _tool_versions()
    if stats.ai_model:
        stats.tool_versions["ai"] = stats.ai_model
    return ReviewResult(summary=summary, findings=findings, stats=stats)
