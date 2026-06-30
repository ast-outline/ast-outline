"""Elixir adapter -- parses .ex / .exs files via tree-sitter-elixir.

Design notes (how Elixir concepts map to the IR):

- ``defmodule Name do ... end``          -> KIND_NAMESPACE. The module name
                                            is the first ``alias`` child of
                                            the call's ``arguments`` node
                                            (``MyApp.Foo`` is a single
                                            ``alias`` node in the grammar).
                                            Nested ``defmodule`` calls
                                            produce nested KIND_NAMESPACE
                                            declarations; they are NOT
                                            collapsed (unlike Ruby's
                                            ``module A; module B`` collapse)
                                            because an Elixir outer module
                                            may contain code outside the
                                            inner one.
- ``def / defp``                         -> KIND_FUNCTION at top level,
                                            KIND_METHOD inside a module.
                                            ``defp`` carries
                                            ``visibility="private"``.
- ``defmacro / defmacrop``               -> same kind rules as def/defp, but
                                            with an attrs entry ``[macro]``.
- ``defguard / defguardp``               -> KIND_FUNCTION / KIND_METHOD with
                                            attrs entry ``[guard]``.
- ``defdelegate``                        -> KIND_METHOD with ``[delegate]``.
                                            The ``to:`` keyword is kept in
                                            the signature so the delegation
                                            target is visible in the digest.
- ``defprotocol Name do ... end``        -> KIND_INTERFACE.
- ``defimpl Name, for: Type do ... end`` -> KIND_CLASS (``native_kind=
                                            "defimpl"``). Signature is
                                            source-true: ``defimpl Name,
                                            for: Type``.
- ``defstruct`` / ``defexception``       -> one KIND_FIELD per declared key,
                                            with markers ``[struct]`` /
                                            ``[exception]``. Splitting
                                            multi-key declarations makes each
                                            field individually grep-able,
                                            mirroring how the Ruby adapter
                                            handles ``attr_accessor``.
                                            Default values are dropped from
                                            per-field signatures -- they are
                                            implementation detail, not
                                            interface.
- ``@type name :: ...``                  -> KIND_FIELD with ``[type]``.
- ``@typep name :: ...``                 -> KIND_FIELD with ``[typep]`` and
                                            ``visibility="private"``.
- ``@opaque name :: ...``                -> KIND_FIELD with ``[opaque]``.
- ``@callback name(args) :: return``     -> KIND_METHOD with ``[callback]``.
- ``@doc`` / ``@moduledoc``              -> absorbed as ``docs`` on the next
                                            declaration (``@doc``) or on the
                                            module itself (``@moduledoc``).
- ``# comment`` lines preceding a decl  -> absorbed into ``docs``.
- ``@spec``                              -> NOT surfaced. Specs annotate
                                            functions; the function
                                            declaration already carries name
                                            and arity. Surfacing specs as
                                            separate fields would double-
                                            declare every function.
- ``use`` / ``import`` / ``alias`` /
  ``require``                            -> ``imports`` entries, source-true.
                                            All four are compile-time.
                                            ``alias MyApp.{Bar, Baz}``
                                            expands to two entries so each
                                            alias is individually grep-able.

Callback-DSL blocks (the KIND_BLOCK rule)
-----------------------------------------
Elixir DSLs -- ExUnit (``describe`` / ``test``), Phoenix Router
(``scope`` / ``pipeline``), Ecto (``schema``), Plug (``pipeline`` /
``plug``), and custom frameworks -- are expressed as function calls that
take a ``do...end`` block. The same structural rule used by the Ruby
adapter applies here, with no hard-coded name list:

1. The call's function is a plain identifier (no ``.`` receiver).
   Dot calls (``Enum.each``, ``IO.inspect``) are excluded.
2. The FIRST argument is a string or atom label. This separates
   ``describe "#full_name"`` and ``test "returns true"`` (promoted) from
   ``if condition do`` (no label -- pure control flow) and
   ``Enum.each(list, fn x ->`` (excluded by rule 1).

A block that fails either clause is descended transparently: its body is
walked so any named DSL containers nested inside an unrecognised outer
wrapper still surface.

Function clause deduplication
------------------------------
Elixir functions are defined by one or more *clauses* -- each ``def foo``
at the same name with a different pattern-match head is a separate AST
node. The adapter emits only the FIRST clause as the representative
declaration and skips subsequent clauses with the same (name, visibility)
key. This prevents ``handle_call/3`` from producing one entry per arm.
The first clause carries whatever ``@doc`` / ``# comment`` preceded it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_elixir as tse
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    Declaration,
    ParseResult,
)

_LANGUAGE = Language(tse.language())
_PARSER = Parser(_LANGUAGE)

# Maps the def-form keyword to (attr_marker, visibility).
# Empty string means "no marker" / "public".
_DEF_NAMES: dict[str, tuple[str, str]] = {
    "def":         ("",           ""),
    "defp":        ("",           "private"),
    "defmacro":    ("[macro]",    ""),
    "defmacrop":   ("[macro]",    "private"),
    "defguard":    ("[guard]",    ""),
    "defguardp":   ("[guard]",    "private"),
    "defdelegate": ("[delegate]", ""),
}

_STRUCT_MACROS: dict[str, str] = {
    "defstruct":    "[struct]",
    "defexception": "[exception]",
}

_MODULE_ATTR_TYPES: dict[str, tuple[str, str]] = {
    # attr_name -> (marker, visibility)
    "type":   ("[type]",   ""),
    "typep":  ("[typep]",  "private"),
    "opaque": ("[opaque]", ""),
}

_IMPORT_MACROS = frozenset({"use", "import", "alias", "require"})

# First-argument node types that qualify a do_block call as a KIND_BLOCK
# named container (the structural label discriminator).
_BLOCK_LABEL_TYPES = frozenset({"string", "atom"})


class ElixirAdapter:
    language_name = "elixir"
    display_name = "Elixir"
    extensions = {".ex", ".exs"}
    definition_keywords = frozenset({
        "defmodule", "def", "defp", "defmacro", "defmacrop",
        "defprotocol", "defimpl", "defstruct", "defguard",
    })
    comment_line_prefixes = ("#",)
    import_line_prefixes = ("use ", "import ", "alias ", "require ")
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        _walk_body(tree.root_node.named_children, src, scope="top",
                   seen_defs=set(), out=decls)
        imports: list[str] = []
        _collect_imports(tree.root_node.named_children, src, imports)
        conditional_count = _count_conditional_imports(tree.root_node, src)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_count,
        )


# ---------------------------------------------------------------------------
# Body walker
# ---------------------------------------------------------------------------

def _walk_body(
    nodes: list[Node],
    src: bytes,
    *,
    scope: str,
    seen_defs: set[tuple[str, str]],
    out: list[Declaration],
) -> None:
    """Walk a list of named AST nodes, accumulating declarations into ``out``.

    ``scope`` is ``"top"`` (file root or callback-block body), ``"module"``
    (inside a defmodule), ``"protocol"`` (inside a defprotocol), or
    ``"impl"`` (inside a defimpl).

    ``seen_defs`` tracks ``(name, visibility)`` pairs so subsequent clauses
    of the same function are deduplicated within the current scope.
    """
    pending_docs: list[str] = []
    pending_doc_node: Optional[Node] = None

    for node in nodes:
        t = node.type

        if t == "comment":
            pending_docs.append(_text(node, src).strip())
            continue

        if t == "unary_operator":
            result = _handle_module_attr(node, src, pending_docs)
            if result == "doc_captured":
                # _handle_module_attr already mutated pending_docs in place
                pending_doc_node = node
                continue
            if isinstance(result, Declaration):
                result.docs = pending_docs + result.docs
                if pending_docs:
                    result.doc_start_byte = _doc_start_byte(
                        pending_doc_node or node, pending_docs, src
                    )
                out.append(result)
            pending_docs = []
            pending_doc_node = None
            continue

        if t == "call":
            fn_name = _call_fn_name(node, src)

            if fn_name == "defmodule":
                decl = _defmodule_to_decl(node, src)
                if decl is not None:
                    decl.docs = pending_docs + decl.docs
                    if pending_docs:
                        decl.doc_start_byte = _doc_start_byte(
                            pending_doc_node or node, pending_docs, src
                        )
                    out.append(decl)
                pending_docs = []
                pending_doc_node = None
                continue

            if fn_name == "defprotocol":
                decl = _defprotocol_to_decl(node, src)
                if decl is not None:
                    decl.docs = pending_docs + decl.docs
                    out.append(decl)
                pending_docs = []
                pending_doc_node = None
                continue

            if fn_name == "defimpl":
                decl = _defimpl_to_decl(node, src)
                if decl is not None:
                    decl.docs = pending_docs + decl.docs
                    out.append(decl)
                pending_docs = []
                pending_doc_node = None
                continue

            if fn_name in _DEF_NAMES:
                decl = _def_to_decl(node, src, scope, fn_name, seen_defs)
                if decl is not None:
                    decl.docs = pending_docs + decl.docs
                    if pending_docs:
                        decl.doc_start_byte = _doc_start_byte(
                            pending_doc_node or node, pending_docs, src
                        )
                    out.append(decl)
                # Always clear pending even when we skip a duplicate clause --
                # the @doc that preceded a second clause belongs to it and
                # should not bleed onto the next distinct declaration.
                pending_docs = []
                pending_doc_node = None
                continue

            if fn_name in _STRUCT_MACROS:
                fields = _struct_to_decls(node, src, fn_name)
                if fields:
                    fields[0].docs = pending_docs + fields[0].docs
                out.extend(fields)
                pending_docs = []
                pending_doc_node = None
                continue

            # Callback-DSL blocks (describe/test/scope/pipeline/...).
            do_blk = _do_block(node)
            if do_blk is not None:
                if _is_block_call(node, src):
                    decl = _block_call_to_decl(node, src, do_blk)
                    decl.docs = pending_docs + decl.docs
                    if pending_docs:
                        decl.doc_start_byte = _doc_start_byte(
                            pending_doc_node or node, pending_docs, src
                        )
                    out.append(decl)
                else:
                    # Transparent descent -- body contents still surface.
                    _walk_body(do_blk.named_children, src,
                               scope=scope, seen_defs=seen_defs, out=out)
                pending_docs = []
                pending_doc_node = None
                continue

        # Unhandled node -- drop pending docs.
        pending_docs = []
        pending_doc_node = None


# ---------------------------------------------------------------------------
# Module-attribute handling
# ---------------------------------------------------------------------------

def _handle_module_attr(
    node: Node,
    src: bytes,
    pending_docs: list[str],
) -> "Declaration | str | None":
    """Handle a ``unary_operator`` ``@attr ...`` node.

    Returns:
    - a Declaration to emit (for @type / @typep / @opaque / @callback)
    - the sentinel string ``"doc_captured"`` when @doc / @moduledoc text
      has been placed into ``pending_docs`` in-place
    - None for @spec, @behaviour, @impl, and any other non-surfaced attrs
    """
    inner = _unary_inner_call(node)
    if inner is None:
        return None

    attr_name = _call_fn_name(inner, src)
    if attr_name is None:
        return None

    if attr_name in ("doc", "moduledoc"):
        doc_text = _attr_string_arg(inner, src)
        # Replace pending_docs in-place so the walker knows to carry them.
        pending_docs.clear()
        if doc_text:
            pending_docs.append(doc_text)
        return "doc_captured"

    if attr_name in _MODULE_ATTR_TYPES:
        marker, visibility = _MODULE_ATTR_TYPES[attr_name]
        name, sig = _type_attr_name_sig(inner, src, attr_name)
        return Declaration(
            kind=KIND_FIELD,
            name=name,
            signature=sig,
            attrs=[marker],
            visibility=visibility,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            doc_start_byte=node.start_byte,
        )

    if attr_name == "callback":
        name, sig = _callback_name_sig(inner, src)
        return Declaration(
            kind=KIND_METHOD,
            name=name,
            signature=f"@callback {sig}",
            attrs=["[callback]"],
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            doc_start_byte=node.start_byte,
        )

    return None


def _attr_string_arg(inner: Node, src: bytes) -> str:
    """Return the text inside the string argument of @doc / @moduledoc,
    or empty string for ``@doc false`` / missing arg."""
    args = _arguments_node(inner)
    if args is None:
        return ""
    for c in args.named_children:
        if c.type == "string":
            return _string_content(c, src).strip()
    return ""


def _type_attr_name_sig(
    inner: Node, src: bytes, attr_name: str
) -> tuple[str, str]:
    """Extract (name, signature) for @type / @typep / @opaque."""
    args = _arguments_node(inner)
    if args is None:
        return "?", f"@{attr_name} ?"
    for c in args.named_children:
        if c.type == "binary_operator":
            # left: name or name(params) :: right: type
            left = c.named_children[0] if c.named_children else None
            if left is not None:
                if left.type == "call":
                    # e.g. `t(a) :: ...` -- name is the call identifier
                    name_node = left.named_children[0] if left.named_children else left
                    name = _text(name_node, src)
                elif left.type == "identifier":
                    name = _text(left, src)
                else:
                    name = _text(left, src).split("(")[0]
            else:
                name = "?"
            sig = _collapse_ws(f"@{attr_name} " + _text(c, src))
            return name, sig
        if c.type == "identifier":
            name = _text(c, src)
            return name, f"@{attr_name} {name}"
    return "?", f"@{attr_name} ?"


def _callback_name_sig(inner: Node, src: bytes) -> tuple[str, str]:
    """Extract (name, signature) from the inner call of a @callback node."""
    args = _arguments_node(inner)
    if args is None:
        return "?", "?"
    for c in args.named_children:
        if c.type == "binary_operator":
            # call(name, (args)) :: return_type
            left = c.named_children[0] if c.named_children else None
            if left is not None and left.type == "call":
                name_node = left.named_children[0] if left.named_children else left
                name = _text(name_node, src)
            else:
                name = "?"
            return name, _collapse_ws(_text(c, src))
    return "?", "?"


# ---------------------------------------------------------------------------
# defmodule
# ---------------------------------------------------------------------------

def _defmodule_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name = _first_alias_arg(node, src)
    do_blk = _do_block(node)
    children: list[Declaration] = []
    moduledoc_lines: list[str] = []

    if do_blk is not None:
        body_nodes = do_blk.named_children
        # Pick up @moduledoc from the first non-comment statement.
        for bn in body_nodes:
            if bn.type == "comment":
                continue
            if bn.type == "unary_operator":
                inner = _unary_inner_call(bn)
                if inner and _call_fn_name(inner, src) == "moduledoc":
                    doc_text = _attr_string_arg(inner, src)
                    if doc_text:
                        moduledoc_lines = [doc_text]
            break  # stop after the first non-comment node
        _walk_body(body_nodes, src, scope="module", seen_defs=set(), out=children)

    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"defmodule {name}",
        docs=moduledoc_lines,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


# ---------------------------------------------------------------------------
# defprotocol
# ---------------------------------------------------------------------------

def _defprotocol_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name = _first_alias_arg(node, src)
    do_blk = _do_block(node)
    children: list[Declaration] = []
    if do_blk is not None:
        _walk_body(do_blk.named_children, src,
                   scope="protocol", seen_defs=set(), out=children)
    return Declaration(
        kind=KIND_INTERFACE,
        name=name,
        signature=f"defprotocol {name}",
        native_kind="defprotocol",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


# ---------------------------------------------------------------------------
# defimpl
# ---------------------------------------------------------------------------

def _defimpl_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    args = _arguments_node(node)
    if args is None:
        return None
    named = args.named_children
    if not named:
        return None

    protocol = _text(named[0], src)
    for_type: Optional[str] = None
    for c in named[1:]:
        if c.type == "keywords":
            for pair in c.named_children:
                if pair.type != "pair":
                    continue
                parts = pair.named_children
                if len(parts) >= 2 and _text(parts[0], src).rstrip() == "for:":
                    for_type = _text(parts[1], src)
                    break

    name = f"{protocol}({for_type})" if for_type else protocol
    sig = f"defimpl {protocol}" + (f", for: {for_type}" if for_type else "")

    do_blk = _do_block(node)
    children: list[Declaration] = []
    if do_blk is not None:
        _walk_body(do_blk.named_children, src,
                   scope="impl", seen_defs=set(), out=children)

    return Declaration(
        kind=KIND_CLASS,
        name=name,
        signature=sig,
        native_kind="defimpl",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


# ---------------------------------------------------------------------------
# def / defp / defmacro / defguard / defdelegate
# ---------------------------------------------------------------------------

def _def_to_decl(
    node: Node,
    src: bytes,
    scope: str,
    fn_name: str,
    seen_defs: set[tuple[str, str]],
) -> Optional[Declaration]:
    attr_marker, priv = _DEF_NAMES[fn_name]
    visibility = "private" if priv else ""

    head = _def_head(node, src)
    if head is None:
        return None
    name, sig = head

    # Skip subsequent clauses of the same function within this scope.
    key = (name, visibility)
    if key in seen_defs:
        return None
    seen_defs.add(key)

    kind = KIND_FUNCTION if scope == "top" else KIND_METHOD
    attrs: list[str] = [attr_marker] if attr_marker else []

    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        attrs=attrs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
    )


def _def_head(node: Node, src: bytes) -> Optional[tuple[str, str]]:
    """Return (name, signature) for a def/defp/defmacro/... call.

    The function head is the first argument to the def-form:
    - ``def foo(x) do``         -> arguments[0] is ``call(foo, (x))``
    - ``def foo(x) when g, do:`` -> arguments[0] is ``binary_operator when``
      whose left child is ``call(foo, (x))``
    - ``def foo do``             -> arguments[0] is ``identifier foo``
    - ``defdelegate foo(x), to: M`` -> arguments[0] is ``call(foo, (x))``
      with no do_block and no ``do:`` keyword -- include full args in sig
    """
    args = _arguments_node(node)
    if args is None:
        return None
    named = args.named_children
    if not named:
        return None

    head_node = named[0]

    # Determine the name from the head node.
    if head_node.type == "call":
        name = _inner_fn_name(head_node, src)
    elif head_node.type == "binary_operator":
        # when-guard: left child is the actual call head
        left = head_node.named_children[0] if head_node.named_children else None
        name = _inner_fn_name(left, src) if left is not None else "?"
    elif head_node.type == "identifier":
        name = _text(head_node, src)
    else:
        return None

    # Determine the signature slice.
    do_blk = _do_block(node)
    if do_blk is not None:
        # Slice up to the start of the do_block to omit the body.
        raw = src[node.start_byte:do_blk.start_byte].decode("utf8", errors="replace")
        sig = _collapse_ws(raw).rstrip().rstrip(",")
    else:
        # No do_block -- check whether there is a ``do:`` keyword shorthand
        # (inline body). If so, slice to the head end to drop the body.
        # If not (defdelegate, abstract def), include the full args.
        has_do_kw = _has_do_keyword(named, src)
        if has_do_kw:
            raw = src[node.start_byte:head_node.end_byte].decode("utf8", errors="replace")
            sig = _collapse_ws(raw)
        else:
            raw = src[node.start_byte:args.end_byte].decode("utf8", errors="replace")
            sig = _collapse_ws(raw)

    return name, sig


def _has_do_keyword(arg_nodes: list[Node], src: bytes) -> bool:
    """Return True when the argument list contains a ``do:`` keyword pair.

    Keyword nodes carry their text including the trailing colon and space,
    e.g. ``b"do: "`` vs ``b"to: "``. We match exactly ``"do:"`` after
    stripping trailing whitespace so ``defdelegate ..., to: M`` is not
    mistaken for an inline-body form.
    """
    for c in arg_nodes:
        if c.type == "keywords":
            for pair in c.named_children:
                if pair.type == "pair" and pair.named_children:
                    kw_node = pair.named_children[0]
                    if kw_node.type == "keyword" and _text(kw_node, src).rstrip() == "do:":
                        return True
    return False


def _inner_fn_name(node: Optional[Node], src: bytes) -> str:
    """Get the function identifier from a call node (the head inside a def)."""
    if node is None:
        return "?"
    for c in node.named_children:
        if c.type == "identifier":
            return _text(c, src)
    return "?"


# ---------------------------------------------------------------------------
# defstruct / defexception
# ---------------------------------------------------------------------------

def _struct_to_decls(node: Node, src: bytes, fn_name: str) -> list[Declaration]:
    """Emit one KIND_FIELD per declared struct key."""
    marker = _STRUCT_MACROS[fn_name]
    args = _arguments_node(node)
    if args is None:
        return []

    keys: list[str] = []
    for c in args.named_children:
        if c.type == "list":
            for item in c.named_children:
                if item.type == "atom":
                    # :name -> strip leading colon
                    keys.append(_text(item, src).lstrip(":"))
                elif item.type == "pair":
                    # `name: default` form
                    kn = item.named_children[0] if item.named_children else None
                    if kn is not None:
                        keys.append(_text(kn, src).rstrip(":"))
        elif c.type == "keywords":
            # `defstruct name: nil, age: 0` without a list wrapper
            for pair in c.named_children:
                if pair.type == "pair" and pair.named_children:
                    kn = pair.named_children[0]
                    keys.append(_text(kn, src).rstrip(":"))

    out: list[Declaration] = []
    for k in keys:
        out.append(Declaration(
            kind=KIND_FIELD,
            name=k,
            signature=k,
            attrs=[marker],
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
        ))
    return out


# ---------------------------------------------------------------------------
# Callback-DSL blocks (KIND_BLOCK)
# ---------------------------------------------------------------------------

def _is_block_call(node: Node, src: bytes) -> bool:
    """True when a do_block-bearing call is a named DSL container.

    Structural rule (no hard-coded name list):
    1. The call function is a plain identifier -- no dot receiver.
    2. The first argument is a string or atom label.
    """
    for c in node.named_children:
        if c.type == "dot":
            return False
        if c.type == "identifier":
            break

    args = _arguments_node(node)
    named = args.named_children if args is not None else []
    return bool(named) and named[0].type in _BLOCK_LABEL_TYPES


def _block_label(node: Node, src: bytes) -> str:
    """Strip quotes / leading colon from the first arg to get the block name."""
    args = _arguments_node(node)
    named = args.named_children if args is not None else []
    if not named:
        return "?"
    first = named[0]
    text = _collapse_ws(_text(first, src))
    if len(text) >= 2 and text[0] in '"\'':
        if text[-1] == text[0]:
            text = text[1:-1]
    elif text.startswith(":"):
        text = text[1:]
    return text or "?"


def _block_call_to_decl(node: Node, src: bytes, do_blk: Node) -> Declaration:
    raw = src[node.start_byte:do_blk.start_byte].decode("utf8", errors="replace")
    sig = _collapse_ws(raw).rstrip().rstrip(",")
    if len(sig) > 140:
        sig = sig[:137] + "..."
    children: list[Declaration] = []
    _walk_body(do_blk.named_children, src, scope="top", seen_defs=set(), out=children)
    return Declaration(
        kind=KIND_BLOCK,
        name=_block_label(node, src),
        signature=sig,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


# ---------------------------------------------------------------------------
# Import collection
# ---------------------------------------------------------------------------

def _collect_imports(nodes: list[Node], src: bytes, out: list[str]) -> None:
    """Collect use/import/alias/require calls at module-body depth.

    Descends one level into defmodule do_blocks so that imports inside
    ``defmodule MyApp.Foo do ... end`` are surfaced even though they are
    not at the file root.
    """
    for node in nodes:
        if node.type != "call":
            continue
        fn = _call_fn_name(node, src)
        if fn in _IMPORT_MACROS:
            _emit_import(node, src, fn, out)
        elif fn == "defmodule":
            do_blk = _do_block(node)
            if do_blk is not None:
                _collect_imports(do_blk.named_children, src, out)


def _emit_import(node: Node, src: bytes, fn_name: str, out: list[str]) -> None:
    """Render a use/import/alias/require call as source-true text.

    ``alias MyApp.{Bar, Baz}`` is expanded to ``alias MyApp.Bar`` and
    ``alias MyApp.Baz`` so each alias is individually grep-able.
    """
    args = _arguments_node(node)
    if args is None:
        return
    named = args.named_children
    if not named:
        return

    first = named[0]

    # alias MyApp.{Bar, Baz} -- dot node: alias + tuple
    if fn_name == "alias" and first.type == "dot":
        dot_children = first.named_children
        if len(dot_children) >= 2 and dot_children[1].type == "tuple":
            prefix = _text(dot_children[0], src)
            for alias_node in dot_children[1].named_children:
                if alias_node.type == "alias":
                    out.append(f"alias {prefix}.{_text(alias_node, src)}")
            return

    out.append(f"{fn_name} {_text(first, src)}")


def _count_conditional_imports(root: Node, src: bytes) -> int:
    """Count use/import/alias/require calls inside function bodies.

    A ``do_block`` that is a direct child of a def/defp/... call is a
    function body -- imports inside it are "lazy" (fire per call, not at
    compile-time of the module). This is rare but meaningful to flag.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, inside_fn = stack.pop()
        t = node.type
        if t == "call" and inside_fn:
            fn = _call_fn_name(node, src)
            if fn in _IMPORT_MACROS:
                count += 1
        new_inside = inside_fn
        if t == "do_block" and node.parent is not None:
            parent = node.parent
            if parent.type == "call":
                fn = _call_fn_name(parent, src)
                if fn in _DEF_NAMES:
                    new_inside = True
        for c in node.children:
            stack.append((c, new_inside))
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_fn_name(node: Node, src: bytes) -> Optional[str]:
    """Return the identifier of a ``call`` node's function, or None if it
    is a dot call (receiver.method) or any non-identifier-headed call."""
    for c in node.named_children:
        if c.type == "identifier":
            return _text(c, src)
        if c.type in ("dot", "alias", "atom"):
            return None
    return None


def _arguments_node(node: Node) -> Optional[Node]:
    for c in node.named_children:
        if c.type == "arguments":
            return c
    return None


def _do_block(node: Node) -> Optional[Node]:
    for c in node.named_children:
        if c.type == "do_block":
            return c
    return None


def _unary_inner_call(node: Node) -> Optional[Node]:
    """Return the inner ``call`` child of a ``unary_operator`` node."""
    for c in node.named_children:
        if c.type == "call":
            return c
    return None


def _first_alias_arg(node: Node, src: bytes) -> str:
    """Return the text of the first ``alias`` (or ``atom``) in the call's
    arguments -- used to extract defmodule / defprotocol names."""
    args = _arguments_node(node)
    if args is None:
        return "?"
    for c in args.named_children:
        if c.type in ("alias", "atom"):
            return _text(c, src)
    return "?"


def _string_content(node: Node, src: bytes) -> str:
    """Extract the inner text of a ``string`` node (quoted_content child)."""
    for c in node.named_children:
        if c.type == "quoted_content":
            return _text(c, src)
    # Fallback: strip outermost quotes from raw text.
    raw = _text(node, src)
    if len(raw) >= 2 and raw[0] in '"\'':
        return raw[1:-1]
    return raw


def _doc_start_byte(doc_node: Node, pending: list[str], src: bytes) -> int:
    """Walk backward from ``doc_node.start_byte`` to find where the leading
    doc block begins (same logic as the Ruby adapter's ``_doc_start_byte``)."""
    if not pending:
        return doc_node.start_byte
    pos = doc_node.start_byte
    for _ in pending:
        while pos > 0 and src[pos - 1:pos] in (b"\n", b" ", b"\t", b"\r"):
            pos -= 1
        line_start = src.rfind(b"\n", 0, pos)
        pos = 0 if line_start < 0 else line_start + 1
    return pos


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")
