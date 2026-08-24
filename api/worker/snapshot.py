"""Extract a GitHub repository tarball into a review workspace, safely.

The tarball is attacker-influenced input: a repository chooses its own file
names, sizes, and member types. Extraction therefore streams member by member
(never extractall), keeps only regular files, drops anything that would land
outside the workspace, refuses symlinks rather than resolving them, and
enforces hard ceilings on member count, extracted file count, and written
bytes. Past a ceiling the whole extraction refuses rather than truncates: a
half-extracted repository would produce a confidently wrong "no findings"
for the missing half.

Vendored trees (node_modules and friends) and .git are skipped at extraction
time. The analyzers would refuse those paths anyway via is_reviewable, so not
writing them is the same honesty for a fraction of the disk.
"""

import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import structlog

from app.analysis.diffs.validator import MAX_FILE_BYTES, SKIPPED_SEGMENTS

log = structlog.get_logger()

# Extracted-file ceiling: past this, analyzer wall clock, argv length, and
# disk make any "review" dishonest on free-tier infrastructure
MAX_SNAPSHOT_FILES = 20_000
# Written-bytes ceiling: bounds ephemeral disk and is the zip-bomb backstop
MAX_SNAPSHOT_TOTAL_BYTES = 200 * 1024 * 1024
# Scanned-member ceiling: a member bomb (millions of zero-byte entries) must
# cost bounded CPU even when nothing is written
MAX_TARBALL_ENTRIES = 200_000

_SKIPPED_DIRS = SKIPPED_SEGMENTS | {".git"}


class SnapshotTooLarge(Exception):
    pass


@dataclass
class SnapshotStats:
    files_extracted: int = 0
    files_skipped_large: int = 0
    entries_seen: int = 0
    bytes_written: int = 0


def extract_snapshot(tar_path: Path, workspace: Path) -> SnapshotStats:
    """Stream the tarball into the workspace and report what was written."""
    stats = SnapshotStats()
    root = workspace.resolve()
    with tarfile.open(tar_path, mode="r:gz") as archive:
        while True:
            member = archive.next()
            if member is None:
                break
            stats.entries_seen += 1
            if stats.entries_seen > MAX_TARBALL_ENTRIES:
                raise SnapshotTooLarge(f"tarball has over {MAX_TARBALL_ENTRIES} entries")
            if not member.isreg():
                # Symlinks are skipped, never resolved, so a link pointing at
                # /etc can never be followed by an analyzer
                continue
            rel = _strip_prefix(member.name)
            if rel is None:
                continue
            parts = PurePosixPath(rel).parts
            if any(segment in _SKIPPED_DIRS for segment in parts):
                continue
            if member.size > MAX_FILE_BYTES:
                stats.files_skipped_large += 1
                continue
            destination = (root / rel).resolve()
            if not destination.is_relative_to(root):
                log.warning("workspace_escape_dropped", path=member.name)
                continue
            if stats.files_extracted + 1 > MAX_SNAPSHOT_FILES:
                raise SnapshotTooLarge(f"snapshot has over {MAX_SNAPSHOT_FILES} reviewable files")
            if stats.bytes_written + member.size > MAX_SNAPSHOT_TOTAL_BYTES:
                raise SnapshotTooLarge(
                    f"snapshot exceeds {MAX_SNAPSHOT_TOTAL_BYTES} extracted bytes"
                )
            source = archive.extractfile(member)
            if source is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as out:
                out.write(source.read())
            stats.files_extracted += 1
            stats.bytes_written += member.size
    return stats


def _strip_prefix(name: str) -> str | None:
    """Drop the single top-level owner-repo-sha/ directory GitHub wraps
    everything in. A member not under exactly one top-level directory is not
    something GitHub produces, so it is dropped."""
    parts = PurePosixPath(name).parts
    if len(parts) < 2:
        return None
    rel = str(PurePosixPath(*parts[1:]))
    if rel.startswith("/") or ".." in parts:
        return None
    return rel
