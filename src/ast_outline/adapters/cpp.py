"""C++ adapter — parses .cpp/.h files via tree-sitter-cpp into Declaration IR.

Design notes (how C++ concepts map to the IR):

- ``namespace_definition``         → KIND_NAMESPACE. C++17 nested namespace
                                     (``namespace a::b::c { }``) lands as
                                     a single declaration with name
                                     ``a::b::c``. Old-style nested
                                     ``namespace a { namespace b { … } }``
                                     where each level holds exactly one
                                     namespace and nothing else is
                                     transparently collapsed into the
                                     same single-line form, so the
                                     outline reads the same regardless
                                     of which spelling the source uses.
                                     Anonymous namespaces render as
                                     ``namespace <anonymous>``; inline
                                     namespaces keep the keyword in the
                                     name (``namespace inline v1``).
- ``class_specifier``              → KIND_CLASS. Default member visibility
                                     is ``private`` per the C++ language
                                     spec — applied unless an
                                     ``access_specifier`` block (``public:``
                                     / ``protected:``) precedes the member.
- ``struct_specifier``             → KIND_STRUCT. Default visibility
                                     ``public``.
- ``union_specifier``              → KIND_STRUCT with ``native_kind="union"``
                                     so the digest renders ``union Foo``
                                     while symbol search treats it like a
                                     struct. Union variants carry the
                                     ``union`` keyword in the signature.
- ``enum_specifier``               → KIND_ENUM. Both classic ``enum`` and
                                     C++11 ``enum class`` / ``enum struct``
                                     map here; the keyword form is
                                     preserved in the signature so an
                                     agent can tell strongly-typed enums
                                     apart at a glance. Enumerators
                                     surface as KIND_ENUM_MEMBER children.
- ``template_declaration``         → transparent wrapper. The template
                                     header (``template<typename T, …>``)
                                     becomes the prefix of the wrapped
                                     declaration's signature; the IR
                                     declaration is the inner type or
                                     function, not the template node.
- ``function_definition`` /
  ``declaration`` w/ function_declarator
  at file or namespace scope       → KIND_FUNCTION (free function).
- The same nodes inside a class body  → KIND_METHOD / KIND_CTOR / KIND_DTOR /
                                     KIND_OPERATOR depending on the
                                     declarator's name shape (identifier,
                                     destructor_name, operator_name,
                                     operator_cast). ``= default`` /
                                     ``= delete`` clauses are kept in the
                                     signature.
- ``field_declaration`` (data)     → KIND_FIELD.
- ``friend_declaration``           → skipped — declares relationships, not
                                     members of the current scope.
- ``preproc_include``              → ``imports`` entry, source-true
                                     (``#include <vector>`` /
                                     ``#include "foo.h"``). Other
                                     preprocessor directives (``#define``,
                                     ``#if``) are deliberately ignored —
                                     they're not dependencies.

Out-of-class definitions like ``void Widget::draw() const { … }`` at file
scope render as free functions whose name is the qualified form
(``Widget::draw``). The adapter does not attempt to re-attach them to
their declaring class — that would need cross-file resolution and is the
job of a real C++ frontend, not a tree-sitter outline. The ``::`` in the
name keeps the relationship visible to anyone reading the outline.

Unreal Engine reflection macros are recognised by default and surface
on the next declaration as attrs (mirroring how the C# adapter
captures ``[Attribute]`` decorators). The mapping is:

- ``UCLASS(...)``, ``USTRUCT(...)``, ``UENUM(...)``,
  ``UINTERFACE(...)``, ``UDELEGATE(...)`` — at file or namespace
  scope, attached as an attr on the next type declaration.
- ``UPROPERTY(...)``, ``UFUNCTION(...)`` — inside a class / struct
  body, attached as an attr on the next field or method.
- ``GENERATED_BODY()`` family — silently skipped. They're UHT
  (Unreal Header Tool) markers with no semantic content for an
  outline reader.

This is opt-out only by virtue of writing non-UE code (the macros
simply don't appear). The list is restricted to the canonical UE
macro names — other macros (Qt's ``Q_OBJECT``, generic
``DECLARE_DELEGATE_*``, app-specific declarations) parse as plain
function-call expressions without being absorbed, so they remain
visible in the outline as separate lines instead of silently
disappearing.

Note on ``*_API`` DLL-export macros (``ENGINE_API``, ``GAME_API``,
…): without preprocessor expansion tree-sitter mis-parses
``class GAME_API AMyActor`` as a function definition. The adapter
does not work around this. In typical UE projects the macro
expands to ``__declspec(dllexport)`` / empty when the build
system pre-processes; when running ast-outline against bare
source the simplest workaround is to drop the macro before
parsing or accept that those classes won't outline cleanly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_cpp as tscpp

from .base import count_parse_errors
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_OPERATOR,
    KIND_STRUCT,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tscpp.language())
_PARSER = Parser(_LANGUAGE)


# UHT body markers in regex form, alternation longest-first so prefixes
# don't shadow longer names (e.g. ``GENERATED_BODY`` must not match
# ``GENERATED_USTRUCT_BODY``).
_UE_BODY_MARKER_NAMES = (
    "GENERATED_USTRUCT_BODY",
    "GENERATED_UCLASS_BODY",
    "GENERATED_UINTERFACE_BODY",
    "GENERATED_IINTERFACE_BODY",
    "GENERATED_BODY_LEGACY",
    "GENERATED_BODY",
)
_UE_BODY_MARKER_RE = re.compile(
    rb"\b(?:" + b"|".join(n.encode("ascii") for n in _UE_BODY_MARKER_NAMES) + rb")\s*\(",
)


def _strip_ue_body_markers(src: bytes) -> bytes:
    """Replace every ``GENERATED_BODY()``-family invocation in ``src``
    with same-length whitespace, preserving newlines so byte offsets
    and line numbers stay identical to the original source.

    These macros are the main reason tree-sitter mis-parses UE
    headers — they sit inside class bodies as bare identifier(...)
    statements with no trailing semicolon, which the C++ grammar can't
    reconcile and which causes the parser to merge the rest of the
    file into a single ERROR subtree. Stripping them up-front makes
    every subsequent declaration parse cleanly.

    The replacement is byte-precise: each character that was part of
    the macro becomes a space except ``\\n``, which is left intact.
    `show` therefore returns the original source verbatim (the strip
    is invisible to anything that reads `path.read_bytes()` directly),
    and line/byte numbers in the IR continue to refer to the same
    positions an editor would highlight.
    """
    if b"GENERATED_" not in src:
        return src
    out = bytearray(src)
    pos = 0
    while True:
        m = _UE_BODY_MARKER_RE.search(out, pos)
        if m is None:
            break
        depth = 1
        i = m.end()
        n = len(out)
        while i < n and depth > 0:
            ch = out[i]
            if ch == 0x28:  # (
                depth += 1
            elif ch == 0x29:  # )
                depth -= 1
            i += 1
        # If we hit EOF without finding the closing paren, the file
        # is truncated mid-macro — leave the original bytes alone so
        # the partial source still reaches the parser, rather than
        # blanking the rest of the file and erasing recoverable
        # declarations preceding the truncation point.
        if depth == 0:
            for j in range(m.start(), i):
                if out[j] != 0x0A:  # \n
                    out[j] = 0x20  # space
        pos = i
    return bytes(out)


_TYPE_NODE_KIND = {
    "class_specifier": KIND_CLASS,
    "struct_specifier": KIND_STRUCT,
    "union_specifier": KIND_STRUCT,
    "enum_specifier": KIND_ENUM,
}

_TYPE_NATIVE_KEYWORD = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "union",
    "enum_specifier": "enum",
}


# Unreal Engine reflection macros that decorate a *type* — captured as
# an attr on the next type declaration in the same scope.
_UE_TYPE_MACROS = frozenset({
    "UCLASS", "USTRUCT", "UENUM", "UINTERFACE", "UDELEGATE",
})

# Unreal Engine reflection macros that decorate a *member* (field or
# method) — captured as an attr on the next member declaration.
_UE_MEMBER_MACROS = frozenset({
    "UPROPERTY", "UFUNCTION",
})

# Unreal Header Tool body markers — silently dropped from the outline.
# They generate boilerplate but contribute no signal a reader needs.
_UE_BODY_MARKERS = frozenset({
    "GENERATED_BODY",
    "GENERATED_USTRUCT_BODY",
    "GENERATED_UCLASS_BODY",
    "GENERATED_UINTERFACE_BODY",
    "GENERATED_IINTERFACE_BODY",
    "GENERATED_BODY_LEGACY",
})


class CppAdapter:
    language_name = "cpp"
    display_name = "C++"
    extensions = {
        ".cpp", ".cc", ".cxx", ".c++",
        ".h", ".hpp", ".hh", ".hxx", ".h++",
        ".ipp", ".tpp", ".inl",
        ".cppm", ".ixx",
    }
    definition_keywords = frozenset({
        "class", "struct", "enum", "union", "namespace",
    })
    comment_line_prefixes = ('//',)
    import_line_prefixes = ('#include ', '#import ')
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        # Note: ``import_regions`` is populated below via piggyback on
        # ``_collect_imports``. Includes ``#include`` (already in the
        # imports list) AND ``using_directive`` / ``using_declaration``
        # (which are NOT in the imports list to preserve --imports
        # header-only behavior, but ARE imports for grep classification:
        # `using namespace std;` and `using std::vector;` bring names
        # into scope). ``alias_declaration`` (`using A = B;`) is
        # deliberately excluded — it's a type alias, not an import.
        # AST-level distinction is the only reliable way: line-prefix
        # would lump all three under ``using ``.
        # Strip Unreal Header Tool body markers from the source before
        # parsing. Without this, the missing-semicolon convention of
        # `GENERATED_BODY()` confuses tree-sitter into treating the
        # surrounding class body as malformed and bundling everything
        # that follows into a single ERROR subtree, which would hide
        # every type defined after the first UE class. The strip is
        # length-preserving (replaced with spaces, newlines kept) so
        # byte offsets and line numbers stay aligned with the original
        # source — `show` and line ranges remain accurate.
        cleaned = _strip_ue_body_markers(src)
        tree = _PARSER.parse(cleaned)
        declarations: list[Declaration] = []
        _walk_top(tree.root_node, cleaned, declarations)
        imports: list[str] = []
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, cleaned, imports, import_regions)
        import_regions.sort()
        # tree-sitter inserts a synthetic MISSING `;` after every UE
        # reflection macro invocation (`UCLASS(...)`, `UPROPERTY(...)`,
        # `GENERATED_BODY()`, etc.) — by spec the parser expects
        # statement terminators that the macro convention omits.
        # Those are not real syntax errors in UE-style code, so we
        # subtract the recognised macro count from the parser's
        # tally to avoid flagging valid UE headers as broken.
        error_count = max(
            0,
            count_parse_errors(tree.root_node)
            - _count_ue_macro_invocations(tree.root_node, cleaned),
        )
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=declarations,
            error_count=error_count,
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
    """Collect ``#include`` directives at file scope. Each entry is the
    full source-true line including the ``#include`` keyword and the
    ``<…>``/``"…"`` form, so the agent reads exactly what the file
    declared (``#include <vector>``, ``#include "myheader.h"``).
    Other preprocessor directives are not collected — ``#define`` is a
    macro definition, not a dependency.

    If ``regions`` is supplied, also appends byte ranges of:
      - ``preproc_include``     — same as the imports list
      - ``using_directive``     — ``using namespace std;``
      - ``using_declaration``   — ``using std::vector;``
    These three are imports for the grep classifier (they bring names
    into scope). ``alias_declaration`` (``using A = B;``) is
    deliberately NOT collected — it's a type alias, not an import.
    Tree-sitter's distinct node types let us discriminate cleanly;
    a line-prefix approach (``startswith("using ")``) would lump all
    three under ``[import]`` and misclassify type aliases.
    """
    for child in root.named_children:
        kind = child.type
        if kind == "preproc_include":
            text = _collapse_ws(_text(child, src))
            if text:
                out.append(text)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))
        elif kind in ("using_directive", "using_declaration") and regions is not None:
            # Region-only — we do NOT add to the imports list. The
            # imports list keeps its existing header-only scope
            # (#include directives) so --imports output stays stable;
            # `using` lines are imports for grep classification only.
            regions.append((child.start_byte, child.end_byte))


# --- Walk -----------------------------------------------------------------


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    pending_attrs: list[str] = []
    for child in node.named_children:
        if child.type == "linkage_specification":
            _expand_linkage_children(child, src, out, pending_attrs)
            continue
        attr_text = _ue_type_macro_text(child, src)
        if attr_text is not None:
            pending_attrs.append(attr_text)
            continue
        decl = _convert_top_level(child, src)
        if decl is not None:
            if pending_attrs:
                decl.attrs = list(pending_attrs) + decl.attrs
                pending_attrs.clear()
            out.append(decl)


def _convert_top_level(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert a top-level (file or namespace scope) AST node to a
    Declaration. Returns None for nodes that don't carry a structural
    declaration we want to surface (preprocessor directives, raw
    statements, comments, …)."""
    kind = node.type
    if kind == "namespace_definition":
        return _ns_to_decl(node, src)
    if kind in _TYPE_NODE_KIND:
        return _type_to_decl(node, src, parent_default_visibility="")
    if kind == "template_declaration":
        return _template_to_decl(node, src, in_class=False, parent_default_visibility="")
    if kind == "function_definition":
        return _free_function_to_decl(node, src)
    if kind == "declaration":
        # A `declaration` at file/namespace scope can be a forward
        # function declaration, a global variable, a using-decl, etc.
        # We only surface function declarations — globals as fields
        # would inflate noise and the most common "global" in C++ is
        # actually `constexpr`/`const` constants which read fine when
        # skipped from the outline.
        if _has_function_declarator(node):
            return _free_function_to_decl(node, src)
        return None
    return None


def _expand_linkage_children(
    node: Node, src: bytes, out: list[Declaration], pending_attrs: list[str]
) -> None:
    """``extern "C" { … }`` introduces a linkage-specification scope
    that doesn't add a logical container — its members belong to the
    enclosing namespace / file. Walk its body and emit each child at
    the parent's level so C-linkage functions surface alongside
    regular declarations."""
    body = next(
        (c for c in node.named_children if c.type == "declaration_list"),
        None,
    )
    if body is None:
        # Single-declaration form: `extern "C" void foo();` —
        # the declaration is a sibling, not under a list.
        for c in node.named_children:
            if c.type == "string_literal":
                continue
            attr_text = _ue_type_macro_text(c, src)
            if attr_text is not None:
                pending_attrs.append(attr_text)
                continue
            decl = _convert_top_level(c, src)
            if decl is not None:
                if pending_attrs:
                    decl.attrs = list(pending_attrs) + decl.attrs
                    pending_attrs.clear()
                out.append(decl)
        return
    for c in body.named_children:
        attr_text = _ue_type_macro_text(c, src)
        if attr_text is not None:
            pending_attrs.append(attr_text)
            continue
        decl = _convert_top_level(c, src)
        if decl is not None:
            if pending_attrs:
                decl.attrs = list(pending_attrs) + decl.attrs
                pending_attrs.clear()
            out.append(decl)


# --- Namespaces -----------------------------------------------------------


def _ns_to_decl(node: Node, src: bytes) -> Declaration:
    """Build a namespace Declaration, collapsing single-child
    ``namespace a { namespace b { … } }`` chains into one
    ``namespace a::b`` so the outline matches the C++17
    ``namespace a::b { … }`` rendering. The collapse stops as soon
    as a level holds more than one structural child or any non-namespace
    declaration — those levels remain as nested namespaces in the IR.
    """
    name = _ns_name(node, src)
    body = _ns_body(node)
    children: list[Declaration] = []

    if body is not None:
        # Try to collapse a chain of single-namespace bodies.
        # Only collapse when the level holds nothing BUT one inner
        # namespace — siblings of any kind (other types, free
        # functions, using-decls) mean the level carries content the
        # outline must preserve, so the collapse stops there.
        chain = [name]
        current_node = node
        current_body = body
        while True:
            named = [
                c for c in current_body.named_children
                if c.type != "comment"
            ]
            if (
                len(named) == 1
                and named[0].type == "namespace_definition"
            ):
                inner = named[0]
                chain.append(_ns_name(inner, src))
                current_node = inner
                inner_body = _ns_body(inner)
                if inner_body is None:
                    current_body = None
                    break
                current_body = inner_body
                continue
            break

        # If we collapsed at all, the resulting namespace is the chain
        # joined with `::` and its children come from the deepest body.
        collapsed_name = "::".join(chain)
        if current_body is not None:
            pending_attrs: list[str] = []
            for c in current_body.named_children:
                if c.type == "linkage_specification":
                    _expand_linkage_children(c, src, children, pending_attrs)
                    continue
                attr_text = _ue_type_macro_text(c, src)
                if attr_text is not None:
                    pending_attrs.append(attr_text)
                    continue
                child_decl = _convert_top_level(c, src)
                if child_decl is not None:
                    if pending_attrs:
                        child_decl.attrs = list(pending_attrs) + child_decl.attrs
                        pending_attrs.clear()
                    children.append(child_decl)
        # End coordinates take from the OUTERMOST namespace — that's
        # what the user sees as the syntactic span of the namespace
        # block in source.
        return Declaration(
            kind=KIND_NAMESPACE,
            name=collapsed_name,
            signature=f"namespace {collapsed_name}",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            children=children,
        )

    # No body — namespace declared with a missing body (parse error).
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"namespace {name}",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# Top-level node types we consider "structural" — i.e. nodes that
# would show up in the outline as their own declaration line. Used by
# the namespace-collapse heuristic to decide whether a namespace level
# is "purely a passthrough" (collapsible) or holds real content.
_STRUCTURAL_TOP_LEVEL_KINDS = frozenset({
    "namespace_definition",
    "class_specifier",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "template_declaration",
    "function_definition",
    "declaration",
})


def _ns_name(node: Node, src: bytes) -> str:
    """Render a namespace's display name.

    Three shapes:
    - ``namespace foo::bar { … }`` (C++17) — child is
      ``nested_namespace_specifier`` containing several
      ``namespace_identifier`` parts. Joined with ``::``.
    - ``namespace foo { … }`` — single ``namespace_identifier`` child.
    - ``namespace { … }`` — no identifier child → ``<anonymous>``.

    An ``inline`` keyword (anonymous child token, present for
    ``inline namespace v1 { … }``) is preserved as a name prefix
    so the rendered line reads ``namespace inline v1`` —
    keeps the inline marker visible without needing a separate IR field.
    """
    is_inline = any(
        c.type == "inline" for c in node.children
    )
    nested = next(
        (c for c in node.named_children if c.type == "nested_namespace_specifier"),
        None,
    )
    if nested is not None:
        parts = [
            _text(c, src)
            for c in nested.named_children
            if c.type == "namespace_identifier"
        ]
        name = "::".join(parts) if parts else "<anonymous>"
    else:
        ident = next(
            (c for c in node.named_children if c.type == "namespace_identifier"),
            None,
        )
        name = _text(ident, src) if ident is not None else "<anonymous>"
    if is_inline:
        return f"inline {name}"
    return name


def _ns_body(node: Node) -> Optional[Node]:
    return next(
        (c for c in node.named_children if c.type == "declaration_list"),
        None,
    )


# --- Types ----------------------------------------------------------------


def _type_to_decl(
    node: Node,
    src: bytes,
    *,
    parent_default_visibility: str,
    template_prefix: str = "",
) -> Declaration:
    """Convert class/struct/union/enum to a Declaration, recursing into
    its body to collect members. ``template_prefix`` is the rendered
    ``template<…>`` header when the type is wrapped in a
    ``template_declaration`` — empty otherwise.

    ``parent_default_visibility`` is reserved for future use (member of
    a struct vs a class); the type itself is rendered with whatever
    keyword it used in source (``class`` / ``struct`` / ``union``).
    """
    kind = _TYPE_NODE_KIND[node.type]
    native_keyword = _TYPE_NATIVE_KEYWORD[node.type]
    name = _type_name(node, src)

    bases = _base_types(node, src) if node.type != "enum_specifier" else []
    signature = _type_signature(node, src, template_prefix=template_prefix)

    children: list[Declaration] = []
    body = _type_body(node)
    if body is not None:
        if node.type == "enum_specifier":
            for c in body.named_children:
                if c.type == "enumerator":
                    children.append(_enumerator_to_decl(c, src))
        else:
            default_vis = "public" if node.type in ("struct_specifier", "union_specifier") else "private"
            current_vis = default_vis
            pending_attrs: list[str] = []
            for c in body.named_children:
                if c.type == "access_specifier":
                    current_vis = _text(c, src).strip()
                    continue
                if _is_ue_body_marker(c, src):
                    continue
                attr_text = _ue_member_macro_text(c, src)
                if attr_text is not None:
                    pending_attrs.append(attr_text)
                    continue
                member = _convert_class_member(c, src, current_vis)
                if member is not None:
                    if pending_attrs:
                        member.attrs = list(pending_attrs) + member.attrs
                        pending_attrs.clear()
                    children.append(member)

    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        bases=bases,
        native_kind=native_keyword,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=children,
    )


def _type_name(node: Node, src: bytes) -> str:
    """The type's identifier. ``class A::B::C { … }`` is a valid
    out-of-namespace definition shape — for those we keep the qualified
    form (``A::B::C``) so the relationship to the declaring scope stays
    visible in the outline."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _text(name_node, src).strip()
    for c in node.named_children:
        if c.type in ("type_identifier", "qualified_identifier"):
            return _text(c, src).strip()
    return "?"


def _type_body(node: Node) -> Optional[Node]:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for c in node.named_children:
        if c.type in ("field_declaration_list", "enumerator_list"):
            return c
    return None


def _type_signature(node: Node, src: bytes, *, template_prefix: str = "") -> str:
    body = _type_body(node)
    start = node.start_byte
    end = body.start_byte if body is not None else node.end_byte
    text = src[start:end].decode("utf8", errors="replace")
    text = _collapse_ws(text).rstrip(" {").strip()
    if template_prefix:
        return f"{template_prefix} {text}".strip()
    return text


def _base_types(type_node: Node, src: bytes) -> list[str]:
    base = next(
        (c for c in type_node.named_children if c.type == "base_class_clause"),
        None,
    )
    if base is None:
        return []
    out: list[str] = []
    pending_access = ""
    pending_virtual = False
    for child in base.children:
        t = child.type
        if t == "access_specifier":
            pending_access = _text(child, src).strip()
        elif t == "virtual":
            pending_virtual = True
        elif t in ("type_identifier", "qualified_identifier", "template_type"):
            name = _collapse_ws(_text(child, src))
            parts = []
            if pending_access:
                parts.append(pending_access)
            if pending_virtual:
                parts.append("virtual")
            parts.append(name)
            out.append(" ".join(parts))
            pending_access = ""
            pending_virtual = False
    return out


# --- Class members --------------------------------------------------------


def _convert_class_member(node: Node, src: bytes, visibility: str) -> Optional[Declaration]:
    kind = node.type
    if kind in _TYPE_NODE_KIND:
        decl = _type_to_decl(node, src, parent_default_visibility=visibility)
        decl.visibility = visibility
        return decl
    if kind == "template_declaration":
        return _template_to_decl(node, src, in_class=True, parent_default_visibility=visibility)
    if kind == "function_definition":
        decl = _function_definition_to_decl(node, src, in_class=True)
        if decl is not None:
            decl.visibility = visibility
        return decl
    if kind == "field_declaration":
        nested_type = next(
            (c for c in node.named_children if c.type in _TYPE_NODE_KIND),
            None,
        )
        if nested_type is not None:
            decl = _type_to_decl(nested_type, src, parent_default_visibility=visibility)
            decl.visibility = visibility
            return decl
        return _field_declaration_to_decl(node, src, visibility)
    if kind == "declaration":
        # Member-level forward declarations: regular function-shaped
        # (`virtual void foo();`) or conversion operators
        # (`explicit operator bool() const;` — the latter lives under
        # an `operator_cast` node, not a `function_declarator`).
        cast_node = next(
            (c for c in node.named_children if c.type == "operator_cast"), None
        )
        if cast_node is not None:
            decl = _operator_cast_to_decl(node, cast_node, src)
            if decl is not None:
                decl.visibility = visibility
            return decl
        if _has_function_declarator(node):
            decl = _free_function_to_decl(node, src, in_class=True)
            if decl is not None:
                decl.visibility = visibility
            return decl
        return None
    if kind == "friend_declaration":
        return None
    return None


def _field_declaration_to_decl(
    node: Node, src: bytes, visibility: str
) -> Optional[Declaration]:
    """A C++ ``field_declaration`` is overloaded — it represents data
    members, non-defining method declarations, and (in some grammar
    versions) conversion operators. Triage in priority order:
    ``operator_cast`` child → conversion operator;
    ``function_declarator`` reachable → method;
    otherwise → data field.
    """
    cast_node = next(
        (c for c in node.named_children if c.type == "operator_cast"),
        None,
    )
    if cast_node is not None:
        decl = _operator_cast_to_decl(node, cast_node, src)
        if decl is not None:
            decl.visibility = visibility
        return decl
    fdecl = _find_function_declarator(node)
    if fdecl is not None:
        decl = _function_declarator_to_decl(node, fdecl, src, in_class=True)
        if decl is not None:
            decl.visibility = visibility
        return decl
    name = _field_data_name(node, src)
    if not name:
        return None
    signature = _signature_text(node, src)
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=signature,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _field_data_name(node: Node, src: bytes) -> Optional[str]:
    """Pull the identifier out of a data ``field_declaration``. Walks
    through declarator wrappers (``array_declarator``, pointer/ref,
    ``init_declarator``) until it finds the bare identifier."""
    for c in node.named_children:
        if c.type == "field_identifier":
            return _text(c, src)
        if c.type in ("array_declarator", "init_declarator", "pointer_declarator", "reference_declarator"):
            inner = _innermost_identifier(c, src)
            if inner:
                return inner
    return None


def _innermost_identifier(node: Node, src: bytes) -> Optional[str]:
    for c in node.named_children:
        if c.type in ("identifier", "field_identifier"):
            return _text(c, src)
        if c.type in ("array_declarator", "init_declarator", "pointer_declarator", "reference_declarator", "function_declarator"):
            r = _innermost_identifier(c, src)
            if r:
                return r
    return None


# --- Functions ------------------------------------------------------------


def _free_function_to_decl(
    node: Node, src: bytes, *, in_class: bool = False
) -> Optional[Declaration]:
    """Build a Declaration for a ``function_definition`` or function-
    shaped ``declaration``. Used both for free functions (top level)
    and for member methods declared without a body."""
    fdecl = _find_function_declarator(node)
    if fdecl is None:
        return None
    return _function_declarator_to_decl(node, fdecl, src, in_class=in_class)


def _function_definition_to_decl(
    node: Node, src: bytes, *, in_class: bool
) -> Optional[Declaration]:
    return _free_function_to_decl(node, src, in_class=in_class)


def _function_declarator_to_decl(
    outer: Node, fdecl: Node, src: bytes, *, in_class: bool
) -> Optional[Declaration]:
    name, kind = _callable_name_and_kind(fdecl, src, in_class=in_class)
    if not name:
        return None
    signature = _signature_text(outer, src)
    return Declaration(
        kind=kind,
        name=name,
        signature=signature,
        start_line=outer.start_point[0] + 1,
        end_line=outer.end_point[0] + 1,
        start_byte=outer.start_byte,
        end_byte=outer.end_byte,
    )


def _callable_name_and_kind(
    fdecl: Node, src: bytes, *, in_class: bool
) -> tuple[Optional[str], str]:
    """Extract a callable's name + kind from its ``function_declarator``.

    The declarator's named child carries the name shape:
    - ``identifier`` → plain function / method (or ctor when name == enclosing class)
    - ``destructor_name`` → KIND_DTOR
    - ``operator_name`` → KIND_OPERATOR
    - ``operator_cast`` → KIND_OPERATOR (e.g. ``operator bool``)
    - ``qualified_identifier`` → out-of-class definition (``Widget::draw``);
      we keep the qualified form as the name and classify by the trailing
      identifier shape.
    - ``field_identifier`` → method declared inside class via
      ``ReturnType& method(args)`` shape, where the parser puts the
      field identifier inside the declarator chain.
    """
    name_child = _function_declarator_name(fdecl)
    if name_child is None:
        return None, KIND_METHOD if in_class else KIND_FUNCTION

    nt = name_child.type
    if nt == "destructor_name":
        return _text(name_child, src), KIND_DTOR
    if nt == "operator_name":
        return _text(name_child, src), KIND_OPERATOR
    if nt == "operator_cast":
        # `operator bool() const` — name reads `operator <type>`. Use
        # the same text-slice strategy as `_operator_cast_to_decl` so
        # multi-token return types are preserved.
        declarator = next(
            (c for c in name_child.named_children if c.type == "abstract_function_declarator"),
            None,
        )
        type_end = declarator.start_byte if declarator is not None else name_child.end_byte
        type_text = src[name_child.start_byte:type_end].decode("utf8", errors="replace").strip()
        if type_text.startswith("operator"):
            type_text = type_text[len("operator"):].strip()
        type_text = _collapse_ws(type_text) or "?"
        return f"operator {type_text}".strip(), KIND_OPERATOR
    if nt == "qualified_identifier":
        # Out-of-class def: the name carries the full `Outer::method` form.
        # Classify by the trailing token.
        full = _collapse_ws(_text(name_child, src))
        last = full.rsplit("::", 1)[-1]
        if last.startswith("~"):
            return full, KIND_DTOR
        if last.startswith("operator"):
            return full, KIND_OPERATOR
        # Heuristic: if the qualifier and the trailing identifier match,
        # it's a constructor (`Foo::Foo`).
        parts = full.split("::")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            return full, KIND_CTOR
        return full, KIND_METHOD if in_class else KIND_FUNCTION
    if nt in ("identifier", "field_identifier"):
        name = _text(name_child, src)
        if in_class:
            # Constructor heuristic: identifier matches the enclosing
            # class's name. We don't have that name here without a walk,
            # so let the caller decide — but the common case is plain
            # methods. Constructor classification is refined below
            # using `_classify_in_class_name`.
            kind = _classify_in_class_name(fdecl, name)
            return name, kind
        return name, KIND_FUNCTION
    return None, KIND_METHOD if in_class else KIND_FUNCTION


def _classify_in_class_name(fdecl: Node, name: str) -> str:
    """Walk up from a function_declarator to the enclosing
    class/struct/union and check whether the function's name matches
    the type's name — that's a constructor. Anything else stays a
    method."""
    cur: Optional[Node] = fdecl
    while cur is not None:
        if cur.type in ("class_specifier", "struct_specifier", "union_specifier"):
            name_node = cur.child_by_field_name("name") or next(
                (c for c in cur.named_children if c.type == "type_identifier"),
                None,
            )
            if name_node is not None:
                if _node_text_eq(name_node, name):
                    return KIND_CTOR
            return KIND_METHOD
        cur = cur.parent
    return KIND_METHOD


def _function_declarator_name(fdecl: Node) -> Optional[Node]:
    declarator = fdecl.child_by_field_name("declarator")
    if declarator is not None:
        return declarator
    for c in fdecl.named_children:
        if c.type in (
            "identifier",
            "field_identifier",
            "destructor_name",
            "operator_name",
            "operator_cast",
            "qualified_identifier",
        ):
            return c
    return None


def _has_function_declarator(node: Node) -> bool:
    return _find_function_declarator(node) is not None


def _find_function_declarator(node: Node) -> Optional[Node]:
    """Locate the ``function_declarator`` inside a declarator chain.

    A C++ declaration's declarator is recursively wrapped — pointers,
    references, arrays all add a layer. We descend through every
    wrapper kind we expect to encounter; anything else stops the
    search."""
    for c in node.named_children:
        if c.type == "function_declarator":
            return c
        if c.type in (
            "pointer_declarator",
            "reference_declarator",
            "init_declarator",
            "parenthesized_declarator",
            "array_declarator",
        ):
            inner = _find_function_declarator(c)
            if inner is not None:
                return inner
    return None


# --- Templates ------------------------------------------------------------


def _template_to_decl(
    node: Node, src: bytes, *, in_class: bool, parent_default_visibility: str
) -> Optional[Declaration]:
    """Unwrap ``template<…> <decl>`` — we surface the wrapped declaration
    with the template header injected as a signature prefix so an LLM
    sees ``template<typename T> class Foo`` on one line."""
    template_prefix = _template_prefix(node, src)
    inner = _template_inner(node)
    if inner is None:
        return None
    if inner.type in _TYPE_NODE_KIND:
        decl = _type_to_decl(
            inner,
            src,
            parent_default_visibility=parent_default_visibility,
            template_prefix=template_prefix,
        )
        # Outer template node spans more than the inner type — keep the
        # outer span on BOTH ends so `show` returns the template header
        # and the trailing `;` (the inner class_specifier ends at `}`,
        # the template_declaration one byte later at `;`).
        decl.start_line = node.start_point[0] + 1
        decl.start_byte = node.start_byte
        decl.end_line = node.end_point[0] + 1
        decl.end_byte = node.end_byte
        return decl
    if inner.type == "function_definition":
        decl = _free_function_to_decl(inner, src, in_class=in_class)
        if decl is not None:
            decl.signature = f"{template_prefix} {decl.signature}".strip()
            decl.start_line = node.start_point[0] + 1
            decl.start_byte = node.start_byte
            decl.end_line = node.end_point[0] + 1
            decl.end_byte = node.end_byte
        return decl
    if inner.type == "declaration":
        if _has_function_declarator(inner):
            decl = _free_function_to_decl(inner, src, in_class=in_class)
            if decl is not None:
                decl.signature = f"{template_prefix} {decl.signature}".strip()
                decl.start_line = node.start_point[0] + 1
                decl.start_byte = node.start_byte
                decl.end_line = node.end_point[0] + 1
                decl.end_byte = node.end_byte
            return decl
    if inner.type == "concept_definition":
        return _concept_to_decl(node, inner, src, template_prefix=template_prefix)
    return None


def _concept_to_decl(
    outer: Node, concept_node: Node, src: bytes, *, template_prefix: str
) -> Declaration:
    """C++20 ``template<typename T> concept Foo = …;`` — a named
    constraint, not a type and not a callable. Surfaced as KIND_FIELD
    with ``native_kind="concept"`` so the digest renders ``Foo
    [field]`` (the closest-fitting tag in the canonical kind set) and
    the full signature stays available in the outline. The whole
    declaration including the ``template<…>`` header lives in the
    signature so a reader sees the constraint's full contract."""
    name_node = next(
        (c for c in concept_node.named_children if c.type == "identifier"),
        None,
    )
    name = _text(name_node, src) if name_node is not None else "?"
    full_text = _collapse_ws(_text(outer, src)).rstrip(";").strip()
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=full_text,
        native_kind="concept",
        start_line=outer.start_point[0] + 1,
        end_line=outer.end_point[0] + 1,
        start_byte=outer.start_byte,
        end_byte=outer.end_byte,
    )


def _template_prefix(node: Node, src: bytes) -> str:
    plist = next(
        (c for c in node.named_children if c.type == "template_parameter_list"),
        None,
    )
    if plist is None:
        return ""
    return f"template{_collapse_ws(_text(plist, src))}"


def _template_inner(node: Node) -> Optional[Node]:
    for c in node.named_children:
        if c.type in (
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
            "function_definition",
            "declaration",
            "template_declaration",
            "concept_definition",
        ):
            return c
    return None


# --- Enumerators ----------------------------------------------------------


def _operator_cast_to_decl(
    outer: Node, cast_node: Node, src: bytes
) -> Optional[Declaration]:
    """Build a Declaration for a conversion operator
    (``operator bool()``, ``operator double()``,
    ``operator const Foo*()``). The ``operator_cast`` node holds the
    target type followed by an ``abstract_function_declarator`` — we
    take everything before the declarator as the type expression so
    multi-token return types (cv-qualifiers, pointers, references,
    qualified names) survive intact in the rendered name."""
    declarator = next(
        (c for c in cast_node.named_children if c.type == "abstract_function_declarator"),
        None,
    )
    type_end = declarator.start_byte if declarator is not None else cast_node.end_byte
    # Skip the leading `operator` keyword token in cast_node's source.
    type_text = src[cast_node.start_byte:type_end].decode("utf8", errors="replace")
    type_text = type_text.strip()
    if type_text.startswith("operator"):
        type_text = type_text[len("operator"):].strip()
    type_text = _collapse_ws(type_text) or "?"
    name = f"operator {type_text}".strip()
    return Declaration(
        kind=KIND_OPERATOR,
        name=name,
        signature=_signature_text(outer, src),
        start_line=outer.start_point[0] + 1,
        end_line=outer.end_point[0] + 1,
        start_byte=outer.start_byte,
        end_byte=outer.end_byte,
    )


def _enumerator_to_decl(node: Node, src: bytes) -> Declaration:
    name_node = next(
        (c for c in node.named_children if c.type == "identifier"),
        None,
    )
    name = _text(name_node, src) if name_node is not None else "?"
    return Declaration(
        kind=KIND_ENUM_MEMBER,
        name=name,
        signature=_collapse_ws(_text(node, src)),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Helpers --------------------------------------------------------------


def _signature_text(node: Node, src: bytes) -> str:
    """Render a member/function signature: source text up to (but not
    including) the body or initialiser. ``= default`` / ``= delete``
    method clauses are kept since they're part of the contract."""
    start = node.start_byte
    end = node.end_byte
    cut = None
    for c in node.children:
        if c.type in ("compound_statement", "field_declaration_list"):
            cut = c.start_byte
            break
        if c.type == "default_method_clause" or c.type == "delete_method_clause":
            cut = c.end_byte
    if cut is not None:
        end = cut
    text = src[start:end].decode("utf8", errors="replace")
    text = _collapse_ws(text)
    return text.rstrip(" ;{").strip()


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Optional[Node], src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")


# --- Unreal Engine macro detection ---------------------------------------


def _ue_type_macro_text(node: Node, src: bytes) -> Optional[str]:
    """If ``node`` looks like a UE type-decorator macro invocation
    (``UCLASS(...)``, ``USTRUCT(...)``, ``UENUM(...)``,
    ``UINTERFACE(...)``, ``UDELEGATE(...)``) at file or namespace
    scope, return its source-true text; otherwise None.

    The macro lives in the AST as either an ``expression_statement``
    wrapping a ``call_expression`` (the common form for top-level
    invocations) or a ``declaration`` whose function declarator is
    named after the macro (a parser fallback that fires when the
    invocation lacks a trailing semicolon)."""
    name = _macro_invocation_name(node, src)
    if name is None:
        return None
    if name not in _UE_TYPE_MACROS:
        return None
    return _collapse_ws(_text(node, src)).rstrip(";").strip()


def _ue_member_macro_text(node: Node, src: bytes) -> Optional[str]:
    """If ``node`` looks like a UE member-decorator macro invocation
    (``UPROPERTY(...)``, ``UFUNCTION(...)``) inside a class/struct
    body, return its source-true text; otherwise None.

    Inside a ``field_declaration_list`` the parser exposes the macro
    in a few different shapes depending on form:
    - ``field_declaration`` whose ``type_identifier`` matches the
      macro name and whose declarator is a ``parenthesized_declarator``
      holding the macro arguments.
    - ``declaration`` whose function declarator matches the macro
      (the no-semicolon-followed-by-real-decl shape).
    Both forms are recognised here so the next field/method picks
    up the macro as an attr regardless of which spelling the source
    used."""
    name = _macro_invocation_name(node, src)
    if name is None:
        return None
    if name not in _UE_MEMBER_MACROS:
        return None
    return _collapse_ws(_text(node, src)).rstrip(";").strip()


def _is_ue_body_marker(node: Node, src: bytes) -> bool:
    """True if ``node`` is a UHT body-marker like ``GENERATED_BODY()``.
    These get silently dropped — they're boilerplate with no signal
    for an outline reader."""
    name = _macro_invocation_name(node, src)
    if name is None:
        return False
    return name in _UE_BODY_MARKERS


def _count_ue_macro_invocations(root: Node, src: bytes) -> int:
    """Count UE reflection macro invocations in the contexts where
    they actually appear in real code — file scope, namespace bodies,
    and class/struct/union bodies. Each one corresponds to one
    synthetic MISSING ``;`` the C++ parser inserts after the macro,
    so the count is the right amount to subtract from the parser's
    error tally to avoid flagging valid UE headers as broken.

    Restricting to those scopes (rather than walking the whole tree)
    avoids over-counting macro-shaped expressions that appear inside
    function bodies / initialisers — those don't generate MISSING
    nodes anyway, and a user-defined function happening to be named
    ``UFUNCTION`` shouldn't inflate the suppression."""
    known = _UE_TYPE_MACROS | _UE_MEMBER_MACROS | _UE_BODY_MARKERS
    count = 0
    scope_kinds = (
        "translation_unit",
        "declaration_list",
        "field_declaration_list",
        "linkage_specification",
    )
    stack: list[Node] = [root]
    while stack:
        n = stack.pop()
        if n.type in scope_kinds:
            for child in n.named_children:
                if _macro_invocation_name(child, src) in known:
                    count += 1
                stack.append(child)
        elif n.type in (
            "namespace_definition",
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "template_declaration",
        ):
            stack.extend(n.named_children)
    return count


def _macro_invocation_name(node: Node, src: bytes) -> Optional[str]:
    """Extract the bare identifier name of a macro-shaped invocation,
    handling the multiple AST shapes tree-sitter produces for the same
    source-level construct.

    Recognised shapes:
    - ``expression_statement`` → ``call_expression`` →
      ``identifier`` (the canonical form for top-level invocations).
    - ``declaration`` → ``function_declarator`` whose declarator is
      an ``identifier`` (parser fallback when the macro lacks ``;``
      and is followed by another declaration).
    - ``field_declaration`` whose ``type_identifier`` is the macro
      name and whose declarator is a ``parenthesized_declarator``
      (member-scope form for UPROPERTY-shaped lines).
    Returns None if the node doesn't match any of these shapes."""
    if node.type == "expression_statement":
        for c in node.named_children:
            if c.type == "call_expression":
                fn = c.child_by_field_name("function")
                if fn is None:
                    fn = next(
                        (cc for cc in c.named_children if cc.type == "identifier"),
                        None,
                    )
                if fn is not None and fn.type == "identifier":
                    return _text(fn, src).strip()
        return None
    if node.type == "declaration":
        fdecl = next(
            (c for c in node.named_children if c.type == "function_declarator"),
            None,
        )
        if fdecl is not None:
            ident = next(
                (c for c in fdecl.named_children if c.type == "identifier"),
                None,
            )
            if ident is not None:
                return _text(ident, src).strip()
        return None
    if node.type == "field_declaration":
        type_node = next(
            (c for c in node.named_children if c.type == "type_identifier"),
            None,
        )
        paren = next(
            (c for c in node.named_children if c.type == "parenthesized_declarator"),
            None,
        )
        if type_node is not None and paren is not None:
            return _text(type_node, src).strip()
        return None
    return None


def _node_text_eq(node: Node, expected: str) -> bool:
    encoded = expected.encode("utf8")
    if node.end_byte - node.start_byte != len(encoded):
        return False
    # The src bytes aren't reachable from the Node, but in callers we
    # always have it — so compare via parser's `text` attribute on the
    # node itself, which tree-sitter populates.
    return bool(node.text == encoded)
