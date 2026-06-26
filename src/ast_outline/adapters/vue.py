"""Vue Single-File Component adapter (.vue).

Vue SFCs contain up to three top-level sections — ``<template>``,
``<script>``, and ``<style>`` — each in a different language. This
adapter parses the SFC structure with tree-sitter-html, then delegates
each section to the appropriate grammar:

* ``<template>`` → tree-sitter-html (same rendering as ``.html``:
  CSS-selector tokens, ``[import]`` classification for external assets,
  text preview on headings, bare-element drop/lift rules).
* ``<script>`` → tree-sitter-typescript (class, interface, enum,
  function, type alias, callback-DSL blocks, lexical declarations,
  import statements).
* ``<style>`` → tree-sitter-css (rule sets, at-rule wrappers,
  ``@import`` collection).

Declarations from all three sections are merged into a single flat list;
each retains byte offsets and line numbers relative to the original
``.vue`` file so ``show`` and ``grep`` work without remapping.

Well-known limitations (deliberate exclusions, may revisit in 1.x):

* ``<script lang="tsx">`` / ``<script lang="jsx">`` still uses the
  plain TypeScript grammar (safe: TSX grammar is a superset, but for
  now we keep one parser path).
* ``<style lang="scss">`` still uses the CSS grammar (safe for the
  common SCSS subset that CSS also accepts; SCSS-specific features
  like ``@mixin`` / ``$variable`` are not surfaced).
* Multi-file components and ``<custom-block>`` sections (``<i18n>``,
  ``<docs>``, etc.) are ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_css as tscss
import tree_sitter_html as tshtml
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from ._css_base import first_named_child, line_for, text_of
from .base import count_parse_errors
from .css import _walk_top_level as _css_walk_top_level
from .html import (
    _collapse_details_runs,
    _node_to_decls as _html_node_to_decls,
    _recover_elements as _html_recover_elements,
)
from .typescript import _walk_module as _ts_walk_module
from .typescript import _collect_imports as _ts_collect_imports
from ..core import (
    Declaration,
    ParseResult,
)


_LANG_HTML = Language(tshtml.language())
_LANG_TS = Language(tsts.language_typescript())
_LANG_CSS = Language(tscss.language())
_PARSER_HTML = Parser(_LANG_HTML)
_PARSER_TS = Parser(_LANG_TS)
_PARSER_CSS = Parser(_LANG_CSS)


# Top-level SFC section tags that this adapter recognises.
_SFC_SECTION_TAGS = frozenset({"template", "script", "style"})


class VueAdapter:
    language_name = "vue"
    display_name = "Vue"
    extensions = {".vue"}
    definition_keywords = frozenset(
        {
            "class",
            "interface",
            "type",
            "enum",
            "function",
        }
    )
    comment_line_prefixes = ("//",)
    import_line_prefixes = ("import ",)
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER_HTML.parse(src)

        decls: list[Declaration] = []
        imports: list[str] = []
        import_regions: list[tuple[int, int]] = []
        noise_regions: list[tuple[int, int, str]] = []
        total_errors = count_parse_errors(tree.root_node)

        for child in tree.root_node.named_children:
            tag = _sfc_tag(child, src)
            if tag is None:
                continue
            if tag == "template":
                _parse_template(
                    child, src, decls, imports, import_regions, noise_regions
                )
            elif tag == "script":
                total_errors += _parse_script(
                    child, src, decls, imports, import_regions
                )
            elif tag == "style":
                total_errors += _parse_style(child, src, decls)

        decls = _collapse_details_runs(decls)
        noise_regions.sort()
        import_regions.sort()

        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=total_errors,
            imports=imports,
            import_regions=import_regions,
            noise_regions=noise_regions,
        )


# ---------------------------------------------------------------------------
# Top-level section detection
# ---------------------------------------------------------------------------


def _sfc_tag(node: Node, src: bytes) -> Optional[str]:
    """Return the lowercased tag name if ``node`` is an SFC section
    element (``<template>``, ``<script>``, ``<style>``), else None.

    Accepts both ``element`` nodes (template) and
    ``script_element``/``style_element`` nodes.
    """
    if node.type not in ("element", "script_element", "style_element"):
        return None
    start = first_named_child(node, "start_tag")
    if start is None:
        return None
    tag_node = first_named_child(start, "tag_name")
    if tag_node is None:
        return None
    tag = text_of(tag_node, src).lower()
    return tag if tag in _SFC_SECTION_TAGS else None


# ---------------------------------------------------------------------------
# Template section — parse children as HTML
# ---------------------------------------------------------------------------


def _parse_template(
    node: Node,
    src: bytes,
    decls: list[Declaration],
    imports: list[str],
    import_regions: list[tuple[int, int]],
    noise_regions: list[tuple[int, int, str]],
) -> None:
    """Walk the children of ``<template>`` as HTML elements."""
    added: list[Declaration] = []
    for child in node.named_children:
        added.extend(
            _html_node_to_decls(
                child,
                src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            )
        )
    # Recovery for when the template body is wrapped in a single ERROR
    # node (e.g. heavy Vue template syntax that tree-sitter-html rejects).
    if not added and node.has_error:
        added.extend(
            _html_recover_elements(
                node,
                src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            )
        )
    decls.extend(added)


# ---------------------------------------------------------------------------
# Script section — parse with tree-sitter-typescript
# ---------------------------------------------------------------------------


def _parse_script(
    node: Node,
    src: bytes,
    decls: list[Declaration],
    imports: list[str],
    import_regions: list[tuple[int, int]],
) -> int:
    """Parse the <script> section with tree-sitter-typescript.

    Returns the count of parse errors from the script section.
    """
    raw = first_named_child(node, "raw_text")
    if raw is None:
        return 0
    section_bytes = src[raw.start_byte : raw.end_byte]
    if not section_bytes.strip():
        return 0
    tree = _PARSER_TS.parse(section_bytes)
    section_decls: list[Declaration] = []
    _ts_walk_module(tree.root_node, section_bytes, section_decls)
    # Adjust byte offsets and line numbers to the original file
    byte_offset = raw.start_byte
    line_offset = src[: raw.start_byte].count(b"\n")
    for d in section_decls:
        _adjust_decl(d, byte_offset, line_offset)
    decls.extend(section_decls)
    # Collect imports from the script section — only adjust the
    # section-local regions that were just appended; template import
    # regions are already file-relative.
    prev_len = len(import_regions)
    _ts_collect_imports(tree.root_node, section_bytes, imports, import_regions)
    for i in range(prev_len, len(import_regions)):
        start, end = import_regions[i]
        import_regions[i] = (start + byte_offset, end + byte_offset)
    return count_parse_errors(tree.root_node)


# ---------------------------------------------------------------------------
# Style section — parse with tree-sitter-css
# ---------------------------------------------------------------------------


def _parse_style(
    node: Node,
    src: bytes,
    decls: list[Declaration],
) -> int:
    """Parse the <style> section with tree-sitter-css.

    Returns the count of parse errors from the style section.
    """
    raw = first_named_child(node, "raw_text")
    if raw is None:
        return 0
    section_bytes = src[raw.start_byte : raw.end_byte]
    if not section_bytes.strip():
        return 0
    tree = _PARSER_CSS.parse(section_bytes)
    section_imports: list[str] = []
    section_decls: list[Declaration] = []
    _css_walk_top_level(tree.root_node, section_bytes, section_decls, section_imports)
    byte_offset = raw.start_byte
    line_offset = src[: raw.start_byte].count(b"\n")
    for d in section_decls:
        _adjust_decl(d, byte_offset, line_offset)
    decls.extend(section_decls)
    return count_parse_errors(tree.root_node)


# ---------------------------------------------------------------------------
# Offset adjustment
# ---------------------------------------------------------------------------


def _adjust_decl(decl: Declaration, byte_offset: int, line_offset: int) -> None:
    """Add ``byte_offset`` and ``line_offset`` (0-based row offset) to
    ``decl`` and all its children so that positions are relative to the
    original ``.vue`` file instead of a section substring."""
    decl.start_byte += byte_offset
    decl.end_byte += byte_offset
    decl.doc_start_byte += byte_offset
    decl.start_line += line_offset
    decl.end_line += line_offset
    for child in decl.children:
        _adjust_decl(child, byte_offset, line_offset)
