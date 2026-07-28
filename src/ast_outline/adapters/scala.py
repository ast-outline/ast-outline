"""Scala adapter — parses .scala / .sc files via tree-sitter-scala.

Supports both Scala 2.x and Scala 3.x syntax (the grammar is unified):
brace-delimited bodies and Scala 3 indentation-based bodies, traits with
default methods, `case class` / `case object`, `sealed` hierarchies,
Scala 3 `enum`, `given` / `using`, `extension` methods, opaque type
aliases, and curried function parameter lists.

Mapping (how Scala concepts land in the IR):

- `package_clause`                 → KIND_NAMESPACE (absorbs trailing
                                     top-level declarations; nested
                                     `package foo { ... }` blocks also
                                     produce a namespace with its own
                                     children)
- `class_definition`               → KIND_CLASS; with `case` keyword →
                                     KIND_RECORD (case class is Scala's
                                     record-equivalent data class)
- `trait_definition`               → KIND_INTERFACE (traits are the
                                     closest semantic match; they may
                                     carry default implementations,
                                     like Java interfaces)
- `object_definition`              → KIND_CLASS (singleton; signature
                                     begins with `object` or `case
                                     object`)
- `enum_definition`                → KIND_ENUM (Scala 3)
- `given_definition`               → KIND_CLASS (named or anonymous
                                     implicit-value declaration; body-
                                     bearing forms surface as a class-
                                     shaped group of methods)
- `function_definition` /
  `function_declaration`           → KIND_METHOD inside a type / trait,
                                     KIND_FUNCTION at module/package
                                     level or directly inside an
                                     `extension` block
- `val_definition` / `var_definition` /
  `val_declaration` / `var_declaration` → KIND_FIELD
- `class_parameter` with val/var (or any param inside a case class /
  case object primary ctor)        → KIND_FIELD (implicit property)
- `type_definition`                → KIND_DELEGATE (type alias / opaque
                                     type)
- `enum_case_definitions` wrapping
  `simple_enum_case` / `full_enum_case` → KIND_ENUM_MEMBER
- `extension_definition`           — the `extension (receiver)` block
                                     itself is transparent: its inner
                                     `function_definition`s are
                                     flattened into the parent scope
                                     with the receiver type prefixed
                                     into the rendered signature, so
                                     each extension method surfaces as
                                     its own declaration
- `package_object`                 → KIND_CLASS with signature starting
                                     `package object` (Scala's hybrid
                                     of namespace + object)

Modifiers: access (`private` / `protected`; Scala's default is `public`),
`final`, `sealed`, `abstract`, `implicit`, `lazy`, `inline`, `opaque`,
`override`, and the `case` marker on classes/objects.

Annotations (`@deprecated`, `@Inline`, `@SerialVersionUID`, …) live as
direct children of the declaration node (NOT inside `modifiers`, unlike
Java/Kotlin). They are harvested into `Declaration.attrs` and stripped
from the rendered signature.

Scaladoc: `/** ... */` is a `block_comment` node whose text begins with
`/**`; plain `/* ... */` and `//` comments stop the doc walk. Line
comments are `comment` nodes — not `line_comment` — so the adapter's
doc walker only advances through `block_comment` siblings.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_scala as tss
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors, read_source
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DELEGATE,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_RECORD,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tss.language())
_PARSER = Parser(_LANGUAGE)


class ScalaAdapter:
    language_name = "scala"
    display_name = "Scala"
    extensions = {".scala", ".sc"}
    definition_keywords = frozenset({
        "class", "trait", "object", "def", "type",
    })
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
        # Lets ``import foo.{\n  Bar,\n  Baz,\n}`` multi-line braced
        # form classify inner symbols as ``[import]`` instead of ``[ref]``.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        import_regions.sort()
        # Scala `import` can appear at any depth. Imports inside a
        # function / method body are scoped to that callable —
        # they're effectively local-scope and not part of the file's
        # public dependency surface. Object / class / trait bodies
        # are not counted: their imports execute at definition time
        # (load-time, not runtime), so they're closer to "scoped but
        # eager" than to "conditional / runtime".
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
    """Scala imports — `import foo.bar.Baz`, `import foo.{A, B}`,
    `import foo.{A => Renamed}`, `import foo.*` (Scala 3) or
    `import foo._` (Scala 2). Source-true text reads as either dialect.

    Limitation: imports inside a **braced** `package foo { ... }` body
    are not collected. That form is rare in modern Scala (braceless
    `package foo` followed by file-level declarations is the idiom),
    and the file-level imports are what an agent typically needs for a
    "where does this file pull from" view. If a real-world need
    surfaces, descend into `package_object` and braced `package_clause`
    bodies.

    If ``regions`` is supplied, also appends each declaration's byte
    range — covers single-line ``import foo.{A, B}`` AND multi-line
    braced ``import foo.{\\n  A,\\n  B,\\n}`` for grep's classifier."""
    for child in root.named_children:
        if child.type == "import_declaration":
            text = _collapse_ws(_text(child, src)).strip()
            if text:
                out.append(text)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))


# AST node types that put a Scala `import` into runtime / function-
# local scope. Only `function_definition` (concrete `def m = ...`)
# qualifies — `function_declaration` is the abstract `def m: T`
# variant which has no body and therefore can't host an `import`.
# Object / class / trait bodies are deliberately excluded: their
# imports execute eagerly at load time, just within a definition's
# namespace — closer to "scoped but eager" than to "conditional /
# runtime".
_SCALA_RUNTIME_OR_SCOPED = frozenset({
    "function_definition",
})


def _count_conditional_imports(root: Node) -> int:
    """Count `import` declarations that live inside a function or
    method body — locally-scoped, runtime-only.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        if node.type == "import_declaration":
            if in_scope:
                count += 1
            continue
        new_in_scope = in_scope or node.type in _SCALA_RUNTIME_OR_SCOPED
        for c in node.children:
            stack.append((c, new_in_scope))
    return count


# --- Top-level walk -------------------------------------------------------


_DECL_NODE_TYPES = {
    "class_definition",
    "trait_definition",
    "object_definition",
    "enum_definition",
    "given_definition",
    "function_definition",
    "function_declaration",
    "val_definition",
    "val_declaration",
    "var_definition",
    "var_declaration",
    "type_definition",
    "extension_definition",
    "package_object",
}


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Handle file-level structure.

    Scala has three flavours of packaging to worry about:
    1. Braceless `package foo` at the top — everything that follows
       belongs to it until EOF or the next braceless package.
    2. Multiple consecutive braceless `package` headers — they nest:
       `package a; package b; class X` puts X in `a.b`.
    3. Braced `package foo { ... }` — the grammar models this with a
       `template_body` field; declarations inside that body belong to
       `foo` but declarations after the closing `}` do NOT.

    We collapse (1) + (2) into a single outer namespace with a dotted
    name. Braced packages are modelled as regular namespace
    declarations; their outside-the-braces siblings stay at their
    lexical scope.
    """
    # Split the node's children into: leading braceless package
    # headers (collapsed into one dotted namespace), a stream of
    # declarations, and stand-alone braced packages (which are
    # structurally like classes — they have their own template_body).
    children = list(node.named_children)
    braceless_pkgs: list[Node] = []
    i = 0
    while i < len(children):
        c = children[i]
        if c.type == "package_clause" and c.child_by_field_name("body") is None:
            braceless_pkgs.append(c)
            i += 1
        else:
            break

    if braceless_pkgs:
        package_ns = _dotted_package_namespace(braceless_pkgs, src)
        out.append(package_ns)
        sink: list[Declaration] = package_ns.children
    else:
        sink = out

    # Walk remaining children; known declarations become Declarations,
    # braced packages become their own nested namespaces.
    while i < len(children):
        c = children[i]
        if c.type == "package_clause" and c.child_by_field_name("body") is not None:
            sink.append(_braced_package_to_decl(c, src))
        elif c.type in _DECL_NODE_TYPES:
            decls = _decl_from_node(c, src, parent_kind=None)
            for d in decls:
                sink.append(d)
        # imports / comments / stray tokens — skip
        i += 1

    # Propagate end positions to the top-level namespace so line suffix
    # renders `L<start>-<end>` covering the whole package, not just the
    # `package foo` header line.
    if braceless_pkgs:
        last = sink[-1] if sink else None
        if last is not None:
            ns = out[0]
            ns.end_line = max(ns.end_line, last.end_line)
            ns.end_byte = max(ns.end_byte, last.end_byte)


def _dotted_package_namespace(
    pkg_clauses: list[Node], src: bytes
) -> Declaration:
    """Collapse multiple leading `package foo` / `package bar` headers
    into one namespace with a dotted name `foo.bar` — mirrors Scala's
    implicit package nesting.
    """
    parts: list[str] = []
    for c in pkg_clauses:
        pid = c.child_by_field_name("name")
        if pid is None:
            # Fall back: look for a package_identifier / identifier child
            for child in c.named_children:
                if child.type in ("package_identifier", "identifier"):
                    pid = child
                    break
        if pid is not None:
            parts.append(_collapse_ws(_text(pid, src)))
    name = ".".join(p for p in parts if p)
    first = pkg_clauses[0]
    last = pkg_clauses[-1]
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"package {name}" if name else "package",
        start_line=first.start_point[0] + 1,
        end_line=last.end_point[0] + 1,
        start_byte=first.start_byte,
        end_byte=last.end_byte,
    )


def _braced_package_to_decl(node: Node, src: bytes) -> Declaration:
    """`package foo { ... }` — a namespace whose children come from the
    braced `template_body`. Unlike a braceless package, this one does
    not absorb its post-brace siblings.
    """
    pid = node.child_by_field_name("name")
    name = _collapse_ws(_text(pid, src)) if pid is not None else ""
    body = node.child_by_field_name("body")
    children: list[Declaration] = []
    if body is not None:
        for c in body.named_children:
            if c.type in _DECL_NODE_TYPES:
                children.extend(_decl_from_node(c, src, parent_kind=None))
            elif c.type == "package_clause" and c.child_by_field_name("body") is not None:
                children.append(_braced_package_to_decl(c, src))
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"package {name}" if name else "package",
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=children,
    )


# --- Dispatch -------------------------------------------------------------


def _decl_from_node(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> list[Declaration]:
    """Dispatch for one Scala declaration node.

    Returns a LIST because `extension_definition` expands into multiple
    child declarations (one per method in the extension group). Every
    other node type returns a single-element list.
    """
    t = node.type
    if t == "class_definition":
        return [_type_to_decl(node, src, parent_kind=parent_kind)]
    if t == "trait_definition":
        return [_type_to_decl(node, src, parent_kind=parent_kind)]
    if t == "object_definition":
        return [_type_to_decl(node, src, parent_kind=parent_kind)]
    if t == "enum_definition":
        return [_enum_to_decl(node, src, parent_kind=parent_kind)]
    if t == "given_definition":
        d = _given_to_decl(node, src, parent_kind=parent_kind)
        return [d] if d is not None else []
    if t in ("function_definition", "function_declaration"):
        return [_function_to_decl(node, src, parent_kind=parent_kind)]
    if t in ("val_definition", "var_definition", "val_declaration", "var_declaration"):
        d = _property_to_decl(node, src, parent_kind=parent_kind)
        return [d] if d is not None else []
    if t == "type_definition":
        d = _type_alias_to_decl(node, src)
        return [d] if d is not None else []
    if t == "extension_definition":
        return _extension_to_decls(node, src, parent_kind=parent_kind)
    if t == "package_object":
        return [_package_object_to_decl(node, src)]
    return []


# --- Type-bearing declarations (class / trait / object / enum) ----------


def _name_line(node: Node) -> int:
    """1-based line where the declaration's signature begins.

    Scala places annotations as **direct children** of the declaration
    (`annotation` nodes, unlike Java/Kotlin which nest them in
    `modifiers`), and visibility / `final` modifiers in a `modifiers`
    child. Either can precede the declaration on earlier lines, so
    `node.start_point` lands on an annotation / modifier line. The first
    child that is neither is the real head (the `class` / `def` keyword or
    the name), and the name token sits on that line. grep's def-classifier
    compares a match against it, so an annotated declaration is tagged
    [def] on its signature line, not on an annotation.
    """
    for c in node.children:
        if c.type not in ("annotation", "modifiers"):
            return c.start_point[0] + 1
    return node.start_point[0] + 1


def _type_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Build a Declaration for `class_definition`, `trait_definition`,
    or `object_definition`. Case-class detection is a direct-child
    `case` keyword before `class`/`object`.
    """
    kind = _type_decl_kind(node)
    # Carry the source-true keyword separately from the canonical kind:
    # Scala `trait` maps to KIND_INTERFACE (so cross-language search
    # treats mixins uniformly with Java/Rust traits) but digest should
    # print `trait` rather than `interface` when the source actually
    # says `trait`. Same for `object` (singleton) which lives under
    # KIND_CLASS but wants its own keyword in the digest.
    native = _native_keyword_for_type(node)
    name = _field_text(node, "name", src) or "?"
    bases = _extends_bases(node, src)
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children: list[Declaration] = []
    # Primary-ctor parameters: case classes expose every param as a
    # public val; regular classes expose only ones with `val`/`var`.
    ctor_fields = _primary_ctor_fields(node, src, is_case=_has_case_keyword(node))
    children.extend(ctor_fields)

    body = node.child_by_field_name("body")
    if body is not None:
        for c in body.named_children:
            if c.type in _DECL_NODE_TYPES:
                children.extend(_decl_from_node(c, src, parent_kind=kind))
            # stray comments / end markers — skip

    return Declaration(
        kind=kind,
        native_kind=native,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


def _native_keyword_for_type(node: Node) -> str:
    """Source-true keyword for a `trait` / `class` / `object` /
    `case class` / `case object` definition.

    Mirrors Kotlin's `data class` treatment: the canonical kind
    (KIND_RECORD for case classes) keeps cross-language search
    consistent, while the digest restores the actual source keyword
    so a Scala reader sees `case class`, not `record`.
    """
    if node.type == "trait_definition":
        return "trait"
    if node.type == "object_definition":
        return "case object" if _has_case_keyword(node) else "object"
    if node.type == "class_definition" and _has_case_keyword(node):
        return "case class"
    return ""


def _type_decl_kind(node: Node) -> str:
    """Map a type-bearing node to a canonical kind.

    - `trait_definition` → KIND_INTERFACE (no other option; sealed /
      final are modifiers but the kind doesn't change)
    - `class_definition` with `case` keyword → KIND_RECORD
    - `class_definition` → KIND_CLASS
    - `object_definition` → KIND_CLASS (the `object` nature is carried
      by the rendered signature, not by the kind)
    """
    if node.type == "trait_definition":
        return KIND_INTERFACE
    if node.type == "class_definition" and _has_case_keyword(node):
        return KIND_RECORD
    return KIND_CLASS


def _has_case_keyword(node: Node) -> bool:
    """True if the declaration is prefixed with a top-level `case`
    keyword — marks `case class` and `case object`. `case` appears as a
    direct token child, distinct from the `modifiers` container.
    """
    for c in node.children:
        if c.type == "case":
            return True
        if c.type in ("class", "trait", "object"):
            break
    return False


def _enum_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Scala 3 enum — a sealed-hierarchy hybrid. Its `enum_body` holds
    `enum_case_definitions` wrappers; each wrapper contributes one or
    more `simple_enum_case` / `full_enum_case` entries (Scala allows
    `case A, B, C` as a single wrapper).
    """
    name = _field_text(node, "name", src) or "?"
    bases = _extends_bases(node, src)
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children = _primary_ctor_fields(node, src, is_case=False)
    body = node.child_by_field_name("body")
    if body is not None:
        for c in body.named_children:
            if c.type == "enum_case_definitions":
                children.extend(_enum_case_entries(c, src))
            elif c.type in _DECL_NODE_TYPES:
                children.extend(_decl_from_node(c, src, parent_kind=KIND_ENUM))

    return Declaration(
        kind=KIND_ENUM,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


def _enum_case_entries(node: Node, src: bytes) -> list[Declaration]:
    """One `enum_case_definitions` may contain multiple cases (`case A, B`)
    or one rich case (`case Rect(w: Int, h: Int) extends Shape`). Emit
    a KIND_ENUM_MEMBER for each.
    """
    out: list[Declaration] = []
    for c in node.named_children:
        if c.type in ("simple_enum_case", "full_enum_case"):
            name_node = c.child_by_field_name("name")
            # Fallback: first identifier / type_identifier child
            if name_node is None:
                for cc in c.named_children:
                    if cc.type in ("identifier", "type_identifier"):
                        name_node = cc
                        break
            if name_node is None:
                continue
            sig = _collapse_ws(_strip_leading_annotations(_text(c, src))).rstrip(",")
            out.append(
                Declaration(
                    kind=KIND_ENUM_MEMBER,
                    name=_text(name_node, src),
                    signature=sig,
                    visibility="public",
                    start_line=c.start_point[0] + 1,
                    end_line=c.end_point[0] + 1,
                    start_byte=c.start_byte,
                    end_byte=c.end_byte,
                )
            )
    return out


def _given_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Scala 3 `given` — an implicit value. Named givens have a `name`
    field; anonymous givens fall back to a type-derived synthetic name
    so they still participate in `find_symbols`. Bodies live under
    `body` (either `with_template_body` or `template_body`).
    """
    name_node = node.child_by_field_name("name")
    name: Optional[str] = _text(name_node, src) if name_node is not None else None
    if not name:
        # Anonymous: use first `type_identifier` / `generic_type` as a
        # label so the declaration is searchable.
        for c in node.named_children:
            if c.type in ("type_identifier", "generic_type"):
                name = "given " + _collapse_ws(_text(c, src))
                break
    if not name:
        name = "given"

    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)
    bases: list[str] = []
    # given X: SomeType[Y] with { ... } — the declared type looks a lot
    # like a parent, so surface it as a base in the type header.
    for c in node.named_children:
        if c.type in ("type_identifier", "generic_type"):
            bases.append(_collapse_ws(_text(c, src)))
            break

    children: list[Declaration] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for c in body.named_children:
            if c.type in _DECL_NODE_TYPES:
                children.extend(_decl_from_node(c, src, parent_kind=KIND_CLASS))

    return Declaration(
        kind=KIND_CLASS,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


def _package_object_to_decl(node: Node, src: bytes) -> Declaration:
    """`package object util { ... }` — a Scala 2 construct. Render as
    KIND_CLASS with a `package object` signature; children come from
    its `template_body`.
    """
    name = _field_text(node, "name", src) or "?"
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    signature = _type_signature(node, src)

    children: list[Declaration] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for c in body.named_children:
            if c.type in _DECL_NODE_TYPES:
                children.extend(_decl_from_node(c, src, parent_kind=KIND_CLASS))

    return Declaration(
        kind=KIND_CLASS,
        name=name,
        signature=signature,
        attrs=attrs,
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


# --- Primary-constructor fields ------------------------------------------


def _primary_ctor_fields(
    node: Node, src: bytes, *, is_case: bool
) -> list[Declaration]:
    """Extract implicit properties from a primary constructor.

    - `class Foo(val x: Int, var y: String)` → x and y are fields.
    - `case class Foo(x: Int, y: String)` → both are public vals by
      Scala rules, so we promote them to fields.
    - `class Foo(x: Int)` (no val/var, not a case class) → x is just a
      ctor arg, not a field; skipped.

    Multiple parameter lists (curried constructors) are all visited;
    the `is_case` promotion applies to every list equally.
    """
    out: list[Declaration] = []
    for child in node.children:
        if child.type != "class_parameters":
            continue
        for param in child.named_children:
            if param.type != "class_parameter":
                continue
            d = _class_parameter_to_field(param, src, is_case=is_case)
            if d is not None:
                out.append(d)
    return out


def _class_parameter_to_field(
    node: Node, src: bytes, *, is_case: bool
) -> Optional[Declaration]:
    """One `class_parameter` → KIND_FIELD, or None when it's a plain
    ctor argument that doesn't produce a property.
    """
    has_val_var = False
    for c in node.children:
        if c.type in ("val", "var"):
            has_val_var = True
            break
    if not (has_val_var or is_case):
        return None

    name_node = node.child_by_field_name("name")
    if name_node is None:
        for c in node.named_children:
            if c.type == "identifier":
                name_node = c
                break
    if name_node is None:
        return None

    attrs = _annotations(node, src)
    sig = _collapse_ws(_strip_leading_annotations(_text(node, src)))
    return Declaration(
        kind=KIND_FIELD,
        name=_text(name_node, src),
        signature=sig,
        attrs=attrs,
        visibility=_visibility(node),
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Functions / properties / type aliases / extensions ------------------


def _function_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Both `function_definition` (has `= expr` body) and
    `function_declaration` (abstract, no body) share this builder. At
    top level we emit KIND_FUNCTION; inside a type we emit KIND_METHOD.
    """
    kind = KIND_METHOD if parent_kind is not None else KIND_FUNCTION
    name = _field_text(node, "name", src) or "?"
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    signature = _callable_signature(node, src)
    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _property_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`val` / `var` definitions and declarations. Pattern-binding
    forms (`val (a, b) = (1, 2)`) are surfaced under the first
    extracted identifier name — good enough for navigation without
    requiring destructuring support in the outline.
    """
    name = _property_name(node, src)
    if not name:
        return None
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    signature = _property_signature(node, src)
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=signature,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _property_name(node: Node, src: bytes) -> Optional[str]:
    """Name extraction. The FIRST named child of a val/var definition
    is its binding side — a bare `identifier`, a `tuple_pattern`
    (`val (a, b) = ...`), a `case_class_pattern`
    (`val List(h, t) = ...`), an `infix_pattern` (`val h :: t = ...`).
    Everything after it belongs to the type / RHS, so only that first
    child may name the declaration: scanning further used to leak the
    RHS identifier as the "name" when the pattern shape wasn't
    recognised (`val List(h, t) = myList` → field `myList`).
    """
    # Leading `modifiers` (`protected`, `lazy`) / `annotation` nodes
    # precede the binding — skip them; the binding is the first child
    # after that prefix.
    head: Optional[Node] = None
    for c in node.named_children:
        if c.type in ("modifiers", "annotation", "comment"):
            continue
        head = c
        break
    if head is None:
        return None
    if head.type == "identifier":
        return _text(head, src)
    if head.type.endswith("pattern"):
        return _first_bound_identifier(head, src)
    return None


def _first_bound_identifier(pattern: Node, src: bytes) -> Optional[str]:
    """First `identifier` bound anywhere inside a binding pattern —
    `h` in `List(h, t)`, `a` in `((a, b), c)`. Type names inside the
    pattern are `type_identifier` nodes and don't match."""
    for c in pattern.named_children:
        if c.type == "identifier":
            return _text(c, src)
        found = _first_bound_identifier(c, src)
        if found is not None:
            return found
    return None


def _type_alias_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`type Handler = String => Unit` or `opaque type UserId = String`.
    The grammar exposes the alias name as `type_identifier`; the
    `opaque` modifier survives in the signature via the verbatim slice.
    """
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "type_identifier":
            name_node = c
            break
    if name_node is None:
        return None
    attrs = _annotations(node, src)
    docs = _scaladocs(node, src)
    visibility = _visibility(node)
    sig = _collapse_ws(_strip_leading_annotations(_text(node, src))).rstrip(";")
    return Declaration(
        kind=KIND_DELEGATE,
        name=_text(name_node, src),
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _extension_to_decls(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> list[Declaration]:
    """`extension (s: String) def foo: Int = ...` — Scala 3 extension
    block. One block can host several `def`s; we flatten them into the
    surrounding scope, prefixing the receiver text to each rendered
    signature so callers see something like
    `extension (s: String) def foo: Int`.
    """
    receiver_text = ""
    for c in node.named_children:
        if c.type == "parameters":
            receiver_text = _collapse_ws(_text(c, src))
            break
    prefix = f"extension {receiver_text} " if receiver_text else "extension "

    out: list[Declaration] = []
    for c in node.named_children:
        if c.type in ("function_definition", "function_declaration"):
            fn = _function_to_decl(c, src, parent_kind=parent_kind)
            fn.signature = prefix + fn.signature
            out.append(fn)
    return out


# --- Signature extraction -------------------------------------------------


def _type_signature(node: Node, src: bytes) -> str:
    """Slice from the declaration start up to the start of the body
    (`template_body` / `enum_body` / `with_template_body`). Annotations
    are stripped — they live in `attrs` separately.
    """
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {:=").rstrip()


def _callable_signature(node: Node, src: bytes) -> str:
    """For a `function_definition` the body lives under the `body`
    field (`= expr` or `block`). Abstract declarations have no body,
    so we take the whole node. Either way, annotations are stripped
    and any trailing `= …` residue is cut.
    """
    body = node.child_by_field_name("body")
    if body is not None:
        end = body.start_byte
    else:
        end = node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    # Cut a trailing `= ` that precedes the body but sits OUTSIDE the
    # body node (happens with `def f(): Int = expr` — the `=` is a
    # sibling of the body, not a body child).
    text = text.rstrip()
    if text.endswith("="):
        text = text[:-1].rstrip()
    return _collapse_ws(text).rstrip(" {;=:").rstrip()


def _property_signature(node: Node, src: bytes) -> str:
    """Slice through the whole val/var declaration; for abstract
    declarations (`val x: Int` with no `=`) the slice naturally ends
    at the type annotation, so we don't need special handling.
    """
    text = _text(node, src)
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


# --- Bases (extends / with) ---------------------------------------------


def _extends_bases(node: Node, src: bytes) -> list[str]:
    """Collect `extends X with Y with Z` as `[X, Y, Z]`.

    The grammar structures `extends_clause` as an `extends` keyword
    followed by alternating type nodes and `with`/`arguments` nodes.
    We keep `type_identifier` and `generic_type` values; `arguments`
    (superclass constructor args) are skipped because they aren't types.
    """
    out: list[str] = []
    ec: Optional[Node] = None
    for c in node.children:
        if c.type == "extends_clause":
            ec = c
            break
    if ec is None:
        return out
    for c in ec.named_children:
        if c.type in ("type_identifier", "generic_type", "compound_type"):
            out.append(_collapse_ws(_text(c, src)))
        # `arguments` (ctor args) and `with` keyword — skip
    return out


# --- Modifiers / annotations / docs --------------------------------------


def _modifiers_node(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


_VISIBILITY_TOKENS = {"public", "protected", "private"}


def _visibility(node: Node) -> str:
    """Scala defaults to `public` at every scope. An `access_modifier`
    inside `modifiers` wraps `private` / `protected` as its first
    token; `private[scope]` still reports as `private`.
    """
    mods = _modifiers_node(node)
    if mods is not None:
        for c in mods.named_children:
            if c.type == "access_modifier":
                for cc in c.children:
                    if cc.type in _VISIBILITY_TOKENS:
                        return cc.type
    return "public"


def _annotations(node: Node, src: bytes) -> list[str]:
    """Scala annotations are DIRECT CHILDREN of the declaration node —
    NOT inside `modifiers` (unlike Java / Kotlin). Walk the declaration
    children for `annotation` entries.
    """
    out: list[str] = []
    for c in node.children:
        if c.type == "annotation":
            out.append(_collapse_ws(_text(c, src)))
    return out


def _scaladocs(node: Node, src: bytes) -> list[str]:
    """Contiguous preceding `block_comment` nodes starting with `/**`
    form the Scaladoc for the declaration. Plain `/* */` and `//`
    comments stop the walk. Line comments have type `comment`, not
    `block_comment`, so they don't interfere.
    """
    docs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "block_comment":
        text = _text(sib, src)
        if not text.startswith("/**"):
            break
        docs.append(text)
        sib = sib.prev_sibling
    docs.reverse()
    return docs


def _leading_doc_start_byte(node: Node, src: bytes) -> Optional[int]:
    first: Optional[Node] = None
    sib = node.prev_sibling
    while sib is not None and sib.type == "block_comment":
        if _text(sib, src).startswith("/**"):
            first = sib
            sib = sib.prev_sibling
        else:
            break
    return first.start_byte if first is not None else None


def _resolved_doc_start(node: Node, src: bytes) -> int:
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Annotation stripping -----------------------------------------------


def _strip_leading_annotations(text: str) -> str:
    """Strip leading `@Foo` / `@Foo(args)` from rendered signatures.
    Scala annotations can be adjacent without separators
    (`@foo @bar class X`) or stacked with newlines; `lstrip` and the
    while-loop handle both. String literals inside args have their
    parens masked out so `@SerialVersionUID(1L)` or `@deprecated("(x)")`
    round-trip cleanly.
    """
    s = text.lstrip()
    while s.startswith("@"):
        i = 1
        while i < len(s) and (s[i].isalnum() or s[i] in "._"):
            i += 1
        if i < len(s) and s[i] == "(":
            depth = 1
            i += 1
            while i < len(s) and depth > 0:
                ch = s[i]
                if ch in ('"', "'"):
                    i = _skip_string_literal(s, i, ch)
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
        s = s[i:].lstrip()
    return s


def _skip_string_literal(s: str, i: int, quote: str) -> int:
    i += 1
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            i += 2
            continue
        if s[i] == quote:
            return i + 1
        i += 1
    return i


# --- Helpers ---------------------------------------------------------------


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
