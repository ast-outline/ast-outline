"""Tests for the Lua adapter."""
from __future__ import annotations

from ast_outline.adapters.lua import LuaAdapter
from ast_outline.core import (
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_OPERATOR,
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


def test_parse_populates_result_metadata(lua_dir):
    path = lua_dir / "module_pattern.lua"
    result = LuaAdapter().parse(path)
    assert result.path == path
    assert result.language == "lua"
    assert result.line_count > 0
    assert result.declarations, "should find decls"
    assert result.error_count == 0


# --- The classic ``local M = {} ... return M`` shape ----------------------


def test_dotted_function_decl_is_public_function(lua_dir):
    result = LuaAdapter().parse(lua_dir / "module_pattern.lua")
    fn = _find(result.declarations, name="M.greet")
    assert fn is not None
    assert fn.kind == KIND_FUNCTION
    assert fn.visibility == ""  # public
    assert fn.signature == "function M.greet(name)"


def test_local_function_is_private(lua_dir):
    result = LuaAdapter().parse(lua_dir / "module_pattern.lua")
    fn = _find(result.declarations, name="_normalize")
    assert fn is not None
    assert fn.kind == KIND_FUNCTION
    assert fn.visibility == "private"


def test_module_table_field_is_public_field(lua_dir):
    result = LuaAdapter().parse(lua_dir / "module_pattern.lua")
    f = _find(result.declarations, kind=KIND_FIELD, name="M.DEFAULT_GREETING")
    assert f is not None
    assert f.visibility == ""


def test_leading_comments_attached_as_docs(lua_dir):
    result = LuaAdapter().parse(lua_dir / "module_pattern.lua")
    fn = _find(result.declarations, name="M.greet")
    assert fn is not None
    # The fixture has two ``---`` lines preceding the decl.
    assert fn.docs, "leading --- lines should attach as docs"


# --- Colon-vs-dot method discrimination -----------------------------------


def test_colon_decl_is_method_kind(lua_dir):
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    m = _find(result.declarations, name="Animal:speak")
    assert m is not None
    assert m.kind == KIND_METHOD


def test_dot_decl_is_function_kind(lua_dir):
    """``function Animal.new()`` has no implicit ``self`` — it is
    KIND_FUNCTION even though it lives on a class-like table."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    m = _find(result.declarations, name="Animal.new")
    assert m is not None
    assert m.kind == KIND_FUNCTION


# --- Metamethods ---------------------------------------------------------


def test_metamethod_method_style_is_operator(lua_dir):
    """``function Animal:__tostring()`` — method-style metamethod
    decl. The metamethod name dominates kind classification, so it's
    KIND_OPERATOR, not KIND_METHOD."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    op = _find(result.declarations, name="Animal:__tostring")
    assert op is not None
    assert op.kind == KIND_OPERATOR


def test_metamethod_assignment_style_is_operator(lua_dir):
    """``Animal.__eq = function() end`` — assignment-style metamethod.
    Same KIND_OPERATOR classification."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    op = _find(result.declarations, name="Animal.__eq")
    assert op is not None
    assert op.kind == KIND_OPERATOR


def test_metamethod_non_function_value_is_operator(lua_dir):
    """``Animal.__index = Animal`` — ``__index`` set to a table, not
    a function. Still KIND_OPERATOR (protocol declaration)."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    idx = _find(result.declarations, name="Animal.__index")
    assert idx is not None
    assert idx.kind == KIND_OPERATOR


def test_metamethod_name_does_not_demote_to_private(lua_dir):
    """``__add``, ``__index``, ... start with ``_`` but are public
    protocol — the visibility heuristic must NOT classify them as
    private just because of the underscore prefix."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    for name in ("Animal.__index", "Animal:__tostring", "Animal.__eq"):
        d = _find(result.declarations, name=name)
        assert d is not None and d.visibility == "", name


def test_nonstandard_dunder_is_private(tmp_path):
    """``M.__custom_hook`` — not in the standard metamethod set, but
    starts with ``__``. The visibility heuristic should treat any
    underscore-prefixed name that ISN'T a real metamethod as private
    (the ``__`` prefix outside the protocol still signals "internal"
    in convention). Regression test for an early bug where the dunder
    check excluded all ``__``-prefixed names from the private
    classification, returning public for non-standard hooks."""
    path = tmp_path / "custom.lua"
    path.write_text(
        "local M = {}\n"
        "function M.__custom_hook() end\n"
        "function M.__internal() end\n"
        "return M\n",
        encoding="utf-8",
    )
    result = LuaAdapter().parse(path)
    hook = _find(result.declarations, name="M.__custom_hook")
    internal = _find(result.declarations, name="M.__internal")
    assert hook is not None and hook.visibility == "private"
    assert internal is not None and internal.visibility == "private"


# --- Direct-return-table module shape -------------------------------------


def test_direct_return_table_exposes_function_fields(lua_dir):
    result = LuaAdapter().parse(lua_dir / "direct_return.lua")
    add = _find(result.declarations, name="add")
    sub = _find(result.declarations, name="sub")
    assert add is not None and add.kind == KIND_FUNCTION
    assert sub is not None and sub.kind == KIND_FUNCTION
    assert add.signature == "function add(a, b)"


def test_direct_return_table_exposes_field_constants(lua_dir):
    result = LuaAdapter().parse(lua_dir / "direct_return.lua")
    v = _find(result.declarations, name="VERSION")
    assert v is not None and v.kind == KIND_FIELD


def test_direct_return_table_recognises_metamethods(lua_dir):
    result = LuaAdapter().parse(lua_dir / "direct_return.lua")
    op = _find(result.declarations, name="__call")
    assert op is not None and op.kind == KIND_OPERATOR


# --- Neovim plugin shape --------------------------------------------------


def test_setup_function_is_public(lua_dir):
    result = LuaAdapter().parse(lua_dir / "neovim_plugin.lua")
    setup = _find(result.declarations, name="M.setup")
    assert setup is not None and setup.kind == KIND_FUNCTION
    assert setup.visibility == ""


def test_underscore_prefix_local_function_is_private(lua_dir):
    result = LuaAdapter().parse(lua_dir / "neovim_plugin.lua")
    fn = _find(result.declarations, name="_validate")
    assert fn is not None and fn.visibility == "private"


def test_table_field_with_table_value_is_field(lua_dir):
    """``M.config = { ... }`` — table-valued field, KIND_FIELD."""
    result = LuaAdapter().parse(lua_dir / "neovim_plugin.lua")
    cfg = _find(result.declarations, name="M.config")
    assert cfg is not None and cfg.kind == KIND_FIELD


# --- LÖVE callback shape (global table-member assignment) -----------------


def test_love_callback_is_public_function(lua_dir):
    result = LuaAdapter().parse(lua_dir / "love_callbacks.lua")
    load = _find(result.declarations, name="love.load")
    update = _find(result.declarations, name="love.update")
    assert load is not None and load.kind == KIND_FUNCTION and load.visibility == ""
    assert update is not None and update.signature == "function love.update(dt)"


# --- require / imports ---------------------------------------------------


def test_bare_require_is_static_import(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    assert any(imp.startswith("require ") and "socket" in imp for imp in result.imports)


def test_parenthesised_require_is_static_import(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    assert any('require("socket.http")' == imp for imp in result.imports)


def test_local_assigned_require_is_static_import(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    # The fixture has ``local json = require("dkjson")`` and
    # ``local ltn12 = require "ltn12"``. The require call's
    # source-true text becomes the imports line; the ``local X =``
    # prefix is intentionally NOT preserved (other adapters render
    # ``import X``, not ``local X = import X``).
    joined = "\n".join(result.imports)
    assert "dkjson" in joined
    assert "ltn12" in joined


def test_require_inside_function_body_is_conditional(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    assert result.conditional_imports_count >= 1


def test_static_require_populates_import_regions(lua_dir):
    """Static-require statements register their full byte range in
    ``import_regions`` so the grep classifier can promote
    ``local X = require(...)`` lines to ``[import]`` even though the
    line prefix is ``local``."""
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    assert result.import_regions, "static requires should populate regions"


# --- Lua 5.4 attribute markers --------------------------------------------


def test_const_attribute_captured(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    pi = _find(result.declarations, name="PI")
    assert pi is not None
    assert "<const>" in pi.attrs


def test_close_attribute_captured(lua_dir):
    result = LuaAdapter().parse(lua_dir / "requires_and_attrs.lua")
    f = _find(result.declarations, name="FILE")
    assert f is not None
    assert "<close>" in f.attrs


# --- Nested / multi-level dotted names ------------------------------------


def test_deeply_nested_dotted_function_name(lua_dir):
    result = LuaAdapter().parse(lua_dir / "nested_names.lua")
    fn = _find(result.declarations, name="ns.deep.nested.helper")
    assert fn is not None and fn.kind == KIND_FUNCTION


def test_method_on_nested_namespace(lua_dir):
    result = LuaAdapter().parse(lua_dir / "nested_names.lua")
    m = _find(result.declarations, name="ns.deep:methodOnNested")
    assert m is not None and m.kind == KIND_METHOD


# --- Strings & comments --------------------------------------------------


def test_long_block_comment_in_noise_regions(lua_dir):
    """``--[[ ... ]]`` block comments span multiple lines and must
    appear as ``comment`` ranges in ``noise_regions`` so the grep
    command can filter matches inside them."""
    result = LuaAdapter().parse(lua_dir / "strings_and_comments.lua")
    comment_regions = [r for r in result.noise_regions if r[2] == "comment"]
    assert any(
        end - start > 50  # block comment, not a one-liner
        for start, end, _ in comment_regions
    )


def test_long_string_in_noise_regions(lua_dir):
    """``[[ ... ]]`` long-bracket strings span lines and live in
    ``noise_regions`` as ``string`` ranges. A grep for a name that
    appears only inside such a string should classify as KIND_STRING,
    not KIND_REF."""
    result = LuaAdapter().parse(lua_dir / "strings_and_comments.lua")
    string_regions = [r for r in result.noise_regions if r[2] == "string"]
    assert any(end - start > 30 for start, end, _ in string_regions)


def test_level_2_long_bracket_comment_handled(lua_dir):
    """``--[==[ ... ]==]`` — level-2 long-bracket comment must also
    land in ``noise_regions``. The grammar handles every level
    uniformly, but the test pins the contract."""
    result = LuaAdapter().parse(lua_dir / "strings_and_comments.lua")
    comment_regions = [r for r in result.noise_regions if r[2] == "comment"]
    # We should have at least two block comments (level-0 and level-2).
    big_blocks = [r for r in comment_regions if r[1] - r[0] > 50]
    assert len(big_blocks) >= 2


# --- Broken syntax --------------------------------------------------------


def test_broken_syntax_reports_errors(lua_dir):
    result = LuaAdapter().parse(lua_dir / "broken_syntax.lua")
    assert result.error_count > 0


def test_broken_syntax_still_surfaces_valid_decls_before_break(lua_dir):
    result = LuaAdapter().parse(lua_dir / "broken_syntax.lua")
    ok = _find(result.declarations, name="ok_fn")
    assert ok is not None, "declarations before the parse error should survive"


# --- Empty file ----------------------------------------------------------


def test_empty_file_no_declarations(lua_dir):
    result = LuaAdapter().parse(lua_dir / "empty.lua")
    assert result.error_count == 0
    assert result.declarations == []


# --- Anonymous-function assignment ---------------------------------------


def test_assignment_with_function_value_is_function_kind(lua_dir):
    """``Animal.__eq = function() end`` — assignment to a function-
    valued RHS produces KIND_OPERATOR (because ``__eq`` is a
    metamethod). The non-metamethod variant is tested below."""
    result = LuaAdapter().parse(lua_dir / "mt_class.lua")
    eq = _find(result.declarations, name="Animal.__eq")
    assert eq is not None and eq.kind == KIND_OPERATOR


# --- Grep classifier (Lua-specific) ---------------------------------------
#
# The Lua quirks are adapter-declared attributes (`name_chain_separators`,
# `call_sugar_openers`) consumed by grep's call-vs-ref walker. The tests
# pass the LuaAdapter's own declarations so they cover both the walker
# logic and the adapter's attribute values.


def _lua_walker_kwargs() -> dict:
    return dict(
        name_chain_separators=LuaAdapter.name_chain_separators,
        call_sugar_openers=LuaAdapter.call_sugar_openers,
    )


def test_lua_colon_method_call_classifies_as_call():
    """``obj:method()`` matched on ``obj`` — Lua chain skipping
    promotes the receiver match to KIND_CALL, matching the v0.8.12
    bias-toward-call policy."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("obj:method(x)", 3, **_lua_walker_kwargs())


def test_lua_dot_chain_call_classifies_as_call():
    """``a.b.c.d(x)`` matched on ``a`` — deep dot chain ending in
    a call should classify as KIND_CALL under the Lua walker."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("a.b.c.d(x)", 1, **_lua_walker_kwargs())


def test_lua_dot_chain_without_call_is_ref():
    """``obj.field`` matched on ``obj`` — no trailing call shape,
    so the chain skipping reaches end-of-line and returns False."""
    from ast_outline.grep import _next_call_paren_after
    assert not _next_call_paren_after("obj.field", 3, **_lua_walker_kwargs())


def test_lua_string_sugar_call():
    """``f"hello"`` — bare-string-arg sugar call, Lua-only."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after('f"hello"', 1, **_lua_walker_kwargs())


def test_lua_table_sugar_call():
    """``f{1, 2}`` — table-arg sugar call, Lua-only."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("f{1, 2}", 1, **_lua_walker_kwargs())


def test_lua_long_string_sugar_call_double_bracket():
    """``f[[long string]]`` — long-bracket-string sugar call. Must
    win over the bare ``[`` subscript case below."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("f[[long]]", 1, **_lua_walker_kwargs())


def test_lua_subscript_is_not_call():
    """``f[1]`` — single ``[`` is a table subscript, NOT a call. The
    sugar openers deliberately list ``[[`` but not bare ``[``."""
    from ast_outline.grep import _next_call_paren_after
    assert not _next_call_paren_after("f[1]", 1, **_lua_walker_kwargs())


def test_lua_classifier_does_not_leak_to_other_languages():
    """The same shapes that classify as call with the Lua attributes
    must NOT classify as call without them (the default for every
    other adapter) — chain skipping and sugar calls are Lua-specific."""
    from ast_outline.grep import _next_call_paren_after
    # ``f"x"`` in TypeScript is not a call (it would be invalid syntax
    # or a tagged-template; we don't promote it to call).
    assert not _next_call_paren_after('f"x"', 1)
    # ``f{x}`` in Rust is a struct literal, not a call.
    assert not _next_call_paren_after("f{x}", 1)
    # ``obj.field`` in TS / Python / Rust without a trailing ``(`` is
    # still a ref — chain skipping is Lua-only.
    assert not _next_call_paren_after("obj.method(x)", 3)
