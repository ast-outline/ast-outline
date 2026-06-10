"""Tests for the Scala adapter.

Covers Scala-specific ground that Java/Kotlin tests don't exercise:

- `package` → namespace (braceless, braced, and nested/multi-line forms)
- `class` / `trait` (→ KIND_INTERFACE) / `object` (singleton)
- `case class` (→ KIND_RECORD) and `case object` subclassing a sealed trait
- Primary-constructor val/var (and the case-class promotion where bare
  params become implicit public vals)
- `object` companions and the `class X` + `object X` twin-top-level idiom
- Scala 3 `enum` with primary ctor + `case Red extends Color(...)`
- Scala 3 `given` — both named (`given x: Ord[Int] with`) and anonymous
- Scala 3 `extension` — flattening of the block into individual methods
  with the receiver type prefixed to the rendered signature
- Scala 3 indentation-based bodies (`:` instead of braces)
- `type` + `opaque type` → KIND_DELEGATE
- Higher-kinded type parameters (`F[_]`) and context bounds (`T: Ordering`)
- Scaladoc `/** ... */` (same grammar node as `/* */`; distinguished by text)
- Package objects (`package object utils { ... }`)
- Annotations as DIRECT CHILDREN of the declaration (unlike Java/Kotlin
  where they live inside `modifiers`)
- Visibility default is `public` (no modifier = public)
"""
from __future__ import annotations

from ast_outline.adapters.scala import ScalaAdapter
from ast_outline.core import (
    KIND_CLASS,
    KIND_DELEGATE,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
    KIND_RECORD,
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


def test_parse_populates_result_metadata(scala_dir):
    path = scala_dir / "user_service.scala"
    result = ScalaAdapter().parse(path)
    assert result.path == path
    assert result.language == "scala"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_adapter_extension_set():
    assert ScalaAdapter().extensions == {".scala", ".sc"}


def test_scala_files_discovered_via_collect_files(scala_dir):
    from ast_outline.adapters import collect_files, get_adapter_for

    files = collect_files([scala_dir])
    scala_files = [f for f in files if f.suffix in {".scala", ".sc"}]
    # Top-level fixtures + 4 multidir files
    assert len(scala_files) >= 10
    for f in scala_files:
        assert isinstance(get_adapter_for(f), ScalaAdapter)


# --- Package / top level --------------------------------------------------


def test_package_creates_namespace(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    assert ns.name == "com.example.demo.service"
    assert ns.signature == "package com.example.demo.service"


def test_package_absorbs_sibling_declarations(scala_dir):
    """Scala's braceless `package foo` absorbs every trailing top-level
    declaration until EOF (or the next braceless package). UserService
    fixture declares both the class AND its companion object at top
    level — they should both land inside the namespace."""
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    names = {c.name for c in ns.children}
    assert {"UserService"}.issubset(names)
    # Companion object has the same name as the class — both entries
    # live side-by-side in the namespace.
    user_services = [c for c in ns.children if c.name == "UserService"]
    assert len(user_services) == 2  # the class AND the companion object


def test_braced_package_isolates_children(scala_dir):
    """Unlike braceless `package foo`, a braced `package foo { ... }`
    only captures declarations inside the braces. Siblings after the
    closing `}` stay at file-top scope."""
    r = ScalaAdapter().parse(scala_dir / "braced_package.scala")
    ns = _find(r.declarations, kind=KIND_NAMESPACE, name="alpha")
    assert ns is not None
    inside = _find(ns.children, kind=KIND_CLASS, name="Inside")
    assert inside is not None
    # `Outside` is NOT under the namespace — it's a top-level sibling.
    outside = [d for d in r.declarations if d.name == "Outside"]
    assert len(outside) == 1
    assert outside[0].kind == KIND_CLASS


def test_package_object_renders_as_class(scala_dir):
    """`package object utils { ... }` is a Scala 2 construct; we render
    it as KIND_CLASS with `package object` in the signature. Its body
    members (type alias, def, val) become children."""
    r = ScalaAdapter().parse(scala_dir / "package_object.scala")
    pkg_obj = _find(r.declarations, kind=KIND_CLASS, name="utils")
    assert pkg_obj is not None
    assert "package object" in pkg_obj.signature
    member_names = {c.name for c in pkg_obj.children}
    assert {"Handler", "helper", "PI"}.issubset(member_names)


def test_no_package_file(scala_dir):
    """A file without `package` keeps declarations at the top level."""
    r = ScalaAdapter().parse(scala_dir / "no_package.scala")
    assert _find(r.declarations, kind=KIND_NAMESPACE) is None
    assert _find(r.declarations, kind=KIND_CLASS, name="Top") is not None
    assert _find(r.declarations, kind=KIND_CLASS, name="Helper") is not None
    assert _find(r.declarations, kind=KIND_FUNCTION, name="freeFunction") is not None
    assert _find(r.declarations, kind=KIND_FIELD, name="freeVal") is not None


# --- Classes / traits / objects ------------------------------------------


def test_class_basic_structure(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert svc is not None
    assert svc.visibility == "public"
    method_names = {c.name for c in svc.children if c.kind == KIND_METHOD}
    assert {"save", "findMax", "compute", "close"}.issubset(method_names)
    field_names = {c.name for c in svc.children if c.kind == KIND_FIELD}
    # `val name` (primary ctor), `private val items` (primary ctor),
    # `MAX_USERS`, `packageDefault`, `cache`
    assert {"name", "items", "MAX_USERS", "packageDefault", "cache"}.issubset(field_names)


def test_class_bases_extends_and_with(scala_dir):
    """Scala uses `extends X with Y with Z` — all three should land in
    bases[]. The `arguments` node for superclass ctor args must NOT
    leak in as a base."""
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert "BaseService" in svc.bases
    assert "UserRepository" in svc.bases
    assert "AutoCloseable" in svc.bases


def test_class_signature_strips_annotations(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert not svc.signature.lstrip().startswith("@")
    # Annotations survive in attrs
    assert any("@deprecated" in a for a in svc.attrs)
    assert any("@SerialVersionUID" in a for a in svc.attrs)


def test_scala_default_visibility_is_public(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert svc.visibility == "public"
    pkg_default = next(c for c in svc.children if c.name == "packageDefault")
    assert pkg_default.visibility == "public"


def test_explicit_visibility_modifier(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    items = next(c for c in svc.children if c.name == "items")
    assert items.visibility == "private"
    cache = next(c for c in svc.children if c.name == "cache")
    assert cache.visibility == "protected"
    find_max = next(c for c in svc.children if c.name == "findMax")
    assert find_max.visibility == "private"


def test_trait_maps_to_interface(scala_dir):
    """Scala's `trait` closely resembles a Java interface (methods may
    have default implementations), so we map it to KIND_INTERFACE."""
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    cb = _find(r.declarations, kind=KIND_INTERFACE, name="Callback")
    assert cb is not None


def test_abstract_method_no_body(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    compute = _find(r.declarations, kind=KIND_METHOD, name="compute")
    assert compute is not None
    assert compute.start_line == compute.end_line
    # `function_declaration` — the abstract form, no `= body`
    assert "=" not in compute.signature


def test_nested_trait_inside_class(scala_dir):
    """A trait declared inside a class body should appear as a nested
    KIND_INTERFACE child — not leak to top level."""
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    cb = next(c for c in svc.children if c.name == "Callback")
    assert cb.kind == KIND_INTERFACE


def test_top_level_object(scala_dir):
    """`object Logger { ... }` — singleton rendered as KIND_CLASS with
    `object` in the signature."""
    r = ScalaAdapter().parse(scala_dir / "companion_and_objects.scala")
    logger = _find(r.declarations, kind=KIND_CLASS, name="Logger")
    assert logger is not None
    assert "object Logger" in logger.signature


def test_case_object_is_class(scala_dir):
    """`case object Rex extends Dog(...)` is a singleton — we map it to
    KIND_CLASS so `implements` queries pick it up as a subclass of Dog."""
    r = ScalaAdapter().parse(scala_dir / "hierarchy.scala")
    rex = _find(r.declarations, kind=KIND_CLASS, name="Rex")
    assert rex is not None
    assert "case object" in rex.signature
    assert "Dog" in rex.bases


def test_companion_class_and_object_coexist(scala_dir):
    """Scala companion: `class X` and `object X` sit side-by-side as
    two separate top-level declarations. Both must surface."""
    r = ScalaAdapter().parse(scala_dir / "companion_and_objects.scala")
    registries = _find_all(r.declarations, name="Registry")
    # We find BOTH the class and the object, distinguished by signature
    assert len(registries) == 2
    sigs = [d.signature for d in registries]
    assert any("class Registry" in s for s in sigs)
    assert any("object Registry" in s for s in sigs)


def test_object_with_extends_clause(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "companion_and_objects.scala")
    root = _find(r.declarations, kind=KIND_CLASS, name="RootHandler")
    assert root is not None
    assert "BaseHandler" in root.bases
    assert "Named" in root.bases


# --- Case classes / sealed hierarchies -----------------------------------


def test_case_class_is_record_kind(scala_dir):
    """Case classes are Scala's data classes — KIND_RECORD."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    point = _find(r.declarations, kind=KIND_RECORD, name="Point")
    assert point is not None


def test_case_class_params_without_val_become_fields(scala_dir):
    """In a `case class Point(x: Int, y: Int)` the params x, y have
    NEITHER `val` nor `var`, yet the compiler promotes them to public
    vals. The adapter matches that behaviour (unlike regular classes
    where such bare params are just ctor args)."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    point = _find(r.declarations, kind=KIND_RECORD, name="Point")
    fields = {c.name for c in point.children if c.kind == KIND_FIELD}
    assert {"x", "y"}.issubset(fields)


def test_regular_class_params_without_val_are_not_fields(scala_dir):
    """For a non-case class, ctor params without val/var are plain
    arguments — they MUST NOT appear as fields."""
    r = ScalaAdapter().parse(scala_dir / "hierarchy.scala")
    dog = _find(r.declarations, kind=KIND_CLASS, name="Dog")
    # Dog's ctor has `name: String` (no val/var) — the class has no fields
    assert not any(c.kind == KIND_FIELD for c in dog.children)


def test_sealed_trait_signature(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    shape = _find(r.declarations, kind=KIND_INTERFACE, name="Shape")
    assert shape is not None
    assert "sealed trait" in shape.signature


# --- Scala 3 enum ---------------------------------------------------------


def test_enum_kind_and_entries(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert status is not None
    entries = [c for c in status.children if c.kind == KIND_ENUM_MEMBER]
    names = [e.name for e in entries]
    assert names == ["Active", "Inactive", "Banned", "Unknown"]


def test_enum_primary_ctor_params_become_fields(scala_dir):
    """`enum Status(val label: String, val weight: Int)` — both ctor
    params have explicit `val` and must surface as fields."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    fields = {c.name for c in status.children if c.kind == KIND_FIELD}
    assert {"label", "weight"}.issubset(fields)


def test_enum_method_after_entries(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    method_names = {c.name for c in status.children if c.kind == KIND_METHOD}
    assert "display" in method_names


def test_enum_signature_contains_extends(scala_dir):
    """The enum extends a trait — `extends_clause` must survive into
    the rendered signature."""
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert "extends java.io.Serializable" in status.signature


# --- Scala 3 `given` -----------------------------------------------------


def test_named_given(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    ord_given = _find(r.declarations, kind=KIND_CLASS, name="intOrdering")
    assert ord_given is not None
    assert "given intOrdering" in ord_given.signature
    assert "Ordering[Int]" in ord_given.bases
    # Body has a `compare` method
    assert any(c.name == "compare" for c in ord_given.children)


def test_anonymous_given_gets_synthetic_name(scala_dir):
    """`given Ordering[String] with ...` has no name field; adapter
    synthesises one from the type so the declaration is searchable."""
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    anon = _find(r.declarations, name="given Ordering[String]")
    assert anon is not None
    assert anon.kind == KIND_CLASS


# --- Scala 3 `extension` -------------------------------------------------


def test_extension_method_flattened_with_receiver_prefix(scala_dir):
    """`extension (s: String) def reversed2` — the extension block is
    transparent; the inner def becomes a top-level function whose
    signature is prefixed with the extension receiver for context."""
    r = ScalaAdapter().parse(scala_dir / "extensions_and_toplevel.scala")
    rev = _find(r.declarations, kind=KIND_FUNCTION, name="reversed2")
    assert rev is not None
    assert rev.signature.startswith("extension (s: String)")


def test_extension_block_with_multiple_methods(scala_dir):
    """A single extension block can declare several methods; each must
    surface as an independent declaration."""
    r = ScalaAdapter().parse(scala_dir / "extensions_and_toplevel.scala")
    sum_as = _find(r.declarations, kind=KIND_FUNCTION, name="sumAs")
    length_as = _find(r.declarations, kind=KIND_FUNCTION, name="lengthAs")
    assert sum_as is not None
    assert length_as is not None
    # Both share the extension receiver text in their signature
    assert "extension" in sum_as.signature
    assert "extension" in length_as.signature


# --- Scala 3 indentation-based bodies ------------------------------------


def test_indented_class_body_parses(scala_dir):
    """Scala 3 `class Foo(...):` with an indentation-based body — must
    be parsed identically to the braced form."""
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    foo = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    assert foo is not None
    method_names = {c.name for c in foo.children if c.kind == KIND_METHOD}
    assert {"double", "triple"}.issubset(method_names)


def test_indented_trait_body_parses(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    greeter = _find(r.declarations, kind=KIND_INTERFACE, name="Greeter")
    assert greeter is not None
    greet = next(c for c in greeter.children if c.name == "greet")
    assert greet.kind == KIND_METHOD


# --- Higher-kinded + context bounds --------------------------------------


def test_higher_kinded_type_parameter_preserved(scala_dir):
    """`trait Functor[F[_]]` — the `F[_]` syntax must survive into the
    rendered signature intact."""
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    functor = _find(r.declarations, kind=KIND_INTERFACE, name="Functor")
    assert functor is not None
    assert "F[_]" in functor.signature


def test_context_bound_in_method_signature(scala_dir):
    """`def findMax[T: Ordering](...)` — context bound (shorthand for
    an implicit parameter) must survive into the signature."""
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    find_max = _find(r.declarations, kind=KIND_FUNCTION, name="findMax")
    assert find_max is not None
    assert "[T: Ordering]" in find_max.signature


def test_using_parameter_in_signature(scala_dir):
    """`def sorted[T](xs: List[T])(using ord: Ordering[T]): ...` — the
    `using` keyword and the second parameter list must survive."""
    r = ScalaAdapter().parse(scala_dir / "scala3_features.scala")
    sorted_fn = _find(r.declarations, kind=KIND_FUNCTION, name="sorted")
    assert sorted_fn is not None
    assert "using" in sorted_fn.signature


# --- Type aliases --------------------------------------------------------


def test_type_alias_is_delegate_kind(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "extensions_and_toplevel.scala")
    handler = _find(r.declarations, kind=KIND_DELEGATE, name="Handler")
    assert handler is not None
    assert "type Handler" in handler.signature
    assert "String => Unit" in handler.signature


def test_opaque_type_alias(scala_dir):
    """`opaque type UserId = String` — same KIND_DELEGATE, but
    the `opaque` keyword survives in the signature."""
    r = ScalaAdapter().parse(scala_dir / "extensions_and_toplevel.scala")
    user_id = _find(r.declarations, kind=KIND_DELEGATE, name="UserId")
    assert user_id is not None
    assert "opaque" in user_id.signature


# --- Annotations ---------------------------------------------------------


def test_annotations_on_class_captured_into_attrs(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert any("@deprecated" in a for a in svc.attrs)
    assert any("@SerialVersionUID" in a for a in svc.attrs)


def test_annotations_on_method_captured(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    save = _find(r.declarations, kind=KIND_METHOD, name="save")
    assert any("@throws" in a for a in save.attrs)


def test_annotation_with_parens_in_string_literal():
    """Round-trip the annotation stripper on an annotation whose
    argument is a string containing parens — the parser must mask the
    literal while balancing."""
    from ast_outline.adapters.scala import _strip_leading_annotations

    out = _strip_leading_annotations('@SuppressWarnings("(foo)") class X')
    assert out.startswith("class X")


# --- Scaladoc ------------------------------------------------------------


def test_scaladoc_collected_for_class(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert svc.docs, "Scaladoc above UserService must be captured"
    assert svc.docs[0].startswith("/**")


def test_scaladoc_collected_for_method(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    save = _find(r.declarations, kind=KIND_METHOD, name="save")
    assert save.docs
    assert "Persists" in save.docs[0]


def test_plain_block_comment_not_treated_as_scaladoc(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "plain_block_comment.scala")
    foo = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    assert foo is not None
    assert foo.docs == []


def test_line_comment_not_treated_as_scaladoc(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "line_comment.scala")
    foo = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    assert foo is not None
    assert foo.docs == []


# --- Line / byte ranges --------------------------------------------------


def test_line_ranges_reasonable(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "data_and_sealed.scala")
    point = _find(r.declarations, kind=KIND_RECORD, name="Point")
    assert point.start_line < point.end_line
    assert point.start_line >= 1
    assert point.end_line <= r.line_count


def test_doc_start_byte_precedes_declaration(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert svc.doc_start_byte < svc.start_byte


def test_doc_start_byte_equals_start_byte_without_doc(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "no_package.scala")
    helper = _find(r.declarations, kind=KIND_CLASS, name="Helper")
    assert helper.doc_start_byte == helper.start_byte


# --- Broken syntax -------------------------------------------------------


def test_broken_syntax_reports_error_count(scala_dir):
    r = ScalaAdapter().parse(scala_dir / "broken_syntax.scala")
    assert r.error_count > 0
    # The intact `good` method should still surface
    broken = _find(r.declarations, kind=KIND_CLASS, name="Broken")
    assert broken is not None
    good = next(c for c in broken.children if c.name == "good")
    assert good.kind == KIND_METHOD


# --- End-to-end renderer check -------------------------------------------


def test_outline_renderer_smoke(scala_dir):
    from ast_outline.core import OutlineOptions, render_outline

    r = ScalaAdapter().parse(scala_dir / "user_service.scala")
    text = render_outline(r, OutlineOptions())
    assert "UserService" in text
    assert "namespace com.example.demo.service" in text
    assert "override def save" in text


def test_digest_includes_scala_types(scala_dir):
    from ast_outline.adapters import collect_files
    from ast_outline.core import DigestOptions, render_digest

    files = collect_files([scala_dir])
    scala_files = [f for f in files if f.suffix in {".scala", ".sc"}]
    assert scala_files
    results = [ScalaAdapter().parse(f) for f in scala_files]
    text = render_digest(results, DigestOptions(), root=scala_dir)
    assert "UserService" in text
    assert "Point" in text
    # `case class Point` maps to KIND_RECORD canonically (so search
    # finds it uniformly with Java records / Kotlin data classes),
    # but digest restores the source-true `case class` keyword.
    assert "case class" in text


def test_case_class_pattern_binding_names_bound_identifier(tmp_path):
    """`val List(h, t) = myList` — the binding is the FIRST named child
    (a case_class_pattern); the trailing `identifier` is the RHS. The
    old scan walked past the unrecognised pattern and named the field
    after the RHS variable (`myList`)."""
    p = tmp_path / "a.scala"
    p.write_text(
        "object O {\n  val List(h, t) = myList\n  val (a, b) = pair\n}\n",
        encoding="utf-8",
    )
    r = ScalaAdapter().parse(p)
    obj = r.declarations[0]
    names = [c.name for c in obj.children]
    assert "myList" not in names, names
    assert "pair" not in names, names
    assert "h" in names, names
    assert "a" in names, names
