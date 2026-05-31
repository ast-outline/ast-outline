"""SQL adapter (.sql).

Targets DDL — the structural skeleton of a database — over DML or
imperative procedure bodies. The killer use case is letting an agent
read a multi-thousand-line ``schema.sql`` (a ``pg_dump`` artefact, a
Flyway/Rails migration set, a dbt project) without loading the whole
file. ``outline`` returns the full table list with columns and types;
``digest`` returns the table list plus a column-count tag per file
(use ``--include-fields`` for the columns themselves — they're hidden
in digest by default, in line with how digest treats fields in every
other language).

Surfaced constructs
-------------------

* ``CREATE TABLE`` → :data:`KIND_TABLE`, with each column emitted as a
  :data:`KIND_FIELD` child whose signature is the source-true column
  definition (``id INTEGER PRIMARY KEY NOT NULL``).
* ``CREATE VIEW`` / ``CREATE MATERIALIZED VIEW`` → :data:`KIND_VIEW`;
  ``native_kind`` distinguishes ``view`` vs ``materialized view``.
* ``CREATE FUNCTION`` → :data:`KIND_FUNCTION`. The header (name +
  parameter list + return type) parses cleanly; the PL/pgSQL body
  inside ``AS $$ … $$`` is ignored for outline purposes.
* ``CREATE TRIGGER`` → :data:`KIND_FUNCTION` + ``native_kind="trigger"``.
* ``CREATE TYPE foo AS (…)`` (composite) → :data:`KIND_RECORD` with
  the composite's fields as :data:`KIND_FIELD` children.
* ``CREATE TYPE foo AS ENUM (…)`` → :data:`KIND_ENUM` with literal
  members as :data:`KIND_ENUM_MEMBER` children.
* ``CREATE INDEX`` → :data:`KIND_FIELD` + ``native_kind="index"``.
* ``CREATE SEQUENCE`` → :data:`KIND_FIELD` + ``native_kind="sequence"``.
* ``CREATE SCHEMA`` → :data:`KIND_NAMESPACE`. Schemas appear as
  siblings of the objects in a file, not parents — SQL uses
  qualified-name references rather than lexical scoping, so an agent
  asking ``find_symbols("users")`` should match regardless of which
  ``CREATE SCHEMA`` line precedes it.
* ``CREATE EXTENSION`` → ``imports`` list, source-true.

Surfaced via regex fallback
---------------------------

The DerekStride grammar emits ``ERROR`` nodes for four constructs we
care about: ``CREATE PROCEDURE``, ``CREATE DOMAIN``, ``LOAD``, and
``IMPORT FOREIGN SCHEMA``. Migrating to ``tree-sitter-postgres`` was
evaluated and rejected — that grammar parses these but introduces a
worse regression: consecutive ``CREATE FUNCTION`` statements merge
into a single AST node, so the second function and beyond disappear.

Instead, after the AST walk we run a small regex fallback over the
source bytes, anchored at line starts and gated by AST-derived skip
ranges (we don't scan inside ``comment`` / ``marginalia`` /
``literal`` / ``block`` subtrees). Each match emits the same
:class:`Declaration` shape its AST-parsed cousin would produce:

* ``CREATE [OR REPLACE] PROCEDURE name(args)`` → :data:`KIND_FUNCTION`
  + ``native_kind="procedure"``.
* ``CREATE DOMAIN name AS type`` → :data:`KIND_FIELD`
  + ``native_kind="domain"``.
* ``LOAD 'lib'`` → ``imports`` list.
* ``IMPORT FOREIGN SCHEMA name FROM SERVER server INTO target``
  → ``imports`` list.

The fallback only sees the signature line; bodies aren't inspected.
That's the same scope outline gives every other callable.

Not surfaced (deliberate)
-------------------------

* ``ALTER`` / ``DROP`` / ``GRANT`` / ``REVOKE`` — modifications and
  permissions, not declarations. Migration files containing only
  ``ALTER`` produce a sparse outline; that's correct behaviour, not
  a bug.
* CTEs (``WITH x AS (…) SELECT …``) — deferred; named subqueries are
  statement-scoped, not file-level declarations.

Doc-comment attachment
----------------------

The grammar emits two top-level node types for comments:
``comment`` for ``-- line`` runs and ``marginalia`` for
``/* block */`` content. Both appear as preceding siblings of the
``statement`` node they document; we walk back over contiguous
comment / marginalia nodes (no blank line in between) and attach them
to the next statement.

Dialect coverage
----------------

DerekStride's ``tree-sitter-sql`` is generic ANSI + PostgreSQL with
partial coverage of MySQL / SQLite / T-SQL DDL. MSSQL ``[bracketed]``
identifiers and some MySQL ``ENGINE=InnoDB`` table options will
``ERROR``, inflating ``error_count``. That's an honest signal — we
don't pre-process the source. Realistic ``pg_dump`` artefacts (with
``SET …`` headers and ``ALTER TABLE … ADD CONSTRAINT`` follow-ups)
parse with ``error_count > 0`` but every ``CREATE`` still surfaces
as a clean ``statement`` node; the ERROR noise is around the edges.
"""
from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Optional

import tree_sitter_sql as tssql
from tree_sitter import Language, Node, Parser

from ..core import (
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_NAMESPACE,
    KIND_RECORD,
    KIND_TABLE,
    KIND_VIEW,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tssql.language())
_PARSER = Parser(_LANGUAGE)


# Top-level wrappers we descend through to reach the actual create_*
# node. Every CREATE statement comes wrapped in a ``statement`` node.
_STATEMENT_WRAPPER = "statement"

# Comment-bearing node types. ``comment`` is ``-- line``,
# ``marginalia`` is ``/* block */``. Both appear as direct children
# of ``program`` and we walk back from a statement to collect them.
_COMMENT_TYPES = {"comment", "marginalia"}


class SqlAdapter:
    language_name = "sql"
    extensions = {".sql"}
    # SQL declarations lead with multi-word DDL (`CREATE TABLE foo`), not
    # a single keyword + identifier, so the keyword-strip shape doesn't
    # apply.
    definition_keywords = frozenset()

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        imports: list[str] = []
        _walk_top_level(tree.root_node, src, decls, imports)

        # Cheap pre-check: does the source even contain any of the
        # four fallback keywords (PROCEDURE / DOMAIN / LOAD / IMPORT
        # FOREIGN SCHEMA)? A bytes-level substring scan is O(n) over
        # the source but at C speed; on a clean PG schema file with
        # zero hits we skip the much more expensive ``_collect_skip_
        # ranges`` AST walk entirely. Realistic ``pg_dump`` output and
        # ORM-generated schemas typically have none of these
        # constructs, so this short-circuit pays off on the hot path.
        line_starts = _build_line_starts(src)
        if _has_fallback_keyword(src):
            skip_ranges = _collect_skip_ranges(tree.root_node)
            _scan_unparsed_constructs(
                src, skip_ranges, line_starts, decls, imports
            )
            decls.sort(key=lambda d: d.start_byte)
        else:
            skip_ranges = []

        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=len(line_starts),
            declarations=decls,
            error_count=_count_meaningful_errors(tree.root_node, skip_ranges),
            imports=imports,
        )


# ---------------------------------------------------------------------------
# Top-level walk
# ---------------------------------------------------------------------------


def _walk_top_level(
    root: Node, src: bytes, out: list[Declaration], imports: list[str]
) -> None:
    """Iterate ``program``'s named children. Each ``statement`` wraps a
    single ``create_*`` node; comments and marginalia are siblings we
    walk back to from the next statement."""
    children = root.named_children
    for i, child in enumerate(children):
        if child.type != _STATEMENT_WRAPPER:
            continue
        inner = _first_named_child(child)
        if inner is None:
            continue
        decl_or_import = _node_to_decl(inner, src, imports)
        if decl_or_import is None:
            continue
        # Attach contiguous preceding comment / marginalia siblings as
        # docs. We walk back from the statement (not the inner create_*
        # node) because comments are program-level siblings.
        docs = _collect_preceding_docs(children, i, src)
        if docs:
            decl_or_import.docs = docs
            decl_or_import.doc_start_byte = _earliest_comment_byte(children, i)
        out.append(decl_or_import)


def _node_to_decl(
    node: Node, src: bytes, imports: list[str]
) -> Optional[Declaration]:
    """Dispatch a top-level ``create_*`` node to its handler.

    ``CREATE EXTENSION`` is collected into ``imports`` and returns
    ``None`` so the walker skips it as a declaration.
    """
    t = node.type
    if t == "create_table":
        return _table_to_decl(node, src)
    if t == "create_view":
        return _view_to_decl(node, src, materialized=False)
    if t == "create_materialized_view":
        return _view_to_decl(node, src, materialized=True)
    if t == "create_function":
        return _function_to_decl(node, src)
    if t == "create_trigger":
        return _trigger_to_decl(node, src)
    if t == "create_type":
        return _type_to_decl(node, src)
    if t == "create_index":
        return _index_to_decl(node, src)
    if t == "create_sequence":
        return _sequence_to_decl(node, src)
    if t == "create_schema":
        return _schema_to_decl(node, src)
    if t == "create_extension":
        stmt = _statement_text(node, src)
        if stmt:
            imports.append(stmt)
        return None
    return None


# ---------------------------------------------------------------------------
# Per-construct handlers
# ---------------------------------------------------------------------------


def _table_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = _first_named_child_of_type(node, "object_reference")
    if name_node is None:
        return None
    qualified, unqualified = _object_reference_names(name_node, src)
    columns = _columns_for_block(
        _first_named_child_of_type(node, "column_definitions"), src
    )
    signature = f"CREATE TABLE {qualified}"
    return Declaration(
        kind=KIND_TABLE,
        name=unqualified,
        signature=signature,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=columns,
    )


def _view_to_decl(
    node: Node, src: bytes, *, materialized: bool
) -> Optional[Declaration]:
    name_node = _first_named_child_of_type(node, "object_reference")
    if name_node is None:
        return None
    qualified, unqualified = _object_reference_names(name_node, src)
    keyword = "CREATE MATERIALIZED VIEW" if materialized else "CREATE VIEW"
    native = "materialized view" if materialized else "view"
    return Declaration(
        kind=KIND_VIEW,
        name=unqualified,
        signature=f"{keyword} {qualified}",
        native_kind=native,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _function_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = _first_named_child_of_type(node, "object_reference")
    if name_node is None:
        return None
    qualified, unqualified = _object_reference_names(name_node, src)

    args_node = _first_named_child_of_type(node, "function_arguments")
    args_text = _collapse_ws(_text(args_node, src)) if args_node is not None else "()"
    if not args_text.startswith("("):
        # Older grammar variants may surface the args without parens
        # — wrap defensively so the digest stays uniform.
        args_text = f"({args_text})"

    returns = _function_return_text(node, src)
    signature = f"CREATE FUNCTION {qualified}{args_text}"
    if returns:
        signature += f" RETURNS {returns}"

    return Declaration(
        kind=KIND_FUNCTION,
        name=unqualified,
        signature=signature,
        native_kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _trigger_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Trigger structure: first ``object_reference`` is the trigger name;
    second is the table the trigger is bound to. We surface only the
    name + a compact signature line — the timing/event/target
    information is implicit in the source slice agents can ``show``.
    """
    refs = [c for c in node.named_children if c.type == "object_reference"]
    if not refs:
        return None
    qualified, unqualified = _object_reference_names(refs[0], src)
    target = ""
    if len(refs) >= 2:
        target_qualified, _ = _object_reference_names(refs[1], src)
        target = f" ON {target_qualified}"
    return Declaration(
        kind=KIND_FUNCTION,
        name=unqualified,
        signature=f"CREATE TRIGGER {qualified}{target}",
        native_kind="trigger",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _type_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """``CREATE TYPE`` covers two distinct shapes:

    * Composite: ``CREATE TYPE addr AS (street TEXT, …)`` →
      ``KIND_RECORD`` with ``column_definitions`` as field children.
    * Enum: ``CREATE TYPE order_status AS ENUM ('a', 'b', …)`` →
      ``KIND_ENUM`` with ``enum_elements`` as member children.

    We pick the shape from which structural child the grammar emitted.
    """
    name_node = _first_named_child_of_type(node, "object_reference")
    if name_node is None:
        return None
    qualified, unqualified = _object_reference_names(name_node, src)

    enum_node = _first_named_child_of_type(node, "enum_elements")
    if enum_node is not None:
        members: list[Declaration] = []
        for c in enum_node.named_children:
            if c.type != "literal":
                continue
            raw = _text(c, src)
            label = raw.strip().strip("'\"")
            members.append(
                Declaration(
                    kind=KIND_ENUM_MEMBER,
                    name=label,
                    signature=raw,
                    start_line=c.start_point[0] + 1,
                    end_line=c.end_point[0] + 1,
                    start_byte=c.start_byte,
                    end_byte=c.end_byte,
                )
            )
        return Declaration(
            kind=KIND_ENUM,
            name=unqualified,
            signature=f"CREATE TYPE {qualified} AS ENUM",
            native_kind="enum",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            children=members,
        )

    cols_node = _first_named_child_of_type(node, "column_definitions")
    columns = _columns_for_block(cols_node, src)
    return Declaration(
        kind=KIND_RECORD,
        name=unqualified,
        signature=f"CREATE TYPE {qualified}",
        native_kind="type",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=columns,
    )


def _index_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """``CREATE [UNIQUE] INDEX name ON table(cols)``.

    The index name is a bare ``identifier`` (NOT wrapped in
    ``object_reference``) per the grammar. Signature is the full
    statement collapsed onto one line so the agent sees uniqueness +
    target table + columns at a glance.
    """
    name_node = _first_named_child_of_type(node, "identifier")
    if name_node is None:
        return None
    name = _text(name_node, src)
    signature = _collapse_ws(_text(node, src))
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=signature,
        native_kind="index",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _sequence_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = _first_named_child_of_type(node, "object_reference")
    if name_node is None:
        return None
    _, unqualified = _object_reference_names(name_node, src)
    signature = _collapse_ws(_text(node, src))
    return Declaration(
        kind=KIND_FIELD,
        name=unqualified,
        signature=signature,
        native_kind="sequence",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _schema_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Schemas are emitted as flat top-level :data:`KIND_NAMESPACE`
    decls with no children — the schema and the objects living "in"
    it are file-order siblings. SQL uses qualified-name references,
    not lexical scoping, so an agent asking ``find_symbols("users")``
    should match the table regardless of file ordering."""
    name_node = _first_named_child_of_type(node, "identifier")
    if name_node is None:
        return None
    name = _text(name_node, src)
    return Declaration(
        kind=KIND_NAMESPACE,
        name=name,
        signature=f"CREATE SCHEMA {name}",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# ---------------------------------------------------------------------------
# Column extraction (shared between CREATE TABLE and CREATE TYPE composite)
# ---------------------------------------------------------------------------


def _columns_for_block(
    block: Optional[Node], src: bytes
) -> list[Declaration]:
    """Walk a ``column_definitions`` block and emit one
    :data:`KIND_FIELD` per ``column_definition`` child. Signature is
    the source-true column line, whitespace-collapsed so a multi-line
    column definition (``email TEXT NOT NULL\\n  UNIQUE``) reads as one
    line in outline."""
    if block is None:
        return []
    out: list[Declaration] = []
    for c in block.named_children:
        if c.type != "column_definition":
            continue
        col_name = _column_name(c, src)
        if not col_name:
            continue
        signature = _collapse_ws(_text(c, src))
        out.append(
            Declaration(
                kind=KIND_FIELD,
                name=col_name,
                signature=signature,
                start_line=c.start_point[0] + 1,
                end_line=c.end_point[0] + 1,
                start_byte=c.start_byte,
                end_byte=c.end_byte,
            )
        )
    return out


def _column_name(col: Node, src: bytes) -> str:
    """Pull the column name out of a ``column_definition``.

    Quoted names like ``"Email Address"`` parse as ``literal`` rather
    than ``identifier`` in this grammar (the column name slot accepts
    either). Try the identifier first; fall back to the first literal
    if that's where the name landed.
    """
    for c in col.named_children:
        if c.type == "identifier":
            return _text(c, src)
    for c in col.named_children:
        if c.type == "literal":
            return _text(c, src)
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _object_reference_names(node: Node, src: bytes) -> tuple[str, str]:
    """Return ``(qualified, unqualified)`` for an ``object_reference``.

    A bare reference (``users``) returns ``("users", "users")``. A
    qualified reference (``analytics.events``) returns
    ``("analytics.events", "events")``. The unqualified form becomes
    the declaration's ``name``; the qualified form is rendered into
    the ``signature`` so the agent sees the full source-true name in
    outline / digest.

    Schema-qualified queries (``find_symbols("analytics.events")``)
    are NOT supported — ``find_symbols`` splits queries on ``.`` and
    walks down through ancestor declarations, but SQL schemas are
    siblings of the objects "in" them rather than parents (``CREATE
    SCHEMA`` doesn't lexically wrap subsequent ``CREATE TABLE``
    statements). Agents should query by the unqualified table name;
    the schema prefix is visible in the rendered signature.

    Quoted identifiers preserve their quotes (``"User Profile"``).
    """
    parts: list[str] = []
    for c in node.named_children:
        if c.type in ("identifier", "literal"):
            parts.append(_text(c, src))
    if not parts:
        return "", ""
    qualified = ".".join(parts)
    unqualified = parts[-1]
    return qualified, unqualified


def _function_return_text(node: Node, src: bytes) -> str:
    """Extract the return-type fragment of a ``CREATE FUNCTION``.

    The grammar puts the type node immediately after ``keyword_returns``
    among the function's named children. We scan through the children
    looking for the keyword's anonymous form and pick whatever named
    node follows. Returns ``""`` if no RETURNS clause is present (e.g.
    a function returning ``void`` implicitly via SETOF, or a syntax
    variant the grammar didn't surface).
    """
    found_returns = False
    for c in node.children:
        if c.type == "keyword_returns":
            found_returns = True
            continue
        if found_returns and c.is_named:
            # Skip the function body / language clause if RETURNS was
            # missing and we landed on them.
            if c.type in ("function_body", "function_language"):
                return ""
            return _collapse_ws(_text(c, src))
    return ""


def _statement_text(node: Node, src: bytes) -> str:
    """Source-true single-line text for a top-level statement (used for
    the ``imports`` list). Trailing semicolon stripped — matches the
    Ruby/Python adapters which store ``require "x"`` without the
    trailing punctuation."""
    text = _collapse_ws(_text(node, src))
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _collect_preceding_docs(
    siblings: list[Node], idx: int, src: bytes
) -> list[str]:
    """Walk back from ``siblings[idx]`` over contiguous comment /
    marginalia siblings.

    "Contiguous" means each comment sits on the line immediately above
    the next item (or shares a line span for multi-line block
    comments). A blank line breaks the chain — that's the user's
    paragraph separator and we honour it.

    Returns lines in source order (top-to-bottom) so a multi-line
    ``/* … */`` block reads as the user wrote it.
    """
    blocks: list[list[str]] = []
    cur_line = siblings[idx].start_point[0]
    j = idx - 1
    while j >= 0:
        prev = siblings[j]
        if prev.type not in _COMMENT_TYPES:
            break
        prev_end_line = prev.end_point[0]
        if prev_end_line < cur_line - 1:
            break
        blocks.append(_text(prev, src).splitlines())
        cur_line = prev.start_point[0]
        j -= 1
    # ``blocks`` collected newest-first; reverse to source order, then
    # flatten preserving each block's internal line order.
    out: list[str] = []
    for block in reversed(blocks):
        out.extend(block)
    return out


def _earliest_comment_byte(siblings: list[Node], idx: int) -> int:
    """Byte offset of the earliest contiguous comment / marginalia
    sibling preceding ``siblings[idx]``, or 0 if none. Used as
    ``doc_start_byte`` so ``show`` includes the doc in its source slice."""
    cur_line = siblings[idx].start_point[0]
    j = idx - 1
    earliest = 0
    while j >= 0:
        prev = siblings[j]
        if prev.type not in _COMMENT_TYPES:
            break
        prev_end_line = prev.end_point[0]
        if prev_end_line < cur_line - 1:
            break
        earliest = prev.start_byte
        cur_line = prev.start_point[0]
        j -= 1
    return earliest


def _first_named_child(node: Node) -> Optional[Node]:
    for c in node.named_children:
        return c
    return None


def _first_named_child_of_type(node: Node, type_name: str) -> Optional[Node]:
    for c in node.named_children:
        if c.type == type_name:
            return c
    return None


def _text(node: Optional[Node], src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _collapse_ws(s: str) -> str:
    """Collapse consecutive whitespace (including newlines) into single
    spaces. Used everywhere a multi-line source fragment must render as
    a one-line outline signature."""
    return " ".join(s.split())


def _count_meaningful_errors(
    root: Node, skip_ranges: list[tuple[int, int]]
) -> int:
    """Count ``ERROR`` / ``MISSING`` nodes outside opaque regions and
    outside the ranges the regex fallback successfully recovered.

    Two categories of "noise" are filtered out:

    * **PL/pgSQL bodies** inside ``CREATE FUNCTION … AS $$ … $$`` use
      syntax (``:=`` assignment, ``IF … THEN … END IF;``, exception
      handlers) the grammar can't parse. Errors deep inside don't
      reflect malformed top-level DDL; the function header still
      extracts cleanly.
    * **Regex-recovered ranges** — when the fallback successfully
      pulls a ``CREATE PROCEDURE`` / ``CREATE DOMAIN`` / ``LOAD`` /
      ``IMPORT FOREIGN SCHEMA`` out of an ERROR node, the user got
      what they wanted. Counting that ERROR would falsely brand the
      file as broken when the construct is actually surfaced.
    """
    if not root.has_error:
        return 0
    total = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        n, in_body = stack.pop()
        # Pruning: a subtree with no ERROR/MISSING anywhere can't
        # contribute. tree-sitter caches this on every node; checking
        # is O(1). Skipping whole clean subtrees turns the walk from
        # "visit every node in a 100k-line file" into "visit only the
        # error sites" — typically two orders of magnitude smaller.
        if not n.has_error:
            continue
        if (
            not in_body
            and (n.type == "ERROR" or n.is_missing)
            and not _overlaps_any_range(n.start_byte, n.end_byte, skip_ranges)
        ):
            total += 1
        descend_in_body = in_body or n.type == "function_body"
        for c in n.children:
            stack.append((c, descend_in_body))
    return total


# ---------------------------------------------------------------------------
# Regex fallback for grammar-unsupported constructs
# ---------------------------------------------------------------------------


# Each pattern is anchored at line start (with optional indentation) so we
# don't pick up CREATE PROCEDURE references buried inside a multi-statement
# line or a wider expression. Case-insensitive — ``create procedure foo()``
# is just as valid SQL as ``CREATE PROCEDURE FOO()``.
#
# We capture the structural pieces the outline cares about (name + args,
# name + type, etc.) and stop at the first natural boundary. Bodies are
# intentionally NOT captured — outline shows headers, full source is one
# ``show`` away.

# An identifier in the grammar's eyes: word chars, dots (schema-qualified),
# or a quoted literal. Used inside the patterns below.
_IDENT = r'(?:"[^"]+"|[\w.]+)'

# CREATE [OR REPLACE] PROCEDURE [schema.]name([args])
_PROCEDURE_RE = re.compile(
    rb'(?im)^[ \t]*CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+'
    + _IDENT.encode()
    + rb'\s*(?:\([^)]*\))?'
)
_PROCEDURE_NAME_RE = re.compile(
    rb'(?im)CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(' + _IDENT.encode() + rb')'
)
_PROCEDURE_ARGS_RE = re.compile(rb'\(([^)]*)\)', re.DOTALL)

# CREATE DOMAIN name [AS] type [CHECK (...)] [...]
# Greedy through to end-of-statement (`;` or end of line, whichever
# comes first). Most domains fit on one line, so trailing `CHECK`
# constraints surface in the signature too. Multi-line CHECK clauses
# get only their first line — that's a fine tradeoff for outline
# display, and ``show`` covers the full source if needed.
_DOMAIN_RE = re.compile(
    rb'(?im)^[ \t]*CREATE\s+DOMAIN\s+'
    + _IDENT.encode()
    + rb'(?:\s+AS)?\s+[^;\n]+'
)
_DOMAIN_NAME_RE = re.compile(
    rb'(?im)CREATE\s+DOMAIN\s+(' + _IDENT.encode() + rb')'
)

# LOAD 'lib_name'
_LOAD_RE = re.compile(rb"(?im)^[ \t]*LOAD\s+'[^']+'")

# IMPORT FOREIGN SCHEMA name [LIMIT TO|EXCEPT (...)] FROM SERVER server INTO target
# Per PG syntax, the optional LIMIT/EXCEPT clause sits BEFORE FROM,
# not after.
_IFS_RE = re.compile(
    rb'(?im)^[ \t]*IMPORT\s+FOREIGN\s+SCHEMA\s+'
    + _IDENT.encode()
    + rb'(?:\s+(?:LIMIT\s+TO|EXCEPT)\s*\([^)]*\))?'
    + rb'\s+FROM\s+SERVER\s+'
    + _IDENT.encode()
    + rb'\s+INTO\s+'
    + _IDENT.encode()
)

# CREATE TABLE name PARTITION OF parent ...
# Modern PostgreSQL declarative partitioning. The grammar errors on
# the entire ``PARTITION OF`` syntax, so partition child tables vanish
# from the outline without this fallback. We capture the table name
# and the parent it inherits from; column structure is implicit (the
# partition shares its parent's columns), so no children are emitted.
_PARTITION_TABLE_RE = re.compile(
    rb'(?im)^[ \t]*CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    + _IDENT.encode()
    + rb'\s+PARTITION\s+OF\s+'
    + _IDENT.encode()
)
_PARTITION_NAME_RE = re.compile(
    rb'(?im)CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    + rb'(' + _IDENT.encode() + rb')'
    + rb'\s+PARTITION\s+OF\s+'
    + rb'(' + _IDENT.encode() + rb')'
)

# CREATE [OR REPLACE] FUNCTION — picks up functions the AST missed
# (e.g. ``SECURITY DEFINER`` modifiers, exotic option ordering). To
# avoid double-extracting functions the AST already produced, the
# fallback gates on AST-emitted-function byte ranges in addition to
# the standard skip ranges.
_FUNCTION_FALLBACK_RE = re.compile(
    rb'(?im)^[ \t]*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+'
    + _IDENT.encode()
    + rb'\s*\([^)]*\)'
)
_FUNCTION_FALLBACK_NAME_RE = re.compile(
    rb'(?im)CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(' + _IDENT.encode() + rb')'
)
_FUNCTION_FALLBACK_ARGS_RE = re.compile(rb'\(([^)]*)\)', re.DOTALL)


# AST node types whose subtrees the regex pass MUST avoid:
# - ``comment`` / ``marginalia``: ``-- line`` and ``/* block */`` text
# - ``literal``: SQL string literals (``'CREATE PROCEDURE in_string'``)
# - ``block``: PL/pgSQL function bodies — example procedures inside a
#   body's comments would otherwise produce false positives.
_REGEX_SKIP_NODE_TYPES = {"comment", "marginalia", "literal", "block"}


# Bytes-level substring fingerprints used as a cheap pre-check
# (``bytes.find``) before paying for the AST-wide skip-range walk.
# Case-insensitive matching is irrelevant — the keywords are
# upper-case in 99.9% of real SQL, and a false negative just means we
# skip the fallback (the regex is the real arbiter when we do run
# it). Both upper and lower variants would double the substring work
# for negligible additional coverage, so we screen on upper-case
# only.
#
# ``PARTITION OF`` triggers the partition-child recovery; it's the
# rarest fingerprint by far in non-PG dialects so the lazy
# short-circuit still kicks in for typical files. ``SECURITY DEFINER``
# triggers the FUNCTION fallback for the niche modifier ordering the
# upstream grammar errors on.
_FALLBACK_FINGERPRINTS = (
    b"PROCEDURE",
    b"DOMAIN",
    b"LOAD",
    b"IMPORT FOREIGN",
    b"PARTITION OF",
    b"SECURITY DEFINER",
)


def _has_fallback_keyword(src: bytes) -> bool:
    """``True`` if at least one fallback construct's signature
    keyword appears anywhere in the source — a permissive screen, the
    regex pass enforces line-anchoring and skip-range gating later."""
    for fp in _FALLBACK_FINGERPRINTS:
        if fp in src:
            return True
    return False


def _collect_skip_ranges(root: Node) -> list[tuple[int, int]]:
    """Walk the AST once and collect byte ranges the regex fallback
    must skip. Whole subtrees are excluded — once we hit a skip-type
    node we don't descend into it."""
    out: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        n = stack.pop()
        if n.type in _REGEX_SKIP_NODE_TYPES:
            out.append((n.start_byte, n.end_byte))
            continue
        stack.extend(n.children)
    out.sort()
    return out


def _in_any_range(offset: int, ranges: list[tuple[int, int]]) -> bool:
    """True if ``offset`` falls inside any (start, end) range.

    Uses ``bisect`` to find the rightmost range whose ``start <=
    offset`` in O(log N), then checks ``offset < end``. ``ranges``
    must be sorted by start byte (the producers in this module sort
    before passing in). For pathological inputs with many overlapping
    ranges only the rightmost-starting one is checked; this is fine
    here because skip-region producers (``_collect_skip_ranges``)
    emit non-overlapping subtrees.
    """
    if not ranges:
        return False
    # Find rightmost range with start <= offset.
    idx = bisect.bisect_right(ranges, (offset, float("inf"))) - 1
    if idx < 0:
        return False
    s, e = ranges[idx]
    return s <= offset < e


def _overlaps_any_range(
    start: int, end: int, ranges: list[tuple[int, int]]
) -> bool:
    """True if ``[start, end)`` overlaps any range in ``ranges``.

    Uses ``bisect`` for the same O(log N) speedup as
    :func:`_in_any_range`. We find the rightmost range with
    ``range.start <= end - 1`` and check whether its ``range.end``
    extends past ``start`` — a single candidate suffices because
    ranges are non-overlapping after the producers sort them.
    """
    if not ranges:
        return False
    idx = bisect.bisect_right(ranges, (end - 1, float("inf"))) - 1
    if idx < 0:
        return False
    s, e = ranges[idx]
    return s < end and start < e


def _build_line_starts(src: bytes) -> list[int]:
    """Build a sorted list of byte offsets where each 1-based line
    starts. Index 0 holds 0 (line 1 begins at byte 0); subsequent
    entries hold the byte right after each ``\\n``.

    The list is the index ``_line_for_byte`` consults to map a byte
    offset to a line number in O(log n). Without precomputation we
    were calling ``src[:offset].count(b'\\n')`` per match, which is
    O(offset) — quadratic in file size for a large schema dump.
    """
    starts = [0]
    pos = 0
    while True:
        nl = src.find(b"\n", pos)
        if nl < 0:
            break
        starts.append(nl + 1)
        pos = nl + 1
    return starts


def _line_for_byte(line_starts: list[int], offset: int) -> int:
    """1-based line number of a byte position. O(log n) via bisect
    over a precomputed line-start index."""
    return bisect.bisect_right(line_starts, offset)


def _scan_unparsed_constructs(
    src: bytes,
    skip_ranges: list[tuple[int, int]],
    line_starts: list[int],
    decls: list[Declaration],
    imports: list[str],
) -> None:
    """Run the regex fallback. Each match outside a skip range emits
    a :class:`Declaration` (procedures, domains, partition tables,
    AST-missed functions) or appends to ``imports`` (LOAD, IMPORT
    FOREIGN SCHEMA).

    Skip ranges grow with each successful match so
    ``_count_meaningful_errors`` won't count ERRORs that overlap a
    surfaced construct. The FUNCTION fallback additionally treats
    AST-emitted declaration ranges as skip zones — without this, every
    cleanly-parsed function would be re-extracted by the regex pass.
    We re-sort once at the end rather than maintaining sorted order
    through inserts.
    """
    # Snapshot AST-emitted declaration byte ranges so the FUNCTION
    # fallback skips them. Other fallback patterns don't need this:
    # PROCEDURE / DOMAIN / PARTITION are constructs the AST never
    # produced a decl for, so there's no double-extract risk.
    ast_decl_ranges = [(d.start_byte, d.end_byte) for d in decls]
    ast_decl_ranges.sort()

    for m in _PROCEDURE_RE.finditer(src):
        if _in_any_range(m.start(), skip_ranges):
            continue
        decls.append(_procedure_decl_from_match(src, m, line_starts))
        skip_ranges.append((m.start(), m.end()))

    for m in _DOMAIN_RE.finditer(src):
        if _in_any_range(m.start(), skip_ranges):
            continue
        decls.append(_domain_decl_from_match(src, m, line_starts))
        skip_ranges.append((m.start(), m.end()))

    for m in _PARTITION_TABLE_RE.finditer(src):
        if _in_any_range(m.start(), skip_ranges):
            continue
        decls.append(_partition_table_decl_from_match(src, m, line_starts))
        skip_ranges.append((m.start(), m.end()))

    for m in _FUNCTION_FALLBACK_RE.finditer(src):
        # Two-stage gating for FUNCTION: skip if inside a comment /
        # literal / block (standard skip ranges), AND skip if the
        # match overlaps an AST-emitted declaration (the AST already
        # produced this function correctly).
        if _in_any_range(m.start(), skip_ranges):
            continue
        if _in_any_range(m.start(), ast_decl_ranges):
            continue
        decls.append(_function_fallback_decl_from_match(src, m, line_starts))
        skip_ranges.append((m.start(), m.end()))

    for m in _LOAD_RE.finditer(src):
        if _in_any_range(m.start(), skip_ranges):
            continue
        imports.append(_collapse_ws(_text_of_match(src, m)))
        skip_ranges.append((m.start(), m.end()))

    for m in _IFS_RE.finditer(src):
        if _in_any_range(m.start(), skip_ranges):
            continue
        imports.append(_collapse_ws(_text_of_match(src, m)))
        skip_ranges.append((m.start(), m.end()))

    skip_ranges.sort()


def _text_of_match(src: bytes, match: "re.Match[bytes]") -> str:
    return src[match.start():match.end()].decode("utf-8", errors="replace")


def _procedure_decl_from_match(
    src: bytes, match: "re.Match[bytes]", line_starts: list[int]
) -> Declaration:
    """Build a :data:`KIND_FUNCTION` ``Declaration`` from a regex hit.

    Name is the identifier after ``PROCEDURE``; args list is the
    parenthesised group when present (procedures with no args are
    legal). Source-line position comes from the match's start byte.
    """
    name_m = _PROCEDURE_NAME_RE.search(match.group(0))
    name = name_m.group(1).decode("utf-8") if name_m else "<unknown>"
    args_m = _PROCEDURE_ARGS_RE.search(match.group(0))
    args = (
        _collapse_ws(args_m.group(0).decode("utf-8")) if args_m else "()"
    )
    qualified = name
    unqualified = name.split(".")[-1].strip('"')
    return Declaration(
        kind=KIND_FUNCTION,
        name=unqualified,
        signature=f"CREATE PROCEDURE {qualified}{args}",
        native_kind="procedure",
        start_line=_line_for_byte(line_starts, match.start()),
        end_line=_line_for_byte(line_starts, match.end()),
        start_byte=match.start(),
        end_byte=match.end(),
    )


def _domain_decl_from_match(
    src: bytes, match: "re.Match[bytes]", line_starts: list[int]
) -> Declaration:
    """Build a :data:`KIND_FIELD` ``Declaration`` (with
    ``native_kind="domain"``) from a regex hit. Signature is the full
    matched line so the agent sees the underlying type and any
    ``CHECK`` constraint at a glance."""
    matched = _collapse_ws(_text_of_match(src, match))
    name_m = _DOMAIN_NAME_RE.search(match.group(0))
    name = name_m.group(1).decode("utf-8") if name_m else "<unknown>"
    unqualified = name.split(".")[-1].strip('"')
    return Declaration(
        kind=KIND_FIELD,
        name=unqualified,
        signature=matched,
        native_kind="domain",
        start_line=_line_for_byte(line_starts, match.start()),
        end_line=_line_for_byte(line_starts, match.end()),
        start_byte=match.start(),
        end_byte=match.end(),
    )


def _partition_table_decl_from_match(
    src: bytes, match: "re.Match[bytes]", line_starts: list[int]
) -> Declaration:
    """Build a :data:`KIND_TABLE` ``Declaration`` for a PG declarative
    partition child (``CREATE TABLE foo PARTITION OF parent …``). The
    grammar errors on the entire ``PARTITION OF`` syntax; without
    this fallback partition children vanish from the outline.

    Columns are NOT extracted as children — partitions inherit their
    parent's column structure rather than declaring their own. The
    parent table appears separately in the outline (it parses fine),
    so an agent following the chain reads columns from there.
    """
    name_m = _PARTITION_NAME_RE.search(match.group(0))
    if name_m is None:
        child_name = "<unknown>"
        parent_name = "<unknown>"
    else:
        child_name = name_m.group(1).decode("utf-8")
        parent_name = name_m.group(2).decode("utf-8")
    unqualified = child_name.split(".")[-1].strip('"')
    return Declaration(
        kind=KIND_TABLE,
        name=unqualified,
        signature=f"CREATE TABLE {child_name} PARTITION OF {parent_name}",
        start_line=_line_for_byte(line_starts, match.start()),
        end_line=_line_for_byte(line_starts, match.end()),
        start_byte=match.start(),
        end_byte=match.end(),
    )


def _function_fallback_decl_from_match(
    src: bytes, match: "re.Match[bytes]", line_starts: list[int]
) -> Declaration:
    """Build a :data:`KIND_FUNCTION` ``Declaration`` for a function
    the AST grammar couldn't parse (typically due to ``SECURITY
    DEFINER`` or other modifier-ordering quirks). Same shape as a
    natively-parsed function, with ``native_kind="function"``."""
    name_m = _FUNCTION_FALLBACK_NAME_RE.search(match.group(0))
    name = name_m.group(1).decode("utf-8") if name_m else "<unknown>"
    args_m = _FUNCTION_FALLBACK_ARGS_RE.search(match.group(0))
    args = (
        _collapse_ws(args_m.group(0).decode("utf-8")) if args_m else "()"
    )
    qualified = name
    unqualified = name.split(".")[-1].strip('"')
    return Declaration(
        kind=KIND_FUNCTION,
        name=unqualified,
        signature=f"CREATE FUNCTION {qualified}{args}",
        native_kind="function",
        start_line=_line_for_byte(line_starts, match.start()),
        end_line=_line_for_byte(line_starts, match.end()),
        start_byte=match.start(),
        end_byte=match.end(),
    )
