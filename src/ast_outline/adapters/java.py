"""Java adapter — parses .java files via tree-sitter-java into Declaration IR.

Design notes (how Java concepts map to the IR):

- `package_declaration`                 → KIND_NAMESPACE (absorbs trailing
                                          type siblings, like a C# file-scoped
                                          namespace)
- `class_declaration`                   → KIND_CLASS
- `interface_declaration`               → KIND_INTERFACE
- `annotation_type_declaration`         → KIND_INTERFACE (`@interface Foo`)
- `enum_declaration`                    → KIND_ENUM
- `record_declaration`                  → KIND_RECORD (Java 14+)
- `method_declaration`                  → KIND_METHOD
- `constructor_declaration`             → KIND_CTOR
- `compact_constructor_declaration`     → KIND_CTOR  (record compact ctor)
- `annotation_type_element_declaration` → KIND_METHOD  (members of @interface)
- `field_declaration`                   → KIND_FIELD (first declarator only;
                                          `int a, b;` → a single entry for `a`)
- `enum_constant`                       → KIND_ENUM_MEMBER

Modifiers & annotations live inside a `modifiers` child node. Visibility
tokens (`public` / `protected` / `private`) appear there as anonymous
children; annotations appear there as `marker_annotation` / `annotation`
named children.

Visibility defaults:
- Top-level types without a modifier  → "internal"  (Java package-private)
- Class/record/enum members           → "internal"  (Java package-private)
- Interface / @interface members      → "public"    (Java spec default)
- Enum constants                      → "public"
- Enum constructors without modifier  → "private"   (Java spec)

Docs: Javadoc `/** ... */` is a `block_comment` node — only block comments
starting with `/**` are captured. Line comments (`//`) and plain block
comments (`/* ... */`) are ignored.

Supported features: generics, `throws` clauses, annotations (including
multi-line), records + compact constructors, sealed/non-sealed/permits,
nested types, multi-variable field declarations (first name wins).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_java as tsj
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_RECORD,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tsj.language())
_PARSER = Parser(_LANGUAGE)


_TYPE_NODE_KIND = {
    "class_declaration": KIND_CLASS,
    "interface_declaration": KIND_INTERFACE,
    "annotation_type_declaration": KIND_INTERFACE,
    "enum_declaration": KIND_ENUM,
    "record_declaration": KIND_RECORD,
}

_MEMBER_NODE_KIND = {
    "method_declaration": KIND_METHOD,
    "constructor_declaration": KIND_CTOR,
    "compact_constructor_declaration": KIND_CTOR,
    "annotation_type_element_declaration": KIND_METHOD,
    "field_declaration": KIND_FIELD,
    "enum_constant": KIND_ENUM_MEMBER,
}


class JavaAdapter:
    language_name = "java"
    extensions = {".java"}
    definition_keywords = frozenset({"class", "interface", "enum", "record"})

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        declarations: list[Declaration] = []
        _walk_top(tree.root_node, src, declarations)
        imports: list[str] = []
        _collect_imports(tree.root_node, src, imports)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=declarations,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
        )


# --- Imports --------------------------------------------------------------


def _collect_imports(root: Node, src: bytes, out: list[str]) -> None:
    """Java imports are top-level only. Source-true text reads natively
    (`import java.util.List`, `import static foo.Bar.baz`,
    `import com.example.*`) — no synthetic format needed."""
    for child in root.named_children:
        if child.type == "import_declaration":
            text = _collapse_ws(_text(child, src)).rstrip(";").strip()
            if text:
                out.append(text)


# --- Walk -----------------------------------------------------------------


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Java files have at most one `package_declaration` followed by imports
    and then type declarations. The package is modelled as a namespace that
    absorbs all following type siblings (mirrors C# file-scoped namespaces).
    """
    package_ns: Optional[Declaration] = None
    for child in node.named_children:
        kind = child.type
        if kind == "package_declaration":
            package_ns = _package_to_decl(child, src)
            out.append(package_ns)
        elif kind in _TYPE_NODE_KIND:
            type_decl = _type_to_decl(child, src, parent_kind=None)
            if package_ns is not None:
                package_ns.children.append(type_decl)
                package_ns.end_line = type_decl.end_line
                package_ns.end_byte = type_decl.end_byte
            else:
                out.append(type_decl)
        # imports, comments — skip


def _package_to_decl(node: Node, src: bytes) -> Declaration:
    # Name node is either `scoped_identifier` (e.g. com.example.foo) or a
    # bare `identifier` (e.g. `package demo;`).
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type in ("scoped_identifier", "identifier"):
            name_node = c
            break
    name = _text(name_node, src) if name_node is not None else ""
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"package {name}",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _type_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str] = None
) -> Declaration:
    kind = _TYPE_NODE_KIND[node.type]
    name = _field_text(node, "name", src) or "?"
    bases = _base_types(node, src)
    attrs = _annotations(node, src)
    docs = _javadocs(node, src)
    # Nested types inside an interface / @interface are implicitly public
    # (Java spec); outside that context, defaults are package-private.
    visibility = _visibility(
        node, src, is_member=parent_kind is not None, parent_kind=parent_kind
    )
    signature = _type_signature(node, src)

    children: list[Declaration] = []

    # Record components are a special case: the `formal_parameters` field
    # on a record_declaration lists the implicit fields (e.g. `record Point(double x, double y)`).
    # We include them as KIND_FIELD children so the outline surfaces them.
    if node.type == "record_declaration":
        params = node.child_by_field_name("parameters")
        if params is not None:
            for p in params.named_children:
                if p.type == "formal_parameter":
                    field = _record_component_to_decl(p, src)
                    if field is not None:
                        children.append(field)

    # Enum declarations have `enum_body` with enum_constant + enum_body_declarations.
    if node.type == "enum_declaration":
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.named_children:
                if c.type == "enum_constant":
                    m = _member_to_decl(c, src, parent_kind=kind)
                    if m is not None:
                        children.append(m)
                elif c.type == "enum_body_declarations":
                    for cc in c.named_children:
                        children.extend(_child_from_body(cc, src, parent_kind=kind))
    elif node.type == "annotation_type_declaration":
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.named_children:
                children.extend(_child_from_body(c, src, parent_kind=kind))
    else:
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.named_children:
                children.extend(_child_from_body(c, src, parent_kind=kind))

    # `@interface Foo` (annotation type) and `interface Foo` both map
    # to KIND_INTERFACE in the IR. We deliberately do NOT carry a
    # separate native_kind for `@interface`: in Java conversation,
    # docs, and APIs the term is just "interface" / "annotation type",
    # and rendering `@interface` in digest reads as a stray source
    # token rather than a category. The annotation nature is recoverable
    # from the type's `@Retention` / `@Target` attrs and from its
    # element signatures (which carry `default` clauses).

    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


def _child_from_body(
    node: Node, src: bytes, *, parent_kind: str
) -> list[Declaration]:
    k = node.type
    if k in _TYPE_NODE_KIND:
        return [_type_to_decl(node, src, parent_kind=parent_kind)]
    if k in _MEMBER_NODE_KIND:
        m = _member_to_decl(node, src, parent_kind=parent_kind)
        return [m] if m is not None else []
    return []


def _member_to_decl(
    node: Node, src: bytes, *, parent_kind: str
) -> Optional[Declaration]:
    kind = _MEMBER_NODE_KIND[node.type]
    name = _member_name(node, src)
    if not name:
        return None

    attrs = _annotations(node, src)
    docs = _javadocs(node, src)
    visibility = _visibility(
        node, src, is_member=True, parent_kind=parent_kind
    )
    signature = _member_signature_text(node, src)

    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _record_component_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """A single formal_parameter inside `record R(...)` — becomes a FIELD."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    sig = _collapse_ws(_text(node, src))
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        visibility="public",  # record components expose a public accessor
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Signature extraction -------------------------------------------------


def _type_signature(node: Node, src: bytes) -> str:
    """Slice from start of the type declaration up to (but not including)
    the body — covers modifiers, keywords, name, generics, `extends`,
    `implements`, and (for sealed classes) `permits`. Annotations are then
    stripped off the front.
    """
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _member_signature_text(node: Node, src: bytes) -> str:
    """Slice up to the method `block` body, or the whole node for abstract
    methods / fields (which end in `;`). Annotations stripped; trailing
    `{`/`;` trimmed.
    """
    cut = None
    # method_declaration / constructor_declaration — cut before the block body
    body = node.child_by_field_name("body")
    if body is not None:
        cut = body.start_byte
    else:
        # Fall back: look for any body-like child (block / constructor_body)
        for c in node.children:
            if c.type in ("block", "constructor_body"):
                cut = c.start_byte
                break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    text = _collapse_ws(text)
    return text.rstrip(" {;").rstrip()


# --- Annotation stripping -------------------------------------------------


def _strip_leading_annotations(text: str) -> str:
    """Drop one or more leading `@Foo` / `@Foo(...)` annotations from the
    signature text. Handles balanced parens across multiple lines and
    skips over string/char literals that may contain stray parens.

    Does NOT strip `@interface` — that's the declaration keyword for Java
    annotation types, not a usage annotation. The check requires a word
    boundary after `@interface` so that an annotation like `@interfaceAware`
    (unconventional but syntactically valid) is still stripped normally.
    """
    s = text.lstrip()
    while s.startswith("@") and not _starts_with_interface_keyword(s):
        i = 1
        # Annotation identifier — supports dots (fully qualified) and nested
        # like `@foo.bar.Baz`.
        while i < len(s) and (s[i].isalnum() or s[i] in "._"):
            i += 1
        # Optional (...) argument list — balanced parens, skipping string
        # and char literals so `@Foo(value = "(literal)")` parses correctly.
        if i < len(s) and s[i] == "(":
            depth = 1
            i += 1
            while i < len(s) and depth > 0:
                ch = s[i]
                if ch in ("\"", "'"):
                    i = _skip_string_literal(s, i, ch)
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
        s = s[i:].lstrip()
    return s


def _starts_with_interface_keyword(s: str) -> bool:
    """True iff `s` begins with `@interface` followed by a non-identifier
    character (whitespace, EOF, etc.) — distinguishing the declaration
    keyword from an annotation whose name happens to start with `interface`.
    """
    if not s.startswith("@interface"):
        return False
    if len(s) == len("@interface"):
        return True
    nxt = s[len("@interface")]
    return not (nxt.isalnum() or nxt == "_")


def _skip_string_literal(s: str, i: int, quote: str) -> int:
    """Advance past a `"..."` or `'...'` literal (including escape
    sequences), returning the index of the char after the closing quote.
    If the string is unterminated, return `len(s)`.
    """
    # Caller passes `i` pointing at the opening quote.
    i += 1
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            i += 2
            continue
        if s[i] == quote:
            return i + 1
        i += 1
    return i


# --- Bases / heritage ----------------------------------------------------


def _base_types(node: Node, src: bytes) -> list[str]:
    """Collect `extends` superclass + `implements` interfaces.

    - class/record/enum: `superclass` (one) + `interfaces` (super_interfaces)
    - interface:         `extends_interfaces` child (no named field for it)

    `permits` is deliberately not included — it's sealing metadata, not a
    parent type.
    """
    out: list[str] = []

    # Classes / records / enums — `extends One` + `implements A, B`.
    superclass = node.child_by_field_name("superclass")
    if superclass is not None:
        for c in superclass.named_children:
            t = _collapse_ws(_text(c, src)).rstrip(",")
            if t:
                out.append(t)
    super_ifaces = node.child_by_field_name("interfaces")
    if super_ifaces is not None:
        out.extend(_collect_type_list(super_ifaces, src))

    # Interfaces — `extends A, B, C` appears as `extends_interfaces` node
    # (not a named field).
    for c in node.children:
        if c.type == "extends_interfaces":
            out.extend(_collect_type_list(c, src))

    return out


def _collect_type_list(container: Node, src: bytes) -> list[str]:
    """A `super_interfaces` / `extends_interfaces` node wraps a `type_list`
    child; extract each comma-separated type name from it.
    """
    out: list[str] = []
    for c in container.named_children:
        if c.type == "type_list":
            for t_node in c.named_children:
                t = _collapse_ws(_text(t_node, src)).rstrip(",")
                if t:
                    out.append(t)
        else:
            t = _collapse_ws(_text(c, src)).rstrip(",")
            if t:
                out.append(t)
    return out


# --- Modifiers / annotations / docs ---------------------------------------


def _modifiers_node(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


def _annotations(node: Node, src: bytes) -> list[str]:
    """Collect `@Annotation` entries from the `modifiers` child node."""
    mods = _modifiers_node(node)
    if mods is None:
        return []
    out: list[str] = []
    for c in mods.named_children:
        if c.type in ("marker_annotation", "annotation"):
            out.append(_collapse_ws(_text(c, src)))
    return out


_VISIBILITY_TOKENS = {"public", "protected", "private"}


def _visibility(
    node: Node,
    src: bytes,
    *,
    is_member: bool,
    parent_kind: Optional[str] = None,
) -> str:
    mods = _modifiers_node(node)
    if mods is not None:
        for c in mods.children:
            if c.type in _VISIBILITY_TOKENS:
                return c.type
    # No explicit modifier — apply Java defaults.
    if not is_member:
        return "internal"  # top-level type: package-private
    # Nested types & members inside an interface / @interface are public by default.
    if parent_kind == KIND_INTERFACE:
        return "public"
    if parent_kind == KIND_ENUM:
        if node.type == "enum_constant":
            return "public"
        if node.type == "constructor_declaration":
            return "private"  # Java spec: enum ctors are implicitly private
    return "internal"  # class / record / enum instance members


def _javadocs(node: Node, src: bytes) -> list[str]:
    """Collect contiguous preceding `/** ... */` block comments as docs.
    Plain `/* ... */` and `//` (`line_comment`) stop the walk — only
    genuine Javadoc blocks count.
    """
    docs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "block_comment":
        text = _text(sib, src)
        if not text.startswith("/**"):
            break  # plain block comment — stop here
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
    """doc_start_byte falling back to node.start_byte — using an explicit
    `is None` check (byte-offset `0` would be falsy under `or`).
    """
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Member name extraction ----------------------------------------------


def _member_name(node: Node, src: bytes) -> Optional[str]:
    kind = node.type
    if kind in (
        "method_declaration",
        "constructor_declaration",
        "compact_constructor_declaration",
        "annotation_type_element_declaration",
        "enum_constant",
    ):
        return _field_text(node, "name", src)
    if kind == "field_declaration":
        # `int a, b, c;` → one field_declaration with multiple variable_declarator
        # children. Pick the first declarator's name (consistent with C# adapter).
        for c in node.named_children:
            if c.type == "variable_declarator":
                return _field_text(c, "name", src)
    return None


# --- Misc helpers --------------------------------------------------------


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
