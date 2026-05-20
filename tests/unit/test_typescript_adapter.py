"""Tests for the TypeScript / JavaScript adapter (.ts .tsx .js .jsx)."""
from __future__ import annotations

from ast_outline.adapters.typescript import TypeScriptAdapter
from ast_outline.core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    Declaration,
    DigestOptions,
    render_digest,
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


def test_parse_ts_file(fixtures_dir):
    path = fixtures_dir / "typescript" / "storage_service.ts"
    result = TypeScriptAdapter().parse(path)
    assert result.path == path
    assert result.language == "typescript"
    assert result.line_count > 0
    assert result.declarations


def test_parse_tsx_file(fixtures_dir):
    path = fixtures_dir / "typescript" / "react_page.tsx"
    result = TypeScriptAdapter().parse(path)
    assert result.language == "typescript"
    assert result.declarations


def test_parse_js_file(fixtures_dir):
    """JS is parsed by the TS grammar (superset); adapter should still produce IR."""
    path = fixtures_dir / "typescript" / "plain_module.js"
    result = TypeScriptAdapter().parse(path)
    assert result.declarations
    # `greet` and `add` are exported functions, `Counter` is a class
    assert _find(result.declarations, kind=KIND_FUNCTION, name="greet") is not None
    assert _find(result.declarations, kind=KIND_FUNCTION, name="add") is not None
    assert _find(result.declarations, kind=KIND_CLASS, name="Counter") is not None


# --- Classes --------------------------------------------------------------


def test_class_basic_structure(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    svc = _find(r.declarations, kind=KIND_CLASS, name="StorageService")
    assert svc is not None
    # Class has fields + methods
    method_names = {c.name for c in svc.children if c.kind in (KIND_METHOD, KIND_CTOR)}
    assert {"init", "doInit", "getAll", "getProject", "saveProject", "log"}.issubset(method_names)
    field_names = {c.name for c in svc.children if c.kind == KIND_FIELD}
    assert {"db", "initPromise"}.issubset(field_names)


def test_class_method_visibility_modifiers(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    svc = _find(r.declarations, kind=KIND_CLASS, name="StorageService")
    methods = {c.name: c for c in svc.children if c.kind == KIND_METHOD}
    # `async init(): ...` has no modifier → default public in TS
    assert methods["init"].visibility == "public"
    # `private async doInit`
    assert methods["doInit"].visibility == "private"
    # `protected log`
    assert methods["log"].visibility == "protected"


def test_class_field_visibility(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    svc = _find(r.declarations, kind=KIND_CLASS, name="StorageService")
    fields = {c.name: c for c in svc.children if c.kind == KIND_FIELD}
    assert fields["db"].visibility == "private"
    assert fields["initPromise"].visibility == "private"


def test_class_field_signature_drops_default_value(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    svc = _find(r.declarations, kind=KIND_CLASS, name="StorageService")
    db = next(c for c in svc.children if c.name == "db")
    # Signature should keep the type but drop the `= null` default
    assert "IDBDatabase" in db.signature
    assert "= null" not in db.signature


def test_constructor_mapped_to_kind_ctor(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "plain_module.js")
    counter = _find(r.declarations, kind=KIND_CLASS, name="Counter")
    ctor = _find(counter.children, name="constructor")
    assert ctor is not None
    assert ctor.kind == KIND_CTOR


def test_class_extends_captured_as_base(fixtures_dir):
    """`class User extends Entity` → bases == ['Entity']. Uses types.ts
    which has an `interface User extends Entity` — tests the interface path;
    heritage for classes is covered in storage_service / decorators."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    user = _find(r.declarations, kind=KIND_INTERFACE, name="User")
    assert user is not None
    assert "Entity" in user.bases


def test_generic_heritage_preserved(fixtures_dir):
    """`interface Repository<T extends Entity>` keeps its type parameters in
    the signature."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    repo = _find(r.declarations, kind=KIND_INTERFACE, name="Repository")
    assert repo is not None
    assert "<T extends Entity>" in repo.signature


# --- Interfaces -----------------------------------------------------------


def test_interface_properties_become_fields(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    iface = _find(r.declarations, kind=KIND_INTERFACE, name="DBSchema")
    names = {c.name for c in iface.children if c.kind == KIND_FIELD}
    assert {"projects", "documents", "settings"}.issubset(names)


def test_interface_method_signatures_become_methods(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    repo = _find(r.declarations, kind=KIND_INTERFACE, name="Repository")
    method_names = {c.name for c in repo.children if c.kind == KIND_METHOD}
    assert {"get", "list", "save"}.issubset(method_names)


# --- Enums ---------------------------------------------------------------


def test_numeric_enum_members(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    e = _find(r.declarations, kind=KIND_ENUM, name="Status")
    assert e is not None
    members = [c.name for c in e.children if c.kind == KIND_ENUM_MEMBER]
    assert members == ["Idle", "Loading", "Ready", "Error"]


def test_string_enum_members(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    e = _find(r.declarations, kind=KIND_ENUM, name="Priority")
    members = [c.name for c in e.children if c.kind == KIND_ENUM_MEMBER]
    assert members == ["Low", "Medium", "High"]


# --- Functions -----------------------------------------------------------


def test_top_level_function_declaration(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "react_page.tsx")
    # `function wrapBody(content: string): string`
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="wrapBody")
    assert fn is not None
    assert "function wrapBody" in fn.signature
    assert "): string" in fn.signature


def test_async_function_keeps_async_keyword(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "react_page.tsx")
    gen = _find(r.declarations, kind=KIND_FUNCTION, name="generateStaticParams")
    assert gen is not None
    assert "async" in gen.signature


def test_export_default_function_component(fixtures_dir):
    """`export default function Page(...) {...}` is captured and retains its
    signature; the byte range starts at `export`."""
    path = fixtures_dir / "typescript" / "react_page.tsx"
    r = TypeScriptAdapter().parse(path)
    page = _find(r.declarations, kind=KIND_FUNCTION, name="Page")
    assert page is not None
    assert "function Page" in page.signature
    # Byte range should start at the `export` keyword so `show` prints it too
    slice_text = path.read_bytes()[page.start_byte : page.end_byte].decode("utf8")
    assert slice_text.startswith("export default function Page")


def test_arrow_function_assigned_to_const_is_function(fixtures_dir):
    """`export const Sidebar = ({ items }: ...): JSX.Element => (...)` → KIND_FUNCTION."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "react_page.tsx")
    sidebar = _find(r.declarations, kind=KIND_FUNCTION, name="Sidebar")
    assert sidebar is not None
    assert "Sidebar" in sidebar.signature
    # `=>` present at the end of the signature line (body starts after)
    assert "=>" in sidebar.signature


def test_const_with_primitive_value_is_field(fixtures_dir):
    """`const DB_NAME = "demo-db"` → KIND_FIELD, not KIND_FUNCTION."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    dbname = _find(r.declarations, kind=KIND_FIELD, name="DB_NAME")
    assert dbname is not None
    assert dbname.signature.startswith("const DB_NAME")


# --- Exports handling ----------------------------------------------------


def test_exported_class_preserves_export_in_byte_range(fixtures_dir):
    """`export class Foo` — byte range starts at `export`, so `show` prints
    the export keyword."""
    path = fixtures_dir / "typescript" / "storage_service.ts"
    r = TypeScriptAdapter().parse(path)
    svc = _find(r.declarations, kind=KIND_CLASS, name="StorageService")
    slice_text = path.read_bytes()[svc.start_byte : svc.end_byte].decode("utf8")
    assert slice_text.startswith("export class StorageService")


def test_non_exported_interface_still_captured(fixtures_dir):
    """`interface DBSchema` (no export) should still appear in the IR."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "storage_service.ts")
    schema = _find(r.declarations, kind=KIND_INTERFACE, name="DBSchema")
    assert schema is not None


# --- Type aliases --------------------------------------------------------


def test_type_alias_becomes_field(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    ta = _find(r.declarations, kind=KIND_FIELD, name="UserId")
    assert ta is not None
    assert ta.signature.startswith("export type UserId")


def test_generic_type_alias(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "types.ts")
    ta = _find(r.declarations, kind=KIND_FIELD, name="Result")
    assert ta is not None
    assert "Result<T>" in ta.signature


# --- Decorators ----------------------------------------------------------


def test_class_decorator_captured(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "decorators.ts")
    ctl = _find(r.declarations, kind=KIND_CLASS, name="UserController")
    assert ctl is not None
    # `@Controller("/users")`
    assert any("@Controller" in a for a in ctl.attrs)


def test_method_decorators_captured(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "decorators.ts")
    ctl = _find(r.declarations, kind=KIND_CLASS, name="UserController")
    find_all = _find(ctl.children, name="findAll")
    create = _find(ctl.children, name="create")
    assert find_all is not None
    assert create is not None
    assert any("@Get" in a for a in find_all.attrs)
    assert any("@Post" in a for a in create.attrs)


def test_decorated_class_byte_range_includes_decorator(fixtures_dir):
    """`show` should print the @Controller line together with the class."""
    path = fixtures_dir / "typescript" / "decorators.ts"
    r = TypeScriptAdapter().parse(path)
    ctl = _find(r.declarations, kind=KIND_CLASS, name="UserController")
    slice_text = path.read_bytes()[ctl.doc_start_byte : ctl.end_byte].decode("utf8")
    # The decorator line must be present before `class`
    assert "@Controller" in slice_text
    assert "export class UserController" in slice_text


# --- Visibility ----------------------------------------------------------


def test_class_member_without_modifier_defaults_to_public(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "visibility.ts")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Visibility")
    m = next(c for c in cls.children if c.name == "publicByDefault")
    assert m.visibility == "public"


def test_explicit_private_modifier_captured(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "visibility.ts")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Visibility")
    m = next(c for c in cls.children if c.name == "explicitPrivate")
    assert m.visibility == "private"


def test_protected_modifier_captured(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "visibility.ts")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Visibility")
    m = next(c for c in cls.children if c.name == "explicitProtected")
    assert m.visibility == "protected"


def test_hash_private_name_is_private(fixtures_dir):
    """`#truePrivate()` — TS 4.3+ hard-private names should be flagged private."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "visibility.ts")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Visibility")
    m = next((c for c in cls.children if "truePrivate" in c.name), None)
    assert m is not None
    assert m.visibility == "private"


def test_underscore_prefix_is_conventionally_private(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "visibility.ts")
    cls = _find(r.declarations, kind=KIND_CLASS, name="Visibility")
    m = next(c for c in cls.children if c.name == "_conventionallyPrivate")
    assert m.visibility == "private"


# --- Docs (preceding comments) -------------------------------------------


def test_jsdoc_above_function_captured_as_docs(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "react_page.tsx")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="generateMetadata")
    assert fn.docs
    joined = "\n".join(fn.docs)
    assert "Generate metadata" in joined


def test_line_comments_above_function_captured_as_docs(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "react_page.tsx")
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="wrapBody")
    assert fn.docs
    assert any("plain helper" in d for d in fn.docs)


# --- Conditional imports counter (dynamic `import('...')`) ---------------
#
# Adapter-level integration check that `parse()` actually wires the
# counter into `ParseResult`. Cross-language semantic coverage (what
# does and doesn't count, edge cases per scope kind) lives in
# `test_imports.py`. Mirrors `test_php_adapter::
# test_conditional_imports_count_set_for_skipped_includes`.


def test_conditional_imports_count_set_for_dynamic_imports(tmp_path):
    p = tmp_path / "lazy.ts"
    p.write_text(
        "import { foo } from './a';\n"
        "async function loadOne() {\n"
        "  return import('./one');\n"
        "}\n"
        "class Service {\n"
        "  async loadTwo() {\n"
        "    return import('./two');\n"
        "  }\n"
        "}\n"
        "if (cond) {\n"
        "  const three = await import('./three');\n"
        "}\n"
    )
    r = TypeScriptAdapter().parse(p)
    assert r.imports == ["import { foo } from './a'"]
    assert r.conditional_imports_count == 3


def test_conditional_imports_count_zero_for_only_static_top_level(tmp_path):
    p = tmp_path / "static.ts"
    p.write_text(
        "import { foo } from './a';\n"
        "import bar from './b';\n"
        "export const x = 1;\n"
    )
    r = TypeScriptAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_conditional_imports_count_works_for_plain_js_extension(tmp_path):
    """The adapter handles `.js` / `.mjs` / `.cjs` with the TypeScript
    grammar (TS is a JS superset). Dynamic `import('...')` is native
    ES2020+, so JS files must get the same counter treatment as `.ts`."""
    p = tmp_path / "lazy.mjs"
    p.write_text(
        "import { foo } from './a.js';\n"
        "export async function loadPlugin() {\n"
        "  const mod = await import('./plugin.js');\n"
        "  return mod;\n"
        "}\n"
    )
    r = TypeScriptAdapter().parse(p)
    assert r.imports == ["import { foo } from './a.js'"]
    assert r.conditional_imports_count == 1


# --- Callback-DSL blocks (KIND_BLOCK) -------------------------------------
#
# Issue #3: TS/JS expresses structure through callback-passing calls
# (`describe`/`it`/`test`, Pinia `defineStore`, …) that a declaration-only
# walk misses entirely. These tests pin both the true-positive descent and
# the false-positive rejections.


def test_callback_blocks_found(fixtures_dir):
    """describe / it / defineStore calls become KIND_BLOCK declarations.

    A bare-call block (`describe('...')`, `it('...')`) is named after its
    string label; an assigned one (`const s = defineStore(...)`) after
    the variable it binds to."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    block_names = {b.name for b in _find_all(r.declarations, kind=KIND_BLOCK)}
    assert block_names == {
        "outer suite",               # describe('outer suite', ...)
        "nested suite",              # describe('nested suite', ...)
        "case with locals",          # it('case with locals', ...)
        "case with only assertions", # it('case with only assertions', ...)
        "useCounter",                # const useCounter = defineStore(...)
        "useExportedStore",          # export const useExportedStore = defineStore(...)
    }


def test_callback_block_nesting(fixtures_dir):
    """describe → nested describe → it → inner const all nest correctly."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    outer = _find(r.declarations, kind=KIND_BLOCK, name="outer suite")
    assert outer is not None
    assert outer.signature == "describe('outer suite')"
    nested = _find(outer.children, kind=KIND_BLOCK, name="nested suite")
    assert nested is not None and nested.signature == "describe('nested suite')"
    case = _find(nested.children, kind=KIND_BLOCK, name="case with locals")
    assert case is not None and case.signature == "it('case with locals')"
    # The local `const` inside the `it` body is descended into.
    assert _find(case.children, kind=KIND_FIELD, name="local") is not None


def test_it_with_only_assertions_is_still_a_block(fixtures_dir):
    """An `it` whose body holds only assertions (no declarations) still
    surfaces — the string label is the qualifying signal, not body
    content. Without this a typical leaf test would vanish."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    leaf = _find(r.declarations, kind=KIND_BLOCK, name="case with only assertions")
    assert leaf is not None
    assert leaf.signature == "it('case with only assertions')"
    assert leaf.children == []


def test_setup_store_block_has_members(fixtures_dir):
    """`const useCounter = defineStore('counter', () => {...})` descends
    into the setup callback — its inner ref / function become children."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    store = _find(r.declarations, kind=KIND_BLOCK, name="useCounter")
    assert store is not None
    assert store.signature == "const useCounter = defineStore('counter')"
    assert _find(store.children, kind=KIND_FIELD, name="count") is not None
    assert _find(store.children, kind=KIND_FUNCTION, name="increment") is not None


def test_exported_store_block_signature_keeps_export(fixtures_dir):
    """`export const` prefix survives onto the block signature."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    store = _find(r.declarations, kind=KIND_BLOCK, name="useExportedStore")
    assert store is not None
    assert store.signature == "export const useExportedStore = defineStore('exported')"


def test_block_signature_drops_callback_body(fixtures_dir):
    """No block signature leaks the callback body — that was the original
    `nasa.ts` garbage symptom in issue #3."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    for b in _find_all(r.declarations, kind=KIND_BLOCK):
        assert "=>" not in b.signature
        assert "{" not in b.signature
        assert len(b.signature) <= 140


def test_false_positives_are_not_blocks(fixtures_dir):
    """Calls that resemble callback-DSLs but aren't named containers must
    NOT be promoted: setTimeout (trailing number), registerEffect
    (trailing deps array), emitter.on (member-expression callee),
    console.log (no callback). None should appear as a KIND_BLOCK."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    block_names = {b.name for b in _find_all(r.declarations, kind=KIND_BLOCK)}
    for bait in ("setTimeout", "registerEffect", "emitter", "on", "console", "log"):
        assert bait not in block_names


def test_label_must_be_first_argument(fixtures_dir):
    """A call carrying a string label in a NON-first slot —
    `defineGetter(obj, 'name', fn)`, Express's property-definition
    wrapper — is not a named container and must not become a block.
    Only a leading string label qualifies."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    blocks = _find_all(r.declarations, kind=KIND_BLOCK)
    for b in blocks:
        assert b.name != "computedValue"
        assert "defineGetter" not in b.signature


def test_function_wrapper_without_label_stays_field(fixtures_dir):
    """`const wrapped = action(fn)` — plain-identifier callee + trailing
    callback but NO string label — is a bare function wrapper, not a
    named container. It must stay a KIND_FIELD, not become a block."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    wrapped = _find(r.declarations, name="wrapped")
    assert wrapped is not None
    assert wrapped.kind == KIND_FIELD


def test_exported_function_wrapper_keeps_field_signature(fixtures_dir):
    """`export const w = action(fn)` matches the block *shape* but has no
    label — it must stay a field, and the `export`-widened signature path
    must not fabricate a block-style `(label)` signature for it."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    w = _find(r.declarations, name="exportedWrapper")
    assert w is not None
    assert w.kind == KIND_FIELD
    # The real RHS is `action((payload) => ...)`; the signature must
    # reflect that, not a synthetic `action(…)` block head.
    assert w.signature.startswith("export const exportedWrapper = action(")
    assert "…)" not in w.signature


def test_field_embedded_body_is_elided_not_dumped(fixtures_dir):
    """A field whose value embeds a function body must render with the
    body collapsed to `{…}` — an outline shows structure, it never dumps
    implementation code into a signature line."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    wrapped = _find(r.declarations, name="wrapped")
    assert wrapped is not None and wrapped.kind == KIND_FIELD
    assert wrapped.signature == "const wrapped = action((payload: string) => {…})"
    # No statement from the body leaked into the signature.
    assert "JSON.parse" not in wrapped.signature
    # Same for the `export`-widened signature path.
    exported = _find(r.declarations, name="exportedWrapper")
    assert exported.signature == (
        "export const exportedWrapper = action((payload: string) => {…})"
    )
    assert "payload.trim" not in exported.signature


def test_no_statement_block_leaks_into_any_outline_signature(fixtures_dir):
    """Whole-file guard: no declaration's signature may contain a raw
    statement (the symptom issue #3 complained about). Every embedded
    body must have been elided to `{…}`."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    for d in _find_all(r.declarations):
        # A real statement block opens `{` then code; after elision the
        # only brace pair a signature may carry is the `{…}` placeholder
        # or an inline data object — never `{ const`, `{ return`, etc.
        for leak in ("{ const ", "{ return ", "{ let ", "; "):
            assert leak not in d.signature, (d.name, d.signature)


def test_plain_declarations_unaffected_by_block_rule(fixtures_dir):
    """Ordinary functions / classes sitting next to callback-DSL blocks
    are still picked up exactly as before."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    assert _find(r.declarations, kind=KIND_FUNCTION, name="plainFunction") is not None
    cls = _find(r.declarations, kind=KIND_CLASS, name="PlainClass")
    assert cls is not None
    assert _find(cls.children, kind=KIND_METHOD, name="method") is not None


def test_callbacks_fixture_parses_clean(fixtures_dir):
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    assert r.error_count == 0


def test_ordinary_file_produces_no_blocks(fixtures_dir):
    """Regression guard: the block rule must not fire on a normal
    class-and-method file with no callback-DSL calls."""
    for name in ("storage_service.ts", "hierarchy.ts", "visibility.ts"):
        r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / name)
        assert _find_all(r.declarations, kind=KIND_BLOCK) == []


def test_digest_renders_block_with_members(fixtures_dir):
    """`digest` renders a top-level block like a type: a `block <label>`
    header followed by member tokens for the nested cases. Without this
    the digest would either hide the block's content (inconsistent with
    the header counters) or show a useless `describe [block]` token."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "callbacks.ts")
    out = render_digest([r], DigestOptions())
    # Header line names the suite by its label, not the `describe` keyword.
    assert "block outer suite" in out
    # Nested `it` cases surface as member tokens — label quoted so an
    # internal comma can't collide with the `, ` token separator.
    assert "'case with only assertions' [block]" in out
    # The store block is named after its variable.
    assert "block useCounter" in out
    # The DSL keyword must not leak as a digest name.
    assert "describe [block]" not in out


def test_digest_block_label_with_comma_stays_one_token(tmp_path):
    """A test name containing a comma must remain a single digest token —
    the quoted label keeps the internal comma from reading as the `, `
    separator that `_wrap_tokens` joins member tokens with."""
    p = tmp_path / "x.spec.ts"
    p.write_text(
        "describe('math', () => {\n"
        "  it('handles a, b, and c', () => { check() })\n"
        "  it('plain case', () => { check() })\n"
        "})\n"
    )
    r = TypeScriptAdapter().parse(p)
    out = render_digest([r], DigestOptions())
    assert "'handles a, b, and c' [block]" in out


# --- Commented-out code vs genuine docs ----------------------------------


def test_commented_out_code_run_is_dropped(fixtures_dir):
    """A leading run of `//` comments that is disabled code (braces,
    arrows, declarations) must not render as documentation."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "commented_code.ts")
    fn = _find(r.declarations, name="afterDisabledCode")
    assert fn is not None
    assert fn.docs == []


def test_genuine_doc_comment_is_kept(fixtures_dir):
    """One-line and two-line prose doc comments survive the heuristic."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "commented_code.ts")
    documented = _find(r.declarations, name="documented")
    assert documented is not None
    assert any("real one-line doc comment" in d for d in documented.docs)
    two_line = _find(r.declarations, name="twoLineProse")
    assert two_line is not None
    assert len(two_line.docs) == 2


def test_prose_starting_with_keywords_is_kept(fixtures_dir):
    """Prose that happens to begin with words like `For` / `If` must not
    be misclassified as code — the heuristic keys on structural shape
    (brackets, `=>`, declaration keywords), not leading words."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "commented_code.ts")
    fn = _find(r.declarations, name="proseStartingWithKeywords")
    assert fn is not None
    assert len(fn.docs) == 2


def test_prose_with_bare_arrows_is_kept(fixtures_dir):
    """`=>` in prose ("maps key => value") must not count as code — only
    an arrow opening a body/paren-group (`=> {` / `=> (`) is a code
    signal. Two prose lines each carrying a bare `=>` must survive."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "commented_code.ts")
    fn = _find(r.declarations, name="proseWithArrows")
    assert fn is not None
    assert len(fn.docs) == 2


def test_jsdoc_block_is_always_kept(fixtures_dir):
    """A `/** ... */` JSDoc block is documentation by definition — the
    commented-out-code heuristic only judges `//` line-comment runs."""
    r = TypeScriptAdapter().parse(fixtures_dir / "typescript" / "commented_code.ts")
    fn = _find(r.declarations, name="jsDocumented")
    assert fn is not None
    assert fn.docs and fn.docs[0].startswith("/**")


def test_bare_describe_statement_block(tmp_path):
    """A top-level `describe(...)` expression statement (no assignment)
    surfaces as a block named after its string label."""
    p = tmp_path / "suite.spec.ts"
    p.write_text(
        "describe('my suite', () => {\n"
        "  it('does a thing', () => {\n"
        "    expect(1).toBe(1)\n"
        "  })\n"
        "})\n"
    )
    r = TypeScriptAdapter().parse(p)
    suite = _find(r.declarations, kind=KIND_BLOCK, name="my suite")
    assert suite is not None
    assert suite.signature == "describe('my suite')"
    assert _find(suite.children, kind=KIND_BLOCK, name="does a thing") is not None


def test_multiline_describe_signature_has_no_spurious_space(tmp_path):
    """A `describe(` whose label sits on the next line must still render
    `describe('label')` — not `describe( 'label')` with a stray space
    from the collapsed newline."""
    p = tmp_path / "wrapped.spec.ts"
    p.write_text(
        "describe(\n"
        "  'a long suite name',\n"
        "  () => {\n"
        "    it('case', () => { expect(1).toBe(1) })\n"
        "  },\n"
        ")\n"
    )
    r = TypeScriptAdapter().parse(p)
    suite = _find(r.declarations, kind=KIND_BLOCK, name="a long suite name")
    assert suite is not None
    assert suite.signature == "describe('a long suite name')"
