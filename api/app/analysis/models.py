"""Shared types for the analysis pipeline.

This package is pure: no database, no GitHub, no network at analysis time.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Severity = Literal["critical", "high", "medium", "low", "info"]
Category = Literal["correctness", "security", "performance", "maintainability", "testing", "style"]
Confidence = Literal["high", "medium", "low"]
Source = Literal["deterministic", "ai", "hybrid"]


class Finding(BaseModel):
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    severity: Severity
    category: Category
    confidence: Confidence
    source: Source
    title: str
    explanation: str | None = None
    recommendation: str | None = None
    rule_id: str | None = None
    tool: str | None = None
    fingerprint: str = ""  # filled by the dedup stage


class ReviewJob(BaseModel):
    repo_full_name: str
    pr_title: str = ""
    pr_body: str | None = None
    base_sha: str | None = None
    head_sha: str
    diff_text: str
    # directory holding head-SHA contents of changed files (or, for a
    # repository snapshot, the whole extracted tree), relative paths
    # mirroring the repo layout
    workspace: Path
    mode: Literal["deterministic_only", "cheap", "demo"] = "deterministic_only"
    target: Literal["pull_request", "repository"] = "pull_request"
    # Repository snapshots only. The worker decides the chunk cap from who
    # supplied the AI key; the pipeline stays policy-free and just obeys it.
    ai_chunk_cap: int | None = None
    ai_cap_reason: Literal["keyless"] | None = None
    # Whose key pays, so a provider rejection blames the party who can fix it
    ai_source: Literal["user", "server"] = "server"
    # Changed files the ingestion step could not hand to the analyzers at all
    # (GitHub returned no patch, or the file was too large to fetch). Counted
    # rather than dropped, because a review that silently skips a file still
    # prints "all deterministic checks" underneath.
    files_not_reviewed: int = 0


class ReviewStats(BaseModel):
    stage_durations_ms: dict[str, float] = {}
    analyzers_run: list[str] = []
    analyzers_skipped: dict[str, str] = {}
    findings_before_dedup: int = 0
    findings_after_dedup: int = 0
    truncated: bool = False
    tool_versions: dict[str, str] = {}
    # AI stage; all zero-valued when the mode is deterministic_only
    ai_model: str | None = None
    ai_refused: bool = False
    ai_parse_failed: bool = False
    ai_truncated: bool = False
    ai_skipped: str | None = None  # e.g. "diff_too_large"
    # The provider rejected the request in a way no retry fixes (bad key, bad
    # model id). Recorded rather than raised, so the deterministic findings
    # that were already computed still reach the user.
    ai_config_failed: bool = False
    ai_candidates: int = 0
    ai_discarded: dict[str, int] = {}
    # Repository snapshots: how much of the repo the AI actually read
    ai_files_total: int = 0
    ai_files_covered: int = 0
    ai_files_skipped_large: int = 0
    ai_chunks_planned: int = 0
    ai_chunks_run: int = 0
    ai_chunks_failed: int = 0
    ai_capped: bool = False


class ReviewResult(BaseModel):
    summary: str
    findings: list[Finding]
    stats: ReviewStats
