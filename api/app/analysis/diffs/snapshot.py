"""Build a DiffIndex for a repository snapshot, where every file counts.

The demo already proved that presenting content as all-added lets the whole
pipeline run unchanged: analyzers pick their files from the index, and
touches_change passes everywhere. A snapshot takes the same idea without
materializing a repo-sized diff string or a set of every line number: one
FileDiff per file with all_changed=True, which touches_change honors
directly. Memory cost is one small object per file.
"""

from pathlib import Path

from app.analysis.diffs.parser import DiffIndex, FileDiff


def build_snapshot_index(workspace: Path) -> DiffIndex:
    files: dict[str, FileDiff] = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        # Sorted on the posix string below, not on Path: Path comparison is
        # case-insensitive on Windows and case-sensitive on Linux
        rel = path.relative_to(workspace).as_posix()
        files[rel] = FileDiff(rel, None, "added", set(), [], all_changed=True)
    return DiffIndex({path: files[path] for path in sorted(files)})
