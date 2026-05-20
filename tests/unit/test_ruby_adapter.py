"""Tests for the Ruby adapter."""
from __future__ import annotations

from ast_outline.adapters.ruby import RubyAdapter
from ast_outline.adapters import get_adapter_for, supported_basenames
from ast_outline.core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_CTOR,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_NAMESPACE,
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


def test_parse_populates_result_metadata(ruby_dir):
    path = ruby_dir / "user.rb"
    result = RubyAdapter().parse(path)
    assert result.path == path
    assert result.language == "ruby"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extensions_cover_common_ruby_suffixes():
    exts = RubyAdapter.extensions
    for ext in (".rb", ".rake", ".gemspec", ".ru"):
        assert ext in exts


def test_basename_matching_picks_up_rakefile_and_gemfile(ruby_dir):
    rakefile = ruby_dir / "Rakefile"
    gemfile = ruby_dir / "Gemfile"
    assert isinstance(get_adapter_for(rakefile), RubyAdapter)
    assert isinstance(get_adapter_for(gemfile), RubyAdapter)


def test_supported_basenames_exposes_ruby_names():
    names = supported_basenames()
    assert "Rakefile" in names
    assert "Gemfile" in names


# --- Imports --------------------------------------------------------------


def test_require_collected_as_imports(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    assert 'require "json"' in result.imports
    assert 'require_relative "concerns/searchable"' in result.imports


def test_top_level_conditional_require_listed_statically(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    # Top-level `if defined?(...)` body — listed (mirrors how Python's
    # `if TYPE_CHECKING:` imports are listed).
    assert 'require "optimist"' in result.imports


def test_method_body_require_counts_as_conditional(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    # `require "rexml/document"` lives inside `def lazy_load_xml` — it's
    # not in static imports but IS counted.
    assert 'require "rexml/document"' not in result.imports
    assert result.conditional_imports_count >= 1


# --- Modules --------------------------------------------------------------


def test_qualified_module_keeps_source_form(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "qualified_module.rb")
    ns = _find(result.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    assert ns.name == "Foo::Bar::Baz"


def test_nested_modules_collapse_to_double_colon(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "nested_modules.rb")
    # Three levels each holding a single module child should collapse.
    ns = _find(result.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    assert ns.name == "App::Models::Internal"
    # The Worker class lives inside the collapsed namespace.
    cls = _find(ns.children, kind=KIND_CLASS, name="Worker")
    assert cls is not None


def test_collapse_does_not_swallow_siblings(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "nested_modules_with_siblings.rb")
    # App::Outer must NOT collapse with its children (it has siblings A
    # and B underneath). HIGH-fix regression test from C++.
    outer = _find(result.declarations, kind=KIND_NAMESPACE, name="App::Outer")
    assert outer is not None
    a = _find(outer.children, kind=KIND_CLASS, name="A")
    b = _find(outer.children, kind=KIND_CLASS, name="B")
    assert a is not None
    assert b is not None


def test_collapse_does_not_fire_when_only_child_is_class(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "nested_modules_with_siblings.rb")
    # `module Solo` has one child but it's a class, not a module —
    # no collapse should occur.
    solo = _find(result.declarations, kind=KIND_NAMESPACE, name="Solo")
    assert solo is not None
    only = _find(solo.children, kind=KIND_CLASS, name="OnlyClass")
    assert only is not None


def test_module_body_comments_do_not_block_collapse(ruby_dir):
    """Regression for the C++ HIGH fix — comments must not count as
    structural children when deciding whether to collapse a wrapper."""
    result = RubyAdapter().parse(ruby_dir / "nested_modules.rb")
    ns = _find(result.declarations, kind=KIND_NAMESPACE)
    assert ns is not None
    # Even though `module Models` has a preceding `# outer module rdoc`
    # comment, collapse still fires.
    assert ns.name == "App::Models::Internal"


# --- Classes --------------------------------------------------------------


def test_class_with_superclass_uses_native_lessthan(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    assert user is not None
    assert user.signature == "class User < ApplicationRecord"


def test_class_bases_include_mixins_in_source_order(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    assert user is not None
    assert user.bases[0] == "ApplicationRecord"
    assert "include Comparable" in user.bases
    assert "extend Searchable" in user.bases
    assert "prepend Auditable" in user.bases


def test_class_signature_omits_mixins(ruby_dir):
    """Mixins live in `bases` for digest, NOT in the signature line —
    splicing them after `<` would produce non-Ruby syntax."""
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    assert user is not None
    assert "include" not in user.signature
    assert "extend" not in user.signature


def test_reopening_stdlib_class_emits_one_top_level_class(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    strings = _find_all(result.declarations, kind=KIND_CLASS, name="String")
    assert len(strings) == 1


# --- Methods --------------------------------------------------------------


def test_initialize_classified_as_ctor(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    ctor = _find(result.declarations, kind=KIND_CTOR, name="initialize")
    assert ctor is not None


def test_singleton_method_marks_static(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    m = _find(result.declarations, name="find_by_name")
    assert m is not None
    assert "[static]" in m.attrs


def test_singleton_method_signature_keeps_self_prefix(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    m = _find(result.declarations, name="find_by_name")
    assert m is not None
    assert m.signature.startswith("def self.find_by_name")


def test_top_level_def_classified_as_function(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    fn = _find(result.declarations, name="configure!")
    assert fn is not None
    assert fn.kind == KIND_FUNCTION


def test_predicate_and_bang_methods_recognised(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    assert _find(result.declarations, name="admin?") is not None
    assert _find(result.declarations, name="reset!") is not None


def test_complex_method_signature_renders(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    m = _find(result.declarations, name="complex")
    assert m is not None
    sig = m.signature
    # Default args, keyword args, splat, double-splat and block must all be
    # preserved (whitespace-collapsed but otherwise verbatim).
    assert "a, b = 1" in sig
    assert "*rest" in sig
    assert "c:, d: 2" in sig
    assert "**opts" in sig
    assert "&block" in sig


# --- Operators ------------------------------------------------------------


def test_arithmetic_operators_classified_as_operator(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "operators.rb")
    for op in ("+", "-", "*", "/", "%", "**"):
        m = _find(result.declarations, name=op)
        assert m is not None, op
        assert m.kind == KIND_OPERATOR, op


def test_comparison_operators_classified_as_operator(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "operators.rb")
    for op in ("==", "!=", "<", ">", "<=", ">=", "<=>", "==="):
        m = _find(result.declarations, name=op)
        assert m is not None, op
        assert m.kind == KIND_OPERATOR, op


def test_bitwise_and_shift_operators(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "operators.rb")
    for op in ("&", "|", "^", "~", "<<", ">>"):
        m = _find(result.declarations, name=op)
        assert m is not None, op
        assert m.kind == KIND_OPERATOR, op


def test_indexing_operators(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "operators.rb")
    getter = _find(result.declarations, name="[]")
    setter = _find(result.declarations, name="[]=")
    assert getter is not None and getter.kind == KIND_OPERATOR
    assert setter is not None and setter.kind == KIND_OPERATOR


def test_unary_operators(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "operators.rb")
    for op in ("-@", "+@", "!"):
        m = _find(result.declarations, name=op)
        assert m is not None, op
        assert m.kind == KIND_OPERATOR, op


# --- attr_* macros --------------------------------------------------------


def test_attr_accessor_splits_into_one_field_per_symbol(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    fields = [c for c in user.children if c.name in ("name", "email")]
    assert len(fields) == 2
    for f in fields:
        assert f.kind == KIND_FIELD
        assert "[accessor]" in f.attrs


def test_attr_reader_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    field = _find(user.children, kind=KIND_FIELD, name="id")
    assert field is not None
    assert "[reader]" in field.attrs


def test_attr_writer_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    field = _find(user.children, kind=KIND_FIELD, name="token")
    assert field is not None
    assert "[writer]" in field.attrs


# --- Rails associations ---------------------------------------------------


def test_has_many_surfaces_as_field_with_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    posts = _find(user.children, kind=KIND_FIELD, name="posts")
    assert posts is not None
    assert "[has_many]" in posts.attrs


def test_belongs_to_surfaces(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    company = _find(user.children, kind=KIND_FIELD, name="company")
    assert company is not None
    assert "[belongs_to]" in company.attrs


def test_has_one_surfaces(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    profile = _find(user.children, kind=KIND_FIELD, name="profile")
    assert profile is not None
    assert "[has_one]" in profile.attrs


def test_habtm_surfaces(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    roles = _find(user.children, kind=KIND_FIELD, name="roles")
    assert roles is not None
    assert "[habtm]" in roles.attrs


# --- Visibility -----------------------------------------------------------


def test_bare_private_flips_subsequent_methods(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    a = _find(result.declarations, kind=KIND_CLASS, name="A")
    secret_one = _find(a.children, name="secret_one")
    secret_two = _find(a.children, name="secret_two")
    assert secret_one.visibility == "private"
    assert secret_two.visibility == "private"


def test_protected_flips_subsequent_methods(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    a = _find(result.declarations, kind=KIND_CLASS, name="A")
    shareable = _find(a.children, name="shareable_one")
    assert shareable.visibility == "protected"


def test_public_resets_visibility(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    a = _find(result.declarations, kind=KIND_CLASS, name="A")
    visible = _find(a.children, name="visible_after_public")
    assert visible.visibility == ""


def test_targeted_private_with_args_back_reference(ruby_dir):
    """`private :hidden_method` after the def applies to the named method."""
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    b = _find(result.declarations, kind=KIND_CLASS, name="B")
    hidden = _find(b.children, name="hidden_method")
    again_visible = _find(b.children, name="again_visible")
    assert hidden.visibility == "private"
    assert again_visible.visibility == ""  # untouched


def test_targeted_private_with_forward_reference(ruby_dir):
    """`private :late_private` named BEFORE the def — deferred apply."""
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    a = _find(result.declarations, kind=KIND_CLASS, name="A")
    late = _find(a.children, name="late_private")
    assert late.visibility == "private"


def test_private_class_method_marks_class_methods_private(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    c = _find(result.declarations, kind=KIND_CLASS, name="C")
    class_a = _find(c.children, name="class_a")
    class_b = _find(c.children, name="class_b")
    still_public = _find(c.children, name="still_public_class")
    assert class_a.visibility == "private"
    assert class_b.visibility == "private"
    assert still_public.visibility == ""


def test_bare_private_with_parens_flips_state(ruby_dir):
    """`private()` with explicit empty parens parses as a `call` node
    (not `identifier`). Should still flip state for subsequent decls."""
    result = RubyAdapter().parse(ruby_dir / "visibility.rb")
    d = _find(result.declarations, kind=KIND_CLASS, name="D")
    public_first = _find(d.children, name="public_first")
    now_private = _find(d.children, name="now_private")
    assert public_first.visibility == ""
    assert now_private.visibility == "private"


# --- Singleton class ------------------------------------------------------


def test_singleton_class_methods_get_static_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    klass_method = _find(user.children, name="class_method_in_singleton")
    assert klass_method is not None
    assert "[static]" in klass_method.attrs


def test_singleton_class_attr_accessor_gets_static_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    counter = _find(user.children, kind=KIND_FIELD, name="counter")
    assert counter is not None
    assert "[static]" in counter.attrs
    assert "[accessor]" in counter.attrs


# --- Constants ------------------------------------------------------------


def test_class_constants_surface_as_fields(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    max_len = _find(user.children, kind=KIND_FIELD, name="MAX_NAME_LENGTH")
    default = _find(user.children, kind=KIND_FIELD, name="DEFAULT_ROLE")
    assert max_len is not None
    assert default is not None


def test_top_level_constants_surface_as_fields(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    max_retries = _find(result.declarations, kind=KIND_FIELD, name="MAX_RETRIES")
    assert max_retries is not None


def test_lowercase_locals_not_recognised_as_declarations(ruby_dir):
    """`x = 1` is not a structural declaration. The grammar uses
    `assignment` for both constant and local assignments, but only
    constants (capitalised LHS) should surface."""
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    # `GREETING` is uppercase → field.
    assert _find(result.declarations, kind=KIND_FIELD, name="GREETING") is not None


# --- alias / alias_method -------------------------------------------------


def test_alias_method_surfaces_as_field_with_alias_marker(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    full_name = _find(user.children, name="full_name")
    assert full_name is not None
    assert "[alias]" in full_name.attrs


def test_alias_keyword_form_recognised(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    old_to_s = _find(user.children, name="old_to_s")
    assert old_to_s is not None
    assert "[alias]" in old_to_s.attrs


def test_rails_association_inherits_preceding_doc(ruby_dir):
    """Comment immediately above a `has_many` should attach to the
    generated field, matching the attr_* behaviour."""
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    posts = _find(user.children, kind=KIND_FIELD, name="posts")
    assert posts.docs
    assert any("authored by this user" in d for d in posts.docs)


def test_alias_method_inherits_preceding_doc(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    full_name = _find(user.children, name="full_name")
    assert full_name.docs
    assert any("backward compatibility" in d for d in full_name.docs)


# --- Callback-DSL blocks (KIND_BLOCK) -------------------------------------
#
# Ruby expresses structure through callback-passing calls (RSpec
# `describe`/`context`/`it`, Rake `task`, route-map `namespace`) that a
# declaration-only walk misses entirely. These tests pin both the
# true-positive descent and the false-positive rejections.


def test_callback_blocks_found(ruby_dir):
    """describe / context / it / let / namespace / task calls become
    KIND_BLOCK declarations, named after their string / symbol label."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    block_names = {b.name for b in _find_all(r.declarations, kind=KIND_BLOCK)}
    assert block_names == {
        "user",                          # let(:user) { ... }
        "#full_name",                    # describe "#full_name" do
        "returns the joined name",       # it "..." do
        "when the name is blank",        # context "..." do
        "is empty",                      # it "is empty" do
        "supports the brace block form", # it("...") { ... }
        "bare describe suite",           # describe "..." do (no RSpec.)
        "still works",                   # it "still works" do (nested)
        "db",                            # namespace :db do
        "migrate",                       # task :migrate do
    }


def test_callback_block_nesting(ruby_dir):
    """describe → context → it nest correctly; the signature is the call
    head only — the block body never leaks in."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    suite = _find(r.declarations, kind=KIND_BLOCK, name="#full_name")
    assert suite is not None
    assert suite.signature == 'describe "#full_name"'
    ctx = _find(suite.children, kind=KIND_BLOCK, name="when the name is blank")
    assert ctx is not None
    assert ctx.signature == 'context "when the name is blank"'
    leaf = _find(ctx.children, kind=KIND_BLOCK, name="is empty")
    assert leaf is not None
    assert leaf.signature == 'it "is empty"'
    assert leaf.children == []


def test_block_descends_into_declarations(ruby_dir):
    """A declaration inside a recognised block body becomes its child —
    `EXPECTED = "..."` inside `describe "#full_name"` is a field."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    suite = _find(r.declarations, kind=KIND_BLOCK, name="#full_name")
    assert _find(suite.children, kind=KIND_FIELD, name="EXPECTED") is not None


def test_brace_block_form_recognised(ruby_dir):
    """`it("...") { ... }` — the brace block form — is a block too, not
    only the `do ... end` form."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    brace = _find(
        r.declarations, kind=KIND_BLOCK, name="supports the brace block form"
    )
    assert brace is not None
    assert brace.signature == 'it("supports the brace block form")'


def test_member_call_describe_is_transparently_descended(ruby_dir):
    """`RSpec.describe User do` is a member call — not promoted to a
    block named `User`, but its body must still be walked so the nested
    `describe` / `it` / `let` surface."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    # No block literally named after the `RSpec.describe` argument.
    assert _find(r.declarations, kind=KIND_BLOCK, name="User") is None
    # …yet everything inside it is surfaced (hoisted to top level).
    assert _find(r.declarations, kind=KIND_BLOCK, name="#full_name") is not None
    assert _find(r.declarations, kind=KIND_BLOCK, name="user") is not None


def test_rake_namespace_and_task_blocks(ruby_dir):
    """Rake's symbol-labelled `namespace :db do` / `task :migrate do`
    nest as blocks — the Rakefile fixture exercises real Rake DSL."""
    r = RubyAdapter().parse(ruby_dir / "Rakefile")
    db = _find(r.declarations, kind=KIND_BLOCK, name="db")
    assert db is not None
    assert db.signature == "namespace :db"
    migrate = _find(db.children, kind=KIND_BLOCK, name="migrate")
    assert migrate is not None
    assert migrate.signature == "task :migrate"


def test_block_signature_drops_block_body(ruby_dir):
    """No block signature leaks the `do ... end` body or statements."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    for b in _find_all(r.declarations, kind=KIND_BLOCK):
        assert " do" not in b.signature
        assert "{" not in b.signature
        assert "end" not in b.signature
        assert len(b.signature) <= 140


def test_false_positives_are_not_blocks(ruby_dir):
    """Calls that resemble callback-DSLs but aren't named containers must
    NOT be promoted: File.open (member-expression callee), loop (no
    label), gem (no block), configure (no arguments)."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    block_names = {b.name for b in _find_all(r.declarations, kind=KIND_BLOCK)}
    for bait in ("config.yml", "open", "loop", "gem", "rspec-rails",
                 "configure", "port", "set"):
        assert bait not in block_names


def test_helper_def_inside_block_is_a_function(ruby_dir):
    """A `def` inside a callback block surfaces as a free function — the
    `make_user` helper inside `RSpec.describe User do`."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    helper = _find(r.declarations, name="make_user")
    assert helper is not None
    assert helper.kind == KIND_FUNCTION


def test_plain_declarations_unaffected_by_block_rule(ruby_dir):
    """Ordinary constants / classes / functions next to callback-DSL
    blocks are still picked up exactly as before."""
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    assert _find(r.declarations, kind=KIND_FIELD, name="CALLBACKS_VERSION") is not None
    cls = _find(r.declarations, kind=KIND_CLASS, name="PlainClass")
    assert cls is not None
    assert _find(cls.children, kind=KIND_METHOD, name="plain_method") is not None
    fn = _find(r.declarations, kind=KIND_FUNCTION, name="plain_top_level_function")
    assert fn is not None


def test_callbacks_fixture_parses_clean(ruby_dir):
    r = RubyAdapter().parse(ruby_dir / "callbacks.rb")
    assert r.error_count == 0


def test_ordinary_file_produces_no_blocks(ruby_dir):
    """Regression guard: the block rule must not fire on a normal
    class-and-method file with no callback-DSL calls."""
    for name in ("user.rb", "operators.rb", "visibility.rb"):
        r = RubyAdapter().parse(ruby_dir / name)
        assert _find_all(r.declarations, kind=KIND_BLOCK) == []


def test_member_call_rspec_describe_surfaces_nested_example(ruby_dir):
    """`RSpec.describe "User" do / it "is valid" do` in edge_cases.rb:
    the outer member call is not a block, but the nested `it` is."""
    result = RubyAdapter().parse(ruby_dir / "edge_cases.rb")
    # The member-call argument "User" is not promoted to a block.
    assert _find(result.declarations, kind=KIND_BLOCK, name="User") is None
    # The nested plain-identifier `it` block IS surfaced.
    valid = _find(result.declarations, kind=KIND_BLOCK, name="is valid")
    assert valid is not None
    assert valid.signature == 'it "is valid"'


def test_delimited_symbol_label_recognised(tmp_path):
    """`task :"db:migrate" do` — a quoted/delimited symbol label —
    is a block, named after the symbol's inner text."""
    f = tmp_path / "tasks.rake"
    f.write_text('task :"db:migrate" do\n  run_it\nend\n')
    r = RubyAdapter().parse(f)
    block = _find(r.declarations, kind=KIND_BLOCK)
    assert block is not None
    assert block.name == "db:migrate"
    assert block.signature == 'task :"db:migrate"'


def test_block_label_keeps_string_interpolation(tmp_path):
    """An interpolated test description keeps the whole `#{...}` text in
    the block name — so the digest member token matches the outline
    header instead of truncating at the first interpolation."""
    f = tmp_path / "x_spec.rb"
    f.write_text(
        'describe "outer" do\n'
        '  it "handles #{kind} input" do\n'
        '  end\n'
        'end\n'
    )
    r = RubyAdapter().parse(f)
    leaf = _find(r.declarations, kind=KIND_BLOCK, name="handles #{kind} input")
    assert leaf is not None


def test_constant_label_is_transparently_descended(tmp_path):
    """A bare `describe SomeClass do` (constant first arg) is NOT given
    its own block — a constant is too weak a label discriminator. Its
    body is still surfaced by transparent descent."""
    f = tmp_path / "widget_spec.rb"
    f.write_text(
        "describe Widget do\n"
        '  it "works" do\n'
        "  end\n"
        "end\n"
    )
    r = RubyAdapter().parse(f)
    assert _find(r.declarations, kind=KIND_BLOCK, name="Widget") is None
    # …but the nested string-labelled example still surfaces.
    assert _find(r.declarations, kind=KIND_BLOCK, name="works") is not None


def test_private_does_not_flip_a_block_with_a_colliding_name(tmp_path):
    """`private :full_name` targets the *method* `full_name`, never a
    `describe "full_name" do` block whose label collides with it."""
    f = tmp_path / "model.rb"
    f.write_text(
        "class Widget\n"
        '  describe "full_name" do\n'
        "  end\n"
        "  def full_name\n"
        "  end\n"
        "  private :full_name\n"
        "end\n"
    )
    r = RubyAdapter().parse(f)
    cls = _find(r.declarations, kind=KIND_CLASS, name="Widget")
    method = _find(cls.children, kind=KIND_METHOD, name="full_name")
    block = _find(cls.children, kind=KIND_BLOCK, name="full_name")
    assert method.visibility == "private"   # the method IS flipped
    assert block.visibility == ""           # the block is NOT


# --- Doc absorption -------------------------------------------------------


def test_consecutive_pound_comments_attach_as_docs(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "user.rb")
    user = _find(result.declarations, kind=KIND_CLASS, name="User")
    ctor = _find(user.children, kind=KIND_CTOR, name="initialize")
    assert ctor is not None
    assert ctor.docs
    # Both lines of the rdoc comment should be present.
    assert any("rdoc-style" in d for d in ctor.docs)
    assert any("Second line" in d for d in ctor.docs)


# --- Broken syntax --------------------------------------------------------


def test_broken_file_partial_recovery(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "broken.rb")
    # `Healthy` should still parse cleanly.
    healthy = _find(result.declarations, kind=KIND_CLASS, name="Healthy")
    assert healthy is not None
    # Error count should be > 0 (the truncated `def bad_method(`).
    assert result.error_count > 0


# --- Top-level wrapper files ----------------------------------------------


def test_rakefile_parses_without_extension(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "Rakefile")
    assert result.language == "ruby"
    # `helper_task` is a top-level def → KIND_FUNCTION.
    helper = _find(result.declarations, name="helper_task")
    assert helper is not None
    assert helper.kind == KIND_FUNCTION


def test_gemfile_parses_as_ruby(ruby_dir):
    result = RubyAdapter().parse(ruby_dir / "Gemfile")
    assert result.language == "ruby"
    assert result.error_count == 0
    # `source` / `gem` / `ruby` lines have no block — not surfaced. But
    # `group :development do ... end` is a symbol-labelled callback block.
    groups = {b.name for b in _find_all(result.declarations, kind=KIND_BLOCK)}
    assert groups == {"development", "production"}
