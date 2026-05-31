"""Swift adapter — parses .swift files via tree-sitter-swift into Declaration IR.

Design notes (how Swift concepts map to the IR):

- `import_declaration`                → imports (source-true text)
- `class_declaration` + `class`       → KIND_CLASS
- `class_declaration` + `struct`      → KIND_STRUCT
- `class_declaration` + `enum`        → KIND_ENUM
- `class_declaration` + `extension`   → KIND_CLASS (signature starts `extension Name`)
- `class_declaration` + `actor`       → KIND_CLASS (`native_kind=actor`)
- `protocol_declaration`              → KIND_INTERFACE
- `function_declaration`              → KIND_METHOD inside a type,
                                        KIND_FUNCTION at top level
- `init_declaration`                  → KIND_CTOR
- `deinit_declaration`                → KIND_DTOR
- `subscript_declaration`             → KIND_INDEXER
- `property_declaration`              → KIND_PROPERTY if it has a `computed_property`
                                        child (getter/setter); otherwise KIND_FIELD
- `protocol_function_declaration`     → KIND_METHOD (inside protocol)
- `protocol_property_declaration`     → KIND_PROPERTY (inside protocol)
- `typealias_declaration`             → KIND_DELEGATE
- `associatedtype_declaration`        → KIND_DELEGATE (`native_kind=associatedtype`)
- `enum_entry`                        → KIND_ENUM_MEMBER

Modifiers live inside a `modifiers` child node:
- Visibility: `public` / `private` / `internal` / `fileprivate` / `open` via
  `visibility_modifier`
- Inheritance: `final` via `inheritance_modifier`
- Function: `static` / `class` / `override` / `convenience` / `required` via
  their respective modifier nodes
- Attributes: `attribute` nodes — `@objc`, `@available(...)`, `@Published`, etc.

Visibility default is **internal** at every scope (Swift's language default).
Only an explicit `visibility_modifier` overrides.

Docs: Swift doc comments `/// ...` appear as `comment` nodes; `/** ... */`
appear as `multiline_comment` nodes.
Contiguous preceding `///` comments are captured; plain `//` or `/* */` break
the walk.

Supported: generics with bounds, `where` clauses, protocol conformance,
extensions, attributes (including multi-line), computed properties,
convenience/required inits, subscripts, nested types, typealiases, actors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_swift as tss
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DELEGATE,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INDEXER,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_PROPERTY,
    KIND_STRUCT,
    Declaration,
    ParseResult,
)

_LANGUAGE = Language(tss.language())
_PARSER = Parser(_LANGUAGE)


class SwiftAdapter:
    language_name = "swift"
    extensions = {".swift"}
    definition_keywords = frozenset({
        "class", "struct", "enum", "protocol", "func", "extension",
    })

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
    """Swift imports are top-level only. Source-true text — `import Foundation`,
    `import UIKit.View` — reads as the language.
    """
    for child in root.named_children:
        if child.type == "import_declaration":
            text = _collapse_ws(_text(child, src)).rstrip(";").strip()
            if text:
                out.append(text)


# --- Walk -----------------------------------------------------------------

_TOP_DECL_KINDS = {
    "class_declaration",
    "protocol_declaration",
    "function_declaration",
    "property_declaration",
    "typealias_declaration",
}


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Handle the file-level structure: imports followed by declarations."""
    for child in node.named_children:
        kind = child.type
        if kind in _TOP_DECL_KINDS:
            decl = _decl_from_node(child, src, parent_kind=None)
            if decl is not None:
                out.append(decl)
        # imports, comments — skip


def _decl_from_node(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Dispatch to the right builder for a top-level or nested declaration."""
    t = node.type
    if t == "class_declaration":
        return _class_decl_to_decl(node, src, parent_kind=parent_kind)
    if t == "protocol_declaration":
        return _protocol_to_decl(node, src, parent_kind=parent_kind)
    if t == "function_declaration":
        return _function_to_decl(node, src, parent_kind=parent_kind)
    if t == "property_declaration":
        return _property_to_decl(node, src, parent_kind=parent_kind)
    if t == "init_declaration":
        return _init_to_decl(node, src, parent_kind=parent_kind)
    if t == "deinit_declaration":
        return _deinit_to_decl(node, src, parent_kind=parent_kind)
    if t == "subscript_declaration":
        return _subscript_to_decl(node, src, parent_kind=parent_kind)
    if t == "typealias_declaration":
        return _typealias_to_decl(node, src)
    if t == "associatedtype_declaration":
        return _associatedtype_to_decl(node, src)
    if t == "enum_entry":
        return _enum_entry_to_decl(node, src)
    if t == "protocol_function_declaration":
        return _protocol_function_to_decl(node, src, parent_kind=parent_kind)
    if t == "protocol_property_declaration":
        return _protocol_property_to_decl(node, src, parent_kind=parent_kind)
    return None


# --- Types (class / struct / enum / extension) ----------------------------


def _class_decl_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Build a Declaration for a `class_declaration` node — covers classes,
    structs, enums, and extensions.
    """
    keyword = _class_keyword(node)
    if keyword == "extension":
        kind = KIND_CLASS
        native_kind = "extension"
    elif keyword == "actor":
        kind = KIND_CLASS
        native_kind = "actor"
    elif keyword == "struct":
        kind = KIND_STRUCT
        native_kind = ""
    elif keyword == "enum":
        kind = KIND_ENUM
        native_kind = ""
    else:
        kind = KIND_CLASS
        native_kind = ""

    name = _field_text(node, "name", src) or "?"
    bases = _inheritance_bases(node, src)
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children = _collect_type_children(node, src, kind=kind)

    return Declaration(
        kind=kind,
        native_kind=native_kind,
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


def _protocol_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`protocol Foo : Bar { ... }`"""
    name = _field_text(node, "name", src) or "?"
    bases = _protocol_bases(node, src)
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    signature = _protocol_signature(node, src)

    children: list[Declaration] = []
    body = _protocol_body(node)
    if body is not None:
        for c in body.named_children:
            decl = _decl_from_node(c, src, parent_kind=KIND_INTERFACE)
            if decl is not None:
                children.append(decl)

    return Declaration(
        kind=KIND_INTERFACE,
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


def _class_keyword(node: Node) -> str:
    """Returns the declaration keyword: `class`, `struct`, `enum`,
    `extension`, or `actor`. Defaults to `class` if not found."""
    for c in node.children:
        if c.type in ("class", "struct", "enum", "extension", "actor"):
            return c.type
    return "class"


# --- Type bodies ----------------------------------------------------------


def _type_body(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type in ("class_body", "enum_class_body"):
            return c
    return None


def _protocol_body(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type == "protocol_body":
            return c
    return None


def _collect_type_children(
    node: Node, src: bytes, *, kind: str
) -> list[Declaration]:
    """Walk the class/enum body, producing Declaration children."""
    out: list[Declaration] = []
    body = _type_body(node)
    if body is None:
        return out
    for c in body.named_children:
        decl = _decl_from_node(c, src, parent_kind=kind)
        if decl is not None:
            out.append(decl)
    return out


# --- Functions / properties / constructors --------------------------------


def _function_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    kind = KIND_METHOD if parent_kind is not None else KIND_FUNCTION
    name = _field_text(node, "name", src) or "?"
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
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
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _protocol_function_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Function declaration inside a protocol body."""
    name = _field_text(node, "name", src) or "?"
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = "public"  # Protocol members are implicitly public
    signature = _protocol_callable_signature(node, src)

    return Declaration(
        kind=KIND_METHOD,
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


def _init_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`init(...) { ... }` or `init?(...) { ... }`"""
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    signature = _init_signature(node, src)

    return Declaration(
        kind=KIND_CTOR,
        name="init",
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


def _deinit_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`deinit { ... }`"""
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    sig = _collapse_ws(_text(node, src))
    # Strip the body for the signature
    for c in node.children:
        if c.type == "function_body":
            sig = src[node.start_byte:c.start_byte].decode("utf8", errors="replace")
            sig = _collapse_ws(sig).rstrip(" {;")
            break

    return Declaration(
        kind=KIND_DTOR,
        name="deinit",
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _subscript_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`subscript(index: Int) -> String { get { ... } set { ... } }`"""
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    signature = _subscript_signature(node, src)

    return Declaration(
        kind=KIND_INDEXER,
        name="subscript",
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


def _property_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`var` / `let` — at class level or top-level. Presence of a
    `computed_property` child promotes the kind from FIELD to PROPERTY.
    """
    name = _property_name(node, src)
    if not name:
        return None

    has_computed = any(c.type == "computed_property" for c in node.children)
    kind = KIND_PROPERTY if has_computed else KIND_FIELD

    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    signature = _property_signature(node, src)

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


def _protocol_property_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Property declaration inside a protocol body."""
    name = _property_name(node, src)
    if not name:
        return None

    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = "public"  # Protocol members are implicitly public
    signature = _protocol_property_signature(node, src)

    return Declaration(
        kind=KIND_PROPERTY,
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


def _property_name(node: Node, src: bytes) -> Optional[str]:
    """Extract the identifier name from a property_declaration's pattern."""
    for c in node.named_children:
        if c.type == "pattern":
            for cc in c.named_children:
                if cc.type == "simple_identifier":
                    return _text(cc, src)
    return None


# --- Enum entries / typealias ---------------------------------------------


def _enum_entry_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """A single `enum_entry` like `case active` or `case active = 1`."""
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "simple_identifier":
            name_node = c
            break
    if name_node is None:
        return None
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    sig = _collapse_ws(_text(node, src)).rstrip(",")
    return Declaration(
        kind=KIND_ENUM_MEMBER,
        name=_text(name_node, src),
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _typealias_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`typealias Handler = (String) -> Void`"""
    name = _field_text(node, "name", src) or "?"
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = _visibility(node)
    sig = _collapse_ws(_text(node, src)).rstrip(";")
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
        doc_start_byte=_resolved_doc_start(node, src),
    )


def _associatedtype_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`associatedtype Item` inside a protocol body."""
    name = _field_text(node, "name", src) or "?"
    attrs = _attributes(node, src)
    docs = _swift_docs(node, src)
    visibility = "public"  # Protocol members are implicitly public
    sig = _collapse_ws(_text(node, src)).rstrip(";")
    return Declaration(
        kind=KIND_DELEGATE,
        native_kind="associatedtype",
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


# --- Signature extraction -------------------------------------------------


def _type_signature(node: Node, src: bytes) -> str:
    """Slice from start of the declaration up to (but not including) the
    body — covers modifiers, keywords, name, generics, inheritance, and
    `where` constraints. Leading attributes are stripped.
    """
    body = _type_body(node)
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _protocol_signature(node: Node, src: bytes) -> str:
    """Same as _type_signature but for protocols."""
    body = _protocol_body(node)
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _callable_signature(node: Node, src: bytes) -> str:
    """Slice up to the function body. Annotations stripped."""
    cut: Optional[int] = None
    for c in node.children:
        if c.type == "function_body":
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;=").rstrip()


def _protocol_callable_signature(node: Node, src: bytes) -> str:
    """Protocol function declarations have no body — take the whole node."""
    text = _text(node, src)
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;")


def _init_signature(node: Node, src: bytes) -> str:
    """Include the `init` keyword and parameters, cut before body."""
    cut: Optional[int] = None
    for c in node.children:
        if c.type == "function_body":
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;=").rstrip()


def _subscript_signature(node: Node, src: bytes) -> str:
    """Include the `subscript` keyword, parameters, and return type."""
    cut: Optional[int] = None
    for c in node.children:
        if c.type == "computed_property":
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;=").rstrip()


def _property_signature(node: Node, src: bytes) -> str:
    """Cut before `=` or `computed_property` if present."""
    cut: Optional[int] = None
    for c in node.children:
        if c.type in ("computed_property",):
            cut = c.start_byte
            break
        if c.type == "=" and cut is None:
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;=").rstrip()


def _protocol_property_signature(node: Node, src: bytes) -> str:
    """Protocol property declarations end at the getter/setter spec."""
    text = _text(node, src)
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;")


# --- Inheritance / base types ---------------------------------------------


def _inheritance_bases(node: Node, src: bytes) -> list[str]:
    """Collect inheritance specifiers from a class/struct/enum declaration."""
    out: list[str] = []
    for c in node.children:
        if c.type == "inheritance_specifier":
            text = _collapse_ws(_text(c, src)).rstrip(",")
            if text:
                out.append(text)
    return out


def _protocol_bases(node: Node, src: bytes) -> list[str]:
    """Collect inherited protocols from a protocol declaration."""
    out: list[str] = []
    for c in node.children:
        if c.type == "inheritance_specifier":
            text = _collapse_ws(_text(c, src)).rstrip(",")
            if text:
                out.append(text)
    return out


# --- Attributes / docs / modifiers ----------------------------------------


def _modifiers_node(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


def _attributes(node: Node, src: bytes) -> list[str]:
    """Collect `@Attribute` / `@Attribute(args)` entries from the
    `modifiers` child. We take the surface `attribute` node's text as-is.
    """
    mods = _modifiers_node(node)
    if mods is None:
        return []
    out: list[str] = []
    for c in mods.named_children:
        if c.type == "attribute":
            out.append(_collapse_ws(_text(c, src)))
    return out


_VISIBILITY_TOKENS = {"public", "private", "internal", "fileprivate", "open"}


def _visibility(node: Node) -> str:
    """Swift defaults to `internal` everywhere — only a `visibility_modifier`
    child inside `modifiers` overrides.
    """
    mods = _modifiers_node(node)
    if mods is not None:
        for c in mods.children:
            if c.type == "visibility_modifier":
                for cc in c.children:
                    if cc.type in _VISIBILITY_TOKENS:
                        return cc.type
    return "internal"


def _swift_docs(node: Node, src: bytes) -> list[str]:
    """Contiguous preceding `/// ...` and `/** ... */` comments are docs.
    Plain `// ...` or `/* ... */` comments break the walk.
    """
    docs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type in ("comment", "multiline_comment"):
        text = _text(sib, src)
        if not _is_swift_doc_comment(text):
            break
        docs.append(text)
        sib = sib.prev_sibling
    docs.reverse()
    return docs


def _leading_doc_start_byte(node: Node, src: bytes) -> Optional[int]:
    first: Optional[Node] = None
    sib = node.prev_sibling
    while sib is not None and sib.type in ("comment", "multiline_comment"):
        if _is_swift_doc_comment(_text(sib, src)):
            first = sib
            sib = sib.prev_sibling
        else:
            break
    return first.start_byte if first is not None else None


def _is_swift_doc_comment(text: str) -> bool:
    return text.startswith("///") or text.startswith("/**")


def _resolved_doc_start(node: Node, src: bytes) -> int:
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Annotation stripping -------------------------------------------------


def _strip_leading_annotations(text: str) -> str:
    """Drop one or more leading `@Foo` / `@Foo(...)` annotations from the
    signature text. Handles balanced parens and skips string/char literals.
    """
    s = text.lstrip()
    while s.startswith("@"):
        i = 1
        # Identifier (supports dots for qualified names)
        while i < len(s) and (s[i].isalnum() or s[i] in "._"):
            i += 1
        # Optional (...) argument list
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
    """Advance past a quoted string literal."""
    i += 1
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            i += 2
            continue
        if s[i] == quote:
            return i + 1
        i += 1
    return i


# --- Misc helpers --------------------------------------------------------


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
