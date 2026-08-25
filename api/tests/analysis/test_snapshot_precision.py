"""Precision of a repository snapshot review, against a realistic project.

This file exists because of a measured failure. A repository review of a real
project returned exactly 100 findings, the cap: 56 critical, 3 high, 41
medium. Four were real, and all four came from the AI. Every one of the other
96 came from the deterministic analyzers, and the causes were mundane:
fake passwords in test fixtures, a placeholder connection string in
`.env.example`, the base32 alphabet that every TOTP implementation contains,
and two ESLint rules that fire on idiomatic async code.

Nothing in the suite caught it because every fixture was a small curated pull
request diff. A pull request rarely touches twenty test files, and
`touches_change` discarded anything away from the changed lines anyway. A
snapshot marks every line as changed, so that filter vanished and the
analyzers spoke about the whole repository at once.

The workspace below is therefore shaped like a real project rather than a
diff: source, a test suite that holds fake credentials on purpose, an example
env file, and a TOTP module. Two properties are asserted together, and they
pull against each other on purpose: the noise must be gone, and a genuinely
leaked key must still be found.
"""

from pathlib import Path

from app.analysis.analyzers.secrets_adapter import SecretsAnalyzer
from app.analysis.diffs.parser import DiffIndex
from app.analysis.diffs.snapshot import build_snapshot_index

# Amazon's own documentation key. Not a live credential, and the shape is what
# AWSKeyDetector matches, which is the point: this stands in for a real leak.
PLANTED_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

REALISTIC_REPO = {
    # Production code that happens to contain an encoding alphabet
    "server/src/totp.js": (
        "const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';\n"
        "export function decode(input) {\n"
        "  return input.split('').map((c) => BASE32_ALPHABET.indexOf(c));\n"
        "}\n"
    ),
    # A file whose whole job is to hold placeholders
    "server/.env.example": (
        "DATABASE_URL=postgresql://user:password@localhost:5432/app\nSESSION_SECRET=replace-me\n"
    ),
    # Tests holding fake credentials, which is what tests do
    "server/src/auth.test.js": (
        "const user = { email: 'a@b.com', password: 'test-password-123' };\n"
        "const adminSecret = 'super-secret-value-for-tests';\n"
        "test('signs in', async () => {\n"
        "  await new Promise((r) => setTimeout(r, 10));\n"
        "  expect(user.password).toBeTruthy();\n"
        "});\n"
    ),
    "src/__tests__/session.spec.ts": (
        "const token = 'abcdef0123456789abcdef0123456789';\n"
        "it('works', () => { expect(token).toHaveLength(32); });\n"
    ),
    # Ordinary production source with nothing wrong with it
    "server/src/util.js": "export const add = (a, b) => a + b;\n",
}


def _build(tmp_path: Path, files: dict[str, str]) -> tuple[Path, DiffIndex]:
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path, build_snapshot_index(tmp_path)


def test_a_realistic_repository_produces_no_secret_noise(tmp_path):
    """The exact shape that produced 56 false criticals must produce none."""
    workspace, index = _build(tmp_path, REALISTIC_REPO)
    analyzer = SecretsAnalyzer()

    findings = analyzer.analyze(workspace, index)

    assert findings == [], f"expected no secret findings, got {[f.file_path for f in findings]}"
    # Held back, not silently dropped: the reviewer is told the count
    assert analyzer.suppressed > 0


def test_a_real_key_is_still_found_in_production_code(tmp_path):
    """The other half of the contract. Quieting fixtures must not go so far
    that an actual leaked credential stops being reported."""
    files = dict(REALISTIC_REPO)
    files["server/src/config.js"] = f"export const accessKeyId = '{PLANTED_AWS_KEY}';\n"
    workspace, index = _build(tmp_path, files)

    findings = SecretsAnalyzer().analyze(workspace, index)

    assert len(findings) == 1, [f.file_path for f in findings]
    assert findings[0].file_path == "server/src/config.js"
    assert findings[0].severity == "critical"
    assert findings[0].rule_id == "AWSKeyDetector"


def test_a_real_key_inside_a_test_file_is_still_reported(tmp_path):
    """A live key does not become harmless by sitting in a fixture. It is
    reported one notch down rather than suppressed, because the file it is in
    is a reason to look twice, not a reason to look away."""
    files = dict(REALISTIC_REPO)
    files["server/src/leak.test.js"] = f"const key = '{PLANTED_AWS_KEY}';\n"
    workspace, index = _build(tmp_path, files)

    findings = SecretsAnalyzer().analyze(workspace, index)

    assert [f.file_path for f in findings] == ["server/src/leak.test.js"]
    assert findings[0].severity == "medium"


def test_an_encoding_alphabet_in_production_code_is_not_a_secret(tmp_path):
    """The base32 constant is in production code, so no path rule saves it.
    It is quiet because an alphabet uses each symbol once and a random secret
    of that length almost never does."""
    workspace, index = _build(
        tmp_path, {"server/src/totp.js": REALISTIC_REPO["server/src/totp.js"]}
    )

    findings = SecretsAnalyzer().analyze(workspace, index)

    assert findings == []


def test_a_line_that_only_mentions_credentials_is_not_one(tmp_path):
    """The keyword detector fires on an identifier. `currentPassword` in a
    type check quotes only the word "string", which is neither long enough
    nor unusual enough to be a secret."""
    workspace, index = _build(
        tmp_path,
        {
            "server/src/routes/auth.js": (
                "export function change(currentPassword) {\n"
                "  if (typeof currentPassword !== 'string' || !currentPassword) {\n"
                "    throw new Error('bad input');\n"
                "  }\n"
                "}\n"
            )
        },
    )

    assert SecretsAnalyzer().analyze(workspace, index) == []


def test_a_value_that_calls_itself_a_placeholder_is_believed(tmp_path):
    workspace, index = _build(
        tmp_path,
        {"server/src/env.js": "const PLACEHOLDER_SECRET = 'not-a-real-value-here';\n"},
    )

    assert SecretsAnalyzer().analyze(workspace, index) == []


def test_a_local_default_connection_string_is_not_a_leak(tmp_path):
    workspace, index = _build(
        tmp_path,
        {
            "server/src/env.js": "const DEFAULT_URL = 'postgresql://app:devpass@localhost:5432/app';\n"
        },
    )

    assert SecretsAnalyzer().analyze(workspace, index) == []


def test_the_same_connection_string_against_a_real_host_still_reports(tmp_path):
    """The localhost rule is about where it points, not about the shape. Move
    the host off this machine and it is a credential again.

    Note the host. example.com and example.net are reserved for documentation
    by RFC 2606, so they really are placeholders and the analyzer is right to
    ignore them; a test that used one to mean "a real host" would be asserting
    the opposite of what it claims.
    """
    workspace, index = _build(
        tmp_path,
        {
            "server/src/env.js": "const URL = 'postgresql://app:devpass@db.internal.acme-corp.net:5432/app';\n"
        },
    )

    findings = SecretsAnalyzer().analyze(workspace, index)

    assert [f.rule_id for f in findings] == ["BasicAuthDetector"]
    assert findings[0].severity == "high"


def test_a_hardcoded_password_in_a_script_still_reports(tmp_path):
    """The refinements must not talk their way out of a real one."""
    workspace, index = _build(
        tmp_path, {"server/scripts/pg.js": "const PASSWORD = 'hunter2-actual-value';\n"}
    )

    findings = SecretsAnalyzer().analyze(workspace, index)

    assert [f.rule_id for f in findings] == ["KeywordDetector"]


def test_the_two_noisy_eslint_rules_stay_off():
    """A guard, not a style opinion. These two produced 45 of the 47 ESLint
    findings on a real repository and none of them was a defect:
    no-promise-executor-return fires on `new Promise((r) => setTimeout(r, ms))`
    and require-atomic-updates fires on ordinary async assignment. Turning
    either back on should be a deliberate act with this test to argue with.
    """
    from app.analysis.analyzers.eslint_adapter import RUNTIME_DIR

    config = (RUNTIME_DIR / "eslint.config.mjs").read_text(encoding="utf-8")

    assert '"no-promise-executor-return": "off"' in config
    assert '"require-atomic-updates": "off"' in config
