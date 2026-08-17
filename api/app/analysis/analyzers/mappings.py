"""Severity, category, and confidence tables for deterministic analyzers.

Lookup order: exact rule id, then longest matching prefix, then the tool default.
"""

from app.analysis.models import Category, Confidence, Severity

RuleMapping = tuple[Severity, Category, Confidence]

RUFF_EXACT: dict[str, RuleMapping] = {
    "F821": ("high", "correctness", "high"),
    "F823": ("high", "correctness", "high"),
    "F811": ("low", "maintainability", "high"),
    "F841": ("low", "maintainability", "high"),
    "F401": ("low", "maintainability", "high"),
    "B006": ("medium", "correctness", "high"),
    "B008": ("medium", "correctness", "high"),
    "S105": ("high", "security", "medium"),
    "S106": ("high", "security", "medium"),
    "S107": ("high", "security", "medium"),
    "S602": ("high", "security", "high"),
    "S604": ("high", "security", "high"),
    "S605": ("high", "security", "high"),
    "S608": ("high", "security", "medium"),
    "S501": ("high", "security", "high"),
    "S301": ("medium", "security", "high"),
    "S307": ("medium", "security", "high"),
    "S324": ("medium", "security", "high"),
    "S101": ("info", "testing", "high"),
}

RUFF_PREFIX: dict[str, RuleMapping] = {
    "E9": ("critical", "correctness", "high"),
    "F5": ("medium", "correctness", "high"),
    "F6": ("medium", "correctness", "high"),
    "F7": ("medium", "correctness", "high"),
    "B": ("medium", "correctness", "medium"),
    "S": ("medium", "security", "medium"),
}

RUFF_DEFAULT: RuleMapping = ("medium", "maintainability", "medium")

# these match known token shapes; everything else in detect-secrets is an
# entropy or keyword guess and gets one notch less confidence
STRUCTURED_SECRET_DETECTORS = {
    "AWSKeyDetector",
    "PrivateKeyDetector",
    "JwtTokenDetector",
    "StripeDetector",
    "GitHubTokenDetector",
    "BasicAuthDetector",
}


def map_ruff(code: str) -> RuleMapping:
    exact = RUFF_EXACT.get(code)
    if exact:
        return exact
    best = ""
    for prefix in RUFF_PREFIX:
        if code.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return RUFF_PREFIX[best] if best else RUFF_DEFAULT


def secret_confidence(detector: str) -> Confidence:
    return "high" if detector in STRUCTURED_SECRET_DETECTORS else "medium"
