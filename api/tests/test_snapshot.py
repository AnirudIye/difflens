"""extract_snapshot against hostile and oversized tarballs. Pure unit tests.

The tarball is attacker-influenced input, so every guard here is a security
or honesty boundary: traversal and symlinks must never land on disk, and the
ceilings must refuse rather than truncate.
"""

import io
import tarfile

import pytest

import worker.snapshot as snapshot
from app.analysis.diffs.validator import MAX_FILE_BYTES
from tests.conftest import make_tarball
from worker.snapshot import SnapshotTooLarge, extract_snapshot


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _write(tmp_path, blob: bytes):
    tar_path = tmp_path / "snapshot.tar.gz"
    tar_path.write_bytes(blob)
    return tar_path


def test_top_level_prefix_is_stripped(tmp_path, workspace):
    tar_path = _write(tmp_path, make_tarball({"b.py": b"x = 1\n"}))

    stats = extract_snapshot(tar_path, workspace)

    assert (workspace / "b.py").read_bytes() == b"x = 1\n"
    assert stats.files_extracted == 1


def test_nested_paths_keep_their_layout_under_the_prefix(tmp_path, workspace):
    tar_path = _write(tmp_path, make_tarball({"src/deep/mod.py": b"y = 2\n"}))

    extract_snapshot(tar_path, workspace)

    assert (workspace / "src" / "deep" / "mod.py").read_bytes() == b"y = 2\n"


def test_traversal_member_is_dropped_and_nothing_escapes(tmp_path, workspace):
    evil = tarfile.TarInfo("../../evil.txt")
    payload = b"escaped"
    evil.size = len(payload)
    tar_path = _write(
        tmp_path, make_tarball({"ok.py": b"x = 1\n"}, extra_members=[(evil, payload)])
    )

    stats = extract_snapshot(tar_path, workspace)

    assert stats.files_extracted == 1
    assert (workspace / "ok.py").is_file()
    # Nothing named evil.txt landed anywhere reachable from the tmp tree
    assert list(tmp_path.rglob("evil.txt")) == []


def test_traversal_inside_the_prefix_is_dropped_too(tmp_path, workspace):
    evil = tarfile.TarInfo("octocat-alpha-abc1234/../../evil.txt")
    payload = b"escaped"
    evil.size = len(payload)
    tar_path = _write(tmp_path, make_tarball({}, extra_members=[(evil, payload)]))

    stats = extract_snapshot(tar_path, workspace)

    assert stats.files_extracted == 0
    assert list(tmp_path.rglob("evil.txt")) == []


def test_symlink_member_is_skipped_never_resolved(tmp_path, workspace):
    link = tarfile.TarInfo("octocat-alpha-abc1234/link.py")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    tar_path = _write(tmp_path, make_tarball({"ok.py": b"x = 1\n"}, extra_members=[(link, None)]))

    stats = extract_snapshot(tar_path, workspace)

    assert not (workspace / "link.py").exists()
    assert stats.files_extracted == 1


def test_member_over_the_per_file_cap_is_skipped_and_counted(tmp_path, workspace):
    big = b"x" * (MAX_FILE_BYTES + 1)
    tar_path = _write(tmp_path, make_tarball({"big.py": big, "ok.py": b"x = 1\n"}))

    stats = extract_snapshot(tar_path, workspace)

    assert not (workspace / "big.py").exists()
    assert (workspace / "ok.py").is_file()
    assert stats.files_skipped_large == 1
    assert stats.files_extracted == 1


def test_too_many_tarball_entries_refuses(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_TARBALL_ENTRIES", 3)
    files = {f"f{i}.py": b"x = 1\n" for i in range(4)}
    tar_path = _write(tmp_path, make_tarball(files))

    with pytest.raises(SnapshotTooLarge, match="entries"):
        extract_snapshot(tar_path, workspace)


def test_too_many_extracted_files_refuses(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_FILES", 1)
    tar_path = _write(tmp_path, make_tarball({"a.py": b"x = 1\n", "b.py": b"y = 2\n"}))

    with pytest.raises(SnapshotTooLarge, match="files"):
        extract_snapshot(tar_path, workspace)


def test_too_many_extracted_bytes_refuses(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_TOTAL_BYTES", 10)
    tar_path = _write(tmp_path, make_tarball({"a.py": b"x" * 11}))

    with pytest.raises(SnapshotTooLarge, match="bytes"):
        extract_snapshot(tar_path, workspace)


def test_vendored_and_git_members_are_not_written(tmp_path, workspace):
    tar_path = _write(
        tmp_path,
        make_tarball(
            {
                "node_modules/pkg/index.js": b"module.exports = 1\n",
                ".git/config": b"[core]\n",
                "ok.py": b"x = 1\n",
            }
        ),
    )

    stats = extract_snapshot(tar_path, workspace)

    assert not (workspace / "node_modules").exists()
    assert not (workspace / ".git").exists()
    assert stats.files_extracted == 1


def test_corrupt_bytes_raise_tarfile_read_error(tmp_path, workspace):
    tar_path = _write(tmp_path, b"not a tar at all")

    with pytest.raises(tarfile.ReadError):
        extract_snapshot(tar_path, workspace)


def test_stats_count_what_actually_happened(tmp_path, workspace):
    big = b"x" * (MAX_FILE_BYTES + 1)
    tar_path = _write(
        tmp_path,
        make_tarball({"a.py": b"x = 1\n", "src/b.py": b"y = 22\n", "big.bin": big}),
    )

    stats = extract_snapshot(tar_path, workspace)

    assert stats.entries_seen == 3
    assert stats.files_extracted == 2
    assert stats.files_skipped_large == 1
    assert stats.bytes_written == len(b"x = 1\n") + len(b"y = 22\n")


def test_a_name_the_filesystem_refuses_is_skipped_not_raised(tmp_path):
    """A repository chooses its own file names. One this filesystem will not
    create is a permanent property of that repository, so it is skipped and
    counted; raising sent it to the generic retry path, which burned all
    three attempts and then blamed a temporary problem."""
    import tarfile

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tar_path = tmp_path / "snap.tar.gz"
    # A file and a directory that cannot both exist under the same name, which
    # fails the same way on every platform
    members = {
        "octocat-repo-abc/collide": b"first\n",
        "octocat-repo-abc/collide/inside.py": b"x = 1\n",
        "octocat-repo-abc/fine.py": b"y = 2\n",
    }
    with tarfile.open(tar_path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    stats = extract_snapshot(tar_path, workspace)

    assert (workspace / "fine.py").read_bytes() == b"y = 2\n"
    assert stats.files_unwritable >= 1
    assert stats.files_extracted >= 1
