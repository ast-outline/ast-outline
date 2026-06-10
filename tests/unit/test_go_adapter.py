"""Tests for the Go adapter.

Covers Go-specific ground that other adapters don't exercise:
- `package` → namespace (single per file, absorbs trailing siblings)
- `type X struct { ... }` → KIND_STRUCT
- `type X interface { ... }` → KIND_INTERFACE
- `type X = Y` (alias) and `type X Y` (newtype) → KIND_DELEGATE
- **Method grouping under receiver type** — Go's flat AST has methods
  at file level; the adapter regroups them inside their receiver's
  Declaration so the outline matches reader expectation
- Methods whose receiver is declared in another file stay at the
  namespace level (no false grouping)
- **Embedding as `bases`** — both struct embedding (`type Dog struct
  { Animal }`) and interface embedding (`type Walker interface {
  Movable }`)
- Generics (Go 1.18+): type-parameter lists on types AND functions
- `iota`-driven const blocks (Go's enum idiom) emit one field per spec
- Visibility = case-of-first-letter rule (capital → public, else private)
- Doc comments: contiguous `//` lines + `/* */` blocks; blank-line gap
  breaks the doc attachment
- `const`/`var` declarations both inline and in parenthesised blocks
"""
from __future__ import annotations

from ast_outline.adapters.go import GoAdapter
from ast_outline.core import (
    KIND_DELEGATE,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
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


def test_parse_populates_result_metadata(go_dir):
    path = go_dir / "user_service.go"
    result = GoAdapter().parse(path)
    assert result.path == path
    assert result.language == "go"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_adapter_extension_set():
    assert GoAdapter().extensions == {".go"}


def test_go_files_discovered_via_collect_files(go_dir):
    from ast_outline.adapters import collect_files, get_adapter_for

    files = collect_files([go_dir])
    go_files = [f for f in files if f.suffix == ".go"]
    # 7 top-level fixtures + 4 multidir
    assert len(go_files) >= 10
    for f in go_files:
        assert isinstance(get_adapter_for(f), GoAdapter)


# --- Package / top level --------------------------------------------------


def test_package_creates_namespace(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    assert ns.name == "service"
    assert ns.signature == "package service"


def test_package_absorbs_top_level_declarations(go_dir):
    """Every top-level type / func / const goes inside the namespace."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    names = {c.name for c in ns.children}
    # Sample of expected top-level decls
    assert {"BaseService", "UserService", "Repository", "AdminRepository",
            "MaxUsers", "GlobalCounter", "Reader", "UserID"}.issubset(names)


# --- Structs --------------------------------------------------------------


def test_struct_kind_and_signature(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    assert base is not None
    assert base.signature.startswith("type BaseService")


def test_struct_named_fields(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    fields = {c.name for c in base.children if c.kind == KIND_FIELD}
    assert {"Name", "closed"}.issubset(fields)


def test_struct_visibility_by_case(go_dir):
    """Go convention: capital first → exported (public), lowercase → unexported."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    name = next(c for c in base.children if c.name == "Name")
    closed = next(c for c in base.children if c.name == "closed")
    assert name.visibility == "public"
    assert closed.visibility == "private"


def test_struct_top_level_visibility(go_dir):
    """Type-level visibility follows the same rule."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    assert base.visibility == "public"


# --- Method grouping under receiver --------------------------------------


def test_methods_regroup_under_local_receiver(go_dir):
    """`func (b *BaseService) Open()` lives at file level in the AST,
    but the adapter regroups it under BaseService."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    method_names = {c.name for c in base.children if c.kind == KIND_METHOD}
    assert {"Open", "close"}.issubset(method_names)


def test_method_signature_includes_receiver(go_dir):
    """Receiver lives in the rendered signature so an agent can see
    the binding without an extra lookup."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    open_m = _find(r.declarations, kind=KIND_METHOD, name="Open")
    assert open_m is not None
    assert "(b *BaseService)" in open_m.signature


def test_method_visibility_by_case(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    open_m = _find(r.declarations, kind=KIND_METHOD, name="Open")
    close_m = _find(r.declarations, kind=KIND_METHOD, name="close")
    assert open_m.visibility == "public"
    assert close_m.visibility == "private"


def test_methods_with_pointer_and_value_receivers(go_dir):
    """Both `(a *Animal)` and `(a Animal)` receivers are recognised."""
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    skater = _find(r.declarations, kind=KIND_STRUCT, name="Skater")
    move_m = next(c for c in skater.children if c.name == "Move")
    assert move_m.kind == KIND_METHOD
    assert "(s *Skater)" in move_m.signature


def test_method_with_unknown_receiver_stays_at_top_level(go_dir):
    """If the receiver type isn't declared in this file, the method
    must NOT be lost — it stays inside the namespace at top level so
    cross-file methods are still visible."""
    r = GoAdapter().parse(go_dir / "no_package_methods.go")
    foreign = _find(r.declarations, kind=KIND_METHOD, name="ForeignReceiver")
    assert foreign is not None
    # It shouldn't have been grouped under any local type
    local = _find(r.declarations, kind=KIND_STRUCT, name="LocalThing")
    foreign_under_local = next(
        (c for c in local.children if c.name == "ForeignReceiver"), None
    )
    assert foreign_under_local is None


def test_method_with_local_receiver_groups_correctly(go_dir):
    r = GoAdapter().parse(go_dir / "no_package_methods.go")
    local = _find(r.declarations, kind=KIND_STRUCT, name="LocalThing")
    assert any(c.name == "LocalMethod" for c in local.children)


# --- Embedded types as bases --------------------------------------------


def test_embedded_struct_is_base(go_dir):
    """`type Dog struct { Animal }` → bases = ["Animal"]."""
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    dog = _find(r.declarations, kind=KIND_STRUCT, name="Dog")
    assert "Animal" in dog.bases


def test_named_field_does_not_become_base(go_dir):
    """`type Dog struct { Animal; Breed string }` → Breed is a regular
    field, not a base. Make sure the adapter distinguishes."""
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    dog = _find(r.declarations, kind=KIND_STRUCT, name="Dog")
    assert "Breed" not in dog.bases
    field_names = {c.name for c in dog.children if c.kind == KIND_FIELD}
    assert "Breed" in field_names


def test_embedded_interface_is_base(go_dir):
    """`type Walker interface { Movable; Walk() }` → bases = ["Movable"]."""
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    walker = _find(r.declarations, kind=KIND_INTERFACE, name="Walker")
    assert "Movable" in walker.bases


def test_struct_with_pointer_embed_as_base(go_dir):
    """Pointer embedding `*Foo` should drill through pointer_type to the
    bare type identifier (not "Animal*" or similar)."""
    # The user_service.UserService embeds BaseService (not pointer-embedded).
    # Sanity-check the simpler value form here:
    r = GoAdapter().parse(go_dir / "user_service.go")
    user_service = _find(r.declarations, kind=KIND_STRUCT, name="UserService")
    assert "BaseService" in user_service.bases


# --- Interfaces ----------------------------------------------------------


def test_interface_kind_and_methods(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    repo = _find(r.declarations, kind=KIND_INTERFACE, name="Repository")
    assert repo is not None
    method_names = {c.name for c in repo.children if c.kind == KIND_METHOD}
    assert {"Get", "List", "Has"}.issubset(method_names)


def test_interface_method_visibility(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    repo = _find(r.declarations, kind=KIND_INTERFACE, name="Repository")
    get_m = next(c for c in repo.children if c.name == "Get")
    assert get_m.visibility == "public"


def test_interface_signature_starts_with_type(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    repo = _find(r.declarations, kind=KIND_INTERFACE, name="Repository")
    assert repo.signature.startswith("type Repository")


# --- Type aliases / defined types ---------------------------------------


def test_type_alias_is_delegate(go_dir):
    """`type Reader = io.Reader` — real alias."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    reader = _find(r.declarations, kind=KIND_DELEGATE, name="Reader")
    assert reader is not None
    assert "type Reader" in reader.signature
    assert "io.Reader" in reader.signature


def test_defined_type_is_delegate(go_dir):
    """`type UserID int64` — newtype-shaped, not a real alias but
    semantically alias-like for outline purposes."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    uid = _find(r.declarations, kind=KIND_DELEGATE, name="UserID")
    assert uid is not None
    assert "int64" in uid.signature


def test_defined_type_records_underlying_as_base(go_dir):
    """`type UserID int64` records int64 in bases — useful as a hint
    for downstream tools, even if Go itself doesn't see it as inheritance."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    uid = _find(r.declarations, kind=KIND_DELEGATE, name="UserID")
    assert "int64" in uid.bases


# --- Generics -----------------------------------------------------------


def test_generic_struct_signature(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    coord = _find(r.declarations, kind=KIND_STRUCT, name="Coordinate")
    assert coord is not None
    assert "[T any]" in coord.signature


def test_generic_function_signature(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    min_fn = _find(r.declarations, kind=KIND_FUNCTION, name="Min")
    assert min_fn is not None
    assert "[T cmp.Ordered]" in min_fn.signature


def test_multi_param_generic_function(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    map_fn = _find(r.declarations, kind=KIND_FUNCTION, name="Map")
    assert map_fn is not None
    assert "[A, B any]" in map_fn.signature


def test_generic_struct_with_multiple_constraints(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    pair = _find(r.declarations, kind=KIND_STRUCT, name="Pair")
    assert "[K comparable, V any]" in pair.signature


def test_generic_interface(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    container = _find(r.declarations, kind=KIND_INTERFACE, name="Container")
    assert container is not None
    assert "[T any]" in container.signature


# --- Functions / module-level decls -------------------------------------


def test_top_level_function_kind(go_dir):
    r = GoAdapter().parse(go_dir / "generics.go")
    min_fn = _find(r.declarations, kind=KIND_FUNCTION, name="Min")
    assert min_fn is not None


def test_unexported_function_is_private(go_dir):
    """Function with a lowercase first letter is unexported."""
    # generics.go has no unexported fn; comments_edge has none either —
    # synthesise via a small inline string.
    import tempfile
    from pathlib import Path

    src = "package x\n\nfunc bar() int { return 0 }\nfunc Foo() int { return 0 }\n"
    with tempfile.NamedTemporaryFile(suffix=".go", mode="w", delete=False) as f:
        f.write(src)
        p = Path(f.name)
    try:
        r = GoAdapter().parse(p)
        bar = _find(r.declarations, kind=KIND_FUNCTION, name="bar")
        foo = _find(r.declarations, kind=KIND_FUNCTION, name="Foo")
        assert bar.visibility == "private"
        assert foo.visibility == "public"
    finally:
        p.unlink()


# --- Const / var --------------------------------------------------------


def test_top_level_const_is_field(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    max_users = _find(r.declarations, kind=KIND_FIELD, name="MaxUsers")
    assert max_users is not None
    assert "MaxUsers" in max_users.signature
    assert "100" in max_users.signature


def test_typed_const(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    max_items = _find(r.declarations, kind=KIND_FIELD, name="MaxItems")
    assert max_items is not None
    assert "int" in max_items.signature


def test_var_block_emits_each_spec(go_dir):
    r = GoAdapter().parse(go_dir / "user_service.go")
    g = _find(r.declarations, kind=KIND_FIELD, name="GlobalCounter")
    p = _find(r.declarations, kind=KIND_FIELD, name="privateCount")
    assert g is not None
    assert p is not None
    assert g.visibility == "public"
    assert p.visibility == "private"


def test_iota_const_block_emits_each_entry(go_dir):
    r = GoAdapter().parse(go_dir / "enum_iota.go")
    for name in ("Red", "Green", "Blue", "Yellow"):
        d = _find(r.declarations, kind=KIND_FIELD, name=name)
        assert d is not None, f"{name} missing"


def test_const_block_first_spec_signature_includes_iota(go_dir):
    """`Red Color = iota` — the first const_spec carries the iota; we
    keep it verbatim in the signature so an agent recognises the
    enum-by-iota pattern."""
    r = GoAdapter().parse(go_dir / "enum_iota.go")
    red = _find(r.declarations, kind=KIND_FIELD, name="Red")
    assert "iota" in red.signature
    assert "Color" in red.signature


# --- Doc comments -------------------------------------------------------


def test_singleline_doc_collected(go_dir):
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="SingleLineDoc")
    assert fn.docs == ["// SingleLineDoc is a one-line doc comment."]


def test_multiline_doc_collected_in_order(go_dir):
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="MultiLine")
    assert fn.docs == [
        "// MultiLine doc spans",
        "// across multiple lines",
        "// and stops at the function.",
    ]


def test_blank_line_breaks_doc_attachment(go_dir):
    """A comment that has a blank line gap from the declaration is NOT
    attached as docs — Go convention."""
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    spaced = _find(r.declarations, kind=KIND_FUNCTION, name="Spaced")
    assert spaced.docs == []


def test_function_without_doc(go_dir):
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    no_doc = _find(r.declarations, kind=KIND_FUNCTION, name="NoDoc")
    assert no_doc.docs == []


def test_block_comment_can_be_doc(go_dir):
    """`/* … */` is also valid as a Go doc comment per spec."""
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="BlockDoc")
    assert fn.docs
    assert fn.docs[0].startswith("/*")


# --- Line / byte ranges -------------------------------------------------


def test_method_line_range_inside_struct(go_dir):
    """Even though methods are physically below the struct in source,
    after regrouping the method's line range stays accurate."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    open_m = next(c for c in base.children if c.name == "Open")
    # Method's start_line should reflect its actual source location,
    # not the struct's start_line.
    assert open_m.start_line > base.start_line


def test_doc_start_byte_precedes_declaration(go_dir):
    """When doc comment is present, doc_start_byte points at the first
    `//` line, BEFORE the declaration's actual start_byte."""
    r = GoAdapter().parse(go_dir / "user_service.go")
    base = _find(r.declarations, kind=KIND_STRUCT, name="BaseService")
    assert base.doc_start_byte < base.start_byte


def test_doc_start_byte_equals_start_byte_without_doc(go_dir):
    r = GoAdapter().parse(go_dir / "comments_edge.go")
    no_doc = _find(r.declarations, kind=KIND_FUNCTION, name="NoDoc")
    assert no_doc.doc_start_byte == no_doc.start_byte


# --- Broken syntax ------------------------------------------------------


def test_broken_syntax_reports_error_count(go_dir):
    r = GoAdapter().parse(go_dir / "broken_syntax.go")
    assert r.error_count > 0
    # Earlier intact declarations still emerge
    good = _find(r.declarations, kind=KIND_STRUCT, name="Good")
    assert good is not None
    method = _find(r.declarations, kind=KIND_METHOD, name="Method")
    assert method is not None


# --- End-to-end renderer check ------------------------------------------


def test_outline_renderer_smoke(go_dir):
    from ast_outline.core import OutlineOptions, render_outline

    r = GoAdapter().parse(go_dir / "user_service.go")
    text = render_outline(r, OutlineOptions())
    assert "UserService" in text
    assert "namespace service" in text
    assert "func (u *UserService) Save" in text


def test_digest_includes_go_types(go_dir):
    from ast_outline.adapters import collect_files
    from ast_outline.core import DigestOptions, render_digest

    files = collect_files([go_dir])
    go_files = [f for f in files if f.suffix == ".go"]
    assert go_files
    results = [GoAdapter().parse(f) for f in go_files]
    text = render_digest(results, DigestOptions(), root=go_dir)
    assert "UserService" in text
    assert "Animal" in text


# --- Edge cases (fixture: edge_cases.go) --------------------------------


def test_edge_fixture_parses_clean(go_dir):
    """Sanity gate — every other test in this section assumes the
    fixture parses without errors. If it ever degrades, fail fast
    here instead of in 15 unrelated places."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    assert r.error_count == 0


def test_multi_name_struct_field_picks_first_identifier(go_dir):
    """`X, Y, Z float64` — adapter emits ONE Declaration named `X`
    (consistent with how Java/Kotlin handle multi-variable fields).
    The full slice survives in the rendered signature so the type
    annotation isn't lost."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    vec3 = _find(r.declarations, kind=KIND_STRUCT, name="Vec3")
    fields = [c for c in vec3.children if c.kind == KIND_FIELD]
    assert len(fields) == 1
    assert fields[0].name == "X"
    assert "X, Y, Z float64" in fields[0].signature


def test_multi_name_var_picks_first_identifier(go_dir):
    """`var A, B, C int = ...` — same rule for module-level vars."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    a = _find(r.declarations, kind=KIND_FIELD, name="A")
    assert a is not None
    assert "A, B, C" in a.signature
    # B and C should NOT surface as separate Declarations
    b = _find(r.declarations, kind=KIND_FIELD, name="B")
    c_field = _find(r.declarations, kind=KIND_FIELD, name="C")
    assert b is None
    assert c_field is None


def test_multi_name_const_picks_first_identifier(go_dir):
    """`const D, E = 10, 20` — same rule for consts."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    d = _find(r.declarations, kind=KIND_FIELD, name="D")
    assert d is not None
    assert "D, E" in d.signature


def test_type_block_emits_separate_declarations(go_dir):
    """`type ( A struct{}; B interface{}; C int )` — each spec inside
    the parenthesised block becomes its OWN Declaration of the right
    kind, not lumped together."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    a = _find(r.declarations, kind=KIND_STRUCT, name="BlockStruct")
    b = _find(r.declarations, kind=KIND_INTERFACE, name="BlockInterface")
    c = _find(r.declarations, kind=KIND_DELEGATE, name="BlockNewtype")
    assert a is not None
    assert b is not None
    assert c is not None


def test_empty_struct_and_interface(go_dir):
    """`type Marker struct{}` / `type Any interface{}` — must classify
    correctly and not crash on the empty body."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    marker = _find(r.declarations, kind=KIND_STRUCT, name="Marker")
    any_iface = _find(r.declarations, kind=KIND_INTERFACE, name="Anything")
    assert marker is not None
    assert any_iface is not None
    # No children for empty composites
    assert marker.children == []
    assert any_iface.children == []


def test_function_type_named(go_dir):
    """`type HandlerFunc func(int) error` — named function type, lands
    as KIND_DELEGATE (alias-shaped)."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    h = _find(r.declarations, kind=KIND_DELEGATE, name="HandlerFunc")
    assert h is not None
    assert "func(int) error" in h.signature


def test_function_type_alias(go_dir):
    """`type CallbackAlias = func(string)` — explicit alias to a
    function type."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    cb = _find(r.declarations, kind=KIND_DELEGATE, name="CallbackAlias")
    assert cb is not None
    assert "= func(string)" in cb.signature


def test_generic_receiver_groups_under_generic_type(go_dir):
    """**Regression test for a real bug.** `func (s *Stack[T]) Push(...)`
    must be grouped under Stack — earlier the adapter only drilled
    pointer→identifier and missed pointer→generic→identifier, leaving
    methods orphaned at the namespace level."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    stack = _find(r.declarations, kind=KIND_STRUCT, name="Stack")
    assert stack is not None
    method_names = {c.name for c in stack.children if c.kind == KIND_METHOD}
    assert {"Push", "Pop"}.issubset(method_names)


def test_generic_receiver_signature_preserves_parameter(go_dir):
    """The receiver text in the signature includes `[T]` so an agent
    sees the receiver is type-parameterised."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    push = _find(r.declarations, kind=KIND_METHOD, name="Push")
    assert push is not None
    assert "*Stack[T]" in push.signature


def test_variadic_signature_preserved(go_dir):
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    sum_fn = _find(r.declarations, kind=KIND_FUNCTION, name="Sum")
    assert sum_fn is not None
    assert "nums ...int" in sum_fn.signature


def test_named_multi_return_signature(go_dir):
    """`(total int, err error)` named returns must survive in signature."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    sum_fn = _find(r.declarations, kind=KIND_FUNCTION, name="Sum")
    assert "(total int, err error)" in sum_fn.signature


def test_channel_field_types(go_dir):
    """`chan struct{}`, `<-chan int`, `chan<- string` — bidirectional
    and directional channels must round-trip in the field signature."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    server = _find(r.declarations, kind=KIND_STRUCT, name="Server")
    fields = {c.name: c for c in server.children if c.kind == KIND_FIELD}
    assert "chan struct{}" in fields["Done"].signature
    assert "<-chan int" in fields["Receive"].signature
    assert "chan<- string" in fields["Send"].signature


def test_map_and_function_slice_field_types(go_dir):
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    server = _find(r.declarations, kind=KIND_STRUCT, name="Server")
    fields = {c.name: c for c in server.children if c.kind == KIND_FIELD}
    assert "map[string]any" in fields["Cache"].signature
    assert "[]func(string) error" in fields["Outputs"].signature


def test_blank_identifier_field_emerges(go_dir):
    """`var _ Closer = (*MyConn)(nil)` — interface-satisfaction check
    pattern. The adapter still emits a Declaration; name is `_`."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    blanks = [
        c for c in _find(r.declarations, kind=KIND_NAMESPACE).children
        if c.kind == KIND_FIELD and c.name == "_"
    ]
    assert len(blanks) == 1
    assert "Closer" in blanks[0].signature


def test_pointer_embedded_type_is_base(go_dir):
    """`type Owner struct { *Base }` — pointer-embed must register Base
    as a base, NOT as a field. Drills through `pointer_type`."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    owner = _find(r.declarations, kind=KIND_STRUCT, name="Owner")
    assert "Base" in owner.bases
    field_names = {c.name for c in owner.children if c.kind == KIND_FIELD}
    assert "Base" not in field_names
    assert "Extra" in field_names


def test_generic_embedded_type_is_base(go_dir):
    """`type ColorBox struct { Container[int] }` — generic-embed must
    drill through `generic_type` and register Container as a base."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    cb = _find(r.declarations, kind=KIND_STRUCT, name="ColorBox")
    assert "Container" in cb.bases
    field_names = {c.name for c in cb.children if c.kind == KIND_FIELD}
    assert "Container" not in field_names


def test_init_and_main_special_functions(go_dir):
    """Go's special-named functions (`init`, `main`) parse like any
    other function and visibility falls under the case-rule:
    `init` and `main` start lowercase → "private"."""
    r = GoAdapter().parse(go_dir / "edge_cases.go")
    init_fn = _find(r.declarations, kind=KIND_FUNCTION, name="init")
    helper_fn = _find(r.declarations, kind=KIND_FUNCTION, name="helper")
    assert init_fn is not None
    assert helper_fn is not None
    assert init_fn.visibility == "private"
    assert helper_fn.visibility == "private"


def test_generic_method_value_receiver_groups(go_dir):
    """`func (c Coordinate[T]) Translate(...)` — value receiver (no
    pointer) on a generic type must also group correctly. Covered
    via the generics fixture, but the existing tests only assert
    Coordinate exists, not that Translate lands inside it."""
    r = GoAdapter().parse(go_dir / "generics.go")
    coord = _find(r.declarations, kind=KIND_STRUCT, name="Coordinate")
    method_names = {c.name for c in coord.children if c.kind == KIND_METHOD}
    assert "Translate" in method_names


# --- namespace stretch / interface embeddings ------------------------------


def test_package_range_covers_regrouped_methods(tmp_path):
    """Pass 2 regroups receiver methods under their struct, extending
    the STRUCT's range — but the namespace stretch looked only at its
    LAST child. A plain function declared between the struct and its
    methods clipped the package range short of the last method."""
    p = tmp_path / "a.go"
    p.write_text(
        "package pkg\n\n"
        "type Animal struct {\n\tName string\n}\n\n"
        "func NewAnimal(name string) *Animal {\n\treturn &Animal{Name: name}\n}\n\n"
        "func (a *Animal) Speak() string {\n\treturn a.Name\n}\n",
        encoding="utf-8",
    )
    r = GoAdapter().parse(p)
    ns = r.declarations[0]
    assert ns.end_line == 13, ns.end_line
    assert ns.end_byte == max(
        c.end_byte for c in ns.children
    ), (ns.end_byte, [c.end_byte for c in ns.children])


def test_interface_qualified_embeddings_become_bases(tmp_path):
    """`io.Reader` inside an interface body is a `qualified_type`, not
    a `type_identifier` — the old loop never matched it and dropped
    every package-qualified embedding from `bases`."""
    p = tmp_path / "a.go"
    p.write_text(
        "package pkg\n\n"
        'import "io"\n\n'
        "type ReadWriter interface {\n\tio.Reader\n\tio.Writer\n\tClose() error\n}\n",
        encoding="utf-8",
    )
    r = GoAdapter().parse(p)
    iface = _find(r.declarations, name="ReadWriter")
    assert iface.bases == ["io.Reader", "io.Writer"], iface.bases


def test_interface_union_constraint_keeps_every_type(tmp_path):
    """`int | int32 | int64` is one type_elem with several children —
    the `break` after the first match kept only `int`."""
    p = tmp_path / "a.go"
    p.write_text(
        "package pkg\n\ntype Number interface {\n\tint | int32 | int64\n}\n",
        encoding="utf-8",
    )
    r = GoAdapter().parse(p)
    iface = _find(r.declarations, name="Number")
    assert iface.bases == ["int", "int32", "int64"], iface.bases
