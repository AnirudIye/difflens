from pathlib import Path

import pytest

from app.analysis.languages.detect import detect_language
from app.analysis.languages.scopes import CodeBlock, enclosing_blocks


def write(tmp_path: Path, name: str, text: str) -> Path:
    target = tmp_path / name
    target.write_text(text)
    return target


def test_detect_language_extensions():
    cases = {
        "a.py": "python",
        "a.ts": "typescript",
        "a.tsx": "typescript",
        "a.js": "javascript",
        "a.jsx": "javascript",
        "a.mjs": "javascript",
        "a.cjs": "javascript",
        "a.rs": None,
        "a.txt": None,
    }
    for name, expected in cases.items():
        assert detect_language(Path(name)) == expected, name


def test_detect_language_shebang(tmp_path):
    py = write(tmp_path, "runme", "#!/usr/bin/env python3\nprint('hi')\n")
    node = write(tmp_path, "cli", "#!/usr/bin/env node\nconsole.log('hi')\n")
    plain = write(tmp_path, "notes", "just text\n")
    assert detect_language(py) == "python"
    assert detect_language(node) == "javascript"
    assert detect_language(plain) is None
    assert detect_language(tmp_path / "missing") is None


def test_window_padding_and_clamping(tmp_path):
    target = write(tmp_path, "a.rs", "line\n" * 40)
    assert enclosing_blocks(target, None, [(20, 22)]) == [CodeBlock(5, 37, "window")]
    assert enclosing_blocks(target, None, [(1, 3)]) == [CodeBlock(1, 18, "window")]
    assert enclosing_blocks(target, None, [(38, 40)]) == [CodeBlock(23, 40, "window")]


def test_window_merging(tmp_path):
    target = write(tmp_path, "a.rs", "line\n" * 100)
    assert enclosing_blocks(target, None, [(20, 22), (30, 32)]) == [CodeBlock(5, 47, "window")]


def test_windows_apart_stay_separate(tmp_path):
    target = write(tmp_path, "a.rs", "line\n" * 100)
    assert enclosing_blocks(target, None, [(20, 20), (70, 70)]) == [
        CodeBlock(5, 35, "window"),
        CodeBlock(55, 85, "window"),
    ]


def test_no_ranges_no_file(tmp_path):
    target = write(tmp_path, "a.rs", "line\n")
    assert enclosing_blocks(target, None, []) == []
    assert enclosing_blocks(tmp_path / "gone.rs", None, [(1, 2)]) == []


def test_python_range_in_method_yields_whole_method(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    source = (
        "import os\n"
        "\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        "        parts = ['hello', name]\n"
        "        if name:\n"
        "            parts.append('!')\n"
        "        return ' '.join(parts)\n"
        "\n"
        "    def other(self):\n"
        "        return os.getcwd()\n"
    )
    target = write(tmp_path, "greeter.py", source)
    assert enclosing_blocks(target, "python", [(7, 8)]) == [CodeBlock(5, 9, "scope")]


def test_python_nested_function_yields_inner_scope(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    source = "def outer():\n    def inner():\n        a = 1\n        return a\n    return inner\n"
    target = write(tmp_path, "nest.py", source)
    assert enclosing_blocks(target, "python", [(3, 3)]) == [CodeBlock(2, 4, "scope")]


def test_python_oversized_scope_falls_back_to_window(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    body = "".join(f"    x{i} = {i}\n" for i in range(200))
    target = write(tmp_path, "big.py", "def huge():\n" + body + "    return x0\n")
    assert enclosing_blocks(target, "python", [(50, 52)]) == [CodeBlock(35, 67, "window")]


def test_python_module_level_range_gets_window(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    target = write(tmp_path, "flat.py", "import os\nprint(os.getcwd())\n" + "x = 1\n" * 30)
    assert enclosing_blocks(target, "python", [(1, 2)]) == [CodeBlock(1, 17, "window")]
