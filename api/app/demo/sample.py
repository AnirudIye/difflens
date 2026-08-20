"""The bundled pull request the demo reviews.

`api/tests/` is not in the production image (the Dockerfile copies app,
worker, alembic and the eslint runtime, nothing else), so the demo cannot
reuse the regression fixtures the way the plan assumed. The sample lives
here, under app/, because that is what ships.

The files under sample/files/ are the single source of truth. The unified
diff is synthesized from them at load time rather than stored beside them,
so the two can never drift: there is only one copy of the content. The
synthesis is the same trick the worker already uses on GitHub's compare
payload in `worker.runner.build_diff_text`, which is what makes it safe to
rely on here.

Every file is read and written with LF endings regardless of platform. The
repository normalizes with `* text=auto`, so these files are CRLF in a
Windows working tree and LF in the image; without the normalization below,
the demo would produce different bytes, and therefore potentially different
findings, depending on where it ran.
"""

from pathlib import Path

FILES_DIR = Path(__file__).parent / "sample" / "files"

# Build artefacts that can appear beside the sample but are not part of it
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})

# The demo pull request as the UI describes it. These are not GitHub rows and
# nothing here is ever sent to GitHub; they exist so the demo review renders
# with the same shape of context as a real one.
REPO_FULL_NAME = "difflens-demo/storefront"
REPO_HTML_URL = "https://github.com/AnirudIye/difflens"
PR_NUMBER = 1
PR_TITLE = "Add settlement reporting and cart totals"
PR_AUTHOR = "difflens-demo"
PR_HTML_URL = ""

# Fixed, so the demo review is one immutable snapshot exactly like a real
# one, and so the live-review index has a stable key to enforce against.
BASE_SHA = "d3m0base0000000000000000000000000000000a"
HEAD_SHA = "d3m0head0000000000000000000000000000000b"


def sample_files() -> list[tuple[str, str]]:
    """(repo-relative path, text) for every file in the sample, path-sorted.

    Sorted because the order decides the order of the diff, and the diff
    decides the order findings come back in. A demo that reshuffles itself
    between runs, or between a Windows checkout and the Linux image, is not
    a demo anyone can screenshot or write a golden test against.
    """
    files: list[tuple[str, str]] = []
    root = FILES_DIR.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # The sample is deliberately buggy Python, so anything that compiles
        # or copies it can leave artefacts beside it: `pip install .` writes
        # __pycache__ into the installed copy. Reading a .pyc as UTF-8 raises
        # and would take the whole demo down with it.
        if "__pycache__" in path.parts or path.suffix in EXCLUDED_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if not text.endswith("\n"):
            text += "\n"
        files.append((path.relative_to(root).as_posix(), text))
    # Sorted on the posix string, not on Path: Path comparison is
    # case-insensitive on Windows and case-sensitive elsewhere, which would
    # order the diff differently on the two platforms the demo has to match
    return sorted(files)


def build_diff() -> str:
    """The sample as a unified diff adding every file.

    Every file is new, which means every line is a changed line. That is
    deliberate: the AI candidates in `candidates.py` are validated against
    this diff by the same `touches_change` check a live model's output goes
    through, and a wholly-new file leaves no room for a candidate to land on
    an unchanged line and be discarded for the wrong reason.
    """
    parts: list[str] = []
    for path, text in sample_files():
        lines = text.split("\n")[:-1]  # trailing "" from the final newline
        parts.append(f"diff --git a/{path} b/{path}")
        parts.append("new file mode 100644")
        parts.append("index 0000000..1111111")
        parts.append("--- /dev/null")
        parts.append(f"+++ b/{path}")
        parts.append(f"@@ -0,0 +1,{len(lines)} @@")
        parts.extend(f"+{line}" for line in lines)
    return "\n".join(parts) + "\n"


def populate_workspace(workspace: Path) -> None:
    """Materialize the sample into an analyzer workspace.

    Mirrors what `worker.runner.populate_workspace` does with blobs fetched
    from GitHub, so the analysis package receives exactly the same thing it
    receives for a real review and cannot tell the two apart.
    """
    root = workspace.resolve()
    for path, text in sample_files():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
