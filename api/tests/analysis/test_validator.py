from pathlib import Path

from app.analysis.diffs.parser import DiffIndex, FileDiff, FileStatus
from app.analysis.diffs.validator import is_reviewable, location_exists, touches_change


def make_index(path, status: FileStatus = "modified", changed_lines=None):
    file_diff = FileDiff(path, None, status, changed_lines or set(), [])
    return DiffIndex({path: file_diff})


def put(workspace: Path, path: str, data: bytes) -> None:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_location_exists_bounds(tmp_path):
    put(tmp_path, "a.py", b"one\ntwo\nthree\n")
    assert location_exists(tmp_path, "a.py", 1, 3)
    assert location_exists(tmp_path, "a.py", 3, 3)
    assert not location_exists(tmp_path, "a.py", 0, 1)
    assert not location_exists(tmp_path, "a.py", 3, 2)
    assert not location_exists(tmp_path, "a.py", 1, 4)
    assert not location_exists(tmp_path, "a.py", 4, 4)


def test_location_exists_missing_file(tmp_path):
    assert not location_exists(tmp_path, "ghost.py", 1, 1)


def test_touches_change(tmp_path):
    index = make_index("a.py", changed_lines={10, 20})
    assert touches_change(index, "a.py", 10, 10)
    assert touches_change(index, "a.py", 8, 12)
    assert not touches_change(index, "a.py", 12, 15)
    assert touches_change(index, "a.py", 12, 15, pad=2)
    assert touches_change(index, "a.py", 22, 25, pad=2)
    assert not touches_change(index, "other.py", 10, 10)


def test_denylist_lockfiles(tmp_path):
    for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"):
        put(tmp_path, name, b"content\n")
        assert not is_reviewable(make_index(name), tmp_path, name)


def test_denylist_minified_and_maps(tmp_path):
    put(tmp_path, "bundle.min.js", b"var a=1;\n")
    put(tmp_path, "bundle.js.map", b"{}\n")
    assert not is_reviewable(make_index("bundle.min.js"), tmp_path, "bundle.min.js")
    assert not is_reviewable(make_index("bundle.js.map"), tmp_path, "bundle.js.map")


def test_denylist_path_segments(tmp_path):
    for path in (
        "node_modules/pkg/index.js",
        "web/dist/main.js",
        "build/out.py",
        "third_party/vendor/lib.py",
        ".venv/lib/site.py",
    ):
        put(tmp_path, path, b"content\n")
        assert not is_reviewable(make_index(path), tmp_path, path)


def test_denylist_oversize(tmp_path):
    put(tmp_path, "big.py", b"a" * (500 * 1024 + 1))
    assert not is_reviewable(make_index("big.py"), tmp_path, "big.py")


def test_denylist_binary(tmp_path):
    put(tmp_path, "blob.dat", b"text then\x00null\n")
    assert not is_reviewable(make_index("blob.dat"), tmp_path, "blob.dat")


def test_denylist_deleted_and_unknown(tmp_path):
    put(tmp_path, "a.py", b"content\n")
    assert not is_reviewable(make_index("a.py", status="deleted"), tmp_path, "a.py")
    assert not is_reviewable(make_index("other.py"), tmp_path, "a.py")


def test_reviewable_normal_file(tmp_path):
    put(tmp_path, "src/service.py", b"def handler():\n    return 1\n")
    index = make_index("src/service.py", changed_lines={2})
    assert is_reviewable(index, tmp_path, "src/service.py")


def test_missing_workspace_file_not_reviewable(tmp_path):
    assert not is_reviewable(make_index("gone.py"), tmp_path, "gone.py")
