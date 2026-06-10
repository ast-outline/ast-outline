"""Tests for the C++ adapter."""
from __future__ import annotations

from ast_outline.adapters.cpp import CppAdapter
from ast_outline.core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_OPERATOR,
    KIND_STRUCT,
    Declaration,
)


# --- Helpers --------------------------------------------------------------


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


def test_parse_populates_result_metadata(cpp_dir):
    path = cpp_dir / "widget.h"
    result = CppAdapter().parse(path)
    assert result.path == path
    assert result.language == "cpp"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extensions_cover_common_cpp_suffixes():
    exts = CppAdapter.extensions
    # Bread-and-butter — implementation + headers
    for ext in (".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh"):
        assert ext in exts


# --- Imports --------------------------------------------------------------


def test_includes_collected_as_imports(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    # Source-true: `#include` keyword stays, both <…> and "…" forms preserved.
    assert "#include <vector>" in result.imports
    assert "#include <string>" in result.imports
    assert '#include "base.h"' in result.imports


# --- Namespaces -----------------------------------------------------------


def test_cpp17_nested_namespace_renders_with_double_colon(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="ui::widgets")
    assert ns is not None
    # Types live under the namespace as children.
    assert any(c.name == "Widget" for c in ns.children)


def test_old_style_single_chain_collapses(cpp_dir):
    """`namespace a { namespace b { namespace c { ... } } }` with one
    child at every level should fold into a single `namespace a::b::c`."""
    result = CppAdapter().parse(cpp_dir / "nested_namespaces.h")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="solo::deep::nested")
    assert ns is not None
    assert any(c.name == "OnlyClass" for c in ns.children)


def test_namespace_collapse_preserves_sibling_declarations(cpp_dir):
    """`namespace a { using X = …; namespace b { … } }` — collapse
    must NOT fire when the level carries non-namespace siblings, or
    those siblings (using-decls, typedefs, free functions, types
    declared at this level) would silently disappear from the IR."""
    result = CppAdapter().parse(cpp_dir / "namespace_with_siblings.h")
    # `outer` has typedef + using-alias siblings alongside `inner`,
    # so it must NOT collapse to `outer::inner`.
    outer = _find(result.declarations, kind=KIND_NAMESPACE, name="outer")
    assert outer is not None, "outer must remain as a separate namespace"
    inner = _find(outer.children, kind=KIND_NAMESPACE, name="inner")
    assert inner is not None
    assert any(c.name == "Tucked" for c in inner.children)
    # `mixed` has a free function alongside `deep` — same expectation.
    mixed = _find(result.declarations, kind=KIND_NAMESPACE, name="mixed")
    assert mixed is not None
    assert any(
        c.kind == KIND_FUNCTION and c.name == "hello" for c in mixed.children
    )
    deep = _find(mixed.children, kind=KIND_NAMESPACE, name="deep")
    assert deep is not None


def test_old_style_with_siblings_does_not_collapse(cpp_dir):
    """`namespace splits { namespace one { ... } namespace two { ... } }`
    has two children at the inner level — collapse must stop at the
    sibling boundary so each branch stays distinct."""
    result = CppAdapter().parse(cpp_dir / "nested_namespaces.h")
    splits = _find(result.declarations, kind=KIND_NAMESPACE, name="splits")
    assert splits is not None
    inner_names = {c.name for c in splits.children if c.kind == KIND_NAMESPACE}
    assert inner_names == {"one", "two"}


def test_anonymous_namespace_is_marked(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    anon = _find(result.declarations, kind=KIND_NAMESPACE, name="<anonymous>")
    assert anon is not None


def test_inline_namespace_keeps_keyword_in_name(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    inline_ns = _find(result.declarations, kind=KIND_NAMESPACE, name="inline v1")
    assert inline_ns is not None
    assert any(c.name == "Versioned" for c in inline_ns.children)


# --- Types ----------------------------------------------------------------


def test_class_with_bases_captures_access_specifiers(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    widget = _find(result.declarations, kind=KIND_CLASS, name="Widget")
    assert widget is not None
    assert widget.bases == ["public Base", "protected Themed"]
    assert widget.native_kind == "class"


def test_struct_native_kind_is_struct(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    point = _find(result.declarations, kind=KIND_STRUCT, name="Point")
    assert point is not None
    assert point.native_kind == "struct"


def test_enum_class_and_classic_enum_both_map_to_enum(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    color = _find(result.declarations, kind=KIND_ENUM, name="Color")
    mode = _find(result.declarations, kind=KIND_ENUM, name="Mode")
    assert color is not None
    assert mode is not None
    # Enumerators surface as KIND_ENUM_MEMBER children
    color_members = {c.name for c in color.children if c.kind == KIND_ENUM_MEMBER}
    assert color_members == {"Red", "Green", "Blue"}


def test_template_class_signature_carries_template_prefix(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    arr = _find(result.declarations, kind=KIND_CLASS, name="FixedArray")
    assert arr is not None
    assert arr.signature.startswith("template<typename T, int N>")
    assert "class FixedArray" in arr.signature


# --- Members --------------------------------------------------------------


def test_class_member_visibility_tracks_access_specifiers(cpp_dir):
    """Class members default to private; `public:` and `protected:`
    blocks change the level for everything that follows."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    widget = _find(result.declarations, kind=KIND_CLASS, name="Widget")
    assert widget is not None
    by_name = {c.name: c for c in widget.children}
    # `Widget()` is under `public:`
    assert by_name["Widget"].visibility == "public"
    # `~Widget()` also under public
    assert by_name["~Widget"].visibility == "public"
    # `x_`, `y_` declared under `protected:`
    assert by_name["x_"].visibility == "protected"
    assert by_name["y_"].visibility == "protected"
    # `kMax`, `w_`, `h_`, `data_` under `private:`
    assert by_name["kMax"].visibility == "private"
    assert by_name["w_"].visibility == "private"
    assert by_name["data_"].visibility == "private"


def test_struct_members_default_to_public(cpp_dir):
    """C++ default member visibility is `public` for `struct`."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    point = _find(result.declarations, kind=KIND_STRUCT, name="Point")
    assert point is not None
    fields = [c for c in point.children if c.kind == KIND_FIELD]
    assert fields, "struct Point should have at least one field"
    assert all(f.visibility == "public" for f in fields)


def test_constructor_classification(cpp_dir):
    """`Widget()` and `Widget(int, int)` both classify as KIND_CTOR
    because the function name matches the enclosing class."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    widget = _find(result.declarations, kind=KIND_CLASS, name="Widget")
    ctors = [c for c in widget.children if c.kind == KIND_CTOR]
    assert len(ctors) == 2


def test_destructor_classification(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    widget = _find(result.declarations, kind=KIND_CLASS, name="Widget")
    dtors = [c for c in widget.children if c.kind == KIND_DTOR]
    assert len(dtors) == 1
    assert dtors[0].name == "~Widget"


def test_pure_virtual_method_is_in_signature(cpp_dir):
    """`virtual void draw() const = 0;` — the `= 0` pure-virtual marker
    should survive in the signature so an LLM sees the contract."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    draw = _find(result.declarations, kind=KIND_METHOD, name="draw")
    assert draw is not None
    assert "= 0" in draw.signature
    assert "virtual" in draw.signature


def test_template_method_inside_class(cpp_dir):
    """`template<typename T> T cast() const;` — the template header
    survives as the signature prefix."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    cast = _find(result.declarations, kind=KIND_METHOD, name="cast")
    assert cast is not None
    assert cast.signature.startswith("template<typename T>")


def test_field_with_initialiser(cpp_dir):
    """`int x_ = 0;` — initialiser visible in signature."""
    result = CppAdapter().parse(cpp_dir / "widget.h")
    x_ = _find(result.declarations, kind=KIND_FIELD, name="x_")
    assert x_ is not None
    assert "= 0" in x_.signature


# --- Operators / special members -----------------------------------------


def test_overloaded_operators_classify_as_operator(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "operators_and_special.h")
    money = _find(result.declarations, kind=KIND_CLASS, name="Money")
    operators = [c for c in money.children if c.kind == KIND_OPERATOR]
    op_names = {c.name for c in operators}
    # operator=, operator+, operator==, plus the two conversion operators
    assert "operator=" in op_names
    assert "operator+" in op_names
    assert "operator==" in op_names
    # Conversion operators carry the target type in the name
    assert any(n.startswith("operator ") and "bool" in n for n in op_names)
    assert any(n.startswith("operator ") and "double" in n for n in op_names)


def test_defaulted_and_deleted_clauses_kept_in_signature(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "operators_and_special.h")
    money = _find(result.declarations, kind=KIND_CLASS, name="Money")
    sigs = [c.signature for c in money.children]
    assert any("= default" in s for s in sigs)
    assert any("= delete" in s for s in sigs)


# --- Out-of-class definitions --------------------------------------------


def test_out_of_class_member_definitions_render_qualified(cpp_dir):
    """`void Widget::draw() const { … }` at namespace scope keeps the
    qualified name in the IR so the relationship to the declaring class
    stays visible."""
    result = CppAdapter().parse(cpp_dir / "impl.cpp")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="ui::widgets")
    assert ns is not None
    qualified = [c for c in ns.children if "::" in c.name]
    assert qualified, "expected at least one out-of-class definition"
    names = {c.name: c for c in qualified}
    assert "Widget::Widget" in names
    assert "Widget::~Widget" in names
    assert "Widget::width" in names
    # Constructor is detected via `Foo::Foo` shape
    assert names["Widget::Widget"].kind == KIND_CTOR
    # Destructor by leading `~`
    assert names["Widget::~Widget"].kind == KIND_DTOR
    # Plain method otherwise
    assert names["Widget::width"].kind == KIND_FUNCTION


# --- Free functions -------------------------------------------------------


def test_free_function_inside_namespace_is_function_kind(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "widget.h")
    ns = _find(result.declarations, kind=KIND_NAMESPACE, name="ui::widgets")
    free = [c for c in ns.children if c.kind == KIND_FUNCTION]
    names = {c.name for c in free}
    assert "freeFunc" in names
    assert "add" in names


# --- Error handling -------------------------------------------------------


def test_broken_syntax_reports_parse_errors(cpp_dir):
    """Malformed C++ should yield a non-zero error_count without
    crashing the adapter — partial declarations may still surface."""
    result = CppAdapter().parse(cpp_dir / "broken_syntax.cpp")
    assert result.error_count > 0
    # Adapter should still return SOME declarations rather than nothing
    assert result.declarations, "adapter should still emit partial IR"


# --- Templates: variadic / specialisation / template templates ----------


def test_variadic_template_class(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "templates_advanced.h")
    tup = _find(result.declarations, kind=KIND_CLASS, name="Tuple")
    assert tup is not None
    assert "..." in tup.signature
    assert "Args" in tup.signature


def test_full_template_specialisation_keeps_specialised_name(cpp_dir):
    """`template<> class TypeTraits<int>` — the specialised type
    parameter is part of the name so it can be told apart from the
    primary template."""
    result = CppAdapter().parse(cpp_dir / "templates_advanced.h")
    specs = [
        d for d in _find_all(result.declarations, kind=KIND_CLASS)
        if d.name.startswith("TypeTraits")
    ]
    names = {d.name for d in specs}
    # Primary, full specialisation, partial specialisation
    assert "TypeTraits" in names
    assert "TypeTraits<int>" in names
    assert "TypeTraits<T*>" in names


def test_template_template_parameter_class(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "templates_advanced.h")
    wrapper = _find(result.declarations, kind=KIND_CLASS, name="Wrapper")
    assert wrapper is not None
    # Outer template header preserves the inner `template<typename> class Container` form
    assert "template<typename>" in wrapper.signature.replace(" ", "")


def test_member_template_inside_non_templated_class(cpp_dir):
    """Templated method on a plain class — `template<typename T> void
    visit(...)` — must surface as a method whose signature carries
    its own template header."""
    result = CppAdapter().parse(cpp_dir / "templates_advanced.h")
    visitor = _find(result.declarations, kind=KIND_CLASS, name="Visitor")
    visit = _find(visitor.children, kind=KIND_METHOD, name="visit")
    assert visit is not None
    assert visit.signature.startswith("template<typename T>")


def test_default_template_arguments_preserved_in_signature(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "templates_advanced.h")
    buf = _find(result.declarations, kind=KIND_CLASS, name="Buffer")
    assert buf is not None
    assert "T = int" in buf.signature
    assert "= 16" in buf.signature


# --- C++20 features: concepts, spaceship --------------------------------


def test_concept_definition_surfaces(cpp_dir):
    """C++20 `concept Foo = …;` lands as a field-kind declaration with
    `native_kind="concept"` so it shows up in the outline without
    needing a new top-level kind."""
    result = CppAdapter().parse(cpp_dir / "cpp20_features.h")
    numeric = _find(result.declarations, name="Numeric")
    assert numeric is not None
    assert numeric.kind == KIND_FIELD
    assert numeric.native_kind == "concept"
    assert "concept Numeric" in numeric.signature


def test_concept_with_requires_body_is_captured(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "cpp20_features.h")
    sortable = _find(result.declarations, name="Sortable")
    assert sortable is not None
    assert sortable.native_kind == "concept"
    assert "requires" in sortable.signature


def test_spaceship_operator_classifies_as_operator(cpp_dir):
    """`operator<=>` is a regular operator overload despite the
    multi-character token — it should classify as KIND_OPERATOR."""
    result = CppAdapter().parse(cpp_dir / "cpp20_features.h")
    version = _find(result.declarations, name="Version")
    spaceship = next(
        (c for c in version.children
         if c.kind == KIND_OPERATOR and "<=>" in c.signature),
        None,
    )
    assert spaceship is not None


def test_consteval_function_is_function_kind(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "cpp20_features.h")
    sq = _find(result.declarations, kind=KIND_FUNCTION, name="square")
    assert sq is not None
    assert "consteval" in sq.signature


# --- Operators: full coverage --------------------------------------------


def test_subscript_call_arrow_operators(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "operators_full.h")
    vec = _find(result.declarations, name="Vec")
    op_names = {c.name for c in vec.children if c.kind == KIND_OPERATOR}
    # Subscript and call operators carry their full token form
    assert "operator[]" in op_names
    assert "operator()" in op_names


def test_increment_operators_distinguished_by_signature(cpp_dir):
    """Pre- and post-increment share the name `operator++` but differ
    by signature (post-increment takes a dummy `int`). Both should
    surface as separate operator entries."""
    result = CppAdapter().parse(cpp_dir / "operators_full.h")
    vec = _find(result.declarations, name="Vec")
    incs = [c for c in vec.children if c.kind == KIND_OPERATOR and c.name == "operator++"]
    assert len(incs) == 2
    sigs = {c.signature for c in incs}
    # Post-increment signature carries the dummy `int` parameter
    assert any("(int)" in s for s in sigs)


def test_allocation_operators_classify_as_operator(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "operators_full.h")
    vec = _find(result.declarations, name="Vec")
    op_names = {c.name for c in vec.children if c.kind == KIND_OPERATOR}
    # All four allocation operators
    assert "operator new" in op_names
    assert "operator delete" in op_names
    # `operator new[]` and `operator delete[]` use the array form
    assert any("new[]" in n for n in op_names)
    assert any("delete[]" in n for n in op_names)


def test_user_defined_literal_at_namespace_scope(cpp_dir):
    """`constexpr long double operator""_kg(...)` — operator overloads
    classify as KIND_OPERATOR regardless of where they're defined
    (free function or member). The literal suffix is part of the name."""
    result = CppAdapter().parse(cpp_dir / "operators_full.h")
    lit = _find(result.declarations, name='operator""_kg')
    assert lit is not None
    assert lit.kind == KIND_OPERATOR


def test_free_stream_operator_outside_class(cpp_dir):
    """Free `operator<<` defined outside a class — classifies as
    KIND_OPERATOR (operator-ness wins over function-vs-method
    distinction since operators are syntactically distinguishable)."""
    result = CppAdapter().parse(cpp_dir / "operators_full.h")
    op = _find(result.declarations, name="operator<<")
    assert op is not None
    assert op.kind == KIND_OPERATOR


# --- Edge cases ----------------------------------------------------------


def test_forward_declarations_surface_as_empty_types(cpp_dir):
    """`class Foo;` is a forward declaration — surfaces with no body
    children, but should still appear so the agent can see what's
    declared in the file."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    fwd_class = _find(result.declarations, kind=KIND_CLASS, name="ForwardClass")
    fwd_struct = _find(result.declarations, kind=KIND_STRUCT, name="ForwardStruct")
    fwd_enum = _find(result.declarations, kind=KIND_ENUM, name="ForwardEnum")
    assert fwd_class is not None
    assert fwd_struct is not None
    assert fwd_enum is not None
    assert fwd_class.children == []


def test_friend_declarations_are_dropped(cpp_dir):
    """`friend class Helper;` and `friend void global_friend_fn(...)`
    are not members of the declaring class — they must not appear as
    children of `Container`."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    container = _find(result.declarations, name="Container")
    child_names = {c.name for c in container.children}
    assert "Helper" not in child_names
    assert "global_friend_fn" not in child_names


def test_nested_types_inside_class_are_unwrapped(cpp_dir):
    """`class Container { class Iterator { … }; struct Pair { … }; }`
    — the inner types should appear as nested type children of
    Container, not as opaque field entries."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    container = _find(result.declarations, name="Container")
    nested = {c.name: c for c in container.children if c.kind in (KIND_CLASS, KIND_STRUCT)}
    assert "Iterator" in nested
    assert "Pair" in nested
    # Iterator's own methods are visible too
    iterator = nested["Iterator"]
    method_names = {c.name for c in iterator.children if c.kind == KIND_METHOD}
    assert "hasNext" in method_names
    assert "next" in method_names


def test_bitfield_members_are_fields(cpp_dir):
    """`unsigned int flag_a : 1;` — the bitfield form still classifies
    as a field; the bit count is part of the signature."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    container = _find(result.declarations, name="Container")
    flag_a = _find(container.children, kind=KIND_FIELD, name="flag_a")
    assert flag_a is not None
    assert ": 1" in flag_a.signature


def test_default_member_initialisers_kept_in_signature(cpp_dir):
    """Both `int counter = 0;` and `int width{640};` keep their
    initialiser visible in the signature."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    container = _find(result.declarations, name="Container")
    by_name = {c.name: c for c in container.children}
    assert "= 0" in by_name["counter"].signature
    assert "{640}" in by_name["width"].signature.replace(" ", "")


def test_trailing_return_type_kept(cpp_dir):
    """`auto compute(int input) const -> double` — the trailing
    `-> double` return-type clause survives in the signature."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    container = _find(result.declarations, name="Container")
    compute = _find(container.children, kind=KIND_METHOD, name="compute")
    assert compute is not None
    assert "->" in compute.signature
    assert "double" in compute.signature


def test_extern_c_block_is_transparent(cpp_dir):
    """`extern "C" { void c_function(...); … }` — the C-linkage block
    has no semantic container, so its declarations should appear at
    the enclosing scope level (namespace `edges`), not nested under a
    synthetic `extern` decl."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    edges_ns = _find(result.declarations, kind=KIND_NAMESPACE, name="edges")
    free_names = {c.name for c in edges_ns.children if c.kind == KIND_FUNCTION}
    assert "c_function" in free_names
    assert "c_other_function" in free_names


def test_lambda_inside_function_does_not_surface(cpp_dir):
    """Lambdas live inside function bodies — they should not appear
    as siblings of the enclosing function."""
    result = CppAdapter().parse(cpp_dir / "edge_cases.h")
    # `make_adder` itself surfaces as a top-level function
    adder = _find(result.declarations, name="make_adder")
    assert adder is not None
    # No declaration named like a lambda (anonymous closure) should
    # appear at any level in the IR
    all_names = {d.name for d in _find_all(result.declarations)}
    # Free functions only contain `make_adder` + the c-linkage pair
    edges_ns = _find(result.declarations, kind=KIND_NAMESPACE, name="edges")
    free = [c for c in edges_ns.children if c.kind == KIND_FUNCTION]
    assert len(free) == 3, f"unexpected functions: {[c.name for c in free]}"


# --- Inheritance ---------------------------------------------------------


def test_virtual_inheritance_marker_kept_in_bases(cpp_dir):
    """`class Mammal : public virtual Animal` — both `public` access
    and `virtual` keyword survive on the recorded base."""
    result = CppAdapter().parse(cpp_dir / "inheritance_complex.h")
    mammal = _find(result.declarations, name="Mammal")
    assert mammal is not None
    assert any("virtual" in b and "Animal" in b for b in mammal.bases)


def test_diamond_with_four_bases_records_all(cpp_dir):
    """`Duck : public Mammal, public Bird, public Swimmer, public Flier`
    — every base in the list ends up in `bases`."""
    result = CppAdapter().parse(cpp_dir / "inheritance_complex.h")
    duck = _find(result.declarations, name="Duck")
    base_names = " ".join(duck.bases)
    for parent in ("Mammal", "Bird", "Swimmer", "Flier"):
        assert parent in base_names


def test_final_class_keyword_in_signature(cpp_dir):
    """`class FinalAnimal final : public Animal` — the `final`
    keyword on the type itself survives in the signature."""
    result = CppAdapter().parse(cpp_dir / "inheritance_complex.h")
    fa = _find(result.declarations, name="FinalAnimal")
    assert fa is not None
    assert "final" in fa.signature


def test_override_marker_in_method_signature(cpp_dir):
    """`void speak() const override` — the `override` keyword stays
    in the method signature so the digest can pick it up as a marker."""
    result = CppAdapter().parse(cpp_dir / "inheritance_complex.h")
    duck = _find(result.declarations, name="Duck")
    speak = _find(duck.children, kind=KIND_METHOD, name="speak")
    assert speak is not None
    assert "override" in speak.signature


def test_private_inheritance_visibility_kept(cpp_dir):
    """`class Wrapper : private Animal` — the access keyword on the
    base reflects the source-level visibility."""
    result = CppAdapter().parse(cpp_dir / "inheritance_complex.h")
    wrapper = _find(result.declarations, name="Wrapper")
    assert any("private" in b and "Animal" in b for b in wrapper.bases)


# --- Unreal Engine reflection macros -------------------------------------


def test_uclass_macro_attaches_to_next_type(cpp_dir):
    """`UCLASS(Blueprintable, BlueprintType)` directly above a class
    declaration becomes an attr on that class."""
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    actor = _find(result.declarations, kind=KIND_CLASS, name="AMyActor")
    assert actor is not None
    assert any("UCLASS" in a for a in actor.attrs)
    assert any("Blueprintable" in a for a in actor.attrs)


def test_ustruct_macro_attaches_to_next_struct(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    item = _find(result.declarations, kind=KIND_STRUCT, name="FItemData")
    assert item is not None
    assert any("USTRUCT" in a for a in item.attrs)


def test_uenum_macro_attaches_to_next_enum(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    slot = _find(result.declarations, kind=KIND_ENUM, name="EWeaponSlot")
    assert slot is not None
    assert any("UENUM" in a for a in slot.attrs)


def test_uinterface_macro_attaches_to_next_class(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    iface = _find(result.declarations, kind=KIND_CLASS, name="UInteractable")
    assert iface is not None
    assert any("UINTERFACE" in a for a in iface.attrs)


def test_uproperty_macro_attaches_to_next_field(cpp_dir):
    """`UPROPERTY(EditAnywhere, BlueprintReadWrite)` decorating a
    field should land in that field's `attrs`, not the next field."""
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    actor = _find(result.declarations, kind=KIND_CLASS, name="AMyActor")
    health = _find(actor.children, kind=KIND_FIELD, name="Health")
    assert health is not None
    assert any("UPROPERTY" in a for a in health.attrs)
    # Specifically the `Stats` category UPROPERTY (not the one on `Mesh`)
    assert any("Stats" in a for a in health.attrs)


def test_ufunction_macro_attaches_to_next_method(cpp_dir):
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    actor = _find(result.declarations, kind=KIND_CLASS, name="AMyActor")
    take_dmg = _find(actor.children, kind=KIND_METHOD, name="TakeDamage")
    assert take_dmg is not None
    assert any("UFUNCTION" in a for a in take_dmg.attrs)


def test_generated_body_marker_is_dropped(cpp_dir):
    """`GENERATED_BODY()` should not appear as a member or as an attr
    on the enclosing class — it's UHT boilerplate with no signal."""
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    actor = _find(result.declarations, kind=KIND_CLASS, name="AMyActor")
    member_names = {c.name for c in actor.children}
    assert "GENERATED_BODY" not in member_names
    assert not any("GENERATED_BODY" in a for a in actor.attrs)


def test_ue_file_parses_without_errors(cpp_dir):
    """The full UE actor + struct + enum + interface fixture should
    parse with zero reported errors after the GENERATED_BODY strip
    and the synthetic-MISSING-`;` compensation."""
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    assert result.error_count == 0


def test_ue_member_with_template_argument(cpp_dir):
    """`UPROPERTY() TArray<FItemData> Inventory;` — template type as
    field declaration must surface as a regular field."""
    result = CppAdapter().parse(cpp_dir / "ue_actor.h")
    actor = _find(result.declarations, kind=KIND_CLASS, name="AMyActor")
    inv = _find(actor.children, kind=KIND_FIELD, name="Inventory")
    assert inv is not None
    assert "TArray" in inv.signature


def test_template_class_show_slice_includes_trailing_semicolon(tmp_path):
    """`template<…> class Foo { … };` — the inner class_specifier ends
    at `}`, the template_declaration one byte later at `;`. The decl
    must take the OUTER end so the `show` slice is a syntactically
    complete declaration."""
    from ast_outline.core import find_symbols
    p = tmp_path / "a.cpp"
    p.write_text(
        "template<typename T>\nclass Foo { T x; };\n", encoding="utf-8"
    )
    r = CppAdapter().parse(p)
    m = find_symbols(r, "Foo")[0]
    assert m.source.endswith("};"), m.source
