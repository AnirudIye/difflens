"""The recorded AI response the demo replays.

These are not injected into the results. They enter the pipeline as provider
candidates, exactly where a live model's output enters it, and then go
through the whole chain: shape validation, reviewable-file check, real
location check, `touches_change`, fingerprinting, dedup, and the hybrid
merge. A candidate that cited a line the diff never touched would be
discarded here just as a hallucination would be, which is the point of
running them through rather than around.

Three of the four land within `MERGE_LINE_PAD` of a deterministic finding in
a mergeable category, so they merge and the finding becomes `hybrid` through
production code rather than by being labelled one. The merge keeps the
analyzer's title and appends the model's explanation, so the substance of
these candidates belongs in `explanation`; `recommendation` is dropped on
merge and is only carried by the candidate that stays AI-only.

Every bug described below is a real bug in `sample/files/`. The demo is free
and repeatable, and it is not fiction.
"""

# Re-exported so callers here have one obvious place to reach for it. The
# literal lives in the analysis package, which is what recognizes it.
from app.analysis.ai_review import DEMO_AI_MODEL

__all__ = ["DEMO_AI_MODEL", "DEMO_CANDIDATES"]

DEMO_CANDIDATES: list[dict] = [
    {
        # Merges with ruff S608 on the same line. ruff can see the f-string;
        # it cannot see that `day` is a request parameter.
        "file_path": "checkout/payments.py",
        "start_line": 11,
        "end_line": 11,
        "severity": "high",
        "category": "security",
        "confidence": "high",
        "title": "Day parameter is interpolated straight into SQL",
        "explanation": (
            "settlement_report takes day from the caller and formats it into the query "
            "text, so anything the caller sends becomes SQL. A day value of "
            "' OR '1'='1 returns every settlement in the table rather than one day's."
        ),
        "recommendation": (
            "Pass day as a bound parameter instead of formatting it into the string."
        ),
    },
    {
        # No deterministic finding is near line 29, so this one stays AI-only
        # and keeps its recommendation.
        "file_path": "checkout/payments.py",
        "start_line": 29,
        "end_line": 29,
        "severity": "high",
        "category": "correctness",
        "confidence": "high",
        "title": "refund_total returns an average, not a total",
        "explanation": (
            "The loop correctly skips lines that were already refunded, then the return "
            "divides by len(lines), which counts every line including the skipped ones. "
            "The function returns a per-line average rather than the total its name "
            "promises, and it raises ZeroDivisionError when lines is empty."
        ),
        "recommendation": (
            "Return total directly. If an average is genuinely wanted, divide by the "
            "number of lines actually added and handle the empty case."
        ),
    },
    {
        # Merges with eslint no-unreachable on line 20. The linter reports
        # that the line cannot run; this says what it costs.
        "file_path": "src/checkout.ts",
        "start_line": 19,
        "end_line": 19,
        "severity": "high",
        "category": "correctness",
        "confidence": "high",
        "title": "orderTotal returns before tax is applied",
        "explanation": (
            "The return on this line ends the function, so the tax multiplication below "
            "it never runs and every order total is short by the standard rate. The "
            "unreachable line is the symptom; the wrong total is the bug."
        ),
        "recommendation": "Apply the tax to sum before returning it.",
    },
    {
        # Merges with eslint no-eval on the same line.
        "file_path": "src/checkout.ts",
        "start_line": 24,
        "end_line": 24,
        "severity": "high",
        "category": "security",
        "confidence": "high",
        "title": "Promo codes are executed as JavaScript",
        "explanation": (
            "evaluatePromo passes its argument to eval, so a promo code is not data but "
            "code running with the privileges of whatever calls it. If a promo code can "
            "reach this from a request, so can arbitrary JavaScript."
        ),
        "recommendation": (
            "Parse the expression with a small arithmetic parser, or look the promo up "
            "in a table of known codes."
        ),
    },
]
