"""Content-anchored fingerprints and cross-analyzer merging of findings."""

import hashlib
from pathlib import Path

from app.analysis.models import Finding

MAX_FINDINGS = 100
MERGE_LINE_PAD = 2
# security and correctness overlap enough that cross-category merges are safe
MERGEABLE_CATEGORIES = {"security", "correctness"}

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _normalize(line: str) -> str:
    return " ".join(line.split())


def fingerprint(finding: Finding, file_lines: list[str]) -> str:
    # anchored to line content, not line numbers, so it survives drift
    tool = finding.tool or "ai"
    rule = finding.rule_id or finding.category
    if finding.start_line is None:
        anchor, occurrence = "file-level", 0
    else:
        idx = finding.start_line - 1
        if 0 <= idx < len(file_lines):
            anchor = _normalize(file_lines[idx])
            occurrence = sum(1 for line in file_lines[:idx] if _normalize(line) == anchor)
        else:
            anchor, occurrence = "", 0
    key = f"{tool}|{rule}|{finding.file_path}|{anchor}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _mergeable(ai_finding: Finding, candidate: Finding) -> bool:
    if candidate.source not in ("deterministic", "hybrid"):
        return False
    if ai_finding.file_path != candidate.file_path:
        return False
    if ai_finding.start_line is None or candidate.start_line is None:
        return False
    ai_end = ai_finding.end_line or ai_finding.start_line
    det_end = candidate.end_line or candidate.start_line
    if ai_finding.start_line - MERGE_LINE_PAD > det_end:
        return False
    if candidate.start_line - MERGE_LINE_PAD > ai_end:
        return False
    if ai_finding.category == candidate.category:
        return True
    return {ai_finding.category, candidate.category} <= MERGEABLE_CATEGORIES


def _merge_target(ai_finding: Finding, candidates: list[Finding]) -> Finding | None:
    """The deterministic finding an AI finding belongs to, or None.

    Two rules, and the second matters as much as the first:

    Closest wins, measured against the candidate's whole range rather than
    its first line. Analyzers report real multi-line spans: ruff anchors an
    S608 SQL injection at the start of the statement while the interpolation
    the model describes can be several lines further down. Measured from
    start lines, that S608 loses to any unrelated point finding sitting on
    the model's own line.

    A tie merges nothing. Taking the first mergeable candidate put the
    model's explanation on whichever analyzer happened to run first, and
    since security and correctness are cross-mergeable that could graft a
    note about SQL injection onto a hardcoded-credential finding and render
    it under that title. When two candidates are equally close, the line
    numbers do not say which one the model meant, so the honest answer is to
    leave the AI finding standing on its own rather than to guess. It costs
    a hybrid and buys never attributing an explanation to the wrong bug.
    """
    matches = [c for c in candidates if _mergeable(ai_finding, c)]
    if not matches:
        return None
    ai_line = ai_finding.start_line or 0

    def rank(candidate: Finding) -> tuple[int, bool]:
        start = candidate.start_line or 0
        end = candidate.end_line or start
        gap = max(start - ai_line, ai_line - end, 0)  # 0 when the range contains it
        return gap, candidate.category != ai_finding.category

    best = min(rank(c) for c in matches)
    winners = [c for c in matches if rank(c) == best]
    if len(winners) != 1:
        return None
    return winners[0]


def _sort_key(finding: Finding) -> tuple:
    return (
        _SEVERITY_RANK[finding.severity],
        finding.category,
        finding.file_path,
        finding.start_line is None,
        finding.start_line or 0,
    )


def dedupe(findings: list[Finding], workspace: Path) -> tuple[list[Finding], bool]:
    # ponytail: mutates the passed findings in place; the pipeline owns them
    lines_cache: dict[str, list[str] | None] = {}

    def file_lines(path: str) -> list[str] | None:
        if path not in lines_cache:
            try:
                text = (workspace / path).read_text(encoding="utf-8", errors="replace")
                lines_cache[path] = text.splitlines()
            except OSError:
                lines_cache[path] = None
        return lines_cache[path]

    for finding in findings:
        lines = file_lines(finding.file_path)
        if lines is None:
            # unreadable workspace file: fall back to a file-level anchor
            finding.fingerprint = fingerprint(finding.model_copy(update={"start_line": None}), [])
        else:
            finding.fingerprint = fingerprint(finding, lines)

    by_fp: dict[str, Finding] = {}
    for finding in findings:
        kept = by_fp.get(finding.fingerprint)
        if kept is None or _SEVERITY_RANK[finding.severity] < _SEVERITY_RANK[kept.severity]:
            by_fp[finding.fingerprint] = finding

    merged = [f for f in by_fp.values() if f.source != "ai"]
    for ai_finding in [f for f in by_fp.values() if f.source == "ai"]:
        target = _merge_target(ai_finding, merged)
        if target is None:
            merged.append(ai_finding)
            continue
        target.source = "hybrid"
        if ai_finding.explanation:
            if target.explanation:
                target.explanation = f"{target.explanation}\n\n{ai_finding.explanation}"
            else:
                target.explanation = ai_finding.explanation
        if _CONFIDENCE_RANK[ai_finding.confidence] < _CONFIDENCE_RANK[target.confidence]:
            target.confidence = ai_finding.confidence

    merged.sort(key=_sort_key)
    return merged[:MAX_FINDINGS], len(merged) > MAX_FINDINGS
