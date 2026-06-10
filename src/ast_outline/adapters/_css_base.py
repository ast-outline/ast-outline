"""Shared CSS / SCSS extraction logic.

The CSS and SCSS adapters share most of their structure: rule sets,
at-rule wrappers (`@media`, `@supports`, `@layer`, `@keyframes`,
`@container`, `@font-face`, …), nested rules with `&` parent
references, and `@import` collection. SCSS-specific constructs —
`@mixin`, `@function`, `$variable`, `%placeholder`, plus `@use` /
`@forward` imports — live in `scss.py`. CSS-only logic lives in
`css.py`. Both call into this module for the common pieces.

Selector matching design: each rule's `match_names` carries the bare
simple-selector tokens extracted from the selector list (`.btn-primary`,
`#header`, `body`, `%placeholder`). Pseudo-classes and attribute
filters are stripped — `.btn-primary:hover[disabled]` contributes
just `.btn-primary` — so `find_symbols(".btn-primary")` finds rules
that style the class in any state. `:is(...)` and `:where(...)`
arguments recurse (additive); `:not(...)` and `:has(...)` do not
(different semantics). For nested SCSS rules, `&` is resolved against
each parent simple selector — `.card { &__header { } }` becomes
findable as `.card__header`.
"""
from __future__ import annotations

from typing import Optional

from tree_sitter import Node


# --- Selector decomposition ----------------------------------------------


def extract_simple_selectors(selectors_node: Node, src: bytes) -> list[str]:
    """Walk a `selectors` node and return the bare simple-selector
    tokens that the rule defines or styles.

    For a rule like `.modal .btn-primary:hover, .btn-secondary[disabled]`
    returns `[".modal", ".btn-primary", ".btn-secondary"]`. Used to
    populate `Declaration.match_names`. Returns an empty list for
    unrecognisable input rather than raising — graceful degradation
    keeps the parse going on weird Tailwind-style or escape-heavy
    selectors.
    """
    if selectors_node is None:
        return []
    out: list[str] = []
    if selectors_node.type == "selectors":
        for child in selectors_node.named_children:
            _emit_simple_selectors(child, src, out)
    else:
        _emit_simple_selectors(selectors_node, src, out)
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def _emit_simple_selectors(node: Node, src: bytes, out: list[str]) -> None:
    """Extract simple selectors from one compound or combined selector.

    Combinators (`>`, `+`, `~`, descendant whitespace) are transparent
    — we descend into both sides because `.modal .btn` styles both
    `.modal` AND `.btn` in this rule, and an agent looking for either
    should find the rule.
    """
    t = node.type

    if t == "class_selector":
        _emit_class_selector(node, src, out)
    elif t == "id_selector":
        out.append(_id_selector_text(node, src))
    elif t in ("tag_name", "type_selector"):
        out.append(_text(node, src))
    elif t == "universal_selector":
        out.append("*")
    elif t == "nesting_selector":
        out.append("&")
    elif t == "placeholder":
        for c in node.named_children:
            if c.type == "identifier":
                out.append("%" + _text(c, src))
                return
        out.append(_text(node, src))
    elif t == "pseudo_class_selector":
        # Form: <inner_selector?> : <pseudo_name> [<arguments>]
        # Strip the pseudo decoration; keep the inner selector if present.
        # If there's no inner (`:root`, `:focus-within` standalone), keep
        # the pseudo as `:name` so the rule is still findable.
        pseudo_name = ""
        arguments: Optional[Node] = None
        inner_added = False
        for c in node.children:
            if not c.is_named:
                continue
            if c.type == "class_name":
                pseudo_name = _text(c, src)
            elif c.type == "arguments":
                arguments = c
            else:
                _emit_simple_selectors(c, src, out)
                inner_added = True
        if not inner_added and pseudo_name:
            out.append(":" + pseudo_name)
        # `:is(.a, .b)` / `:where(.a, .b)` are additive — a rule using
        # them styles `.a` AND `.b`. `:not(...)` / `:has(...)` aren't
        # additive (negation, has-relation), so we don't recurse.
        if arguments is not None and pseudo_name in ("is", "where"):
            for c in arguments.named_children:
                _emit_simple_selectors(c, src, out)
    elif t == "pseudo_element_selector":
        # Form: <inner_selector?> :: <pseudo_element_name>
        # Same shape as pseudo_class_selector — strip the `::name`
        # decoration, keep the inner selector if present. The
        # tag_name/class_name child here holds the pseudo-element NAME
        # (`before`, `after`, `selection`), not a real selector — we
        # only fall back to `::name` when there's no inner.
        pseudo_name = ""
        inner_added = False
        for c in node.children:
            if not c.is_named:
                continue
            if c.type in ("tag_name", "class_name"):
                pseudo_name = _text(c, src)
            else:
                _emit_simple_selectors(c, src, out)
                inner_added = True
        if not inner_added and pseudo_name:
            out.append("::" + pseudo_name)
    elif t == "attribute_selector":
        attr_name = ""
        inner_added = False
        for c in node.named_children:
            if c.type == "attribute_name":
                attr_name = _text(c, src)
            elif c.type in ("string_value", "plain_value"):
                continue
            else:
                _emit_simple_selectors(c, src, out)
                inner_added = True
        if not inner_added and attr_name:
            out.append(f"[{attr_name}]")
    elif t in (
        "descendant_selector",
        "child_selector",
        "sibling_selector",
        "adjacent_sibling_selector",
    ):
        for c in node.named_children:
            _emit_simple_selectors(c, src, out)
    # Unknown compound shape — skip silently.


def _emit_class_selector(node: Node, src: bytes, out: list[str]) -> None:
    """Emit match tokens for a ``class_selector``, compound shapes included.

    The grammar nests compound selectors inside the outermost
    ``class_selector``:

    - Plain class: ``.btn`` → ``class_name(btn)`` only.
    - Class-on-class: ``.a.b`` → ``class_selector(.a)`` + ``class_name(b)``.
    - Id-qualified: ``#header.nav`` → ``id_selector(#header)`` + ``class_name(nav)``.
    - Tag-qualified: ``div.container`` → ``tag_name(div)`` + ``class_name(container)``.
    - SCSS compound nesting: ``&.active`` → ``nesting_selector(&)`` +
      ``class_name(active)``.
    - SCSS BEM nesting: ``&__header`` → ``nesting_selector(&)`` +
      ``class_name(__header)`` — same node shape as ``&.active``; the
      two are told apart by whether a literal ``.`` precedes the class
      name in the source.

    Emitted tokens: each inner selector on its own (an agent searching
    ``#header`` must find the ``#header.nav`` rule), the bare class
    (``.nav``) when it is dot-prefixed in source, and the rebuilt
    compound (``#header.nav``, ``&.active`` pre-resolution) so the
    exact-compound query also resolves. Pseudo decorations are stripped
    by the inner recursion, matching the module's selector contract.
    """
    name_node: Optional[Node] = None
    has_nesting = False
    inner: list[str] = []
    for c in node.named_children:
        if c.type == "nesting_selector":
            has_nesting = True
        elif c.type in ("class_name", "identifier"):
            name_node = c
        else:
            _emit_simple_selectors(c, src, inner)
    out.extend(inner)
    if name_node is None:
        if has_nesting and not inner:
            out.append("&")
        return
    name = _text(name_node, src)
    dot = src[name_node.start_byte - 1 : name_node.start_byte] == b"."
    sep = "." if dot else ""
    if dot:
        out.append("." + name)
    if has_nesting:
        out.append("&" + sep + name)
    elif inner:
        out.append(inner[-1] + sep + name)
    elif not dot:
        # No dot, no nesting, no inner context — keep the raw name so
        # degenerate grammar shapes still yield a findable token.
        out.append(name)


def _id_selector_text(node: Node, src: bytes) -> str:
    """Render an id_selector to its bare token form (`#header`)."""
    for c in node.named_children:
        if c.type in ("id_name", "identifier"):
            return "#" + _text(c, src)
    raw = _text(node, src)
    return raw if raw.startswith("#") else "#" + raw


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


# --- Parent-reference (`&`) resolution -----------------------------------


def resolve_match_names(
    raw_names: list[str], parent_match_names: list[str]
) -> list[str]:
    """Substitute `&` in nested selectors against each parent simple
    selector, producing the resolved names a `find_symbols` query can
    reach.

    Rules:
    - Child without `&` passes through unchanged.
    - Child with `&` and N parents → N substitutions per child name.
    - Child with `&` and no parent (top-level use, invalid CSS but
      harmless to handle) → `&`-prefixed name kept as literal.

    For a multi-selector parent (`.a, .b { &__x { } }`) the child is
    findable under both resolved forms — `[".a__x", ".b__x"]`.
    """
    if not raw_names:
        return []
    if not parent_match_names:
        return list(raw_names)
    out: list[str] = []
    for raw in raw_names:
        if "&" not in raw:
            out.append(raw)
            continue
        for pn in parent_match_names:
            out.append(raw.replace("&", pn))
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def resolve_signature(raw_signature: str, parent_match_names: list[str]) -> str:
    """Substitute `&` in a rule's literal selector text against the
    parent's first match name, for display in the outline.

    For `& .item { }` under `.card`, returns `.card .item`. For
    multi-selector parents we use the first one (best-effort) — the
    outline shows one resolved variant; `match_names` carries the
    full set for matching. This is a display compromise: showing
    every variant in the signature line would balloon outline width
    for the common SCSS BEM pattern.

    Returns `raw_signature` unchanged when there's no `&` to resolve
    or no parent context.
    """
    if "&" not in raw_signature or not parent_match_names:
        return raw_signature
    return raw_signature.replace("&", parent_match_names[0])


# --- Doc / leading-comment detection -------------------------------------


def doc_start_byte_for(node: Node, src: bytes) -> tuple[int, list[str]]:
    """Find the contiguous `/* ... */` block(s) immediately preceding
    `node`, returning (start byte, raw lines).

    Walks backwards from `node.start_byte` over horizontal whitespace
    and at most ONE newline, expecting a `*/` boundary. Multiple
    consecutive comment blocks separated by a single newline are all
    included. A blank line breaks the chain (the comment becomes a
    stray annotation, not doc).

    Returns `(node.start_byte, [])` when no leading doc is found.
    """
    i = node.start_byte
    out_lines: list[str] = []
    earliest = node.start_byte
    while True:
        j = i
        newlines = 0
        while j > 0 and src[j - 1:j] in (b" ", b"\t", b"\n", b"\r"):
            if src[j - 1:j] == b"\n":
                newlines += 1
                if newlines > 1:
                    return earliest, out_lines
            j -= 1
        if j < 2 or src[j - 2:j] != b"*/":
            return earliest, out_lines
        start = src.rfind(b"/*", 0, j)
        if start < 0:
            return earliest, out_lines
        comment_text = src[start:j].decode("utf8", errors="replace")
        for line in reversed(comment_text.splitlines()):
            out_lines.insert(0, line)
        earliest = start
        i = start


# --- Filter helpers ------------------------------------------------------


def is_private_scss_name(name: str) -> bool:
    """Sass treats names with a leading `_` or `-` as private when the
    file is consumed via `@use` — they aren't exported. Mirror that
    convention so `--include-private=False` hides what the language
    itself hides.

    Strips the leading sigil (`$`, `%`) before checking, so
    `$_internal`, `%_internal`, `_internal` (mixin/function name) all
    read as private.
    """
    if not name:
        return False
    bare = name.lstrip("$%")
    return bare.startswith(("_", "-"))


def first_named_child(node: Node, type_name: str) -> Optional[Node]:
    """Return the first named child with the given type, or None."""
    for c in node.named_children:
        if c.type == type_name:
            return c
    return None


def text_of(node: Node, src: bytes) -> str:
    """Public alias for `_text`. Adapters use this when extracting
    signatures from arbitrary nodes."""
    return _text(node, src)


# --- Common at-rule / rule helpers ----------------------------------------


# Generic at-rule statement node types that wrap a block of rules.
# CSS produces typed nodes for the well-known forms (`media_statement`,
# `supports_statement`, `keyframes_statement`) and a generic `at_rule`
# for everything else (`@layer`, `@container`, `@font-face`, `@page`,
# …). We treat them uniformly: emit a `KIND_AT_RULE` Declaration whose
# children are the rules inside its `block`.
AT_RULE_STATEMENT_TYPES = {
    "media_statement",
    "supports_statement",
    "keyframes_statement",
    "at_rule",
}


def at_rule_signature(node: Node, src: bytes) -> str:
    """Render the at-rule's header — everything from the `@` keyword up
    to (but not including) the opening `{`. For `@media (max-width:
    768px) { ... }` returns `@media (max-width: 768px)`.

    Falls back to the `@`-keyword token alone if the block can't be
    located, so unusual at-rules still render readably.
    """
    block = first_named_child(node, "block")
    if block is None:
        block = first_named_child(node, "keyframe_block_list")
    end = block.start_byte if block is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    # Collapse internal whitespace so a multi-line at-rule header
    # (`@media (min-width: 600px)\n   and (max-width: 1024px)`) renders
    # as one tidy line in the outline.
    return " ".join(text.split())


def line_for(node: Node, src: bytes, *, end: bool = False) -> int:
    """1-based line number for the start (or end) byte of `node`."""
    target = node.end_byte if end else node.start_byte
    return src[:target].count(b"\n") + 1
