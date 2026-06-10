"""Rust adapter — parses .rs files via tree-sitter-rust into Declaration IR.

Design notes (how Rust concepts map to the IR):

- `mod_item`                       → KIND_NAMESPACE (recursive — modules
                                     can nest arbitrarily). External-file
                                     module references (`pub mod foo;`)
                                     also surface as KIND_NAMESPACE leaves
                                     so an agent can see the declared
                                     module surface even without the body.
- `struct_item`                    → KIND_STRUCT — covers regular,
                                     tuple-struct (`struct Pair(T, U);`),
                                     and unit-struct (`struct Unit;`)
                                     shapes. The signature line preserves
                                     the original form so the agent can
                                     tell them apart at a glance.
- `union_item`                     → KIND_STRUCT — Rust unions are
                                     structurally indistinguishable from
                                     structs for outline purposes; the
                                     `union` keyword survives in the
                                     rendered signature.
- `enum_item`                      → KIND_ENUM, with `enum_variant`
                                     children → KIND_ENUM_MEMBER. Variants
                                     keep their data shape in the
                                     signature (`Running(u32)`,
                                     `Done { code: i32 }`).
- `trait_item`                     → KIND_INTERFACE. Supertraits land in
                                     `bases` so the type header reads
                                     `Sub : Super`. Methods defined in
                                     the trait body (both forward
                                     `function_signature_item` and
                                     default-impl `function_item`)
                                     surface as KIND_METHOD children.
- `function_item` (top level)      → KIND_FUNCTION
- `function_item` (in `impl` body) → KIND_METHOD, regrouped under the
                                     impl's target type. Mirrors the Go
                                     adapter's two-pass strategy: methods
                                     are collected per impl block, then
                                     attached to the named type's children
                                     during pass 2.
- `function_signature_item`        → KIND_METHOD inside trait bodies (the
                                     forward-declaration form without a
                                     body). Treated identically to a
                                     bodied method for the outline.
- `impl_item` (`impl Foo`)         → contributes methods/consts/types
                                     into Foo's children. Inherent impl —
                                     no trait recorded.
- `impl_item` (`impl Trait for X`) → adds `Trait` to X's `bases` AND
                                     contributes the impl body's items
                                     to X's children. So X's type header
                                     reads `X : Trait`.
- `const_item` / `static_item`     → KIND_FIELD (top-level constants and
                                     statics have the same structural
                                     role in the outline).
- `type_item` (`type X = Y;`)      → KIND_DELEGATE (named type alias —
                                     same shape as Go's `type_alias`).
- `associated_type` (in trait)     → KIND_DELEGATE inside the trait body.
- `macro_definition`               → KIND_DELEGATE (a top-level named
                                     definition, distinct from any
                                     callable but the same surface from
                                     the agent's perspective: a name to
                                     find via `show`).
- `foreign_mod_item` (`extern "C"`) → KIND_NAMESPACE labelled by ABI,
                                     with `function_signature_item`
                                     children as KIND_FUNCTION and
                                     `static_item` children as
                                     KIND_FIELD.

**Visibility (Rust convention):** the `visibility_modifier` child token
holds the full visibility text. Mapping:
- `pub`                                          → "public"
- `pub(crate)` / `pub(super)` / `pub(self)` / `pub(in path)`
                                                 → "internal" (analogous
                                                   to C# `internal` —
                                                   restricted-pub)
- absent                                         → "private"
This lets `--no-private` filter module-private items the way it does for
other languages without losing the distinction between truly public and
crate-restricted exports.

**Doc comments:** Rust uses `/// ...` (outer line doc) or `/** ... */`
(outer block doc) IMMEDIATELY preceding an item. Tree-sitter-rust models
these as ordinary `line_comment` / `block_comment` nodes that contain an
`outer_doc_comment_marker` child. We walk back through `prev_sibling`
collecting contiguous outer-doc comments (stopping at blank lines, other
items, or non-doc comments). Inner docs (`//!`, `/*!`) attach to the
*enclosing* module — the source-file walk skips them.

**Attributes:** `attribute_item` (e.g. `#[derive(Debug)]`) appears as a
SIBLING node preceding the item. The same prev_sibling walk also
collects them and renders them in the `attrs` slot, so the agent sees
`#[derive(Debug)] pub struct Foo` style on a single outline line. Inner
attributes (`#![...]`, with `inner_attribute_item` type) at the file or
module level are not item-attached and are ignored.

**Trait impl bases:** for every `impl Trait for Type { ... }` block in a
file, the adapter records `Trait` in `Type`'s `bases`. The renderer then
surfaces every implemented trait in the type header as
`Type : Trait1, Trait2`, exactly the way an inherent base list reads
elsewhere — agents see all of a type's trait bounds at a glance,
without a separate query.

**Cross-file impl caveat:** if `impl Foo` (or `impl T for Foo`) lives in
a different file from where `struct Foo` is declared (legal Rust — and
common for trait-implementations of upstream types), the impl's methods
will surface at the top level of THAT file rather than nested under
`Foo`. Cross-file regrouping would require parsing the whole crate
together, which is out of scope; we operate per-file like every other
adapter.

**Generics, lifetimes, where clauses:** all preserved verbatim in
rendered signatures. Signature slicing simply cuts from the item's
start_byte to the start of its body (or end_byte for body-less forms),
so generic parameters, lifetime parameters, return types, and where
clauses all flow through naturally without explicit handling.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_rust as tsr
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_DELEGATE,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_STRUCT,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tsr.language())
_PARSER = Parser(_LANGUAGE)


class RustAdapter:
    language_name = "rust"
    display_name = "Rust"
    extensions = {".rs"}
    definition_keywords = frozenset({
        "fn", "struct", "enum", "trait", "impl", "type", "mod", "union",
    })
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('use ',)
    render_family = "code"
    # Lifetimes (`'a`, `'static`, `'_`) are UNPAIRED single quotes —
    # grep's quote-counting string heuristic must not treat them as
    # string delimiters, or every match after `&'a` on a line would
    # classify as [string]. See LanguageAdapter's optional attributes.
    single_quote_lifetimes = True

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        declarations: list[Declaration] = []
        _walk_items(tree.root_node, src, declarations)
        imports: list[str] = []
        # Piggyback byte-range collection for grep's import classifier.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        import_regions.sort()
        # `use` inside a `fn` / `impl` body is function-local — the
        # symbol it brings in is only visible inside that function.
        # Counting these (without listing them) tells the agent
        # "this file has additional locally-scoped uses" while keeping
        # the static imports list a clean module-level dependency map.
        # Nested `mod foo { use ... }` is NOT counted: it's a separate
        # scope but still load-time, and its `use` belongs to that
        # nested module's surface, not the parent file's.
        conditional_count = _count_conditional_imports(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=declarations,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_count,
            import_regions=import_regions,
        )


# --- Imports --------------------------------------------------------------


def _collect_imports(
    root: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    """Rust `use` declarations + legacy `extern crate`. Source-true text
    preserves visibility (`pub use ...` for re-exports), nested groups
    (`use foo::{Bar, Baz}`), aliasing (`as Qux`), and wildcards (`*`).

    `mod foo;` (submodule declaration without a body) is NOT included
    — it is a structural file-tree marker, not an import in the
    "where does this code pull from" sense. Keeping `--imports`
    semantically clean (only consumption-direction statements) avoids
    confusing the agent with mixed concepts.

    If ``regions`` is supplied, also appends each declaration's byte
    range — covers single-line ``use foo::Bar;`` AND multi-line group
    ``use foo::{\\n Bar,\\n};`` for grep's classifier."""
    for child in root.named_children:
        if child.type in ("use_declaration", "extern_crate_declaration"):
            text = _collapse_ws(_text(child, src)).rstrip(";").strip()
            if text:
                out.append(text)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))


# AST node types that put a `use` declaration into runtime / function-
# local scope. We deliberately do NOT include `mod_item` — a `use`
# inside `mod foo { ... }` belongs to that submodule's surface, not
# the parent file's, and is structurally distinct from a fn-local
# import (it's still loaded eagerly at compile time, just within a
# different namespace).
_RUST_RUNTIME_OR_SCOPED = frozenset({
    "function_item",
    "closure_expression",
})


def _count_conditional_imports(root: Node) -> int:
    """Count `use` and `extern crate` declarations that live inside a
    function body or closure — i.e. locally-scoped imports the agent
    would not see in the module-level imports list.

    `if` / `match` / `loop` blocks at the file's top level are rare in
    real Rust — and a top-level `use` cannot legally appear inside one
    anyway (the grammar parses `use` only at item position). The walk
    tracks scope through function/closure boundaries only.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        t = node.type
        if t in ("use_declaration", "extern_crate_declaration"):
            if in_scope:
                count += 1
            continue
        new_in_scope = in_scope or t in _RUST_RUNTIME_OR_SCOPED
        for c in node.children:
            stack.append((c, new_in_scope))
    return count


# --- Item walk -----------------------------------------------------------


def _walk_items(scope: Node, src: bytes, out: list[Declaration]) -> None:
    """Walk the children of a `source_file` or `declaration_list` and
    populate `out` with Declarations for the items found.

    Two-pass approach:
    - Pass 1: resolve each named child to a Declaration (or to a buffered
      impl block). Methods end up nested in the impl-block buffer rather
      than directly in `out`.
    - Pass 2: distribute impl-block contents:
        * inherent impls (`impl Foo { ... }`) → append items to `Foo`'s
          children (creating it on the fly only if Foo lives elsewhere
          AND we still want the methods to surface — in that case they
          drop to `out` at the current scope).
        * trait impls (`impl Tr for Foo { ... }`) → same children move,
          AND `Tr` is appended to `Foo`'s `bases`.
    Methods whose target type isn't declared locally surface at the
    current scope (parallel to Go's cross-file method rule).
    """
    type_index: dict[str, Declaration] = {}
    pending_impls: list[_ImplPending] = []

    for child in scope.named_children:
        kind = child.type

        # Comments / inner docs / inner attributes are not items.
        if kind in ("line_comment", "block_comment"):
            continue
        if kind == "attribute_item" or kind == "inner_attribute_item":
            continue
        # `use ...;`, `extern crate ...;` carry no structural surface.
        if kind in ("use_declaration", "extern_crate_declaration"):
            continue
        # Empty statements / expression statements at module level —
        # ignore.
        if kind in (";", "expression_statement"):
            continue

        if kind == "struct_item":
            d = _struct_to_decl(child, src)
            if d is not None:
                type_index[d.name] = d
                out.append(d)
            continue
        if kind == "union_item":
            d = _union_to_decl(child, src)
            if d is not None:
                type_index[d.name] = d
                out.append(d)
            continue
        if kind == "enum_item":
            d = _enum_to_decl(child, src)
            if d is not None:
                type_index[d.name] = d
                out.append(d)
            continue
        if kind == "trait_item":
            d = _trait_to_decl(child, src)
            if d is not None:
                type_index[d.name] = d
                out.append(d)
            continue
        if kind == "impl_item":
            pending = _impl_pending(child, src)
            if pending is not None:
                pending_impls.append(pending)
            continue
        if kind == "function_item":
            out.append(_function_to_decl(child, src, kind=KIND_FUNCTION))
            continue
        if kind == "function_signature_item":
            # Top-level forward declarations are vanishingly rare in real
            # Rust outside `extern` blocks (handled below). If one shows
            # up here, treat it as a function so it surfaces.
            out.append(_function_to_decl(child, src, kind=KIND_FUNCTION))
            continue
        if kind == "const_item":
            out.append(_const_or_static_to_field(child, src, keyword="const"))
            continue
        if kind == "static_item":
            out.append(_const_or_static_to_field(child, src, keyword="static"))
            continue
        if kind == "type_item":
            out.append(_type_alias_to_decl(child, src))
            continue
        if kind == "mod_item":
            d = _mod_to_decl(child, src)
            if d is not None:
                out.append(d)
            continue
        if kind == "macro_definition":
            d = _macro_to_decl(child, src)
            if d is not None:
                out.append(d)
            continue
        if kind == "foreign_mod_item":
            d = _foreign_mod_to_decl(child, src)
            if d is not None:
                out.append(d)
            continue
        # Unknown / not-an-item — skip silently.

    # Pass 2 — distribute impl-block items.
    for pending in pending_impls:
        target = type_index.get(pending.target)
        if target is None:
            # Receiver type lives elsewhere → spill items at this scope
            # so they're still discoverable.
            out.extend(pending.children)
            continue
        if pending.trait is not None and pending.trait not in target.bases:
            target.bases.append(pending.trait)
        if pending.children:
            target.children.extend(pending.children)
        # Always stretch the target's range to cover the impl block —
        # even when the impl is empty (`impl Marker for Foo {}`) — so
        # `show Foo` slices include every related impl, not just the
        # original struct/enum/union body.
        target.end_line = max(target.end_line, pending.end_line)
        target.end_byte = max(target.end_byte, pending.end_byte)


# --- Pending-impl record -------------------------------------------------


class _ImplPending:
    __slots__ = ("target", "trait", "children", "end_line", "end_byte")

    def __init__(
        self,
        target: str,
        trait: Optional[str],
        children: list[Declaration],
        end_line: int,
        end_byte: int,
    ):
        self.target = target
        self.trait = trait
        self.children = children
        self.end_line = end_line
        self.end_byte = end_byte


def _impl_pending(node: Node, src: bytes) -> Optional[_ImplPending]:
    """Resolve an `impl_item`:
    - target type name (drilled out of `type` field)
    - trait name (drilled out of `trait` field, or None for inherent impl)
    - the items inside the impl body, converted to KIND_METHOD /
      KIND_DELEGATE / KIND_FIELD declarations.

    Returns None if the target type can't be resolved to a bare name —
    that includes impls on tuple types, references, fn pointers, etc.,
    which can't be regrouped in a per-file outline.
    """
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return None
    target = _drill_to_type_name(type_node, src)
    if target is None:
        return None

    trait_name: Optional[str] = None
    trait_node = node.child_by_field_name("trait")
    if trait_node is not None:
        trait_name = _drill_to_type_name(trait_node, src) or _collapse_ws(_text(trait_node, src))

    body = node.child_by_field_name("body")
    children: list[Declaration] = []
    if body is not None:
        for c in body.named_children:
            ck = c.type
            if ck in ("line_comment", "block_comment"):
                continue
            if ck == "attribute_item" or ck == "inner_attribute_item":
                continue
            if ck == "function_item":
                children.append(_function_to_decl(c, src, kind=KIND_METHOD))
            elif ck == "function_signature_item":
                children.append(_function_to_decl(c, src, kind=KIND_METHOD))
            elif ck == "const_item":
                children.append(_const_or_static_to_field(c, src, keyword="const"))
            elif ck == "type_item":
                children.append(_type_alias_to_decl(c, src))
            elif ck == "associated_type":
                children.append(_associated_type_to_decl(c, src))
            elif ck == "macro_invocation":
                # `impl_item` bodies sometimes call declarative macros
                # that expand to method definitions (e.g. derive-style
                # boilerplate). We can't see what the macro produces, so
                # skip silently — the outline reflects what's literally
                # in the source.
                continue
    return _ImplPending(
        target=target,
        trait=trait_name,
        children=children,
        end_line=node.end_point[0] + 1,
        end_byte=node.end_byte,
    )


# --- Struct / union ------------------------------------------------------


def _struct_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    body = node.child_by_field_name("body")

    # Signature: from start of node up to body (excluding it). For
    # tuple-structs the body type is `ordered_field_declaration_list`
    # which is part of the signature shape (`pub struct Pair(i32, i32);`)
    # — we keep the whole node text and trim the trailing `;` only.
    if body is not None and body.type == "field_declaration_list":
        sig = _slice_until_byte(node.start_byte, body.start_byte, src)
        sig = sig.rstrip(" {").rstrip()
    else:
        sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()

    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)

    children: list[Declaration] = []
    if body is not None:
        if body.type == "field_declaration_list":
            children = _struct_fields_to_decls(body, src)
        elif body.type == "ordered_field_declaration_list":
            children = _tuple_fields_to_decls(body, src)

    return Declaration(
        kind=KIND_STRUCT,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _union_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`union_item` → KIND_STRUCT (same shape as a regular struct)."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    body = node.child_by_field_name("body")
    sig = (
        _slice_until_byte(node.start_byte, body.start_byte, src).rstrip(" {").rstrip()
        if body is not None
        else _collapse_ws(_text(node, src))
    )
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)
    children: list[Declaration] = []
    if body is not None and body.type == "field_declaration_list":
        children = _struct_fields_to_decls(body, src)
    return Declaration(
        kind=KIND_STRUCT,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _struct_fields_to_decls(body: Node, src: bytes) -> list[Declaration]:
    """Walk a `field_declaration_list` (regular struct body / union)."""
    out: list[Declaration] = []
    for fd in body.named_children:
        if fd.type != "field_declaration":
            continue
        name_node = fd.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, src)
        sig = _collapse_ws(_text(fd, src))
        out.append(
            Declaration(
                kind=KIND_FIELD,
                name=name,
                signature=sig,
                visibility=_visibility(fd, src),
                start_line=fd.start_point[0] + 1,
                end_line=fd.end_point[0] + 1,
                start_byte=fd.start_byte,
                end_byte=fd.end_byte,
            )
        )
    return out


def _tuple_fields_to_decls(body: Node, src: bytes) -> list[Declaration]:
    """Walk an `ordered_field_declaration_list` — the body of tuple-structs
    like `pub struct Pair(pub i32, i32)`. Each positional field becomes a
    KIND_FIELD with the synthetic name `0`, `1`, ... (matching how they're
    accessed in Rust source: `pair.0`, `pair.1`).

    tree-sitter-rust renders the field list as a flat sequence of
    `visibility_modifier?` + a type node per field. We track the
    visibility flag (carried over to the next type encountered) and emit
    one Declaration per type child.
    """
    out: list[Declaration] = []
    pending_visibility = "private"
    pending_attrs: list[str] = []
    index = 0
    for c in body.named_children:
        if c.type == "visibility_modifier":
            text = _text(c, src).strip()
            pending_visibility = "public" if text == "pub" else "internal"
            continue
        if c.type == "attribute_item":
            pending_attrs.append(_collapse_ws(_text(c, src)))
            continue
        # Otherwise, c is a type node for the next positional field.
        sig = _collapse_ws(_text(c, src))
        if pending_visibility == "public":
            sig = "pub " + sig
        elif pending_visibility == "internal":
            sig = "pub(...) " + sig
        out.append(
            Declaration(
                kind=KIND_FIELD,
                name=str(index),
                signature=sig,
                attrs=pending_attrs,
                visibility=pending_visibility,
                start_line=c.start_point[0] + 1,
                end_line=c.end_point[0] + 1,
                start_byte=c.start_byte,
                end_byte=c.end_byte,
            )
        )
        index += 1
        pending_visibility = "private"
        pending_attrs = []
    return out


# --- Enum ---------------------------------------------------------------


def _enum_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    body = node.child_by_field_name("body")
    sig = (
        _slice_until_byte(node.start_byte, body.start_byte, src).rstrip(" {").rstrip()
        if body is not None
        else _collapse_ws(_text(node, src))
    )
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)

    variants: list[Declaration] = []
    if body is not None:
        for v in body.named_children:
            if v.type != "enum_variant":
                continue
            v_name_node = v.child_by_field_name("name")
            if v_name_node is None:
                # Fall back: first identifier child.
                for c in v.named_children:
                    if c.type == "identifier":
                        v_name_node = c
                        break
            if v_name_node is None:
                continue
            v_name = _text(v_name_node, src)
            v_sig = _collapse_ws(_text(v, src)).rstrip(",").rstrip()
            # Enum variants are always at least as visible as the enum
            # itself — there's no per-variant visibility modifier in
            # Rust. Marking them "public" prevents `--no-private` from
            # silently hiding every variant of a `pub enum`.
            variants.append(
                Declaration(
                    kind=KIND_ENUM_MEMBER,
                    name=v_name,
                    signature=v_sig,
                    visibility="public",
                    start_line=v.start_point[0] + 1,
                    end_line=v.end_point[0] + 1,
                    start_byte=v.start_byte,
                    end_byte=v.end_byte,
                )
            )

    return Declaration(
        kind=KIND_ENUM,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=variants,
    )


# --- Trait --------------------------------------------------------------


def _trait_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    body = node.child_by_field_name("body")
    sig = (
        _slice_until_byte(node.start_byte, body.start_byte, src).rstrip(" {").rstrip()
        if body is not None
        else _collapse_ws(_text(node, src))
    )
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)

    bases: list[str] = []
    bounds = node.child_by_field_name("bounds")
    if bounds is not None:
        bases = _trait_bound_names(bounds, src)

    members: list[Declaration] = []
    if body is not None:
        for m in body.named_children:
            mk = m.type
            if mk in ("line_comment", "block_comment"):
                continue
            if mk in ("attribute_item", "inner_attribute_item"):
                continue
            if mk == "function_item" or mk == "function_signature_item":
                members.append(_function_to_decl(m, src, kind=KIND_METHOD))
            elif mk == "const_item":
                members.append(_const_or_static_to_field(m, src, keyword="const"))
            elif mk == "associated_type":
                members.append(_associated_type_to_decl(m, src))
            elif mk == "type_item":
                members.append(_type_alias_to_decl(m, src))

    return Declaration(
        kind=KIND_INTERFACE,
        native_kind="trait",
        name=name,
        signature=sig,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=members,
    )


def _trait_bound_names(bounds: Node, src: bytes) -> list[str]:
    """Extract bare names from a `trait_bounds` node (`: A + B + 'static`).

    Lifetimes (`'static`, `'a`) are NOT real super-traits and would
    pollute the rendered `bases` list, so we skip them. Generic bounds
    (`Iterator<Item = T>`) drill to their leading `type_identifier`.
    """
    names: list[str] = []
    for c in bounds.named_children:
        if c.type == "lifetime":
            continue
        n = _drill_to_type_name(c, src)
        if n:
            names.append(n)
    return names


# --- Function / method --------------------------------------------------


def _function_to_decl(node: Node, src: bytes, *, kind: str) -> Declaration:
    """Handles both `function_item` (with body) and
    `function_signature_item` (no body — trait forward decls). Slices the
    signature from start_byte to body.start_byte; for sig-items, the
    whole node is the signature minus the trailing `;`.
    """
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "?"
    body = node.child_by_field_name("body")
    if body is not None:
        sig = _slice_until_byte(node.start_byte, body.start_byte, src)
    else:
        sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()
    sig = sig.rstrip(" {").rstrip()

    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)

    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


# --- Const / static / type alias ---------------------------------------


def _const_or_static_to_field(node: Node, src: bytes, *, keyword: str) -> Declaration:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "?"
    sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _type_alias_to_decl(node: Node, src: bytes) -> Declaration:
    """`type Alias = Foo<u32>;` — KIND_DELEGATE."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "?"
    sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)
    return Declaration(
        kind=KIND_DELEGATE,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _associated_type_to_decl(node: Node, src: bytes) -> Declaration:
    """`type Item;` (in trait body, no `=`) — also KIND_DELEGATE.

    Visibility on associated items follows the enclosing trait. We mark
    them "public" so they don't get filtered by `--no-private`, since a
    trait's associated types are part of its public surface by
    definition.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for c in node.named_children:
            if c.type == "type_identifier":
                name_node = c
                break
    name = _text(name_node, src) if name_node is not None else "?"
    sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()
    return Declaration(
        kind=KIND_DELEGATE,
        name=name,
        signature=sig,
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Module / foreign module / macro -----------------------------------


def _mod_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`mod foo { ... }` or `pub mod foo;` — KIND_NAMESPACE.

    The body-less form (`pub mod foo;` — file-defined module) becomes a
    childless namespace so the outline still surfaces the declared module
    name. With a body we recurse into it via `_walk_items`.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    body = node.child_by_field_name("body")

    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = _visibility(node, src)

    # Signature carries the bare `mod foo` form; the renderer suppresses
    # this for namespaces (it emits its own `namespace name` line), but
    # we keep the source slice for downstream tools that do read the
    # signature.
    if body is not None:
        sig = _slice_until_byte(node.start_byte, body.start_byte, src).rstrip(" {").rstrip()
    else:
        sig = _collapse_ws(_text(node, src)).rstrip(";").rstrip()

    children: list[Declaration] = []
    if body is not None:
        _walk_items(body, src, children)

    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _foreign_mod_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`extern "C" { ... }` — surface as a namespace named after the ABI
    string so the agent sees foreign declarations grouped together
    (rather than buried as anonymous fns at module level).

    Items inside an extern block are ALWAYS forward declarations
    (`function_signature_item`, `static_item`); methods don't apply.
    """
    body = node.child_by_field_name("body")
    if body is None:
        return None

    # Name: use the ABI string verbatim (`extern "C"` → `extern "C"`).
    # Falls back to bare `extern` when the modifier omits the string.
    abi_text = "extern"
    for c in node.named_children:
        if c.type == "extern_modifier":
            abi_text = _collapse_ws(_text(c, src))
            break

    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)

    children: list[Declaration] = []
    for c in body.named_children:
        ck = c.type
        if ck in ("line_comment", "block_comment"):
            continue
        if ck in ("attribute_item", "inner_attribute_item"):
            continue
        if ck == "function_signature_item" or ck == "function_item":
            children.append(_function_to_decl(c, src, kind=KIND_FUNCTION))
        elif ck == "static_item":
            children.append(_const_or_static_to_field(c, src, keyword="static"))

    return Declaration(
        kind=KIND_NAMESPACE,
        name=abi_text,
        signature=abi_text,
        attrs=attrs,
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
        children=children,
    )


def _macro_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`macro_rules! foo { ... }` → KIND_DELEGATE.

    Rust macros have NO `visibility_modifier` syntax — visibility is
    instead controlled by the `#[macro_export]` attribute. Default to
    "private" (module-local) and promote to "public" only when that
    attribute is present, mirroring real Rust semantics so `--no-private`
    correctly hides unexported macros.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    sig = f"macro_rules! {name}"
    docs, doc_start = _outer_docs(node, src)
    attrs = _attrs_before(node, src)
    visibility = "public" if any("macro_export" in a for a in attrs) else "private"
    return Declaration(
        kind=KIND_DELEGATE,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


# --- Visibility / docs / attrs -----------------------------------------


def _visibility(node: Node, src: bytes) -> str:
    """Read the `visibility_modifier` child of an item.

    `pub`                  → "public"
    `pub(crate|super|...)` → "internal"
    absent                 → "private"
    """
    for c in node.children:
        if c.type == "visibility_modifier":
            text = _text(c, src).strip()
            if text == "pub":
                return "public"
            return "internal"
    return "private"


def _outer_docs(node: Node, src: bytes) -> tuple[list[str], int]:
    """Walk preceding sibling comments collecting outer-doc comments
    (`///` or `/** */`). Stops at:
    - a non-comment, non-attribute sibling
    - a blank-line gap (≥ 1 fully blank line between the comment's end
      and the next-thing's start)
    - a non-doc comment (regular `//` or `/* */`)

    Attributes (`#[...]`) interleaved with doc comments are skipped over,
    since their order is doc → attr → item or attr → doc → item — both
    are common — and we want all preceding docs regardless.

    Gap detection counts newlines in the source bytes between the
    sibling's `end_byte` and the next anchor's `start_byte` (which is
    either `node.start_byte` for the first iteration or the previously
    walked sibling's `start_byte` thereafter). More than one newline in
    that span means there is at least one fully blank line between them,
    which detaches the comment from the item.

    Returns: (docs_in_source_order, doc_start_byte_or_node_start_byte).
    """
    docs: list[str] = []
    first_doc_start: Optional[int] = None
    sib = node.prev_sibling
    next_anchor_start: int = node.start_byte
    while sib is not None:
        if sib.type == "attribute_item":
            if _has_blank_line_between(sib.end_byte, next_anchor_start, src):
                break
            next_anchor_start = sib.start_byte
            sib = sib.prev_sibling
            continue
        if sib.type in ("line_comment", "block_comment"):
            if not _is_outer_doc_comment(sib):
                # Regular `// ...` comment — stops the doc chain.
                break
            if _has_blank_line_between(sib.end_byte, next_anchor_start, src):
                break
            # tree-sitter-rust includes the trailing newline in line_comment
            # ranges, so naive text would render a blank line after each
            # `///`. Strip trailing whitespace per doc line.
            docs.insert(0, _text(sib, src).rstrip())
            first_doc_start = sib.start_byte
            next_anchor_start = sib.start_byte
            sib = sib.prev_sibling
            continue
        break
    doc_start = first_doc_start if first_doc_start is not None else node.start_byte
    return docs, doc_start


def _has_blank_line_between(start: int, end: int, src: bytes) -> bool:
    """True if the bytes from `start` to `end` represent at least one
    fully blank line separating two anchors.

    Quirk this exists to handle: tree-sitter-rust's `line_comment` byte
    range INCLUDES the trailing `\\n`, but `attribute_item` does NOT. A
    naive `src.count(b'\\n', start, end) > 1` rule would (a) miss the
    blank-line case for `///` (only sees the gap's `\\n`, comment's own
    `\\n` was already eaten) and (b) wrongly fire on direct-attachment
    of `#[...]\\n<item>` (sees the one separator newline).

    The fix: also count the trailing `\\n` *of the sibling itself* when
    present. After that adjustment the rule is uniform — `>= 2` line
    breaks total means a blank line.
    """
    if end <= start:
        # Sibling ranges already overlap / are adjacent — no gap.
        return False
    nl = src.count(b"\n", start, end)
    if start > 0 and src[start - 1:start] == b"\n":
        nl += 1
    return nl >= 2


def _is_outer_doc_comment(node: Node) -> bool:
    """A comment node carries `outer_doc_comment_marker` field iff it's
    a `///` or `/** */` outer-doc comment. Inner-doc (`//!`, `/*!`) and
    plain comments do not.
    """
    if node.type not in ("line_comment", "block_comment"):
        return False
    for c in node.children:
        if c.type == "outer_doc_comment_marker":
            return True
    return False


def _attrs_before(node: Node, src: bytes) -> list[str]:
    """Collect all `#[...]` attributes immediately preceding `node`,
    crossing over interleaved doc comments. Return them in source order.
    Same blank-line detachment rule as `_outer_docs`.
    """
    attrs: list[str] = []
    sib = node.prev_sibling
    next_anchor_start: int = node.start_byte
    while sib is not None:
        if sib.type == "attribute_item":
            if _has_blank_line_between(sib.end_byte, next_anchor_start, src):
                break
            attrs.insert(0, _collapse_ws(_text(sib, src)))
            next_anchor_start = sib.start_byte
            sib = sib.prev_sibling
            continue
        if sib.type in ("line_comment", "block_comment"):
            # Walk past doc / non-doc comments without collecting them,
            # but a blank-line gap still detaches further-up attrs.
            if _has_blank_line_between(sib.end_byte, next_anchor_start, src):
                break
            next_anchor_start = sib.start_byte
            sib = sib.prev_sibling
            continue
        break
    return attrs


# --- Type-name drilling -------------------------------------------------


def _drill_to_type_name(node: Node, src: bytes) -> Optional[str]:
    """Walk a type-expression node down to its leading bare type name.

    Handles every common shape that can appear in `impl Foo`,
    `impl Trait for Foo`, struct field types, trait bounds:

        Foo                            → "Foo"
        &Foo                           → "Foo"
        &'a mut Foo                    → "Foo"
        Foo<T, U>                      → "Foo"
        &Foo<T>                        → "Foo"
        std::collections::HashMap      → "HashMap"
        crate::module::Foo<T>          → "Foo"
        Box<Foo>                       → "Box" (caller decides whether
                                                that's the right base)

    Returns None for tuple types, function pointers, or anything that
    doesn't reduce to a single identifier.
    """
    t = node.type
    if t == "type_identifier":
        return _text(node, src)
    if t == "scoped_type_identifier":
        # `std::path::Path` → "Path" (the trailing identifier).
        ids = [c for c in node.named_children if c.type == "type_identifier"]
        if ids:
            return _text(ids[-1], src)
        return None
    if t == "generic_type":
        for c in node.named_children:
            if c.type == "type_identifier":
                return _text(c, src)
            if c.type == "scoped_type_identifier":
                return _drill_to_type_name(c, src)
        return None
    if t == "reference_type":
        # `&Foo` / `&'a mut Foo` — drill into named children.
        for c in node.named_children:
            r = _drill_to_type_name(c, src)
            if r is not None:
                return r
        return None
    if t == "scoped_identifier":
        ids = [c for c in node.named_children if c.type in ("identifier", "type_identifier")]
        if ids:
            return _text(ids[-1], src)
    return None


# --- Slice / text helpers ----------------------------------------------


def _slice_until_byte(start: int, end: int, src: bytes) -> str:
    return _collapse_ws(src[start:end].decode("utf8", errors="replace"))


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")
