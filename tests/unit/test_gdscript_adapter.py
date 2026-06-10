"""Tests for the GDScript adapter (hand-written parser — no tree-sitter)."""
from __future__ import annotations

from ast_outline.adapters import get_adapter_for
from ast_outline.adapters.gdscript import GDScriptAdapter
from ast_outline.core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_EVENT,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_PROPERTY,
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


# --- Registration / parse smoke --------------------------------------------


def test_gd_extension_resolves_to_adapter(gdscript_dir):
    adapter = get_adapter_for(gdscript_dir / "player.gd")
    assert isinstance(adapter, GDScriptAdapter)


def test_parse_populates_result_metadata(gdscript_dir):
    path = gdscript_dir / "player.gd"
    result = GDScriptAdapter().parse(path)
    assert result.path == path
    assert result.language == "gdscript"
    assert result.line_count > 0
    assert result.declarations, "should find decls"
    assert result.error_count == 0


# --- Script header: class_name + extends ------------------------------------


def test_class_name_and_extends_merge_into_one_class(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    classes = _find_all(result.declarations, kind=KIND_CLASS)
    headers = [c for c in classes if c.native_kind == "class_name"]
    assert len(headers) == 1
    player = headers[0]
    assert player.name == "Player"
    assert player.bases == ["CharacterBody2D"]
    assert player.signature == "class_name Player extends CharacterBody2D"
    # No leftover separate `extends` node — one script, one type.
    assert not [c for c in classes if c.native_kind == "extends"]


def test_script_header_annotations_and_docs(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    player = _find(result.declarations, kind=KIND_CLASS, name="Player")
    assert player.attrs == ['@icon("res://icons/player.svg")']
    assert any("Player avatar" in line for line in player.docs)
    assert player.docs_inside is False
    # Span covers @icon + class_name + extends lines.
    assert player.start_line == 3
    assert player.end_line == 5


def test_extends_only_script_becomes_extends_class_node(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "attached_script.gd")
    head = _find(result.declarations, kind=KIND_CLASS)
    assert head.native_kind == "extends"
    assert head.name == "Node2D"
    assert head.signature == "extends Node2D"
    assert head.start_line == head.end_line == 1


# --- Signals, enums, constants ----------------------------------------------


def test_signal_is_event(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    died = _find(result.declarations, kind=KIND_EVENT, name="died")
    assert died is not None
    assert died.native_kind == "signal"
    assert died.signature == "signal died(cause)"


def test_named_enum_with_members(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    state = _find(result.declarations, kind=KIND_ENUM, name="State")
    assert state is not None
    assert [m.name for m in state.children] == ["IDLE", "RUNNING", "JUMPING", "DEAD"]
    jumping = _find(state.children, kind=KIND_ENUM_MEMBER, name="JUMPING")
    assert jumping.signature == "JUMPING = 10"
    # Members inherit the enum's range so `show` prints useful context.
    assert jumping.start_line == state.start_line
    assert jumping.end_line == state.end_line


def test_anonymous_enum(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    anon = _find(result.declarations, kind=KIND_ENUM, name="<anonymous>")
    assert anon is not None
    assert [m.name for m in anon.children] == ["FLAG_A", "FLAG_B"]


def test_const_signatures(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    typed = _find(result.declarations, kind=KIND_FIELD, name="MAX_SPEED")
    assert typed.signature == "const MAX_SPEED: float"
    untyped = _find(result.declarations, kind=KIND_FIELD, name="GRAVITY")
    assert untyped.signature == "const GRAVITY"


def test_preload_const_keeps_value_and_feeds_imports(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    bullet = _find(result.declarations, kind=KIND_FIELD, name="BulletScene")
    assert bullet.signature == 'const BulletScene = preload("res://weapons/bullet.tscn")'
    assert result.imports == ['const BulletScene = preload("res://weapons/bullet.tscn")']
    assert len(result.import_regions) == 1
    start, end = result.import_regions[0]
    assert b"preload" in result.source[start:end]


def test_load_inside_function_counts_as_conditional_import(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    assert result.conditional_imports_count == 1


# --- Variables, annotations, properties -------------------------------------


def test_export_annotation_becomes_attr(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    hp = _find(result.declarations, kind=KIND_FIELD, name="max_health")
    assert hp.attrs == ["@export"]
    assert hp.signature == "var max_health: int"
    friction = _find(result.declarations, kind=KIND_FIELD, name="friction")
    assert friction.attrs == ["@export_range(0.0, 1.0)"]
    assert friction.signature == "var friction"  # := inferred, no type


def test_onready_annotation_and_node_path_value(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    sprite = _find(result.declarations, kind=KIND_FIELD, name="sprite")
    assert sprite.attrs == ["@onready"]
    assert sprite.signature == "var sprite: Sprite2D"


def test_static_var(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    count = _find(result.declarations, name="instance_count")
    assert count.kind == KIND_FIELD
    assert count.signature == "static var instance_count: int"


def test_property_with_get_set_block(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    health = _find(result.declarations, name="health")
    assert health.kind == KIND_PROPERTY
    assert health.signature == "var health: int"
    # Body spans the get/set block.
    assert health.end_line > health.start_line
    assert any("Current health" in line for line in health.docs)
    # Locals inside the setter must not leak out as declarations.
    assert _find(result.declarations, name="old") is None


def test_property_reference_block_form(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    label = _find(result.declarations, name="speed_label")
    assert label.kind == KIND_PROPERTY
    assert label.signature == "var speed_label: String"


def test_property_reference_inline_forms(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    typed_ref = _find(result.declarations, name="ref_prop")
    assert typed_ref.kind == KIND_PROPERTY
    assert typed_ref.signature == "var ref_prop: int"
    valued_ref = _find(result.declarations, name="valued_ref")
    assert valued_ref.kind == KIND_PROPERTY


def test_lambda_initializers_stay_fields(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    callback = _find(result.declarations, name="callback")
    assert callback.kind == KIND_FIELD
    lam = _find(result.declarations, name="named_lambda")
    assert lam.kind == KIND_FIELD  # block lambda body, NOT a property
    assert lam.end_line > lam.start_line  # ...but the body still folds in
    assert _find(result.declarations, name="local_inside_lambda") is None
    assert _find(result.declarations, name="heavy") is None


# --- Functions ---------------------------------------------------------------


def test_top_level_func_is_function(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    dmg = _find(result.declarations, kind=KIND_FUNCTION, name="take_damage")
    assert dmg is not None
    assert dmg.signature == "func take_damage(amount: int, source: Node = null) -> void"
    assert any("Applies damage" in line for line in dmg.docs)
    assert dmg.end_line > dmg.start_line


def test_init_is_ctor(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    init = _find(result.declarations, kind=KIND_CTOR, name="_init")
    assert init is not None
    assert init.visibility == ""


def test_static_func_signature(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    factory = _find(result.declarations, name="from_save")
    assert factory.signature == "static func from_save(data: Dictionary) -> Player"
    assert _find(result.declarations, name="p") is None  # local, not captured


def test_engine_callbacks_are_not_private(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    ready = _find(result.declarations, name="_ready")
    assert ready.visibility == ""
    physics = _find(result.declarations, name="_physics_process")
    assert physics.visibility == ""
    # Plain underscore helpers stay private by convention.
    helper = _find(result.declarations, name="_get_speed_label")
    assert helper.visibility == "private"
    state = _find(result.declarations, name="_state")
    assert state.visibility == "private"


def test_function_locals_not_captured(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    assert _find(result.declarations, name="bullet") is None
    assert _find(result.declarations, name="fx") is None


def test_inline_body_function_is_single_line(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    fn = _find(result.declarations, name="inline_body")
    assert fn is not None
    assert fn.start_line == fn.end_line


def test_abstract_func_has_no_body(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    fn = _find(result.declarations, name="must_override")
    assert fn is not None
    assert fn.attrs == ["@abstract"]
    assert fn.signature == "func must_override(amount: int) -> void"
    assert fn.start_line == fn.end_line


def test_multiline_signature_collapsed(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    fn = _find(result.declarations, name="compute")
    assert "first: int" in fn.signature
    assert "-> int" in fn.signature
    assert "\n" not in fn.signature
    # The signature spans physical lines; the body extends further.
    assert fn.end_line > fn.start_line


# --- Inner classes -----------------------------------------------------------


def test_inner_class_nests_members(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "player.gd")
    inv = _find(result.declarations, kind=KIND_CLASS, name="Inventory")
    assert inv is not None
    assert inv.native_kind == "class"
    names = [c.name for c in inv.children]
    assert names == ["_items", "add", "count"]
    add = _find(inv.children, name="add")
    assert add.kind == KIND_METHOD  # method inside a class, not a free function
    assert inv.end_line >= add.end_line


# --- Strings / comments can't fake declarations ------------------------------


def test_declarations_inside_strings_ignored(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    assert _find(result.declarations, name="fake_function") is None
    assert _find(result.declarations, name="FakeClass") is None
    assert _find(result.declarations, name="in_comment") is None
    assert _find(result.declarations, name="in_string") is None


def test_triple_string_recorded_as_noise_region(gdscript_dir):
    path = gdscript_dir / "edge_cases.gd"
    result = GDScriptAdapter().parse(path)
    idx = result.source.find(b"fake_function")
    assert idx != -1
    assert any(
        start <= idx < end and kind == "string"
        for start, end, kind in result.noise_regions
    )


def test_string_prefixes_do_not_break_scanning(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    for name in ("RAW_PATH", "SIG_NAME", "NODE_PATH"):
        assert _find(result.declarations, kind=KIND_FIELD, name=name) is not None
    assert result.error_count == 0


def test_backslash_continuation_joins_statement(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    cont = _find(result.declarations, name="with_continuation")
    assert cont is not None
    assert cont.end_line == cont.start_line + 1


def test_semicolon_separated_statements_both_captured(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    a = _find(result.declarations, name="a")
    b = _find(result.declarations, name="b")
    assert a is not None and b is not None
    assert a.start_line == b.start_line


def test_annotation_args_with_space_before_paren(gdscript_dir):
    """Godot tokenizes, so `@export_enum ("A", "B") var x` (space before
    the argument parens) is legal — found in material-maker."""
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    shape = _find(result.declarations, kind=KIND_FIELD, name="shape_kind")
    assert shape is not None
    assert shape.attrs == ['@export_enum ("Sphere", "Octahedron")']
    assert shape.signature == "var shape_kind: int"


def test_plain_string_with_raw_newlines(gdscript_dir):
    """Godot allows raw newlines in ANY string literal, not just
    triple-quoted ones — real projects (dialogic, phantom-camera) ship
    plain `"` strings spanning lines."""
    result = GDScriptAdapter().parse(gdscript_dir / "edge_cases.gd")
    assert result.error_count == 0
    banner = _find(result.declarations, name="banner")
    assert banner.kind == KIND_FIELD
    assert banner.end_line == banner.start_line + 2
    assert _find(result.declarations, name="not_a_decl") is None
    assert _find(result.declarations, kind=KIND_EVENT, name="after_banner") is not None
    # The multiline string is a noise region even without triple quotes.
    idx = result.source.find(b"not_a_decl")
    assert any(
        s <= idx < e and kind == "string" for s, e, kind in result.noise_regions
    )


# --- Godot 3 compatibility ----------------------------------------------------


def test_godot3_export_onready_modifiers(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "legacy_godot3.gd")
    title = _find(result.declarations, name="title")
    assert title.signature == "export var title"
    power = _find(result.declarations, name="power")
    assert power.signature == "export(int, 0, 10) var power"
    mesh = _find(result.declarations, name="mesh")
    assert mesh.signature == "onready var mesh"


def test_godot3_setget_is_property(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "legacy_godot3.gd")
    health = _find(result.declarations, name="health")
    assert health.kind == KIND_PROPERTY


def test_godot3_rpc_keywords_kept_in_signature(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "legacy_godot3.gd")
    fn = _find(result.declarations, name="request_action")
    assert fn.signature == "master func request_action(action)"


# --- Line endings / byte offsets ----------------------------------------------


def test_crlf_file_parses_and_excludes_cr_from_end_byte(tmp_path):
    src = (
        b"extends Node\r\n"
        b"\r\n"
        b"func greet() -> void:\r\n"
        b"\tprint(1)\r\n"
    )
    path = tmp_path / "crlf.gd"
    path.write_bytes(src)
    result = GDScriptAdapter().parse(path)
    assert result.error_count == 0
    fn = _find(result.declarations, name="greet")
    assert fn is not None
    # `show` slices source[start_byte:end_byte] — must not end in a bare CR.
    assert not result.source[fn.start_byte : fn.end_byte].endswith(b"\r")


def test_doc_comment_at_byte_zero_survives_header_merge(tmp_path):
    path = tmp_path / "documented.gd"
    path.write_bytes(b"## Doc at byte zero.\nextends Node\nclass_name Zed\n")
    result = GDScriptAdapter().parse(path)
    head = _find(result.declarations, kind=KIND_CLASS, name="Zed")
    assert head is not None
    assert head.docs == ["## Doc at byte zero."]
    # Offset 0 is a real doc offset, not "no docs".
    assert head.doc_start_byte == 0


# --- Broken input -------------------------------------------------------------


def test_unterminated_string_reports_error_and_keeps_earlier_decls(gdscript_dir):
    result = GDScriptAdapter().parse(gdscript_dir / "broken_syntax.gd")
    assert result.error_count >= 1
    assert _find(result.declarations, name="ok") is not None
    assert _find(result.declarations, name="phantom") is None
    # The runaway string is noise to its end.
    idx = result.source.find(b"phantom")
    assert any(s <= idx < e for s, e, _ in result.noise_regions)
