"""SCSS adapter (.scss).

Builds on the CSS structural model (rules, at-rules, nested selectors
with `&` resolution) and adds SCSS-specific first-class symbols:

- ``@mixin name($args)`` → ``KIND_MIXIN`` (callable — gets `()` in
  digest, parameter list visible in outline signature)
- ``@function name($args)`` → ``KIND_FUNCTION`` (callable, same shape)
- ``$variable: value`` → ``KIND_VARIABLE`` (top-level only — variables
  inside rule bodies stay in the rule's source slice, not surfaced as
  separate symbols)
- ``%placeholder`` → ``KIND_PLACEHOLDER`` (extend-only selector)
- ``@use`` / ``@forward`` / legacy ``@import`` → imports list
- Sass privacy convention: names with leading ``_`` or ``-`` are
  marked ``visibility="private"`` so ``--include-private=False``
  hides them in outline / digest.

Indented Sass (``.sass``) is **not** supported — tree-sitter-scss
handles only the brace-delimited ``.scss`` syntax. Indented Sass is
in long decline and would need a separate grammar.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_scss as tssscss
from tree_sitter import Node, Parser

from .base import count_parse_errors, load_language, read_source
from ._css_base import (
    AT_RULE_STATEMENT_TYPES,
    at_rule_signature,
    doc_start_byte_for,
    extract_simple_selectors,
    first_named_child,
    is_private_scss_name,
    line_for,
    resolve_match_names,
    resolve_signature,
    text_of,
)
from ..core import (
    KIND_AT_RULE,
    KIND_FUNCTION,
    KIND_MIXIN,
    KIND_PLACEHOLDER,
    KIND_RULE,
    KIND_VARIABLE,
    Declaration,
    ParseResult,
)


# tree-sitter-scss 1.0.0 returns its language handle as a raw int
# instead of the typed PyCapsule tree-sitter-css exposes. Feeding that
# int straight to ``Language()`` crashes on 64-bit Windows and warns
# everywhere else — ``load_language`` wraps it in a capsule first. See
# its docstring for the full story.
_LANGUAGE = load_language(tssscss.language())
_PARSER = Parser(_LANGUAGE)


# Top-level node types that introduce imports — we collect their
# source-true text and don't emit Declarations for them.
_IMPORT_NODE_TYPES = {"import_statement", "use_statement", "forward_statement"}


class ScssAdapter:
    language_name = "scss"
    display_name = "SCSS"
    extensions = {".scss"}
    # `@mixin` / `@function` lead with `@`, not a bare identifier — the
    # keyword-strip shape (`<kw> Identifier`) never applies.
    definition_keywords = frozenset()
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('@import ', '@use ', '@forward ')
    render_family = "css"

    def parse(self, path: Path) -> ParseResult:
        src = read_source(path)
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


def _walk_top_level(
    root: Node, src: bytes, out: list[Declaration], imports: list[str]
) -> None:
    for child in root.named_children:
        decl = _node_to_decl(
            child, src, parent_match_names=[], imports=imports, top_level=True
        )
        if decl is not None:
            out.append(decl)


def _node_to_decl(
    node: Node,
    src: bytes,
    *,
    parent_match_names: list[str],
    imports: list[str],
    top_level: bool,
) -> Optional[Declaration]:
    t = node.type
    if t == "rule_set":
        return _rule_set_to_decl(node, src, parent_match_names, imports)
    if t in AT_RULE_STATEMENT_TYPES:
        return _at_rule_to_decl(node, src, parent_match_names, imports)
    if t == "mixin_statement":
        return _mixin_to_decl(node, src)
    if t == "function_statement":
        return _function_to_decl(node, src)
    if t in _IMPORT_NODE_TYPES:
        stmt = _import_text(node, src)
        if stmt:
            imports.append(stmt)
        return None
    if t == "declaration" and top_level:
        # SCSS top-level `$var: value;` is parsed as a `declaration` node
        # with a `property_name` whose text starts with `$`. Only emit
        # variables — non-`$` declarations at top level (rare, generally
        # invalid SCSS) are skipped.
        return _variable_to_decl(node, src)
    return None


def _rule_set_to_decl(
    node: Node,
    src: bytes,
    parent_match_names: list[str],
    imports: list[str],
) -> Optional[Declaration]:
    """Same shape as the CSS adapter's rule extraction. Placeholder
    selectors (`%foo`) come through as ordinary rules whose first
    `match_names` entry starts with `%` — we tag the kind as
    `KIND_PLACEHOLDER` in that case so digest / privacy filtering
    treat them distinctly."""
    selectors_node = first_named_child(node, "selectors")
    if selectors_node is None:
        return None
    block = first_named_child(node, "block")
    # Collapse internal whitespace — multi-line selector lists like
    # `&__header,\n        &__body` would otherwise render with the
    # newline + indent baked into the signature.
    raw_selector_text = " ".join(text_of(selectors_node, src).split())
    raw_match_names = extract_simple_selectors(selectors_node, src)
    match_names = resolve_match_names(raw_match_names, parent_match_names)
    signature = resolve_signature(raw_selector_text, parent_match_names)
    name = match_names[0] if match_names else raw_selector_text

    children: list[Declaration] = []
    if block is not None:
        for c in block.named_children:
            child_decl = _node_to_decl(
                c,
                src,
                parent_match_names=match_names,
                imports=imports,
                top_level=False,
            )
            if child_decl is not None:
                children.append(child_decl)

    is_placeholder = bool(match_names) and match_names[0].startswith("%")
    kind = KIND_PLACEHOLDER if is_placeholder else KIND_RULE

    visibility = ""
    if is_placeholder and is_private_scss_name(match_names[0]):
        visibility = "private"

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        match_names=match_names,
        docs=doc_lines,
        visibility=visibility,
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
    block = first_named_child(node, "block")
    if block is None:
        block = first_named_child(node, "keyframe_block_list")

    signature = at_rule_signature(node, src)
    children: list[Declaration] = []
    if block is not None and node.type != "keyframes_statement":
        for c in block.named_children:
            child_decl = _node_to_decl(
                c,
                src,
                parent_match_names=parent_match_names,
                imports=imports,
                top_level=False,
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


def _mixin_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert ``@mixin name [(params)] { ... }`` into ``KIND_MIXIN``.

    Signature includes the parameter list verbatim (`@mixin button($color,
    $size: medium)`) so the agent reads the full contract from outline
    without needing `show`. Mixins with no params still get the
    `@mixin name` form — the parameter list is omitted, not rendered
    as `()`, mirroring how Sass itself prints it.
    """
    name_node = first_named_child(node, "identifier")
    if name_node is None:
        return None
    name = text_of(name_node, src)
    parameters_node = first_named_child(node, "parameters")
    block = first_named_child(node, "block")

    if parameters_node is not None:
        params_text = text_of(parameters_node, src)
        signature = f"@mixin {name}{params_text}"
    else:
        signature = f"@mixin {name}"

    visibility = "private" if is_private_scss_name(name) else ""

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=KIND_MIXIN,
        name=name,
        signature=signature,
        docs=doc_lines,
        visibility=visibility,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _function_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert ``@function name($args) { ... }`` into ``KIND_FUNCTION``.
    Same shape as ``_mixin_to_decl`` — the only difference is the kind
    constant (so digest renders `name()` for both) and the source-true
    `@function` keyword in the signature."""
    name_node = first_named_child(node, "identifier")
    if name_node is None:
        return None
    name = text_of(name_node, src)
    parameters_node = first_named_child(node, "parameters")
    if parameters_node is not None:
        params_text = text_of(parameters_node, src)
        signature = f"@function {name}{params_text}"
    else:
        signature = f"@function {name}"

    visibility = "private" if is_private_scss_name(name) else ""

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=KIND_FUNCTION,
        name=name,
        signature=signature,
        docs=doc_lines,
        visibility=visibility,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _variable_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert a top-level ``$name: value;`` declaration into
    ``KIND_VARIABLE``.

    Signature is the full literal text (``$primary: #00ff00 !default``)
    minus the trailing semicolon — so the outline shows both the name
    and its value, matching how Sass developers read variable files.

    Returns None for non-variable declarations (rare at top level —
    mostly caused by parse drift), so the walker can skip them.
    """
    prop = first_named_child(node, "property_name")
    if prop is None:
        return None
    name = text_of(prop, src)
    if not name.startswith("$"):
        return None

    raw = text_of(node, src).strip()
    if raw.endswith(";"):
        raw = raw[:-1].rstrip()
    signature = raw

    visibility = "private" if is_private_scss_name(name) else ""

    doc_start, doc_lines = doc_start_byte_for(node, src)
    return Declaration(
        kind=KIND_VARIABLE,
        name=name,
        signature=signature,
        docs=doc_lines,
        visibility=visibility,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _import_text(node: Node, src: bytes) -> str:
    text = text_of(node, src).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text
