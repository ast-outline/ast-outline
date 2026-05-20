"""Tests for the digest output format conventions documented in the
legend line: callable `()` marker, `[N overloads]` collapse, native-kind
keyword for types, and comma-space token separator.

These are LLM-facing format guarantees — a regression here is a silent
contract change with downstream agent prompts, so each rule has its own
test that names what it's protecting and why."""
from __future__ import annotations

from ast_outline.adapters.csharp import CSharpAdapter
from ast_outline.adapters.go import GoAdapter
from ast_outline.adapters.java import JavaAdapter
from ast_outline.adapters.kotlin import KotlinAdapter
from ast_outline.adapters.markdown import MarkdownAdapter
from ast_outline.adapters.python import PythonAdapter
from ast_outline.adapters.rust import RustAdapter
from ast_outline.adapters.scala import ScalaAdapter
from ast_outline.adapters.swift import SwiftAdapter
from ast_outline.adapters.typescript import TypeScriptAdapter
from ast_outline.adapters.yaml import YamlAdapter
from ast_outline.core import (
    CALLABLE_KINDS,
    Declaration,
    DigestOptions,
    KIND_CLASS,
    KIND_FIELD,
    KIND_INTERFACE,
    KIND_METHOD,
    _collapse_overloads,
    _is_deprecated,
    _member_token,
    _method_markers,
    render_digest,
)


# --- Legend --------------------------------------------------------------


def test_digest_starts_with_legend_line(csharp_dir):
    """A self-describing legend is the first line of a code digest so
    the format is parseable cold by an LLM that hasn't loaded
    `ast-outline prompt`. Code fixtures always emit at least the
    `name()=callable` entry, so the legend is always present here."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    first = out.splitlines()[0]
    assert first.startswith("# legend:")


def test_legend_documents_each_non_obvious_token_when_present(csharp_dir):
    """Legend must mention every non-English token shape that ACTUALLY
    appears in the body, so an LLM reading cold can decode it. Plain-
    English labels (`[tiny]` / `[broken]`) deliberately stay out of the
    legend.

    `nested_and_overloads.cs` is picked because it exercises every
    optional token shape — overloaded methods, properties, fields,
    inheritance — so the legend should be fully populated."""
    r = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    legend = render_digest([r], DigestOptions()).splitlines()[0]
    assert "name()" in legend
    assert "[kind]" in legend
    assert "overloads" in legend
    assert "L<a>-<b>" in legend


def test_legend_is_a_single_line(csharp_dir):
    """Legend must fit on one line — multi-line legends get truncated by
    naive consumers and read awkwardly when piped into prompts."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    legend = out.splitlines()[0]
    # No internal newlines, fits comfortably in a wide terminal.
    assert "\n" not in legend
    assert len(legend) < 250


# --- Callable marker -----------------------------------------------------


def test_callables_render_with_paren_suffix(csharp_dir):
    """`name()` is the LLM-native marker for "this is a function" — every
    callable kind (method, function, ctor, dtor, operator) must carry it
    so the agent can distinguish callables from properties/fields without
    consulting the legend."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    assert "TakeDamage()" in out          # method
    assert "HeroController()" in out      # constructor


def test_non_callables_keep_kind_tag(csharp_dir):
    """Properties / events / fields render as `name [kind]` so the
    non-callable nature is explicit — avoids any "is this a method or a
    property" ambiguity."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    assert "CurrentHealth [property]" in out
    assert "OnHealthChanged [event]" in out


def test_callables_do_not_carry_plus_prefix(csharp_dir):
    """No `+` prefix on member tokens — earlier revisions used `+name`
    but the marker collides with diff syntax and adds no signal beyond
    `()` / `[kind]`. This test guards against re-introduction."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    body = "\n".join(out.splitlines()[1:])
    assert "+TakeDamage" not in body
    assert "+CurrentHealth" not in body
    assert "+HeroController" not in body


def test_member_token_pure_unit():
    """`_member_token` is the canonical formatter — exercised directly
    so the rule is enforced without going through an adapter."""
    method = Declaration(kind=KIND_METHOD, name="Foo", signature="void Foo()")
    field = Declaration(kind=KIND_FIELD, name="bar", signature="int bar")
    assert _member_token(method, count=1) == "Foo()"
    assert _member_token(method, count=3) == "Foo() [3 overloads]"
    assert _member_token(field, count=1) == "bar [field]"


# --- Overload collapse ---------------------------------------------------


def test_overloaded_methods_collapse_to_single_token(csharp_dir):
    """Money has two `Equals` overloads — one accepting Money, one
    decimal. They should appear as a single token annotated
    `[2 overloads]`, not two indistinguishable `Equals()` lines."""
    r = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    out = render_digest([r], DigestOptions())
    assert "Equals() [2 overloads]" in out
    # Only one Equals occurrence — the collapse must not also keep the
    # individual entries.
    assert out.count("Equals()") == 1


def test_single_method_does_not_carry_overload_tag(csharp_dir):
    """A unique callable name carries no `[N overloads]` — the
    annotation fires only when N > 1, otherwise it would be noise on
    every line."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    assert "[1 overloads]" not in out
    assert "TakeDamage() [" not in out  # no annotation for unique names


def test_collapse_overloads_preserves_first_occurrence_order():
    """`_collapse_overloads` returns groups in first-seen order so the
    digest still points to the agent's likeliest entry-point."""
    a = Declaration(kind=KIND_METHOD, name="A", signature="A()", start_line=10)
    b1 = Declaration(kind=KIND_METHOD, name="B", signature="B()", start_line=20)
    b2 = Declaration(kind=KIND_METHOD, name="B", signature="B(int)", start_line=25)
    c = Declaration(kind=KIND_METHOD, name="C", signature="C()", start_line=30)
    out = _collapse_overloads([a, b1, b2, c])
    assert [(d.name, n) for d, n in out] == [("A", 1), ("B", 2), ("C", 1)]
    # Representative for B is the FIRST occurrence (line 20), not the last.
    b_rep = next(d for d, _ in out if d.name == "B")
    assert b_rep.start_line == 20


def test_collapse_does_not_merge_non_callables():
    """Same-name fields must NOT collapse — they're a real source-level
    distinction (rare but possible) and `[N overloads]` would be
    semantically wrong for a non-callable."""
    f1 = Declaration(kind=KIND_FIELD, name="x", signature="int x")
    f2 = Declaration(kind=KIND_FIELD, name="x", signature="int x")
    out = _collapse_overloads([f1, f2])
    assert len(out) == 2
    assert all(count == 1 for _, count in out)


def test_overload_collapse_covers_every_callable_kind():
    """The collapse rule must apply uniformly to every kind in
    CALLABLE_KINDS — method, function, ctor, dtor, operator. A
    regression where one kind silently isn't collapsed would leak
    duplicates."""
    decls = [
        Declaration(kind=k, name="dup", signature="dup()")
        for k in CALLABLE_KINDS
    ] + [
        Declaration(kind=k, name="dup", signature="dup(int)")
        for k in CALLABLE_KINDS
    ]
    out = _collapse_overloads(decls)
    # Name "dup" used across all kinds, all with the same name in the
    # same scope — they all collapse into a single entry.
    assert len(out) == 1
    _, count = out[0]
    assert count == len(CALLABLE_KINDS) * 2


# --- Native keyword ------------------------------------------------------


def test_rust_trait_renders_as_trait_not_interface(rust_dir):
    """Rust `trait` maps to KIND_INTERFACE in the IR (so `implements`
    queries find sub-traits uniformly with Java/C# interfaces), but the
    digest must print `trait` so the agent doesn't see a fictional
    `interface` keyword that grep against the source would not find."""
    r = RustAdapter().parse(rust_dir / "hierarchy.rs")
    out = render_digest([r], DigestOptions())
    assert "trait Animal" in out
    # And NOT the canonical-kind keyword.
    assert "interface Animal" not in out


def test_scala_trait_renders_as_trait(scala_dir):
    """Scala traits — same compromise as Rust: KIND_INTERFACE for
    search, `trait` for digest."""
    r = ScalaAdapter().parse(scala_dir / "hierarchy.scala")
    out = render_digest([r], DigestOptions())
    # Names are package-qualified in digest (`zoo.Movable`); just
    # confirm the source-true keyword precedes them.
    assert "trait zoo.Movable" in out
    assert "interface zoo.Movable" not in out


def test_scala_object_renders_as_object(scala_dir):
    """Scala singleton `object` is KIND_CLASS in the IR but the digest
    should reflect the actual keyword."""
    r = ScalaAdapter().parse(scala_dir / "companion_and_objects.scala")
    out = render_digest([r], DigestOptions())
    # At least one `object` declaration in this fixture must surface
    # with the native keyword.
    assert "object " in out
    # Find a known object name from the fixture and check its header
    # uses `object`, not `class`.
    lines = [line for line in out.splitlines() if "object " in line]
    assert any(line.lstrip().startswith("object ") for line in lines)


def test_java_annotation_type_renders_as_interface(java_dir):
    """Java `@interface` annotation types render as plain `interface`
    in digest. Java devs don't say "@interface" in conversation —
    the annotation nature is recoverable from `@Retention`/`@Target`
    attrs in outline. Treating annotation types as ordinary interfaces
    keeps the digest legend short and matches the language's
    everyday vocabulary."""
    r = JavaAdapter().parse(java_dir / "annotation_type.java")
    out = render_digest([r], DigestOptions())
    assert "interface com.example.demo.ann.Tagged" in out
    # Specifically NOT the `@interface` literal from source.
    assert "@interface " not in out


def test_kotlin_data_class_renders_as_data_class(kotlin_dir):
    """`data class` maps to KIND_RECORD; digest must restore the source
    keyword so `data class Point` reads truthfully."""
    r = KotlinAdapter().parse(kotlin_dir / "data_and_sealed.kt")
    out = render_digest([r], DigestOptions())
    assert "data class com.example.demo.model.Point" in out
    # Should NOT use the canonical `record` keyword.
    assert "record com.example.demo.model.Point" not in out


def test_kotlin_object_renders_as_object(kotlin_dir):
    """Kotlin `object` (singleton) → KIND_CLASS in IR, `object` in
    digest."""
    r = KotlinAdapter().parse(kotlin_dir / "companion_and_objects.kt")
    out = render_digest([r], DigestOptions())
    assert "object app.Logger" in out


def test_kotlin_annotation_class_renders_as_annotation_class(kotlin_dir):
    """`annotation class Foo` maps to KIND_INTERFACE for query parity,
    but digest carries the source-true `annotation class` keyword."""
    r = KotlinAdapter().parse(kotlin_dir / "annotations_generics.kt")
    out = render_digest([r], DigestOptions())
    assert "annotation class gen.Marker" in out


def test_native_kind_falls_back_to_canonical_kind(python_dir):
    """When an adapter doesn't set `native_kind`, the canonical `kind`
    is used. Python only has `class` (no divergence), so the digest
    must use the canonical keyword unchanged."""
    r = PythonAdapter().parse(python_dir / "domain_model.py")
    out = render_digest([r], DigestOptions())
    # Type lines start with `class ` (canonical kind, no native override).
    assert "class " in out


def test_native_kind_default_is_empty_string():
    """Declaration default for `native_kind` is the empty string —
    sentinel used by the renderer to decide fallback. A regression to
    None or a non-empty default would change behavior silently."""
    d = Declaration(kind=KIND_CLASS, name="X", signature="class X")
    assert d.native_kind == ""


# --- Separator -----------------------------------------------------------


def test_digest_uses_comma_separator_between_member_tokens(csharp_dir):
    """Member tokens are joined with `, ` (comma-space) — universal
    list-separator convention, parsed cleanly by every BPE tokenizer.
    Earlier revisions used double-space, which was a weaker LLM signal."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    body = "\n".join(out.splitlines()[1:])
    # Comma-space appears in the member wrap-line (HeroController has
    # multiple members on a single line).
    assert ", " in body
    # Specifically: a wrap-line with at least two tokens must join them
    # with ", " — never with double-space.
    member_lines = [
        line for line in body.splitlines()
        if line.lstrip().startswith(
            ("CurrentHealth", "TakeDamage", "value", "priority")
        )
    ]
    assert member_lines, "fixture must produce a member wrap-line"
    for line in member_lines:
        stripped = line.lstrip()
        # No double-space between tokens — `, ` is the separator.
        assert "  " not in stripped, f"unexpected double-space in {line!r}"


# --- Visual grouping (blank-line rule) ----------------------------------


def test_blank_line_after_type_with_members(csharp_dir):
    """A type whose body emits at least one member row gets a trailing
    blank line — paragraph break that visually owns the "type + its
    members" block. Empty types (no body lines) stay tight so digest
    remains compact for declaration-heavy files."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    lines = out.splitlines()
    # Locate `class Demo.Hierarchy.Animal` (has a body member `Eat()`).
    animal_idx = next(i for i, line in enumerate(lines) if "class Demo.Hierarchy.Animal" in line)
    # Member line right after.
    assert "Eat()" in lines[animal_idx + 1]
    # Then a blank — the paragraph break.
    assert lines[animal_idx + 2] == ""


def test_no_blank_line_between_empty_types(csharp_dir):
    """Empty types (record / marker class with no body) stack tightly —
    introducing a blank between every empty declaration would defeat
    digest's compactness goal."""
    r = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    out = render_digest([r], DigestOptions())
    lines = out.splitlines()
    # `record UserDto` and `record Vec2` are both bodyless and adjacent
    # in the source.
    user_dto_idx = next(i for i, line in enumerate(lines) if "record Demo.Services.UserDto" in line)
    # Next non-blank line should be `record Vec2`, NOT a blank.
    assert "record Demo.Services.Vec2" in lines[user_dto_idx + 1]


# --- Type modifiers -----------------------------------------------------


def test_type_modifier_extracted_from_signature(csharp_dir):
    """`abstract class Foo` in source — the `abstract` modifier is in
    the signature line and should appear before the kind keyword in the
    digest header. Otherwise the agent can't tell instantiable types
    from abstract ones at a glance."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    assert "abstract class Demo.Hierarchy.Animal" in out


def test_static_class_modifier_surfaces(csharp_dir):
    """C# `static class` is a meaningful semantic — only static
    members allowed, can't be instantiated. Must show in digest."""
    r = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    out = render_digest([r], DigestOptions())
    assert "static class Demo.Services.UserExtensions" in out


def test_multiple_modifiers_stack(java_dir):
    """Java `static final` (or any combination) — both modifiers
    surface in source order before the kind keyword."""
    r = JavaAdapter().parse(java_dir / "user_service.java")
    out = render_digest([r], DigestOptions())
    # `static final class Inner` lives in the fixture.
    assert "static final class" in out


def test_visibility_keywords_excluded_from_modifiers(csharp_dir):
    """`public` / `private` / `protected` / `internal` are visibility,
    handled by the visibility filter — they must NOT appear as
    modifiers, otherwise every header line would carry redundant
    `public`."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    # No `public class` — even though the source says `public class Animal`,
    # the digest must drop visibility keywords.
    assert "public class" not in out
    assert "public abstract class" not in out


# --- Type attrs prefix --------------------------------------------------


def test_type_attrs_render_before_keyword(csharp_dir):
    """C# `[RequireComponent(...)]` attrs surface before the kind
    keyword, in the source-natural order: `[Attr] class Name`. Without
    them the agent reads framework-bound types as if they had no
    runtime contract."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    assert "[RequireComponent(typeof(Rigidbody2D))] class Demo.Combat.HeroController" in out


def test_python_decorator_renders_before_class(python_dir):
    """`@dataclass class Point` style — the decorator is part of the
    type's identity; without it `class Point` reads as a regular
    class, not a value record."""
    r = PythonAdapter().parse(python_dir / "domain_model.py")
    out = render_digest([r], DigestOptions())
    assert "@dataclass class User" in out


def test_rust_derive_attr_renders_before_struct(rust_dir):
    """Rust `#[derive(Debug, Clone)]` is the canonical "this struct
    auto-implements these traits" signal. Must surface in digest so
    the agent doesn't think it has to write an impl by hand."""
    r = RustAdapter().parse(rust_dir / "user_service.rs")
    out = render_digest([r], DigestOptions())
    assert "#[derive(Debug, Clone)] struct User" in out


def test_multiple_attrs_render_space_separated(rust_dir):
    """A struct with two attrs (`#[derive(Debug)]` + `#[repr(C)]`) shows
    both, joined by a single space — the source-natural form for
    inline attribute groups."""
    r = RustAdapter().parse(rust_dir / "comments_edge.rs")
    out = render_digest([r], DigestOptions())
    assert "#[derive(Debug)] #[repr(C)] struct InterleavedDocAttrs" in out


# --- Deprecated marker --------------------------------------------------


def test_deprecated_type_carries_tag(java_dir):
    """`@Deprecated` on a Java class surfaces as a compact
    `[deprecated]` tag after the type name. Without it, the agent will
    happily recommend retired APIs."""
    r = JavaAdapter().parse(java_dir / "user_service.java")
    out = render_digest([r], DigestOptions())
    # UserService is annotated `@Deprecated(since = "2.0", ...)`.
    assert "[deprecated]" in out
    # And specifically attaches to UserService, not some other type.
    user_service_lines = [
        line for line in out.splitlines()
        if "UserService" in line and "Inner" not in line and "Callback" not in line
    ]
    assert any("[deprecated]" in line for line in user_service_lines)


def test_kotlin_deprecated_type_carries_tag(kotlin_dir):
    """Kotlin `@Deprecated("use V2")` on a class surfaces as
    `[deprecated]` tag — same pattern as Java/Rust/C#."""
    r = KotlinAdapter().parse(kotlin_dir / "user_service.kt")
    out = render_digest([r], DigestOptions())
    user_service_lines = [
        line for line in out.splitlines()
        if "UserService" in line and "Inner" not in line and "Callback" not in line and "Companion" not in line
    ]
    assert any("[deprecated]" in line for line in user_service_lines)


def test_deprecation_attr_filtered_from_visible_attrs(java_dir):
    """When deprecation triggers the `[deprecated]` tag, the
    underlying `@Deprecated(...)` attr must NOT also be printed in
    the visible attrs prefix — same signal twice is noise. Other
    attrs (e.g. `@Service`) stay visible."""
    r = JavaAdapter().parse(java_dir / "user_service.java")
    out = render_digest([r], DigestOptions())
    # `@Service` should still appear (it's not a deprecation marker).
    assert "@Service" in out
    # `@Deprecated(...)` should NOT appear — replaced by the tag.
    assert "@Deprecated(" not in out


def test_is_deprecated_helper_recognises_each_pattern():
    """`_is_deprecated` accepts every common deprecation syntax across
    languages. Case-insensitive substring match keeps the rule simple
    and forgiving (`@deprecated`, `@Deprecated`, `[Obsolete]`,
    `[Obsolete("reason")]`, `#[deprecated]`, `#[deprecated(since=...)]`)."""
    cases = [
        "@Deprecated",
        '@Deprecated(since = "2.0")',
        "@deprecated",
        "[Obsolete]",
        '[Obsolete("Use NewMethod instead")]',
        "#[deprecated]",
        '#[deprecated(since = "1.2")]',
    ]
    for attr_text in cases:
        d = Declaration(
            kind=KIND_CLASS, name="X", signature="class X", attrs=[attr_text]
        )
        assert _is_deprecated(d), f"failed to detect deprecation in {attr_text!r}"


def test_is_deprecated_does_not_false_positive():
    """An attr that incidentally contains the word should NOT trigger
    the tag — but our cheap substring rule is permissive on purpose
    (a real attr literally named `Deprecated` IS a deprecation
    marker). We just check non-deprecation attrs stay clean."""
    d = Declaration(
        kind=KIND_CLASS, name="X", signature="class X",
        attrs=["@Component", "[Authorize]", "#[derive(Debug)]"],
    )
    assert not _is_deprecated(d)


def test_deprecated_member_carries_tag():
    """A deprecated METHOD must carry `[deprecated]` in its member
    token, separate from the overload count tag if present."""
    method = Declaration(
        kind=KIND_METHOD, name="OldFoo", signature="void OldFoo()",
        attrs=["@Deprecated"],
    )
    assert _member_token(method, count=1) == "OldFoo() [deprecated]"
    # With overloads — both tags surface independently.
    assert (
        _member_token(method, count=3) == "OldFoo() [3 overloads] [deprecated]"
    )


# --- Method markers -----------------------------------------------------


def test_csharp_abstract_method_marker(csharp_dir):
    """C# `public abstract void Eat()` — `abstract` is a source-true
    keyword in the signature, must surface as a method-marker prefix."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    assert "abstract Eat()" in out


def test_csharp_override_method_marker(csharp_dir):
    """C# `public override void Eat()` — `override` is the most useful
    member-marker for OO languages (changes polymorphism story)."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    assert "override Eat()" in out


def test_python_async_method_marker(python_dir):
    """Python `async def push()` — `async` is the only universally
    applicable callable marker, changes the call contract (must
    await)."""
    r = PythonAdapter().parse(python_dir / "async_service.py")
    out = render_digest([r], DigestOptions())
    assert "async push()" in out
    assert "async pop()" in out
    # Free async function — module-level, no parent scope.
    assert "async run_forever()" in out


def test_python_decorator_marker_source_true(python_dir):
    """Python `@staticmethod` / `@classmethod` / `@abstractmethod` are
    method markers, but they're decorators — not signature keywords.
    We render them source-true (with the leading `@`), so a Python
    reader recognises the form and grep against the source still
    works."""
    r = PythonAdapter().parse(python_dir / "async_service.py")
    out = render_digest([r], DigestOptions())
    # Source-true forms (with @, not translated to canonical keyword)
    assert "@staticmethod describe()" in out
    assert "@classmethod default()" in out
    # Namespaced decorator preserved verbatim.
    assert "@abc.abstractmethod" in out


def test_python_async_and_decorator_stack(python_dir):
    """A method with both `async` keyword and `@abstractmethod`
    decorator surfaces both markers, in source order — signature
    tokens first, then decorators."""
    r = PythonAdapter().parse(python_dir / "async_service.py")
    out = render_digest([r], DigestOptions())
    # `async @abc.abstractmethod handle()` — async (sig) then
    # decorator (attr).
    assert "async @abc.abstractmethod handle()" in out


def test_kotlin_open_method_marker(kotlin_dir):
    """Kotlin `open fun bark()` — Kotlin uses `open` as the dual of
    C#'s `virtual`. We keep the source-true keyword instead of
    translating, so `open bark()` reads native to a Kotlin user."""
    r = KotlinAdapter().parse(kotlin_dir / "hierarchy.kt")
    out = render_digest([r], DigestOptions())
    assert "open bark()" in out


def test_kotlin_override_method_marker(kotlin_dir):
    """Kotlin `override fun bark()` — same source-true keyword as in
    Kotlin source; not translated to anything else."""
    r = KotlinAdapter().parse(kotlin_dir / "hierarchy.kt")
    out = render_digest([r], DigestOptions())
    assert "override bark()" in out


def test_java_override_annotation_marker(java_dir):
    """Java `@Override` on a method — annotations carry the marker
    info in Java (no `override` keyword in the language). We render
    the annotation source-true so a Java user sees exactly what's in
    the source."""
    r = JavaAdapter().parse(java_dir / "user_service.java")
    out = render_digest([r], DigestOptions())
    assert "@Override save()" in out
    assert "@Override close()" in out


def test_java_abstract_method_marker(java_dir):
    """Java `public abstract int compute()` — `abstract` is a source
    keyword, surfaces as a marker."""
    r = JavaAdapter().parse(java_dir / "user_service.java")
    out = render_digest([r], DigestOptions())
    assert "abstract compute()" in out


def test_typescript_async_function_marker(fixtures_dir):
    """TypeScript `export async function generateMetadata()` — the
    `async` keyword in the signature surfaces as a callable marker
    on a free function."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "react_page.tsx"
    )
    out = render_digest([r], DigestOptions())
    assert "async generateStaticParams()" in out
    assert "async generateMetadata()" in out


# --- Per-language method-marker coverage --------------------------------


def test_csharp_async_method_marker(tmp_path):
    """C# `public async Task Foo()` — `async` keyword in the
    signature surfaces as a marker. No async-fixture exists in the
    repo, so we use an inline tmp_path file to make the round-trip
    end-to-end (signature parsing → marker extraction → digest)."""
    src = tmp_path / "Async.cs"
    src.write_text(
        "namespace Demo;\n"
        "public class Worker {\n"
        "    public async Task DoAsync() { await Task.Yield(); }\n"
        "    public void DoSync() { }\n"
        "}\n"
    )
    r = CSharpAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "async DoAsync()" in out
    # Sync method must not get an async marker leaked from anywhere.
    assert "async DoSync" not in out


def test_csharp_virtual_method_marker(tmp_path):
    """C# `public virtual void Foo()` — `virtual` is the C#-specific
    "you may override me" keyword, parallel to Kotlin's `open`. Less
    common than `override`, but a meaningful API surface signal."""
    src = tmp_path / "Virtual.cs"
    src.write_text(
        "namespace Demo;\n"
        "public class Base {\n"
        "    public virtual void Hook() { }\n"
        "}\n"
    )
    r = CSharpAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "virtual Hook()" in out


def test_kotlin_suspend_function_marker(kotlin_dir):
    """Kotlin `suspend fun fetch()` — Kotlin's coroutine equivalent
    of `async`. Source-true, kept as-is rather than translated to
    `async`. Existing fixture has a top-level suspend function."""
    r = KotlinAdapter().parse(kotlin_dir / "extensions_and_toplevel.kt")
    out = render_digest([r], DigestOptions())
    assert "suspend fetch()" in out


def test_rust_async_fn_marker(tmp_path):
    """Rust `async fn` — the `async` keyword is universal. Inline
    fixture because no Rust async fixture exists in the repo."""
    src = tmp_path / "fetch.rs"
    src.write_text(
        "pub async fn fetch(url: &str) -> String {\n"
        "    String::new()\n"
        "}\n"
    )
    r = RustAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "async fetch()" in out


def test_rust_unsafe_fn_marker(tmp_path):
    """Rust `unsafe fn` — flags that the callee may violate memory
    safety. High-signal marker, agent should not propose calling
    such a function from safe context casually."""
    src = tmp_path / "unsafe_ops.rs"
    src.write_text(
        "pub unsafe fn deref_raw(p: *const i32) -> i32 {\n"
        "    *p\n"
        "}\n"
    )
    r = RustAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "unsafe deref_raw()" in out


def test_rust_const_fn_marker(tmp_path):
    """Rust `const fn` — compile-time evaluable. Distinct from
    `const X: T = ...` (field), which never reaches this code path."""
    src = tmp_path / "const_fn.rs"
    src.write_text(
        "pub const fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n"
    )
    r = RustAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "const add()" in out


def test_typescript_async_class_method_marker(fixtures_dir):
    """TypeScript class method with `async` — marker surfaces
    inside a class body, not just on free functions."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "storage_service.ts"
    )
    out = render_digest([r], DigestOptions())
    assert "async init()" in out
    assert "async getProject()" in out
    assert "async saveProject()" in out


def test_scala_override_method_marker(scala_dir):
    """Scala `override def compare()` — uses the same `override`
    keyword as Kotlin and C#. Method markers cross-language uniform
    where the source word coincides."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    out = render_digest([r], DigestOptions())
    assert "override compare()" in out


# --- JavaScript coverage ------------------------------------------------


def test_javascript_class_and_methods_render(fixtures_dir):
    """Plain JavaScript files (`.js`) are parsed by the TS adapter
    using the TypeScript grammar (TS is a superset of JS). Class
    declarations and methods surface in digest the same way as in
    `.ts`, with no JS-specific noise."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "plain_module.js"
    )
    out = render_digest([r], DigestOptions())
    assert "class Counter" in out
    assert "constructor()" in out
    assert "increment()" in out
    assert "reset()" in out


def test_javascript_function_declaration_renders(fixtures_dir):
    """JS `export function greet(name)` — function declaration
    surfaces as a free callable."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "plain_module.js"
    )
    out = render_digest([r], DigestOptions())
    assert "greet()" in out


def test_javascript_const_arrow_does_not_emit_const_marker(fixtures_dir):
    """JS `export const add = (a, b) => a + b` — `const` here is a
    variable-binding keyword, NOT a Rust-style `const fn` callable
    modifier. It must NOT surface as a method marker; that would
    misrepresent JS semantics ("the binding is const" vs "the
    function is compile-time evaluable")."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "plain_module.js"
    )
    out = render_digest([r], DigestOptions())
    # The arrow assignment should render as a plain callable (no
    # `const` marker leaked from the variable declaration).
    assert "add()" in out
    assert "const add" not in out


def test_rust_const_fn_marker_still_works(tmp_path):
    """Regression guard against the JS `const` fix: Rust's genuine
    `const fn` (compile-time evaluable callable) must keep its
    marker. The disambiguator is the presence of the `fn` keyword
    in the signature."""
    src = tmp_path / "rust_const.rs"
    src.write_text(
        "pub const fn add(a: i32, b: i32) -> i32 { a + b }\n"
    )
    r = RustAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "const add()" in out


# --- Native-kind coverage continued -------------------------------------


def test_scala_case_class_renders_natively(scala_dir):
    """Scala `case class` is KIND_RECORD canonically (so search /
    `implements` find them uniformly with Java/Kotlin records), but
    digest must restore `case class` — Scala devs don't say
    "record", they say "case class"."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    out = render_digest([r], DigestOptions())
    assert "case class com.example.demo.model.Point" in out
    assert "case class com.example.demo.model.Circle" in out
    # And specifically NOT the canonical `record` keyword for Scala.
    assert "record com.example.demo.model.Point" not in out


def test_scala_case_object_renders_natively(scala_dir):
    """Scala `case object` — same compromise as `case class`. The
    canonical kind is KIND_CLASS, the digest restores the `case
    object` source keyword."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    out = render_digest([r], DigestOptions())
    assert "case object com.example.demo.model.UnitShape" in out


# --- Method marker skip rules -------------------------------------------


def test_static_marker_skipped_in_static_class(csharp_dir):
    """A `static` keyword on every member of a `static class` is
    redundant — the type itself already conveys the signal. Skipping
    keeps lines short and avoids noise on every method."""
    r = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    out = render_digest([r], DigestOptions())
    # UserExtensions is `static class`; DisplayLabel is its only
    # method. The token must be plain `DisplayLabel()`, not
    # `static DisplayLabel()`.
    assert "static class Demo.Services.UserExtensions" in out
    assert "DisplayLabel()" in out
    # Specifically: no `static` marker on the member.
    member_line = next(
        line for line in out.splitlines() if "DisplayLabel" in line
    )
    assert "static DisplayLabel" not in member_line


def test_abstract_marker_skipped_in_interface(csharp_dir):
    """Every interface method is implicitly abstract. Showing
    `abstract` on each one would clutter the line for zero
    information gain — we suppress it inside interface bodies."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    # IService.Run() lives in an interface — must NOT carry abstract.
    body = "\n".join(out.splitlines())
    iservice_idx = body.find("interface Demo.Hierarchy.IService")
    assert iservice_idx >= 0
    iservice_section = body[iservice_idx : iservice_idx + 200]
    assert "Run()" in iservice_section
    assert "abstract Run" not in iservice_section


def test_abstract_marker_kept_outside_interface(csharp_dir):
    """Abstract methods in an `abstract class` (not interface) MUST
    retain the marker — there the `abstract` keyword is meaningful
    (some methods abstract, others not). The skip rule is interface-
    only."""
    r = CSharpAdapter().parse(csharp_dir / "hierarchy.cs")
    out = render_digest([r], DigestOptions())
    # Animal is `abstract class`, Eat is `abstract Eat()` — marker stays.
    assert "abstract Eat()" in out


# --- _method_markers unit ------------------------------------------------


def test_method_markers_extracted_from_signature():
    """Direct unit test of `_method_markers`: a pure-keyword signature
    yields the whitelisted modifier tokens, dropping visibility and
    return-type clutter."""
    d = Declaration(
        kind=KIND_METHOD,
        name="Foo",
        signature="public async override void Foo()",
    )
    assert _method_markers(d, parent=None) == ["async", "override"]


def test_method_markers_extracted_from_decorators():
    """Decorator-only marker — `@staticmethod` becomes a marker
    rendered verbatim, no signature keywords involved."""
    d = Declaration(
        kind=KIND_METHOD,
        name="describe",
        signature="def describe()",
        attrs=["@staticmethod"],
    )
    assert _method_markers(d, parent=None) == ["@staticmethod"]


def test_method_markers_combined_signature_and_decorator():
    """`async` keyword + `@abc.abstractmethod` decorator — both
    surface, signature first, decorator second (source order)."""
    d = Declaration(
        kind=KIND_METHOD,
        name="handle",
        signature="async def handle(self, event: object) -> None",
        attrs=["@abc.abstractmethod"],
    )
    assert _method_markers(d, parent=None) == [
        "async",
        "@abc.abstractmethod",
    ]


def test_method_markers_static_skipped_in_static_class():
    """Skip rule check via direct call: parent is `static class`,
    member's `static` marker drops out."""
    parent = Declaration(
        kind=KIND_CLASS, name="X", signature="public static class X"
    )
    member = Declaration(
        kind=KIND_METHOD, name="Build", signature="public static void Build()"
    )
    assert _method_markers(member, parent=parent) == []


def test_method_markers_abstract_skipped_in_interface():
    """Skip rule check via direct call: parent is interface, member's
    `abstract` marker (or `@abstractmethod`) drops out — every
    interface member is abstract by definition."""
    iface = Declaration(
        kind=KIND_INTERFACE, name="I", signature="public interface I"
    )
    member_kw = Declaration(
        kind=KIND_METHOD, name="m", signature="public abstract void m()"
    )
    member_dec = Declaration(
        kind=KIND_METHOD,
        name="m",
        signature="def m(self)",
        attrs=["@abstractmethod"],
    )
    assert _method_markers(member_kw, parent=iface) == []
    assert _method_markers(member_dec, parent=iface) == []


def test_method_markers_no_false_positive_from_method_name():
    """A method literally named `static` or `override` (legal in some
    languages) must NOT trigger a marker — the last token before `(`
    is the name, not a modifier, and we drop it from filtering."""
    d = Declaration(
        kind=KIND_METHOD,
        name="static",
        signature="public void static()",
    )
    # No markers — the only `static`-shaped token IS the name.
    assert _method_markers(d, parent=None) == []


# --- Per-language modifier coverage ------------------------------------


def test_typescript_abstract_class_modifier(fixtures_dir):
    """TypeScript `abstract class Animal` — same `abstract` keyword
    survives in the signature, must surface in digest."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "hierarchy.ts"
    )
    out = render_digest([r], DigestOptions())
    assert "abstract class Animal" in out


def test_typescript_decorator_renders_before_class(fixtures_dir):
    """TS class decorator (`@Controller("/users")`) — a runtime
    contract for frameworks like NestJS / Angular. Surfaces verbatim
    before the kind keyword."""
    r = TypeScriptAdapter().parse(
        fixtures_dir / "typescript" / "decorators.ts"
    )
    out = render_digest([r], DigestOptions())
    assert '@Controller("/users") class UserController' in out


def test_java_sealed_class_modifier(java_dir):
    """Java 17+ `sealed class` — must surface so the agent doesn't
    propose subclasses outside the permitted list."""
    r = JavaAdapter().parse(java_dir / "records_and_sealed.java")
    out = render_digest([r], DigestOptions())
    assert "sealed class com.example.demo.model.Shape" in out


def test_java_final_class_modifier(java_dir):
    """Java `final class` — can't be subclassed, materially affects
    how the agent recommends extension points."""
    r = JavaAdapter().parse(java_dir / "records_and_sealed.java")
    out = render_digest([r], DigestOptions())
    assert "final class com.example.demo.model.Circle" in out


def test_kotlin_open_class_modifier(kotlin_dir):
    """Kotlin classes are `final` by default — `open class` is the
    explicit "you may subclass me" signal. Lose it and the agent
    can't tell which classes are extension points."""
    r = KotlinAdapter().parse(kotlin_dir / "hierarchy.kt")
    out = render_digest([r], DigestOptions())
    assert "open class zoo.Animal" in out


def test_kotlin_sealed_class_modifier(kotlin_dir):
    """Kotlin `sealed class` — closed hierarchy, exhaustive `when`,
    same semantic as Java sealed."""
    r = KotlinAdapter().parse(kotlin_dir / "data_and_sealed.kt")
    out = render_digest([r], DigestOptions())
    assert "sealed class com.example.demo.model.Shape" in out


def test_kotlin_abstract_class_modifier(kotlin_dir):
    """Kotlin `abstract class` — can't be instantiated, abstract
    members must be implemented."""
    r = KotlinAdapter().parse(kotlin_dir / "hierarchy.kt")
    out = render_digest([r], DigestOptions())
    assert "abstract class zoo.Dog" in out


def test_scala_abstract_class_modifier(scala_dir):
    """Scala `abstract class` — same OO semantic, surfaces same way."""
    r = ScalaAdapter().parse(scala_dir / "hierarchy.scala")
    out = render_digest([r], DigestOptions())
    assert "abstract class zoo.Dog" in out


def test_scala_sealed_trait_modifier(scala_dir):
    """Scala `sealed trait` — combines two divergent signals (sealed
    modifier, native `trait` keyword in place of canonical
    `interface`). Both must survive into the digest header."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    out = render_digest([r], DigestOptions())
    assert "sealed trait com.example.demo.model.Shape" in out


# --- Per-language no-regression smoke ----------------------------------


def test_go_digest_renders_without_modifiers(go_dir):
    """Go has no `abstract` / `sealed` / `final` keywords, so the
    digest must surface only the kind keyword and name without any
    leading modifier tokens. Smoke-test that the modifier extractor
    silently returns the empty list rather than emitting noise."""
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    out = render_digest([r], DigestOptions())
    # Type lines must not start (after indent) with random bare
    # words like `func` / `type` / `var` — modifier extraction
    # whitelist only allows OO modifiers.
    body = "\n".join(out.splitlines()[1:])
    assert "struct zoo.Animal" in body
    assert "interface zoo.Movable" in body
    # Anti-regression — no spurious modifier slipped through.
    assert "func struct" not in body
    assert "type struct" not in body


def test_yaml_keys_use_bare_names_no_plus_prefix(fixtures_dir):
    """YAML key digest tokens follow the same bare-name convention as
    code-digest member tokens. Earlier revisions used `+key` markers;
    those are gone — YAML now reads as a comma-joined list of
    top-level keys, consistent with the rest of digest."""
    yaml_files = sorted(
        p for p in (fixtures_dir / "yaml").iterdir()
        if p.is_file() and p.suffix in {".yaml", ".yml"}
    )
    assert yaml_files
    r = YamlAdapter().parse(yaml_files[0])
    out = render_digest([r], DigestOptions())
    body = "\n".join(out.splitlines()[1:])
    # No bare `+key` tokens — was the old YAML form.
    import re
    assert not re.search(r"^\s*\+\w", body, flags=re.M), out
    # Comma separator survives (or single-key files).
    if "," in body:
        assert ", " in body


def test_markdown_digest_unaffected_by_format_changes(fixtures_dir):
    """Markdown digest renders a heading TOC, NOT the type/member
    tokens — so callable parens / overload tags / modifiers should
    never appear there. Smoke-test that none of the new format
    surface accidentally leaked into the markdown render path."""
    md_files = sorted(
        p for p in (fixtures_dir / "markdown").iterdir()
        if p.is_file() and p.suffix == ".md"
    )
    assert md_files
    r = MarkdownAdapter().parse(md_files[0])
    out = render_digest([r], DigestOptions())
    body = "\n".join(out.splitlines()[1:])
    # No callable-marker `()` should sneak into a heading TOC.
    assert "()" not in body
    # No overload tag.
    assert "overloads]" not in body


# --- Member-level deprecation through a real adapter -------------------


def test_member_deprecated_tag_through_java_adapter(tmp_path):
    """Integration counterpart to the `_member_token` unit test:
    proves a method-level `@Deprecated` annotation actually flows
    through the Java adapter into a `[deprecated]` tag in the
    rendered digest. Uses an inline fixture so no shared file has
    to be edited."""
    src = tmp_path / "Sample.java"
    src.write_text(
        "package demo;\n"
        "public class Sample {\n"
        "    @Deprecated public void oldApi() {}\n"
        "    public void newApi() {}\n"
        "}\n"
    )
    r = JavaAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    # The deprecated method carries the tag; the fresh one doesn't.
    assert "oldApi() [deprecated]" in out
    assert "newApi()" in out
    assert "newApi() [deprecated]" not in out


def test_member_deprecated_tag_through_python_adapter(tmp_path):
    """Same end-to-end check via the Python adapter, using a
    `@deprecated` decorator (case-insensitive substring match means
    the `_is_deprecated` detector accepts any decorator whose name
    contains "deprecated")."""
    src = tmp_path / "sample.py"
    src.write_text(
        "from typing import Any\n"
        "\n"
        "def deprecated(reason):\n"
        "    def deco(fn): return fn\n"
        "    return deco\n"
        "\n"
        "class Sample:\n"
        "    @deprecated('use new_api instead')\n"
        "    def old_api(self) -> None: pass\n"
        "\n"
        "    def new_api(self) -> None: pass\n"
    )
    r = PythonAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    assert "old_api() [deprecated]" in out
    assert "new_api() [deprecated]" not in out


# --- Version sync guard -------------------------------------------------


def test_version_string_matches_pyproject():
    """`__version__` in the package and `version` in pyproject.toml
    must agree. Drift between the two has bitten us before — pip
    install metadata reports the pyproject value, `import; print(...)`
    returns the package value, and a mismatch confuses both users
    and `pip show`."""
    import re
    from pathlib import Path
    from ast_outline import __version__

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    assert m is not None, "could not find version in pyproject.toml"
    assert __version__ == m.group(1), (
        f"__version__={__version__!r} but pyproject.toml has "
        f"version={m.group(1)!r} — keep them in sync."
    )


# --- Help drift guard ---------------------------------------------------


def test_help_digest_describes_actual_format():
    """`ast-outline help digest` documents the digest format. If the
    format changes but the help text is forgotten, agents copy-pasting
    from `--help` will hit a mismatch with real output. This guard
    fails when the help drifts from the rendered legend tokens.

    Mirrors `test_prompt_command.py`'s drift guard for AGENT_PROMPT —
    same pattern: assert the help body mentions the non-obvious format
    tokens by name."""
    from ast_outline.cli import GUIDE_DIGEST

    # Each token below is a stable contract surface — if the renderer
    # changes any of them, the help line that documents it must be
    # updated in the same commit.
    assert "name()" in GUIDE_DIGEST
    assert "[kind]" in GUIDE_DIGEST
    assert "[N overloads]" in GUIDE_DIGEST
    assert "[deprecated]" in GUIDE_DIGEST
    assert "abstract" in GUIDE_DIGEST  # modifier example
    assert "async" in GUIDE_DIGEST  # method marker example
    assert "@dataclass" in GUIDE_DIGEST or "[Serializable]" in GUIDE_DIGEST
    assert "@Override" in GUIDE_DIGEST or "@staticmethod" in GUIDE_DIGEST
    assert ", " in GUIDE_DIGEST  # comma separator
    assert "blank line" in GUIDE_DIGEST  # paragraph-break rule


def test_truncation_marker_uses_parenthesised_count(csharp_dir):
    """Truncation marker is `... (N more)` — readable English. Earlier
    revisions used `... +N more` with a leading `+`, which collided
    with the (now-removed) `+` member prefix."""
    r = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    out = render_digest([r], DigestOptions(max_members_per_type=1))
    assert "(more)" in out or "more)" in out
    import re
    assert re.search(r"\.\.\.\s+\(\d+\s+more\)", out), out


# --- Dynamic legend ------------------------------------------------------
#
# The legend is built from `_LegendFlags` populated during the render
# pass — only tokens that actually appear in the body get documented.
# These tests cover:
# (a) omission rules — pure yaml / pure markdown / mixed yaml+md drop
#     the legend entirely (line_range alone doesn't justify it);
# (b) per-flag triggers — each legend entry shows up if and only if
#     the corresponding token shape is actually emitted somewhere;
# (c) cross-language sweep — one trigger per language to catch
#     adapter-level regressions where flag-setting paths are bypassed.

def _legend_line(out: str) -> str | None:
    """Extract the legend line from a digest output, or None if absent."""
    first = out.splitlines()[0] if out.splitlines() else ""
    return first if first.startswith("# legend:") else None


# --- Omission rules ------------------------------------------------------


def test_legend_omitted_for_pure_yaml_digest(yaml_dir):
    """A YAML-only digest has no callables, no kinds, no markers, no
    inheritance. The only legend-worthy token would be `L<a>-<b>`, and
    line ranges are intrinsically obvious from the trailing form — so
    the legend is dropped entirely."""
    files = sorted(p for p in yaml_dir.iterdir() if p.suffix in {".yaml", ".yml"})
    results = [YamlAdapter().parse(p) for p in files]
    out = render_digest(results, DigestOptions())
    assert _legend_line(out) is None, (
        "YAML-only digest must not emit a legend line — none of the "
        "legend tokens apply to YAML output:\n" + out.splitlines()[0]
    )


def test_legend_omitted_for_pure_markdown_digest(md_dir):
    """Same omission rule for markdown: heading TOC entries carry only
    line-range suffixes, so the legend collapses to nothing."""
    files = sorted(p for p in md_dir.iterdir() if p.suffix == ".md")
    results = [MarkdownAdapter().parse(p) for p in files]
    out = render_digest(results, DigestOptions())
    assert _legend_line(out) is None


def test_legend_omitted_for_yaml_and_markdown_mixed(yaml_dir, md_dir):
    """A batch combining only YAML and markdown contributes no
    legend-worthy tokens between them — legend stays absent."""
    yaml_files = sorted(p for p in yaml_dir.iterdir() if p.suffix in {".yaml", ".yml"})
    md_files = sorted(p for p in md_dir.iterdir() if p.suffix == ".md")
    results = [YamlAdapter().parse(p) for p in yaml_files] + [
        MarkdownAdapter().parse(p) for p in md_files
    ]
    out = render_digest(results, DigestOptions())
    assert _legend_line(out) is None


def test_legend_emerges_when_yaml_mixed_with_python(yaml_dir, python_dir):
    """As soon as a code language joins the batch, the legend re-emerges
    — code files contribute callables, kinds, and so on. The legend
    documents the union of tokens across the batch."""
    py = PythonAdapter().parse(python_dir / "domain_model.py")
    yamls = [YamlAdapter().parse(p) for p in sorted(yaml_dir.iterdir()) if p.suffix in {".yaml", ".yml"}]
    out = render_digest([py] + yamls, DigestOptions())
    legend = _legend_line(out)
    assert legend is not None, "code language in batch must restore legend"
    # `domain_model.py` has classes with bases, methods, properties.
    assert "name()" in legend
    assert "[kind]" in legend
    assert ": Base" in legend


# --- Per-flag triggers ---------------------------------------------------


def test_legend_drops_marker_when_no_modifiers_present(tmp_path):
    """A code file with plain functions and no async/static/override
    yields `name()=callable` but no `marker name()=...` entry."""
    src = tmp_path / "plain.py"
    src.write_text(
        "def foo() -> int:\n"
        "    return 1\n"
        "\n"
        "def bar(x):\n"
        "    return x\n"
    )
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "name()" in legend
    assert "marker name()" not in legend


def test_legend_includes_marker_when_async_present(python_dir):
    """An async function (or any signature with a method marker)
    triggers the modifier entry."""
    r = PythonAdapter().parse(python_dir / "async_service.py")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "marker name()" in legend


def test_legend_drops_overloads_when_no_overloaded_callables(tmp_path):
    """Each callable name is unique → no `[N overloads]` annotation
    will fire → entry dropped."""
    src = tmp_path / "uniq.py"
    src.write_text("def a(): pass\n\ndef b(): pass\n\ndef c(): pass\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "overloads" not in legend


def test_legend_includes_overloads_when_csharp_overload_present(csharp_dir):
    """`nested_and_overloads.cs` has Money.Equals overloaded; legend
    must surface the `[N overloads]` entry to explain the body tag."""
    r = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "overloads" in legend


def test_legend_drops_deprecated_when_none_present(tmp_path):
    """No `@deprecated` / `[Obsolete]` / `@Deprecated` anywhere → no
    `[deprecated]=obsolete` entry."""
    src = tmp_path / "fresh.py"
    src.write_text("def foo(): pass\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[deprecated]" not in legend


def test_legend_includes_deprecated_when_java_annotation_present(tmp_path):
    """A single `@Deprecated` method anywhere in the batch is enough to
    re-include the `[deprecated]` entry."""
    src = tmp_path / "S.java"
    src.write_text(
        "package x;\n"
        "public class S {\n"
        "    @Deprecated public void old() {}\n"
        "    public void fresh() {}\n"
        "}\n"
    )
    r = JavaAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[deprecated]" in legend


def test_legend_drops_inheritance_when_no_bases(tmp_path):
    """A class with no `extends` / `implements` / `:` clause → no
    `: Base, …=inheritance` entry."""
    src = tmp_path / "lone.py"
    src.write_text("class Lone:\n    def foo(self): pass\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert ": Base" not in legend
    assert "inheritance" not in legend


def test_legend_includes_inheritance_when_class_has_base(python_dir):
    """`hierarchy.py` has `class Dog(Animal)` etc. — bases populate the
    inheritance entry."""
    r = PythonAdapter().parse(python_dir / "hierarchy.py")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert ": Base" in legend
    assert "inheritance" in legend


def test_legend_drops_kind_for_pure_function_module(tmp_path):
    """A module with only free functions and no fields / properties /
    classes → no `name [kind]=non-callable` entry."""
    src = tmp_path / "funcs.py"
    src.write_text("def a(): pass\n\ndef b(): pass\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[kind]" not in legend
    assert "name()" in legend  # callable still surfaces


def test_legend_includes_kind_when_property_present(csharp_dir):
    """`unity_behaviour.cs` has properties + events + fields — the
    `[kind]` entry must appear to explain `[property]` / `[event]`
    tags in the body."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[kind]" in legend


# --- All-or-nothing degenerate cases -------------------------------------


def test_legend_omitted_for_empty_batch():
    """`render_digest([])` returns `# no files\n` with no legend.
    Smoke-test: legend builder shouldn't be invoked at all."""
    out = render_digest([], DigestOptions())
    assert out == "# no files\n"


def test_legend_omitted_for_files_with_no_declarations(tmp_path):
    """A code file containing only comments / blank lines yields no
    declarations → no body tokens → no legend."""
    src = tmp_path / "blank.py"
    src.write_text("# just a comment\n# nothing else here\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is None


def test_legend_omitted_when_code_batch_only_emits_empty_types(tmp_path):
    """Edge case: a code file that contains only empty type
    declarations (no members, no bases, no deprecated attrs) renders
    type headers carrying just `keyword Name  L<a>-<b>`. The only
    legend-worthy token across the body is `L<a>-<b>` — and the
    omission rule drops the legend in that case (line_range alone
    isn't worth a one-entry legend).

    This case is the practical edge of the rule for code languages —
    most real code files have at least one member, base, or modifier,
    but a file of pure marker types (e.g. empty exception classes)
    legitimately hits this branch."""
    src = tmp_path / "markers.py"
    src.write_text(
        "class A: pass\n"
        "class B: pass\n"
        "class C: pass\n"
    )
    r = PythonAdapter().parse(src)
    out = render_digest([r], DigestOptions())
    legend = _legend_line(out)
    assert legend is None, (
        "empty-type-only code batch must hit the line_range-only "
        "omission branch; got: " + (legend or "<none>")
    )
    # And `L<a>` ranges must still appear in the body unchanged.
    assert "L" in out


def test_legend_present_when_only_callable_flag_fires(tmp_path):
    """Single free function file → only `callable` flag fires (free
    functions render without a line-range suffix in digest, only type
    headers carry one). Legend has just the one entry — no
    `line_range` because no `L<n>` token is emitted for this body."""
    src = tmp_path / "one.py"
    src.write_text("def foo() -> int:\n    return 1\n")
    r = PythonAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "name()=callable" in legend
    # Body is a single free-function token `foo()` — no `L<n>` suffix
    # appears anywhere, so the line_range entry stays out.
    assert "L<a>-<b>" not in legend


def test_legend_includes_line_range_when_class_present(python_dir):
    """The line_range entry surfaces as soon as the body has any
    `L<a>-<b>` token — type headers and YAML doc separators are the
    surfaces that emit it. Verify with a class-bearing fixture."""
    r = PythonAdapter().parse(python_dir / "hierarchy.py")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "L<a>-<b>" in legend


# --- Cross-language sweep ------------------------------------------------
#
# One assertion per language: callable token surfaces and so does the
# legend. Keeps every adapter honest about populating Declaration data
# in a way the renderer can recognise as a callable.

def test_legend_present_for_python_fixtures(python_dir):
    r = PythonAdapter().parse(python_dir / "domain_model.py")
    assert _legend_line(render_digest([r], DigestOptions())) is not None


def test_legend_present_for_csharp_fixtures(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    assert _legend_line(render_digest([r], DigestOptions())) is not None


def test_legend_present_for_java_fixtures(java_dir):
    r = JavaAdapter().parse(java_dir / "user_service.java")
    assert _legend_line(render_digest([r], DigestOptions())) is not None


def test_legend_present_for_kotlin_fixtures(kotlin_dir):
    """Kotlin should trigger marker entry via `suspend` / `override` /
    `open` keywords."""
    r = KotlinAdapter().parse(kotlin_dir / "data_and_sealed.kt")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_scala_fixtures(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "hierarchy.scala")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_go_fixtures(go_dir):
    r = GoAdapter().parse(go_dir / "hierarchy.go")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_rust_fixtures(rust_dir):
    r = RustAdapter().parse(rust_dir / "hierarchy.rs")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_typescript_fixtures(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "hierarchy.ts")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_swift_fixtures(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None


def test_legend_present_for_php_fixtures(php_dir):
    """At least one PHP fixture must produce a digest with callables
    (and therefore a legend). We pick the directory's first parseable
    file rather than naming a specific one — adapters evolve, fixtures
    move, and the per-language sweep should be robust to that."""
    from ast_outline.adapters.php import PhpAdapter
    files = sorted(p for p in php_dir.iterdir() if p.suffix == ".php")
    assert files
    found_legend = False
    for p in files:
        r = PhpAdapter().parse(p)
        if _legend_line(render_digest([r], DigestOptions())) is not None:
            found_legend = True
            break
    assert found_legend, "no PHP fixture produced a legend-bearing digest"


# --- Token-shape sanity --------------------------------------------------


def test_legend_entries_in_canonical_order(csharp_dir):
    """When multiple legend entries fire, they appear in the canonical
    order defined by `_LEGEND_ENTRIES` (callable → kind → marker →
    overloads → deprecated → line_range → inheritance). A regression
    that shuffles entries would still pass token-presence tests but
    would change the legend's visual stability."""
    r = CSharpAdapter().parse(csharp_dir / "nested_and_overloads.cs")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    body = legend[len("# legend: "):]
    # Canonical order, using each entry's full literal text. We don't
    # assert specific entries are present (that's what the per-flag
    # tests cover) — just that whichever ARE present appear in this
    # order in the legend body.
    canonical_full = [
        "name()=callable",
        "name [kind]=non-callable",
        "marker name()=method modifier (async/static/override/…)",
        "[N overloads]=N callables share name",
        "[deprecated]=obsolete",
        "L<a>-<b>=line range",
        ": Base, …=inheritance",
    ]
    pos = 0
    for entry in canonical_full:
        if entry in body:
            idx = body.index(entry)
            assert idx >= pos, (
                f"entry {entry!r} appears before previous canonical entry "
                f"in {legend!r}"
            )
            pos = idx


def test_legend_entries_use_comma_space_separator(csharp_dir):
    """Entries are joined by `, ` — same separator as the body member
    tokens, parses cleanly under any BPE tokeniser."""
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    # If multiple entries fire, comma-space must appear between them.
    body = legend[len("# legend: "):]
    if "," in body:
        assert ", " in body


# --- Body parsing invariant ----------------------------------------------


def test_legend_absence_does_not_break_directory_header(yaml_dir):
    """When the legend is omitted, the very first line must be the
    directory header (`./` or `path/`), not a blank line, not the file
    line. Regression guard: an off-by-one in `render_digest` that left
    a stray empty leading line would shift every consumer's
    splitlines indexing by one."""
    files = sorted(p for p in yaml_dir.iterdir() if p.suffix in {".yaml", ".yml"})
    results = [YamlAdapter().parse(p) for p in files]
    out = render_digest(results, DigestOptions())
    first = out.splitlines()[0]
    assert first.endswith("/"), (
        f"first line should be a directory header when legend is "
        f"omitted; got: {first!r}"
    )


def test_legend_count_matches_exact_token_set():
    """End-to-end check on the legend builder itself: build flags
    manually and confirm the exact entries emitted match expectation.
    Catches regressions where an entry text drifts (e.g. someone
    rewords `name()=callable` to `name() = callable`) without anyone
    noticing."""
    from ast_outline.core import _LegendFlags, _build_legend

    # No flags → no legend.
    assert _build_legend(_LegendFlags()) == ""

    # Only line_range → still no legend (intrinsically obvious).
    assert _build_legend(_LegendFlags(line_range=True)) == ""

    # Only callable → legend is just that one entry.
    f = _LegendFlags(callable=True)
    assert _build_legend(f) == "# legend: name()=callable"

    # callable + line_range → both, in canonical order.
    f = _LegendFlags(callable=True, line_range=True)
    assert _build_legend(f) == "# legend: name()=callable, L<a>-<b>=line range"

    # All flags → full legend with every entry literally present, in
    # canonical order. We assert each expected entry text individually
    # rather than splitting on `, ` (the inheritance entry contains an
    # internal comma, so a split-and-count assertion would be fragile —
    # any rewording that adds or removes a comma in any entry would
    # silently drift the count).
    f = _LegendFlags(
        callable=True, kind=True, marker=True, overloads=True,
        deprecated=True, line_range=True, inheritance=True,
    )
    full = _build_legend(f)
    assert full.startswith("# legend: ")
    expected_entries = [
        "name()=callable",
        "name [kind]=non-callable",
        "marker name()=method modifier (async/static/override/…)",
        "[N overloads]=N callables share name",
        "[deprecated]=obsolete",
        "L<a>-<b>=line range",
        ": Base, …=inheritance",
    ]
    pos = 0
    for entry in expected_entries:
        idx = full.find(entry, pos)
        assert idx >= 0, f"missing legend entry {entry!r} in {full!r}"
        pos = idx + len(entry)


# --- Adapter-level deprecation surface check ----------------------------


def test_legend_includes_deprecated_when_csharp_obsolete_attribute(tmp_path):
    """C# `[Obsolete(...)]` is the deprecation surface. A method
    carrying it should re-include the `[deprecated]` legend entry,
    same as Java `@Deprecated` and Python `@deprecated`."""
    src = tmp_path / "S.cs"
    src.write_text(
        "namespace X {\n"
        "    public class S {\n"
        "        [Obsolete(\"use NewApi\")]\n"
        "        public void OldApi() {}\n"
        "        public void NewApi() {}\n"
        "    }\n"
        "}\n"
    )
    r = CSharpAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[deprecated]" in legend


def test_legend_includes_deprecated_when_rust_attribute(tmp_path):
    """Rust `#[deprecated]` is its deprecation surface."""
    src = tmp_path / "lib.rs"
    src.write_text(
        "pub struct S;\n"
        "impl S {\n"
        "    #[deprecated(note = \"use new_api\")]\n"
        "    pub fn old_api(&self) {}\n"
        "    pub fn new_api(&self) {}\n"
        "}\n"
    )
    r = RustAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[deprecated]" in legend


# --- Type-level deprecation triggers legend (not just member-level) -----


def test_legend_includes_deprecated_for_type_level_annotation(tmp_path):
    """Deprecation can come from a type header too — Java's
    `@Deprecated` annotation on a class. The flag should fire whether
    deprecation surfaced on a member or a type."""
    src = tmp_path / "S.java"
    src.write_text(
        "package x;\n"
        "@Deprecated public class S {\n"
        "    public void method() {}\n"
        "}\n"
    )
    r = JavaAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None
    assert "[deprecated]" in legend


# --- Backward-compat: existing single-file-with-everything test ---------


def test_legend_full_when_csharp_fixture_carries_all_token_shapes(
    csharp_dir, tmp_path,
):
    """Synthesise a tiny C# file that exercises every legend entry
    simultaneously (callable, kind, marker, overloads, deprecated,
    inheritance, line_range) and confirm every entry surfaces in the
    legend. Acts as a comprehensive end-to-end gate that the renderer
    + flag wiring stays in lockstep."""
    src = tmp_path / "Mega.cs"
    src.write_text(
        "namespace X {\n"
        "    public abstract class Base {}\n"
        "    public class Mega : Base {\n"
        "        public int Counter { get; set; }\n"
        "        public static async Task RunAsync() {}\n"
        "        public void Foo(int x) {}\n"
        "        public void Foo(string s) {}\n"
        "        [Obsolete] public void OldApi() {}\n"
        "    }\n"
        "}\n"
    )
    r = CSharpAdapter().parse(src)
    legend = _legend_line(render_digest([r], DigestOptions()))
    assert legend is not None, "expected a fully populated legend"
    assert "name()=callable" in legend
    assert "name [kind]=non-callable" in legend     # property
    assert "marker name()" in legend                # async / static
    assert "[N overloads]" in legend                # Foo overloaded
    assert "[deprecated]" in legend                 # [Obsolete]
    assert "L<a>-<b>=line range" in legend
    assert ": Base" in legend                       # Mega : Base
