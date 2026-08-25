"""What kind of file is this, judged from its path alone.

A pull request review never needed this. Every deterministic finding had to
sit on or near a line the author had just written, and `touches_change` threw
away everything else, so the analyzers only ever spoke about new code. That
predicate was carrying precision, not just topicality.

A repository snapshot marks every line as changed, so the predicate is
universally true and that filter disappears. Nothing replaced it, and the
result was a review of one real project that opened with fifty-six critical
findings, none of them real: fake passwords in test fixtures, placeholder
credentials in `.env.example`, and the base32 alphabet that appears in every
TOTP implementation ever written.

This module is the replacement. It does not decide whether something is a
finding; it says where the finding lives, so the analyzers can be strict
about production code and quiet about the places whose whole job is to hold
things that look like credentials.
"""

from pathlib import PurePosixPath
from typing import Literal

FileClass = Literal["test", "example", "generated", "production"]

# Directory names whose contents are, by convention, not production code
TEST_DIR_NAMES = {
    "tests",
    "test",
    "__tests__",
    "__mocks__",
    "spec",
    "specs",
    "e2e",
    "fixtures",
    "fixture",
    "testdata",
    "__fixtures__",
    "mocks",
}

# Files that exist to be copied and filled in. A placeholder credential here
# is the file working as intended, not a leak.
EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")
EXAMPLE_STEMS = {"env.example", "env.sample", "env.template"}

# Checked-in build output and vendored bundles. Not authored, not reviewable
# as someone's work.
GENERATED_DIR_NAMES = {"generated", "__generated__", "migrations", ".next", "coverage"}
GENERATED_SUFFIXES = (".min.js", ".map", ".lock", ".snap", ".bundle.js")


def _is_test_name(name: str) -> bool:
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    return ".spec." in name or ".test." in name


def _is_example_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in EXAMPLE_STEMS:
        return True
    if lowered.startswith(".env.") and lowered != ".env.local":
        # .env.example, .env.sample, .env.production.example
        return True
    return lowered.endswith(EXAMPLE_SUFFIXES)


def classify(path: str) -> FileClass:
    """The file's kind, from its path. Order matters: a fixture inside a test
    directory is a test file, and an example inside one is still an example,
    because what makes a placeholder safe is the placeholder, not the folder.
    """
    pure = PurePosixPath(path)
    name = pure.name
    parts = set(pure.parts[:-1])

    if _is_example_name(name):
        return "example"
    if parts & TEST_DIR_NAMES or _is_test_name(name):
        return "test"
    if parts & GENERATED_DIR_NAMES or name.endswith(GENERATED_SUFFIXES):
        return "generated"
    return "production"


def is_credential_bearing(path: str) -> bool:
    """Is a credential-shaped string here expected rather than alarming.

    True for tests and examples: both exist to hold values that look exactly
    like the real thing. A structured detector (a real AWS key id, a private
    key block) still means something here, which is why this answers a
    question about the PLACE and leaves the detector's own strength to the
    caller.
    """
    return classify(path) in ("test", "example")
