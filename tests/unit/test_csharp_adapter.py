"""Tests for the C# adapter."""
from __future__ import annotations

from ast_outline.adapters.csharp import CSharpAdapter
from ast_outline.core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_EVENT,
    KIND_FIELD,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_OPERATOR,
    KIND_PROPERTY,
    KIND_RECORD,
    KIND_STRUCT,
    Declaration,
)


# --- Helpers --------------------------------------------------------------


def _find(decls, kind=None, name=None):
    """Recursive search for a declaration matching kind and/or name."""
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


def test_parse_populates_result_metadata(csharp_dir):
    path = csharp_dir / "unity_behaviour.cs"
    result = CSharpAdapter().parse(path)
    assert result.path == path
    assert result.language == "csharp"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations, "must find at least one top-level decl"


# --- Namespaces -----------------------------------------------------------


def test_traditional_namespace_wraps_types(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    ns = result.declarations[0]
    assert ns.kind == KIND_NAMESPACE
    assert ns.name == "Demo.Combat"
    # Both the class and the interface live inside the namespace.
    type_names = [c.name for c in ns.children]
    assert "HeroController" in type_names
    assert "IDamageable" in type_names


def test_file_scoped_namespace_is_detected(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    ns = result.declarations[0]
    assert ns.kind == KIND_NAMESPACE
    assert ns.name == "Demo.Services"
    type_names = {c.name for c in ns.children}
    # All top-level types end up as namespace children in file-scoped form.
    assert {"UserDto", "Vec2", "IRepository", "UserRepository", "UserExtensions"}.issubset(type_names)


# --- Types ----------------------------------------------------------------


def test_class_has_bases_attrs_and_docs(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    hero = _find(result.declarations, kind=KIND_CLASS, name="HeroController")
    assert hero is not None
    assert hero.bases == ["MonoBehaviour", "IDamageable"]
    # Attribute captured and not part of signature
    assert any("RequireComponent" in a for a in hero.attrs)
    # XML doc preserved (starts with ///)
    assert hero.docs
    assert all(line.startswith("///") for line in hero.docs)


def test_interface_members_default_to_public(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    iface = _find(result.declarations, kind=KIND_INTERFACE, name="IDamageable")
    assert iface is not None
    method = _find(iface.children, kind=KIND_METHOD, name="TakeDamage")
    assert method is not None
    # Interface members have no explicit modifier → default is "public"
    assert method.visibility == "public"


def test_explicit_private_modifier_is_captured(csharp_dir):
    """`private void Die()` — explicit modifier preserved on the IR."""
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    die = _find(result.declarations, kind=KIND_METHOD, name="Die")
    assert die is not None
    assert die.visibility == "private"


def test_record_and_record_struct_map_to_kind_record(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    user_dto = _find(result.declarations, name="UserDto")
    vec2 = _find(result.declarations, name="Vec2")
    assert user_dto is not None and user_dto.kind == KIND_RECORD
    assert vec2 is not None and vec2.kind == KIND_RECORD


def test_generic_interface_bases_preserved(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    repo = _find(result.declarations, name="UserRepository")
    assert repo is not None
    # Base list should include the generic parameter verbatim.
    assert "IRepository<UserDto>" in repo.bases


# --- Members --------------------------------------------------------------


def test_auto_property_signature_preserves_accessors(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    prop = _find(result.declarations, kind=KIND_PROPERTY, name="CurrentHealth")
    assert prop is not None
    assert "get" in prop.signature
    assert "private set" in prop.signature


def test_expression_bodied_property_keeps_arrow(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    is_alive = _find(result.declarations, kind=KIND_PROPERTY, name="IsAlive")
    assert is_alive is not None
    assert "=>" in is_alive.signature


def test_event_field_captured(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    evt = _find(result.declarations, kind=KIND_EVENT, name="OnHealthChanged")
    assert evt is not None


def test_serialize_field_attribute_inlined(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    field = _find(result.declarations, kind=KIND_FIELD, name="_speed")
    assert field is not None
    assert any("SerializeField" in a for a in field.attrs)


def test_method_xml_doc_captured_before_signature(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    take_damage = _find(result.declarations, kind=KIND_METHOD, name="TakeDamage")
    # C# docs are NOT inside the body
    assert take_damage is not None
    assert take_damage.docs_inside is False
    assert take_damage.docs
    assert any("Apply damage" in line for line in take_damage.docs)


def test_constructor_mapped_to_kind_ctor(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    ctor = _find(result.declarations, kind=KIND_CTOR, name="HeroController")
    assert ctor is not None


# --- Line ranges ----------------------------------------------------------


def test_line_ranges_are_inside_file(csharp_dir):
    path = csharp_dir / "unity_behaviour.cs"
    result = CSharpAdapter().parse(path)
    total = result.line_count
    for decl in _find_all(result.declarations):
        assert 1 <= decl.start_line <= total
        assert decl.start_line <= decl.end_line <= total


def test_take_damage_range_matches_source(csharp_dir):
    path = csharp_dir / "unity_behaviour.cs"
    result = CSharpAdapter().parse(path)
    src_lines = path.read_text().splitlines()
    td = _find(result.declarations, kind=KIND_METHOD, name="TakeDamage")
    assert "TakeDamage" in src_lines[td.start_line - 1]
    # end_line points at the closing brace line
    assert src_lines[td.end_line - 1].strip() == "}"


# --- Nested types + operators --------------------------------------------


def test_nested_enum_inside_class(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    hero = _find(result.declarations, kind=KIND_CLASS, name="HeroController")
    state = _find(hero.children, kind=KIND_ENUM, name="State")
    assert state is not None
    members = [m.name for m in state.children if m.kind == KIND_ENUM_MEMBER]
    assert members == ["Idle", "Moving", "Dead"]


def test_nested_class_inside_struct(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    money = _find(result.declarations, kind=KIND_STRUCT, name="Money")
    assert money is not None
    builder = _find(money.children, kind=KIND_CLASS, name="Builder")
    assert builder is not None
    # Builder has its own members
    methods = {m.name for m in builder.children if m.kind == KIND_METHOD}
    assert {"WithAmount", "WithCurrency", "Build"}.issubset(methods)


def test_method_overloads_both_captured(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    equals_overloads = _find_all(result.declarations, kind=KIND_METHOD, name="Equals")
    assert len(equals_overloads) == 2
    # Different line numbers
    lines = {o.start_line for o in equals_overloads}
    assert len(lines) == 2


def test_operator_and_conversion_mapped_to_kind_operator(csharp_dir):
    result = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    ops = _find_all(result.declarations, kind=KIND_OPERATOR)
    # + and - arithmetic + implicit and explicit conversion = 4
    assert len(ops) >= 4


# --- Top-level default visibility ----------------------------------------


def test_explicit_public_class_visibility(csharp_dir):
    """`public class HeroController` — explicit `public` preserved."""
    result = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    hero = _find(result.declarations, kind=KIND_CLASS, name="HeroController")
    assert hero.visibility == "public"


def test_top_level_type_without_modifier_defaults_to_internal(csharp_dir):
    """C# rule: a type in a namespace with no modifier → `internal`."""
    result = CSharpAdapter().parse(csharp_dir / "visibility_defaults.cs")
    cls = _find(result.declarations, kind=KIND_CLASS, name="DefaultInternalClass")
    assert cls is not None
    assert cls.visibility == "internal"


def test_class_member_without_modifier_defaults_to_private(csharp_dir):
    """C# rule: a class member with no modifier → `private`."""
    result = CSharpAdapter().parse(csharp_dir / "visibility_defaults.cs")
    m = _find(result.declarations, kind=KIND_METHOD, name="DefaultPrivateMethod")
    assert m is not None
    assert m.visibility == "private"
    # Field with no modifier on a class → also private
    f = _find(result.declarations, kind=KIND_FIELD, name="defaultPrivateField")
    assert f is not None
    assert f.visibility == "private"


def test_interface_member_without_modifier_defaults_to_public(csharp_dir):
    """C# rule: an interface member with no modifier → `public`."""
    result = CSharpAdapter().parse(csharp_dir / "visibility_defaults.cs")
    m = _find(result.declarations, kind=KIND_METHOD, name="InterfaceDefaultPublicMethod")
    assert m is not None
    assert m.visibility == "public"


# --- Operator naming -----------------------------------------------------


def test_operator_names_follow_convention(csharp_dir):
    """`operator+` / `operator-` for arithmetic; `operator_<Type>` for conversions."""
    result = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    op_names = {o.name for o in _find_all(result.declarations, kind=KIND_OPERATOR)}
    assert "operator+" in op_names
    assert "operator-" in op_names
    # implicit conversion to decimal, explicit conversion to Money
    assert "operator_decimal" in op_names
    assert "operator_Money" in op_names


# --- Multi-declarator fields / doc-at-byte-0 / attr edge cases -----------


def test_multi_declarator_field_emits_every_name(tmp_path):
    """`int x, y, z;` declares three fields in ONE field_declaration —
    a single-name path silently dropped `y` and `z` from the IR."""
    p = tmp_path / "a.cs"
    p.write_text("class Foo { int x, y, z; }\n", encoding="utf-8")
    r = CSharpAdapter().parse(p)
    names = [d.name for d in _find_all(r.declarations, kind=KIND_FIELD)]
    assert names == ["x", "y", "z"], names


def test_multi_declarator_event_field_emits_every_name(tmp_path):
    p = tmp_path / "a.cs"
    p.write_text(
        "class Foo { event System.EventHandler A, B; }\n", encoding="utf-8"
    )
    r = CSharpAdapter().parse(p)
    names = [d.name for d in _find_all(r.declarations, kind=KIND_EVENT)]
    assert names == ["A", "B"], names


def test_doc_comment_at_byte_zero_included_in_show_slice(tmp_path):
    """A file that OPENS with `///` puts the doc block at byte 0 —
    `doc_start_byte or start_byte` treated that 0 as "no doc" and the
    `show` slice skipped the doc entirely."""
    from ast_outline.core import find_symbols
    p = tmp_path / "a.cs"
    p.write_text("/// Summary\nclass Foo {}\n", encoding="utf-8")
    r = CSharpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    assert cls.doc_start_byte == 0
    m = find_symbols(r, "Foo")[0]
    assert m.source.startswith("/// Summary"), m.source


def test_attribute_with_bracket_inside_string_argument(tmp_path):
    """A `]` inside a quoted attribute argument is content, not the
    attribute-list closer — the naive depth scan cut the signature
    mid-string (`in it")] public class Foo`)."""
    p = tmp_path / "a.cs"
    p.write_text(
        '[System.ComponentModel.Description("has ] in it")]\n'
        "public class Foo {}\n",
        encoding="utf-8",
    )
    r = CSharpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    assert cls.signature == "public class Foo", cls.signature
