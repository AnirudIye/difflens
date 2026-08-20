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

# ESLint rule ids are words, not codes, so there is no prefix hierarchy to
# lean on the way ruff's has. The table stays short on purpose: only rules
# that eslint-runtime/eslint.config.mjs turns on can ever appear here.
ESLINT_EXACT: dict[str, RuleMapping] = {
    # a file that will not parse is not a style opinion
    "parse-error": ("critical", "correctness", "high"),
    "no-undef": ("high", "correctness", "high"),
    "no-dupe-keys": ("high", "correctness", "high"),
    "no-dupe-args": ("high", "correctness", "high"),
    "no-dupe-class-members": ("high", "correctness", "high"),
    "no-duplicate-case": ("high", "correctness", "high"),
    "no-unreachable": ("high", "correctness", "high"),
    "valid-typeof": ("high", "correctness", "high"),
    "no-unsafe-negation": ("high", "correctness", "high"),
    "no-unsafe-optional-chaining": ("high", "correctness", "high"),
    "use-isnan": ("high", "correctness", "high"),
    "no-self-assign": ("medium", "correctness", "high"),
    "no-self-compare": ("medium", "correctness", "high"),
    "no-cond-assign": ("medium", "correctness", "medium"),
    "no-constant-condition": ("medium", "correctness", "medium"),
    "no-sparse-arrays": ("medium", "correctness", "medium"),
    "no-fallthrough": ("medium", "correctness", "medium"),
    "no-compare-neg-zero": ("medium", "correctness", "high"),
    "no-async-promise-executor": ("medium", "correctness", "high"),
    "no-promise-executor-return": ("medium", "correctness", "medium"),
    "no-unmodified-loop-condition": ("medium", "correctness", "medium"),
    "require-atomic-updates": ("medium", "correctness", "medium"),
    # the eval family: arbitrary code built from a string
    "no-eval": ("high", "security", "high"),
    "no-implied-eval": ("high", "security", "high"),
    "no-new-func": ("high", "security", "high"),
    "no-script-url": ("high", "security", "high"),
    "no-unused-vars": ("low", "maintainability", "high"),
    # The typescript-eslint replacements, which run in place of the base
    # rules on .ts files because the base ones misread TypeScript syntax
    "@typescript-eslint/no-unused-vars": ("low", "maintainability", "high"),
    "@typescript-eslint/no-dupe-class-members": ("high", "correctness", "high"),
    "no-debugger": ("low", "maintainability", "high"),
}

ESLINT_DEFAULT: RuleMapping = ("medium", "correctness", "medium")

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


def map_eslint(rule_id: str) -> RuleMapping:
    return ESLINT_EXACT.get(rule_id, ESLINT_DEFAULT)


def secret_confidence(detector: str) -> Confidence:
    return "high" if detector in STRUCTURED_SECRET_DETECTORS else "medium"
