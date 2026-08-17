"""Map changed files to the languages the analyzers understand."""

from pathlib import Path

EXTENSION_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def detect_language(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix:
        return EXTENSION_MAP.get(suffix)
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    if b"python" in first:
        return "python"
    if b"node" in first:
        return "javascript"
    return None
