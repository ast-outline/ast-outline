"""Lua adapter — parses .lua files via tree-sitter-lua into Declaration IR.

Design notes (how Lua concepts map to the IR):

Lua has no classes, modules, or visibility keywords — every API surface
is a convention over plain tables. We extract what is reliably
recoverable from the syntax and stay close to source-true names:

- ``function foo()``                → KIND_FUNCTION (top-level global)
- ``local function foo()``          → KIND_FUNCTION, visibility=private
- ``function M.foo()``              → KIND_FUNCTION, name="M.foo"
- ``function M:bar()``              → KIND_METHOD,   name="M:bar"
  (the colon is source-true syntax for implicit ``self`` —
  the cheapest, most honest signal that the function is an
  instance method rather than a module statics)
- ``function M.__add()`` /
  ``M.__add = function() end``      → KIND_OPERATOR (metamethod)
- ``M.foo = function() end``        → KIND_FUNCTION on M
- ``M.CONST = 1``                   → KIND_FIELD
- ``local x = 1``                   → KIND_FIELD, visibility=private
- ``local x = function() end``      → KIND_FUNCTION, visibility=private
- ``return { foo = function() end,  → KIND_FUNCTION (one per table field
  CONST = 1 }``                       at the top level of a direct-return
                                      table — the second common module
                                      shape in Lua besides ``local M = {}``)
- ``require "x"`` / ``require("x")`` → ``imports`` (source-true), with
  ``local Y = require("x")`` recognised as one statement
- ``--`` / ``--[[ ... ]]`` /         → ``noise_regions``; leading ``--``
  ``--[==[ ... ]==]``                 comments before a decl → ``docs``

Module-shape detection is intentionally NOT done. Each declaration is
emitted at the file's top level, with the qualifier baked into
``name`` (``M.foo``, ``M:bar``). This stays flat the way Python adapter
is flat — Lua is a flat-file language whose "modules" are naming
convention, not syntax. Nesting under a synthetic KIND_NAMESPACE would
need a heuristic ("which ``local X = {}`` is THE module?") and would
fail on the many real files that hold several module-shaped tables.
``setmetatable``-based inheritance is also deferred — there is no
syntactic anchor, only convention, and getting it right needs DSL-
specific handling (middleclass, 30log) we don't have user signal for.

Visibility rule (single source of truth in ``_visibility_for_name``):
- Inside a ``local`` declaration → "private" (the language's actual
  scoping is private — module-internal helpers).
- Name starts with ``_`` AND is not a metamethod (``__add``,
  ``__index``, …) → "private" (Python-style underscore convention,
  widely used in Neovim / LÖVE ecosystems).
- Metamethods (``__add``, ``__index``, …) are public protocol —
  visibility stays "" (public), the same way Python dunders do.
- Everything else → "" (public).

Luau (Roblox's typed dialect, ``.luau``) is intentionally out of scope
for v1 — vanilla tree-sitter-lua produces ERROR nodes on Luau type
annotations, and Roblox developers mostly live inside Studio anyway.
Planned for v0.9.1+ if user signal materialises.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_lua
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_OPERATOR,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tree_sitter_lua.language())
_PARSER = Parser(_LANGUAGE)


# Lua metamethod names — every protocol hook the language reserves
# under the ``__name`` convention, across 5.1 through 5.4. Any
# declaration whose final name segment is in this set is emitted as
# KIND_OPERATOR so the digest's ``--kind`` filter can isolate operator
# overloads / protocol hooks from regular methods. Arithmetic and
# bitwise metaметоds ARE operators in the conventional sense;
# protocol hooks (``__index``, ``__tostring``, ``__gc``, …) are
# stretched into the same bucket on purpose — they share the
# "implementation of a language-level contract" character, and
# splitting into two kinds would force a renderer-level distinction
# the IR doesn't currently make.
_METAMETHODS: frozenset[str] = frozenset({
    # Arithmetic
    "__add", "__sub", "__mul", "__div", "__mod", "__pow", "__unm", "__idiv",
    # Bitwise (Lua 5.3+)
    "__band", "__bor", "__bxor", "__bnot", "__shl", "__shr",
    # Comparison
    "__eq", "__lt", "__le",
    # String / length / call
    "__concat", "__len", "__call",
    # Indexing / introspection / lifecycle
    "__index", "__newindex", "__tostring", "__metatable", "__pairs",
    "__name", "__close", "__gc", "__mode",
})


class LuaAdapter:
    language_name = "lua"
    display_name = "Lua"
    extensions = {".lua", ".wlua"}
    definition_keywords = frozenset({"function"})
    comment_line_prefixes = ('--',)
    import_line_prefixes = ('require ', 'require(')
    render_family = "code"
    # Method / field chains (`a.b:c(...)`) and paren-less sugar calls
    # (`f"x"` / `f'x'` / `f{...}` / `f[[...]]`) — consumed by grep's
    # call-vs-ref walker. Bare `[` is deliberately NOT a sugar opener
    # (`f[key]` is a subscript); only the doubled `[[` long-string
    # bracket opens a call argument. See LanguageAdapter's optional
    # attributes.
    name_chain_separators = ".:"
    call_sugar_openers = ('"', "'", "{", "[[")

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        imports: list[str] = []
        import_regions: list[tuple[int, int]] = []
        conditional_count = _walk_chunk(
            tree.root_node, src, decls, imports, import_regions
        )
        import_regions.sort()
        noise_regions = _collect_noise_regions(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_count,
            noise_regions=noise_regions,
            import_regions=import_regions,
        )


# --- Top-level walk -------------------------------------------------------


def _walk_chunk(
    root: Node,
    src: bytes,
    decls: list[Declaration],
    imports: list[str],
    import_regions: list[tuple[int, int]],
) -> int:
    """Walk the file's top-level statements once.

    Two things happen per pass:

    1. Each top-level statement is dispatched to its handler, which
       appends one or more ``Declaration`` objects.
    2. Top-level ``require`` calls (bare or wrapped in
       ``local X = require(...)``) are pulled out as imports. Nested
       ``require`` calls (inside function bodies, ``if``/``while``/
       loops, deeper expressions) bump the conditional counter
       instead — Lua's ``require`` is a runtime function call, so
       only file-top-level ones are statically guaranteed to load.

    Leading ``--`` comments are accumulated as a pending docs block
    and attached to the next non-comment declaration (Ruby-style),
    then cleared. Two blank-line-separated comment blocks therefore
    don't bleed into the next decl — accumulator clears on each decl.
    """
    conditional = 0
    pending_docs: list[str] = []

    for child in root.named_children:
        t = child.type

        if t == "comment":
            pending_docs.append(_text(child, src).rstrip())
            continue

        if t == "function_declaration":
            d = _function_decl_to_decl(child, src)
            if d is not None:
                _attach_docs(d, pending_docs)
                decls.append(d)
            # The function body itself is conditional from the
            # module's perspective — any ``require`` call inside is
            # a lazy / runtime dependency and gets counted, never
            # listed in static imports.
            conditional += _count_nested_require(child, src)
            pending_docs = []
            continue

        if t == "variable_declaration":
            made = _local_decl_to_decls(child, src)
            if made:
                _attach_docs(made[0], pending_docs)
                decls.extend(made)
            # Count any nested ``require`` calls inside the RHS as
            # conditional imports — ``local X = require("y")`` itself
            # is a top-level static import (handled by the explicit
            # branch below); but ``local X = somecond and require("y")``
            # is conditional. Static-require shapes also push the
            # ENTIRE ``local X = require(...)`` byte range into
            # ``import_regions`` so the grep classifier can promote
            # those lines to ``[import]`` even though the stripped
            # line starts with ``local`` (which would otherwise be
            # too broad a prefix to whitelist).
            conditional += _add_top_level_or_count_require(
                child, src, imports, import_regions,
                statement_node=child,
            )
            pending_docs = []
            continue

        if t == "assignment_statement":
            made = _assignment_to_decls(child, src)
            if made:
                _attach_docs(made[0], pending_docs)
                decls.extend(made)
            conditional += _add_top_level_or_count_require(
                child, src, imports, import_regions,
                statement_node=child,
            )
            pending_docs = []
            continue

        if t == "function_call":
            # Bare top-level ``require "foo"`` (no assignment) — also
            # a static dependency for the file.
            if _is_require_call(child, src):
                imp = _render_require_import(child, src)
                if imp:
                    imports.append(imp)
                import_regions.append((child.start_byte, child.end_byte))
            else:
                conditional += _count_nested_require(child, src)
            pending_docs = []
            continue

        if t == "return_statement":
            # Direct-return-table module shape: the last statement is
            # ``return { foo = function() end, CONST = 1 }``. The
            # grammar wraps the value in an ``expression_list`` whose
            # first entry is the table_constructor. Walk the returned
            # table's fields and emit each as a top-level declaration
            # so the file's API surface shows up in outline / digest.
            table = _find_returned_table(child)
            if table is not None:
                decls.extend(_table_fields_to_decls(table, src))
            pending_docs = []
            continue

        # Other statements (do/while/if/for at top level, etc.) — we
        # don't emit decls from them, but ``require`` calls inside
        # are conditional dependencies worth counting.
        conditional += _count_nested_require(child, src)
        pending_docs = []

    return conditional


def _attach_docs(decl: Declaration, pending: list[str]) -> None:
    if pending:
        decl.docs = list(pending)


# --- Function declarations ------------------------------------------------


def _function_decl_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Convert a ``function_declaration`` node to a Declaration.

    Three name shapes share this node type:

    - bare identifier — ``function foo()`` or ``local function foo()``;
      ``local`` keyword sibling toggles visibility.
    - ``dot_index_expression`` — ``function A.B.foo()``; recursively
      flattened to ``A.B.foo``.
    - ``method_index_expression`` — ``function A.B:foo()``; the colon
      is the implicit-``self`` marker, so kind becomes KIND_METHOD
      and the colon is preserved verbatim in the name (``A.B:foo``).

    Metamethod name (``__add``, ``__index``, …) on the final segment
    upgrades kind to KIND_OPERATOR regardless of dot/colon — these
    are language-level protocol hooks and worth their own kind for
    ``--kind`` filtering.
    """
    is_local = any(c.type == "local" for c in node.children)

    # The name node is the first non-keyword named child. tree-sitter-lua
    # gives one of: ``identifier``, ``dot_index_expression``,
    # ``method_index_expression``.
    name_node: Optional[Node] = None
    for c in node.children:
        if c.type in ("identifier", "dot_index_expression", "method_index_expression"):
            name_node = c
            break
    if name_node is None:
        return None

    name, is_method, last_segment = _resolve_name(name_node, src)
    if not name:
        return None

    params = _params_text(node, src)
    is_meta = last_segment in _METAMETHODS

    if is_meta:
        kind = KIND_OPERATOR
    elif is_method:
        kind = KIND_METHOD
    else:
        kind = KIND_FUNCTION

    visibility = _visibility_for(name, last_segment, is_local=is_local)
    sig = f"function {name}{params}"

    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
    )


def _resolve_name(node: Node, src: bytes) -> tuple[str, bool, str]:
    """Resolve a function-decl name node to ``(qualified, is_method, last)``.

    ``qualified`` is the source-true dotted name with a colon kept for
    method-style decls (``A.B:method``). ``is_method`` is True only
    when the final separator is ``:`` (implicit ``self``).
    ``last`` is the final identifier segment, used by the caller to
    detect metamethods (``__add`` etc.) without re-parsing the name.
    """
    t = node.type
    if t == "identifier":
        name = _text(node, src)
        return name, False, name

    if t == "method_index_expression":
        # method_index_expression children: <table-expr> ':' identifier
        # The table-expr can itself be an identifier or dot_index_expression
        table = node.child(0)
        method_id = node.named_child(node.named_child_count - 1)
        if table is None or method_id is None:
            return "", False, ""
        left_name, _, _ = _resolve_name(table, src)
        last = _text(method_id, src)
        return f"{left_name}:{last}", True, last

    if t == "dot_index_expression":
        # dot_index_expression children: <table-expr> '.' identifier
        table = node.child(0)
        field_id = node.named_child(node.named_child_count - 1)
        if table is None or field_id is None:
            return "", False, ""
        left_name, _, _ = _resolve_name(table, src)
        last = _text(field_id, src)
        return f"{left_name}.{last}", False, last

    # Unknown shape — fall back to raw text, no metamethod / method info.
    raw = _text(node, src)
    return raw, False, raw


def _params_text(fn_node: Node, src: bytes) -> str:
    """Return ``(params)`` text for the function-decl or function_definition.

    tree-sitter-lua exposes parameters as a ``parameters`` child whose
    span already includes the parens — we just take the source slice.
    """
    for c in fn_node.children:
        if c.type == "parameters":
            return _text(c, src)
    return "()"


# --- Local declarations ---------------------------------------------------


def _local_decl_to_decls(node: Node, src: bytes) -> list[Declaration]:
    """``local x = 1``, ``local x = function() end``, ``local a, b = 1, 2``.

    The grammar wraps a ``local`` keyword around an inner
    ``assignment_statement``. We delegate the LHS×RHS pairing to the
    same helper that handles top-level assignments, then mark every
    resulting decl as private (``local`` IS the language's private
    scope).
    """
    assignment = None
    for c in node.named_children:
        if c.type == "assignment_statement":
            assignment = c
            break

    if assignment is None:
        return []

    # Lua 5.4: ``local X <const> = …`` / ``<close> = …``. The grammar
    # parents the ``attribute`` node inside the inner ``variable_list``
    # (one attribute per variable slot), not at the variable_declaration
    # level — tree-sitter-lua doesn't expose field names so we access
    # ``variable_list`` positionally as the first named child of
    # ``assignment_statement``.
    attrs: list[str] = []
    assignment_named = list(assignment.named_children)
    var_list_node: Optional[Node] = assignment_named[0] if assignment_named else None
    if var_list_node is not None:
        for c in var_list_node.named_children:
            if c.type == "attribute":
                attrs.append(_text(c, src))

    decls = _assignment_to_decls(assignment, src, force_private=True)
    # Widen byte range so ``show`` includes the leading ``local``
    # keyword (and any ``<const>`` / ``<close>`` attribute) — without
    # this the slice would start at the variable name and the agent
    # would miss the privacy marker that justified visibility=private.
    for d in decls:
        if attrs:
            d.attrs = attrs + d.attrs
        d.start_byte = node.start_byte
        d.start_line = node.start_point[0] + 1
        d.doc_start_byte = min(d.doc_start_byte or node.start_byte, node.start_byte)
    return decls


# --- Assignment statements (both top-level globals and ``local`` inner) ---


def _assignment_to_decls(
    node: Node,
    src: bytes,
    *,
    force_private: bool = False,
) -> list[Declaration]:
    """Convert ``LHS = RHS`` (or comma-list versions) to declarations.

    Pairs each variable in ``variable_list`` with its matching RHS in
    ``expression_list`` by position. Missing RHS slots default to nil
    (still a binding worth surfacing as KIND_FIELD).

    Each LHS variable resolves via :func:`_resolve_name`:
    - bare identifier → simple field / function name
    - ``dot_index_expression`` → ``M.x`` / ``M.__add``
    - ``method_index_expression`` is grammatically not a valid
      assignment target in Lua, so we don't expect it here.

    Variables whose names don't resolve (table-bracket access with
    non-identifier keys: ``M["weird"] = …``, ``M[42] = …``) are
    skipped silently — we can't render a stable name and the agent
    can find them via ``grep`` if needed.
    """
    # tree-sitter-lua exposes ``variable_list`` and ``expression_list``
    # as the first two named children of ``assignment_statement``; the
    # grammar does NOT register them as named fields, so positional
    # access is the contract. (``child_by_field_name`` returns None.)
    children = list(node.named_children)
    var_list: Optional[Node] = children[0] if len(children) >= 1 else None
    expr_list: Optional[Node] = children[1] if len(children) >= 2 else None

    if var_list is None:
        return []

    variables = [
        c for c in var_list.named_children
        if c.type in ("identifier", "dot_index_expression", "bracket_index_expression")
    ]
    values: list[Optional[Node]] = []
    if expr_list is not None:
        values = [c for c in expr_list.named_children]

    out: list[Declaration] = []
    for i, var in enumerate(variables):
        if var.type == "bracket_index_expression":
            # ``M["weird"] = …`` / ``M[42] = …`` — non-identifier
            # key, no stable name to emit. Silently skip; the agent
            # can find such fields via grep.
            continue

        name, is_method, last = _resolve_name(var, src)
        if not name or is_method:
            continue

        value = values[i] if i < len(values) else None
        decl = _make_decl_from_assignment(
            var, value, name, last, node, src,
            force_private=force_private,
        )
        if decl is not None:
            out.append(decl)
    return out


def _make_decl_from_assignment(
    var: Node,
    value: Optional[Node],
    name: str,
    last_segment: str,
    parent_stmt: Node,
    src: bytes,
    *,
    force_private: bool,
) -> Optional[Declaration]:
    """Build one Declaration for one ``var = value`` pair.

    Kind comes from the RHS shape:
    - function_definition → KIND_FUNCTION (or KIND_OPERATOR if name
      is a metamethod; the qualifier-with-colon shape doesn't appear
      here because LHS is always table-member assignment, never
      method-style — ``M:foo = function() end`` is a syntax error).
    - anything else (number / string / table / call / nil) → KIND_FIELD.

    For function-valued assignments we also extract a ``params``
    string from the RHS so the signature reads as a function decl
    rather than a bare ``M.foo = function`` (which is what raw source
    would show but doesn't help a reader scan).
    """
    is_meta = last_segment in _METAMETHODS

    if value is not None and value.type == "function_definition":
        params = _params_text(value, src)
        sig = f"function {name}{params}"
        kind = KIND_OPERATOR if is_meta else KIND_FUNCTION
    else:
        sig = name
        # Even non-function metamethod assignments (e.g.
        # ``C.__index = C`` setting up a class) belong with operators
        # — they're protocol declarations, not regular fields.
        kind = KIND_OPERATOR if is_meta else KIND_FIELD

    visibility = _visibility_for(
        name, last_segment,
        is_local=force_private,
    )

    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        visibility=visibility,
        start_line=parent_stmt.start_point[0] + 1,
        end_line=parent_stmt.end_point[0] + 1,
        start_byte=parent_stmt.start_byte,
        end_byte=parent_stmt.end_byte,
        doc_start_byte=parent_stmt.start_byte,
    )


# --- Direct-return-table module shape -------------------------------------


def _find_returned_table(return_stmt: Node) -> Optional[Node]:
    """Locate the ``table_constructor`` inside a ``return { ... }`` stmt.

    The grammar shape is ``return_statement → expression_list →
    table_constructor`` for the direct-return-table module pattern.
    Returns None for any other return value (``return M`` /
    ``return nil`` / ``return a, b``) — those don't carry exportable
    field declarations.
    """
    for c in return_stmt.named_children:
        if c.type == "table_constructor":
            return c
        if c.type == "expression_list":
            for inner in c.named_children:
                if inner.type == "table_constructor":
                    return inner
                # Stop after first value — ``return T, "trailing"`` is
                # multi-return, the table is the primary export only
                # when it's the sole value.
                break
    return None


def _table_fields_to_decls(table: Node, src: bytes) -> list[Declaration]:
    """``return { foo = function() end, CONST = 1, [k] = v }`` —
    surface each named field as a top-level declaration.

    Non-identifier keys (``[string]`` / ``[number]``) are skipped:
    they're rare in the export-table position and there's no stable
    name to render.
    """
    out: list[Declaration] = []
    for f in table.named_children:
        if f.type != "field":
            continue
        # ``field`` children layout for ``name = value``: identifier, '=', value.
        # For ``[key] = value``: '[', key-expr, ']', '=', value.
        children = list(f.children)
        if not children:
            continue
        key_node = children[0]
        if key_node.type != "identifier":
            continue  # bracketed key, skip
        name = _text(key_node, src)

        value_node: Optional[Node] = None
        for c in f.named_children:
            if c is key_node:
                continue
            value_node = c
            break

        is_meta = name in _METAMETHODS

        if value_node is not None and value_node.type == "function_definition":
            params = _params_text(value_node, src)
            sig = f"function {name}{params}"
            kind = KIND_OPERATOR if is_meta else KIND_FUNCTION
        else:
            sig = name
            kind = KIND_OPERATOR if is_meta else KIND_FIELD

        out.append(Declaration(
            kind=kind,
            name=name,
            signature=sig,
            visibility=_visibility_for(name, name, is_local=False),
            start_line=f.start_point[0] + 1,
            end_line=f.end_point[0] + 1,
            start_byte=f.start_byte,
            end_byte=f.end_byte,
            doc_start_byte=f.start_byte,
        ))
    return out


# --- Imports --------------------------------------------------------------


def _add_top_level_or_count_require(
    stmt: Node,
    src: bytes,
    imports: list[str],
    import_regions: list[tuple[int, int]],
    *,
    statement_node: Node,
) -> int:
    """Walk a top-level assignment / local-decl looking for ``require``.

    The RHS expression_list can contain:
    - a direct ``function_call`` whose callee is ``require`` → static
      import, append to ``imports`` and don't count.
    - any other expression that internally calls ``require``
      (conditional / wrapped) → bump conditional counter.

    For static imports, the byte range of the WHOLE statement
    (``statement_node``) goes into ``import_regions`` so the grep
    classifier promotes the full source line — including the leading
    ``local X = `` prefix — to ``[import]``. Using the statement
    range (not just the call) is essential because the line prefix
    in ``local X = require(...)`` is ``local``, not ``require``, so
    the line-prefix heuristic alone can't classify it.

    Returns the conditional count contributed by this statement.
    """
    # tree-sitter-lua doesn't register named fields, so we look up
    # ``expression_list`` positionally. For ``assignment_statement``
    # it's named-child #1 (after ``variable_list``). For
    # ``variable_declaration`` we descend through the wrapped
    # ``assignment_statement`` first.
    expr_list: Optional[Node] = None
    inner = stmt
    if stmt.type == "variable_declaration":
        for c in stmt.named_children:
            if c.type == "assignment_statement":
                inner = c
                break
        else:
            return 0
    inner_children = list(inner.named_children)
    if len(inner_children) >= 2:
        expr_list = inner_children[1]
    if expr_list is None:
        return 0

    conditional = 0
    found_static = False
    for expr in expr_list.named_children:
        if expr.type == "function_call" and _is_require_call(expr, src):
            imp = _render_require_import(expr, src)
            if imp:
                imports.append(imp)
            found_static = True
            # Skip nested-require count for this expression — we
            # already accounted for it as a static import.
            continue
        # Anything else that contains a nested ``require`` call is
        # conditional (wrapped in arithmetic, ternary-like ``and``/
        # ``or`` short-circuits, function-call wrappers, etc.).
        conditional += _count_nested_require(expr, src)

    if found_static:
        import_regions.append(
            (statement_node.start_byte, statement_node.end_byte)
        )
    return conditional


def _count_nested_require(node: Node, src: bytes) -> int:
    """Count every ``require(...)`` call anywhere under ``node``.

    Used for both:
    - The RHS-of-assignment scan when the top-level ``require`` is
      wrapped in a conditional expression (counted but not listed).
    - Function / loop / branch bodies where ``require`` calls are
      lazy-loaded at runtime.

    Doesn't try to dedupe — three calls to ``require("x")`` count as
    three. Real-world this is rare; the counter exists to flag "there
    are dynamic dependencies the agent should know about", precise
    count is unnecessary.
    """
    count = 0
    stack: list[Node] = [node]
    while stack:
        n = stack.pop()
        if n.type == "function_call" and _is_require_call(n, src):
            count += 1
            continue
        stack.extend(n.children)
    return count


def _is_require_call(node: Node, src: bytes) -> bool:
    """True if ``node`` is ``require <string>`` or ``require(<string>)``.

    The grammar wraps both forms (``require "x"`` and ``require("x")``)
    as a single ``function_call`` node — the callee is an identifier,
    and the arguments are either a single ``arguments`` (parenthesised)
    or a single ``string`` (bare string-arg).
    """
    callee = node.child(0)
    if callee is None or callee.type != "identifier":
        return False
    return _text(callee, src) == "require"


def _render_require_import(node: Node, src: bytes) -> str:
    """Build the source-true ``require ...`` line for the imports list.

    We preserve the call's original surface form — ``require "x"``
    stays bare, ``require("x")`` keeps its parens, ``require "foo.bar"``
    keeps the dotted module path. The grammar wraps both bare and
    parenthesised arguments in an ``arguments`` node whose text
    differs only by the parens, so re-emitting from the raw source
    span is simpler and source-truer than reconstructing from fields.
    Multi-line whitespace inside the call (rare) is collapsed to keep
    one statement on one line, matching how every other adapter
    presents imports.
    """
    return " ".join(_text(node, src).split())


# --- Noise regions --------------------------------------------------------


def _collect_noise_regions(root: Node) -> list[tuple[int, int, str]]:
    """Walk the tree once, returning string and comment byte ranges.

    Lua block comments (``--[[ ... ]]`` and the level-N variants
    ``--[=[ ... ]=]``) and long-bracket strings (``[[ ... ]]``,
    ``[=[ ... ]=]``) span multiple lines, so the line-prefix
    heuristics in ``grep.py`` can't classify matches inside them on
    their own. Sourcing noise regions from tree-sitter is the only
    reliable way to filter false positives in long-form Lua content
    (LDoc blocks, embedded SQL / shader text inside long-string
    literals, …).
    """
    out: list[tuple[int, int, str]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "string":
            out.append((node.start_byte, node.end_byte, "string"))
            continue
        if node.type == "comment":
            out.append((node.start_byte, node.end_byte, "comment"))
            continue
        stack.extend(node.children)
    out.sort()
    return out


# --- Visibility / helpers -------------------------------------------------


def _visibility_for(name: str, last_segment: str, *, is_local: bool) -> str:
    """Return the IR visibility string for a declaration name.

    Rules (single source of truth — see module docstring):
    - ``local`` scope → "private" (the language's actual private).
    - Metamethod (``__add``, ``__index``, …) → "" (public protocol).
    - Last segment of qualified name starts with ``_`` and isn't a
      metamethod → "private" (Python-style underscore convention,
      universal in Neovim / LÖVE / Roblox-Lua codebases).
    - Anything else → "" (public).
    """
    if is_local:
        return "private"
    if last_segment in _METAMETHODS:
        return ""
    # Use last segment, not the whole qualified name — ``M._helper``
    # is private (helper on a public module), but ``_M.foo`` isn't a
    # real Lua idiom we need to handle. The metamethod set is already
    # filtered above; everything else starting with ``_`` (including
    # non-standard double-underscore names like ``M.__custom_hook``
    # that don't exist in the language-level protocol) is private —
    # users who reach for the ``__`` prefix outside the standard
    # metamethod names are still signalling "internal".
    if last_segment.startswith("_"):
        return "private"
    return ""


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")
