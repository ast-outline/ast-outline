"""C# adapter — parses .cs files via tree-sitter-c-sharp into Declaration IR."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_c_sharp as tscs

from .base import count_parse_errors, read_source
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DELEGATE,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_EVENT,
    KIND_FIELD,
    KIND_INDEXER,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_OPERATOR,
    KIND_PROPERTY,
    KIND_RECORD,
    KIND_STRUCT,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tscs.language())
_PARSER = Parser(_LANGUAGE)


_TYPE_NODE_KIND = {
    "class_declaration": KIND_CLASS,
    "struct_declaration": KIND_STRUCT,
    "interface_declaration": KIND_INTERFACE,
    "record_declaration": KIND_RECORD,
    "record_struct_declaration": KIND_RECORD,
    "enum_declaration": KIND_ENUM,
}

_MEMBER_NODE_KIND = {
    "method_declaration": KIND_METHOD,
    "constructor_declaration": KIND_CTOR,
    "destructor_declaration": KIND_DTOR,
    "property_declaration": KIND_PROPERTY,
    "indexer_declaration": KIND_INDEXER,
    "event_declaration": KIND_EVENT,
    "event_field_declaration": KIND_EVENT,
    "field_declaration": KIND_FIELD,
    "delegate_declaration": KIND_DELEGATE,
    "operator_declaration": KIND_OPERATOR,
    "conversion_operator_declaration": KIND_OPERATOR,
    "enum_member_declaration": KIND_ENUM_MEMBER,
}


class CSharpAdapter:
    language_name = "csharp"
    display_name = "C#"
    extensions = {".cs"}
    definition_keywords = frozenset({
        "class", "struct", "interface", "enum",
        "record", "delegate", "namespace",
    })
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('global using ', 'using ')
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = read_source(path)
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
    """C# `using` directives at file scope. Source-true preserves
    every shape: plain (`using System`), static (`using static
    System.Math`), alias (`using Foo = System.Bar`), and global
    (`global using System`). Block-scoped `using` inside a `namespace
    Foo { ... }` is intentionally not collected — those are scoped to
    the namespace, not file-level dependencies, and adding them would
    blur the meaning of the imports list."""
    for child in root.named_children:
        if child.type == "using_directive":
            text = _collapse_ws(_text(child, src)).rstrip(";").strip()
            if text:
                out.append(text)


# --- Walk -----------------------------------------------------------------


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    # File-scoped namespaces (`namespace Foo;`) don't nest their types as
    # AST children — the types are siblings. Absorb those siblings into
    # the namespace declaration so the IR matches the user's intent.
    file_scoped_ns: Optional[Declaration] = None
    for child in node.named_children:
        kind = child.type
        if kind == "namespace_declaration":
            out.append(_ns_to_decl(child, src))
            file_scoped_ns = None
        elif kind == "file_scoped_namespace_declaration":
            ns_decl = _ns_to_decl(child, src)
            out.append(ns_decl)
            file_scoped_ns = ns_decl
        elif kind in _TYPE_NODE_KIND:
            type_decl = _type_to_decl(child, src)
            if file_scoped_ns is not None:
                file_scoped_ns.children.append(type_decl)
                file_scoped_ns.end_line = type_decl.end_line
                file_scoped_ns.end_byte = type_decl.end_byte
            else:
                out.append(type_decl)
        elif kind in _MEMBER_NODE_KIND:
            # Rare: top-level members (global using etc)
            for decl in _member_to_decls(child, src):
                if file_scoped_ns is not None:
                    file_scoped_ns.children.append(decl)
                else:
                    out.append(decl)
        # Skip using_directive etc at top level


def _ns_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or ""
    children: list[Declaration] = []
    body = node.child_by_field_name("body")
    scope = body if body is not None else node
    for c in scope.named_children:
        k = c.type
        if k in _TYPE_NODE_KIND:
            children.append(_type_to_decl(c, src))
        elif k in _MEMBER_NODE_KIND:
            children.extend(_member_to_decls(c, src))
        elif k in ("namespace_declaration", "file_scoped_namespace_declaration"):
            children.append(_ns_to_decl(c, src))
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"namespace {name}",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=children,
    )


def _type_to_decl(node: Node, src: bytes) -> Declaration:
    kind = _TYPE_NODE_KIND[node.type]
    name = _field_text(node, "name", src) or "?"
    bases = _base_types(node, src)
    attrs = _attrs(node, src)
    docs = _xml_docs(node, src)
    visibility = _visibility(node, src)
    signature = _type_signature(node, src)

    children: list[Declaration] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for c in body.named_children:
            k = c.type
            if k in _TYPE_NODE_KIND:
                children.append(_type_to_decl(c, src))
            elif k in _MEMBER_NODE_KIND:
                children.extend(_member_to_decls(c, src))

    # Explicit None check — see _member_to_decls for the byte-0 trap.
    doc_start = _leading_doc_start_byte(node, src)
    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        name_line=_decl_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte if doc_start is None else doc_start,
        children=children,
    )


def _member_to_decls(node: Node, src: bytes) -> list[Declaration]:
    """All declarations one member node produces.

    One for most members; one PER declarator for multi-declarator field /
    event-field declarations (``int x, y, z;``,
    ``event EventHandler A, B;``) — a single-name path would silently
    drop every declarator after the first. The siblings share the
    declaration's signature and byte/line range (the source statement is
    one line; ``show y`` should print the whole ``int x, y, z;``).
    """
    kind = _MEMBER_NODE_KIND[node.type]
    names = _member_names(node, src)
    if not names:
        return []
    attrs = _attrs(node, src)
    docs = _xml_docs(node, src)
    visibility = _visibility(node, src, is_member=True, parent_type_kind=_parent_type_kind(node))
    if node.type in ("property_declaration", "indexer_declaration"):
        signature = _property_signature(node, src)
    else:
        signature = _member_signature_text(node, src)
    doc_start = _leading_doc_start_byte(node, src)
    return [
        Declaration(
            kind=kind,
            name=name,
            signature=signature,
            attrs=attrs,
            docs=docs,
            visibility=visibility,
            start_line=node.start_point[0] + 1,
            name_line=_decl_name_line(node),
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            # Explicit None check: `doc_start or node.start_byte` would
            # treat a doc comment at byte 0 (file starts with `///`) as
            # "no doc" and slice past it.
            doc_start_byte=node.start_byte if doc_start is None else doc_start,
        )
        for name in names
    ]


# --- Signature extraction -------------------------------------------------


def _decl_name_line(node: Node) -> int:
    """1-based line where the declaration's signature begins.

    C# folds leading ``[Attribute]`` lists into the declaration node, so
    ``node.start_point`` lands on the attribute line, not the signature.
    The first child that is NOT an ``attribute_list`` is the real head of
    the declaration (modifiers / return type / name), and the name token
    sits on that line for any idiomatic single-head signature. grep's
    def-classifier compares against this line, so an attributed class /
    method is tagged [def] on its signature line, not on an attribute.
    """
    for c in node.children:
        if c.type != "attribute_list":
            return c.start_point[0] + 1
    return node.start_point[0] + 1


def _type_signature(node: Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    start = node.start_byte
    end = body.start_byte if body is not None else node.end_byte
    text = src[start:end].decode("utf8", errors="replace")
    text = _strip_leading_attrs(text)
    return _collapse_ws(text).rstrip(" {")


def _member_signature_text(node: Node, src: bytes) -> str:
    start = node.start_byte
    end = node.end_byte
    cut = None
    for c in node.children:
        if c.type in ("block", "arrow_expression_clause", "accessor_list"):
            cut = c.start_byte
            break
    if cut is not None:
        end = cut
    text = src[start:end].decode("utf8", errors="replace")
    text = _strip_leading_attrs(text)
    text = _collapse_ws(text)
    return text.rstrip(" {=;>").strip()


def _property_signature(node: Node, src: bytes) -> str:
    accessor_list = None
    expr_body = None
    head_end = None
    for c in node.children:
        if c.type == "accessor_list":
            accessor_list = c
            if head_end is None:
                head_end = c.start_byte
            break
        if c.type == "arrow_expression_clause":
            expr_body = c
            if head_end is None:
                head_end = c.start_byte
            break
    head = src[node.start_byte : head_end or node.end_byte].decode("utf8", errors="replace")
    head = _strip_leading_attrs(head)
    head = _collapse_ws(head).rstrip(" {=>")

    if accessor_list is not None:
        accessors: list[str] = []
        for a in accessor_list.named_children:
            if a.type != "accessor_declaration":
                continue
            t = _collapse_ws(_text(a, src))
            for sep in (" {", " =>", ";"):
                i = t.find(sep)
                if i > 0:
                    t = t[:i]
                    break
            accessors.append(t.strip() + ";")
        return f"{head} {'{ ' + ' '.join(accessors) + ' }'}".strip()
    if expr_body is not None:
        expr = _collapse_ws(_text(expr_body, src)).rstrip(";")
        if not expr.startswith("=>"):
            expr = "=> " + expr.lstrip("=> ")
        if len(expr) > 80:
            expr = expr[:77] + "..."
        return f"{head} {expr}".strip()
    return head.strip()


# --- Helpers --------------------------------------------------------------


def _strip_leading_attrs(text: str) -> str:
    s = text.lstrip()
    while s.startswith("["):
        depth = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if ch in "\"'":
                # Skip string / char literal — a `]` or `[` inside a
                # quoted attribute argument (`[Description("has ] in
                # it")]`) is content, not a bracket. Backslash escapes
                # are honored so `"\""` doesn't end the literal early.
                quote = ch
                i += 1
                while i < len(s):
                    if s[i] == "\\":
                        i += 2
                        continue
                    if s[i] == quote:
                        break
                    i += 1
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        s = s[i:].lstrip()
    return s


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _attrs(node: Node, src: bytes) -> list[str]:
    return [_collapse_ws(_text(c, src)) for c in node.children if c.type == "attribute_list"]


def _xml_docs(node: Node, src: bytes) -> list[str]:
    docs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "comment":
        text = _text(sib, src)
        if text.startswith("///"):
            docs.append(text)
        else:
            break
        sib = sib.prev_sibling
    docs.reverse()
    return docs


def _leading_doc_start_byte(node: Node, src: bytes) -> Optional[int]:
    first_doc: Optional[Node] = None
    sib = node.prev_sibling
    while sib is not None and sib.type == "comment":
        if _text(sib, src).startswith("///"):
            first_doc = sib
            sib = sib.prev_sibling
        else:
            break
    return first_doc.start_byte if first_doc is not None else None


def _visibility(node: Node, src: bytes, is_member: bool = False, parent_type_kind: Optional[str] = None) -> str:
    for c in node.children:
        if c.type == "modifier":
            t = _text(c, src)
            if t in ("public", "protected", "internal", "private"):
                return t
    # No explicit modifier:
    if not is_member:
        return "internal"  # C# default for top-level types
    if parent_type_kind in ("interface_declaration", "enum_declaration"):
        return "public"
    return "private"  # C# default for class/struct members


def _parent_type_kind(node: Node) -> Optional[str]:
    p = node.parent
    if p is None:
        return None
    g = p.parent
    return g.type if g is not None else None


def _base_types(type_node: Node, src: bytes) -> list[str]:
    base_list = type_node.child_by_field_name("bases") or next(
        (c for c in type_node.children if c.type == "base_list"), None
    )
    if base_list is None:
        return []
    out: list[str] = []
    for child in base_list.named_children:
        t = _text(child, src).strip().rstrip(",")
        if t:
            out.append(t)
    return out


def _member_names(node: Node, src: bytes) -> list[str]:
    """Names a member node declares — one for most members, one per
    ``variable_declarator`` for (event-)field declarations, where
    ``int x, y, z;`` declares three fields in one statement."""
    kind = node.type
    if kind in (
        "method_declaration",
        "property_declaration",
        "event_declaration",
        "delegate_declaration",
        "indexer_declaration",
    ):
        name = _field_text(node, "name", src)
        return [name] if name else []
    if kind in ("constructor_declaration", "destructor_declaration"):
        name = _field_text(node, "name", src)
        return [name] if name else []
    if kind in ("event_field_declaration", "field_declaration"):
        vd = next((c for c in node.children if c.type == "variable_declaration"), None)
        if vd is not None:
            return [
                name
                for c in vd.named_children
                if c.type == "variable_declarator"
                and (name := _field_text(c, "name", src))
            ]
        return []
    if kind == "enum_member_declaration":
        name = _field_text(node, "name", src)
        return [name] if name else []
    if kind == "operator_declaration":
        # tree-sitter exposes the operator token via the `operator` field
        op_tok = node.child_by_field_name("operator")
        if op_tok is not None:
            return ["operator" + _text(op_tok, src)]
        return ["operator"]
    if kind == "conversion_operator_declaration":
        # `implicit operator decimal(Money m)` → name by target type.
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return ["operator_" + _text(type_node, src)]
        return ["operator"]
    return []


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
