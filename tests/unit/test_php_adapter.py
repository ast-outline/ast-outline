r"""Tests for the PHP adapter.

Covers:
- file-level: file-scoped namespaces absorbing siblings, bracketed
  `namespace Foo { ... }`, multiple namespaces in one file, no-namespace
  legacy files, the unnamed `namespace { ... }` block
- types: class / interface / trait / backed enum / enumless enum, with
  modifiers (`abstract`, `final`, `readonly`, `final readonly`,
  `abstract readonly`)
- members: methods, `__construct` → KIND_CTOR, `__destruct` → KIND_DTOR,
  abstract methods (no body), properties (single + multi-variable),
  promoted constructor properties (PHP 8.0+), class constants (incl.
  typed ones, PHP 8.3), enum cases (pure + backed)
- modifiers & attributes: visibility defaults to public, explicit
  `private`/`protected`, `#[Attr(...)]` attribute stripping from
  signatures. (Note: PHP's classic `@deprecated` lives in PHPDoc text
  which the IR keeps in `docs`. The digest's `[deprecated]` marker
  fires off `attrs`, so deprecation surfaces only via attribute
  forms — `#[\Deprecated]` (PHP 8.4+) or any user-defined attribute
  whose name contains "deprecated"/"obsolete".)
- inheritance: `extends Base`, `implements I1, I2`, interface
  `extends I1, I2`, backed enum `: string implements ...`
- PHPDoc (`/** ... */`) vs plain block comments and `//` / `#` line
  comments — only PHPDoc captured
- imports: flat, aliased, function/const, grouped (incl. mixed kinds)
- partial-parse robustness: `[broken]` files surface error_count > 0
"""
from __future__ import annotations

from ast_outline.adapters.php import PhpAdapter
from ast_outline.core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_DTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_NAMESPACE,
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


# --- Smoke ----------------------------------------------------------------


def test_parse_populates_result_metadata(php_dir):
    path = php_dir / "user_service.php"
    result = PhpAdapter().parse(path)
    assert result.path == path
    assert result.language == "php"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_adapter_extension_set():
    assert PhpAdapter().extensions == {".php", ".phtml", ".phps", ".php8"}


def test_php_files_discovered_via_collect_files(php_dir):
    from ast_outline.adapters import collect_files, get_adapter_for

    files = collect_files([php_dir])
    php_files = [f for f in files if f.suffix == ".php"]
    assert len(php_files) >= 10  # we ship 10+ PHP fixtures
    for f in php_files:
        assert isinstance(get_adapter_for(f), PhpAdapter)


# --- Namespaces -----------------------------------------------------------


def test_file_scoped_namespace_creates_namespace(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    assert ns.name == "App\\Service"
    assert ns.signature == "namespace App\\Service"


def test_file_scoped_namespace_absorbs_sibling_types(php_dir):
    """`namespace Foo;` followed by classes/functions — every following
    top-level decl must land inside the namespace's children."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    ns = _find(r.declarations, kind=KIND_NAMESPACE, name="App\\Service")
    assert ns is not None
    names = {c.name for c in ns.children}
    assert {"UserService", "BaseService", "make_service", "APP_VERSION"}.issubset(names)


def test_bracketed_namespace_contains_only_its_block(php_dir):
    """Each `namespace Foo { ... }` block must contain only its own block's
    declarations — they don't leak across blocks."""
    r = PhpAdapter().parse(php_dir / "bracketed_namespaces.php")
    first = _find(r.declarations, kind=KIND_NAMESPACE, name="App\\First")
    second = _find(r.declarations, kind=KIND_NAMESPACE, name="App\\Second")
    assert first is not None and second is not None
    assert {c.name for c in first.children} == {"FirstA", "FirstB"}
    assert {c.name for c in second.children} == {"Greeter", "helper"}


def test_unnamed_bracketed_namespace_is_present(php_dir):
    """`namespace { ... }` (the global namespace block) appears as a
    namespace declaration with empty name."""
    r = PhpAdapter().parse(php_dir / "bracketed_namespaces.php")
    nss = _find_all(r.declarations, kind=KIND_NAMESPACE)
    unnamed = [n for n in nss if n.name == ""]
    assert len(unnamed) == 1
    assert any(c.name == "GlobalScoped" for c in unnamed[0].children)


def test_no_namespace_file_decls_are_top_level(php_dir):
    """Legacy global-scope file (no `namespace` directive) must put types
    and functions at the top level, not inside any namespace."""
    r = PhpAdapter().parse(php_dir / "no_namespace.php")
    assert _find(r.declarations, kind=KIND_NAMESPACE) is None
    names = {d.name for d in r.declarations}
    assert {"GlobalThing", "global_helper", "MAX_RETRIES"}.issubset(names)


# --- Types ----------------------------------------------------------------


def test_class_with_extends_and_implements(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert cls is not None
    # `final` modifier ends up in the signature.
    assert cls.signature.startswith("final class UserService")


def test_interface_extends_multiple(php_dir):
    r = PhpAdapter().parse(php_dir / "repository.php")
    iface = _find(r.declarations, kind=KIND_INTERFACE, name="PagedRepository")
    assert iface is not None
    # `extends Repository, \Countable` — both bases captured (order-preserved).
    assert iface.bases == ["Repository", "\\Countable"]


def test_trait_uses_native_kind(php_dir):
    """PHP traits map to KIND_INTERFACE for cross-language search uniformity
    but native_kind="trait" preserves the source-true keyword for digest."""
    r = PhpAdapter().parse(php_dir / "traits_and_attributes.php")
    trait = _find(r.declarations, kind=KIND_INTERFACE, name="HasTimestamps")
    assert trait is not None
    assert trait.native_kind == "trait"
    assert trait.signature.startswith("trait HasTimestamps")


def test_backed_enum_is_kind_enum(php_dir):
    r = PhpAdapter().parse(php_dir / "status_enum.php")
    enum = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert enum is not None
    # Backed enum: signature carries the scalar type.
    assert "enum Status: string" in enum.signature
    assert enum.bases == ["HasName"]


def test_pure_enum_has_cases_no_backing(php_dir):
    r = PhpAdapter().parse(php_dir / "status_enum.php")
    enum = _find(r.declarations, kind=KIND_ENUM, name="Priority")
    assert enum is not None
    assert "Priority: " not in enum.signature
    case_names = {c.name for c in enum.children if c.kind == KIND_ENUM_MEMBER}
    assert case_names == {"Low", "High"}


def test_enum_cases_kind_and_visibility(php_dir):
    r = PhpAdapter().parse(php_dir / "status_enum.php")
    cases = _find_all(r.declarations, kind=KIND_ENUM_MEMBER)
    assert {c.name for c in cases if c.visibility == "public"} >= {
        "Active", "Pending", "Banned", "Low", "High",
    }


def test_readonly_class_modifier_in_signature(php_dir):
    r = PhpAdapter().parse(php_dir / "readonly_class.php")
    vec = _find(r.declarations, kind=KIND_CLASS, name="Vec2")
    assert vec is not None
    assert vec.signature.startswith("readonly class Vec2")


def test_abstract_readonly_class(php_dir):
    r = PhpAdapter().parse(php_dir / "readonly_class.php")
    money = _find(r.declarations, kind=KIND_CLASS, name="Money")
    assert money is not None
    sig = money.signature
    assert "abstract" in sig and "readonly" in sig and "class Money" in sig


# --- Members --------------------------------------------------------------


def test_construct_is_ctor_destruct_is_dtor(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert cls is not None
    ctors = [c for c in cls.children if c.kind == KIND_CTOR]
    assert len(ctors) == 1 and ctors[0].name == "__construct"
    # No `__destruct` here — verify in a separate file.


def test_abstract_method_signature_has_no_body(php_dir):
    """An abstract method has no `{...}` block — its signature is the full
    declaration text up to (and excluding) the trailing `;`."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    abs_m = _find(r.declarations, kind=KIND_METHOD, name="name")
    assert abs_m is not None
    assert abs_m.signature == "abstract public function name(): string"


def test_method_visibility_defaults_to_public(php_dir):
    """Class methods without a visibility modifier default to public
    (PHP spec)."""
    r = PhpAdapter().parse(php_dir / "no_namespace.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="GlobalThing")
    assert cls is not None
    hello = _find(cls.children, name="hello")
    assert hello is not None and hello.visibility == "public"


def test_explicit_protected_and_private_visibility(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert cls is not None
    flush = _find(cls.children, name="flush")
    assert flush is not None and flush.visibility == "private"
    base = _find(r.declarations, kind=KIND_CLASS, name="BaseService")
    tag = _find(base.children, name="tag")
    assert tag is not None and tag.visibility == "public"


def test_property_strips_dollar_prefix_from_name(php_dir):
    """`private string $name;` → name="name" (no `$`). Source-true text
    lives in the signature; the IR name is the bare identifier so dotted
    symbol search reads naturally."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    fields = [c for c in cls.children if c.kind == KIND_FIELD]
    names = {f.name for f in fields}
    # Includes promoted properties too.
    assert {"cache", "DEFAULT_ROLE", "repository", "maxCacheSize"}.issubset(names)
    # No `$` leaked into any field name.
    assert all("$" not in f.name for f in fields)


def test_multi_variable_property_emits_one_decl_per_name(php_dir):
    """`public string $a, $b = "x", $c;` → three separate field decls so
    every name resolves under symbol search."""
    r = PhpAdapter().parse(php_dir / "multi_var_property.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Bag")
    fields = [c for c in cls.children if c.kind == KIND_FIELD]
    field_names = {f.name for f in fields}
    assert {"a", "b", "c", "count"}.issubset(field_names)
    # Each field reuses the same modifier+type prefix in its signature.
    a, b, c = (
        _find(cls.children, name="a"),
        _find(cls.children, name="b"),
        _find(cls.children, name="c"),
    )
    for f in (a, b, c):
        assert f.signature.startswith("public string ")


def test_promoted_constructor_properties_become_fields(php_dir):
    """`__construct(public readonly string $name)` should add a field
    `name` on the enclosing class, separate from the constructor entry."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    fields = {f.name: f for f in cls.children if f.kind == KIND_FIELD}
    assert "repository" in fields
    rep = fields["repository"]
    assert rep.visibility == "private"
    assert "readonly" in rep.signature
    # Ctor is still present as a method-shaped CTOR entry — promotion is
    # additive, not destructive.
    assert _find(cls.children, kind=KIND_CTOR, name="__construct") is not None


def test_promoted_property_default_value_kept_in_signature(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    mc = _find(cls.children, name="maxCacheSize")
    assert mc is not None
    assert mc.visibility == "protected"
    assert "= 100" in mc.signature


def test_class_constant_with_type(php_dir):
    """PHP 8.3 typed class constants — adapter must keep the type in the
    signature and surface the declaration as a KIND_FIELD."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    const = _find(cls.children, name="DEFAULT_ROLE")
    assert const is not None
    assert const.kind == KIND_FIELD
    assert const.signature == 'public const string DEFAULT_ROLE = "guest"'


def test_top_level_constant(php_dir):
    """Top-level `const FOO = ...;` — KIND_FIELD on the namespace."""
    r = PhpAdapter().parse(php_dir / "user_service.php")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    const = _find(ns.children, name="APP_VERSION")
    assert const is not None
    assert const.kind == KIND_FIELD
    assert "APP_VERSION" in const.signature


def test_top_level_function_kind(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    ns = _find(r.declarations, kind=KIND_NAMESPACE)
    fn = _find(ns.children, name="make_service")
    assert fn is not None
    assert fn.kind == KIND_FUNCTION


def test_destructor_is_dtor(tmp_path):
    src = "<?php\nclass Foo { public function __destruct() {} }\n"
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    dtor = _find(cls.children, kind=KIND_DTOR, name="__destruct")
    assert dtor is not None


# --- Attributes / PHPDoc -------------------------------------------------


def test_attributes_collected_and_stripped_from_signature(php_dir):
    r = PhpAdapter().parse(php_dir / "traits_and_attributes.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserController")
    assert cls is not None
    # Attribute is captured in `attrs` and not duplicated into `signature`.
    assert any("Route" in a for a in cls.attrs)
    assert "#[" not in cls.signature
    # Member-level attribute on a method.
    show = _find(cls.children, name="show")
    assert show is not None
    assert any("Route" in a for a in show.attrs)
    assert "#[" not in show.signature


def test_attribute_with_string_containing_brackets_handled(tmp_path):
    """`#[Foo("[bar]")]` — bracket-balancer must skip string content so the
    attribute group closes at the right `]`."""
    src = '<?php\n#[Foo("a[b]c")]\nclass X {}\n'
    p = tmp_path / "x.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="X")
    assert cls is not None
    assert cls.signature == "class X"
    assert any("Foo" in a for a in cls.attrs)


def test_phpdoc_captured_only_for_double_star_blocks(php_dir):
    """`/** ... */` blocks → docs; `// ...`, `# ...`, `/* ... */` → not docs."""
    r = PhpAdapter().parse(php_dir / "line_comment.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="CommentedClass")
    foo = _find(cls.children, name="foo")
    bar = _find(cls.children, name="bar")
    baz = _find(cls.children, name="baz")
    quux = _find(cls.children, name="quux")
    assert foo.docs == [] and bar.docs == [] and baz.docs == []
    assert quux.docs and quux.docs[0].startswith("/**")


def test_class_phpdoc_captured(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert cls.docs and cls.docs[0].startswith("/**")
    assert "Coordinates user-related" in "\n".join(cls.docs)


# --- Imports --------------------------------------------------------------


def test_simple_imports(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    assert "use App\\Contracts\\Repository" in r.imports
    assert "use App\\Models\\User" in r.imports
    assert "use App\\Models\\Order as OrderModel" in r.imports
    assert "use function strlen" in r.imports
    assert "use const PHP_INT_MAX" in r.imports


def test_grouped_imports_expanded(php_dir):
    """`use Foo\\{A, B as Bb};` → one entry per leaf, each a complete
    source-true `use ...` statement."""
    r = PhpAdapter().parse(php_dir / "grouped_imports.php")
    assert "use App\\Models\\User" in r.imports
    assert "use App\\Models\\Post as BlogPost" in r.imports
    assert "use App\\Models\\Comment" in r.imports
    assert "use function App\\Helpers\\render" in r.imports
    assert "use function App\\Helpers\\escape as e" in r.imports
    assert "use const App\\Config\\DEFAULT_LIMIT" in r.imports
    assert "use const App\\Config\\MAX_RETRIES" in r.imports
    assert "use App\\Service\\Mailer" in r.imports
    assert "use App\\Service\\Cache as CacheService" in r.imports


def test_imports_collected_inside_bracketed_namespaces(php_dir):
    r = PhpAdapter().parse(php_dir / "bracketed_namespaces.php")
    assert "use App\\Foo" in r.imports
    assert "use App\\Bar" in r.imports


def test_imports_collects_legacy_require_once(php_dir):
    """Pre-Composer code uses `require_once` as the only dependency
    mechanism. The fixture's bootstrap line must surface as an import,
    or every WordPress / pre-PSR-4 file would look dependency-less.

    Membership rather than equality so the assertion stays robust if
    the fixture grows more top-level statements over time.
    """
    r = PhpAdapter().parse(php_dir / "no_namespace.php")
    assert 'require_once __DIR__ . "/bootstrap.php"' in r.imports


# --- include / require collection ---------------------------------------


def test_legacy_fixture_collects_top_level_includes(php_dir):
    """The fixture's top-level `require_once` / `include` statements
    must surface — without them the file would look dependency-less
    to an agent."""
    r = PhpAdapter().parse(php_dir / "legacy_includes.php")
    assert r.error_count == 0
    assert 'require_once __DIR__ . "/config.php"' in r.imports
    assert 'require_once dirname(__FILE__) . "/helpers.php"' in r.imports
    assert 'include "optional-helpers.php"' in r.imports
    assert 'include_once "optional-helpers-once.php"' in r.imports
    assert 'require ABSPATH . WPINC . "/load.php"' in r.imports
    assert 'require_once __DIR__ . "/finalize.php"' in r.imports


def test_legacy_fixture_skips_runtime_scopes(php_dir):
    """Lazy includes inside method bodies and function bodies are
    runtime loading — they must NOT pollute the file-level imports list."""
    r = PhpAdapter().parse(php_dir / "legacy_includes.php")
    joined = "\n".join(r.imports)
    assert "runtime-only.php" not in joined
    assert "init-only.php" not in joined


def test_all_four_include_flavours(tmp_path):
    """All four PHP file-loading keywords (`include`, `include_once`,
    `require`, `require_once`) are collected when used as top-level
    statements."""
    src = (
        "<?php\n"
        'include "a.php";\n'
        'include_once "b.php";\n'
        'require "c.php";\n'
        'require_once "d.php";\n'
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == [
        'include "a.php"',
        'include_once "b.php"',
        'require "c.php"',
        'require_once "d.php"',
    ]


def test_conditional_includes_are_not_collected(tmp_path):
    """`if (cond) require A; elseif (cond) require B; else require C;`
    is a polyfill / fallback pattern. We deliberately do NOT collect
    these — emitting the three branches as if they were a sequence
    would mislead an agent into thinking the file always loads all
    three. Same rule applies to `try/catch`, `switch`, `match`, and
    every loop construct. This matches how every other adapter
    handles conditional imports (Python `try/except` fallbacks,
    `if TYPE_CHECKING`, etc.)."""
    src = (
        "<?php\n"
        "if (file_exists('a.php')) {\n"
        "    require_once 'a.php';\n"
        "} elseif (file_exists('b.php')) {\n"
        "    require_once 'b.php';\n"
        "} else {\n"
        "    require_once 'c.php';\n"
        "}\n"
        "try {\n"
        "    require_once 'optional.php';\n"
        "} catch (\\Throwable $e) {}\n"
        "switch ($mode) {\n"
        "    case 'dev':  require_once 'dev.php';  break;\n"
        "}\n"
        "foreach ($plugins as $p) {\n"
        "    require_once $p;\n"
        "}\n"
        "$x = match ($env) { 'dev' => require 'dev.php', default => null };\n"
        "require_once 'unconditional.php';\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    # Only the unconditional top-level entry survives — everything else
    # lives inside control flow and gets dropped.
    assert r.imports == ["require_once 'unconditional.php'"]


def test_include_inside_function_body_excluded(tmp_path):
    """Includes inside function / method / closure / arrow-function
    bodies are runtime lazy-loading and never module-level. Only the
    top-level entry surfaces."""
    src = (
        "<?php\n"
        "require 'top.php';\n"
        "function helper() {\n"
        "    require 'inside-fn.php';\n"
        "}\n"
        "class C {\n"
        "    public function m() { require 'inside-method.php'; }\n"
        "}\n"
        "$a = function () { require 'closure.php'; };\n"
        "$b = fn() => require 'arrow.php';\n"
        "$obj = new class { public function load() { require 'anon.php'; } };\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == ["require 'top.php'"]


def test_include_inside_bracketed_namespace_collected(tmp_path):
    """`namespace Foo { require_once 'x.php'; ... }` — top-level
    requires inside a bracketed namespace body are file-level just
    like requires before any namespace declaration. Two consecutive
    bracketed namespaces emit their imports in source order."""
    src = (
        "<?php\n"
        "namespace A {\n"
        "    use App\\X;\n"
        "    require_once 'inside-A.php';\n"
        "    class X {}\n"
        "}\n"
        "namespace B {\n"
        "    require_once 'inside-B.php';\n"
        "    use App\\Y;\n"
        "    class Y {}\n"
        "}\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    # Source order: A's imports come before B's, and within each
    # namespace the use/require interleave matches the source.
    assert r.imports == [
        "use App\\X",
        "require_once 'inside-A.php'",
        "require_once 'inside-B.php'",
        "use App\\Y",
    ]


def test_include_dynamic_path_kept_verbatim(tmp_path):
    """Computed paths (concatenation, function calls, variables) are
    kept as source-true text — we don't try to evaluate or normalize.
    The agent reads the expression and decides what it means."""
    src = (
        "<?php\n"
        "require_once ABSPATH . WPINC . '/foo.php';\n"
        "require dirname(__FILE__) . '/lib/' . $name . '.php';\n"
        "require_once $custom_loader;\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == [
        "require_once ABSPATH . WPINC . '/foo.php'",
        "require dirname(__FILE__) . '/lib/' . $name . '.php'",
        "require_once $custom_loader",
    ]


def test_use_and_include_interleaved_in_source_order(tmp_path):
    """A file that mixes `use` declarations with top-level `require_once`
    must list both flavours in source order. Re-ordering would force
    the agent to learn a synthetic-ordering rule; source order is what
    they would see if they Read the file top-to-bottom."""
    src = (
        "<?php\n"
        "require_once 'pre.php';\n"
        "use App\\Foo;\n"
        "require_once 'mid.php';\n"
        "use App\\Bar;\n"
        "require_once 'post.php';\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == [
        "require_once 'pre.php'",
        "use App\\Foo",
        "require_once 'mid.php'",
        "use App\\Bar",
        "require_once 'post.php'",
    ]


def test_assignment_with_include_rhs_is_not_collected(tmp_path):
    """`$ret = require_once 'x.php';` is a top-level statement, but the
    statement itself is an assignment — its expression is the include,
    not the include itself. We collect only statements where the
    require/include IS the expression. Skipping wrapped forms keeps
    the rule simple ("one statement, one entry") and avoids debating
    whether assignment-wrapped requires are "real" imports.

    Assignment-wrapped top-level includes are also NOT counted as
    "conditional" — they execute unconditionally at module load.
    Counting them as conditional would mislead the agent.
    """
    src = (
        "<?php\n"
        "$ok = require_once 'wrapped.php';\n"
        "require_once 'plain.php';\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == ["require_once 'plain.php'"]
    assert r.conditional_imports_count == 0


def test_conditional_imports_count_set_for_skipped_includes(tmp_path):
    """The counter must equal "every include in the file minus the
    static ones we surfaced". This is the agent's signal that more
    dependencies live in the file than the static list shows."""
    src = (
        "<?php\n"
        "require_once 'static-1.php';\n"
        "require_once 'static-2.php';\n"
        "if (cond()) { require_once 'cond-a.php'; }\n"
        "try { require 'cond-b.php'; } catch (\\Throwable $e) {}\n"
        "function helper() { require 'inside-fn.php'; }\n"
        "class C { public function m() { require 'inside-method.php'; } }\n"
        "$a = function () { require 'inside-closure.php'; };\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == ["require_once 'static-1.php'", "require_once 'static-2.php'"]
    # 5 hidden includes: cond-a, cond-b, inside-fn, inside-method, inside-closure
    assert r.conditional_imports_count == 5


def test_conditional_imports_count_zero_when_all_static(tmp_path):
    """If every include lives at the file's top level there's nothing
    "conditional" to flag — counter stays at 0."""
    src = (
        "<?php\n"
        "require_once 'a.php';\n"
        "require_once 'b.php';\n"
        "use App\\Foo;\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_conditional_imports_count_with_no_static(tmp_path):
    """File whose every `require` lives inside an `if`/`else` chain
    (the WordPress wp-load.php shape) — `imports` is empty but the
    counter is non-zero so the agent sees there are deps to find."""
    src = (
        "<?php\n"
        "if (cond_a()) {\n"
        "    require_once 'a.php';\n"
        "} elseif (cond_b()) {\n"
        "    require_once 'b.php';\n"
        "} else {\n"
        "    require_once 'c.php';\n"
        "}\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 3


def test_conditional_count_matches_real_wordpress_load_shape(tmp_path):
    """End-to-end check using the exact wp-load.php shape (six requires,
    all inside the same if/elseif/else cascade): 0 static + 6 dynamic."""
    src = (
        "<?php\n"
        "if (file_exists(ABSPATH . 'wp-config.php')) {\n"
        "    require_once ABSPATH . 'wp-config.php';\n"
        "} elseif (@file_exists(dirname(ABSPATH) . '/wp-config.php')) {\n"
        "    require_once dirname(ABSPATH) . '/wp-config.php';\n"
        "} else {\n"
        "    require_once ABSPATH . WPINC . '/version.php';\n"
        "    require_once ABSPATH . WPINC . '/compat.php';\n"
        "    require_once ABSPATH . WPINC . '/load.php';\n"
        "    require_once ABSPATH . WPINC . '/functions.php';\n"
        "}\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 6


def test_conditional_count_marker_appears_in_digest(tmp_path):
    """Renderer integration: the `[+ N conditional includes]` suffix
    is what the agent actually reads. Verify it lands in digest output
    in both shapes — alongside static imports and on its own."""
    from ast_outline.core import DigestOptions, render_digest

    # Mixed: 1 static + N conditional.
    src_mixed = (
        "<?php\n"
        "require_once 'static.php';\n"
        "if (cond()) { require 'cond.php'; }\n"
    )
    p1 = tmp_path / "mixed.php"
    p1.write_text(src_mixed)
    r1 = PhpAdapter().parse(p1)
    digest1 = render_digest([r1], DigestOptions(show_imports=True))
    assert "imports: require_once 'static.php' [+ 1 conditional include]" in digest1

    # All conditional: 0 static + N conditional → bracket on its own.
    src_all_cond = (
        "<?php\n"
        "if (a()) { require 'a.php'; }\n"
        "if (b()) { require 'b.php'; }\n"
    )
    p2 = tmp_path / "all_cond.php"
    p2.write_text(src_all_cond)
    r2 = PhpAdapter().parse(p2)
    digest2 = render_digest([r2], DigestOptions(show_imports=True))
    assert "imports: [+ 2 conditional includes]" in digest2


def test_conditional_count_with_closure_inside_top_level_assignment(tmp_path):
    """Edge case: `$a = function () { require 'b.php'; };` — the
    closure assignment IS a top-level statement, but the require
    inside the closure body is in a runtime scope (executed when the
    closure is called, not at module load). Walker must descend into
    the closure body even when its containing statement is top-level.
    """
    src = (
        "<?php\n"
        "$a = function () { require 'in-closure.php'; };\n"
        "$b = fn() => require 'in-arrow.php';\n"
        "$c = new class { public function load() { require 'in-anon.php'; } };\n"
        "require 'top.php';\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.imports == ["require 'top.php'"]
    assert r.conditional_imports_count == 3


def test_conditional_count_zero_when_no_includes_at_all(tmp_path):
    """File with no include/require anywhere — counter stays at 0
    even when the file has plenty of other content."""
    src = (
        "<?php\n"
        "namespace App;\n"
        "use App\\Foo;\n"
        "class Bar {\n"
        "    public function m(): void {}\n"
        "}\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_conditional_count_singular_pluralization(tmp_path):
    """`1 conditional include` (singular) vs `2 conditional includes`
    (plural). Agent reads natural English, not `1 conditional includes`."""
    src = "<?php\nif (a()) { require 'one.php'; }\n"
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    assert r.conditional_imports_count == 1
    from ast_outline.core import _format_imports_line
    assert _format_imports_line([], 1).endswith("[+ 1 conditional include]")
    assert _format_imports_line([], 2).endswith("[+ 2 conditional includes]")


def test_includes_appear_in_imports_line_when_show_imports(tmp_path):
    """Renderer integration: `--imports` joins everything with `; `
    (the standard separator). Verify a real WP-style bootstrap file
    renders as one readable imports line."""
    from ast_outline.core import DigestOptions, render_digest

    src = (
        "<?php\n"
        "require_once __DIR__ . '/config.php';\n"
        "use App\\Foo;\n"
        "require_once __DIR__ . '/helpers.php';\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    digest = render_digest([r], DigestOptions(show_imports=True))
    assert (
        "imports: require_once __DIR__ . '/config.php'; "
        "use App\\Foo; "
        "require_once __DIR__ . '/helpers.php'"
    ) in digest


# --- Partial parse --------------------------------------------------------


def test_broken_file_still_yields_partial_outline(php_dir):
    """Malformed PHP must still produce a Declaration tree with whatever
    the parser was able to recover, plus error_count > 0 so the caller
    knows the IR is incomplete."""
    r = PhpAdapter().parse(php_dir / "broken_syntax.php")
    assert r.error_count > 0
    cls = _find(r.declarations, kind=KIND_CLASS, name="Salvageable")
    assert cls is not None
    method_names = {c.name for c in cls.children if c.kind == KIND_METHOD}
    # Both unbroken methods must survive the recovery.
    assert {"ok", "alsoOk"}.issubset(method_names)


# --- Line ranges ---------------------------------------------------------


def test_line_ranges_set_on_declarations(php_dir):
    r = PhpAdapter().parse(php_dir / "user_service.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserService")
    assert cls.start_line > 0 and cls.end_line >= cls.start_line
    for child in cls.children:
        assert child.start_line >= cls.start_line
        assert child.end_line <= cls.end_line


# --- Trait usage inside class body is NOT a member declaration ----------


def test_trait_use_inside_class_is_not_emitted_as_member(php_dir):
    """`use Loggable;` inside a class imports trait methods at runtime;
    it's not a declaration in our IR. The trait's own methods would be
    seen on the trait declaration, not on the using class."""
    r = PhpAdapter().parse(php_dir / "traits_and_attributes.php")
    cls = _find(r.declarations, kind=KIND_CLASS, name="UserController")
    member_names = {c.name for c in cls.children}
    assert "HasTimestamps" not in member_names
    assert "Loggable" not in member_names


# --- Reference returns / variadic / nullable -----------------------------


def test_signature_preserves_reference_return_and_variadic(tmp_path):
    src = (
        "<?php\n"
        "class Foo {\n"
        "    public function &getRef(): array { return $this->arr; }\n"
        "    public function variadic(string ...$args): void {}\n"
        "    public function nullable(?int $x = null): ?string { return null; }\n"
        "}\n"
    )
    p = tmp_path / "f.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="Foo")
    by_name = {c.name: c for c in cls.children}
    assert "&getRef()" in by_name["getRef"].signature
    assert "...$args" in by_name["variadic"].signature
    assert "?int $x = null" in by_name["nullable"].signature
    assert by_name["nullable"].signature.endswith("?string")


def test_multi_attribute_in_one_group(tmp_path):
    """`#[A, B(1)]` declares two attributes in one bracketed group. Adapter
    keeps the group's source-true text — `#[A, B(1)]` — as a single
    `attrs` entry rather than splitting into two."""
    src = "<?php\n#[A, B(1)]\nclass X {}\n"
    p = tmp_path / "x.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="X")
    assert cls is not None
    assert any("A" in a and "B(1)" in a for a in cls.attrs)


def test_class_constant_without_explicit_visibility_defaults_public(tmp_path):
    """PHP 7.1+ allows class consts to carry visibility modifiers; absent
    ones default to public per PHP spec."""
    src = "<?php\nclass C { const FOO = 1; }\n"
    p = tmp_path / "c.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="C")
    const = _find(cls.children, name="FOO")
    assert const is not None
    assert const.visibility == "public"


def test_php_deprecated_attribute_surfaces(tmp_path):
    """Modern PHP (8.4+) `#[\\Deprecated]` lives in `attrs`, so the digest
    deprecation marker fires correctly. PHPDoc `@deprecated` tags stay in
    `docs` (visible in outline) but don't trip the digest `[deprecated]`
    flag — that's an attribute-only signal."""
    from ast_outline.core import DigestOptions, render_digest

    src = (
        "<?php\n"
        "class C {\n"
        "    #[\\Deprecated]\n"
        "    public function old(): void {}\n"
        "}\n"
    )
    p = tmp_path / "c.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="C")
    old = _find(cls.children, name="old")
    assert old is not None
    assert any("Deprecated" in a for a in old.attrs)
    digest = render_digest([r], DigestOptions())
    assert "[deprecated]" in digest


def test_static_property_keeps_static_in_signature(tmp_path):
    src = "<?php\nclass C { public static int $count = 0; }\n"
    p = tmp_path / "c.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="C")
    count = _find(cls.children, name="count")
    assert count is not None
    assert "static" in count.signature
    assert count.visibility == "public"


def test_anonymous_class_does_not_surface_at_top_level(tmp_path):
    """`new class extends Base { ... }` lives inside an expression node;
    the walker never descends into expressions, so anonymous classes
    never end up as Declarations. Real adjacent classes still do."""
    src = (
        "<?php\n"
        "$x = new class extends Base { public function go(): void {} };\n"
        "class RealOne {}\n"
    )
    p = tmp_path / "anon.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    classes = _find_all(r.declarations, kind=KIND_CLASS)
    names = {c.name for c in classes}
    assert names == {"RealOne"}


def test_union_and_intersection_types_in_promoted_property(tmp_path):
    """`Cache|Redis` (union) and `iterable&Countable` (intersection) must
    both survive in the rendered field signature."""
    src = (
        "<?php\n"
        "class C {\n"
        "    public function __construct(\n"
        "        private readonly Cache|Redis $cache,\n"
        "        private iterable&\\Countable $items,\n"
        "    ) {}\n"
        "}\n"
    )
    p = tmp_path / "c.php"
    p.write_text(src)
    r = PhpAdapter().parse(p)
    cls = _find(r.declarations, kind=KIND_CLASS, name="C")
    cache = _find(cls.children, name="cache")
    items = _find(cls.children, name="items")
    assert cache is not None and "Cache|Redis" in cache.signature
    assert items is not None and "iterable&\\Countable" in items.signature


def test_promoted_property_doc_start_byte_points_at_parameter(tmp_path):
    """Promoted ctor properties left `doc_start_byte` at the dataclass
    default 0 — `show` survived via the `or` fallback, but the JSON
    export carries the field verbatim, handing consumers a garbage
    offset. It must equal the parameter's own start."""
    p = tmp_path / "a.php"
    p.write_text(
        "<?php\nclass Point {\n"
        "    public function __construct(public int $x, private int $y) {}\n"
        "}\n",
        encoding="utf-8",
    )
    r = PhpAdapter().parse(p)
    cls = r.declarations[0]
    fields = [c for c in cls.children if c.kind == "field"]
    assert fields, "promoted properties missing"
    for f in fields:
        assert f.doc_start_byte == f.start_byte > 0, (
            f.name, f.doc_start_byte, f.start_byte
        )
