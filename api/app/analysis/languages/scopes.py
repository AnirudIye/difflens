"""Pick the code blocks worth sending to the AI reviewer for a set of changed ranges.

Tree-sitter scopes are an enhancement. The padded window is the guaranteed
fallback for unknown languages, missing grammars, parse failures, and scopes
that are too big to ship as context.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WINDOW_PAD = 15

SCOPE_NODE_TYPES = {
    "python": {"function_definition", "decorated_definition", "class_definition"},
    "typescript": {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "class_declaration",
    },
}
SCOPE_NODE_TYPES["javascript"] = SCOPE_NODE_TYPES["typescript"]


@dataclass
class CodeBlock:
    start_line: int
    end_line: int
    kind: Literal["scope", "window"]


def enclosing_blocks(
    file_path: Path,
    language: str | None,
    ranges: list[tuple[int, int]],
    max_block_lines: int = 150,
) -> list[CodeBlock]:
    if not ranges:
        return []
    try:
        line_count = len(file_path.read_bytes().splitlines())
    except OSError:
        return []
    scopes = _tree_sitter_scopes(file_path, language, ranges, max_block_lines)
    blocks = []
    for position, (start, end) in enumerate(ranges):
        block = scopes[position] if scopes else None
        if block is None:
            block = CodeBlock(
                max(1, start - WINDOW_PAD), min(line_count, end + WINDOW_PAD), "window"
            )
        blocks.append(block)
    return _merge(blocks)


def _tree_sitter_scopes(
    file_path: Path,
    language: str | None,
    ranges: list[tuple[int, int]],
    max_block_lines: int,
) -> list[CodeBlock | None] | None:
    node_types = SCOPE_NODE_TYPES.get(language or "")
    if node_types is None:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        tree = get_parser(language).parse(file_path.read_bytes())  # type: ignore[arg-type]
        results: list[CodeBlock | None] = []
        for start, end in ranges:
            node = tree.root_node.descendant_for_point_range((start - 1, 0), (end - 1, 0))
            while node is not None and node.type not in node_types:
                node = node.parent
            if node is None:
                results.append(None)
                continue
            first, last = node.start_point[0] + 1, node.end_point[0] + 1
            results.append(
                CodeBlock(first, last, "scope") if last - first + 1 <= max_block_lines else None
            )
        return results
    except Exception:
        # any tree-sitter trouble (not installed, unknown grammar, parse crash)
        # degrades every range to the window fallback
        return None


def _merge(blocks: list[CodeBlock]) -> list[CodeBlock]:
    blocks.sort(key=lambda block: (block.start_line, block.end_line))
    merged = [blocks[0]]
    for block in blocks[1:]:
        last = merged[-1]
        if block.start_line <= last.end_line + 1:
            last.end_line = max(last.end_line, block.end_line)
            if block.kind != last.kind:
                # the merged span no longer matches an exact syntax node
                last.kind = "window"
        else:
            merged.append(block)
    return merged
