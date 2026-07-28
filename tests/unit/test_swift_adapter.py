"""Tests for the Swift adapter.

Covers Swift-specific ground:
- `class` / `struct` / `enum` / `extension` / `protocol` declarations
- `init` / `deinit` / `subscript` / `func` / `var` / `let`
- Properties: stored (FIELD) vs computed (PROPERTY)
- Protocol members are implicitly public
- Swift default visibility is `internal`
- `@attributes` collection
- `///` / `/** */` doc comments vs plain comments
- Generics with bounds, `where` constraints
- Top-level functions and variables
- `typealias` / `associatedtype` → KIND_DELEGATE
- Nested types inside classes
"""
from __future__ import annotations

from ast_outline.adapters.swift import SwiftAdapter
from ast_outline.grep import KIND_COMMENT, KIND_IMPORT, grep
from ast_outline.core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DELEGATE,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INDEXER,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_PROPERTY,
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


def test_parse_populates_result_metadata(swift_dir):
    path = swift_dir / "user_service.swift"
    result = SwiftAdapter().parse(path)
    assert result.path == path
    assert result.language == "swift"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_all_swift_fixtures_parse_without_errors(swift_dir):
    adapter = SwiftAdapter()
    for path in swift_dir.glob("*.swift"):
        result = adapter.parse(path)
        assert result.error_count == 0, f"{path.name} has parse errors"


def test_adapter_extension_set():
    assert SwiftAdapter().extensions == {".swift"}


def test_swift_files_discovered_via_collect_files(swift_dir):
    from ast_outline.adapters import collect_files, get_adapter_for

    files = collect_files([swift_dir])
    swift_files = [f for f in files if f.suffix == ".swift"]
    assert len(swift_files) >= 3
    for f in swift_files:
        assert isinstance(get_adapter_for(f), SwiftAdapter)


# --- Imports --------------------------------------------------------------


def test_imports_collected(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    assert "import Foundation" in r.imports
    assert "import Combine" in r.imports


def test_imports_empty_when_none(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    # Only has Foundation import
    assert any("Foundation" in i for i in r.imports)


# --- Types: class / struct / enum / protocol ------------------------------


def test_class_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    animal = _find(r.declarations, kind=KIND_CLASS, name="Animal")
    assert animal is not None
    assert animal.signature.startswith("open class Animal")
    assert animal.visibility == "open"


def test_struct_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    user = _find(r.declarations, kind=KIND_STRUCT, name="User")
    assert user is not None
    assert user.signature.startswith("public struct User")
    assert user.visibility == "public"


def test_enum_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert status is not None
    assert status.signature.startswith("public enum Status")
    assert status.visibility == "public"


def test_protocol_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    proto = _find(r.declarations, kind=KIND_INTERFACE, name="UserServiceProtocol")
    assert proto is not None
    assert proto.signature.startswith("public protocol UserServiceProtocol")
    assert proto.visibility == "public"


def test_extension_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    # There are two UserService entries: class and extension
    exts = _find_all(r.declarations, kind=KIND_CLASS, name="UserService")
    ext = [e for e in exts if e.native_kind == "extension"][0]
    assert ext is not None
    assert ext.signature.startswith("extension UserService")
    assert ext.native_kind == "extension"


def test_actor_declaration(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "language_examples.swift")
    actor = _find(r.declarations, kind=KIND_CLASS, name="CacheActor")
    assert actor is not None
    assert actor.native_kind == "actor"
    assert actor.signature.startswith("public actor CacheActor")
    method = _find(actor.children, kind=KIND_METHOD, name="value")
    assert method is not None
    assert "async" in method.signature


# --- Inheritance / conformance --------------------------------------------


def test_class_inheritance(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    dog = _find(r.declarations, kind=KIND_CLASS, name="Dog")
    assert dog is not None
    assert "Animal" in dog.bases


def test_protocol_conformance(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    skater = _find(r.declarations, kind=KIND_CLASS, name="Skater")
    assert skater is not None
    assert "Animal" in skater.bases
    assert "Movable" in skater.bases


def test_struct_protocol_conformance(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    user = _find(r.declarations, kind=KIND_STRUCT, name="User")
    assert user is not None
    assert "Codable" in user.bases
    assert "Identifiable" in user.bases


# --- Members --------------------------------------------------------------


def test_class_fields(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    demo = _find(r.declarations, kind=KIND_CLASS, name="PropertyDemo")
    assert demo is not None
    constant = _find(demo.children, kind=KIND_FIELD, name="constantValue")
    assert constant is not None
    # Signature cuts before the initializer for outline brevity
    assert constant.signature == "let constantValue: String"


def test_class_properties(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    demo = _find(r.declarations, kind=KIND_CLASS, name="PropertyDemo")
    computed = _find(demo.children, kind=KIND_PROPERTY, name="computed")
    assert computed is not None
    assert "computed_property" not in computed.signature


def test_enum_members(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    status = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert status is not None
    members = _find_all(status.children, kind=KIND_ENUM_MEMBER)
    assert len(members) == 3
    names = {m.name for m in members}
    assert names == {"active", "inactive", "pending"}


def test_methods(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    get_user = _find(svc.children, kind=KIND_METHOD, name="getUser")
    assert get_user is not None
    assert "byId" in get_user.signature


def test_init_is_ctor(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    ctor = _find(svc.children, kind=KIND_CTOR, name="init")
    assert ctor is not None
    assert "apiClient" in ctor.signature


def test_deinit_is_dtor(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    demo = _find(r.declarations, kind=KIND_CLASS, name="PropertyDemo")
    dtor = _find(demo.children, kind=KIND_DTOR, name="deinit")
    assert dtor is not None
    assert dtor.signature == "deinit"


def test_subscript_is_indexer(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    demo = _find(r.declarations, kind=KIND_CLASS, name="PropertyDemo")
    sub = _find(demo.children, kind=KIND_INDEXER, name="subscript")
    assert sub is not None
    assert "subscript" in sub.signature


def test_static_method_marker(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    static_m = _find(svc.children, kind=KIND_METHOD, name="shared")
    assert static_m is not None
    assert static_m.signature.startswith("static func shared()")


# --- Protocol members -----------------------------------------------------


def test_protocol_functions_implicitly_public(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    proto = _find(r.declarations, kind=KIND_INTERFACE, name="UserServiceProtocol")
    get_user = _find(proto.children, kind=KIND_METHOD, name="getUser")
    assert get_user is not None
    assert get_user.visibility == "public"


def test_protocol_properties_implicitly_public(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    proto = _find(r.declarations, kind=KIND_INTERFACE, name="UserServiceProtocol")
    prop = _find(proto.children, kind=KIND_PROPERTY, name="currentUser")
    assert prop is not None
    assert prop.visibility == "public"


# --- Visibility defaults --------------------------------------------------


def test_default_visibility_is_internal(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    animal = _find(r.declarations, kind=KIND_CLASS, name="Animal")
    # Animal is `open`, find something without explicit visibility
    dog = _find(r.declarations, kind=KIND_CLASS, name="Dog")
    assert dog is not None
    assert dog.visibility == "internal"


def test_public_visibility_parsed(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    user = _find(r.declarations, kind=KIND_STRUCT, name="User")
    assert user.visibility == "public"


def test_private_visibility_parsed(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    private_var = _find(svc.children, kind=KIND_FIELD, name="apiClient")
    assert private_var is not None
    assert private_var.visibility == "private"


# --- Attributes -----------------------------------------------------------


def test_class_attribute_collected(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    animal = _find(r.declarations, kind=KIND_CLASS, name="Animal")
    assert "@objc" in animal.attrs


def test_property_attribute_collected(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    # @Published var is a stored property (FIELD), not computed
    current = _find(svc.children, kind=KIND_FIELD, name="currentUser")
    assert current is not None
    assert "@Published" in current.attrs


def test_available_attribute_collected(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    svc = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert any("@available" in a for a in svc.attrs)


def test_compiler_style_attributes_collected(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "language_examples.swift")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="newUnprovenFunc")
    assert fn is not None
    assert "@_spi(Experimental)" in fn.attrs

    service = _find(r.declarations, kind=KIND_CLASS, name="SPIService")
    method = _find(service.children, kind=KIND_METHOD, name="spiAvailableMethod")
    assert method is not None
    assert "@_spi_available(macOS 10.10, tvOS 14.0, *)" in method.attrs
    assert "@available(iOS 8.0, *)" in method.attrs
    assert "@objc" in method.attrs


# --- Doc comments ---------------------------------------------------------


def test_doc_comments_attached(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "hierarchy.swift")
    animal = _find(r.declarations, kind=KIND_CLASS, name="Animal")
    assert animal.docs
    assert "Animal base class" in animal.docs[0]


def test_multiline_doc_comments_attached(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    marker = _find(r.declarations, kind=KIND_INTERFACE, name="Marker")
    assert marker is not None
    assert marker.docs
    assert marker.docs[0].startswith("/**")
    assert "Empty protocol" in marker.docs[0]


def test_plain_comments_not_attached(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "properties.swift")
    demo = _find(r.declarations, kind=KIND_CLASS, name="PropertyDemo")
    # The class itself has a /// doc comment above it
    assert demo.docs
    assert "Property types demo class" in demo.docs[0]
    # The // style comments inside the class body don't become docs for members
    constant = _find(demo.children, kind=KIND_FIELD, name="constantValue")
    assert not constant.docs


# --- Top-level declarations -----------------------------------------------


def test_top_level_function(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    func = _find(r.declarations, kind=KIND_FUNCTION, name="makeDefaultUser")
    assert func is not None
    assert func.signature.startswith("public func makeDefaultUser()")


def test_top_level_variable(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    var = _find(r.declarations, kind=KIND_FIELD, name="defaultTimeout")
    assert var is not None
    assert var.signature.startswith("public let defaultTimeout")


def test_typealias(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    alias = _find(r.declarations, kind=KIND_DELEGATE, name="UserCallback")
    assert alias is not None
    assert alias.signature.startswith("public typealias UserCallback")


def test_associatedtype(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    proto = _find(r.declarations, kind=KIND_INTERFACE, name="ContainerProtocol")
    assoc = _find(proto.children, kind=KIND_DELEGATE, name="Item")
    assert assoc is not None
    assert assoc.native_kind == "associatedtype"
    assert assoc.signature == "associatedtype Item"
    assert assoc.visibility == "public"


def test_associatedtype_with_where_clause(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "language_examples.swift")
    proto = _find(r.declarations, kind=KIND_INTERFACE, name="ExampleProtocol")
    assoc = _find(proto.children, kind=KIND_DELEGATE, name="Item")
    assert assoc is not None
    assert assoc.signature == "associatedtype Item where Item: Codable"


# --- Generics / where constraints -----------------------------------------


def test_generic_class(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    container = _find(r.declarations, kind=KIND_CLASS, name="Container")
    assert container is not None
    assert "<T: Codable>" in container.signature
    assert "where T: Equatable" in container.signature


def test_generic_function(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    func = _find(r.declarations, kind=KIND_FUNCTION, name="genericFunc")
    assert func is not None
    assert "<T: Comparable>" in func.signature


# --- Nested types ---------------------------------------------------------


def test_nested_class(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    outer = _find(r.declarations, kind=KIND_CLASS, name="Outer")
    assert outer is not None
    inner = _find(outer.children, kind=KIND_CLASS, name="Inner")
    assert inner is not None


def test_nested_struct(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    outer = _find(r.declarations, kind=KIND_CLASS, name="Outer")
    inner = _find(outer.children, kind=KIND_STRUCT, name="InnerStruct")
    assert inner is not None


def test_nested_enum(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    outer = _find(r.declarations, kind=KIND_CLASS, name="Outer")
    inner = _find(outer.children, kind=KIND_ENUM, name="InnerEnum")
    assert inner is not None


# --- Special initializers -------------------------------------------------


def test_convenience_init(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    req = _find(r.declarations, kind=KIND_CLASS, name="RequiredInit")
    ctors = _find_all(req.children, kind=KIND_CTOR)
    sigs = {c.signature for c in ctors}
    assert any("convenience" in s for s in sigs)


def test_required_init(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    req = _find(r.declarations, kind=KIND_CLASS, name="RequiredInit")
    ctors = _find_all(req.children, kind=KIND_CTOR)
    sigs = {c.signature for c in ctors}
    assert any("required" in s for s in sigs)


def test_failable_init(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "edge_cases.swift")
    f = _find(r.declarations, kind=KIND_CLASS, name="Failable")
    ctor = _find(f.children, kind=KIND_CTOR, name="init")
    assert ctor is not None
    assert "init?" in ctor.signature


# --- Error count ----------------------------------------------------------


def test_error_count_zero_on_clean_swift(swift_dir):
    r = SwiftAdapter().parse(swift_dir / "user_service.swift")
    assert r.error_count == 0


# --- Grep classifier ------------------------------------------------------


def test_grep_classifies_swift_import_line(swift_dir):
    """A match on an `import` line is tagged [import], not a plain ref."""
    results = grep("Foundation", [swift_dir / "user_service.swift"]).files
    kinds = [m.kind for fr in results for m in fr.matches]
    assert KIND_IMPORT in kinds


def test_grep_filters_swift_line_comments(swift_dir):
    """A match inside a `//`/`///` comment is filtered as noise by default
    and surfaces as a comment match under --include-noise."""
    src = swift_dir / "user_service.swift"
    visible = grep("Concrete", [src]).files
    assert visible[0].filtered_count == 1
    assert KIND_COMMENT not in [m.kind for m in visible[0].matches]
    with_noise = grep("Concrete", [src], include_noise=True).files
    assert KIND_COMMENT in [m.kind for fr in with_noise for m in fr.matches]
