"""Tests for the SQL adapter."""
from __future__ import annotations

from ast_outline.adapters.sql import SqlAdapter
from ast_outline.adapters import get_adapter_for
from ast_outline.core import (
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_NAMESPACE,
    KIND_RECORD,
    KIND_TABLE,
    KIND_VIEW,
    Declaration,
    DigestOptions,
    OutlineOptions,
    find_symbols,
    render_digest,
    render_outline,
)


def _find(decls, kind=None, name=None):
    for d in decls:
        if (kind is None or d.kind == kind) and (name is None or d.name == name):
            return d
        hit = _find(d.children, kind=kind, name=name)
        if hit is not None:
            return hit
    return None


def _find_all(decls, kind=None, name=None):
    out: list[Declaration] = []
    for d in decls:
        if (kind is None or d.kind == kind) and (name is None or d.name == name):
            out.append(d)
        out.extend(_find_all(d.children, kind=kind, name=name))
    return out


# --- Parse smoke ----------------------------------------------------------


def test_parse_populates_result_metadata(sql_dir):
    path = sql_dir / "schema.sql"
    result = SqlAdapter().parse(path)
    assert result.path == path
    assert result.language == "sql"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extension_resolution(sql_dir):
    adapter = get_adapter_for(sql_dir / "schema.sql")
    assert isinstance(adapter, SqlAdapter)


def test_clean_schema_has_zero_errors(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    assert result.error_count == 0


# --- Imports --------------------------------------------------------------


def test_create_extension_collected_as_import(sql_dir):
    result = SqlAdapter().parse(sql_dir / "extensions.sql")
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in result.imports
    assert "CREATE EXTENSION citext" in result.imports


def test_extension_with_schema_clause_preserved(sql_dir):
    result = SqlAdapter().parse(sql_dir / "extensions.sql")
    assert "CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public" in result.imports


def test_extensions_not_emitted_as_declarations(sql_dir):
    result = SqlAdapter().parse(sql_dir / "extensions.sql")
    # Only the encrypted_secrets table — extensions go to imports, not decls.
    table_names = [d.name for d in result.declarations if d.kind == KIND_TABLE]
    assert table_names == ["encrypted_secrets"]


def test_imports_are_semicolon_stripped(sql_dir):
    result = SqlAdapter().parse(sql_dir / "extensions.sql")
    for stmt in result.imports:
        assert not stmt.endswith(";"), stmt


# --- Tables ---------------------------------------------------------------


def test_create_table_emits_kind_table(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    assert users is not None
    assert users.signature == "CREATE TABLE users"


def test_table_columns_emitted_as_field_children(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    column_names = [c.name for c in users.children]
    assert column_names == [
        "id",
        "email",
        "display_name",
        "status",
        "shipping_address",
        "created_at",
        "updated_at",
    ]


def test_column_signature_includes_type_and_constraints(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    email = _find(users.children, name="email")
    assert email.kind == KIND_FIELD
    assert email.signature == "email TEXT NOT NULL UNIQUE"


def test_column_signature_collapses_multiline_whitespace(sql_dir):
    """Source-true text with newlines collapses to a single line."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    ledger = _find(result.declarations, kind=KIND_TABLE, name="points_ledger")
    delta = _find(ledger.children, name="delta")
    assert "\n" not in delta.signature
    assert delta.signature == "delta INTEGER NOT NULL CHECK (delta <> 0)"


def test_qualified_table_name_preserved_in_signature(sql_dir):
    """``analytics.events`` keeps its schema prefix in the signature, but
    the unqualified ``events`` is the canonical name for find_symbols."""
    result = SqlAdapter().parse(sql_dir / "quoted_identifiers.sql")
    events = _find(result.declarations, kind=KIND_TABLE, name="events")
    assert events is not None
    assert events.signature == "CREATE TABLE analytics.events"


# --- Views ----------------------------------------------------------------


def test_create_view_emits_kind_view(sql_dir):
    result = SqlAdapter().parse(sql_dir / "views.sql")
    view = _find(result.declarations, kind=KIND_VIEW, name="active_users")
    assert view is not None
    assert view.signature == "CREATE VIEW active_users"
    assert view.native_kind == "view"


def test_materialized_view_distinguished_via_native_kind(sql_dir):
    result = SqlAdapter().parse(sql_dir / "views.sql")
    mv = _find(result.declarations, kind=KIND_VIEW, name="monthly_revenue")
    assert mv is not None
    assert mv.signature == "CREATE MATERIALIZED VIEW monthly_revenue"
    assert mv.native_kind == "materialized view"


# --- Functions and triggers -----------------------------------------------


def test_create_function_emits_kind_function_with_full_signature(sql_dir):
    result = SqlAdapter().parse(sql_dir / "functions.sql")
    add = _find(result.declarations, kind=KIND_FUNCTION, name="add")
    assert add is not None
    assert add.signature == "CREATE FUNCTION add(a INTEGER, b INTEGER) RETURNS INTEGER"
    assert add.native_kind == "function"


def test_function_body_does_not_inflate_error_count(sql_dir):
    """PL/pgSQL bodies use syntax (``:=``, ``IF…THEN…END IF``) the SQL
    grammar can't parse. Internal ERRORs inside ``function_body`` are
    suppressed — only top-level DDL ERRORs count."""
    result = SqlAdapter().parse(sql_dir / "functions.sql")
    assert result.error_count == 0


def test_function_body_not_surfaced_as_children(sql_dir):
    result = SqlAdapter().parse(sql_dir / "functions.sql")
    fn = _find(result.declarations, kind=KIND_FUNCTION, name="user_points")
    assert fn is not None
    assert fn.children == []


def test_create_trigger_uses_function_kind_with_trigger_native_kind(sql_dir):
    result = SqlAdapter().parse(sql_dir / "functions.sql")
    trig = _find(result.declarations, name="set_updated_at")
    assert trig is not None
    assert trig.kind == KIND_FUNCTION
    assert trig.native_kind == "trigger"
    assert "CREATE TRIGGER set_updated_at" in trig.signature
    assert "ON users" in trig.signature


# --- Types and enums ------------------------------------------------------


def test_composite_type_emits_kind_record_with_field_children(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    address = _find(result.declarations, kind=KIND_RECORD, name="address")
    assert address is not None
    assert address.native_kind == "type"
    assert [c.name for c in address.children] == ["street", "city", "zip"]
    assert all(c.kind == KIND_FIELD for c in address.children)


def test_enum_type_emits_kind_enum_with_member_children(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    status = _find(result.declarations, kind=KIND_ENUM, name="order_status")
    assert status is not None
    assert status.native_kind == "enum"
    member_names = [c.name for c in status.children]
    assert member_names == ["pending", "paid", "shipped", "cancelled"]
    assert all(c.kind == KIND_ENUM_MEMBER for c in status.children)


def test_enum_member_signature_keeps_quotes(sql_dir):
    """The member's ``name`` strips quotes (so find_symbols('paid')
    works), but ``signature`` preserves the source-true literal."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    status = _find(result.declarations, kind=KIND_ENUM, name="order_status")
    paid = _find(status.children, name="paid")
    assert paid.signature == "'paid'"


# --- Indexes and sequences ------------------------------------------------


def test_create_index_emits_field_with_index_native_kind(sql_dir):
    result = SqlAdapter().parse(sql_dir / "migration.sql")
    idx = _find(result.declarations, name="idx_users_email")
    assert idx is not None
    assert idx.kind == KIND_FIELD
    assert idx.native_kind == "index"
    assert "UNIQUE INDEX" in idx.signature
    assert "ON users" in idx.signature


def test_create_sequence_emits_field_with_sequence_native_kind(sql_dir):
    result = SqlAdapter().parse(sql_dir / "migration.sql")
    seq = _find(result.declarations, name="order_id_seq")
    assert seq is not None
    assert seq.kind == KIND_FIELD
    assert seq.native_kind == "sequence"
    assert "CREATE SEQUENCE" in seq.signature


# --- Schemas --------------------------------------------------------------


def test_create_schema_emits_kind_namespace(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="loyalty")
    assert ns is not None
    assert ns.signature == "CREATE SCHEMA loyalty"


def test_objects_are_siblings_of_schema_not_children(sql_dir):
    """SQL doesn't lexically scope objects under a schema — qualified
    names handle it. So the schema decl has no children, and tables
    appear at the top level alongside it."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="loyalty")
    assert ns.children == []
    top_names = [d.name for d in result.declarations]
    assert "loyalty" in top_names
    assert "users" in top_names


# --- Quoted / qualified identifiers --------------------------------------


def test_quoted_table_name_preserves_quotes(sql_dir):
    result = SqlAdapter().parse(sql_dir / "quoted_identifiers.sql")
    tbl = _find(result.declarations, name='"User Profile"')
    assert tbl is not None
    assert tbl.kind == KIND_TABLE
    assert tbl.signature == 'CREATE TABLE "User Profile"'


def test_quoted_column_name_preserves_quotes(sql_dir):
    result = SqlAdapter().parse(sql_dir / "quoted_identifiers.sql")
    tbl = _find(result.declarations, name='"User Profile"')
    col = _find(tbl.children, name='"Email Address"')
    assert col is not None
    assert col.signature == '"Email Address" TEXT NOT NULL'


# --- Doc comments ---------------------------------------------------------


def test_line_comment_attached_as_docs(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    ledger = _find(result.declarations, kind=KIND_TABLE, name="points_ledger")
    assert ledger.docs == ["-- Reward points earned per user, aggregated nightly."]


def test_block_comment_lines_in_source_order(sql_dir):
    """A multi-line ``/* … */`` block keeps its internal line order."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    assert users.docs[0].startswith("/* Users of the loyalty programme.")
    assert users.docs[-1].endswith("*/")


def test_doc_start_byte_set_for_documented_decls(sql_dir):
    """``doc_start_byte`` lets ``show`` include the doc in the slice."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    ledger = _find(result.declarations, kind=KIND_TABLE, name="points_ledger")
    assert ledger.doc_start_byte > 0
    assert ledger.doc_start_byte < ledger.start_byte


def test_blank_line_breaks_doc_chain(sql_dir):
    """A comment followed by a blank line is NOT attached to the next decl."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    # `orders` is preceded by a blank line, then directly by table syntax.
    orders = _find(result.declarations, kind=KIND_TABLE, name="orders")
    assert orders.docs == []


# --- Multi-statement files ------------------------------------------------


def test_multiple_top_level_tables_surface_as_siblings(sql_dir):
    result = SqlAdapter().parse(sql_dir / "multi_statement.sql")
    tables = _find_all(result.declarations, kind=KIND_TABLE)
    assert [t.name for t in tables] == ["alpha", "beta", "gamma", "delta"]


def test_sibling_table_line_ranges_dont_overlap(sql_dir):
    result = SqlAdapter().parse(sql_dir / "multi_statement.sql")
    tables = _find_all(result.declarations, kind=KIND_TABLE)
    for prev, cur in zip(tables, tables[1:]):
        assert prev.end_line < cur.start_line, (prev.name, cur.name)


# --- Renderers and search -------------------------------------------------


def test_render_outline_smoke(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    out = render_outline(result, OutlineOptions())
    assert "CREATE TABLE users" in out
    assert "email TEXT NOT NULL UNIQUE" in out
    assert "CREATE TYPE order_status AS ENUM" in out
    assert "namespace loyalty" in out


def test_render_digest_counts_tables_as_types(sql_dir):
    """Tables/views participate in the ``N types`` counter."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    digest = render_digest([result], DigestOptions())
    assert "5 types" in digest  # 1 enum + 1 record + 3 tables
    assert "table users" in digest


def test_render_digest_with_include_fields_shows_column_names(sql_dir):
    """Default digest hides KIND_FIELD entries, so column tokens are
    NOT in the body — only the ``20 fields`` counter in the header.
    With ``--include-fields`` (i.e. ``include_fields=True``), column
    NAMES appear under their parent table in compact ``name [field]``
    token form. Full column types (``TEXT NOT NULL UNIQUE``) are an
    ``outline`` feature; digest stays terse on purpose."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    default_digest = render_digest([result], DigestOptions())
    assert "email" not in default_digest

    full_digest = render_digest([result], DigestOptions(include_fields=True))
    assert "email [field]" in full_digest
    assert "id [field]" in full_digest


def test_find_symbols_finds_table(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    hits = find_symbols(result, "users")
    assert len(hits) == 1
    assert hits[0].kind == KIND_TABLE
    assert hits[0].qualified_name == "users"


def test_find_symbols_finds_column_via_dotted_path(sql_dir):
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    hits = find_symbols(result, "users.email")
    assert len(hits) == 1
    assert hits[0].kind == KIND_FIELD
    assert hits[0].qualified_name == "users.email"


def test_find_symbols_qualified_table_by_unqualified_name(sql_dir):
    """A schema-qualified table (``analytics.events``) is reachable
    by its unqualified name (``events``) — agents don't need to know
    the schema to navigate."""
    result = SqlAdapter().parse(sql_dir / "quoted_identifiers.sql")
    hits = find_symbols(result, "events")
    assert len(hits) == 1
    assert hits[0].kind == KIND_TABLE


def test_find_symbols_schema_qualified_query_not_supported(sql_dir):
    """``find_symbols("analytics.events")`` returns nothing — schemas
    are siblings of their objects, not lexical parents, so the dotted
    query has no two-level trail to walk. Agents must query the
    unqualified name; the qualified form is visible in ``signature``.
    Pinned as a test so the gap doesn't get accidentally papered over
    with a half-working ``match_names`` shim."""
    result = SqlAdapter().parse(sql_dir / "quoted_identifiers.sql")
    assert find_symbols(result, "analytics.events") == []


def test_find_symbols_returns_whole_table_body(sql_dir):
    """Local mirror of the cross-adapter invariant in
    ``test_core_search.py`` — for a SQL table, the source slice
    spans from the declaration's leading doc-comment through the
    closing paren of its column list."""
    result = SqlAdapter().parse(sql_dir / "schema.sql")
    hits = find_symbols(result, "users")
    src = hits[0].source
    assert "CREATE TABLE users" in src
    assert "email TEXT NOT NULL UNIQUE" in src
    assert "updated_at TIMESTAMP" in src
    assert src.rstrip().endswith(")")


# --- Regex fallback for grammar-unsupported constructs -------------------


def test_create_procedure_recovered_via_regex_fallback(sql_dir):
    """``CREATE PROCEDURE`` is unparseable by the upstream
    ``tree-sitter-sql`` grammar (it produces ERROR nodes), but the
    adapter recovers it with a line-anchored regex pass over the
    source. The recovered declaration uses the same shape a
    natively-parsed function would (``KIND_FUNCTION`` with
    ``native_kind="procedure"``)."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    procs = _find_all(
        result.declarations, kind=KIND_FUNCTION
    )
    procs = [p for p in procs if p.native_kind == "procedure"]
    proc_names = [p.name for p in procs]
    assert "log_event" in proc_names
    assert "process_batch" in proc_names
    assert "no_args_proc" in proc_names


def test_procedure_signature_includes_full_parameter_list(sql_dir):
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    proc = _find(result.declarations, name="process_batch")
    assert proc is not None
    assert (
        proc.signature
        == "CREATE PROCEDURE process_batch(batch_id BIGINT, dry_run BOOLEAN DEFAULT false)"
    )


def test_procedure_with_no_args_signature_has_empty_parens(sql_dir):
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    proc = _find(result.declarations, name="no_args_proc")
    assert proc is not None
    assert proc.signature == "CREATE PROCEDURE no_args_proc()"


def test_create_domain_recovered_via_regex_fallback(sql_dir):
    """``CREATE DOMAIN`` is unparseable by the grammar; recovered as
    a ``KIND_FIELD`` with ``native_kind="domain"``. The signature
    captures the full single-line declaration including any inline
    ``CHECK`` clause."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    domains = [
        d for d in result.declarations
        if d.kind == KIND_FIELD and d.native_kind == "domain"
    ]
    domain_names = [d.name for d in domains]
    assert domain_names == ["positive_int", "email_address", "postal_code"]


def test_domain_signature_keeps_check_clause(sql_dir):
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    dom = _find(result.declarations, name="positive_int")
    assert dom is not None
    assert dom.signature == "CREATE DOMAIN positive_int AS INTEGER CHECK (VALUE > 0)"


def test_load_collected_as_import(sql_dir):
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    assert "LOAD 'auto_explain'" in result.imports
    assert "LOAD 'pg_stat_statements'" in result.imports


def test_import_foreign_schema_collected_as_import(sql_dir):
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    assert (
        "IMPORT FOREIGN SCHEMA remote_schema FROM SERVER fdw_server INTO local_schema"
        in result.imports
    )


def test_import_foreign_schema_with_limit_clause(sql_dir):
    """The optional ``LIMIT TO (...)`` clause sits BEFORE ``FROM`` per
    PostgreSQL syntax; the regex must not require it AFTER."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    found = any("LIMIT TO" in imp for imp in result.imports)
    assert found, f"expected an IFS import with LIMIT TO clause, got {result.imports}"


def test_regex_fallback_skips_red_herrings_in_comments(sql_dir):
    """``CREATE PROCEDURE fake_in_comment()`` and similar bait inside
    line / block comments and inside the body of an outer function
    must NOT surface. The fallback consults AST-derived skip ranges
    (``comment``, ``marginalia``, ``literal``, ``block``) before
    accepting a regex match."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    names = [d.name for d in result.declarations]
    assert "fake_in_comment" not in names
    assert "fake_in_block" not in names
    assert "inner_fake" not in names


def test_regex_fallback_results_interleave_with_ast_results_in_source_order(sql_dir):
    """Regex-recovered and AST-parsed declarations sort together by
    start_byte. Verifies a fixture with a trailing ``CREATE TABLE``
    after the procedures: it appears LAST in declarations even though
    the regex pass ran second."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    last = result.declarations[-1]
    assert last.kind == KIND_TABLE
    assert last.name == "log_archive"


def test_regex_fallback_recovered_constructs_dont_inflate_error_count(sql_dir):
    """Constructs the regex pass successfully recovered should not
    leave their original ERROR nodes counted as parse errors. Without
    this filter, every PROCEDURE / DOMAIN / LOAD / IFS would brand
    the file as broken even though the outline is complete."""
    result = SqlAdapter().parse(sql_dir / "procedures_and_domains.sql")
    assert result.error_count == 0


# --- Modern PostgreSQL features (partitioning, function modifiers) -------


def test_partition_parent_table_parses_normally(sql_dir):
    """The parent of a partitioned table (``CREATE TABLE … PARTITION
    BY RANGE …``) parses cleanly via the AST. Its columns extract as
    children — the children-partitions inherit them implicitly."""
    result = SqlAdapter().parse(sql_dir / "pg_partitioning_and_modifiers.sql")
    parent = _find(result.declarations, kind=KIND_TABLE, name="events")
    assert parent is not None
    assert [c.name for c in parent.children] == ["id", "user_id", "ts", "payload"]


def test_partition_child_recovered_via_regex_fallback(sql_dir):
    """``CREATE TABLE foo PARTITION OF parent FOR VALUES FROM (…) TO (…)``
    is unparseable by the upstream grammar; the regex fallback emits
    a ``KIND_TABLE`` carrying the parent reference in the signature.
    Partitions don't declare their own columns (they inherit the
    parent's), so child decl has no ``children``."""
    result = SqlAdapter().parse(sql_dir / "pg_partitioning_and_modifiers.sql")
    children = [
        d for d in result.declarations
        if d.kind == KIND_TABLE and d.name.startswith("events_")
    ]
    assert [c.name for c in children] == [
        "events_2024",
        "events_2025",
        "events_default",
    ]
    for c in children:
        assert "PARTITION OF events" in c.signature
        assert c.children == []


def test_function_with_security_definer_recovered(sql_dir):
    """``CREATE FUNCTION … SECURITY DEFINER LANGUAGE … AS …`` and
    similar exotic modifier orderings the upstream grammar errors on
    are recovered by the FUNCTION fallback regex with the same
    ``KIND_FUNCTION`` shape a natively-parsed function uses."""
    result = SqlAdapter().parse(sql_dir / "pg_partitioning_and_modifiers.sql")
    admin = _find(result.declarations, name="admin_op")
    trusted = _find(result.declarations, name="trusted_op")
    assert admin is not None and admin.kind == KIND_FUNCTION
    assert trusted is not None and trusted.kind == KIND_FUNCTION


def test_function_fallback_does_not_double_extract_ast_parsed_functions(sql_dir):
    """When a file mixes AST-parseable and AST-unparseable functions,
    the regex fallback must skip ranges already covered by AST decls
    so the cleanly-parsed ones don't appear twice."""
    result = SqlAdapter().parse(sql_dir / "pg_partitioning_and_modifiers.sql")
    add_decls = _find_all(result.declarations, name="add")
    assert len(add_decls) == 1, f"`add` should appear once, got {len(add_decls)}"


def test_partition_and_function_recovery_zero_error_count(sql_dir):
    """All four PG-modern constructs in the fixture (3 partition
    children + 2 SECURITY DEFINER functions) are recovered, so the
    file reports ``error_count == 0`` despite the upstream grammar
    erroring on each one."""
    result = SqlAdapter().parse(sql_dir / "pg_partitioning_and_modifiers.sql")
    assert result.error_count == 0


# --- Non-PostgreSQL dialect coverage --------------------------------------


def test_mysql_dialect_extracts_tables_columns_indexes_views(sql_dir):
    """A realistic MySQL schema with ``ENGINE=InnoDB``,
    ``AUTO_INCREMENT``, inline ``KEY`` constraints, and ``ENUM(...)``
    column types. Some MySQL-specific syntax surfaces as
    ``error_count > 0`` but every structural declaration extracts
    cleanly — tables with all their columns, indexes, and views."""
    result = SqlAdapter().parse(sql_dir / "mysql_dialect.sql")
    tables = _find_all(result.declarations, kind=KIND_TABLE)
    assert [t.name for t in tables] == ["users", "orders"]

    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    assert [c.name for c in users.children] == [
        "id",
        "email",
        "display_name",
        "created_at",
    ]

    indexes = [
        d for d in result.declarations
        if d.kind == KIND_FIELD and d.native_kind == "index"
    ]
    assert [i.name for i in indexes] == ["idx_orders_user", "idx_orders_status"]

    views = _find_all(result.declarations, kind=KIND_VIEW)
    assert [v.name for v in views] == ["active_users"]


def test_mysql_auto_increment_preserved_in_column_signature(sql_dir):
    """``AUTO_INCREMENT`` is MySQL-specific; the column signature
    keeps it source-true so the agent sees the auto-id mechanism."""
    result = SqlAdapter().parse(sql_dir / "mysql_dialect.sql")
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    id_col = _find(users.children, name="id")
    assert "AUTO_INCREMENT" in id_col.signature


def test_sqlite_dialect_extracts_tables_columns_indexes_views(sql_dir):
    """SQLite with ``AUTOINCREMENT``, ``strftime`` defaults, and
    inline foreign keys parses with ``error_count > 0`` but the full
    structural skeleton extracts."""
    result = SqlAdapter().parse(sql_dir / "sqlite_dialect.sql")
    tables = _find_all(result.declarations, kind=KIND_TABLE)
    assert [t.name for t in tables] == ["users", "posts"]

    posts = _find(result.declarations, kind=KIND_TABLE, name="posts")
    assert [c.name for c in posts.children] == [
        "id",
        "user_id",
        "title",
        "body",
    ]

    views = _find_all(result.declarations, kind=KIND_VIEW)
    assert [v.name for v in views] == ["recent_posts"]


# --- Robustness edge cases ------------------------------------------------


def test_empty_file_returns_empty_result(sql_dir, tmp_path):
    f = tmp_path / "empty.sql"
    f.write_bytes(b"")
    result = SqlAdapter().parse(f)
    assert result.declarations == []
    assert result.imports == []
    assert result.error_count == 0


def test_only_comments_no_declarations(tmp_path):
    f = tmp_path / "comments_only.sql"
    f.write_bytes(b"-- Only a comment\n/* And a block one */\n")
    result = SqlAdapter().parse(f)
    assert result.declarations == []
    assert result.error_count == 0


def test_no_trailing_semicolon_still_parses(tmp_path):
    """A file ending without ``;`` (sloppy but common in
    interactive-paste contexts) still surfaces its declarations."""
    f = tmp_path / "no_semi.sql"
    f.write_bytes(b"CREATE TABLE foo (id INT)")
    result = SqlAdapter().parse(f)
    table = _find(result.declarations, kind=KIND_TABLE, name="foo")
    assert table is not None


def test_crlf_line_endings(tmp_path):
    """Windows-style line endings shouldn't break parsing or
    line-number tracking."""
    f = tmp_path / "crlf.sql"
    f.write_bytes(b"CREATE TABLE foo (\r\n  id INT\r\n);\r\n")
    result = SqlAdapter().parse(f)
    table = _find(result.declarations, kind=KIND_TABLE, name="foo")
    assert table is not None
    assert [c.name for c in table.children] == ["id"]


def test_multiple_statements_on_one_line(tmp_path):
    f = tmp_path / "multi.sql"
    f.write_bytes(b"CREATE TABLE a (id INT); CREATE TABLE b (id INT);")
    result = SqlAdapter().parse(f)
    names = [d.name for d in result.declarations if d.kind == KIND_TABLE]
    assert names == ["a", "b"]


def test_reserved_word_quoted_identifier(tmp_path):
    """``"user"`` is a PG reserved keyword used as an identifier via
    double-quoting. Parses fine, name retains the quotes (current
    grammar behaviour)."""
    f = tmp_path / "reserved.sql"
    f.write_bytes(b'CREATE TABLE "user" (id INT);')
    result = SqlAdapter().parse(f)
    table = _find(result.declarations, kind=KIND_TABLE)
    assert table is not None
    assert table.name == '"user"'


def test_unicode_identifier(tmp_path):
    """Non-ASCII quoted identifier (Cyrillic) — the adapter handles
    UTF-8 source bytes correctly throughout the AST and regex paths."""
    f = tmp_path / "unicode.sql"
    f.write_bytes("CREATE TABLE \"Пользователи\" (id INT);".encode("utf-8"))
    result = SqlAdapter().parse(f)
    table = _find(result.declarations, kind=KIND_TABLE)
    assert table is not None
    assert table.name == '"Пользователи"'


def test_inline_column_comment_does_not_break_parsing(tmp_path):
    """A ``-- comment`` between commas in a column list survives —
    the grammar tolerates it and columns extract normally."""
    f = tmp_path / "inline_comment.sql"
    f.write_bytes(
        b"CREATE TABLE users (\n  id INT, -- pkey\n  email TEXT NOT NULL\n);"
    )
    result = SqlAdapter().parse(f)
    users = _find(result.declarations, kind=KIND_TABLE, name="users")
    assert users is not None
    assert [c.name for c in users.children] == ["id", "email"]


def test_create_or_replace_function_extracts(tmp_path):
    """``CREATE OR REPLACE FUNCTION`` — the most common idempotent
    function-creation idiom."""
    f = tmp_path / "or_replace.sql"
    f.write_bytes(
        b"CREATE OR REPLACE FUNCTION add(a INT, b INT) RETURNS INT "
        b"LANGUAGE sql AS $$ SELECT a + b $$;"
    )
    result = SqlAdapter().parse(f)
    fn = _find(result.declarations, kind=KIND_FUNCTION, name="add")
    assert fn is not None


def test_returns_table_function_extracts(tmp_path):
    """Set-returning function with ``RETURNS TABLE(...)``."""
    f = tmp_path / "returns_table.sql"
    f.write_bytes(
        b"CREATE FUNCTION list_users() RETURNS TABLE(id INT, email TEXT) "
        b"LANGUAGE sql AS $$ SELECT id, email FROM users $$;"
    )
    result = SqlAdapter().parse(f)
    fn = _find(result.declarations, kind=KIND_FUNCTION, name="list_users")
    assert fn is not None
    assert "RETURNS TABLE" in fn.signature


def test_generated_column_in_signature(tmp_path):
    """Generated columns (``GENERATED ALWAYS AS … STORED``) keep
    their full source-true definition in the column signature."""
    f = tmp_path / "generated.sql"
    f.write_bytes(b"""CREATE TABLE products (
  price NUMERIC,
  tax_rate NUMERIC,
  total NUMERIC GENERATED ALWAYS AS (price * (1 + tax_rate)) STORED
);""")
    result = SqlAdapter().parse(f)
    products = _find(result.declarations, kind=KIND_TABLE, name="products")
    assert products is not None
    total = _find(products.children, name="total")
    assert "GENERATED ALWAYS AS" in total.signature


def test_array_column_type(tmp_path):
    """SQL array types (``TEXT[]``) round-trip through the column
    signature."""
    f = tmp_path / "arrays.sql"
    f.write_bytes(b"CREATE TABLE tagged (id INT, tags TEXT[]);")
    result = SqlAdapter().parse(f)
    tagged = _find(result.declarations, kind=KIND_TABLE, name="tagged")
    assert tagged is not None
    tags = _find(tagged.children, name="tags")
    assert "TEXT[]" in tags.signature


# --- Broken syntax --------------------------------------------------------


def test_broken_file_recovers_valid_neighbours(sql_dir):
    """A broken trailing statement doesn't kill earlier valid tables."""
    result = SqlAdapter().parse(sql_dir / "broken.sql")
    names = [d.name for d in result.declarations if d.kind == KIND_TABLE]
    assert "healthy_first" in names
    assert "healthy_second" in names


def test_broken_file_reports_error_count(sql_dir):
    result = SqlAdapter().parse(sql_dir / "broken.sql")
    assert result.error_count > 0


def test_function_fallback_skips_block_comments_after_late_procedure(tmp_path):
    """`skip_ranges` is binary-searched by `_in_any_range` and must stay
    sorted. The PROCEDURE loop used a plain `append`, so a procedure
    LATER in the file than a block comment left the list unsorted and
    the FUNCTION fallback extracted fake declarations from inside
    `/* ... */` comments."""
    p = tmp_path / "a.sql"
    p.write_text(
        "CREATE PROCEDURE init() LANGUAGE plpgsql AS $$ BEGIN END; $$;\n"
        "/*\n"
        "CREATE OR REPLACE FUNCTION fake() RETURNS void\n"
        "*/\n"
        "CREATE DOMAIN email AS TEXT;\n"
        "CREATE OR REPLACE FUNCTION real_func(p email) RETURNS VOID\n"
        "LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN END; $$;\n",
        encoding="utf-8",
    )
    r = SqlAdapter().parse(p)
    names = []
    def walk(ds):
        for d in ds:
            names.append(d.name)
            walk(d.children)
    walk(r.declarations)
    assert "fake" not in names, names
    assert "real_func" in names, names
