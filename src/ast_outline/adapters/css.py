"""CSS adapter (.css).

Produces an IR that maps the stylesheet's structure for navigation:

    # styles.css (240 lines)
    .container                     L3-15
    .btn-primary, .btn-secondary   L17-28
    @media (max-width: 768px)      L30-50
        .container                 L31-49
    @keyframes fadeIn              L52-58
    @supports (display: grid)      L60-72
        .grid                      L61-71

Each rule's `match_names` carries the bare simple-selector tokens
extracted from its selector list — `find_symbols(".btn-primary")`
returns every rule whose selectors include `.btn-primary` in any
position (direct, descendant, with pseudo-class, with attribute
filter), which is what an agent debugging a cascade actually wants.
At-rule wrappers (`@media`, `@supports`, `@layer`, `@keyframes`,
`@container`) become `KIND_AT_RULE` parents whose inner rules are
children — so the outline reflects the conditional nesting and
`show .btn` inside a `@media` block shows the wrapping context as
breadcrumb.

The `:root { --token: value; }` block is NOT broken out into a
catalog of custom-property declarations. Each `:root` (and themed
selectors like `[data-theme=dark]`) is one rule; querying
`show :root` returns the whole token block. The decision is intentional:
custom-property names with leading `--` collide with CLI flag parsing,
and breaking them out as separate symbols would invent a new API
surface (kind tags, dotted query forms) that no other adapter has.
Per-token resolution can be added later if real-world use demands it,
without breaking anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_css as tscss
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ._css_base import (
    AT_RULE_STATEMENT_TYPES,
    at_rule_signature,
    doc_start_byte_for,
    extract_simple_selectors,
    first_named_child,
    line_for,
    resolve_match_names,
    resolve_signature,
    text_of,
)
from ..core import (
    KIND_AT_RULE,
    KIND_RULE,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tscss.language())
_PARSER = Parser(_LANGUAGE)


class CssAdapter:
    language_name = "css"
    extensions = {".css"}
    # No leading declaration keyword — rules are selector-based.
    definition_keywords = frozenset()

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        imports: list[str] = []
        _walk_top_level(tree.root_node, src, decls, imports)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
        )


# --- Top-level walk -------------------------------------------------------


def _walk_top_level(
    root: Node, src: bytes, out: list[Declaration], imports: list[str]
) -> None:
    """Walk the stylesheet root. Rules become top-level declarations;
    at-rules with blocks become parents; `@import` populates the imports
    list as source-true text."""
    for child in root.named_children:
        decl = _node_to_decl(child, src, parent_match_names=[], imports=imports)
        if decl is not None:
            out.append(decl)


def _node_to_decl(
    node: Node,
    src: bytes,
    *,
    parent_match_names: list[str],
    imports: list[str],
) -> Optional[Declaration]:
    """Dispatch one structural node → Declaration (or None when the node
    isn't user-visible — comments, raw declarations, `@charset`, …).

    `parent_match_names` carries the parent rule's resolved match names
    so nested rules (CSS native nesting) can resolve `&` if present.
    `imports` is mutated with `@import` statements collected along the
    way, so the caller doesn't need to traverse a second time.
    """
    t = node.type
    if t == "rule_set":
        return _rule_set_to_decl(node, src, parent_match_names, imports)
    if t in AT_RULE_STATEMENT_TYPES:
        return _at_rule_to_decl(node, src, parent_match_names, imports)
    if t == "import_statement":
        stmt = _import_text(node, src)
        if stmt:
            imports.append(stmt)
        return None
    # Skip everything else (comments, top-level declarations like
    # `@charset`, raw `declaration` nodes that occasionally surface in
    # weird files). Not a Declaration in the IR sense.
    return None


def _rule_set_to_decl(
    node: Node,
    src: bytes,
    parent_match_names: list[str],
    imports: list[str],
) -> Optional[Declaration]:
    """Convert a `rule_set` (selector + block) into a `KIND_RULE`
    Declaration. Resolves `&` against the parent for both `match_names`
    (so the rule is findable as `.card__header` not `&__header`) and
    the displayed signature (so outline shows the resolved form)."""
    selectors_node = first_named_child(node, "selectors")
    if selectors_node is None:
        return None
    block = first_named_child(node, "block")
    # Collapse internal whitespace — multi-line selector lists like
    # `&__header,\n        &__body` would otherwise render with the
    # newline + indent baked into the signature, breaking the
    # outline's column alignment.
    raw_selector_text = " ".join(text_of(selectors_node, src).split())
    raw_match_names = extract_simple_selectors(selectors_node, src)
    match_names = resolve_match_names(raw_match_names, parent_match_names)
    signature = resolve_signature(raw_selector_text, parent_match_names)

    # Pick a canonical `name` for trail display — the first match name.
    # Empty fallback (rare: a selector our extractor didn't recognise)
    # uses the raw text so the rule is still inspectable in outline.
    name = match_names[0] if match_names else raw_selector_text

    children: list[Declaration] = []
    if block is not None:
        for c in block.named_children:
            child_decl = _node_to_decl(
                c,
                src,
                parent_match_names=match_names,
                imports=imports,
            )
            if child_decl is not None:
                children.append(child_decl)

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=KIND_RULE,
        name=name,
        signature=signature,
        match_names=match_names,
        docs=doc_lines,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _at_rule_to_decl(
    node: Node,
    src: bytes,
    parent_match_names: list[str],
    imports: list[str],
) -> Optional[Declaration]:
    """Convert an at-rule (`@media`, `@supports`, `@keyframes`, generic
    `@layer`/`@container`/`@font-face`) into a `KIND_AT_RULE`
    Declaration. Inner rules become children; the at-rule's full
    header (everything up to the `{`) becomes both `name` and
    `signature` so it renders intuitively in outline."""
    block = first_named_child(node, "block")
    if block is None:
        block = first_named_child(node, "keyframe_block_list")

    signature = at_rule_signature(node, src)
    children: list[Declaration] = []
    if block is not None and node.type != "keyframes_statement":
        # `@keyframes` children are `keyframe_block` (`0% {}`, `50% {}`)
        # — not user-facing symbols. Outline already shows the keyframes
        # header with line range; the body is read via `show <name>`
        # if anyone wants it.
        for c in block.named_children:
            child_decl = _node_to_decl(
                c,
                src,
                parent_match_names=parent_match_names,
                imports=imports,
            )
            if child_decl is not None:
                children.append(child_decl)

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=KIND_AT_RULE,
        name=signature,
        signature=signature,
        docs=doc_lines,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _import_text(node: Node, src: bytes) -> str:
    """Render an `@import` statement as one source-true line.

    Strips trailing whitespace and any trailing semicolon to keep the
    list normalised; the consuming `--imports` rendering joins entries
    with `; ` itself.
    """
    text = text_of(node, src).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text
