"""Checks that keep findings anchored to real, reviewable, changed code."""

from pathlib import Path, PurePosixPath

from app.analysis.diffs.parser import DiffIndex

MAX_FILE_BYTES = 500 * 1024
BINARY_SNIFF_BYTES = 8192

LOCKFILE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
SKIPPED_SEGMENTS = {"dist", "build", "node_modules", "vendor", ".venv"}
SKIPPED_SUFFIXES = (".min.js", ".map")


def location_exists(workspace: Path, path: str, start: int, end: int) -> bool:
    if start < 1 or end < start:
        return False
    target = workspace / path
    if not target.is_file():
        return False
    return end <= len(target.read_bytes().splitlines())


def touches_change(index: DiffIndex, path: str, start: int, end: int, pad: int = 0) -> bool:
    file_diff = index.files.get(path)
    if file_diff is None:
        return False
    return any(start - pad <= line <= end + pad for line in file_diff.changed_lines)


def is_reviewable(index: DiffIndex, workspace: Path, path: str) -> bool:
    file_diff = index.files.get(path)
    if file_diff is None or file_diff.status == "deleted":
        return False

    pure = PurePosixPath(path)
    if pure.name in LOCKFILE_NAMES or pure.name.endswith(SKIPPED_SUFFIXES):
        return False
    if any(segment in SKIPPED_SEGMENTS for segment in pure.parts):
        return False

    target = workspace / path
    if not target.is_file():
        return False
    if target.stat().st_size > MAX_FILE_BYTES:
        return False
    # same heuristic git uses: a null byte early in the file means binary
    with target.open("rb") as handle:
        return b"\x00" not in handle.read(BINARY_SNIFF_BYTES)
