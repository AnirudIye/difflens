"""Flags diffs that change logic without touching any tests."""

from pathlib import Path, PurePosixPath

from app.analysis.diffs.parser import DiffIndex
from app.analysis.models import Finding

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js")
TEST_DIR_NAMES = {"tests", "__tests__"}


def _is_test_file(path: str) -> bool:
    pure = PurePosixPath(path)
    if TEST_DIR_NAMES & set(pure.parts[:-1]):
        return True
    name = pure.name
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return ".spec." in name or ".test." in name


class TestDetector:
    name = "missing-tests"

    def analyze(self, workspace: Path, index: DiffIndex) -> list[Finding]:
        if any(_is_test_file(path) for path in index.files):
            return []
        sources = [
            diff
            for path, diff in index.files.items()
            if diff.status != "deleted"
            and path.endswith(SOURCE_SUFFIXES)
            and not _is_test_file(path)
        ]
        if not sources:
            return []

        # anchor the finding on the file with the most added lines
        largest = max(sources, key=lambda diff: len(diff.changed_lines))
        return [
            Finding(
                file_path=largest.path,
                start_line=None,
                end_line=None,
                severity="medium",
                category="testing",
                confidence="medium",
                source="deterministic",
                title="Logic changes without accompanying test changes",
                explanation=(
                    "This diff modifies source files but no test files. "
                    "If the change alters behavior, a test should pin it down."
                ),
                recommendation="Add or update tests covering the changed logic",
                rule_id="missing-tests",
                tool="missing-tests",
            )
        ]
