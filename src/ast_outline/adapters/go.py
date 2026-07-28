"""Go adapter — parses .go files via tree-sitter-go into Declaration IR.

Design notes (how Go concepts map to the IR):

- `package_clause`               → KIND_NAMESPACE (single per file; absorbs
                                   all top-level declarations like Java/Kotlin)
- `type_declaration` →
    `type_spec` with `struct_type`     → KIND_STRUCT
    `type_spec` with `interface_type`  → KIND_INTERFACE
    `type_spec` with bare type ident   → KIND_DELEGATE (named/defined type
                                          like `type UserID int64`)
    `type_alias` (with `=`)            → KIND_DELEGATE (real alias)
- `function_declaration`         → KIND_FUNCTION at top level
- `method_declaration`           → KIND_METHOD, **regrouped under the
                                   receiver type's declaration** so
                                   `outline` reads naturally even though
                                   Go's AST lays methods flat at file level
- `const_declaration` /
  `var_declaration`              → KIND_FIELD per spec (block forms produce
                                   one Declaration per const_spec/var_spec)

**Visibility (Go convention):** Go has no explicit modifier. The first
character of the identifier determines exportedness:
- Capital letter → "public"
- Lowercase letter → "private"
This is the universally-understood Go rule; the adapter encodes it.

**Doc comments:** Go uses `//` line comments. The convention is that doc
comments are a contiguous block of `//` lines immediately preceding the
declaration, traditionally starting with the declaration's name (`Foo
returns ...`). The adapter walks back through `prev_sibling` collecting
`comment` nodes. Block comments `/* ... */` are also `comment` nodes in
tree-sitter-go and are accepted.

**Embedded types as `bases`:** Go has no `extends` keyword, but uses
embedding for the same effect:
- `type Dog struct { Animal; Breed string }` — `Animal` is embedded;
  bases = ["Animal"]
- `type Walker interface { Movable; Walk() }` — `Movable` is embedded;
  bases = ["Movable"]
- Pointer embedding `*Animal` is also supported; the base name is the
  underlying type identifier.

This makes type headers like `Foo : Animal` render correctly for the
dominant Go inheritance idiom (struct embedding). It does NOT detect
implicit interface-method-set satisfaction — that requires full
type-checking, which is out of scope.

**Generics (Go 1.18+):** `type_parameter_list` `[T any]` survives in the
rendered signature; no special handling beyond inclusion.

**Method grouping caveat:** if a method's receiver type is declared in a
DIFFERENT file (which is legal Go — methods can live in any file in the
same package), the method will surface at top level inside the namespace
rather than nested under the type. Cross-file regrouping is intentionally
out of scope; it would require parsing the whole package together, and
we operate per-file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_go as tsg
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors, read_source
from ..core import (
    KIND_CLASS,
    KIND_DELEGATE,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_STRUCT,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tsg.language())
_PARSER = Parser(_LANGUAGE)


class GoAdapter:
    language_name = "go"
    display_name = "Go"
    extensions = {".go"}
    definition_keywords = frozenset({"func", "type"})
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('import ',)
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = read_source(path)
        tree = _PARSER.parse(src)
        declarations: list[Declaration] = []
        _walk_top(tree.root_node, src, declarations)
        imports: list[str] = []
        # Piggyback byte-range collection for grep's import classifier.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        import_regions.sort()
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=declarations,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            import_regions=import_regions,
        )


# --- Imports --------------------------------------------------------------


def _collect_imports(
    root: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    """Go imports come in two shapes: single-line `import "fmt"` and
    grouped `import ( "a"; "b" )`. We flatten both into a flat list of
    `import <spec>` lines, each carrying one spec — agents see the same
    syntactic form regardless of whether the source used grouping. The
    spec text preserves package alias (`f "fmt"`) and blank-import
    sentinel (`_ "side/effect"`) verbatim.

    If ``regions`` is supplied, also appends each ``import_declaration``
    byte range — covers single-line AND grouped forms in one pass. Lets
    grep classify package paths inside ``import (...)`` blocks as
    ``[import]`` (see Python adapter for full rationale)."""
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        if regions is not None:
            regions.append((child.start_byte, child.end_byte))
        for sub in child.named_children:
            if sub.type == "import_spec":
                out.append(f"import {_collapse_ws(_text(sub, src)).strip()}")
            elif sub.type == "import_spec_list":
                for spec in sub.named_children:
                    if spec.type == "import_spec":
                        out.append(f"import {_collapse_ws(_text(spec, src)).strip()}")


# --- Top-level walk -------------------------------------------------------


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Two-pass walk.

    Pass 1: scan named children, building Declarations for types,
    functions, fields. Pending methods (`method_declaration`) are
    buffered with their receiver name.

    Pass 2: distribute buffered methods into their receiver-type's
    children list. Methods with no matching local type stay at the top
    level (their receiver lives in another file of the same package).
    """
    package_ns: Optional[Declaration] = None
    type_index: dict[str, Declaration] = {}
    pending_methods: list[tuple[str, Declaration]] = []

    sink: list[Declaration] = out

    for child in node.named_children:
        kind = child.type
        if kind == "package_clause":
            package_ns = _package_to_decl(child, src)
            out.append(package_ns)
            sink = package_ns.children
            continue
        if kind == "import_declaration":
            continue
        if kind == "comment":
            continue
        if kind == "type_declaration":
            for d in _type_declaration_to_decls(child, src):
                if d.kind in (KIND_STRUCT, KIND_INTERFACE):
                    type_index[d.name] = d
                sink.append(d)
            continue
        if kind == "function_declaration":
            sink.append(_function_to_decl(child, src))
            continue
        if kind == "method_declaration":
            recv = _receiver_type_name(child, src)
            decl = _method_to_decl(child, src)
            if recv is not None:
                pending_methods.append((recv, decl))
            else:
                sink.append(decl)
            continue
        if kind == "const_declaration":
            sink.extend(_const_var_to_decls(child, src, kind_name="const"))
            continue
        if kind == "var_declaration":
            sink.extend(_const_var_to_decls(child, src, kind_name="var"))
            continue
        # Anything else (e.g. stray expression) is skipped.

    # Pass 2 — attach methods to their receiver types.
    for recv, method in pending_methods:
        target = type_index.get(recv)
        if target is not None:
            target.children.append(method)
            target.end_line = max(target.end_line, method.end_line)
            target.end_byte = max(target.end_byte, method.end_byte)
        else:
            # Receiver type not declared in this file → leave at top level
            # (or inside the namespace) so the method is still visible.
            sink.append(method)

    # Stretch the namespace's line range to cover everything it absorbed.
    # Max over ALL children, not just the last one: pass 2 extends a
    # receiver type's range to its regrouped methods, so the child that
    # ends furthest in the source isn't necessarily the last list entry
    # (a plain function declared between a struct and its methods would
    # otherwise clip the namespace short).
    if package_ns is not None and package_ns.children:
        package_ns.end_line = max(
            package_ns.end_line,
            max(c.end_line for c in package_ns.children),
        )
        package_ns.end_byte = max(
            package_ns.end_byte,
            max(c.end_byte for c in package_ns.children),
        )


def _package_to_decl(node: Node, src: bytes) -> Declaration:
    """`package foo` — name lives as a `package_identifier` child."""
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "package_identifier":
            name_node = c
            break
    name = _text(name_node, src) if name_node is not None else ""
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"package {name}" if name else "package",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Type declarations ----------------------------------------------------


def _type_declaration_to_decls(node: Node, src: bytes) -> list[Declaration]:
    """A `type_declaration` may wrap one or more specs:
    - single inline:  `type X struct { ... }`
    - block:           `type ( X struct{ ... }; Y interface{ ... } )`

    Each spec/alias becomes its own Declaration. Doc comment is shared
    between siblings of the same `type_declaration` node; we attach it
    only to the FIRST spec to avoid duplication.
    """
    out: list[Declaration] = []
    seen_first = False
    for c in node.named_children:
        if c.type == "type_spec":
            d = _type_spec_to_decl(c, src, attach_outer_doc=node if not seen_first else None)
            if d is not None:
                out.append(d)
                seen_first = True
        elif c.type == "type_alias":
            d = _type_alias_to_decl(c, src, attach_outer_doc=node if not seen_first else None)
            if d is not None:
                out.append(d)
                seen_first = True
        # comments inside parenthesised block — skip
    return out


def _type_spec_to_decl(
    node: Node, src: bytes, *, attach_outer_doc: Optional[Node]
) -> Optional[Declaration]:
    """`type X struct { ... }` / `type X interface { ... }` /
    `type X int64` (defined type — newtype-shaped).
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return None

    # Doc comments live on the parent `type_declaration`; siblings include
    # leading `//` comments. For inline `type X struct { ... }`, the
    # `type_declaration` itself is a sibling of the doc comments. For a
    # block `type ( ... )`, only the FIRST spec gets the outer doc.
    docs_anchor: Node = attach_outer_doc if attach_outer_doc is not None else node
    docs = _go_docs(docs_anchor, src)
    doc_start = _resolved_doc_start(docs_anchor, src)

    visibility = _go_visibility(name)

    if type_node.type == "struct_type":
        children, bases = _struct_members_and_bases(type_node, src)
        signature = _slice_until(
            node.start_byte, type_node, src, "field_declaration_list", default_to_node=node
        )
        # Prepend `type` so the rendered line reads as a complete Go
        # statement (`type Animal struct` vs the bare `Animal struct`),
        # consistent with how we present aliases / newtypes below.
        if not signature.startswith("type "):
            signature = "type " + signature
        return Declaration(
            kind=KIND_STRUCT,
            name=name,
            signature=signature,
            bases=bases,
            docs=docs,
            visibility=visibility,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            doc_start_byte=doc_start,
            children=children,
        )
    if type_node.type == "interface_type":
        children, bases = _interface_members_and_bases(type_node, src)
        signature = _slice_until_brace(node.start_byte, type_node, src, default_to_node=node)
        if not signature.startswith("type "):
            signature = "type " + signature
        return Declaration(
            kind=KIND_INTERFACE,
            name=name,
            signature=signature,
            bases=bases,
            docs=docs,
            visibility=visibility,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            doc_start_byte=doc_start,
            children=children,
        )

    # `type X int64` — defined type / newtype. Treat as KIND_DELEGATE
    # (named type synonym shape). Bases = the underlying type, surfaced
    # in the type header so `type X Animal` reads as `X : Animal`.
    bases: list[str] = []
    base_text = _collapse_ws(_text(type_node, src))
    if base_text:
        bases.append(base_text)
    sig = _collapse_ws(_text(node, src))
    return Declaration(
        kind=KIND_DELEGATE,
        name=name,
        signature=f"type {sig}" if not sig.startswith("type ") else sig,
        bases=bases,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


def _type_alias_to_decl(
    node: Node, src: bytes, *, attach_outer_doc: Optional[Node]
) -> Optional[Declaration]:
    """`type Handler = func(string) error` — Go 1.9+ real alias."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    docs_anchor = attach_outer_doc if attach_outer_doc is not None else node
    docs = _go_docs(docs_anchor, src)
    doc_start = _resolved_doc_start(docs_anchor, src)
    sig = _collapse_ws(_text(node, src))
    return Declaration(
        kind=KIND_DELEGATE,
        name=_text(name_node, src),
        signature=f"type {sig}" if not sig.startswith("type ") else sig,
        docs=docs,
        visibility=_go_visibility(_text(name_node, src)),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


# --- Struct / interface bodies ------------------------------------------


def _struct_members_and_bases(
    struct_node: Node, src: bytes
) -> tuple[list[Declaration], list[str]]:
    """Walk a `struct_type` body. Each `field_declaration` is either:
    - regular field: has `field_identifier` + a type → KIND_FIELD
    - embedded type: ONLY has a type, no field_identifier → base
    - pointer-embedded `*Foo`: same — base = "Foo"
    """
    members: list[Declaration] = []
    bases: list[str] = []
    body = None
    for c in struct_node.children:
        if c.type == "field_declaration_list":
            body = c
            break
    if body is None:
        return members, bases

    for fd in body.named_children:
        if fd.type != "field_declaration":
            continue
        # If there is at least one `field_identifier`, it's a named field.
        ids = [c for c in fd.named_children if c.type == "field_identifier"]
        if ids:
            # Multi-name field (`X, Y T`) → use first name for the
            # outline entry, keep the full slice as signature so the
            # type still surfaces.
            first_name = _text(ids[0], src)
            sig = _collapse_ws(_text(fd, src))
            members.append(
                Declaration(
                    kind=KIND_FIELD,
                    name=first_name,
                    signature=sig,
                    visibility=_go_visibility(first_name),
                    start_line=fd.start_point[0] + 1,
                    end_line=fd.end_point[0] + 1,
                    start_byte=fd.start_byte,
                    end_byte=fd.end_byte,
                )
            )
        else:
            # Embedded type. The base name is the leaf type_identifier;
            # `*Foo` (pointer-embed) drills through `pointer_type`.
            base = _embedded_base_name(fd, src)
            if base:
                bases.append(base)
    return members, bases


def _interface_members_and_bases(
    iface_node: Node, src: bytes
) -> tuple[list[Declaration], list[str]]:
    """Walk an `interface_type` body. Children:
    - `method_elem` → method declaration
    - `type_elem` → embedded interface (base)
    """
    members: list[Declaration] = []
    bases: list[str] = []
    for c in iface_node.named_children:
        if c.type == "method_elem":
            name_node = c.child_by_field_name("name")
            if name_node is None:
                continue
            sig = _collapse_ws(_text(c, src))
            members.append(
                Declaration(
                    kind=KIND_METHOD,
                    name=_text(name_node, src),
                    signature=sig,
                    visibility=_go_visibility(_text(name_node, src)),
                    start_line=c.start_point[0] + 1,
                    end_line=c.end_point[0] + 1,
                    start_byte=c.start_byte,
                    end_byte=c.end_byte,
                )
            )
        elif c.type == "type_elem":
            # `type_elem` wraps a `type_identifier`, a `qualified_type`
            # (embedded `io.Reader`), or a union of types in constraint
            # syntax (`int | float64` — several children). Collect every
            # named entry: stopping at the first one dropped the rest of
            # a union, and skipping `qualified_type` lost every
            # package-qualified embedding.
            for cc in c.named_children:
                if cc.type == "type_identifier":
                    bases.append(_text(cc, src))
                elif cc.type == "qualified_type":
                    bases.append(_collapse_ws(_text(cc, src)))
    return members, bases


def _embedded_base_name(fd: Node, src: bytes) -> Optional[str]:
    """For an embedded `field_declaration` (no field_identifier), pull
    the underlying type name. Handles plain `Foo`, pointer `*Foo`,
    qualified `pkg.Foo`, and generic `Foo[T]` / `*Foo[T]` — all surface
    in the rendered type header as `: Foo`.
    """
    for c in fd.named_children:
        # Prefer the bare leading type-identifier (works for plain,
        # pointer, and generic-receiver shapes).
        name = _drill_to_type_identifier(c, src)
        if name is not None:
            return name
        # Fallback for qualified / generic types whose stripped form
        # still preserves package prefix or generics — keep verbatim;
        # downstream normalisation cleans them up.
        if c.type in ("qualified_type", "generic_type"):
            return _collapse_ws(_text(c, src))
    return None


# --- Functions / methods --------------------------------------------------


def _function_to_decl(node: Node, src: bytes) -> Declaration:
    """Top-level `func Foo(...) ...`. KIND_FUNCTION."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "?"
    docs = _go_docs(node, src)
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    sig = _collapse_ws(src[node.start_byte:end].decode("utf8", errors="replace"))
    return Declaration(
        kind=KIND_FUNCTION,
        name=name,
        signature=sig.rstrip(" {").rstrip(),
        docs=docs,
        visibility=_go_visibility(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _method_to_decl(node: Node, src: bytes) -> Declaration:
    """`func (r *Recv) Method(...) ...`. KIND_METHOD. Receiver is part
    of the rendered signature (e.g. `func (a *Animal) Sound() string`),
    so the agent sees both name and binding without an extra lookup.
    """
    name_node = node.child_by_field_name("name")
    name = _text(name_node, src) if name_node is not None else "?"
    docs = _go_docs(node, src)
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    sig = _collapse_ws(src[node.start_byte:end].decode("utf8", errors="replace"))
    return Declaration(
        kind=KIND_METHOD,
        name=name,
        signature=sig.rstrip(" {").rstrip(),
        docs=docs,
        visibility=_go_visibility(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _receiver_type_name(method: Node, src: bytes) -> Optional[str]:
    """Pull the bare type name out of a method's receiver list:
    `(a *Animal)` → "Animal", `(a Animal)` → "Animal",
    `(a *Foo[T])` → "Foo", `(a Foo[T])` → "Foo".

    Generic types nested inside pointer types (`*Stack[T]`) require a
    recursive drill — early versions of this function only matched
    `pointer_type → type_identifier` and silently failed to group
    methods on generic receivers.
    """
    rcv = method.child_by_field_name("receiver")
    if rcv is None:
        # Fallback: receiver is the FIRST parameter_list child of method
        for c in method.children:
            if c.type == "parameter_list":
                rcv = c
                break
        if rcv is None:
            return None
    for param in rcv.named_children:
        if param.type != "parameter_declaration":
            continue
        for c in param.named_children:
            name = _drill_to_type_identifier(c, src)
            if name is not None:
                return name
    return None


def _drill_to_type_identifier(node: Node, src: bytes) -> Optional[str]:
    """Recursively resolve a type-expression node down to its leading
    `type_identifier`. Handles `Foo`, `*Foo`, `Foo[T]`, `*Foo[T, U]`,
    and qualified `pkg.Foo` (returns the trailing local name).
    Used by both receiver extraction and embedded-base detection.
    """
    if node.type == "type_identifier":
        return _text(node, src)
    if node.type == "pointer_type":
        for c in node.named_children:
            r = _drill_to_type_identifier(c, src)
            if r is not None:
                return r
        return None
    if node.type == "generic_type":
        for c in node.named_children:
            if c.type == "type_identifier":
                return _text(c, src)
        return None
    if node.type == "qualified_type":
        ids = [c for c in node.named_children if c.type == "type_identifier"]
        if ids:
            return _text(ids[-1], src)
    return None


# --- const / var ---------------------------------------------------------


def _const_var_to_decls(
    node: Node, src: bytes, *, kind_name: str
) -> list[Declaration]:
    """`const X = 1` / `var X int` / parenthesised blocks.

    Each `const_spec` / `var_spec` becomes one KIND_FIELD entry. The
    block-form preserves doc comments scoped to the inner spec via the
    spec's own preceding siblings.

    `iota`-driven constant blocks (the Go enum idiom) just produce one
    field per spec — we deliberately don't try to re-construct the
    inherited type/iota arithmetic, since the rendered signature is
    enough for an agent to recognise the pattern.
    """
    out: list[Declaration] = []
    seen_first = False
    for c in node.named_children:
        if c.type in ("const_spec", "var_spec"):
            d = _spec_to_field(
                c,
                src,
                kind_name=kind_name,
                outer_doc_anchor=node if not seen_first else None,
            )
            if d is not None:
                out.append(d)
                seen_first = True
        elif c.type == "var_spec_list":
            for spec in c.named_children:
                if spec.type == "var_spec":
                    d = _spec_to_field(
                        spec,
                        src,
                        kind_name=kind_name,
                        outer_doc_anchor=node if not seen_first else None,
                    )
                    if d is not None:
                        out.append(d)
                        seen_first = True
    return out


def _spec_to_field(
    node: Node, src: bytes, *, kind_name: str, outer_doc_anchor: Optional[Node]
) -> Optional[Declaration]:
    """One `const_spec` or `var_spec` → KIND_FIELD.

    Multi-name specs (`var X, Y int = 0, 0`) emit a single Declaration
    keyed on the first name (consistent with how Java and Kotlin handle
    multi-variable fields).
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # multi-name: first identifier
        for c in node.named_children:
            if c.type == "identifier":
                name_node = c
                break
    if name_node is None:
        return None
    name = _text(name_node, src)

    # Doc precedence: prefer comments above THIS spec; fall back to the
    # outer declaration's doc if THIS is the first spec in a block form.
    docs = _go_docs(node, src)
    if not docs and outer_doc_anchor is not None:
        docs = _go_docs(outer_doc_anchor, src)
    doc_start = _leading_doc_start_byte(node, src)
    if doc_start is None and outer_doc_anchor is not None:
        doc_start = _leading_doc_start_byte(outer_doc_anchor, src)
    doc_start = doc_start if doc_start is not None else node.start_byte

    sig_text = _collapse_ws(_text(node, src))
    # Block-form specs miss the leading `const`/`var` keyword in their
    # own slice; prefix it for clarity.
    if not sig_text.startswith(kind_name + " ") and not sig_text.startswith(kind_name):
        sig_text = f"{kind_name} {sig_text}"
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig_text.rstrip(),
        visibility=_go_visibility(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=doc_start,
    )


# --- Visibility / docs --------------------------------------------------


def _go_visibility(name: str) -> str:
    """Go convention: capital first letter → exported (public), else
    package-private (treated as `private` in our IR for consistency
    with `--no-private` behaviour on other languages).
    """
    if not name:
        return "public"
    first = name[0]
    return "public" if first.isupper() else "private"


def _go_docs(node: Node, src: bytes) -> list[str]:
    """Walk preceding `comment` siblings until a non-comment node, blank
    line gap, or another declaration. Tree-sitter-go represents both
    `// ...` line comments and `/* ... */` block comments under the
    `comment` type.

    Detecting blank-line breaks: between the comment's end_point and the
    next sibling (the doc target's start), if there's >1 line gap, the
    comment is no longer attached. We honour that by checking the
    line-delta between consecutive comments.
    """
    docs: list[str] = []
    sib = node.prev_sibling
    last_start_line: Optional[int] = node.start_point[0]
    while sib is not None and sib.type == "comment":
        # Reject if there's a blank line BETWEEN this comment and the
        # next one above it / the declaration: comment.end_line + 1 must
        # equal the next-thing-down's start_line.
        if last_start_line is not None and sib.end_point[0] + 1 < last_start_line:
            break
        docs.append(_text(sib, src))
        last_start_line = sib.start_point[0]
        sib = sib.prev_sibling
    docs.reverse()
    return docs


def _leading_doc_start_byte(node: Node, src: bytes) -> Optional[int]:
    first: Optional[Node] = None
    sib = node.prev_sibling
    last_start_line: Optional[int] = node.start_point[0]
    while sib is not None and sib.type == "comment":
        if last_start_line is not None and sib.end_point[0] + 1 < last_start_line:
            break
        first = sib
        last_start_line = sib.start_point[0]
        sib = sib.prev_sibling
    return first.start_byte if first is not None else None


def _resolved_doc_start(node: Node, src: bytes) -> int:
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Signature slicing helpers ------------------------------------------


def _slice_until(
    start_byte: int,
    type_node: Node,
    src: bytes,
    body_node_type: str,
    *,
    default_to_node: Node,
) -> str:
    """Slice from `start_byte` up to the first child of `type_node`
    whose type matches `body_node_type`. Used to cut struct signature
    before `field_declaration_list` etc.
    """
    cut: Optional[int] = None
    for c in type_node.children:
        if c.type == body_node_type:
            cut = c.start_byte
            break
    end = cut if cut is not None else default_to_node.end_byte
    text = src[start_byte:end].decode("utf8", errors="replace")
    return _collapse_ws(text).rstrip(" {").rstrip()


def _slice_until_brace(
    start_byte: int, type_node: Node, src: bytes, *, default_to_node: Node
) -> str:
    """Slice from `start_byte` up to the first `{` of `type_node` —
    used for interface signatures (which have braces directly, not a
    named body node).
    """
    cut: Optional[int] = None
    for c in type_node.children:
        if c.type == "{":
            cut = c.start_byte
            break
    end = cut if cut is not None else default_to_node.end_byte
    text = src[start_byte:end].decode("utf8", errors="replace")
    return _collapse_ws(text).rstrip(" {").rstrip()


# --- Misc ---------------------------------------------------------------


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")
