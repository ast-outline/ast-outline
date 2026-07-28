"""Elixir adapter -- parses .ex / .exs files via tree-sitter-elixir.

Mapping (how Elixir concepts land in the IR):

- ``defmodule``                        -> KIND_NAMESPACE. Nested defmodules
                                          produce nested declarations, not
                                          collapsed paths.
- ``def`` / ``defp``                   -> KIND_FUNCTION (top-level) or
                                          KIND_METHOD (inside a module).
                                          ``defp`` -> private.
- ``defmacro`` / ``defmacrop``         -> KIND_METHOD with ``[macro]``.
- ``defguard`` / ``defguardp``         -> KIND_METHOD with ``[guard]``.
- ``defdelegate``                      -> KIND_METHOD with ``[delegate]``.
                                          ``to:`` is kept in the signature.
- ``defprotocol``                      -> KIND_INTERFACE.
- ``defimpl Name, for: Type``          -> KIND_CLASS (``native_kind=
                                          "defimpl"``).
- ``defstruct`` / ``defexception``     -> one KIND_FIELD per key with
                                          ``[struct]`` / ``[exception]``.
                                          Default values are dropped.
- ``@type`` / ``@typep`` / ``@opaque`` -> KIND_FIELD with matching marker.
                                          ``@typep`` -> private.
- ``@callback``                        -> KIND_METHOD with ``[callback]``.
- ``@doc`` / ``@moduledoc``            -> absorbed into ``docs`` on the
                                          next decl / module respectively.
- ``@spec``                            -> not surfaced; functions already
                                          carry name and arity.
- ``use`` / ``import`` / ``alias`` /
  ``require``                          -> ``imports`` entries, source-true.
                                          ``alias MyApp.{Bar, Baz}``
                                          expands to two entries.

KIND_BLOCK rule: a do_block-bearing call is promoted to a named DSL
container (``describe`` / ``test`` / Phoenix ``scope`` etc.) when (1) the
callee is a plain identifier with no dot receiver, and (2) the first
argument is a string or atom label. Calls failing either test are
descended transparently so nested containers still surface.

Function clause deduplication: only the first clause of each
(def-keyword, name, arity) key is emitted; subsequent pattern-match
clauses are skipped. Arity and the def-keyword are part of the key so
distinct functions are never collapsed — ``foo/1`` vs ``foo/2`` (arity
is part of a function's identity in Elixir) and ``def foo`` vs
``defguard foo`` each stay separate. Deduplication is scoped to the
enclosing container: a fresh key set starts at each
``defmodule`` / ``defprotocol`` / ``defimpl`` and at each named DSL
block. The per-DSL-block reset is deliberate — a KIND_BLOCK is rendered
as its own container, so a ``def`` appearing in two sibling blocks
surfaces once under each rather than collapsing across them (transparent
descent, which hoists into the parent, shares the parent's set instead).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_elixir as tse
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors, read_source
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

# (attr_marker, visibility) per def-form keyword.
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

# (marker, visibility) per type-attribute name.
_MODULE_ATTR_TYPES: dict[str, tuple[str, str]] = {
    "type":   ("[type]",   ""),
    "typep":  ("[typep]",  "private"),
    "opaque": ("[opaque]", ""),
}

_IMPORT_MACROS = frozenset({"use", "import", "alias", "require"})
# def-forms that open a module scope — their do_blocks carry top-level
# imports the collector descends into.
_MODULE_CONTAINER_MACROS = frozenset({"defmodule", "defprotocol", "defimpl"})
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
        src = read_source(path)
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
    seen_defs: set[tuple[str, str, int]],
    out: list[Declaration],
) -> None:
    pending_docs: list[str] = []
    pending_doc_node: Optional[Node] = None

    for node in nodes:
        t = node.type

        if t == "comment":
            pending_docs.append(_text(node, src).strip())
            continue

        decl: Optional[Declaration] = None

        if t == "unary_operator":
            decl, is_doc = _handle_module_attr(node, src, pending_docs)
            if is_doc:
                # Anchor the doc-start to this node only when it actually
                # contributed text. A no-op ``@moduledoc`` (or ``@doc
                # false``) leaves pending_docs empty — anchoring here
                # would leave a stale node pointing before any later
                # comment doc, throwing off its computed doc_start_byte.
                pending_doc_node = node if pending_docs else None
                continue
            if decl is None:
                # An ignored module attribute (@spec, @impl, @behaviour,
                # a custom one …) sitting between a @doc and the def it
                # documents must stay transparent — falling through here
                # would reset pending_docs and swallow the doc before it
                # reaches the def.
                continue

        elif t == "call":
            fn_name = _call_fn_name(node, src)
            if fn_name == "defmodule":
                decl = _defmodule_to_decl(node, src)
            elif fn_name == "defprotocol":
                decl = _defprotocol_to_decl(node, src)
            elif fn_name == "defimpl":
                decl = _defimpl_to_decl(node, src)
            elif fn_name in _DEF_NAMES:
                decl = _def_to_decl(node, src, scope, fn_name, seen_defs)
            elif fn_name in _STRUCT_MACROS:
                fields = _struct_to_decls(node, src, fn_name)
                if fields:
                    fields[0].docs = pending_docs + fields[0].docs
                out.extend(fields)
                pending_docs = []
                pending_doc_node = None
                continue
            else:
                do_blk = _do_block(node)
                if do_blk is not None:
                    if _is_block_call(node, src):
                        decl = _block_call_to_decl(node, src, do_blk, scope)
                    else:
                        # Transparent descent -- surface nested DSL containers.
                        _walk_body(do_blk.named_children, src,
                                   scope=scope, seen_defs=seen_defs, out=out)

        if decl is not None:
            decl.docs = pending_docs + decl.docs
            if pending_docs:
                decl.doc_start_byte = _doc_start_byte(
                    pending_doc_node or node, pending_docs, src
                )
            out.append(decl)

        pending_docs = []
        pending_doc_node = None


# ---------------------------------------------------------------------------
# Module attribute handling (@type, @callback, @doc, ...)
# ---------------------------------------------------------------------------

def _handle_module_attr(
    node: Node,
    src: bytes,
    pending_docs: list[str],
) -> tuple[Optional[Declaration], bool]:
    """Return (decl_or_None, is_doc). Mutates pending_docs for @doc/@moduledoc."""
    inner = _unary_inner_call(node)
    if inner is None:
        return None, False

    attr_name = _call_fn_name(inner, src)
    if attr_name is None:
        return None, False

    if attr_name == "moduledoc":
        # The enclosing ``defmodule`` already absorbs its own
        # ``@moduledoc`` (see ``_defmodule_to_decl``). Treat it as a
        # no-op here so it never leaks onto the module's first member
        # as a stray doc — just swallow any pending member doc that
        # preceded it.
        pending_docs.clear()
        return None, True

    if attr_name == "doc":
        pending_docs.clear()
        doc_text = _attr_string_arg(inner, src)
        if doc_text:
            pending_docs.append(doc_text)
        return None, True

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
        ), False

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
        ), False

    return None, False


def _attr_string_arg(inner: Node, src: bytes) -> str:
    """Inner text of the string arg to @doc / @moduledoc; empty for @doc false."""
    args = _arguments_node(inner)
    if args is None:
        return ""
    for c in args.named_children:
        if c.type == "string":
            return _string_content(c, src).strip()
    return ""


def _type_attr_name_sig(inner: Node, src: bytes, attr_name: str) -> tuple[str, str]:
    """(name, sig) for @type / @typep / @opaque."""
    args = _arguments_node(inner)
    if args is None:
        return "?", f"@{attr_name} ?"
    for c in args.named_children:
        if c.type == "binary_operator":
            # ``name(params) :: def`` — and, like @callback, optionally
            # wrapped in ``... when var: bound``, so share the same
            # left-descent head resolver.
            return _spec_head_name(c, src), _collapse_ws(f"@{attr_name} " + _text(c, src))
        if c.type == "identifier":
            name = _text(c, src)
            return name, f"@{attr_name} {name}"
    return "?", f"@{attr_name} ?"


def _callback_name_sig(inner: Node, src: bytes) -> tuple[str, str]:
    """(name, sig) from a @callback spec.

    The spec's outermost node is a chain of ``binary_operator``s —
    ``name(args) :: ret`` optionally wrapped in ``... when guard``. The
    head is the deepest-left ``call`` (``fetch(id)``) or, for a
    paren-less zero-arity callback (``@callback ready :: t``), a bare
    ``identifier``.
    """
    args = _arguments_node(inner)
    if args is None:
        return "?", "?"
    named = args.named_children
    if not named:
        return "?", "?"
    spec = named[0]
    return _spec_head_name(spec, src), _collapse_ws(_text(spec, src))


def _spec_head_name(node: Node, src: bytes) -> str:
    """Name at the head of a ``::`` / ``when`` spec chain — shared by
    @callback and @type/@typep/@opaque, whose specs have the same shape
    (``name(params) :: def`` optionally wrapped in ``when``). Descend the
    left edge of nested ``binary_operator``s to the ``call`` head
    (``fetch(id)`` → ``fetch``) or, for the paren-less zero-arity form
    (``ready :: t``), the bare ``identifier``; ``?`` when neither."""
    cur: Optional[Node] = node
    while cur is not None and cur.type == "binary_operator":
        cur = cur.named_children[0] if cur.named_children else None
    if cur is None:
        return "?"
    if cur.type == "call":
        return _inner_fn_name(cur, src)
    if cur.type == "identifier":
        return _text(cur, src)
    return "?"


# ---------------------------------------------------------------------------
# Container types (defmodule, defprotocol, defimpl)
# ---------------------------------------------------------------------------

def _defmodule_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name = _first_alias_arg(node, src)
    do_blk = _do_block(node)
    children: list[Declaration] = []
    moduledoc: list[str] = []

    if do_blk is not None:
        body = do_blk.named_children
        for bn in body:
            if bn.type == "comment":
                continue
            if bn.type == "unary_operator":
                inner = _unary_inner_call(bn)
                if inner and _call_fn_name(inner, src) == "moduledoc":
                    doc_text = _attr_string_arg(inner, src)
                    if doc_text:
                        moduledoc = [doc_text]
            break
        _walk_body(body, src, scope="module", seen_defs=set(), out=children)

    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"defmodule {name}",
        docs=moduledoc,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


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
                if pair.type == "pair" and len(pair.named_children) >= 2:
                    if _text(pair.named_children[0], src).rstrip() == "for:":
                        for_type = _text(pair.named_children[1], src)
                        break

    name = f"{protocol}({for_type})" if for_type else protocol
    sig = f"defimpl {protocol}" + (f", for: {for_type}" if for_type else "")

    children: list[Declaration] = []
    do_blk = _do_block(node)
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
    seen_defs: set[tuple[str, str, int]],
) -> Optional[Declaration]:
    attr_marker, priv = _DEF_NAMES[fn_name]
    visibility = "private" if priv else ""

    head = _def_head(node, src)
    if head is None:
        return None
    name, sig, arity = head

    # Deduplicate only true multi-clause repeats: successive clauses of
    # one function share name, def-keyword, AND arity. The key keeps all
    # three so distinct functions are never collapsed — ``foo/1`` vs
    # ``foo/2`` (arity is part of a function's identity in Elixir) and
    # ``def foo`` vs ``defguard foo`` (separate constructs) each stay.
    key = (fn_name, name, arity)
    if key in seen_defs:
        return None
    seen_defs.add(key)

    return Declaration(
        kind=KIND_FUNCTION if scope == "top" else KIND_METHOD,
        name=name,
        signature=sig,
        attrs=[attr_marker] if attr_marker else [],
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
    )


def _def_head(node: Node, src: bytes) -> Optional[tuple[str, str, int]]:
    """(name, sig, arity) for a def-form call.

    Head shapes: ``call`` (foo(x)), ``binary_operator`` (foo(x) when guard),
    ``identifier`` (foo with no parens). Signature slice stops before the
    do_block or do: body; defdelegate with no body includes the full args.
    Arity is the parameter count of the head (0 for the paren-less form),
    used to keep distinct arities apart in clause deduplication.
    """
    args = _arguments_node(node)
    if args is None:
        return None
    named = args.named_children
    if not named:
        return None

    head_node = named[0]
    if head_node.type == "call":
        name = _inner_fn_name(head_node, src)
        arity = _call_arity(head_node)
    elif head_node.type == "binary_operator":
        left = head_node.named_children[0] if head_node.named_children else None
        name = _inner_fn_name(left, src) if left is not None else "?"
        arity = _call_arity(left) if left is not None and left.type == "call" else 0
    elif head_node.type == "identifier":
        name = _text(head_node, src)
        arity = 0
    else:
        return None

    do_blk = _do_block(node)
    if do_blk is not None:
        end = do_blk.start_byte
    elif _has_do_keyword(named, src):
        end = head_node.end_byte
    else:
        end = args.end_byte

    sig = _collapse_ws(src[node.start_byte:end].decode("utf8", errors="replace")).rstrip(",")
    return name, sig, arity


def _call_arity(call_node: Node) -> int:
    """Parameter count of a call head — the length of its argument list,
    0 when the call carries no ``arguments`` node (paren-less form)."""
    args = _arguments_node(call_node)
    return len(args.named_children) if args is not None else 0


def _has_do_keyword(arg_nodes: list[Node], src: bytes) -> bool:
    """True when a ``do:`` keyword pair is present (inline body form)."""
    for c in arg_nodes:
        if c.type == "keywords":
            for pair in c.named_children:
                if pair.type == "pair" and pair.named_children:
                    kw = pair.named_children[0]
                    if kw.type == "keyword" and _text(kw, src).rstrip() == "do:":
                        return True
    return False


def _inner_fn_name(node: Optional[Node], src: bytes) -> str:
    """First identifier child of a call node -- the function name."""
    if node is None:
        return "?"
    for c in node.named_children:
        if c.type == "identifier":
            return _text(c, src)
    return "?"


# ---------------------------------------------------------------------------
# defstruct / defexception
# ---------------------------------------------------------------------------

def _pair_key(pair: Node, src: bytes) -> Optional[str]:
    """Key name of a ``key: default`` pair. A ``keyword`` node spans
    ``name: `` — the trailing colon and the space before the value both
    need stripping. None for a non-pair node."""
    if pair.type == "pair" and pair.named_children:
        return _text(pair.named_children[0], src).rstrip(": ")
    return None


def _struct_to_decls(node: Node, src: bytes, fn_name: str) -> list[Declaration]:
    """One KIND_FIELD per declared struct key."""
    marker = _STRUCT_MACROS[fn_name]
    args = _arguments_node(node)
    if args is None:
        return []

    keys: list[str] = []
    for c in args.named_children:
        if c.type == "list":
            for item in c.named_children:
                if item.type == "atom":
                    keys.append(_text(item, src).lstrip(":"))
                elif item.type == "pair":
                    key = _pair_key(item, src)
                    if key is not None:
                        keys.append(key)
                # Trailing ``key: default`` entries in a bracketed list
                # (``defstruct [:a, b: 1]``) are grouped under a
                # ``keywords`` node, not as loose ``pair`` siblings.
                elif item.type == "keywords":
                    keys.extend(
                        k for p in item.named_children
                        if (k := _pair_key(p, src)) is not None
                    )
        elif c.type == "keywords":
            keys.extend(
                k for p in c.named_children
                if (k := _pair_key(p, src)) is not None
            )

    sl, el = node.start_point[0] + 1, node.end_point[0] + 1
    sb, eb = node.start_byte, node.end_byte
    return [
        Declaration(kind=KIND_FIELD, name=k, signature=k, attrs=[marker],
                    start_line=sl, end_line=el, start_byte=sb, end_byte=eb)
        for k in keys
    ]


# ---------------------------------------------------------------------------
# Callback-DSL blocks (KIND_BLOCK)
# ---------------------------------------------------------------------------

def _is_block_call(node: Node, src: bytes) -> bool:
    """True when a do_block call is a named DSL container (no dot receiver,
    first arg is a string or atom label)."""
    for c in node.named_children:
        if c.type == "dot":
            return False
        if c.type == "identifier":
            break
    args = _arguments_node(node)
    named = args.named_children if args is not None else []
    return bool(named) and named[0].type in _BLOCK_LABEL_TYPES


def _block_label(node: Node, src: bytes) -> str:
    """Strip quotes / leading colon from the first argument."""
    args = _arguments_node(node)
    named = args.named_children if args is not None else []
    if not named:
        return "?"
    text = _collapse_ws(_text(named[0], src))
    if len(text) >= 2 and text[0] in '"\'':
        text = text[1:-1] if text[-1] == text[0] else text
    elif text.startswith(":"):
        text = text[1:]
    return text or "?"


def _block_call_to_decl(node: Node, src: bytes, do_blk: Node, scope: str) -> Declaration:
    raw = src[node.start_byte:do_blk.start_byte].decode("utf8", errors="replace")
    sig = _collapse_ws(raw).rstrip().rstrip(",")
    if len(sig) > 140:
        sig = sig[:137] + "..."
    children: list[Declaration] = []
    # Forward the enclosing scope so a ``def`` nested in a DSL block that
    # sits inside a module is still classified METHOD, not FUNCTION —
    # matching the transparent-descent branch in ``_walk_body``.
    _walk_body(do_blk.named_children, src, scope=scope, seen_defs=set(), out=children)
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
    """Collect use/import/alias/require at module-body depth.
    Descends into defmodule / defprotocol / defimpl do_blocks — each
    opens a module scope whose top-level imports are source-true."""
    for node in nodes:
        if node.type != "call":
            continue
        fn = _call_fn_name(node, src)
        if fn in _IMPORT_MACROS:
            _emit_import(node, src, fn, out)
        elif fn in _MODULE_CONTAINER_MACROS:
            do_blk = _do_block(node)
            if do_blk is not None:
                _collect_imports(do_blk.named_children, src, out)


def _emit_import(node: Node, src: bytes, fn_name: str, out: list[str]) -> None:
    """Append source-true import text; expands ``alias MyApp.{Bar, Baz}``."""
    args = _arguments_node(node)
    if args is None:
        return
    named = args.named_children
    if not named:
        return

    first = named[0]
    if fn_name == "alias" and first.type == "dot":
        children = first.named_children
        if len(children) >= 2 and children[1].type == "tuple":
            prefix = _text(children[0], src)
            for alias_node in children[1].named_children:
                if alias_node.type == "alias":
                    out.append(f"alias {prefix}.{_text(alias_node, src)}")
            return

    out.append(f"{fn_name} {_text(first, src)}")


def _count_conditional_imports(root: Node, src: bytes) -> int:
    """Count use/import/alias/require inside function bodies (do_blocks of defs)."""
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, inside_fn = stack.pop()
        if node.type == "call" and inside_fn:
            fn = _call_fn_name(node, src)
            if fn in _IMPORT_MACROS:
                count += 1
        new_inside = inside_fn
        if node.type == "do_block" and node.parent is not None:
            if node.parent.type == "call" and _call_fn_name(node.parent, src) in _DEF_NAMES:
                new_inside = True
        for c in node.children:
            stack.append((c, new_inside))
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_fn_name(node: Node, src: bytes) -> Optional[str]:
    """Identifier of a call's function; None for dot calls."""
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
    for c in node.named_children:
        if c.type == "call":
            return c
    return None


def _first_alias_arg(node: Node, src: bytes) -> str:
    """First alias/atom argument -- used for defmodule / defprotocol names."""
    args = _arguments_node(node)
    if args is None:
        return "?"
    for c in args.named_children:
        if c.type in ("alias", "atom"):
            return _text(c, src)
    return "?"


def _string_content(node: Node, src: bytes) -> str:
    """Inner text of a string node (quoted_content child or stripped raw)."""
    for c in node.named_children:
        if c.type == "quoted_content":
            return _text(c, src)
    raw = _text(node, src)
    return raw[1:-1] if len(raw) >= 2 and raw[0] in "\"'" else raw


def _doc_start_byte(doc_node: Node, pending: list[str], src: bytes) -> int:
    """Walk backward from doc_node to the start of the leading doc block."""
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
