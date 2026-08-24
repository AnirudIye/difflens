"""Turn raw unified diff text into a lookup index keyed by target path."""

from typing import Literal

from unidiff import PatchSet

FileStatus = Literal["added", "modified", "renamed", "deleted"]

CONTEXT_PAD = 3


class FileDiff:
    def __init__(
        self,
        path: str,
        old_path: str | None,
        status: FileStatus,
        changed_lines: set[int],
        context_ranges: list[tuple[int, int]],
        all_changed: bool = False,
    ) -> None:
        self.path = path
        self.old_path = old_path
        self.status = status
        self.changed_lines = changed_lines
        self.context_ranges = context_ranges
        # A repository snapshot treats every line as changed without holding a
        # set of every line number; touches_change honors this flag directly
        self.all_changed = all_changed


class DiffIndex:
    def __init__(self, files: dict[str, FileDiff] | None = None) -> None:
        self.files = files or {}


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _padded_ranges(changed_lines: set[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in sorted(changed_lines):
        start, end = max(1, line - CONTEXT_PAD), line + CONTEXT_PAD
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def build_diff_index(diff_text: str) -> DiffIndex:
    files: dict[str, FileDiff] = {}
    for patched in PatchSet(diff_text):
        source = _strip_prefix(patched.source_file or "")
        target = _strip_prefix(patched.target_file or "")

        status: FileStatus
        old_path: str | None = None
        if patched.is_added_file:
            status, path = "added", target
        elif patched.is_removed_file:
            status, path = "deleted", source
        elif patched.is_rename or source != target:
            status, path, old_path = "renamed", target, source
        else:
            status, path = "modified", target

        changed_lines = {
            line.target_line_no
            for hunk in patched
            for line in hunk
            if line.is_added and line.target_line_no is not None
        }
        files[path] = FileDiff(path, old_path, status, changed_lines, _padded_ranges(changed_lines))
    return DiffIndex(files)
