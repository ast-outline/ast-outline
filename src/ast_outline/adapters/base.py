"""Adapter protocol + shared helpers reused by every tree-sitter adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from ..core import ParseResult


class LanguageAdapter(Protocol):
    language_name: str
    extensions: set[str]

    # Leading source-language keywords that introduce a declaration whose
    # name follows — ``class`` / ``struct`` / ``enum`` / ``def`` / ``fn``
    # / ``func`` / ``type`` … Consumed by ``ast_outline.grep`` to strip a
    # keyword an agent habitually prepended to a symbol it was searching
    # for (``grep "enum Foo"`` → search ``Foo`` as a definition). Adapters
    # for data / markup languages that have no such keyword (CSS, SCSS,
    # SQL, YAML, Markdown, HTML) declare an empty set.
    definition_keywords: frozenset[str]

    def parse(self, path: Path) -> ParseResult: ...


def count_parse_errors(root: Node) -> int:
    """Count `ERROR` and `MISSING` nodes anywhere in the tree.

    tree-sitter parsers always produce a tree — syntax they can't make
    sense of becomes `ERROR` nodes, and expected-but-absent tokens
    become synthetic `MISSING` nodes. Either one means the adapter's
    IR for that region is unreliable, so the outline header reports
    the combined count as a warning.

    Uses `root.has_error` as a fast-path — no walk when the tree is
    clean, which is the common case.
    """
    if not root.has_error:
        return 0
    total = 0
    stack: list[Node] = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            total += 1
        stack.extend(n.children)
    return total
