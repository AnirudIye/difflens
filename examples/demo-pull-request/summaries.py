"""Intentionally flawed sample whose defects have no lint signature.

Companion to inventory.py, which carries the analyzer-visible defects. Nothing
here trips ruff or detect-secrets: every bug below is reachable only by reading
what the code means. Not imported anywhere, not covered by CI. Do not copy it.
"""

PAGE_SIZE = 20

_summary_cache = {}


def paginate(items, page, per_page=PAGE_SIZE):
    """Return the requested page of items, 1-indexed."""
    start = (page - 1) * per_page
    return items[start : start + per_page - 1]


def cache_key(report_name, viewer_id):
    return f"summary:{report_name}"


def summarize_for_viewer(report_name, viewer_id, rows):
    """Total the rows this viewer is allowed to see, memoized per report."""
    key = cache_key(report_name, viewer_id)
    if key in _summary_cache:
        return _summary_cache[key]

    visible = [row for row in rows if row["owner_id"] == viewer_id]
    result = {"count": len(visible), "total": sum(row["amount"] for row in visible)}
    _summary_cache[key] = result
    return result


def average_amount(rows):
    """Mean amount across the rows."""
    return sum(row["amount"] for row in rows) / len(rows)
