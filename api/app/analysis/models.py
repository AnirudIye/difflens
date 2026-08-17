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
    pr_title: str
    pr_body: str | None = None
    base_sha: str
    head_sha: str
    diff_text: str
    # directory holding head-SHA contents of changed files, relative paths
    # mirroring the repo layout
    workspace: Path
    mode: Literal["deterministic_only", "cheap", "demo"] = "deterministic_only"


class ReviewStats(BaseModel):
    stage_durations_ms: dict[str, float] = {}
    analyzers_run: list[str] = []
    analyzers_skipped: dict[str, str] = {}
    findings_before_dedup: int = 0
    findings_after_dedup: int = 0
    truncated: bool = False
    tool_versions: dict[str, str] = {}


class ReviewResult(BaseModel):
    summary: str
    findings: list[Finding]
    stats: ReviewStats
