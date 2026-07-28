r"""PHP adapter — parses .php / .phtml / .php8 files via tree-sitter-php into
Declaration IR. Targets modern PHP (8.x) and earlier LTS lines (7.4).

Design notes (how PHP concepts map to the IR):

- `namespace_definition` (file-scoped, semicolon-style) → KIND_NAMESPACE
                                                         (absorbs trailing
                                                         sibling types like
                                                         the C# / Java
                                                         file-scoped namespace)
- `namespace_definition` (bracketed `namespace Foo { ... }`) → KIND_NAMESPACE
                                                              (children come
                                                              from its inner
                                                              `compound_statement`)
- `class_declaration`                  → KIND_CLASS  (regular / `abstract`
                                                     / `final` / `readonly`)
- `interface_declaration`              → KIND_INTERFACE
- `trait_declaration`                  → KIND_INTERFACE  (native_kind="trait";
                                                          mixin-style mapping
                                                          mirrors Scala / Rust
                                                          for cross-language
                                                          search uniformity)
- `enum_declaration`                   → KIND_ENUM  (PHP 8.1+, with optional
                                                    backed scalar type)
- `enum_case`                          → KIND_ENUM_MEMBER
- `function_definition` (top-level)    → KIND_FUNCTION
- `function_definition` (inside type)  → KIND_METHOD  (rare — PHP doesn't
                                                       allow this except via
                                                       trait composition,
                                                       handled defensively)
- `method_declaration`                 → KIND_METHOD,  except:
    `__construct`                      → KIND_CTOR
    `__destruct`                       → KIND_DTOR
- `property_declaration`               → KIND_FIELD  (one entry per
                                                     `property_element`
                                                     so `public $a, $b;`
                                                     surfaces both names —
                                                     same shape PHP would
                                                     report at runtime)
- `property_promotion_parameter`       → KIND_FIELD  (PHP 8.0+ ctor property
                                                     promotion — implicit
                                                     property declared via
                                                     `__construct(public ... $x)`,
                                                     surfaced as a field on
                                                     the enclosing type so
                                                     the outline shows them
                                                     even when the body is
                                                     empty)
- `const_declaration` (class member)   → KIND_FIELD  (class constant)
- `const_declaration` (top-level)      → KIND_FIELD  (file-level constant)

PHP-specific points:

- Property names lose their leading `$` in the IR: `private string $name;`
  → name="name". This matches how PHP source actually accesses the property
  (`$this->name`, not `$this->$name`) and lets symbol-search treat
  `User.name` like any other dotted path.
- `use_declaration` inside a class body declares trait composition
  (`use HasTimestamps;`) — these are skipped (they're imports of
  implementation, not declarations of new members).
- ``namespace_use_declaration`` (top-level ``use Foo\Bar;``,
  ``use function foo;``, ``use const FOO;``, grouped ``use Foo\{A, B};``)
  is emitted as imports.
- ``include`` / ``include_once`` / ``require`` / ``require_once``
  expressions are also emitted as imports — pre-Composer / WordPress /
  Drupal-7-style legacy code uses these as the *only* dependency
  mechanism, so an "imports = `use` only" view would show empty for
  every WP plugin file. Collection scope: top-level statements only
  (direct children of ``program``, plus the ``compound_statement``
  body of a bracketed namespace). We deliberately do **not** descend
  into ``if`` / ``else`` / ``try`` / ``switch`` / ``match`` / loop
  bodies, nor into function / method / class / closure bodies —
  conditional and runtime ``require``s are out of scope, matching how
  every other adapter handles conditional imports (Python
  ``try/except`` fallbacks, ``if TYPE_CHECKING`` blocks, and the
  like). One statement = one entry; the source-true expression text
  is preserved including computed paths
  (``require_once ABSPATH . 'wp-config.php'``).
- The skipped (conditional / runtime) include count is reported via
  ``ParseResult.conditional_imports_count``, which renderers expose
  as a ``[+ N conditional includes]`` suffix on the imports line.
  Counting them rather than listing them tells the agent
  "this file has additional dynamic dependencies — read it directly
  if you care" without misleading it into thinking all branches load
  every time.
  Grouped imports are expanded into one `use ...` entry per leaf so each
  `imports` string is a single source-true statement.
- Anonymous classes (`new class { ... }`) live inside expression nodes;
  the walker only descends into namespace bodies and class declaration
  lists, so anonymous classes are naturally skipped.

Visibility defaults (PHP spec):

- Top-level types / functions          → "public"  (no real concept of
                                                   non-public at top scope)
- Class members without modifier       → "public"
- Interface members                    → "public"  (always)
- Enum cases                           → "public"
- Class constants without modifier     → "public"  (PHP 7.1+ allows
                                                   modifiers on class
                                                   consts; absent ones
                                                   default to public)

Modifiers recognised: `abstract`, `final`, `readonly`, `static`, plus the
visibility tokens above. They appear as direct children of the declaration
node (`abstract_modifier`, `final_modifier`, `readonly_modifier`,
`static_modifier`, `visibility_modifier`).

Docs: PHPDoc `/** ... */` is a `comment` node whose text starts with `/**`.
Plain block comments (`/* ... */`) and `//` / `#` line comments don't
qualify and break the contiguous-leading-doc walk.

Attributes (PHP 8.0+ `#[Attr]` / `#[Attr(args)]`) are collected as `attrs`
and stripped from the rendered signature, the same way Java annotations and
Rust attribute macros are handled.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_php as tsp
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tsp.language_php())
_PARSER = Parser(_LANGUAGE)


_TYPE_NODE_KIND = {
    "class_declaration": KIND_CLASS,
    "interface_declaration": KIND_INTERFACE,
    "trait_declaration": KIND_INTERFACE,
    "enum_declaration": KIND_ENUM,
}


_TYPE_NATIVE_KIND = {
    "trait_declaration": "trait",
}


_MAGIC_CTOR_NAMES = {"__construct"}
_MAGIC_DTOR_NAMES = {"__destruct"}


class PhpAdapter:
    language_name = "php"
    display_name = "PHP"
    # `.php`  — universal source extension
    # `.phtml` — Zend / older convention for PHP-with-HTML templates
    # `.phps`  — PHP source-display files (server renders highlighted)
    # `.php8`  — occasionally used to flag PHP 8-only sources
    extensions = {".php", ".phtml", ".phps", ".php8"}
    definition_keywords = frozenset({
        "class", "interface", "trait", "function", "enum",
    })
    comment_line_prefixes = ('//', '#')
    import_line_prefixes = ('use ', 'require ', 'require_once ', 'include ', 'include_once ')
    render_family = "code"
    shebang_programs = frozenset({"php"})

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        declarations: list[Declaration] = []
        _walk_top(tree.root_node, src, declarations)
        imports: list[str] = []
        # Piggyback byte-range collection for grep's import classifier.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        import_regions.sort()
        # Count include/require nodes that live OUTSIDE the file's
        # static top level — i.e. inside a control-flow block (if/try/
        # switch/match/loop) or inside a function/method/closure body.
        # Reported as `conditional_imports_count` so renderers can
        # append `[+ N conditional includes]` to the imports line and
        # the agent isn't misled into thinking the file has no
        # dependencies. Top-level assignment-wrapped includes
        # (`$ok = require 'a.php';`) are NOT counted — they're
        # unconditional, just not surfaced as a static-import entry.
        conditional_includes = _count_conditional_includes(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=declarations,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_includes,
            import_regions=import_regions,
        )


# --- Imports --------------------------------------------------------------


# Tree-sitter-php node types for the four include flavours.
_INCLUDE_EXPR_NODES = frozenset({
    "include_expression",
    "include_once_expression",
    "require_expression",
    "require_once_expression",
})


def _collect_imports(
    root: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    r"""Emit `use` declarations and top-level `include` / `require`
    expressions, in source order.

    Scope: direct children of `program` plus the `compound_statement`
    body of a bracketed `namespace Foo { ... }`. We deliberately do
    NOT descend into control-flow blocks (`if`/`else`, `try/catch`,
    `switch`, `match`, loops) — conditional and runtime ``require``s
    are out of scope, the same way Python's ``try/except`` fallback
    imports and ``if TYPE_CHECKING`` blocks lose their condition in
    other adapters. Single-pass over `program` so a file that
    interleaves bracketed namespaces with outer top-level imports
    keeps source order intact.

    If ``regions`` is supplied, also appends each import declaration's
    byte range — covers ``use`` (single-line + group form) AND
    include/require expressions in all four flavours, for grep's
    classifier.
    """
    for child in root.named_children:
        t = child.type
        if t == "namespace_use_declaration":
            out.extend(_expand_use_declaration(child, src))
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))
        elif t == "expression_statement":
            _maybe_emit_include(child, src, out, regions)
        elif t == "namespace_definition":
            # Bracketed `namespace Foo { ... }` — descend into the body
            # at the same emit level. File-scoped `namespace Foo;` has
            # no `compound_statement` child, so this is a no-op for it
            # (its sibling imports are already handled by the outer
            # iteration).
            for cc in child.named_children:
                if cc.type == "compound_statement":
                    for ccc in cc.named_children:
                        ct = ccc.type
                        if ct == "namespace_use_declaration":
                            out.extend(_expand_use_declaration(ccc, src))
                            if regions is not None:
                                regions.append((ccc.start_byte, ccc.end_byte))
                        elif ct == "expression_statement":
                            _maybe_emit_include(ccc, src, out, regions)


def _maybe_emit_include(
    stmt: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    """Emit the include text iff the `expression_statement` directly
    wraps an include/require expression. Wrapped forms like
    `$x = require 'a';` are intentionally skipped — the statement is
    an assignment, not an import.

    If ``regions`` is supplied, also appends the inner expression's
    byte range when an include is emitted.
    """
    # `expression_statement` carries exactly one named child in
    # tree-sitter-php (the expression itself); guard with a `next` so
    # an empty statement degrades safely.
    inner = next(iter(stmt.named_children), None)
    if inner is not None and inner.type in _INCLUDE_EXPR_NODES:
        out.append(_collapse_ws(_text(inner, src)))
        if regions is not None:
            regions.append((inner.start_byte, inner.end_byte))


# AST node types that establish a conditional-or-runtime scope. An
# include expression with any of these on its parent chain is
# conditional/runtime — it doesn't run unconditionally at module load.
# Specific clause subtypes (`else_clause`, `case_statement`, `catch_clause`,
# `else_if_clause`) are NOT listed individually: their parent
# (`if_statement` / `switch_statement` / `try_statement`) sets the flag
# and child traversal inherits it.
_CONDITIONAL_OR_RUNTIME_SCOPES = frozenset({
    # Function / class bodies — runtime scope (the include only runs
    # when the function is invoked or the class instantiated).
    "function_definition",
    "method_declaration",
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
    "anonymous_function",
    "arrow_function",
    "anonymous_class",
    # Control flow — once inside, the include is conditional.
    "if_statement",
    "try_statement",
    "switch_statement",
    "match_expression",
    "while_statement",
    "do_statement",
    "for_statement",
    "foreach_statement",
})


def _count_conditional_includes(root: Node) -> int:
    """Count include/require expression nodes that live inside a
    conditional-or-runtime scope.

    A scope is entered the first time the walk hits any of
    `_CONDITIONAL_OR_RUNTIME_SCOPES` on the parent chain; once inside,
    every nested include is counted regardless of how deep. Top-level
    statements and top-level assignment-wrapped includes
    (`$ok = require 'a.php';`) are never counted — they execute
    unconditionally at module load even though we don't surface the
    latter as a static import (the statement IS an assignment, not
    an import).

    Iterative traversal with an explicit (node, in_scope) stack so
    deeply nested files (real WordPress sources reach 9000+ lines and
    several thousand AST nodes) stay well within Python recursion
    limits.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        if node.type in _INCLUDE_EXPR_NODES:
            if in_scope:
                count += 1
            # An include's argument is a path expression, not another
            # include — skip its subtree.
            continue
        new_in_scope = in_scope or node.type in _CONDITIONAL_OR_RUNTIME_SCOPES
        for c in node.children:
            stack.append((c, new_in_scope))
    return count


def _expand_use_declaration(node: Node, src: bytes) -> list[str]:
    """Return one or more source-true `use ...` strings.

    Shapes handled:
      use Foo\\Bar;                       → ["use Foo\\Bar"]
      use Foo\\Bar as Baz;                → ["use Foo\\Bar as Baz"]
      use function strlen;                → ["use function strlen"]
      use const PHP_INT_MAX;              → ["use const PHP_INT_MAX"]
      use Foo\\{A, B as Bb, function f};  → ["use Foo\\A", "use Foo\\B as Bb",
                                              "use function Foo\\f"]
    """
    # Detect optional `function` / `const` keyword right after `use`.
    # The keyword is an anonymous (non-named) child token, so we walk
    # all children, not just named_children.
    use_kind = ""  # "" | "function" | "const"
    for c in node.children:
        if c.type in ("function", "const") and c.start_byte > node.start_byte:
            use_kind = c.type
            break

    # Group form: `use Prefix\{...}`. The grammar emits the prefix as a
    # `namespace_name` direct child plus a `namespace_use_group`.
    group: Optional[Node] = None
    prefix: str = ""
    for c in node.named_children:
        if c.type == "namespace_use_group":
            group = c
        elif c.type == "namespace_name" and group is None:
            prefix = _text(c, src)

    results: list[str] = []
    if group is not None:
        for clause in group.named_children:
            if clause.type != "namespace_use_clause":
                continue
            results.append(
                _render_grouped_clause(clause, src, prefix=prefix, use_kind=use_kind)
            )
        return results

    # Non-group form: one or more `namespace_use_clause` siblings.
    for clause in node.named_children:
        if clause.type != "namespace_use_clause":
            continue
        results.append(_render_flat_clause(clause, src, use_kind=use_kind))
    return results


def _render_flat_clause(node: Node, src: bytes, *, use_kind: str) -> str:
    """A single `namespace_use_clause` rendered as one `use ...` statement.
    Preserves `... as Alias` if present.
    """
    text = _collapse_ws(_text(node, src)).rstrip(";").rstrip(",").strip()
    head = "use " + (f"{use_kind} " if use_kind else "")
    return head + text


def _render_grouped_clause(
    node: Node, src: bytes, *, prefix: str, use_kind: str
) -> str:
    """Inside `use Foo\\{...}` each clause may carry its own per-clause
    `function`/`const` keyword (mixed groups). The clause text reads as
    `Bar` or `Bar as Baz` (possibly with leading `function ` / `const `).
    Re-attach the prefix and any group-level `use_kind` at the front.
    """
    # Per-clause keyword wins over the group-level keyword.
    clause_kind = ""
    for c in node.children:
        if c.type in ("function", "const"):
            clause_kind = c.type
            break
    effective_kind = clause_kind or use_kind

    # Strip the leading `function ` / `const ` from the clause text since
    # we re-emit it ourselves at a controlled position.
    text = _collapse_ws(_text(node, src)).rstrip(",").rstrip(";").strip()
    for kw in ("function ", "const "):
        if text.startswith(kw):
            text = text[len(kw):]
            break

    head = "use " + (f"{effective_kind} " if effective_kind else "")
    if prefix:
        return f"{head}{prefix}\\{text}"
    return f"{head}{text}"


# --- Walk -----------------------------------------------------------------


def _walk_top(node: Node, src: bytes, out: list[Declaration]) -> None:
    """Top-level walk over `program` children.

    Two namespace shapes coexist in PHP:
      - File-scoped (`namespace Foo;`) — types follow as siblings; the
        namespace declaration absorbs every trailing top-level decl until
        EOF or another namespace declaration.
      - Bracketed (`namespace Foo { ... }`) — children live inside an inner
        `compound_statement`.

    Both produce a KIND_NAMESPACE node with its members nested inside.
    """
    current_ns: Optional[Declaration] = None
    for child in node.named_children:
        kind = child.type
        if kind == "namespace_definition":
            ns = _namespace_to_decl(child, src)
            if ns is not None:
                out.append(ns)
                # If the namespace is bracketed (has its own body), it's
                # self-contained and shouldn't absorb siblings — switch
                # `current_ns` back to None so siblings of THIS namespace
                # land at top level (or get absorbed by the next semicolon
                # namespace).
                current_ns = None if _has_bracketed_body(child) else ns
            continue
        decl = _top_decl(child, src)
        if decl is None:
            continue
        if current_ns is not None:
            current_ns.children.append(decl)
            current_ns.end_line = decl.end_line
            current_ns.end_byte = decl.end_byte
        else:
            out.append(decl)


def _has_bracketed_body(node: Node) -> bool:
    for c in node.named_children:
        if c.type == "compound_statement":
            return True
    return False


def _top_decl(node: Node, src: bytes) -> Optional[Declaration]:
    t = node.type
    if t in _TYPE_NODE_KIND:
        return _type_to_decl(node, src, parent_kind=None)
    if t == "function_definition":
        return _function_to_decl(node, src, parent_kind=None)
    if t == "const_declaration":
        return _const_to_decl(node, src, parent_kind=None)
    return None


def _namespace_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert a `namespace_definition` into a KIND_NAMESPACE.

    Shapes:
      `namespace Foo\\Bar;`        → name="Foo\\Bar", no body children yet
                                    (siblings will be absorbed by the
                                    walker).
      `namespace Foo { ... }`      → name="Foo", body's children are walked
                                    and added immediately so the namespace
                                    is self-contained.
      `namespace { ... }`          → unnamed (global) namespace block —
                                    represented with name="" and a generic
                                    signature.
    """
    name_node: Optional[Node] = None
    for c in node.named_children:
        if c.type == "namespace_name":
            name_node = c
            break
    name = _collapse_ws(_text(name_node, src)) if name_node is not None else ""
    signature = f"namespace {name}" if name else "namespace"

    children: list[Declaration] = []
    body: Optional[Node] = None
    for c in node.named_children:
        if c.type == "compound_statement":
            body = c
            break
    if body is not None:
        for cc in body.named_children:
            decl = _top_decl(cc, src)
            if decl is not None:
                children.append(decl)

    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=signature,
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=children,
    )


# --- Types (class / interface / trait / enum) ----------------------------


def _name_line(node: Node) -> int:
    """1-based line where the declaration's signature begins.

    PHP 8 attributes (`#[Attr]`) arrive as a leading `attribute_list`
    child and modifiers (`public` / `abstract` / `final` / `readonly` /
    `static`) as `*_modifier` children, so `node.start_point` lands on an
    attribute or modifier line when one precedes the declaration. The
    first child that is neither is the real head (the `class` /
    `function` / `const` keyword or the name), and the name token sits on
    that line. grep's def-classifier compares a match against it, so an
    attributed declaration is tagged [def] on its signature line, not on
    an attribute.
    """
    for c in node.children:
        if c.type != "attribute_list" and not c.type.endswith("_modifier"):
            return c.start_point[0] + 1
    return node.start_point[0] + 1


def _type_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Declaration:
    kind = _TYPE_NODE_KIND[node.type]
    native_kind = _TYPE_NATIVE_KIND.get(node.type, "")
    name = _field_text(node, "name", src) or "?"
    bases = _base_types(node, src)
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    visibility = "public"  # PHP types are effectively public
    signature = _type_signature(node, src)

    children: list[Declaration] = []
    promoted: list[Declaration] = []
    body = node.child_by_field_name("body")
    if body is not None:
        if node.type == "enum_declaration":
            for c in body.named_children:
                children.extend(_member_from_enum_body(c, src, parent_kind=kind))
                if c.type == "method_declaration" and _is_constructor(c, src):
                    promoted.extend(_promoted_properties(c, src))
        else:
            for c in body.named_children:
                children.extend(_member_from_class_body(c, src, parent_kind=kind))
                if c.type == "method_declaration" and _is_constructor(c, src):
                    promoted.extend(_promoted_properties(c, src))

    # PHP 8 constructor property promotion: pull promoted parameters out
    # of `__construct` and surface them as KIND_FIELD entries on the
    # type, so the outline shows them like ordinary properties (they
    # ARE ordinary properties at runtime). Mirrors how the Kotlin
    # adapter promotes primary-constructor `val`/`var` parameters into
    # implicit fields.
    children = promoted + children

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
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
        children=children,
    )


def _member_from_class_body(
    node: Node, src: bytes, *, parent_kind: str
) -> list[Declaration]:
    t = node.type
    if t in _TYPE_NODE_KIND:
        # Nested type — PHP doesn't allow these except via anonymous
        # classes inside expressions (which we never reach), but emit
        # defensively: if the parser produces one, surface it.
        return [_type_to_decl(node, src, parent_kind=parent_kind)]
    if t == "method_declaration":
        m = _method_to_decl(node, src)
        return [m] if m is not None else []
    if t == "function_definition":
        # Defensive: a free function inside a class body shouldn't happen,
        # but if it slips through (e.g. parser recovery), treat it as a
        # method.
        m = _function_to_decl(node, src, parent_kind=parent_kind)
        return [m] if m is not None else []
    if t == "property_declaration":
        return _properties_from_decl(node, src)
    if t == "const_declaration":
        d = _const_to_decl(node, src, parent_kind=parent_kind)
        return [d] if d is not None else []
    # use_declaration (trait usage), comment — skip
    return []


def _member_from_enum_body(
    node: Node, src: bytes, *, parent_kind: str
) -> list[Declaration]:
    if node.type == "enum_case":
        d = _enum_case_to_decl(node, src)
        return [d] if d is not None else []
    return _member_from_class_body(node, src, parent_kind=parent_kind)


# --- Methods / functions -------------------------------------------------


def _method_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name = _field_text(node, "name", src)
    if not name:
        return None
    if name in _MAGIC_CTOR_NAMES:
        kind = KIND_CTOR
    elif name in _MAGIC_DTOR_NAMES:
        kind = KIND_DTOR
    else:
        kind = KIND_METHOD
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    visibility = _member_visibility(node)
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


def _function_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    name = _field_text(node, "name", src)
    if not name:
        return None
    kind = KIND_METHOD if parent_kind is not None else KIND_FUNCTION
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    visibility = "public"
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


# --- Properties / constants / enum cases ---------------------------------


def _properties_from_decl(node: Node, src: bytes) -> list[Declaration]:
    """`public string $a, $b = "x";` carries multiple `property_element`
    children — emit one Declaration per element (each with the SAME
    visibility / type signature head) so symbol-search resolves both names.
    """
    visibility = _member_visibility(node)
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    signature_head = _property_signature_head(node, src)

    out: list[Declaration] = []
    first = True
    for c in node.named_children:
        if c.type != "property_element":
            continue
        var_node = c.child_by_field_name("name") or _first_child(c, "variable_name")
        if var_node is None:
            # Defensive: malformed property_element — try identifier child.
            for cc in c.named_children:
                if cc.type == "variable_name":
                    var_node = cc
                    break
        name = _variable_to_name(var_node, src) if var_node is not None else None
        if not name:
            continue
        sig = _collapse_ws(signature_head + " " + _text(c, src)).rstrip(",;").strip()
        # The first element's source slice covers the whole
        # `property_declaration` (so `show` returns modifiers + type +
        # the first variable's initializer); subsequent elements only
        # cover their own `property_element`. start_line / start_byte
        # are kept consistent with each other to avoid mismatched
        # coordinates on multi-line declarations.
        if first:
            start_line = node.start_point[0] + 1
            start_byte = node.start_byte
            doc_start = _resolved_doc_start(node, src)
            element_docs = docs
        else:
            start_line = c.start_point[0] + 1
            start_byte = c.start_byte
            doc_start = c.start_byte
            element_docs = []
        out.append(
            Declaration(
                kind=KIND_FIELD,
                name=name,
                signature=sig,
                attrs=attrs,
                docs=element_docs,
                visibility=visibility,
                start_line=start_line,
                end_line=c.end_point[0] + 1,
                start_byte=start_byte,
                end_byte=c.end_byte,
                doc_start_byte=doc_start,
            )
        )
        first = False
    return out


def _const_to_decl(
    node: Node, src: bytes, *, parent_kind: Optional[str]
) -> Optional[Declaration]:
    """`const FOO = 1;` (top-level) or `public const FOO = 1;` (class member).
    The first `const_element` wins — multi-const declarations
    (`const A = 1, B = 2;`) are rare and the first name remains the
    canonical anchor for symbol search, mirroring how the Java adapter
    handles multi-variable field declarations.
    """
    first_element: Optional[Node] = None
    for c in node.named_children:
        if c.type == "const_element":
            first_element = c
            break
    if first_element is None:
        return None
    name_node = first_element.child_by_field_name("name") or _first_child(
        first_element, "name"
    )
    if name_node is None:
        return None
    name = _text(name_node, src)
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    visibility = (
        _member_visibility(node) if parent_kind is not None else "public"
    )
    sig = _collapse_ws(_strip_leading_attributes(_text(node, src))).rstrip(";")
    return Declaration(
        kind=KIND_FIELD,
        name=name,
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


def _enum_case_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name = _field_text(node, "name", src)
    if not name:
        return None
    attrs = _attributes(node, src)
    docs = _phpdocs(node, src)
    sig = _collapse_ws(_strip_leading_attributes(_text(node, src))).rstrip(",").rstrip(";").rstrip()
    return Declaration(
        kind=KIND_ENUM_MEMBER,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        name_line=_name_line(node),
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_resolved_doc_start(node, src),
    )


# --- Constructor property promotion --------------------------------------


def _is_constructor(method_node: Node, src: bytes) -> bool:
    name = _field_text(method_node, "name", src)
    return name in _MAGIC_CTOR_NAMES


def _promoted_properties(method_node: Node, src: bytes) -> list[Declaration]:
    """Pull `property_promotion_parameter` children out of a constructor's
    `formal_parameters` and produce a KIND_FIELD per promoted parameter.
    """
    params = method_node.child_by_field_name("parameters")
    if params is None:
        return []
    out: list[Declaration] = []
    for p in params.named_children:
        if p.type != "property_promotion_parameter":
            continue
        var_node = _first_child(p, "variable_name")
        name = _variable_to_name(var_node, src)
        if not name:
            continue
        # Visibility on a promoted parameter is the explicit modifier
        # on the parameter itself; PHP requires one to be present, but
        # default to "public" defensively if it's somehow missing.
        visibility = "public"
        for c in p.children:
            if c.type == "visibility_modifier":
                tok = c.text.decode("utf8", errors="replace").strip()
                if tok in _VISIBILITY_TOKENS:
                    visibility = tok
                    break
        attrs = _attributes(p, src)
        sig = _collapse_ws(_strip_leading_attributes(_text(p, src))).rstrip(",")
        out.append(
            Declaration(
                kind=KIND_FIELD,
                name=name,
                signature=sig,
                attrs=attrs,
                visibility=visibility,
                start_line=p.start_point[0] + 1,
                end_line=p.end_point[0] + 1,
                start_byte=p.start_byte,
                end_byte=p.end_byte,
                # No leading doc on a ctor parameter — point at the
                # parameter itself; the default 0 would be a garbage
                # offset for JSON consumers reading the field directly.
                doc_start_byte=p.start_byte,
            )
        )
    return out


# --- Signatures ----------------------------------------------------------


def _type_signature(node: Node, src: bytes) -> str:
    """Slice from the start of the declaration up to (but not including)
    the body — covers attributes, modifiers, `class`/`interface`/`trait`/
    `enum` keyword, name, optional backed-enum type, `extends` /
    `implements`. Leading attributes are stripped from the rendered text.
    """
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_attributes(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _callable_signature(node: Node, src: bytes) -> str:
    """Slice up to the function body (`compound_statement`). Abstract
    methods have no body — fall back to the whole node and trim the
    trailing `;`.
    """
    body = node.child_by_field_name("body")
    cut = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:cut].decode("utf8", errors="replace")
    text = _strip_leading_attributes(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _property_signature_head(node: Node, src: bytes) -> str:
    """Render the modifiers + type prefix that precedes the property
    elements, without the elements themselves. E.g. for
    `public readonly ?string $a, $b = "x";` we want `public readonly ?string`.
    """
    end: Optional[int] = None
    for c in node.named_children:
        if c.type == "property_element":
            end = c.start_byte
            break
    if end is None:
        return ""
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_attributes(text)
    return _collapse_ws(text)


# --- Bases / heritage ----------------------------------------------------


def _base_types(node: Node, src: bytes) -> list[str]:
    """`extends Base` is a `base_clause`, `implements I1, I2` is a
    `class_interface_clause`. Both yield a flat list of base-type names.
    """
    out: list[str] = []
    for c in node.named_children:
        if c.type == "base_clause":
            for nc in c.named_children:
                t = _collapse_ws(_text(nc, src)).rstrip(",")
                if t:
                    out.append(t)
        elif c.type == "class_interface_clause":
            for nc in c.named_children:
                t = _collapse_ws(_text(nc, src)).rstrip(",")
                if t:
                    out.append(t)
    return out


# --- Modifiers / attributes / docs ---------------------------------------


_VISIBILITY_TOKENS = {"public", "protected", "private"}


def _member_visibility(node: Node) -> str:
    """Return the source-level visibility for a member node.

    Explicit `visibility_modifier` token wins. Otherwise falls back to
    `public` — the PHP default everywhere a member can appear (class,
    trait, interface, enum), so no parent-context branching is needed.
    """
    for c in node.children:
        if c.type == "visibility_modifier":
            token = c.text.decode("utf8", errors="replace").strip()
            if token in _VISIBILITY_TOKENS:
                return token
    return "public"


def _attributes(node: Node, src: bytes) -> list[str]:
    """Collect `#[Attr]` / `#[Attr(args)]` entries from any `attribute_list`
    children. PHP groups attributes inside `attribute_list > attribute_group >
    attribute`; the `attribute_group` node's source text already includes
    the `#[...]` delimiters, so we keep each group's text as-is and the
    digest reads source-true.
    """
    out: list[str] = []
    for c in node.named_children:
        if c.type != "attribute_list":
            continue
        for group in c.named_children:
            if group.type != "attribute_group":
                continue
            text = _collapse_ws(_text(group, src))
            if text:
                out.append(text)
    return out


def _phpdocs(node: Node, src: bytes) -> list[str]:
    """Contiguous preceding `/** ... */` PHPDoc blocks. `comment` nodes
    that don't start with `/**` (line comments, plain block comments)
    break the walk.
    """
    docs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "comment":
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
    while sib is not None and sib.type == "comment":
        if _text(sib, src).startswith("/**"):
            first = sib
            sib = sib.prev_sibling
        else:
            break
    return first.start_byte if first is not None else None


def _resolved_doc_start(node: Node, src: bytes) -> int:
    doc = _leading_doc_start_byte(node, src)
    return doc if doc is not None else node.start_byte


# --- Attribute stripping (#[Attr], #[Attr(args)]) ------------------------


def _strip_leading_attributes(text: str) -> str:
    """Drop one or more leading `#[...]` attribute groups from rendered
    signature text, plus any whitespace between them. Brackets are
    balanced and string literals (`"..."` / `'...'`) are skipped so an
    attribute argument like `#[Route("/path[opt]")]` is handled cleanly.
    """
    s = text.lstrip()
    while s.startswith("#["):
        i = 2
        depth = 1
        while i < len(s) and depth > 0:
            ch = s[i]
            if ch in ("\"", "'"):
                i = _skip_string_literal(s, i, ch)
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            i += 1
        s = s[i:].lstrip()
    return s


def _skip_string_literal(s: str, i: int, quote: str) -> int:
    """Advance past `"..."` or `'...'`, honouring `\\` escapes. If the
    literal is unterminated, return `len(s)` so the outer scanner exits.
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


# --- Helpers -------------------------------------------------------------


def _variable_to_name(node: Optional[Node], src: bytes) -> Optional[str]:
    """`variable_name` wraps a `name` child carrying the bare identifier
    (no `$`). Strip the leading `$` defensively in case the grammar
    leaves it on the surface text (older grammar versions did).
    """
    if node is None:
        return None
    for c in node.named_children:
        if c.type == "name":
            return _text(c, src)
    text = _text(node, src).lstrip("$").strip()
    return text or None


def _first_child(node: Node, type_name: str) -> Optional[Node]:
    for c in node.named_children:
        if c.type == type_name:
            return c
    return None


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
