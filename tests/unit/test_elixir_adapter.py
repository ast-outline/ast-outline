"""Tests for the Elixir adapter."""
from __future__ import annotations

from pathlib import Path

from ast_outline.adapters.elixir import ElixirAdapter
from ast_outline.adapters import get_adapter_for, supported_extensions
from ast_outline.core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    Declaration,
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


def test_parse_populates_result_metadata(elixir_dir):
    path = elixir_dir / "accounts.ex"
    result = ElixirAdapter().parse(path)
    assert result.path == path
    assert result.language == "elixir"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extensions_and_adapter_resolution(elixir_dir):
    for ext in (".ex", ".exs"):
        assert ext in ElixirAdapter.extensions
        assert ext in supported_extensions()
    assert isinstance(get_adapter_for(elixir_dir / "accounts.ex"), ElixirAdapter)
    assert isinstance(get_adapter_for(elixir_dir / "accounts_test.exs"), ElixirAdapter)


# --- Modules --------------------------------------------------------------


def test_defmodule_is_namespace_with_full_name(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="MyApp.Accounts")
    assert ns is not None
    assert ns.signature == "defmodule MyApp.Accounts"


def test_nested_defmodule_nests_rather_than_collapses(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    outer = _find(result.declarations, kind=KIND_NAMESPACE, name="MyApp.Accounts")
    assert outer is not None
    inner = _find(outer.children, kind=KIND_NAMESPACE, name="Policy")
    assert inner is not None, "nested Policy module should be a child, not path-collapsed"


def test_moduledoc_absorbed_onto_module(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="MyApp.Accounts")
    assert ns.docs == ["The Accounts context."]


def test_moduledoc_does_not_leak_onto_first_member(elixir_dir):
    """Regression: a nested module whose first body element is a ``def``
    must not have its ``@moduledoc`` re-surface as a doc on that def."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    policy = _find(result.declarations, kind=KIND_NAMESPACE, name="Policy")
    assert policy.docs == ["Authorization rules."]
    can = _find(policy.children, name="can?")
    assert can is not None
    assert can.docs == [], f"moduledoc leaked onto first member: {can.docs!r}"


# --- def / defp / macros / guards / delegate ------------------------------


def test_module_defs_are_methods(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    get_user = _find(result.declarations, kind=KIND_METHOD, name="get_user")
    assert get_user is not None
    assert "is_valid_id(id)" in get_user.signature


def test_top_level_defs_are_functions(tmp_path):
    """A ``def`` walked at top scope maps to KIND_FUNCTION rather than
    KIND_METHOD (the module-vs-top-level distinction)."""
    src = tmp_path / "top.exs"
    src.write_text("def add(a, b), do: a + b\n")
    result = ElixirAdapter().parse(src)
    fn = _find(result.declarations, kind=KIND_FUNCTION, name="add")
    assert fn is not None


def test_defp_marked_private(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    norm = _find(result.declarations, name="normalize")
    assert norm is not None
    assert norm.visibility == "private"


def test_macro_and_guard_markers(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    macro = _find(result.declarations, name="__using__")
    assert macro is not None and "[macro]" in macro.attrs
    macrop = _find(result.declarations, name="debug_log")
    assert macrop is not None and "[macro]" in macrop.attrs
    assert macrop.visibility == "private"
    guard = _find(result.declarations, name="is_valid_id")
    assert guard is not None and "[guard]" in guard.attrs
    guardp = _find(result.declarations, name="is_admin")
    assert guardp is not None and "[guard]" in guardp.attrs
    assert guardp.visibility == "private"


def test_defdelegate_keeps_to_target_in_signature(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    dele = _find(result.declarations, name="list_all")
    assert dele is not None
    assert "[delegate]" in dele.attrs
    assert "to: Repo" in dele.signature


def test_function_clauses_deduplicated(elixir_dir):
    """``get_user/1`` has two clauses (guarded + fallback); only the
    first survives."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    clauses = _find_all(result.declarations, name="get_user")
    assert len(clauses) == 1


def test_distinct_arities_are_kept_separate(elixir_dir):
    """Regression: dedup must key on arity — ``route/1``, ``route/2`` and
    ``route/3`` are distinct functions, not clauses of one."""
    result = ElixirAdapter().parse(elixir_dir / "clauses.ex")
    routes = _find_all(result.declarations, name="route")
    assert len(routes) == 3
    # ``handle/1`` genuinely has three clauses — still collapses to one.
    handles = _find_all(result.declarations, name="handle")
    assert len(handles) == 1


def test_same_name_different_def_kind_kept_separate(tmp_path):
    """Regression: a function and a macro that share a name but differ in
    arity are distinct constructs — dedup must not collapse them."""
    src = tmp_path / "kinds.ex"
    src.write_text(
        "defmodule M do\n"
        "  def foo(x), do: x\n"
        "  defmacro foo(x, y), do: quote(do: unquote(x) + unquote(y))\n"
        "end\n"
    )
    result = ElixirAdapter().parse(src)
    foos = _find_all(result.declarations, name="foo")
    assert len(foos) == 2
    assert any("[macro]" in f.attrs for f in foos)
    assert any(not f.attrs for f in foos)


def test_dedup_is_scoped_per_dsl_block(tmp_path):
    """A KIND_BLOCK is its own rendered container, so clause dedup resets
    per block: the same ``def`` in two sibling blocks surfaces once under
    each, not collapsed across them (intentional — see module docstring)."""
    src = tmp_path / "blocks.ex"
    src.write_text(
        "defmodule M do\n"
        '  feature "a" do\n'
        "    def helper(x), do: x\n"
        "  end\n"
        '  feature "b" do\n'
        "    def helper(x), do: :other\n"
        "  end\n"
        "end\n"
    )
    result = ElixirAdapter().parse(src)
    helpers = _find_all(result.declarations, name="helper")
    assert len(helpers) == 2
    # But within a single block, clauses still collapse.
    src2 = tmp_path / "one_block.ex"
    src2.write_text(
        "defmodule M do\n"
        '  feature "a" do\n'
        "    def helper(:x), do: :x\n"
        "    def helper(:y), do: :y\n"
        "  end\n"
        "end\n"
    )
    result2 = ElixirAdapter().parse(src2)
    assert len(_find_all(result2.declarations, name="helper")) == 1


def test_def_inside_dsl_block_in_module_is_method(tmp_path):
    """Regression: a ``def`` nested in a labelled DSL block that sits
    inside a module must inherit the module scope (KIND_METHOD), not be
    reset to top-level (KIND_FUNCTION)."""
    src = tmp_path / "dsl.ex"
    src.write_text(
        "defmodule M do\n"
        "  feature \"grouped\" do\n"
        "    def helper(x), do: x\n"
        "  end\n"
        "end\n"
    )
    result = ElixirAdapter().parse(src)
    helper = _find(result.declarations, name="helper")
    assert helper is not None
    assert helper.kind == KIND_METHOD
    assert _find(result.declarations, kind=KIND_FUNCTION, name="helper") is None


# --- @doc / @spec / type attributes ---------------------------------------


def test_doc_absorbed_onto_next_function(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    get_user = _find(result.declarations, name="get_user")
    assert get_user.docs == ["Fetches a user by id, returning nil when absent."]


def test_type_attributes_become_fields(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    t = _find(result.declarations, kind=KIND_FIELD, name="t")
    assert t is not None and "[type]" in t.attrs
    tp = _find(result.declarations, kind=KIND_FIELD, name="state")
    assert tp is not None and "[typep]" in tp.attrs
    assert tp.visibility == "private"
    op = _find(result.declarations, kind=KIND_FIELD, name="token")
    assert op is not None and "[opaque]" in op.attrs


def test_type_name_from_when_wrapped_spec(elixir_dir):
    """A parameterized ``@type`` with a ``when`` clause resolves to its
    bare name, not the whole left-hand spec (shares the fix applied to
    @callback — both spec shapes can be ``when``-wrapped)."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    res = _find(result.declarations, kind=KIND_FIELD, name="result")
    assert res is not None and "[type]" in res.attrs


def test_zero_arity_when_wrapped_type_name(tmp_path):
    """Regression (sibling of the @callback fix): a paren-less zero-arity
    ``@type`` wrapped in ``when`` must not yield the whole left spec as
    the name."""
    src = tmp_path / "t.ex"
    src.write_text(
        "defmodule M do\n"
        "  @type ready :: term when x: var\n"
        "end\n"
    )
    result = ElixirAdapter().parse(src)
    assert _find(result.declarations, kind=KIND_FIELD, name="ready") is not None


def test_callback_becomes_method(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    cb = _find(result.declarations, kind=KIND_METHOD, name="fetch")
    assert cb is not None
    assert "[callback]" in cb.attrs
    assert cb.signature.startswith("@callback")


def test_callback_name_from_zero_arity_and_when_clause(elixir_dir):
    """Regression (corpus-found): a paren-less zero-arity callback and a
    callback with a ``when`` clause both carry a real name, not ``?``."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    ready = _find(result.declarations, name="ready?")
    assert ready is not None and "[callback]" in ready.attrs
    merge = _find(result.declarations, name="merge")
    assert merge is not None and "[callback]" in merge.attrs
    assert "when t: var" in merge.signature
    # No callback should degrade to the ``?`` fallback name.
    assert _find(result.declarations, kind=KIND_METHOD, name="?") is None


def test_spec_not_surfaced(elixir_dir):
    """``@spec`` carries no new name/arity beyond the function, so it is
    intentionally dropped."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    specs = _find_all(result.declarations, kind=KIND_FIELD)
    assert not any(f.signature.startswith("@spec") for f in specs)


# --- defstruct / defexception ---------------------------------------------


def test_defstruct_enforced_and_default_keys(elixir_dir):
    """Both bare ``:atom`` enforced keys and trailing ``key: default``
    entries must surface (regression: defaults grouped under a
    ``keywords`` node inside the bracketed list were dropped)."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    struct_fields = [
        f for f in _find_all(result.declarations, kind=KIND_FIELD)
        if "[struct]" in f.attrs
    ]
    names = {f.name for f in struct_fields}
    assert names == {"id", "name", "active", "role"}
    # Keys are clean — no trailing colon or whitespace.
    assert all(":" not in n and n == n.strip() for n in names)


def test_defexception_fields(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "errors.ex")
    fields = [
        f for f in _find_all(result.declarations, kind=KIND_FIELD)
        if "[exception]" in f.attrs
    ]
    names = {f.name for f in fields}
    assert names == {"message", "plug_status"}


# --- Protocols / impls ----------------------------------------------------


def test_defprotocol_is_interface(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "protocols.ex")
    proto = _find(result.declarations, kind=KIND_INTERFACE, name="MyApp.Size")
    assert proto is not None
    assert proto.native_kind == "defprotocol"
    assert _find(proto.children, name="size") is not None


def test_defimpl_is_class_with_for_type(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "protocols.ex")
    impls = _find_all(result.declarations, kind=KIND_CLASS)
    names = {i.name for i in impls}
    assert "MyApp.Size(BitString)" in names
    assert "MyApp.Size(Map)" in names
    bitstring = _find(result.declarations, name="MyApp.Size(BitString)")
    assert bitstring.native_kind == "defimpl"
    assert "for: BitString" in bitstring.signature


def test_imports_inside_impl_body_collected(elixir_dir):
    """Regression: ``alias`` / ``import`` at the top of a defimpl (or
    defprotocol) body is source-true and must be collected, like it is
    for defmodule bodies."""
    result = ElixirAdapter().parse(elixir_dir / "protocols.ex")
    assert "alias MyApp.Helpers" in result.imports


# --- Imports --------------------------------------------------------------


def test_imports_collected_source_true(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    assert "use GenServer" in result.imports
    assert "import Ecto.Query" in result.imports
    assert "require Logger" in result.imports


def test_multi_alias_expands_to_one_entry_each(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    assert "alias MyApp.Repo" in result.imports
    assert "alias MyApp.User" in result.imports
    assert "alias MyApp.Mailer" in result.imports


def test_import_inside_macro_body_is_conditional(elixir_dir):
    """An ``import`` inside a ``def``-form body is counted as conditional,
    not listed among the module's static imports."""
    result = ElixirAdapter().parse(elixir_dir / "accounts.ex")
    assert result.conditional_imports_count >= 1


# --- DSL blocks -----------------------------------------------------------


def test_describe_and_test_blocks_surface_as_named_containers(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts_test.exs")
    describes = _find_all(result.declarations, kind=KIND_BLOCK)
    labels = {d.name for d in describes}
    assert "get_user/1" in labels
    assert "returns the user for a valid id" in labels


def test_nested_test_blocks_nest_under_describe(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "accounts_test.exs")
    describe = _find(result.declarations, kind=KIND_BLOCK, name="get_user/1")
    assert describe is not None
    child = _find(describe.children, kind=KIND_BLOCK,
                  name="returns the user for a valid id")
    assert child is not None


# --- Robustness -----------------------------------------------------------


def test_broken_file_reports_parse_errors_without_crashing(elixir_dir):
    result = ElixirAdapter().parse(elixir_dir / "broken.ex")
    assert result.error_count > 0
    # Still surfaces the module and the well-formed clause before the break.
    assert _find(result.declarations, kind=KIND_NAMESPACE, name="MyApp.Broken")
