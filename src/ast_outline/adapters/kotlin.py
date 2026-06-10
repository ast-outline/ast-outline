"""Kotlin adapter — parses .kt / .kts files via tree-sitter-kotlin into Declaration IR.

Design notes (how Kotlin concepts map to the IR):

- `package_header`                → KIND_NAMESPACE (absorbs all top-level
                                    declarations that follow, mirroring the
                                    Java and C# file-scoped-namespace pattern)
- `class_declaration` + `class`   → KIND_CLASS (regular / abstract / open / sealed)
- `class_declaration` + `interface` keyword → KIND_INTERFACE (also `fun interface`)
- `class_declaration` with class_modifier keyword:
    `data`                        → KIND_RECORD   (data classes are record-like)
    `enum`                        → KIND_ENUM
    `annotation`                  → KIND_INTERFACE (`annotation class Foo`)
    `sealed`                      → KIND_CLASS    (still a class semantically)
- `object_declaration`            → KIND_CLASS   (signature starts `object Name`)
- `companion_object`              → KIND_CLASS   (`companion object [Name]`;
                                    name defaults to `Companion`)
- `function_declaration`          → KIND_METHOD inside a type,
                                    KIND_FUNCTION at top level (extension
                                    functions too — receiver is kept in the
                                    signature)
- `property_declaration`          → KIND_PROPERTY if it declares a `get()` /
                                    `set()` accessor; otherwise KIND_FIELD
- `class_parameter` (val/var)     → KIND_FIELD   (implicit property from
                                    primary constructor, like Java record
                                    components)
- `secondary_constructor`         → KIND_CTOR
- `enum_entry`                    → KIND_ENUM_MEMBER
- `type_alias`                    → KIND_DELEGATE (named type synonym)
- `anonymous_initializer` (`init { }`) — skipped; not a named declaration

Modifiers & annotations live inside a `modifiers` child node:
- Visibility: `public` / `protected` / `private` / `internal` via `visibility_modifier`
- Class kind: `data` / `enum` / `sealed` / `annotation` via `class_modifier`
- Inheritance: `abstract` / `open` / `final` via `inheritance_modifier`
- Function / property flavour: `suspend`, `inline`, `const`, `lateinit`,
  `override`, `operator`, `infix`, `tailrec`, `external` via their respective
  `*_modifier` sub-nodes
- Annotations: `annotation` named child — `@Foo` / `@Foo(args)` / `@Foo.Bar`

Visibility default is **public** at every scope (unlike Java's package-private
or C#'s internal default). Only an explicit `visibility_modifier` overrides.

Docs: KDoc `/** ... */` is a `block_comment` node — only block comments
starting with `/**` are captured. Line comments (`//`) and plain block comments
(`/* ... */`) are ignored. The walk mirrors Java's Javadoc collection.

Supported: generics with bounds, `where` type constraints, data / sealed / enum
classes, annotation classes, companion objects (named + unnamed), extension
functions (receiver kept in signature), nullable types (`Foo?`), typealiases,
secondary constructors, multi-modifier chains (`public open suspend inline`),
`fun interface`, `by` delegation (kept verbatim in the signature).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_kotlin as tsk
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
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
    KIND_PROPERTY,
    KIND_RECORD,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tsk.language())
_PARSER = Parser(_LANGUAGE)


class KotlinAdapter:
    language_name = "kotlin"
    display_name = "Kotlin"
    extensions = {".kt", ".kts"}
    definition_keywords = frozenset({
        "class", "interface", "object", "enum", "fun",
    })
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('import ',)
    render_family = "code"

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
    """Kotlin imports are top-level only. Source-true text — `import foo.Bar`,
    `import foo.*`, `import foo.Bar as Baz` — reads as the language."""
    for child in root.named_children:
        if child.type == "import":
            text = _collapse_ws(_text(child, src)).strip()
            if text:
                out.append(text)


# --- Walk -----------------------------------------------------------------


_TOP_DECL_KINDS = {
    "class_declaration",
    "object_declaration",
    "function_declaration",
    "property_declaration",
    "type_alias",
}


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Handle the file-level structure: optional `package_header` followed by
    imports and declarations. The package node absorbs all trailing top-level
    declarations, matching how Java's `package_declaration` behaves in this
    adapter family.
    """
    package_ns: Optional[Declaration] = None
    for child in node.named_children:
        kind = child.type
        if kind == "package_header":
            package_ns = _package_to_decl(child, src)
            out.append(package_ns)
        elif kind in _TOP_DECL_KINDS:
            decl = _decl_from_node(child, src, parent_kind=None)
            if decl is None:
                continue
            if package_ns is not None:
                package_ns.children.append(decl)
                package_ns.end_line = decl.end_line
                package_ns.end_byte = decl.end_byte
            else:
                out.append(decl)
        # imports, file_annotation, comments — skip


def _package_to_decl(node: Node, src: bytes) -> Declaration:
    """`package_header` wraps a `qualified_identifier` (dotted path) or a bare
    `identifier`. Extract the textual name and synthesise a namespace.
    """
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type in ("qualified_identifier", "identifier"):
            name_node = c
            break
    name = _collapse_ws(_text(name_node, src)) if name_node is not None else ""
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"package {name}" if name else "package",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _decl_from_node(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """Dispatch to the right builder for a top-level or nested declaration.
    Returns None for node types that shouldn't appear in the outline
    (anonymous `init { }`, stray comments, etc.).
    """
    t = node.type
    if t == "class_declaration":
        return _type_to_decl(node, src, parent_kind=parent_kind)
    if t == "object_declaration":
        return _object_to_decl(node, src, parent_kind=parent_kind)
    if t == "companion_object":
        return _companion_to_decl(node, src, parent_kind=parent_kind)
    if t == "function_declaration":
        return _function_to_decl(node, src, parent_kind=parent_kind)
    if t == "property_declaration":
        return _property_to_decl(node, src, parent_kind=parent_kind)
    if t == "secondary_constructor":
        return _secondary_ctor_to_decl(node, src, parent_kind=parent_kind)
    if t == "type_alias":
        return _type_alias_to_decl(node, src)
    if t == "enum_entry":
        return _enum_entry_to_decl(node, src)
    return None


# --- Types (class / interface / object / companion) ----------------------


def _type_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Build a Declaration for a `class_declaration` node — covers classes,
    interfaces (incl. `fun interface`), enum/data/sealed/annotation classes.
    """
    kind = _class_decl_kind(node)
    # Source-true keyword for digest. Kotlin maps several constructs onto
    # the same canonical kind for search uniformity (`data class` →
    # KIND_RECORD, `annotation class` → KIND_INTERFACE), but the digest
    # should print what the source actually says so the agent doesn't
    # mistake an annotation class for an ordinary interface or a data
    # class for a Java-style record.
    native_kind = _kotlin_native_keyword(node, kind)
    name = _field_text(node, "name", src) or "?"
    bases = _delegation_bases(node, src)
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children = _collect_type_children(node, src, kind=kind)
    # Prepend primary-constructor val/var parameters as implicit fields so
    # they surface in the outline even for empty-bodied data classes.
    pc_fields = _primary_ctor_fields(node, src)
    children = pc_fields + children

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


def _object_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Singleton `object Foo : Bar() { ... }`. Modelled as KIND_CLASS with
    an `object` signature and normal delegation/base extraction — so its
    type header carries `: Bar` like any other class.
    """
    name = _field_text(node, "name", src) or "?"
    bases = _delegation_bases(node, src)
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children = _collect_type_children(node, src, kind=KIND_CLASS)

    return Declaration(
        kind=KIND_CLASS,
        native_kind="object",
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


def _companion_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """`companion object [Name] { ... }` — defaults to name `Companion` when
    unnamed (matches Kotlin's compiled name).
    """
    name = _field_text(node, "name", src) or "Companion"
    bases = _delegation_bases(node, src)
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
    visibility = _visibility(node)
    signature = _type_signature(node, src)

    children = _collect_type_children(node, src, kind=KIND_CLASS)

    return Declaration(
        kind=KIND_CLASS,
        native_kind="companion object",
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


def _kotlin_native_keyword(node: Node, kind: str) -> str:
    """Return the source-true keyword for a Kotlin class_declaration when
    the canonical kind hides it.

    - `data class` → "data class" (kind=KIND_RECORD)
    - `annotation class` → "annotation class" (kind=KIND_INTERFACE)
    - regular class / interface / enum class → empty (canonical kind
      already matches the source keyword)
    """
    mods = _modifiers_node(node)
    if mods is None:
        return ""
    for m in mods.named_children:
        if m.type != "class_modifier":
            continue
        token = m.text.decode("utf8", errors="replace").strip()
        if token == "data":
            return "data class"
        if token == "annotation":
            return "annotation class"
    return ""


def _class_decl_kind(node: Node) -> str:
    """Distinguish class / interface / enum / data / annotation by looking
    at direct children (the keyword) and the `modifiers` child (class-
    modifier keywords).
    """
    # An `interface` keyword appears directly as a child of the node, as
    # does `class`. Either wins over the modifiers below.
    for c in node.children:
        if c.type == "interface":
            return KIND_INTERFACE
        if c.type == "class":
            break
    mods = _modifiers_node(node)
    if mods is not None:
        for m in mods.named_children:
            if m.type == "class_modifier":
                token = m.text.decode("utf8", errors="replace").strip()
                if token == "enum":
                    return KIND_ENUM
                if token == "data":
                    return KIND_RECORD
                if token == "annotation":
                    return KIND_INTERFACE
                # sealed / inline / value — still a class
    return KIND_CLASS


# --- Type bodies (class_body / enum_class_body) --------------------------


def _type_body(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type in ("class_body", "enum_class_body"):
            return c
    return None


def _collect_type_children(
    node: Node, src: bytes, *, kind: str
) -> list[Declaration]:
    """Walk the class/enum body, producing Declaration children for every
    member we recognise. Enum entries come first (they appear at the top of
    an `enum_class_body`); regular members follow.
    """
    out: list[Declaration] = []
    body = _type_body(node)
    if body is None:
        return out
    for c in body.named_children:
        decl = _decl_from_node(c, src, parent_kind=kind)
        if decl is not None:
            out.append(decl)
    return out


def _primary_ctor_fields(node: Node, src: bytes) -> list[Declaration]:
    """For `class Foo(val a: Int, var b: String)` — return [a, b] as
    KIND_FIELD declarations so the outline surfaces them even when the
    class has no body (e.g. `data class Point(val x: Int, val y: Int)`).

    Parameters without `val`/`var` are plain constructor arguments and
    don't create properties — they're skipped.
    """
    out: list[Declaration] = []
    pc: Optional[Node] = None
    for c in node.children:
        if c.type == "primary_constructor":
            pc = c
            break
    if pc is None:
        return out

    params: Optional[Node] = None
    for c in pc.children:
        if c.type == "class_parameters":
            params = c
            break
    if params is None:
        return out

    for cp in params.named_children:
        if cp.type != "class_parameter":
            continue
        decl = _class_parameter_to_field(cp, src)
        if decl is not None:
            out.append(decl)
    return out


def _class_parameter_to_field(node: Node, src: bytes) -> Optional[Declaration]:
    """A single `class_parameter`. Returns None for plain args (no val/var)."""
    is_property = False
    for c in node.children:
        if c.type in ("val", "var"):
            is_property = True
            break
    if not is_property:
        return None

    name_node: Optional[Node] = None
    for c in node.children:
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
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Functions / properties / constructors -------------------------------


def _function_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """Function or method. Extension functions keep their receiver type in
    the rendered signature — so `fun String.reversed2()` reads the same as
    the source.
    """
    kind = KIND_METHOD if parent_kind is not None else KIND_FUNCTION
    name = _field_text(node, "name", src) or "?"
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
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


def _secondary_ctor_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    """`constructor(...) [: this(...) / super(...)] { ... }` inside a class."""
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
    visibility = _visibility(node)
    signature = _callable_signature(node, src)
    return Declaration(
        kind=KIND_CTOR,
        name="constructor",
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
    """val / var — at class level or top-level. Presence of a `getter` or
    `setter` child promotes the kind from FIELD to PROPERTY.
    """
    name = _property_name(node, src)
    if not name:
        return None

    has_accessor = any(c.type in ("getter", "setter") for c in node.children)
    kind = KIND_PROPERTY if has_accessor else KIND_FIELD

    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
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


def _property_name(node: Node, src: bytes) -> Optional[str]:
    """The name lives inside a `variable_declaration` child as its first
    `identifier`. Destructuring declarations (`val (a, b) = ...`) use a
    `multi_variable_declaration` node — rare at class/top level, so we just
    pick the first identifier inside it for a sensible fallback.
    """
    for c in node.named_children:
        if c.type in ("variable_declaration", "multi_variable_declaration"):
            for cc in c.named_children:
                if cc.type == "identifier":
                    return _text(cc, src)
    return None


# --- Enum entries / typealias --------------------------------------------


def _enum_entry_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return None
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
    sig = _collapse_ws(_strip_leading_annotations(_text(node, src))).rstrip(",")
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


def _type_alias_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`typealias Handler = (String) -> Unit` — the name is the first
    `identifier` child (tree-sitter-kotlin labels it `field=type`, which is
    misleading, so we ignore the field name and walk positionally).
    """
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return None
    attrs = _annotations(node, src)
    docs = _kdocs(node, src)
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
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


# --- Signature extraction -------------------------------------------------


def _type_signature(node: Node, src: bytes) -> str:
    """Slice from the start of the declaration up to (but not including) the
    body — covers modifiers, keywords, name, type parameters, primary
    constructor params, `: Base` delegation list, and `where` constraints.
    Leading annotations are stripped.
    """
    body = _type_body(node)
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _callable_signature(node: Node, src: bytes) -> str:
    """Slice up to the function/constructor body — a `function_body` child
    (`= expr` or `{ ... }`) or a `block` child for secondary constructors.
    Abstract members have no body, so we take the whole node.
    """
    cut: Optional[int] = None
    for c in node.children:
        if c.type in ("function_body", "block"):
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;=").rstrip()


def _property_signature(node: Node, src: bytes) -> str:
    """Cut before the first `getter`/`setter` if present — so
    `val species: String get() = "unknown"` renders as `val species: String`
    (the accessor body is noise in an outline). Otherwise keep the whole
    declaration (including initialiser), which is meaningful for inferred-
    type properties like `const val MAX = 10`.
    """
    cut: Optional[int] = None
    for c in node.children:
        if c.type in ("getter", "setter"):
            cut = c.start_byte
            break
    end = cut if cut is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_annotations(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


# --- Delegation / base types ---------------------------------------------


def _delegation_bases(node: Node, src: bytes) -> list[str]:
    """Pull the superclass and implemented interfaces out of a
    `delegation_specifiers` child.

    A delegation_specifier is one of:
      - `constructor_invocation` — `Base(args)` → superclass
      - `user_type` — `Interface` → interface
      - `explicit_delegation` — `Iface by impl` → interface (with delegation)

    We return just the type name text (including generics) so downstream
    `_normalize_type_name` can suffix-match it.
    """
    for c in node.children:
        if c.type == "delegation_specifiers":
            return _collect_delegation_types(c, src)
    return []


def _collect_delegation_types(container: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for spec in container.named_children:
        if spec.type != "delegation_specifier":
            continue
        type_text = _delegation_type_text(spec, src)
        if type_text:
            out.append(type_text)
    return out


def _delegation_type_text(spec: Node, src: bytes) -> Optional[str]:
    """Pick the type-name portion from a delegation_specifier's child tree:
    for `Base()` the child is `constructor_invocation` whose first named
    child is the `user_type`; for `Interface` / `Interface<T>` the spec's
    own named child is the `user_type`; for `Iface by impl` we grab the
    leading `user_type`.
    """
    for c in spec.named_children:
        if c.type == "constructor_invocation":
            for cc in c.named_children:
                if cc.type == "user_type":
                    return _collapse_ws(_text(cc, src))
        if c.type == "user_type":
            return _collapse_ws(_text(c, src))
        if c.type == "explicit_delegation":
            for cc in c.named_children:
                if cc.type == "user_type":
                    return _collapse_ws(_text(cc, src))
    return None


# --- Annotations / docs / modifiers --------------------------------------


def _modifiers_node(node: Node) -> Optional[Node]:
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


def _annotations(node: Node, src: bytes) -> list[str]:
    """Collect `@Annotation` / `@Annotation(args)` entries from the
    `modifiers` child. Nested inside annotation nodes there can be a
    `constructor_invocation` (args) or a bare `user_type` (no args); in
    either case we take the surface `annotation` node's text as-is.
    """
    mods = _modifiers_node(node)
    if mods is None:
        return []
    out: list[str] = []
    for c in mods.named_children:
        if c.type == "annotation":
            out.append(_collapse_ws(_text(c, src)))
    return out


_VISIBILITY_TOKENS = {"public", "protected", "private", "internal"}


def _visibility(node: Node) -> str:
    """Kotlin defaults to `public` everywhere — only a `visibility_modifier`
    child inside `modifiers` overrides.
    """
    mods = _modifiers_node(node)
    if mods is not None:
        for c in mods.named_children:
            if c.type == "visibility_modifier":
                # visibility_modifier wraps the keyword as its first child
                for cc in c.children:
                    if cc.type in _VISIBILITY_TOKENS:
                        return cc.type
    return "public"


def _kdocs(node: Node, src: bytes) -> list[str]:
    """Contiguous preceding `/** ... */` comments are KDoc. Plain `/* */`
    or `//` comments break the walk.
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
    """doc_start_byte with a `None`-aware fallback — offset `0` is falsy
    under a naive `or`, so we check explicitly.
    """
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Annotation stripping (shared Java-style scanner) --------------------


def _strip_leading_annotations(text: str) -> str:
    """Drop one or more leading `@Foo` / `@Foo(...)` / `@Foo.Bar` annotations
    from rendered signature text. Parens are balanced; string/char literals
    inside args are skipped so `@Foo(msg = "(...)")` parses cleanly.
    """
    s = text.lstrip()
    while s.startswith("@"):
        i = 1
        # Optional `use-site target` like `@file:`, `@get:`, `@param:` — keep
        # scanning through the colon before the identifier body.
        while i < len(s) and (s[i].isalnum() or s[i] in "._"):
            i += 1
        if i < len(s) and s[i] == ":":
            i += 1
            while i < len(s) and (s[i].isalnum() or s[i] in "._"):
                i += 1
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


def _skip_string_literal(s: str, i: int, quote: str) -> int:
    """Advance past `"..."` or `'...'`, honouring `\\` escapes. If the
    literal is unterminated we return `len(s)` so the outer scanner exits.
    """
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
