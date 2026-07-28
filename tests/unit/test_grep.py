"""Tests for the experimental ast-outline grep command.

Covers the `ast_outline.grep` module — substring/regex search with
scope discovery and kind classification. Marked experimental in the
CLI; tests pin current behavior so future changes are intentional.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ast_outline.grep import (
    KIND_CALL,
    KIND_COMMENT,
    KIND_DEF,
    KIND_IMPORT,
    KIND_REF,
    KIND_STRING,
    grep,
    render_grep,
    strip_definition_keyword,
    suggest_similar_symbols,
)


# --- helpers --------------------------------------------------------------


def _kinds(file_results) -> list[str]:
    return [m.kind for fr in file_results for m in fr.matches]


def _scopes(file_results) -> list[list[str]]:
    return [
        [d.name for d in m.enclosing_path]
        for fr in file_results
        for m in fr.matches
    ]


# --- substring search & kind classification ------------------------------


def test_grep_finds_definition_and_call(tmp_path: Path) -> None:
    """A definition match annotates with [def]; a call match with [call]."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
    )
    results = grep("save", [src]).files
    assert len(results) == 1
    kinds = _kinds(results)
    assert KIND_DEF in kinds
    assert KIND_CALL in kinds


def test_grep_distinguishes_call_from_ref(tmp_path: Path) -> None:
    """Call requires `(` after the match; otherwise it's a ref."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    handler = save\n"      # ref — no parentheses
        "    save()\n"               # call
        "\n"
        "def save():\n"              # def
        "    pass\n"
    )
    results = grep("save", [src]).files
    kinds = _kinds(results)
    assert KIND_REF in kinds
    assert KIND_CALL in kinds
    assert KIND_DEF in kinds


def test_grep_shows_string_literals_by_default(tmp_path: Path) -> None:
    """Matches inside string literals are returned, tagged ``string``.

    Strings are program data — config/translation keys, asset paths,
    reflection targets — so a hit there is a real answer. Hiding it
    produced the worst agent failure mode: a silent false "not used".
    Only comments are noise-filtered by default.
    """
    src = tmp_path / "mod.py"
    src.write_text(
        'def use():\n'
        '    label = "save"\n'       # visible — tagged string
        '    save()\n'                # call — visible
        '\n'
        'def save():\n'
        '    pass\n'
    )
    results = grep("save", [src]).files
    assert len(results) == 1
    fr = results[0]
    assert fr.filtered_count == 0
    visible_kinds = [m.kind for m in fr.matches]
    assert KIND_STRING in visible_kinds
    assert KIND_CALL in visible_kinds
    assert KIND_DEF in visible_kinds


def test_grep_finds_translation_keys_in_string_literals(tmp_path: Path) -> None:
    """The motivating case for the strings-visible default: locale/UI
    content lives in string literals, and identifier-like patterns
    (`embertide`) or non-ASCII ones (`День`) must find it. Under the
    old strings-hidden default these searches returned a false
    "no matches" and agents fled to raw grep.
    """
    src = tmp_path / "locale.ts"
    src.write_text(
        'export const ru = {\n'
        '  fire: "Раздув углей",\n'
        '  embertide: "Embertide event",\n'
        '  day: "День 1",\n'
        '};\n',
        # Explicit: the default is the locale encoding, which is cp1252
        # on Windows and cannot represent this fixture.
        encoding="utf-8",
    )
    # Values inside string literals — the previously-hidden class.
    for pattern in ("День", "Раздув углей", "Embertide event"):
        results = grep(pattern, [src]).files
        assert results, f"pattern {pattern!r} must match"
        assert any(m.kind == KIND_STRING for m in results[0].matches), pattern
    # The identifier-like key still matches as code, not as string.
    results = grep("embertide", [src]).files
    assert results and results[0].matches


def test_grep_filters_comments_by_default(tmp_path: Path) -> None:
    """Matches inside comments are filtered, counted, not returned."""
    src = tmp_path / "mod.py"
    src.write_text(
        "# call save here\n"
        "def caller():\n"
        "    x = 1  # save matters\n"
        "    save()\n"
        "\n"
        "def save():\n"
        "    pass\n"
    )
    results = grep("save", [src]).files
    fr = results[0]
    # 2 comments filtered (one whole-line, one trailing).
    assert fr.filtered_count == 2
    visible_kinds = [m.kind for m in fr.matches]
    assert KIND_COMMENT not in visible_kinds


def test_grep_include_noise_surfaces_filtered(tmp_path: Path) -> None:
    """--include-noise returns the comment/string matches with their kinds."""
    src = tmp_path / "mod.py"
    src.write_text(
        '# uses save\n'
        'def caller():\n'
        '    label = "save"\n'
        '    save()\n'
    )
    results = grep("save", [src], include_noise=True).files
    fr = results[0]
    assert fr.filtered_count == 0
    kinds = _kinds(results)
    assert KIND_COMMENT in kinds
    assert KIND_STRING in kinds
    assert KIND_CALL in kinds


def test_grep_classifies_import_lines(tmp_path: Path) -> None:
    """Lines starting with `from`/`import` get [import] kind."""
    src = tmp_path / "mod.py"
    src.write_text(
        "from .models import User\n"
        "import User as U\n"          # not real code but tests prefix
        "\n"
        "def use():\n"
        "    User()\n"
    )
    results = grep("User", [src]).files
    kinds = _kinds(results)
    assert kinds.count(KIND_IMPORT) == 2
    assert KIND_CALL in kinds


# --- enclosing scope discovery -------------------------------------------


def test_grep_discovers_class_method_scope(tmp_path: Path) -> None:
    """Match inside a method body reports [class, method] as enclosing scope."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class UserHandler:\n"
        "    def update(self, u):\n"
        "        u.save()\n"
    )
    results = grep("save", [src]).files
    scopes = _scopes(results)
    assert scopes == [["UserHandler", "update"]]


def test_grep_top_level_match_has_empty_scope(tmp_path: Path) -> None:
    """Module-level statements report no enclosing scope."""
    src = tmp_path / "mod.py"
    src.write_text(
        "from .models import User\n"
        "\n"
        "MAX = 10\n"
    )
    results = grep("User", [src]).files
    scopes = _scopes(results)
    assert scopes == [[]]


def test_grep_callback_block_in_scope_chain(tmp_path: Path) -> None:
    """A match inside a `describe`/`it` callback reports the block labels
    as its enclosing scope — not the `describe`/`it` keyword. Callback-DSL
    blocks (issue #3) flow into grep's scope walk like any declaration."""
    src = tmp_path / "x.spec.ts"
    src.write_text(
        "describe('outer suite', () => {\n"
        "  it('does the thing', () => {\n"
        "    doSomething()\n"
        "  })\n"
        "})\n"
    )
    results = grep("doSomething", [src]).files
    scopes = _scopes(results)
    assert scopes == [["outer suite", "does the thing"]]


def test_grep_block_label_is_a_definition(tmp_path: Path) -> None:
    """Grepping a test description matches the block's name token, so it
    classifies as [def]. Before callback blocks existed the same text was
    string-literal noise, hidden by default."""
    src = tmp_path / "x.spec.ts"
    src.write_text(
        "describe('user login flow', () => {\n"
        "  it('rejects bad password', () => {\n"
        "    check()\n"
        "  })\n"
        "})\n"
    )
    results = grep("login flow", [src]).files
    assert _kinds(results) == [KIND_DEF]


def test_grep_ruby_callback_block_in_scope_chain(tmp_path: Path) -> None:
    """A match inside an RSpec `describe`/`it` block reports the block
    labels as its enclosing scope — the Ruby callback-DSL blocks flow
    into grep's scope walk like any declaration."""
    src = tmp_path / "user_spec.rb"
    src.write_text(
        'RSpec.describe User do\n'
        '  describe "#full_name" do\n'
        '    it "joins the parts" do\n'
        '      doSomething\n'
        '    end\n'
        '  end\n'
        'end\n'
    )
    results = grep("doSomething", [src]).files
    scopes = _scopes(results)
    assert scopes == [["#full_name", "joins the parts"]]


def test_grep_ruby_block_label_is_a_definition(tmp_path: Path) -> None:
    """Grepping a Ruby test description matches the block's name token,
    so it classifies as [def] rather than string-literal noise."""
    src = tmp_path / "user_spec.rb"
    src.write_text(
        'describe "user login flow" do\n'
        '  it "rejects bad password" do\n'
        '    check\n'
        '  end\n'
        'end\n'
    )
    results = grep("login flow", [src]).files
    assert _kinds(results) == [KIND_DEF]


# --- def-detection precision ----------------------------------------------


def test_grep_does_not_call_typeref_a_def(tmp_path: Path) -> None:
    """A type used in a function signature is [ref], not [def] of that function."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class Handler:\n"
        "    pass\n"
        "\n"
        "def run(h: Handler) -> None:\n"
        "    pass\n"
    )
    results = grep("Handler", [src]).files
    kinds = _kinds(results)
    # One [def] for the class itself, one [ref] for the type annotation.
    assert kinds.count(KIND_DEF) == 1
    assert kinds.count(KIND_REF) == 1


def test_grep_substring_inside_name_still_def(tmp_path: Path) -> None:
    """A search for `save` matches the def line of `save`, not `save_user`."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save_user():\n"
        "    pass\n"
        "\n"
        "def save():\n"               # only this should match as [def]
        "    pass\n"
    )
    # Use exact pattern to constrain. Both contain `save` substring; here
    # we test that the match column inside `save_user` is recognized as
    # the DEF of `save_user` (since it falls in its name token).
    results = grep("save", [src]).files
    kinds = _kinds(results)
    # Both definitions named with `save` in their identifier — both [def].
    assert kinds.count(KIND_DEF) == 2


# --- decorated / attributed definitions ----------------------------------
#
# A decorated declaration extends its `start_line` up over the decorator
# lines (so `show` prints them), which drops the name token onto a lower
# line than `start_line`. The def-classifier must tag [def] on the name
# line, not `start_line` — otherwise `grep <sym> --kind def` (and the
# dir-mode `show` built on it) silently miss real definitions.


def _def_lines(file_results) -> list[int]:
    return [m.line for fr in file_results for m in fr.matches if m.kind == KIND_DEF]


def test_grep_decorated_class_is_def(tmp_path: Path) -> None:
    """A decorator above a class doesn't hide the class from `--kind def`."""
    src = tmp_path / "mod.py"
    src.write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class ModelEntry:\n"          # L4 — the name line
        "    name: str\n"
    )
    results = grep("ModelEntry", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(results) == [4]


def test_grep_decorated_function_is_def(tmp_path: Path) -> None:
    """A stacked decorator above a function keeps it a [def]."""
    src = tmp_path / "mod.py"
    src.write_text(
        "@app.command(name='start')\n"
        "@wraps(fn)\n"
        "def llamacpp_start(port):\n"  # L3 — the name line
        "    pass\n"
    )
    results = grep("llamacpp_start", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(results) == [3]


def test_grep_decorator_name_collision_not_a_def(tmp_path: Path) -> None:
    """`@bar` above `def bar` must NOT tag the decorator line as the def.

    The def belongs to the `def bar` line; the `@bar` usage is a plain
    reference to the decorator, not a definition. Regression guard for the
    name-collision hole in the name-line fix.
    """
    src = tmp_path / "mod.py"
    src.write_text(
        "def bar(fn):\n"               # L1 — undecorated helper def
        "    return fn\n"
        "\n"
        "@bar\n"                        # L4 — decorator use, NOT a def
        "def bar(self):\n"             # L5 — the real decorated def
        "    pass\n"
    )
    results = grep("bar", [src], kind_filter={KIND_DEF}).files
    # Two defs: the helper (L1) and the decorated method (L5). NOT L4.
    assert sorted(_def_lines(results)) == [1, 5]


def test_grep_attributed_csharp_class_is_def(tmp_path: Path) -> None:
    src = tmp_path / "T.cs"
    src.write_text(
        "[Serializable]\n"
        "public class Widget {\n"      # L2 — the name line
        "    [Obsolete]\n"
        "    public void Go() {}\n"     # L4 — the name line
        "}\n"
    )
    cls = grep("Widget", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    meth = grep("Go", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(meth) == [4]


def test_grep_annotated_java_class_is_def(tmp_path: Path) -> None:
    src = tmp_path / "T.java"
    src.write_text(
        "@Deprecated\n"
        "public class Anno {\n"        # L2 — the name line
        "    @Override\n"
        "    public String go() { return \"\"; }\n"  # L4
        "}\n"
    )
    cls = grep("Anno", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    meth = grep("go", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(meth) == [4]


def test_grep_decorated_ts_class_is_def(tmp_path: Path) -> None:
    src = tmp_path / "app.ts"
    src.write_text(
        "@Component({selector: 'app'})\n"
        "export class DecoratedTs {\n"  # L2 — the name line
        "    name: string;\n"
        "}\n"
    )
    results = grep("DecoratedTs", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(results) == [2]


def test_grep_annotated_kotlin_class_and_fun_are_def(tmp_path: Path) -> None:
    src = tmp_path / "t.kt"
    src.write_text(
        "@Serializable\n"
        "class KtAnno(val x: Int)\n"    # L2 — the name line
        "\n"
        "@Deprecated(\"no\")\n"
        "fun ktFn(): Int = 1\n"          # L5
    )
    cls = grep("KtAnno", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    fn = grep("ktFn", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(fn) == [5]


def test_grep_attributed_php_class_and_method_are_def(tmp_path: Path) -> None:
    src = tmp_path / "t.php"
    src.write_text(
        "<?php\n"
        "#[Attribute]\n"
        "class PhpAttr {\n"             # L3
        "    #[Deprecated]\n"
        "    public function go(): void {}\n"  # L5
        "}\n"
    )
    cls = grep("PhpAttr", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [3]
    meth = grep("go", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(meth) == [5]


def test_grep_attributed_swift_class_and_func_are_def(tmp_path: Path) -> None:
    src = tmp_path / "t.swift"
    src.write_text(
        "@objc\n"
        "class SwiftAttr {\n"           # L2
        "    @available(iOS 13, *)\n"
        "    func doThing() {}\n"        # L4
        "}\n"
    )
    cls = grep("SwiftAttr", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    fn = grep("doThing", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(fn) == [4]


def test_grep_annotated_scala_class_and_def_are_def(tmp_path: Path) -> None:
    src = tmp_path / "t.scala"
    src.write_text(
        "@SerialVersionUID(1)\n"
        "class ScalaAnno {\n"           # L2
        "  @deprecated(\"x\", \"1.0\")\n"
        "  def scalaFn(): Int = 1\n"     # L4
        "}\n"
    )
    cls = grep("ScalaAnno", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    fn = grep("scalaFn", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(fn) == [4]


def test_grep_attributed_cpp_function_is_def(tmp_path: Path) -> None:
    """A `[[attr]]` on its own line above a C++ function keeps it a [def].

    Inline attributes (`struct [[nodiscard]] Foo`) never diverged; the
    own-line leading attribute is the case that pushed `start_line` above
    the name.
    """
    src = tmp_path / "t.cpp"
    src.write_text(
        "[[deprecated]]\n"
        "int cppFn() { return 1; }\n"   # L2
        "\n"
        "struct [[nodiscard]] CppStruct {\n"  # L4 — inline attr, no divergence
        "    int x;\n"
        "};\n"
    )
    fn = grep("cppFn", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(fn) == [2]
    st = grep("CppStruct", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(st) == [4]


def test_grep_annotated_gdscript_defs_are_def(tmp_path: Path) -> None:
    """GDScript `@tool` / `@export` / `@rpc` above a declaration keep it [def].

    GDScript uses a hand-written parser; annotations on their own line
    pull `start_line` up the same way tree-sitter preambles do.
    """
    src = tmp_path / "t.gd"
    src.write_text(
        "@tool\n"
        "class_name MyTool\n"           # L2
        "extends Node\n"
        "\n"
        "@export\n"
        "var speed := 1.0\n"            # L6
        "\n"
        "@rpc(\"any_peer\")\n"
        "func do_rpc():\n"              # L9
        "\tpass\n"
    )
    cls = grep("MyTool", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(cls) == [2]
    var = grep("speed", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(var) == [6]
    fn = grep("do_rpc", [src], kind_filter={KIND_DEF}).files
    assert _def_lines(fn) == [9]


# --- regex and case-insensitive ------------------------------------------


def test_grep_regex_mode(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text(
        "def save_user():\n"
        "    pass\n"
        "\n"
        "def save_admin():\n"
        "    pass\n"
    )
    results = grep(r"save_\w+", [src], is_regex=True).files
    assert len(results) == 1
    assert len(results[0].matches) == 2
    assert all(m.kind == KIND_DEF for m in results[0].matches)


def test_grep_case_insensitive_literal(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text(
        "USER = 1\n"
        "user = 2\n"
        "User = 3\n"
    )
    results = grep("user", [src], case_insensitive=True).files
    assert len(results[0].matches) == 3


# --- rendering ------------------------------------------------------------


def test_render_grep_shows_imports_section(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text(
        "from .models import User\n"
        "\n"
        "def use():\n"
        "    User()\n"
    )
    results = grep("User", [src]).files
    output = render_grep(results)
    assert "## imports" in output
    assert "## matches" in output
    assert "[import]" in output
    # [call] is intentionally NOT in output — call/ref tags were
    # dropped because agents trivially infer them from line shape
    # (identifier + `(` = call). The match line for `User()` carries
    # only `> L<n>: User()` with no trailing tag.
    assert "User()" in output
    assert "[call]" not in output


def test_render_grep_omits_imports_section_when_empty(tmp_path: Path) -> None:
    """No `## imports` header if the only matches are inside the code body."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    save()\n"
        "\n"
        "def save():\n"
        "    pass\n"
    )
    results = grep("save", [src]).files
    output = render_grep(results)
    assert "## imports" not in output
    assert "## matches" in output


def test_render_grep_includes_match_count_in_header(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    save()\n"
        "    save()\n"
    )
    results = grep("save", [src]).files
    output = render_grep(results)
    assert "(3 matches)" in output


def test_render_grep_filtered_footer_only_when_no_visible(tmp_path: Path) -> None:
    """The 'in comments hidden' footer renders only when the
    file has NO visible matches — there the header reads '(0 matches)' and,
    without the footer, the agent would wrongly conclude the symbol is
    absent. When visible matches exist the footer is suppressed as noise;
    the count still rides the result's ``filtered_count`` (→ JSON)."""
    # Visible match present (def save) + a comment match → footer suppressed.
    with_visible = tmp_path / "with_visible.py"
    with_visible.write_text(
        '# save here\n'
        'def save():\n'
        '    pass\n'
    )
    results = grep("save", [with_visible]).files
    out = render_grep(results)
    assert "in comments hidden" not in out
    assert results[0].filtered_count == 1   # still tracked for JSON

    # Only a comment match, no visible match → footer present (the guard).
    comment_only = tmp_path / "comment_only.py"
    comment_only.write_text(
        'def other():\n'
        '    pass  # save mentioned only here\n'
    )
    results2 = grep("save", [comment_only]).files
    out2 = render_grep(results2)
    assert "1 match in comments hidden" in out2
    assert "--include-noise" in out2


def test_render_grep_arrow_marker_on_match_lines(tmp_path: Path) -> None:
    """Match lines inside scope use the `> L<n>` marker."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class C:\n"
        "    def m(self):\n"
        "        target()\n"
        "\n"
        "def target():\n"
        "    pass\n"
    )
    results = grep("target", [src]).files
    output = render_grep(results)
    assert "> L3: target()" in output


# --- typescript adapter (cross-language smoke) ----------------------------


def test_grep_typescript_call_classification(tmp_path: Path) -> None:
    src = tmp_path / "app.ts"
    src.write_text(
        "import { User } from './models';\n"
        "\n"
        "function use(u: User): void {\n"
        "  u.save();\n"
        "}\n"
    )
    results = grep("User", [src]).files
    kinds = _kinds(results)
    assert KIND_IMPORT in kinds
    assert KIND_REF in kinds


# --- generic call / turbofish / optional chain ---------------------------


def test_grep_generic_call_classified_as_call(tmp_path: Path) -> None:
    """``foo<T>()`` should be [call], not [ref] — was a real v1 bug."""
    src = tmp_path / "app.ts"
    src.write_text(
        "function genericCall<T>(): T {\n"
        "  return null as T;\n"
        "}\n"
        "\n"
        "function caller() {\n"
        "  return genericCall<string>();\n"
        "}\n"
    )
    results = grep("genericCall", [src]).files
    kinds = _kinds(results)
    # Definition + call — both should be present, not two refs.
    assert kinds.count(KIND_DEF) == 1
    assert kinds.count(KIND_CALL) == 1


def test_grep_optional_chain_call_classified_as_call(tmp_path: Path) -> None:
    """``foo?.()`` (TypeScript optional chain call) is a call."""
    src = tmp_path / "app.ts"
    src.write_text(
        "function maybeRun(fn?: () => void) {\n"
        "  fn?.();\n"                  # optional chain call — should be [call]
        "}\n"
    )
    results = grep("fn", [src]).files
    kinds = _kinds(results)
    assert KIND_CALL in kinds


def test_grep_non_null_assertion_call(tmp_path: Path) -> None:
    """``foo!()`` (TS non-null assertion + call) is a call."""
    src = tmp_path / "app.ts"
    src.write_text(
        "function maybeRun(fn: (() => void) | null) {\n"
        "  fn!();\n"                   # non-null assertion call — [call]
        "}\n"
    )
    results = grep("fn", [src]).files
    kinds = _kinds(results)
    assert KIND_CALL in kinds


def test_grep_indexed_array_is_ref_not_call(tmp_path: Path) -> None:
    """``[fn]`` (array literal) should NOT be classified as a call."""
    src = tmp_path / "app.ts"
    src.write_text(
        "const fns = [genericCall];\n"     # plain ref inside array literal
    )
    results = grep("genericCall", [src]).files
    kinds = _kinds(results)
    assert KIND_REF in kinds
    assert KIND_CALL not in kinds


def test_next_call_paren_after_skips_leading_angle_closer() -> None:
    """Walker must skip a leading ``>`` left over from a match that
    consumed the opener — e.g. regex ``Bind.*SaveSystem`` matches
    ``Bind<SaveSystem`` greedily, ending on ``>`` with no matching
    ``<`` to skip via the existing balanced-block branch. Without
    this, every generic call surfaced by a regex like ``Foo.*Bar``
    classifies as ``ref`` and ``--kind call`` returns 0."""
    from ast_outline.grep import _next_call_paren_after
    # ``Bind<SaveSystem>();`` — cursor starts on ``>``, ``(`` follows.
    assert _next_call_paren_after(">();", 0) is True
    # Nested generic closure ``Foo<Bar<Baz>>()`` — cursor on first ``>``,
    # then second ``>``, then ``(``. The walker must chain closer-skips.
    assert _next_call_paren_after(">>()", 0) is True
    # Stray ``>`` not followed by ``(`` — must still return False so
    # comparisons like ``a > b`` (matching ``a``) don't all classify
    # as call.
    assert _next_call_paren_after("> something", 0) is False


def test_next_call_paren_after_skips_leading_square_closer() -> None:
    """Same closer-skip for ``]`` — covers Go 1.18+ generics ``foo[T]()``,
    Scala type-args ``Map[K, V]()``, and any regex match that ended
    inside an indexing / type-arg block."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("]()", 0) is True
    # ``arr[i]`` (index, no call): cursor on ``]``, nothing follows.
    assert _next_call_paren_after("];", 0) is False


def test_next_call_paren_after_skips_rest_of_identifier() -> None:
    """Walker must skip the tail of an identifier when the cursor lands
    mid-word — the common case is regex alternation ``foo|fooBar``
    against ``fooBar(x)``: Python ``re`` picks the leftmost alternative
    ``foo``, the match ends on ``B`` (inside ``Bar``). Without skipping
    the hidden tail the walker sees an identifier char, returns False,
    and the call classifies as ``ref``. Then ``--kind call`` excludes
    the only structural hit and the agent falls through to ``rg``."""
    from ast_outline.grep import _next_call_paren_after
    # Cursor in middle of ``fooBar(x)`` (on ``B``, position 3).
    # Skip ``Bar``, hit ``(`` → True.
    assert _next_call_paren_after("fooBar(x)", 3) is True
    # Same shape with underscore identifiers (Python / Rust style).
    # Cursor on ``_`` of ``do_stuff_now`` after match ``do_stuff``.
    assert _next_call_paren_after("do_stuff_now(x)", 8) is True
    # Mid-identifier but NOT a call — must still return False so the
    # bias toward ``call`` doesn't leak into truly non-call lines.
    # ``fooBar = 1`` with cursor on ``B``: skip ``Bar``, find space, ``=``.
    assert _next_call_paren_after("fooBar = 1", 3) is False
    # Mid-identifier followed by member access ``.method()`` — the
    # identifier itself isn't called (``.`` isn't skipped by the walker).
    # ``fooBar.method()`` with cursor on ``B``: skip ``Bar``, find ``.``.
    assert _next_call_paren_after("fooBar.method()", 3) is False


def test_next_call_paren_after_identifier_skip_composes_with_generics() -> None:
    """Identifier skip + generic-block balance compose. ``fooBar<T>()``
    with the match ending mid-identifier on ``B``: walker must skip
    ``Bar``, then balance ``<T>``, then find ``(``."""
    from ast_outline.grep import _next_call_paren_after
    # Cursor on ``B`` (position 3) of ``fooBar<T>()``.
    assert _next_call_paren_after("fooBar<T>()", 3) is True
    # Same with Go 1.18+ / Scala bracket generics ``fooBar[T]()``.
    assert _next_call_paren_after("fooBar[T]()", 3) is True
    # And turbofish ``fooBar::<T>()`` (Rust): skip ``Bar``, skip ``::``,
    # balance ``<T>``, find ``(``.
    assert _next_call_paren_after("fooBar::<T>()", 3) is True


def test_next_call_paren_after_identifier_skip_one_shot_not_recursive() -> None:
    """Identifier skip applies only at entry, not mid-walk. Two cases:

    1. ``foo + bar()`` at position 0: identifier-skip consumes ``foo``,
       then walker hits ` `, `+`, ` `, `b`. ``b`` is now mid-walk, NOT
       entry, so the identifier-skip branch must not fire. Returns
       False — the call ``bar()`` belongs to ``bar``, not to ``foo``.
    2. ``foo<T>bar()`` at position 3 (on ``B`` of generics body — a
       degenerate shape that isn't valid in any supported language,
       but pins composition): identifier-skip eats ``bar``... wait.
       Actually this IS entry, so it skips. Test the OTHER axis —
       ``foo<T>bar()`` at position 0 (on ``f``): identifier-skip
       consumes ``foo`` at entry, then balances ``<T>`` mid-walk,
       lands on ``b`` mid-walk. If the identifier-skip were recursive
       it would consume ``bar`` and find ``(`` → True. With one-shot
       semantics, ``b`` is just an "other significant char" → False.
       This pins that ``bar()`` here isn't attributed to a match on
       ``foo``."""
    from ast_outline.grep import _next_call_paren_after
    assert _next_call_paren_after("foo + bar()", 0) is False
    assert _next_call_paren_after("foo<T>bar()", 0) is False


def test_grep_alternation_short_first_classifies_call_python(
    tmp_path: Path,
) -> None:
    """Regex alternation ``foo|fooBar`` (short first) against a call
    site ``obj.fooBar(x)`` — Python ``re`` picks the leftmost
    alternative ``foo``, match ends inside identifier ``Bar``. Before
    the identifier-skip fix the walker landed on ``B`` → ref → excluded
    by ``--kind call``; the agent fell through to ``rg`` having
    "proved" the call doesn't exist.

    Real-world repro: user grepping ``TryAssembleFragments|TryAssembleFragmentsNear``
    across a Unity C# codebase missed every call site at line 1208 of
    ``ThingDragNDropController.cs`` because ``--kind def,call`` dropped
    the misclassified ``ref``."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    obj.fooBar(x, y)\n"
    )
    results = grep("foo|fooBar", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"short-first alternation hitting a call must classify as call, got {kinds}"
    )


def test_grep_alternation_short_first_classifies_call_typescript(
    tmp_path: Path,
) -> None:
    """Same alternation bug in TypeScript — the classifier is
    language-agnostic; a per-language test pins that no adapter-level
    quirk reintroduces the regression."""
    src = tmp_path / "app.ts"
    src.write_text(
        "function caller() {\n"
        "    obj.fooBar(x, y);\n"
        "}\n"
    )
    results = grep("foo|fooBar", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


def test_grep_alternation_short_first_classifies_call_go(
    tmp_path: Path,
) -> None:
    """Go variant — exported (capitalized) identifiers are the common
    shape for the bug in Go codebases."""
    src = tmp_path / "main.go"
    src.write_text(
        "package main\n"
        "func caller() {\n"
        "    obj.FooBar(x, y)\n"
        "}\n"
    )
    results = grep("Foo|FooBar", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


def test_grep_alternation_short_first_classifies_call_rust(
    tmp_path: Path,
) -> None:
    """Rust variant — snake_case identifiers exercise the underscore
    branch of the identifier-skip class."""
    src = tmp_path / "lib.rs"
    src.write_text(
        "fn caller() {\n"
        "    obj.do_stuff_now(x, y);\n"
        "}\n"
    )
    results = grep("do_stuff|do_stuff_now", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


def test_grep_alternation_short_first_classifies_call_csharp(
    tmp_path: Path,
) -> None:
    """C# variant — the language the original Unity bug report
    surfaced on; PascalCase method names are idiomatic and the
    alternation prefix is a natural construction for an agent."""
    src = tmp_path / "Controller.cs"
    src.write_text(
        "public class Controller {\n"
        "    public void Caller() {\n"
        "        other.TryAssembleFragmentsNear(data, pos);\n"
        "    }\n"
        "}\n"
    )
    results = grep(
        "TryAssembleFragments|TryAssembleFragmentsNear", [src], is_regex=True
    ).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


def test_grep_literal_substring_of_called_identifier_classifies_call(
    tmp_path: Path,
) -> None:
    """Identifier-skip fires for literal substring searches too, not
    only regex alternation. ``bytes.find('foo')`` against ``fooBar(x)``
    returns the same mid-identifier end position as the regex case,
    so the fix flips classification from ``ref`` to ``call`` for
    literal searches by symmetry. Pin this so a future "tighten
    semantics" PR has to make an explicit decision rather than
    silently regress."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    obj.fooBar(x, y)\n"
    )
    results = grep("foo", [src]).files  # literal, no is_regex=True
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"literal substring inside called identifier must classify as call, got {kinds}"
    )


def test_grep_alternation_short_first_non_call_still_ref(
    tmp_path: Path,
) -> None:
    """Negative: when the mid-identifier site genuinely is NOT a call
    (assignment, comparison, member access), the identifier-skip fix
    must not flip it to call. Pins that the bias-toward-call doesn't
    silently widen to non-call lines."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    fooBar = 1\n"             # assignment, not call
        "    x = fooBar + 2\n"         # ref in expression
        "    y = fooBar.method()\n"    # member-access ref, .method() is the call
    )
    results = grep("foo|fooBar", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    # The three matches above are all ref (no direct ``fooBar(`` shape).
    assert KIND_CALL not in kinds, (
        f"non-call sites must not flip to call after identifier-skip, got {kinds}"
    )
    assert KIND_REF in kinds


def test_grep_generic_call_regex_match_ends_on_closer(tmp_path: Path) -> None:
    """Regex ``Bind.*SaveSystem`` against ``c.Bind<SaveSystem>();`` —
    greedy ``.*`` consumes ``<`` and the match ends at ``>``. Before
    the closer-skip fix the walker landed on ``>`` and returned False
    → KIND_REF, so ``--kind call`` excluded the only structural hit
    and the agent fell through to ``rg``. Now the closer is skipped
    and ``(`` is found → KIND_CALL.

    Real-world repro from the v0.8.8 changelog."""
    src = tmp_path / "boot.cs"
    src.write_text(
        "public class Bootstrap {\n"
        "    public void Configure(Container c) {\n"
        "        c.Bind<SaveSystem>();\n"
        "    }\n"
        "}\n"
    )
    results = grep("Bind.*SaveSystem", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"regex match ending on `>` must classify as call, got {kinds}"
    )


def test_grep_generic_call_literal_full_invocation(tmp_path: Path) -> None:
    """Literal ``Bind<SaveSystem>`` — match ends ON ``(`` (just past
    ``>``), which the existing trailing-paren check already handles.
    Pins that the closer-skip change doesn't regress this path."""
    src = tmp_path / "boot.cs"
    src.write_text(
        "public class Bootstrap {\n"
        "    public void Configure(Container c) {\n"
        "        c.Bind<SaveSystem>();\n"
        "    }\n"
        "}\n"
    )
    results = grep("Bind<SaveSystem>", [src]).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


def test_grep_nested_generic_call(tmp_path: Path) -> None:
    """Nested generics ``Foo<Bar<Baz>>()`` — greedy regex landing on the
    outer ``>`` must still classify as call. Exercises the chained
    closer-skip: cursor lands on inner ``>``, walker skips, lands on
    outer ``>``, skips again, hits ``(``."""
    src = tmp_path / "app.ts"
    src.write_text(
        "function genericCall<A, B>(): void {}\n"
        "function caller() {\n"
        "  genericCall<Array<string>, number>();\n"   # nested generics
        "}\n"
    )
    # Greedy `.*` consumes through ``number``; the match ends on the
    # last char of ``number``, leaving the cursor on the inner ``>``
    # (the type-arg list closes with ``>>`` here, but the match ends
    # before either of them — the walker chains both closer-skips).
    results = grep(r"genericCall.*number", [src], is_regex=True).files
    # Match ends at end of ``number``, cursor on ``>``. Closer-skip
    # hops over both ``>`` chars, then finds ``(``.
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, kinds


# --- multi-line string filtering (Python docstrings) ---------------------


def test_grep_python_docstring_matches_tagged_string(tmp_path: Path) -> None:
    """Matches inside a triple-quoted Python docstring surface as ``string``.

    A docstring IS a string literal — it follows the strings-visible
    default. The multi-line region detection is tree-sitter-backed
    (``ParseResult.noise_regions``), so the tag is exact even where the
    single-line quote-counting heuristic can't see.
    """
    src = tmp_path / "mod.py"
    src.write_text(
        'def use():\n'
        '    """\n'
        '    Calls save() to persist.\n'   # visible — tagged string
        '    Mentions save in prose.\n'    # visible — tagged string
        '    """\n'
        '    save()\n'                      # visible — real call
        '\n'
        'def save():\n'
        '    pass\n'
    )
    results = grep("save", [src]).files
    fr = results[0]
    assert fr.filtered_count == 0
    visible_kinds = [m.kind for m in fr.matches]
    assert visible_kinds.count(KIND_STRING) == 2
    assert KIND_CALL in visible_kinds
    assert KIND_DEF in visible_kinds


def test_grep_module_docstring_surfaces_tagged_string(tmp_path: Path) -> None:
    """A module-docstring hit is visible but clearly labeled ``string`` —
    the tag lets the agent discount it as prose without losing it."""
    src = tmp_path / "mod.py"
    src.write_text(
        '"""Module that uses User everywhere."""\n'
        'from .models import User\n'
        '\n'
        'def use():\n'
        '    User()\n'
    )
    results = grep("User", [src]).files
    fr = results[0]
    assert fr.filtered_count == 0
    visible_kinds = [m.kind for m in fr.matches]
    assert KIND_STRING in visible_kinds
    assert KIND_IMPORT in visible_kinds
    assert KIND_CALL in visible_kinds


def test_grep_triple_quote_in_code_doesnt_break_filtering(tmp_path: Path) -> None:
    """Code that mentions ``\"\"\"`` as a literal shouldn't confuse the filter.

    Regex-based pre-scan would pair the string literal containing the
    triple-quote with a later docstring's opening ``\"\"\"`` and
    misclassify regions. Tree-sitter walks correctly because it
    distinguishes string nodes from string contents.
    """
    src = tmp_path / "mod.py"
    src.write_text(
        'TRIPLE = \'"""\'\n'                # code with triple-quote as data
        '\n'
        'def docstring_user():\n'
        '    """Calls save here."""\n'      # filtered docstring
        '    save()\n'                       # visible call
        '\n'
        'def save():\n'
        '    pass\n'
    )
    results = grep("save", [src]).files
    fr = results[0]
    # The docstring hit is visible and exactly tagged ``string`` — the
    # region boundaries (not the visibility) are what this test pins.
    assert fr.filtered_count == 0
    kinds = [m.kind for m in fr.matches]
    assert kinds.count(KIND_STRING) == 1
    assert KIND_CALL in kinds
    assert KIND_DEF in kinds


def test_grep_include_noise_surfaces_docstring_matches(tmp_path: Path) -> None:
    """--include-noise returns docstring matches with [string] kind."""
    src = tmp_path / "mod.py"
    src.write_text(
        'def use():\n'
        '    """Calls save() here."""\n'
        '    pass\n'
    )
    results = grep("save", [src], include_noise=True).files
    fr = results[0]
    assert fr.filtered_count == 0
    kinds = [m.kind for m in fr.matches]
    assert KIND_STRING in kinds


# --- gitignore integration -----------------------------------------------


def test_grep_respects_gitignore(tmp_path: Path) -> None:
    """`.gitignore` is honored unless --no-ignore is passed."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "kept.py").write_text("def save(): pass\n")
    (tmp_path / "ignored.py").write_text("def save(): pass\n")

    results = grep("save", [tmp_path]).files
    paths = [str(fr.path) for fr in results]
    assert any("kept.py" in p for p in paths)
    assert not any("ignored.py" in p for p in paths)

    # --no-ignore disables filtering.
    results_all = grep("save", [tmp_path], no_ignore=True).files
    paths_all = [str(fr.path) for fr in results_all]
    assert any("ignored.py" in p for p in paths_all)


# --- edge cases -----------------------------------------------------------


def test_grep_unsupported_extension_skipped(tmp_path: Path) -> None:
    """Files whose extension no adapter claims are skipped silently."""
    (tmp_path / "data.bin").write_text("save save save\n")
    (tmp_path / "code.py").write_text("def save(): pass\n")
    results = grep("save", [tmp_path]).files
    paths = [str(fr.path) for fr in results]
    assert all("code.py" in p for p in paths)


def test_grep_nonexistent_path_returns_empty(tmp_path: Path) -> None:
    """A missing path is silently skipped (CLI surfaces it via --path-not-found)."""
    results = grep("anything", [tmp_path / "does-not-exist"]).files
    assert results == []


def test_grep_no_matches_returns_empty(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text("def foo(): pass\n")
    results = grep("nonexistent_symbol", [src]).files
    assert results == []


def test_grep_empty_pattern_returns_empty(tmp_path: Path) -> None:
    """Empty pattern would match every byte position via `bytes.find(b'')` —
    explicitly rejected so callers never get the file-flooded surprise."""
    src = tmp_path / "mod.py"
    src.write_text("def foo(): pass\n")
    results = grep("", [src]).files
    assert results == []


# --- multi-pattern search ------------------------------------------------


def test_grep_accepts_list_of_patterns(tmp_path: Path) -> None:
    """``patterns=[a, b]`` finds matches for both, combined into one walk."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def load(): pass\n"
        "def update(): pass\n"
    )
    results = grep(["save", "load"], [src]).files
    fr = results[0]
    # Three lines of code; "save" matches one def, "load" matches one def,
    # and "update" is not in the pattern list — should NOT match.
    assert len(fr.matches) == 2
    kinds = sorted(m.kind for m in fr.matches)
    assert kinds == [KIND_DEF, KIND_DEF]
    contents = sorted(m.line_content.strip() for m in fr.matches)
    assert "def save(): pass" in contents
    assert "def load(): pass" in contents


def test_grep_multi_pattern_preserves_classification(tmp_path: Path) -> None:
    """Each pattern's matches go through the same classification pipeline."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def load(): pass\n"
        "\n"
        "def use():\n"
        "    save()\n"     # call to save
        "    load()\n"     # call to load
        "    other = save\n"  # ref to save
    )
    results = grep(["save", "load"], [src]).files
    kinds = _kinds(results)
    assert kinds.count(KIND_DEF) == 2
    assert kinds.count(KIND_CALL) == 2
    assert kinds.count(KIND_REF) == 1


def test_grep_multi_pattern_string_back_compat(tmp_path: Path) -> None:
    """Single-string ``patterns`` still works (back-compat for old callers)."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    # Both call shapes return the same result.
    results_str = grep("save", [src]).files
    results_list = grep(["save"], [src]).files
    assert len(results_str) == len(results_list) == 1
    assert results_str[0].matches[0].kind == results_list[0].matches[0].kind


def test_grep_multi_pattern_filters_empty_strings(tmp_path: Path) -> None:
    """Empty pattern strings are silently dropped; non-empty ones still run."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    results = grep(["", "save", ""], [src]).files
    assert len(results[0].matches) == 1


def test_grep_multi_pattern_all_empty_returns_empty(tmp_path: Path) -> None:
    """If every pattern is empty/dropped, the call returns no results."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    results = grep(["", "", ""], [src]).files
    assert results == []


def test_grep_multi_pattern_with_regex(tmp_path: Path) -> None:
    """``is_regex=True`` applies to every pattern in the list."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save_user(): pass\n"
        "def load_admin(): pass\n"
        "def other(): pass\n"
    )
    results = grep([r"save_\w+", r"load_\w+"], [src], is_regex=True).files
    fr = results[0]
    assert len(fr.matches) == 2


def test_grep_multi_pattern_with_case_insensitive(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text(
        "USER = 1\n"
        "Admin = 2\n"
        "guest = 3\n"
    )
    results = grep(["user", "admin"], [src], case_insensitive=True).files
    assert len(results[0].matches) == 2


# --- regex auto-detection ------------------------------------------------


def test_looks_like_regex_detects_bre_alternation() -> None:
    """``\\|`` is the BRE alternation form — agents fluent in basic
    grep type ``Magnet\\|Container`` and expect OR-matching."""
    from ast_outline.grep import _looks_like_regex
    assert _looks_like_regex(r"Magnet\|Container")


def test_looks_like_regex_detects_bare_pipe() -> None:
    from ast_outline.grep import _looks_like_regex
    assert _looks_like_regex("save|load")


def test_looks_like_regex_detects_char_classes() -> None:
    from ast_outline.grep import _looks_like_regex
    assert _looks_like_regex(r"v\d+")
    assert _looks_like_regex(r"\w+_test")
    assert _looks_like_regex(r"\bword\b")


def test_looks_like_regex_detects_grouping() -> None:
    from ast_outline.grep import _looks_like_regex
    assert _looks_like_regex(r"(?:save|load)")
    assert _looks_like_regex(r"(?P<name>\w+)")


def test_looks_like_regex_skips_literal_code_constructs() -> None:
    """Literal code constructs that look slightly regex-like must NOT
    auto-promote — Java arrays, qualified names with dots, etc."""
    from ast_outline.grep import _looks_like_regex
    assert not _looks_like_regex("string[]")          # Java array
    assert not _looks_like_regex("User.save")          # qualified name
    assert not _looks_like_regex("count++")            # increment
    assert not _looks_like_regex("count?")             # ternary / optional
    assert not _looks_like_regex("a*b")                # multiplication
    assert not _looks_like_regex("$variable")          # PHP / shell var
    assert not _looks_like_regex("^header")            # leading caret OK


def test_looks_like_regex_treats_double_escaped_pipe_as_literal() -> None:
    """``\\\\|`` (escaped backslash + pipe) is genuinely literal — user
    wants to find a literal backslash-pipe sequence, not alternation."""
    from ast_outline.grep import _looks_like_regex
    # Pattern: literal `\` followed by literal `|`. Looks like regex
    # alternation but with the backslash escaped — we treat as bare
    # `|` after the escaped backslash. Conservative: still promote
    # (the pipe is unescaped from the regex's perspective). Edge case
    # documented; users who genuinely want literal `\|` use --regex
    # with their own escaping.
    assert _looks_like_regex(r"\\|")  # \\ then | — pipe is unescaped


def test_looks_like_ambiguous_regex_catches_escaped_metachars() -> None:
    """Escaped metachars like ``\\.``, ``\\(`` are regex-only intent —
    used by the warn-on-no-match safety net."""
    from ast_outline.grep import looks_like_ambiguous_regex
    assert looks_like_ambiguous_regex(r"save\.method")
    assert looks_like_ambiguous_regex(r"User\(")
    assert looks_like_ambiguous_regex(r"arr\[0\]")


def test_looks_like_ambiguous_regex_catches_quantifiers() -> None:
    """Quantifiers attached to identifiers (``foo+``, ``bar*``) signal
    likely-regex intent."""
    from ast_outline.grep import looks_like_ambiguous_regex
    assert looks_like_ambiguous_regex("foo+")
    assert looks_like_ambiguous_regex("bar*")
    assert looks_like_ambiguous_regex("baz?")


def test_looks_like_ambiguous_regex_catches_line_anchors() -> None:
    """``^`` and ``$`` at the edges of the pattern are likely regex
    anchors."""
    from ast_outline.grep import looks_like_ambiguous_regex
    assert looks_like_ambiguous_regex("^class")
    assert looks_like_ambiguous_regex("trailing$")


def test_looks_like_ambiguous_regex_catches_dot_wildcard_with_quantifier() -> None:
    """``.*`` / ``.+`` / ``.?`` is the canonical regex-wildcard shape and
    has no literal-code interpretation — a bare ``.`` legitimately
    appears in qualified names (``foo.bar``), but ``.<quantifier>`` does
    not. Without this trigger, ``Bind.*SaveSystem`` (a common shape
    when agents grep for generic-call invocations) silently matches
    nothing — the previous fingerprint required a *letter* before the
    quantifier (``d*``), which ``.*`` doesn't have."""
    from ast_outline.grep import looks_like_ambiguous_regex
    assert looks_like_ambiguous_regex("Bind.*SaveSystem")
    assert looks_like_ambiguous_regex("foo.+bar")
    assert looks_like_ambiguous_regex("a.?b")
    # Bare dot (no quantifier) still skipped — it's a literal qualified-name
    # separator in nearly all languages.
    assert not looks_like_ambiguous_regex("User.save")


def test_looks_like_ambiguous_regex_skips_plain_literals() -> None:
    """Plain symbol-style literals never trigger the warn-on-no-match
    hint — would be noise in normal "symbol not found" cases."""
    from ast_outline.grep import looks_like_ambiguous_regex
    assert not looks_like_ambiguous_regex("User.save")
    assert not looks_like_ambiguous_regex("compute_total")
    assert not looks_like_ambiguous_regex("MAX_RETRIES")
    # `string[]` ends with `]` not preceded by quantifier — not
    # caught by the quantifier-after-name pattern.
    assert not looks_like_ambiguous_regex("string[]")


# --- -w / --word ---------------------------------------------------------


def test_grep_word_match_filters_substring_noise(tmp_path: Path) -> None:
    """``-w save`` finds whole-word ``save`` only, not ``save_user``,
    ``unsave``, ``saved``, etc."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def save_user(): pass\n"
        "def unsave(): pass\n"
        "def saved(): pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    save_user()\n"
    )
    results = grep("save", [src], word_match=True).files
    fr = results[0]
    # 1 def of `save` + 1 call of `save()` — only whole-word matches.
    # `save_user`, `unsave`, `saved` are all substrings, not whole words.
    assert len(fr.matches) == 2
    kinds = sorted(m.kind for m in fr.matches)
    assert kinds == [KIND_CALL, KIND_DEF]


def test_grep_word_match_with_regex(tmp_path: Path) -> None:
    """``-w`` over regex wraps the whole regex in word boundaries."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save_user(): pass\n"
        "def save_admin(): pass\n"
        "\n"
        "def use():\n"
        "    save_user()\n"
        "    notsave_user()\n"          # has `save_user` substring; -w should reject
    )
    results = grep(r"save_\w+", [src], is_regex=True, word_match=True).files
    fr = results[0]
    # 2 defs + 1 call (the `notsave_user` is a substring within a longer
    # identifier, so word-boundary fails).
    assert len(fr.matches) == 3


def test_grep_word_match_escapes_literal_metachars(tmp_path: Path) -> None:
    """``-w User.save`` (literal) treats the dot as literal, not regex
    'any char' — and still applies word boundaries."""
    src = tmp_path / "mod.py"
    src.write_text(
        '"User.save"\n'                  # in string — filtered
        'def caller():\n'
        '    User.save()\n'              # whole literal match
        '    UserXsave()\n'              # would match if `.` were regex
    )
    results = grep("User.save", [src], word_match=True).files
    fr = results[0]
    visible = [m for m in fr.matches if m.kind != KIND_STRING]
    # The `User.save()` call matches; `UserXsave` does NOT (because
    # `.` is escaped by re.escape before wrapping in \b).
    assert len(visible) == 1
    assert visible[0].kind == KIND_CALL


# --- CLI smoke tests for -l / -c -----------------------------------------
#
# These hit the CLI dispatch layer (``_cmd_grep``) since the grep()
# function itself doesn't know about output modes — those are pure
# rendering concerns. We use subprocess rather than click-runner-style
# invocation so the test exercises the same path real agents do.


def _run_cli(*args: str) -> str:
    """Invoke the CLI's ``main`` and return stdout. We call in-process
    rather than spawning a subprocess so the test stays fast and free
    of any binary-installation dependency.
    """
    import io
    import contextlib
    from ast_outline.cli import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(list(args))
    return buf.getvalue()


def test_cli_files_only_output(tmp_path: Path) -> None:
    """``-l`` outputs just file paths, no scope-trees."""
    src1 = tmp_path / "a.py"
    src1.write_text("def save(): pass\n")
    src2 = tmp_path / "b.py"
    src2.write_text("def save(): pass\n")
    src3 = tmp_path / "c.py"
    src3.write_text("def other(): pass\n")
    output = _run_cli("grep", "-l", "save", str(tmp_path))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    # Only files containing 'save' should be listed; c.py absent.
    paths = set(lines)
    assert src1.as_posix() in paths
    assert src2.as_posix() in paths
    assert src3.as_posix() not in paths
    # No scope-tree leakage — output must not contain `## matches` or `>`.
    assert "## matches" not in output
    assert "> L" not in output


def test_cli_count_output(tmp_path: Path) -> None:
    """``-c`` outputs ``path:N`` per file, skipping zero-count files."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def use():\n"
        "    save()\n"
        "    save()\n"
    )
    output = _run_cli("grep", "-c", "save", str(tmp_path))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 1
    assert lines[0] == f"{src.as_posix()}:3"


def test_cli_files_only_and_count_are_mutex(tmp_path: Path) -> None:
    """``-l`` and ``-c`` together are an argparse error — surfaced
    on stderr as ``# note:`` (nothing ran, so stdout stays empty)."""
    import contextlib
    import io

    from ast_outline.cli import main

    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        main(["grep", "-l", "-c", "save", str(tmp_path)])
    assert out.getvalue() == ""
    assert "# note:" in err.getvalue()
    assert "not allowed" in err.getvalue()


# --- --max-count / -m ----------------------------------------------------
#
# Per-file cap, POSIX `grep -m` semantics. Truncation is surfaced
# explicitly so an LLM consumer can never silently mistake a partial
# result for an exhaustive one.


def test_grep_max_count_caps_per_file_matches(tmp_path: Path) -> None:
    """``max_count=2`` keeps two matches and records the rest as truncated."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    save(); save(); save(); save(); save()\n"
    )
    results = grep("save", [src], max_count=2).files
    assert len(results) == 1
    fr = results[0]
    assert len(fr.matches) == 2
    assert fr.truncated_count == 3


def test_grep_max_count_no_op_when_under_cap(tmp_path: Path) -> None:
    """File with fewer matches than the cap is unaffected."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\nsave()\n")
    results = grep("save", [src], max_count=10).files
    fr = results[0]
    assert len(fr.matches) == 2
    assert fr.truncated_count == 0


def test_grep_max_count_applies_after_noise_filter(tmp_path: Path) -> None:
    """Cap counts visible matches, not pre-filtered noise.

    The 4 comment matches are filtered first; cap=2 then keeps both real
    matches without truncation. Critical for LLMs — otherwise ``-m 2``
    on a comment-heavy file might silently return 0 visible matches
    while reporting "2 cap reached".
    """
    src = tmp_path / "mod.py"
    src.write_text(
        "# save save\n"
        "# save save\n"
        "def use():\n"
        "    save()\n"
        "    save()\n"
    )
    results = grep("save", [src], max_count=2).files
    fr = results[0]
    assert len(fr.matches) == 2
    assert fr.truncated_count == 0


def test_render_includes_truncation_footer(tmp_path: Path) -> None:
    """Rendered output carries an explicit truncation note so the agent
    knows results are partial — silent truncation is the failure mode
    we're guarding against."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    save(); save(); save(); save(); save()\n"
    )
    results = grep("save", [src], max_count=2).files
    rendered = render_grep(results)
    assert "truncated" in rendered
    assert "3 more" in rendered
    assert "--max-count" in rendered


def test_render_truncation_footer_singular(tmp_path: Path) -> None:
    """Singular form ``1 more match`` (not ``1 more matches``) — small
    polish, but agent-facing output should read naturally."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    save(); save(); save()\n"
    )
    results = grep("save", [src], max_count=2).files
    rendered = render_grep(results)
    assert "1 more match" in rendered
    assert "1 more matches" not in rendered


def test_cli_max_count_caps_output(tmp_path: Path) -> None:
    """``ast-outline grep -m 2`` caps and emits truncation footer."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    save(); save(); save(); save()\n"
    )
    output = _run_cli("grep", "-m", "2", "save", str(src))
    # Two visible match lines (`> L...`)
    match_lines = [ln for ln in output.splitlines() if ln.lstrip().startswith("> L")]
    assert len(match_lines) == 2
    assert "truncated" in output
    assert "2 more" in output


def test_cli_max_count_with_count_mode_reflects_cap(tmp_path: Path) -> None:
    """``-c`` with ``-m`` reports the capped count, matching POSIX grep."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def use():\n"
        "    save(); save(); save(); save(); save()\n"
    )
    output = _run_cli("grep", "-c", "-m", "2", "save", str(src))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines == [f"{src.as_posix()}:2"]


def test_cli_max_count_with_files_only_lists_capped_file(tmp_path: Path) -> None:
    """``-l`` lists files with any visible match — the cap doesn't
    change ``did this file match``; just the per-file detail."""
    src1 = tmp_path / "a.py"
    src1.write_text("def use():\n    save(); save(); save()\n")
    src2 = tmp_path / "b.py"
    src2.write_text("def other(): pass\n")
    output = _run_cli("grep", "-l", "-m", "1", "save", str(tmp_path))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    assert src1.as_posix() in lines
    assert src2.as_posix() not in lines


def test_cli_max_count_rejects_zero_and_negative(tmp_path: Path) -> None:
    """``-m 0`` and ``-m -1`` are rejected with a ``# note:`` line —
    consistent with the no-non-zero-exit-code contract."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    output = _run_cli("grep", "-m", "0", "save", str(src))
    assert "# note:" in output
    assert "must be" in output

    output = _run_cli("grep", "--max-count=-1", "save", str(src))
    assert "# note:" in output
    assert "must be" in output


# --- --kind ---------------------------------------------------------------
#
# Filter by classification. The most frequent post-filter agents do
# manually today — "show me only definitions" / "only calls" — collapsed
# into a single flag.


def test_grep_kind_filter_def_only(tmp_path: Path) -> None:
    """``kind_filter={'def'}`` returns only definition matches."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    results = grep("save", [src], kind_filter={KIND_DEF}).files
    kinds = _kinds(results)
    assert kinds == [KIND_DEF]
    assert KIND_CALL not in kinds
    assert KIND_REF not in kinds


def test_grep_kind_filter_call_excludes_def_and_ref(tmp_path: Path) -> None:
    """``kind_filter={'call'}`` keeps calls only."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    results = grep("save", [src], kind_filter={KIND_CALL}).files
    kinds = _kinds(results)
    assert kinds == [KIND_CALL]


def test_grep_kind_filter_multiple_kinds(tmp_path: Path) -> None:
    """Filter accepts a set — multiple kinds keep all of them."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    results = grep("save", [src], kind_filter={KIND_CALL, KIND_REF}).files
    kinds = sorted(set(_kinds(results)))
    assert kinds == sorted([KIND_CALL, KIND_REF])
    assert KIND_DEF not in _kinds(results)


def test_grep_kind_filter_import_only(tmp_path: Path) -> None:
    """``kind_filter={'import'}`` isolates import matches."""
    src = tmp_path / "mod.py"
    src.write_text(
        "from models import User\n"
        "\n"
        "def use():\n"
        "    return User()\n"
    )
    results = grep("User", [src], kind_filter={KIND_IMPORT}).files
    kinds = _kinds(results)
    assert kinds == [KIND_IMPORT]


def test_grep_kind_filter_skips_dont_count_as_noise(tmp_path: Path) -> None:
    """``--kind`` skips are explicit user narrowing — they should NOT
    bump ``filtered_count`` (which is reserved for the noise filter that
    the user can opt back in via ``--include-noise``)."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    results = grep("save", [src], kind_filter={KIND_DEF}).files
    fr = results[0]
    # Two matches dropped (call + ref) but they are not "hidden noise"
    # — the user explicitly asked for defs only, so no opt-in footer.
    assert fr.filtered_count == 0


def test_grep_kind_filter_suppresses_noise_footer_when_irrelevant(
    tmp_path: Path,
) -> None:
    """When ``kind_filter`` excludes comment/string, the noise footer
    must not surface — ``--include-noise`` wouldn't help (those matches
    would still be dropped by the kind filter) so the hint would mislead."""
    src = tmp_path / "mod.py"
    src.write_text(
        '"""docstring with save save save"""\n'
        "# comment with save\n"
        "from models import save\n"
        "def use():\n"
        "    save()\n"
    )
    # Filter to imports only. The comment hit is doubly excluded (noise
    # filter + kind narrow) so it rides ``kind_excluded``, not
    # ``filtered_count`` — the "--include-noise to see" footer would
    # be misleading under a non-comment --kind narrow.
    results = grep("save", [src], kind_filter={KIND_IMPORT}).files
    fr = results[0]
    assert fr.filtered_count == 0  # footer wouldn't render — accurate
    # Sanity: without kind filter, the comment hit IS counted as hidden.
    results_no_filter = grep("save", [src]).files
    assert results_no_filter[0].filtered_count > 0


def test_grep_kind_filter_comment_requires_include_noise(tmp_path: Path) -> None:
    """``kind_filter={'comment'}`` without ``include_noise=True`` returns
    empty — the noise filter runs first. Caller (CLI) is responsible for
    auto-enabling include_noise; the library itself stays composable."""
    src = tmp_path / "mod.py"
    src.write_text(
        "# call save here\n"
        "def use():\n"
        "    pass\n"
    )
    # Without include_noise: noise filter eats the comment match first;
    # file is still in results (filtered_count > 0) but matches list empty.
    results = grep("save", [src], kind_filter={KIND_COMMENT}).files
    assert all(fr.matches == [] for fr in results)
    # With include_noise: comment match surfaces.
    results = grep(
        "save", [src], kind_filter={KIND_COMMENT}, include_noise=True
    ).files
    kinds = _kinds(results)
    assert kinds == [KIND_COMMENT]


# --- CLI: --kind ----------------------------------------------------------


def test_cli_kind_filter_single_value(tmp_path: Path) -> None:
    """``--kind def`` returns only definitions in CLI output."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
    )
    output = _run_cli("grep", "--kind", "def", "save", str(src))
    # Only one match line; `[def]` tag visible.
    match_lines = [ln for ln in output.splitlines() if ln.lstrip().startswith(("def ", "> L"))]
    assert any("[def]" in ln for ln in match_lines)
    # `save()` call line shouldn't appear as a `> L`-prefixed match.
    assert not any("save()" in ln and ln.lstrip().startswith("> L") for ln in match_lines)


def test_cli_kind_filter_comma_separated(tmp_path: Path) -> None:
    """``--kind def,call`` (rg-style) accepts both kinds in one flag."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    output = _run_cli("grep", "--kind", "def,call", "save", str(src))
    # def + call should be present; ref (handler = save) should not.
    assert "[def]" in output
    assert "save()" in output
    # ref line `handler = save` would appear without the `(` after save —
    # absence of `handler = save` means ref was filtered.
    assert "handler = save" not in output


def test_cli_kind_filter_repeated_flag(tmp_path: Path) -> None:
    """``--kind def --kind call`` (POSIX-repeatable style) equivalent
    to comma-separated form."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save()\n"
        "    handler = save\n"
    )
    output = _run_cli(
        "grep", "--kind", "def", "--kind", "call", "save", str(src)
    )
    assert "[def]" in output
    assert "save()" in output
    assert "handler = save" not in output


def test_cli_kind_filter_invalid_value(tmp_path: Path) -> None:
    """Invalid kind name surfaces ``# note:`` and exits cleanly."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\n")
    output = _run_cli("grep", "--kind", "definition", "save", str(src))
    assert "# note:" in output
    assert "invalid --kind" in output
    assert "definition" in output


def test_cli_kind_filter_comment_auto_enables_noise(tmp_path: Path) -> None:
    """``--kind comment`` should auto-enable ``--include-noise`` so the
    user gets results without remembering a second flag."""
    src = tmp_path / "mod.py"
    src.write_text(
        "# call save here\n"
        "def use():\n"
        "    pass\n"
    )
    output = _run_cli("grep", "--kind", "comment", "save", str(src))
    # The comment match should surface because include_noise was auto-set.
    assert "[comment]" in output


def test_cli_kind_filter_empty_normalizes(tmp_path: Path) -> None:
    """``--kind def,,call`` (stray comma) normalizes to {def, call} —
    not an error. Defensive against agent typos."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def caller(): save()\n"
    )
    output = _run_cli("grep", "--kind", "def,,call", "save", str(src))
    assert "[def]" in output
    assert "save()" in output


def test_cli_kind_filter_combines_with_count(tmp_path: Path) -> None:
    """``-c --kind def`` reports counts after kind filtering — same as
    POSIX grep + flag composition: each filter applies independently."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save(): pass\n"
        "def caller():\n"
        "    save()\n"
        "    save()\n"
    )
    # 1 def + 2 calls = 3 total. With --kind def, count should be 1.
    output = _run_cli("grep", "-c", "--kind", "def", "save", str(src))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines == [f"{src.as_posix()}:1"]


def test_cli_kind_filter_excludes_files_with_no_matching_kind(
    tmp_path: Path,
) -> None:
    """``-l --kind def`` excludes files where the symbol exists only as
    calls — they have no matching kind, so file isn't listed."""
    src1 = tmp_path / "with_def.py"
    src1.write_text("def save(): pass\n")
    src2 = tmp_path / "calls_only.py"
    src2.write_text("def use():\n    save()\n    save()\n")
    output = _run_cli("grep", "-l", "--kind", "def", "save", str(tmp_path))
    lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("#")]
    assert src1.as_posix() in lines
    assert src2.as_posix() not in lines


def test_cli_kind_filter_zero_results_hints_at_excluded_kinds(
    tmp_path: Path,
) -> None:
    """``--kind call`` with 0 results but matches present under other
    kinds must surface a ``# hint:`` listing what was excluded and how
    to retry. The bug report: ``ast-outline grep EditorPrefs ... --kind
    call`` returned bare "no matches" while ``rg`` showed dozens of
    ``EditorPrefs.GetString(...)`` lines — those are ``ref`` (dot after
    the match), not ``call``, but the bare "no matches" hid that fact."""
    src = tmp_path / "sample.cs"
    src.write_text(
        "class C {\n"
        "    void M() {\n"
        "        string x = EditorPrefs.GetString(\"k\", \"\");\n"
        "        EditorPrefs.SetString(\"k\", x);\n"
        "        EditorPrefs.DeleteKey(\"k\");\n"
        "    }\n"
        "}\n"
    )
    output = _run_cli("grep", "EditorPrefs", str(src), "--kind", "call")
    assert "# note: no matches for 'EditorPrefs'" in output
    assert "# hint: --kind call excluded" in output
    # Natural-count form, NOT key=value (which reads as a flag value).
    assert "3 matches" in output
    assert "(3 ref)" in output
    # Retry suggestion must include the original kind plus the excluded one.
    assert "--kind call,ref" in output


def test_cli_kind_filter_zero_results_hint_multi_kind_breakdown(
    tmp_path: Path,
) -> None:
    """Hint must list multiple excluded kinds, highest-count first.
    Verifies the ranking rule: agents reading the line should see the
    most-likely-useful kind without scanning."""
    src = tmp_path / "sample.py"
    src.write_text(
        "import target\n"           # import
        "def use():\n"
        "    target()\n"            # call
        "    target()\n"            # call
        "    target()\n"            # call
        "    x = target\n"          # ref
    )
    # Ask for only def → 0 results, but import/call/ref all excluded.
    output = _run_cli("grep", "target", str(src), "--kind", "def")
    assert "# note: no matches for 'target'" in output
    assert "# hint: --kind def excluded" in output
    # Total + breakdown: 5 matches (3 call, 1 import, 1 ref).
    assert "5 matches" in output
    # "3 call" should appear before "1 ref" (highest count first).
    line = next(ln for ln in output.splitlines() if ln.startswith("# hint:"))
    call_idx = line.find("3 call")
    ref_idx = line.find("1 ref")
    assert call_idx != -1 and ref_idx != -1
    assert call_idx < ref_idx


def test_cli_kind_filter_zero_results_hint_skipped_when_no_matches_at_all(
    tmp_path: Path,
) -> None:
    """If the pattern matches nothing regardless of kind, the hint must
    NOT fire — there's nothing to suggest. Bare ``# note:`` is correct."""
    src = tmp_path / "sample.py"
    src.write_text("def save(): pass\n")
    output = _run_cli("grep", "nonexistent_xyz", str(src), "--kind", "call")
    assert "# note: no matches" in output
    assert "# hint: --kind" not in output


def test_cli_kind_filter_hint_skipped_when_results_present(
    tmp_path: Path,
) -> None:
    """When ``--kind`` produces results, the kind-exclusion hint must
    NOT fire — the user got what they asked for. Keeps successful
    output free of advisory noise."""
    src = tmp_path / "sample.py"
    src.write_text(
        "def save(): pass\n"
        "save()\n"
    )
    output = _run_cli("grep", "save", str(src), "--kind", "call")
    assert "save()" in output
    assert "# hint: --kind" not in output


def test_cli_kind_filter_zero_results_hint_covers_comment_only_matches(
    tmp_path: Path,
) -> None:
    """A pattern that lives only inside a comment / string must still
    trigger the kind-exclusion hint when a non-noise ``--kind`` narrow
    is applied. Without this guard, noise filtering runs BEFORE the
    kind filter and drops the matches silently — agents see bare
    "no matches" while ``rg`` would surface dozens of occurrences.
    The suggested retry (``--kind def,comment``) must work because
    ``--kind comment`` auto-enables ``--include-noise`` in the CLI."""
    src = tmp_path / "sample.py"
    src.write_text(
        "def foo():\n"
        "    # secret_token = 'abc'\n"
        "    pass\n"
    )
    output = _run_cli("grep", "secret_token", str(src), "--kind", "def")
    assert "# note: no matches for 'secret_token'" in output
    assert "# hint: --kind def excluded" in output
    assert "1 comment" in output
    assert "--kind comment,def" in output


def test_cli_kind_filter_zero_results_hint_covers_string_only_matches(
    tmp_path: Path,
) -> None:
    """Same gap, with the match in a string literal under ``--kind
    call``. Verifies the per-kind accounting covers all six kinds
    (def / call / ref / import / comment / string) — not just the
    code-side kinds the noise filter happens to leave alone."""
    src = tmp_path / "sample.py"
    src.write_text(
        "def foo():\n"
        "    x = 'secret_token_xyz'\n"
    )
    output = _run_cli("grep", "secret_token_xyz", str(src), "--kind", "call")
    assert "# hint: --kind call excluded" in output
    assert "1 string" in output
    assert "--kind call,string" in output


def test_cli_kind_filter_hint_yields_to_regex_hint(tmp_path: Path) -> None:
    """Regex-syntax hint takes priority over kind-exclusion hint on
    zero results — if the pattern was likely misinterpreted as a
    literal, that's the more actionable fix to surface first. One hint
    per empty result keeps the output scannable."""
    src = tmp_path / "sample.py"
    src.write_text("def save(): pass\nsave()\n")
    # Pattern with quantifier (`+`) — ambiguous regex; under literal
    # mode it won't match `save`, triggering the regex hint. Even with
    # --kind set, the regex hint should fire (more useful) and the
    # kind hint should be suppressed.
    output = _run_cli("grep", "save+", str(src), "--kind", "def")
    assert "# note: no matches" in output
    assert "regex" in output  # regex hint fired
    assert "# hint: --kind" not in output


def test_cli_regex_hint_fires_on_dot_wildcard_with_quantifier(
    tmp_path: Path,
) -> None:
    """``Bind.*SaveSystem`` is the canonical shape an agent types when
    grepping for a generic-call invocation (``Bind<SaveSystem>()`` in
    C#, ``Bind<SaveSystem>`` in Java/TS/Kotlin). Under literal mode the
    pattern doesn't match the source byte-for-byte (the source has
    ``<...>``, not arbitrary chars), so 0 results came back with no
    hint — agents had no signal that ``--regex`` would have worked.
    Now the ``.<quantifier>`` shape is recognized as unambiguous
    regex intent and the existing regex hint fires."""
    src = tmp_path / "sample.cs"
    src.write_text(
        "public class Bootstrap {\n"
        "    public void Configure(Container c) {\n"
        "        c.Bind<SaveSystem>();\n"
        "    }\n"
        "}\n"
    )
    output = _run_cli("grep", "Bind.*SaveSystem", str(src))
    assert "# note: no matches" in output
    assert "regex" in output
    assert "Bind.*SaveSystem" in output


def test_cli_invalid_regex_after_auto_promote_yields_note_not_traceback(
    tmp_path: Path,
) -> None:
    """BRE→Python auto-promote can produce an invalid Python regex when
    the source pattern carries literal parens: ``foo\\|bar\\.method(``
    converts to ``foo|bar\\.method(`` — the unescaped ``(`` opens an
    unterminated group. The CLI must surface a ``# note: invalid
    regex …`` + ``# hint: …`` and return exit 0, not traceback.
    """
    import io
    import contextlib
    from ast_outline.cli import main
    src = tmp_path / "a.py"
    src.write_text("def foo(): pass\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["grep", r"foo\|bar\.method(", str(src)])
    out = buf.getvalue()
    assert rc == 0
    assert "# note: invalid regex" in out
    assert "unterminated subpattern" in out
    assert "# hint:" in out
    assert "\\(" in out  # the hint mentions escaping parens


# --- Per-language KIND classification matrix -----------------------------
#
# Each language adapter must classify matches consistently for `--kind`
# filtering to be trustworthy. Python and TypeScript already have
# scattered tests above; this section is the systematic per-adapter
# coverage for the remaining 14 supported languages.
#
# Each test exercises:
#   - def detection (match on a declaration's own name)
#   - call detection (name followed by `(` after stripping generics/etc.)
#   - ref detection (name not followed by `(`)
#   - import detection (line starts with import-style prefix)
#   - comment detection (line starts with comment marker)
#   - string detection (match inside a string literal)
#
# Where a language doesn't natively have a kind (no imports in SQL,
# no def/call distinction in YAML), the test omits that case rather
# than asserting weakly.
#
# The classifier lives in ``src/ast_outline/grep.py``:
#   - ``_classify_match`` — per-line dispatch; comment / import line
#     prefixes come from each adapter's ``comment_line_prefixes`` /
#     ``import_line_prefixes`` attributes
#   - ``_next_call_paren_after`` — call vs ref (handles generics,
#     turbofish, optional chain, non-null assertion)


def _kinds_for_pattern(
    tmp_path: Path, ext: str, source: str, pattern: str, **kwargs
) -> list[tuple[int, str, str]]:
    """Run ``grep`` on a file with given extension/source, return
    ``[(line, kind, content), ...]`` tuples for every match.

    Helper for per-language kind tests — keeps each test focused on the
    classification claim rather than file plumbing.
    """
    src = tmp_path / f"sample.{ext}"
    src.write_text(source)
    results = grep(pattern, [src], **kwargs).files
    if not results:
        return []
    return [(m.line, m.kind, m.line_content.strip()) for m in results[0].matches]


def _kinds_only(matches: list[tuple[int, str, str]]) -> set[str]:
    """Set of distinct kinds seen across matches — for "is X present" checks."""
    return {kind for _, kind, _ in matches}


# --- C# ------------------------------------------------------------------


def test_kind_csharp(tmp_path: Path) -> None:
    """C# def + call + ref + import + comment + string."""
    src = (
        "using System.Text;\n"            # L1: import
        "namespace App {\n"
        "    public class Player {\n"
        "        public void Save() { }\n"     # L4: def
        "    }\n"
        "    public class Caller {\n"
        "        public void Run() {\n"
        "            var p = new Player();\n"  # L8: ref to Player (constructor uses `new`, also a call to Player ctor)
        "            p.Save();\n"               # L9: call to Save
        "            // call Save here\n"        # L10: comment containing Save
        "            var msg = \"call Save here\";\n"  # L11: string containing Save
        "        }\n"
        "    }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "cs", src, "Save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    # L4 — Save is the method's own name on its declaration line
    assert by_line.get(4) == KIND_DEF, f"expected def at L4, got {by_line}"
    # L9 — p.Save() is a call (paren follows)
    assert by_line.get(9) == KIND_CALL, f"expected call at L9, got {by_line}"
    # L10 — comment line
    assert by_line.get(10) == KIND_COMMENT
    # L11 — string literal
    assert by_line.get(11) == KIND_STRING

    using_matches = _kinds_for_pattern(tmp_path, "cs", src, "System")
    assert any(k == KIND_IMPORT for _, k, _ in using_matches), (
        "C# `using` line should classify as import"
    )


# --- Java ----------------------------------------------------------------


def test_kind_java(tmp_path: Path) -> None:
    """Java def + call + ref + import + comment + string."""
    src = (
        "import java.util.List;\n"            # L1: import
        "public class Player {\n"
        "    public void save() { }\n"            # L3: def
        "}\n"
        "class Caller {\n"
        "    void run() {\n"
        "        Player p = new Player();\n"      # L7: ref + call to Player
        "        p.save();\n"                     # L8: call to save
        "        // save here\n"                   # L9: comment
        "        String msg = \"save here\";\n"   # L10: string
        "    }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "java", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_DEF
    assert by_line.get(8) == KIND_CALL
    assert by_line.get(9) == KIND_COMMENT
    assert by_line.get(10) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "java", src, "java.util")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches)


# --- Kotlin --------------------------------------------------------------


def test_kind_kotlin(tmp_path: Path) -> None:
    """Kotlin def + call + ref + import + comment + string. Kotlin allows
    `import foo.*` star imports — same prefix detection."""
    src = (
        "import kotlin.system.exitProcess\n"   # L1: import
        "class Player {\n"
        "    fun save() { }\n"                       # L3: def
        "}\n"
        "fun run() {\n"
        "    val p = Player()\n"                     # L6
        "    p.save()\n"                             # L7: call
        "    // save here\n"                          # L8: comment
        "    val msg = \"save here\"\n"              # L9: string
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "kt", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_DEF
    assert by_line.get(7) == KIND_CALL
    assert by_line.get(8) == KIND_COMMENT
    assert by_line.get(9) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "kt", src, "kotlin")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches)


# --- Scala ---------------------------------------------------------------


def test_kind_scala(tmp_path: Path) -> None:
    """Scala def + call + import + comment + string."""
    src = (
        "import scala.collection.mutable\n"    # L1: import
        "class Player {\n"
        "  def save(): Unit = { }\n"                  # L3: def
        "}\n"
        "object Caller {\n"
        "  def run(): Unit = {\n"
        "    val p = new Player()\n"                  # L7
        "    p.save()\n"                              # L8: call
        "    // save here\n"                           # L9: comment
        "    val msg = \"save here\"\n"               # L10: string
        "  }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "scala", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_DEF
    assert by_line.get(8) == KIND_CALL
    assert by_line.get(9) == KIND_COMMENT
    assert by_line.get(10) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "scala", src, "scala")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches)


# --- Go ------------------------------------------------------------------


def test_kind_go(tmp_path: Path) -> None:
    """Go def + call + import + comment + string. Go's `import (...)`
    block is a real risk — the prefix check sees only `import ` on
    the opening line, NOT on the indented per-package lines inside
    the block. We assert what we DO support and document the gap."""
    src = (
        'import "fmt"\n'                       # L1: single-line import
        "type Player struct{}\n"
        "func (p *Player) Save() {}\n"          # L3: def
        "func run() {\n"
        "    p := &Player{}\n"                  # L5
        "    p.Save()\n"                        # L6: call
        "    // call Save here\n"                # L7: comment
        "    msg := \"call Save here\"\n"       # L8: string
        "    _ = fmt.Println(msg)\n"
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "go", src, "Save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_DEF, f"go def detection failed: {by_line}"
    assert by_line.get(6) == KIND_CALL
    assert by_line.get(7) == KIND_COMMENT
    assert by_line.get(8) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "go", src, "fmt")
    # The single-line `import "fmt"` should classify as import.
    assert any(k == KIND_IMPORT for _, k, _ in import_matches), (
        "Go single-line `import \"X\"` should classify as import"
    )


def test_kind_go_import_block_inner_lines_classify_as_import(
    tmp_path: Path,
) -> None:
    """Go's parenthesized import block: inner package paths MUST
    classify as ``[import]``, not ``[string]``. The Go adapter
    populates ``import_regions`` from the tree-sitter
    ``import_declaration`` node, which spans the whole ``import (...)``
    block — so every byte position inside is recognized as import
    context regardless of the line's surface syntax.

    Without this, agents searching ``ast-outline grep --kind import
    fmt`` on a Go file would silently miss imports inside blocks —
    semantically wrong since a human/LLM reader trivially recognizes
    those lines as imports. Was a pinned limitation in 0.7.7; fixed
    in 0.7.8."""
    src = (
        "import (\n"             # L1
        '    "fmt"\n'            # L2: inside block
        '    "strings"\n'        # L3: inside block
        ")\n"
        "func use() { _ = strings.Split() }\n"  # L5: real call site
    )
    matches = _kinds_for_pattern(tmp_path, "go", src, "strings")
    by_line = {line: kind for line, kind, _ in matches}
    # L3 inside the block — now correctly classified as import.
    assert by_line.get(3) == KIND_IMPORT, (
        f"Go import-block inner line should classify as [import], "
        f"got {by_line}"
    )
    # L5 — `strings.Split()` matched on `strings` is still a ref (the
    # package, followed by `.`, not `(`). Outside the import region.
    assert by_line.get(5) == KIND_REF


def test_kind_go_single_line_import_still_classifies_as_import(
    tmp_path: Path,
) -> None:
    """Sanity: the single-line ``import "fmt"`` form (covered by both
    the line-prefix heuristic AND the new import_regions) must still
    classify as ``[import]``. Both detection paths overlap here —
    pin that they don't fight each other."""
    src = (
        'import "fmt"\n'        # L1: single-line import
        "func use() { _ = fmt.Println }\n"  # L2: ref to fmt
    )
    matches = _kinds_for_pattern(tmp_path, "go", src, "fmt")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(1) == KIND_IMPORT


def test_kind_python_multiline_from_import(tmp_path: Path) -> None:
    """Python parenthesized multi-line imports: each inner symbol
    must classify as ``[import]``. Was broken before import_regions
    — symbols on indented lines had no ``import ``/``from `` prefix
    and would classify as ``[ref]`` (or filtered if the classifier
    saw them as code). Python adapter now populates import_regions
    via tree-sitter walk of ``import_from_statement`` nodes."""
    src = (
        "from foo import (\n"   # L1
        "    Bar,\n"            # L2: inner — symbol Bar
        "    Baz,\n"            # L3: inner — symbol Baz
        ")\n"
    )
    bar_matches = _kinds_for_pattern(tmp_path, "py", src, "Bar")
    by_line = {line: kind for line, kind, _ in bar_matches}
    assert by_line.get(2) == KIND_IMPORT, (
        f"Python multi-line import inner symbol should be [import], "
        f"got {by_line}"
    )
    baz_matches = _kinds_for_pattern(tmp_path, "py", src, "Baz")
    by_line = {line: kind for line, kind, _ in baz_matches}
    assert by_line.get(3) == KIND_IMPORT


def test_kind_typescript_multiline_import(tmp_path: Path) -> None:
    """TypeScript multi-line ``import { A, B } from '...'``: inner
    identifiers on their own lines must classify as ``[import]``.
    Without import_regions they'd classify as ``[ref]`` because the
    inner lines carry no ``import `` prefix."""
    src = (
        "import {\n"            # L1
        "    foo,\n"            # L2
        "    bar,\n"            # L3
        "} from './module';\n"  # L4
    )
    foo_matches = _kinds_for_pattern(tmp_path, "ts", src, "foo")
    by_line = {line: kind for line, kind, _ in foo_matches}
    assert by_line.get(2) == KIND_IMPORT, (
        f"TS multi-line import inner symbol should be [import], "
        f"got {by_line}"
    )


def test_kind_rust_use_group_inner_classifies_as_import(tmp_path: Path) -> None:
    """Rust ``use foo::{...}`` group form: inner identifiers on their
    own lines must classify as ``[import]``. Without import_regions
    they'd classify as ``[ref]``."""
    src = (
        "use std::collections::{\n"   # L1
        "    HashMap,\n"              # L2
        "    BTreeMap,\n"             # L3
        "};\n"
    )
    matches = _kinds_for_pattern(tmp_path, "rs", src, "HashMap")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_IMPORT, (
        f"Rust use-group inner symbol should be [import], got {by_line}"
    )


def test_kind_php_use_group_inner_classifies_as_import(tmp_path: Path) -> None:
    """PHP ``use App\\{...}`` group form: inner class names on their
    own lines must classify as ``[import]``."""
    src = (
        "<?php\n"
        "use App\\{\n"          # L2
        "    Foo,\n"            # L3
        "    Bar,\n"            # L4
        "};\n"
    )
    matches = _kinds_for_pattern(tmp_path, "php", src, "Foo")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_IMPORT, (
        f"PHP use-group inner symbol should be [import], got {by_line}"
    )


# --- C++ using directives + declarations (not type aliases) ---------------


def test_kind_cpp_using_namespace_classifies_as_import(tmp_path: Path) -> None:
    """``using namespace std;`` brings names from a namespace into scope.
    Semantically an import — agents searching ``--kind import std`` on
    C++ code should find it. The C++ adapter populates import_regions
    via tree-sitter ``using_directive`` nodes."""
    src = (
        "#include <vector>\n"
        "using namespace std;\n"          # L2: directive
        "void use() { vector<int> v; }\n"  # L3: actual use
    )
    matches = _kinds_for_pattern(tmp_path, "cpp", src, "std")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_IMPORT, (
        f"`using namespace std;` should be [import], got {by_line}"
    )


def test_kind_cpp_using_declaration_classifies_as_import(tmp_path: Path) -> None:
    """``using std::vector;`` brings a single symbol into scope —
    semantically an import (different AST node from `using namespace`,
    same import semantics)."""
    src = (
        "#include <vector>\n"
        "using std::vector;\n"            # L2: declaration
        "void use() { vector<int> v; }\n"
    )
    matches = _kinds_for_pattern(tmp_path, "cpp", src, "vector")
    by_line = {line: kind for line, kind, _ in matches}
    # L1 is #include line — also import via line-prefix
    assert by_line.get(1) == KIND_IMPORT
    # L2 is using-declaration — import via piggyback
    assert by_line.get(2) == KIND_IMPORT, (
        f"`using std::vector;` should be [import], got {by_line}"
    )


def test_kind_cpp_alias_declaration_NOT_import(tmp_path: Path) -> None:
    """``using my_int = int;`` is a type alias, NOT an import — even
    though the line starts with ``using ``. The C++ adapter relies on
    AST node distinction (``alias_declaration`` vs ``using_directive``/
    ``using_declaration``) to discriminate; a naive line-prefix would
    misclassify aliases as imports. Pin this so a switch to line-prefix
    detection later doesn't silently break."""
    src = (
        "using my_int = int;\n"          # L1: type alias — NOT import
        "my_int x = 42;\n"                # L2: use site
    )
    matches = _kinds_for_pattern(tmp_path, "cpp", src, "my_int")
    by_line = {line: kind for line, kind, _ in matches}
    # Should classify as def (it's the alias's own declaration line) or
    # ref — but NOT import. The key invariant: not [import].
    assert by_line.get(1) != KIND_IMPORT, (
        f"`using A = B;` is type alias, must NOT be [import]. Got {by_line}"
    )


# --- C# global using (C# 10+ / .NET 6+) ----------------------------------


def test_kind_csharp_global_using_classifies_as_import(tmp_path: Path) -> None:
    """``global using System;`` is the modern .NET 6+ file-scoped
    using directive. Line stripped starts with ``global``, not
    ``using``, so the existing ``"using "`` prefix doesn't catch it.
    A separate ``"global using "`` prefix in the dict handles it."""
    src = (
        "global using System;\n"           # L1: global using
        "using System.Text;\n"             # L2: regular using
        "namespace App {\n"
        "    class Foo { void use(StringBuilder s) {} }\n"
        "}\n"
    )
    # Both kinds of using should classify as import.
    sys_matches = _kinds_for_pattern(tmp_path, "cs", src, "System")
    by_line = {line: kind for line, kind, _ in sys_matches}
    assert by_line.get(1) == KIND_IMPORT, (
        f"`global using System;` should be [import], got {by_line}"
    )
    assert by_line.get(2) == KIND_IMPORT, (
        f"regular `using System.Text;` should still be [import], got {by_line}"
    )


# --- Documented known gaps (pinned for visibility) -----------------------
#
# These pin CURRENT behavior on edge cases we deliberately don't fix
# yet (low frequency, or fix cost > benefit). When/if any of these get
# proper handling, flip the assertion intentionally rather than
# discover the change accidentally.


def test_kind_scala_multiline_braced_inner_lines(tmp_path: Path) -> None:
    """Scala ``import foo.{\\n  Bar,\\n  Baz,\\n}`` multi-line braced
    form: inner symbols on their own indented lines must classify as
    ``[import]``. The Scala adapter populates ``import_regions`` via
    tree-sitter ``import_declaration`` nodes (which span the entire
    multi-line group), so inner lines are recognized regardless of
    their lack of a ``import`` prefix. Was a pinned gap in 0.7.7."""
    src = (
        "import foo.{\n"           # L1: opening — line-prefix
        "    Bar,\n"               # L2: inner — must be [import]
        "    Baz,\n"               # L3: inner — must be [import]
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "scala", src, "Bar")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_IMPORT, (
        f"Scala multi-line braced inner symbol should be [import], "
        f"got {by_line}"
    )
    matches = _kinds_for_pattern(tmp_path, "scala", src, "Baz")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_IMPORT


def test_typescript_reexport_intentionally_not_import(tmp_path: Path) -> None:
    """TS re-exports ``export { X } from './mod'`` are EXPORTS, not
    imports — and not a gap to fix. Per ESM spec, the re-exported
    binding does NOT enter the current module's local scope: writing
    ``new User()`` below an ``export { User } from './user'`` would
    error with ``User is not defined``. So classifying re-exports as
    ``[import]`` would mislead an agent into thinking ``User`` is
    available locally for use.

    The re-export IS a module dependency (``./user`` is loaded), but
    that's covered by the ``--imports`` listing, not by grep
    classification. For ``--kind import`` queries, agents want
    "where is X bound in this file for local use" — and re-exports
    don't bind anything locally.

    Pinned so a well-meaning future change ("re-exports look like
    imports, let's catch them") doesn't silently regress this
    deliberate distinction.
    """
    src = (
        "export { User } from './user';\n"   # L1: re-export — NOT import (by design)
        "export class Other {}\n"             # L2: declaration, NOT import
    )
    matches = _kinds_for_pattern(tmp_path, "ts", src, "User")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(1) != KIND_IMPORT, (
        f"TS re-export `export {{ X }} from '...'` must NOT classify "
        f"as [import] — User isn't bound in local scope per ESM spec. "
        f"Got {by_line}"
    )


def test_known_gap_ruby_autoload(tmp_path: Path) -> None:
    """Ruby ``autoload :Foo, 'foo.rb'`` is semantically a lazy import.
    Not in our import-prefix dict for Ruby — currently classifies as
    a call. Niche, deferred."""
    src = "autoload :Foo, 'foo.rb'\n"
    matches = _kinds_for_pattern(tmp_path, "rb", src, "autoload")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(1) != KIND_IMPORT, (
        f"Pinned known gap: Ruby `autoload` not yet [import]. "
        f"Got {by_line}"
    )


def test_known_gap_php_assignment_wrapped_require(tmp_path: Path) -> None:
    """PHP ``$x = require 'a.php';`` (assignment-wrapped) is excluded
    from the imports list by design (it's an assignment, not a
    declarative include). For grep classification: line stripped
    starts with ``$x``, not ``require ``, so line-prefix doesn't
    catch it either. ``require`` matches as ref/call. Niche."""
    src = (
        "<?php\n"
        "$x = require 'config.php';\n"   # L2: wrapped
    )
    matches = _kinds_for_pattern(tmp_path, "php", src, "require")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) != KIND_IMPORT, (
        f"Pinned known gap: PHP `$x = require ...;` not [import]. "
        f"Got {by_line}"
    )


def test_kind_python_lazy_multiline_in_function_body(tmp_path: Path) -> None:
    """Python ``def foo(): from x import (\\n a,\\n)`` — multi-line
    lazy import inside a function body. Inner symbols must classify
    as ``[import]``. Was a pinned gap; now covered by extending
    ``_count_conditional_imports`` to also collect byte ranges in
    the same tree walk it already does for counting (zero-cost
    piggyback — no extra traversal)."""
    src = (
        "def lazy():\n"
        "    from collections import (\n"   # L2: opening
        "        OrderedDict,\n"             # L3: inner — must be [import]
        "        defaultdict,\n"             # L4: inner — must be [import]
        "    )\n"
    )
    matches = _kinds_for_pattern(tmp_path, "py", src, "OrderedDict")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_IMPORT, (
        f"Python lazy multi-line import inner line should be "
        f"[import], got {by_line}"
    )
    matches = _kinds_for_pattern(tmp_path, "py", src, "defaultdict")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(4) == KIND_IMPORT


def test_kind_python_lazy_import_inside_class_body(tmp_path: Path) -> None:
    """Python class-body lazy import — same coverage as function-body.
    Class bodies are also in ``_PY_RUNTIME_OR_SCOPED`` so the
    conditional walker catches imports inside them."""
    src = (
        "class Manager:\n"
        "    from typing import (\n"     # L2
        "        Dict,\n"                # L3: inner
        "    )\n"
    )
    matches = _kinds_for_pattern(tmp_path, "py", src, "Dict")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_IMPORT


def test_kind_classification_priority_comment_beats_import(
    tmp_path: Path,
) -> None:
    """Priority order: a comment INSIDE an import block stays
    ``[comment]``, not ``[import]``. Agents searching for ``[comment]``
    matches should find them regardless of containing context."""
    src = (
        "import (\n"
        '    // mention strings here\n'   # L2: comment inside import block
        '    "fmt"\n'
        ")\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "go", src, "strings", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_COMMENT, (
        f"comment inside import block should stay [comment], got {by_line}"
    )


# --- Rust ----------------------------------------------------------------


def test_kind_rust(tmp_path: Path) -> None:
    """Rust def + call + import + comment + string. Rust's import is
    `use foo::Bar` — the `use ` prefix is in the dictionary."""
    src = (
        "use std::collections::HashMap;\n"     # L1: import
        "pub struct Player;\n"
        "impl Player {\n"
        "    pub fn save(&self) {}\n"                 # L4: def
        "}\n"
        "fn run() {\n"
        "    let p = Player;\n"                       # L7
        "    p.save();\n"                             # L8: call
        "    // call save here\n"                      # L9: comment
        "    let msg = \"call save here\";\n"         # L10: string
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "rs", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(4) == KIND_DEF
    assert by_line.get(8) == KIND_CALL
    assert by_line.get(9) == KIND_COMMENT
    assert by_line.get(10) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "rs", src, "HashMap")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches)


def test_kind_rust_turbofish_classifies_as_call(tmp_path: Path) -> None:
    """Rust's turbofish `foo::<T>()` — the call detector must skip the
    turbofish and find the trailing `(`. Without this, every generic
    call would misclassify as ref. Critical for Rust idiomatic code."""
    src = (
        "fn run() {\n"
        '    let v = parse::<i32>("42");\n'   # L2: turbofish call
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "rs", src, "parse")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_CALL, (
        f"turbofish `parse::<i32>()` should classify as call, got {by_line}"
    )


def test_kind_rust_turbofish_match_ending_on_angle_closer(tmp_path: Path) -> None:
    """``parse::<i32`` literal match — ends on ``>`` with no opener
    left to skip via the balanced-block branch. The closer-skip is
    what makes ``--kind call`` work here. (Real agents type the
    literal-with-type form to disambiguate between multiple ``parse``
    overloads.)"""
    src = (
        "fn run() {\n"
        '    let v = parse::<i32>("42");\n'
        '    let w = parse::<u64>("42");\n'
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "rs", src, "parse::<i32")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_CALL, by_line


# --- Generic-call matrix (closer-skip across languages) ------------------
#
# Every language with generic / type-argument call syntax must classify
# ``Foo<T>()`` / ``Foo[T]()`` invocations as ``call`` even when the
# grep match ends ON the generic closer (``>`` or ``]``) rather than
# past it. Bug fixed in v0.8.8 — the walker's closer-skip handles all
# of these uniformly. One test per language locks in the matrix.


def test_kind_csharp_generic_call_match_ending_on_closer(tmp_path: Path) -> None:
    """C# DI ``container.Bind<SaveSystem>()`` — match ending on ``>``
    classifies as call. This is the v0.8.8 repro: real agent grep
    against a Unity codebase fell through to ``rg`` because
    ``--kind call`` returned 0 hits under the regex pattern."""
    src = (
        "public class Boot {\n"
        "    public void Configure(Container c) {\n"
        "        c.Bind<SaveSystem>();\n"           # L3
        "    }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "cs", src, "Bind<SaveSystem")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(3) == KIND_CALL, by_line


def test_kind_java_generic_constructor_match_ending_on_closer(
    tmp_path: Path,
) -> None:
    """Java ``new ArrayList<String>()`` — ctor invocation is a call.
    Match ``ArrayList<String`` ends on ``>``."""
    src = (
        "import java.util.ArrayList;\n"
        "class App {\n"
        "    void run() {\n"
        "        var xs = new ArrayList<String>();\n"  # L4
        "    }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "java", src, "ArrayList<String")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(4) == KIND_CALL, by_line


def test_kind_kotlin_generic_call_match_ending_on_closer(tmp_path: Path) -> None:
    """Kotlin ``listOf<String>()`` — match ``listOf<String`` ends on ``>``."""
    src = (
        "fun run() {\n"
        '    val xs = listOf<String>("a", "b")\n'   # L2
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "kt", src, "listOf<String")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_CALL, by_line


def test_kind_typescript_generic_call_match_ending_on_closer(
    tmp_path: Path,
) -> None:
    """TS ``useState<number>(0)`` — match ``useState<number`` ends on ``>``.
    Common shape in React codebases where agents disambiguate hooks by
    type argument."""
    src = (
        "function App() {\n"
        "    const [n, setN] = useState<number>(0);\n"   # L2
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "ts", src, "useState<number")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_CALL, by_line


def test_kind_scala_generic_call_match_ending_on_square_closer(
    tmp_path: Path,
) -> None:
    """Scala uses ``[...]`` for type args: ``Map[String, Int](...)``.
    Match ``Map[String, Int`` ends on ``]``."""
    src = (
        "object App {\n"
        '    val m = Map[String, Int]("a" -> 1)\n'   # L2
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "scala", src, "Map[String, Int")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_CALL, by_line


def test_kind_go_generics_call_match_ending_on_square_closer(
    tmp_path: Path,
) -> None:
    """Go 1.18+ generics: ``Foo[int](42)`` uses ``[]`` for type args.
    Match ``Foo[int`` ends on ``]``."""
    src = (
        "package main\n"
        "\n"
        "func Foo[T any](x T) T { return x }\n"
        "\n"
        "func run() {\n"
        "    Foo[int](42)\n"                         # L6
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "go", src, "Foo[int")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(6) == KIND_CALL, by_line


# --- C++ -----------------------------------------------------------------


def test_kind_cpp(tmp_path: Path) -> None:
    """C++ def + call + import + comment + string. C++ imports use the
    preprocessor `#include` (both `<...>` and `"..."` forms)."""
    src = (
        "#include <string>\n"                  # L1: import
        "#include \"player.h\"\n"              # L2: import
        "class Player {\n"
        "public:\n"
        "    void save();\n"                         # L5: declaration
        "};\n"
        "void Player::save() { }\n"                  # L7: def
        "void run() {\n"
        "    Player p;\n"                            # L9
        "    p.save();\n"                            # L10: call
        "    // call save here\n"                     # L11: comment
        "    const char* msg = \"call save here\";\n"# L12: string
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "cpp", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    # L7 is the actual definition (with body); L5 is just declaration
    # — adapter behavior may vary on which it considers the "real" def.
    assert KIND_DEF in by_line.values(), f"no def detected: {by_line}"
    assert by_line.get(10) == KIND_CALL
    assert by_line.get(11) == KIND_COMMENT
    assert by_line.get(12) == KIND_STRING

    import_matches = _kinds_for_pattern(tmp_path, "cpp", src, "string")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches), (
        "C++ `#include <string>` should classify as import"
    )


# --- PHP -----------------------------------------------------------------


def test_kind_php(tmp_path: Path) -> None:
    """PHP def + call + import + comment + string. PHP has TWO comment
    prefixes (`//` and `#`) — both must work. Imports include `use`,
    `require`, `include`, and their `_once` variants."""
    src = (
        "<?php\n"
        "use App\\Models\\Player;\n"           # L2: import (use)
        "require_once 'config.php';\n"          # L3: import (require_once)
        "class Caller {\n"
        "    public function save(): void {}\n"      # L5: def
        "    public function run(): void {\n"
        "        $p = new Player();\n"               # L7
        "        $this->save();\n"                   # L8: call
        "        // call save here\n"                 # L9: // comment
        "        # call save here too\n"              # L10: # comment (PHP-specific!)
        "        $msg = \"call save here\";\n"       # L11: string
        "    }\n"
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "php", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(5) == KIND_DEF
    assert by_line.get(8) == KIND_CALL
    # Both comment styles should be recognized
    assert by_line.get(9) == KIND_COMMENT, f"// comment failed: {by_line}"
    assert by_line.get(10) == KIND_COMMENT, f"# comment failed: {by_line}"
    assert by_line.get(11) == KIND_STRING

    use_matches = _kinds_for_pattern(tmp_path, "php", src, "App")
    assert any(k == KIND_IMPORT for _, k, _ in use_matches), (
        "PHP `use App\\...` should classify as import"
    )

    require_matches = _kinds_for_pattern(tmp_path, "php", src, "config")
    assert any(k == KIND_IMPORT for _, k, _ in require_matches), (
        "PHP `require_once '...'` should classify as import"
    )


# --- Ruby ----------------------------------------------------------------


def test_kind_ruby(tmp_path: Path) -> None:
    """Ruby def + call + import + comment + string. Ruby imports are
    `require` / `require_relative` / `load`. Ruby has optional parens
    on calls (`save` vs `save()`); the call-with-parens form is what
    the classifier reliably catches — without parens it's ambiguous
    with ref."""
    src = (
        "require 'json'\n"                     # L1: import
        "require_relative './player'\n"         # L2: import
        "class Player\n"
        "  def save\n"                                # L4: def (no parens)
        "  end\n"
        "end\n"
        "class Caller\n"
        "  def run\n"
        "    p = Player.new\n"                        # L9
        "    p.save()\n"                              # L10: call (explicit parens)
        "    # call save here\n"                       # L11: comment
        "    msg = \"call save here\"\n"              # L12: string
        "  end\n"
        "end\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "rb", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(4) == KIND_DEF, f"Ruby `def save` failed: {by_line}"
    assert by_line.get(10) == KIND_CALL
    assert by_line.get(11) == KIND_COMMENT
    assert by_line.get(12) == KIND_STRING

    json_matches = _kinds_for_pattern(tmp_path, "rb", src, "json")
    assert any(k == KIND_IMPORT for _, k, _ in json_matches)
    rel_matches = _kinds_for_pattern(tmp_path, "rb", src, "player")
    assert any(k == KIND_IMPORT for _, k, _ in rel_matches), (
        "Ruby `require_relative` should classify as import"
    )


# --- TypeScript: missing kinds (comment, string) -------------------------
#
# TS already has def/call/ref/import covered above; fill the gap.


def test_kind_javascript_uses_ts_adapter(tmp_path: Path) -> None:
    """``.js`` files are handled by the TypeScript adapter (same
    grammar, language tag = ``typescript``). All TS classification
    rules apply — pin this so a future split into a separate JS
    adapter doesn't silently drop classification rules."""
    src = (
        "import { foo } from './bar';\n"      # L1: ES import
        "function save() { }\n"                  # L2: def
        "function run() {\n"
        "    save();\n"                          # L4: call
        "}\n"
    )
    matches = _kinds_for_pattern(tmp_path, "js", src, "save")
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_DEF
    assert by_line.get(4) == KIND_CALL


def test_kind_javascript_require_is_not_import(tmp_path: Path) -> None:
    """CommonJS ``require()`` is a runtime function call, not a
    statement-level import — classifies as KIND_CALL on ``require``,
    NOT KIND_IMPORT. ES ``import`` statements are the only thing
    we treat as imports in JS/TS. Pin this so the boundary stays
    explicit (and so users understand why ``--kind import`` doesn't
    surface ``require()`` calls)."""
    src = (
        "const foo = require('./bar');\n"     # L1: require call
        "import { baz } from './baz';\n"       # L2: real import
    )
    require_matches = _kinds_for_pattern(tmp_path, "js", src, "require")
    by_line = {line: kind for line, kind, _ in require_matches}
    assert by_line.get(1) == KIND_CALL, (
        f"`require('...')` should classify as call, not import: {by_line}"
    )
    baz_matches = _kinds_for_pattern(tmp_path, "js", src, "baz")
    baz_by_line = {line: kind for line, kind, _ in baz_matches}
    assert baz_by_line.get(2) == KIND_IMPORT, (
        f"ES `import` should still classify as import: {baz_by_line}"
    )


def test_kind_typescript_comment_and_string(tmp_path: Path) -> None:
    """TypeScript comment + string detection — the gap left by earlier
    TS tests above."""
    src = (
        "function run() {\n"
        "    // call save here\n"                  # L2: comment
        '    const msg = "call save here";\n'      # L3: string
        "}\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "ts", src, "save", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(2) == KIND_COMMENT
    assert by_line.get(3) == KIND_STRING


# --- Format languages — only the kinds that make sense -------------------


def test_kind_css_import_and_comment(tmp_path: Path) -> None:
    """CSS supports `@import` (treated as import) and `/* */` comments
    (NOT in our prefix dict — only `//` is, which is SCSS-only).
    Plain CSS comments use `/* */` and we currently don't detect those
    via prefix. We assert imports work and document the comment gap."""
    src = (
        "@import url('reset.css');\n"          # L1: import
        ".btn-primary { color: red; }\n"        # L2: ref
    )
    import_matches = _kinds_for_pattern(tmp_path, "css", src, "reset")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches), (
        "CSS `@import url(...)` should classify as import"
    )


def test_kind_scss_import_variants(tmp_path: Path) -> None:
    """SCSS has `@import`, `@use`, AND `@forward` — all three should
    classify as import. SCSS `// ...` comments work via the dict."""
    src = (
        "@use 'sass:math';\n"                  # L1: @use
        "@forward 'helpers';\n"                 # L2: @forward
        "@import 'reset';\n"                    # L3: @import
        ".btn { color: red; }  // call here\n"  # L4: trailing comment
    )
    use_matches = _kinds_for_pattern(tmp_path, "scss", src, "math")
    assert any(k == KIND_IMPORT for _, k, _ in use_matches), (
        "SCSS `@use` should classify as import"
    )
    forward_matches = _kinds_for_pattern(tmp_path, "scss", src, "helpers")
    assert any(k == KIND_IMPORT for _, k, _ in forward_matches), (
        "SCSS `@forward` should classify as import"
    )
    import_matches = _kinds_for_pattern(tmp_path, "scss", src, "reset")
    assert any(k == KIND_IMPORT for _, k, _ in import_matches), (
        "SCSS `@import` should classify as import"
    )


def test_kind_sql_comment(tmp_path: Path) -> None:
    """SQL uses `--` for line comments. Verify the classifier picks
    that up (the prefix is in our dict)."""
    src = (
        "-- find users by name\n"              # L1: comment
        "SELECT * FROM users;\n"                # L2: ref to users
    )
    matches = _kinds_for_pattern(
        tmp_path, "sql", src, "users", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(1) == KIND_COMMENT, f"SQL `--` comment failed: {by_line}"


def test_kind_yaml_comment(tmp_path: Path) -> None:
    """YAML uses `#` for line comments — same as Python."""
    src = (
        "# database settings\n"                # L1: comment
        "database:\n"
        "  host: localhost\n"
    )
    matches = _kinds_for_pattern(
        tmp_path, "yaml", src, "database", include_noise=True
    )
    by_line = {line: kind for line, kind, _ in matches}
    assert by_line.get(1) == KIND_COMMENT, f"YAML `#` comment failed: {by_line}"


def test_kind_markdown_no_comment_no_import(tmp_path: Path) -> None:
    """Markdown has no comment syntax we recognize and no import concept
    — every match should classify as ref by default. This pins the
    expected behavior so adding markdown comment support later is a
    deliberate choice, not silent drift."""
    src = (
        "# Heading about save\n"                  # L1: heading mentioning save
        "Some prose about save here.\n"            # L2: prose mentioning save
    )
    matches = _kinds_for_pattern(tmp_path, "md", src, "save")
    # Markdown has no comment / import prefixes, so neither kind should appear.
    kinds = _kinds_only(matches)
    assert KIND_COMMENT not in kinds
    assert KIND_IMPORT not in kinds


# --- POSIX-style `-e PATTERN PATHS...` (no positional pattern) -----------
#
# `grep -e PAT PATH` and `rg -e PAT PATH` are valid shapes; ast-outline
# now accepts them too via a pre-argparse rewrite that promotes the
# first -e value into the positional slot. These tests pin the
# acceptance and verify the existing call shapes still parse.


def test_cli_grep_dash_e_only_no_positional_pattern(tmp_path: Path) -> None:
    """``grep -e PAT PATH`` (no positional pattern) must work — that
    was the original POSIX shape that argparse couldn't accept on its
    own. Regression guard for the user-reported error
    'the following arguments are required: paths'."""
    src = tmp_path / "mod.py"
    src.write_text("def TryStartClosingIfFull(): pass\n")
    output = _run_cli("grep", "-e", "TryStartClosingIfFull", str(tmp_path))
    assert "# note:" not in output
    assert "TryStartClosingIfFull" in output


def test_cli_grep_dash_e_only_with_flag_after_path(tmp_path: Path) -> None:
    """The exact shape from the bug report — ``-e PAT PATH -m N`` —
    where a value-taking flag (``-m``) comes after the path. The arg
    rewriter must skip ``-m N`` correctly when scanning for free
    positionals."""
    src = tmp_path / "mod.py"
    src.write_text("def save(): pass\nsave()\nsave()\n")
    output = _run_cli("grep", "-e", "save", str(tmp_path), "-m", "1")
    assert "# note:" not in output
    assert "save" in output


def test_cli_grep_long_expression_form(tmp_path: Path) -> None:
    """Long-form ``--expression PAT PATH`` is the POSIX-spelled
    equivalent of ``-e``."""
    src = tmp_path / "mod.py"
    src.write_text("def widget(): pass\n")
    output = _run_cli("grep", "--expression", "widget", str(tmp_path))
    assert "# note:" not in output
    assert "widget" in output


def test_cli_grep_expression_equals_form(tmp_path: Path) -> None:
    """``--expression=PAT`` (equals form) — argparse accepts it; the
    rewriter must split on ``=`` to recover the pattern value."""
    src = tmp_path / "mod.py"
    src.write_text("def gadget(): pass\n")
    output = _run_cli("grep", "--expression=gadget", str(tmp_path))
    assert "# note:" not in output
    assert "gadget" in output


def test_cli_grep_multiple_dash_e_no_positional(tmp_path: Path) -> None:
    """Multiple ``-e`` patterns with no positional — first promotes to
    pattern, the rest stay as extra_patterns."""
    src = tmp_path / "mod.py"
    src.write_text("def alpha(): pass\ndef beta(): pass\n")
    output = _run_cli("grep", "-e", "alpha", "-e", "beta", str(tmp_path))
    assert "# note:" not in output
    assert "alpha" in output
    assert "beta" in output


def test_cli_grep_positional_then_dash_e_unchanged(tmp_path: Path) -> None:
    """Existing shape ``grep PAT -e PAT2 PATH`` (positional + extra)
    must keep current semantics — no rewrite when a positional pattern
    is already present before the first ``-e``."""
    src = tmp_path / "mod.py"
    src.write_text("def alpha(): pass\ndef beta(): pass\n")
    output = _run_cli("grep", "alpha", "-e", "beta", str(tmp_path))
    assert "# note:" not in output
    assert "alpha" in output
    assert "beta" in output


def test_cli_grep_plain_positional_unchanged(tmp_path: Path) -> None:
    """``grep PAT PATH`` (no -e at all) must behave exactly as before —
    the rewriter is a no-op for this shape."""
    src = tmp_path / "mod.py"
    src.write_text("def widget(): pass\n")
    output = _run_cli("grep", "widget", str(tmp_path))
    assert "# note:" not in output
    assert "widget" in output


def test_normalize_grep_argv_unit() -> None:
    """Unit-test the rewriter directly — covers branches that are awkward
    to hit through the CLI surface (no-op cases, ``--`` separator,
    short bool flags between -e and the path)."""
    from ast_outline.cli import _normalize_grep_argv

    # POSIX-style: -e PAT PATH → PAT PATH
    assert _normalize_grep_argv(["grep", "-e", "foo", "src"]) == [
        "grep", "foo", "src"
    ]
    # Multiple -e: only the first is promoted; the rest stay as -e.
    assert _normalize_grep_argv(["grep", "-e", "foo", "-e", "bar", "src"]) == [
        "grep", "foo", "-e", "bar", "src"
    ]
    # Existing positional comes before -e → no-op.
    assert _normalize_grep_argv(["grep", "foo", "-e", "bar", "src"]) == [
        "grep", "foo", "-e", "bar", "src"
    ]
    # No -e → no-op.
    assert _normalize_grep_argv(["grep", "foo", "src"]) == ["grep", "foo", "src"]
    # Equals form.
    assert _normalize_grep_argv(["grep", "--expression=foo", "src"]) == [
        "grep", "foo", "src"
    ]
    # Long form.
    assert _normalize_grep_argv(["grep", "--expression", "foo", "src"]) == [
        "grep", "foo", "src"
    ]
    # Bool flag between -e and path — flag should not be mistaken for a
    # value, and not for a free positional.
    assert _normalize_grep_argv(["grep", "-e", "foo", "-i", "src"]) == [
        "grep", "foo", "-i", "src"
    ]
    # Value-taking flag (``-m N``) after the path — its value (``2``)
    # must not be treated as a free positional.
    assert _normalize_grep_argv(["grep", "-e", "foo", "src", "-m", "2"]) == [
        "grep", "foo", "src", "-m", "2"
    ]
    # Non-grep subcommand → unchanged.
    assert _normalize_grep_argv(["outline", "src"]) == ["outline", "src"]
    # Empty / missing → unchanged.
    assert _normalize_grep_argv([]) == []


# --- byte → codepoint column conversion (Unicode source lines) -----------
#
# The classifier ``_classify_match`` and its callees index ``line_content``,
# a Python ``str`` whose elements are codepoints. The source is read as
# ``bytes`` and match spans come back as byte offsets. Lines that contain
# any multi-byte UTF-8 character (Cyrillic, CJK, emoji, accented Latin)
# previously had the byte offset passed through as-if it were a codepoint
# index — the walker landed past the intended position into later
# characters, and ``_column_inside_string`` over-counted quotes. The fix
# decodes the prefix once at the boundary so every downstream check sees
# a codepoint-correct column.
#
# Tests below pin the three downstream effects:
#   1. call/ref classification on Unicode identifier call sites
#   2. in-string detection on lines with Unicode before the quoted region
#   3. the stored ``GrepMatch.column`` field as a codepoint offset


def test_next_call_paren_after_skips_unicode_identifier_tail() -> None:
    """Identifier-skip must accept Unicode word characters, not only
    ASCII ``[A-Za-z0-9_]``. The same alternation-leftmost-wins shape
    that drove the v0.8.12 ASCII identifier-skip fix applies to
    Cyrillic / CJK / accented-Latin identifiers in Python / Rust /
    Swift / TypeScript / Java (all of which accept Unicode in
    identifiers). Pin the walker-unit behavior independently of
    the byte→codepoint boundary so a regression on either side
    surfaces with a tight error message."""
    from ast_outline.grep import _next_call_paren_after
    # Cyrillic: cursor at codepoint 4 inside ``привет(x)`` (after ``прив``).
    assert _next_call_paren_after("привет(x)", 4) is True
    # CJK: cursor at codepoint 1 inside ``漢字(x)`` (after ``漢``).
    assert _next_call_paren_after("漢字(x)", 1) is True
    # Accented Latin: cursor at codepoint 3 inside ``café(x)`` (after ``caf``).
    assert _next_call_paren_after("café(x)", 3) is True
    # Composes with generics: ``привет<T>()`` cursor at codepoint 4.
    assert _next_call_paren_after("привет<T>()", 4) is True
    # Negative — Cyrillic mid-identifier NOT followed by ``(``: must
    # stay False so non-call lines don't flip to call.
    assert _next_call_paren_after("привет = 1", 4) is False
    assert _next_call_paren_after("привет.метод()", 4) is False
    # Negative composition — Cyrillic identifier with a generic block
    # followed by NOT a call. Skip ``ет``, balance ``<T>``, hit ` `,
    # then ``=``. Without this pin a future reordering of the
    # generic-block skip relative to the identifier-skip could
    # silently flip Unicode-generic refs to calls.
    assert _next_call_paren_after("привет<T> = x", 4) is False
    # Narrower-than-isalnum check: Unicode No-category numerics
    # (``²``, ``¼``, ``ª``) must NOT be treated as identifier
    # continuation chars; the skip should stop at them. Contrived
    # but pins the boundary so a future ``isalnum()`` regression
    # doesn't silently extend the call-bias policy.
    assert _next_call_paren_after("x²foo(y)", 1) is False


def test_grep_unicode_identifier_call_classifies_call(tmp_path: Path) -> None:
    """End-to-end repro of the byte→codepoint boundary fix. Regex
    alternation ``прив|привет`` against a Cyrillic call site:
    Python ``re`` (bytes mode) picks the leftmost alternative
    ``прив``, match ends mid-identifier between ``прив`` and ``ет``.
    Before the fix the walker received a BYTE offset that over-shot
    the intended codepoint position (Cyrillic chars are 2 bytes in
    UTF-8), then the ASCII-only identifier-skip stopped at the
    Cyrillic tail anyway — two layers of the same bug. After:
    boundary decodes once to land on the codepoint immediately
    after the match, and the identifier-skip accepts Unicode word
    chars to walk the rest of ``ет`` to ``(``."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    obj.привет(x, y)\n",
        encoding="utf-8",
    )
    results = grep("прив|привет", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"Cyrillic identifier call must classify as call, got {kinds}"
    )


def test_grep_cjk_identifier_call_classifies_call(tmp_path: Path) -> None:
    """CJK identifiers (3 bytes per codepoint in UTF-8) are the
    harshest case for the byte→codepoint mismatch — every extra
    Han / Kana / Hangul character on the prefix shifts the byte
    column by 2 past the codepoint column. Pin the worst-case axis
    independently of the Cyrillic test so a regression on the
    decoder branch (only handling 2-byte sequences correctly, say)
    surfaces here."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        "    obj.漢字(x)\n",
        encoding="utf-8",
    )
    results = grep("漢|漢字", [src], is_regex=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"CJK identifier call must classify as call, got {kinds}"
    )


def test_grep_ascii_call_after_unicode_prefix_classifies_call(
    tmp_path: Path,
) -> None:
    """The match itself is ASCII, but the line carries Cyrillic
    BEFORE the match — the byte offset of the match is shifted
    past its codepoint index by the prefix. Without the
    byte→codepoint conversion the walker received a column past
    the end of ``line_content`` and the loop exited early without
    seeing the trailing ``(``, classifying as ``ref``. With the
    conversion the walker lands on ``(`` and returns True →
    ``call``."""
    src = tmp_path / "mod.py"
    # Line 2 has Cyrillic ``привет`` (6 codepoints, 12 bytes) BEFORE
    # the ASCII ``bar(...)`` call site.
    src.write_text(
        "def caller():\n"
        "    привет = bar()\n",
        encoding="utf-8",
    )
    results = grep("bar", [src]).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_CALL in kinds, (
        f"ASCII call after Unicode prefix must classify as call, got {kinds}"
    )


def test_grep_match_inside_string_after_unicode_prefix_classifies_string(
    tmp_path: Path,
) -> None:
    """``_column_inside_string`` counts unescaped quotes before
    ``column`` to decide whether the match falls inside an open
    string. Before the byte→codepoint fix, ``column`` was a byte
    offset — for a line like ``привет = "test bar"`` the byte
    column of ``bar`` over-shoots the codepoint count of the line,
    the quote-counting loop ran past the closing quote, the final
    parity was even, and ``bar`` was misclassified as ``ref``
    instead of ``string`` — surfacing string-literal matches as
    code and breaking the default noise filter on multi-byte
    locales."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def caller():\n"
        '    привет = "test bar"\n',
        encoding="utf-8",
    )
    results = grep("bar", [src]).files
    # Default mode filters out string matches → zero visible matches.
    visible_kinds = [m.kind for m in results[0].matches] if results else []
    assert KIND_REF not in visible_kinds, (
        "match inside a string literal must not classify as ref — "
        f"got visible kinds {visible_kinds}"
    )
    # With include_noise the string match surfaces — confirm the
    # classification went the right way, not that we silently dropped it.
    results_noise = grep("bar", [src], include_noise=True).files
    noise_kinds = [m.kind for m in results_noise[0].matches]
    assert KIND_STRING in noise_kinds, (
        f"match inside string literal must classify as string, got {noise_kinds}"
    )


def test_grep_inline_comment_after_unicode_prefix_classifies_comment(
    tmp_path: Path,
) -> None:
    """``_classify_match`` uses ``column`` to detect "match is past
    an inline comment marker" — ``x = 1  # call foo here`` with
    a match on ``foo`` after the ``#`` must classify as comment.
    The ``#`` index is compared against ``column - 1``; before the
    byte→codepoint fix, ``column`` was a byte offset that could
    place the match BEFORE the ``#`` index on Unicode-prefixed
    lines, leaking comment content into code results."""
    src = tmp_path / "mod.py"
    # ``привет`` codepoints precede the ``#`` marker; ``foo`` after
    # the marker must still register as past the comment.
    src.write_text(
        "def caller():\n"
        "    x = 1  # привет foo here\n",
        encoding="utf-8",
    )
    results = grep("foo", [src], include_noise=True).files
    kinds = [m.kind for m in results[0].matches]
    assert kinds == [KIND_COMMENT], (
        f"match past inline comment marker must classify as comment, got {kinds}"
    )


def test_grep_match_column_is_codepoint_offset(tmp_path: Path) -> None:
    """The stored ``GrepMatch.column`` is documented as a 1-based
    codepoint offset. Pin it on a Cyrillic-prefixed line so a
    regression that re-introduces the byte offset surfaces here
    instead of in a hard-to-diagnose downstream failure."""
    src = tmp_path / "mod.py"
    # Line 2: ``    привет = bar()`` — ``bar`` starts at codepoint 13
    # (4 spaces + 6 Cyrillic + ``  = `` = 4 + 6 + 3 = 13). Column is
    # 1-indexed → 14. The byte offset would be 4 + 12 + 3 = 19 → col 20.
    src.write_text(
        "def caller():\n"
        "    привет = bar()\n",
        encoding="utf-8",
    )
    results = grep("bar", [src]).files
    matches = results[0].matches
    assert len(matches) == 1
    assert matches[0].column == 14, (
        f"column must be a 1-based codepoint offset (14), got {matches[0].column}"
    )


# --- (a) leading definition-keyword stripping -----------------------------
#
# Agents habitually paste the source-language keyword in front of the
# symbol they want (``enum ItemSoundFamily``, ``class MailSpec``, ``def
# foo``). As a literal substring that misbehaves: on the declaration
# line the match starts on the keyword, not the name token, so the
# def-classifier labels it ``ref`` and a ``--kind def`` narrow drops it —
# a bare "no matches". Stripping the keyword and searching the identifier
# (auto-narrowed to ``def``) is the fix.


def test_strip_definition_keyword_unit() -> None:
    """The pure helper recognizes the ``"<keyword> Identifier"`` shape and
    rejects everything else."""
    assert strip_definition_keyword("enum Foo") == ("Foo", "enum")
    assert strip_definition_keyword("class MailSpec") == ("MailSpec", "class")
    assert strip_definition_keyword("def foo_bar") == ("foo_bar", "def")
    assert strip_definition_keyword("fn baz") == ("baz", "fn")
    assert strip_definition_keyword("type T") == ("T", "type")
    # Not the shape: single token, keyword-only, multiple tokens,
    # qualified name, leading non-keyword.
    assert strip_definition_keyword("Foo") is None
    assert strip_definition_keyword("enum") is None
    assert strip_definition_keyword("className") is None
    assert strip_definition_keyword("enum Foo : byte") is None
    assert strip_definition_keyword("Foo.Bar") is None
    assert strip_definition_keyword("widget gadget") is None
    assert strip_definition_keyword("enum  Foo") == ("Foo", "enum")  # extra space ok


def test_definition_keywords_sourced_from_adapters() -> None:
    """The strip keyword set is the union of each adapter's
    ``definition_keywords`` — the source of truth lives in the adapters,
    not in a hand-maintained list in grep.py."""
    from ast_outline.adapters import ADAPTERS
    from ast_outline.adapters.python import PythonAdapter
    from ast_outline.adapters.csharp import CSharpAdapter
    from ast_outline.adapters.rust import RustAdapter
    from ast_outline.grep import _definition_keywords

    # Code adapters declare their own keywords.
    assert "def" in PythonAdapter.definition_keywords
    assert "class" in PythonAdapter.definition_keywords
    assert "record" in CSharpAdapter.definition_keywords
    assert "fn" in RustAdapter.definition_keywords
    assert "impl" in RustAdapter.definition_keywords

    union = _definition_keywords()
    # Every adapter's keywords are reachable through the union.
    for adapter in ADAPTERS:
        assert set(getattr(adapter, "definition_keywords", frozenset())) <= union


def test_cli_strip_keyword_class_equals_kind_def(tmp_path: Path) -> None:
    """``grep "class X"`` (literal) returns the same matches as
    ``grep "X" --kind def``. The strip is silent in text output — it only
    rides the JSON ``notes`` array (see
    ``test_cli_strip_keyword_note_rides_json_envelope``)."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class MailSpec:\n"
        "    pass\n"
        "\n"
        "def use():\n"
        "    m = MailSpec()\n"
    )
    stripped = _run_cli("grep", "class MailSpec", str(src))
    baseline = _run_cli("grep", "MailSpec", str(src), "--kind", "def")
    # The strip is not narrated in text — agents ignore the FYI line.
    assert "stripped leading" not in stripped
    # The actual result rows (lines that aren't notes/hints) are identical.
    def _rows(out: str) -> list[str]:
        return [ln for ln in out.splitlines() if not ln.startswith("#")]
    assert _rows(stripped) == _rows(baseline)


@pytest.mark.parametrize(
    "keyword,lang_file,decl",
    [
        ("enum", "e.cs", "public enum Color { Red }"),
        ("struct", "s.rs", "struct Point { x: i32 }"),
        ("def", "d.py", "def handler():\n    pass"),
        ("interface", "i.ts", "interface Shape { area(): number }"),
        ("func", "f.go", "func Compute() int { return 1 }"),
    ],
)
def test_cli_strip_keyword_across_languages(
    tmp_path: Path, keyword: str, lang_file: str, decl: str
) -> None:
    """Each language's own definition keyword is stripped and the symbol
    is found as a definition (``[def]`` tag), with no text-mode note."""
    src = tmp_path / lang_file
    # Pull the declared name out of the snippet (first identifier after
    # the keyword) so the test stays in lockstep with the snippet.
    name = decl.split(keyword, 1)[1].split()[0].rstrip("(){:")
    src.write_text(decl + "\n")
    out = _run_cli("grep", f"{keyword} {name}", str(src))
    # Stripped + auto-narrowed to def: the match lands on the name and is
    # tagged [def] (a literal "<keyword> name" search would tag it a ref).
    assert "[def]" in out
    assert name in out
    assert "no matches" not in out
    # The strip is silent in text output.
    assert "stripped leading" not in out


def test_cli_strip_keyword_respects_explicit_regex(tmp_path: Path) -> None:
    """Explicit ``--regex`` disables keyword-stripping — the user means
    exactly what they typed, so ``class MailSpec`` is searched literally
    (and matches the declaration line as a ref, not stripped to a def)."""
    src = tmp_path / "mod.py"
    src.write_text("class MailSpec:\n    pass\n")
    out = _run_cli("grep", "--regex", "class MailSpec", str(src))
    assert "stripped leading" not in out
    # Literal regex still matches the declaration line.
    assert "class MailSpec" in out


def test_cli_strip_keyword_keeps_explicit_kind(tmp_path: Path) -> None:
    """When the user gave an explicit ``--kind``, the keyword is still
    stripped but the kind is NOT auto-narrowed to def — their filter
    wins. The strip is silent in text; the JSON note drops the
    "as a definition" phrasing."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class MailSpec:\n"
        "    pass\n"
        "\n"
        "def use():\n"
        "    m = MailSpec()\n"
    )
    out = _run_cli("grep", "class MailSpec", str(src), "--kind", "call")
    # --kind call kept (no auto-narrow to def) → the instantiation is found.
    assert "MailSpec()" in out
    assert "stripped leading" not in out
    # The JSON note documents the strip without the "as a definition" phrase.
    import json
    data = json.loads(
        _run_cli("grep", "class MailSpec", str(src), "--kind", "call", "--json")
    )
    assert any("stripped leading 'class'" in n for n in data["notes"])
    assert not any("as a definition" in n for n in data["notes"])


def test_cli_strip_keyword_not_triggered_on_normal_queries(tmp_path: Path) -> None:
    """Negative: ordinary symbol queries must NOT be stripped — single
    tokens, keyword-only, qualified names, and a leading non-keyword
    word all pass through untouched."""
    src = tmp_path / "mod.py"
    src.write_text("class className:\n    pass\n")
    for pat in ("className", "class", "MyClass", "save"):
        out = _run_cli("grep", pat, str(src))
        assert "stripped leading" not in out, f"{pat!r} must not be stripped"


# --- (b) kind-mismatch + did-you-mean interaction -------------------------


def test_cli_did_you_mean_suppressed_when_kind_hint_fires(tmp_path: Path) -> None:
    """The kind-exclusion hint (the symbol IS present, just a different
    kind) and the did-you-mean hint (no such symbol) are mutually
    exclusive — when the pattern matched under another kind there is no
    typo to suggest, so did-you-mean stays silent."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def save_item():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    save_item()\n"
    )
    out = _run_cli("grep", "save_item", str(src), "--kind", "ref")
    assert "# hint: --kind ref excluded" in out
    assert "did you mean" not in out


# --- (c) did-you-mean by edit distance ------------------------------------


def test_suggest_similar_symbols_unit(tmp_path: Path) -> None:
    """The pure helper returns close declaration names with kind+count,
    rejects blind guesses, short patterns, and non-identifiers."""
    src = tmp_path / "store.py"
    src.write_text(
        "class MissSortPile:\n"
        "    pass\n"
        "\n"
        "class MissSortPileGroup:\n"
        "    pass\n"
    )
    hits = suggest_similar_symbols("MissSortPiles", [src])
    names = [name for name, _kind, _count in hits]
    assert "MissSortPile" in names
    # Blind guess sharing no structure → nothing.
    assert suggest_similar_symbols("ZZZUnrelated", [src]) == []
    # Too short to be meaningful.
    assert suggest_similar_symbols("Mi", [src]) == []
    # Not a bare identifier.
    assert suggest_similar_symbols("Miss.Sort", [src]) == []
    # The pattern itself is never suggested.
    assert all(name != "MissSortPile" for name, _, _ in
               suggest_similar_symbols("MissSortPile", [src]))


def test_cli_did_you_mean_plural_singular(tmp_path: Path) -> None:
    """The dominant blind-retry case: a plural/singular mismatch. The
    closest real symbol is surfaced so the next call is the right one."""
    src = tmp_path / "store.py"
    src.write_text(
        "class MissSortPile:\n"
        "    pass\n"
        "\n"
        "class MissSortPileGroup:\n"
        "    pass\n"
    )
    out = _run_cli("grep", "MissSortPiles", str(src))
    assert "# note: no matches for 'MissSortPiles'" in out
    assert "# hint: did you mean:" in out
    assert "MissSortPile (class)" in out


def test_cli_did_you_mean_typo(tmp_path: Path) -> None:
    """A single-character typo on a real symbol is recovered."""
    src = tmp_path / "mod.py"
    src.write_text("def calculate_total():\n    pass\n")
    out = _run_cli("grep", "calculate_totl", str(src))
    assert "# hint: did you mean:" in out
    assert "calculate_total" in out


def test_cli_did_you_mean_blind_guess_gives_no_suggestion(tmp_path: Path) -> None:
    """Negative: a name sharing no structure with any real symbol must
    NOT produce a suggestion — difflib's cutoff rejects it, and the
    output is the bare ``# note:`` exactly as before."""
    src = tmp_path / "mod.py"
    src.write_text("def calculate_total():\n    pass\n")
    out = _run_cli("grep", "frobnicate_widget", str(src))
    assert "# note: no matches" in out
    assert "did you mean" not in out


def test_cli_did_you_mean_skipped_for_regex(tmp_path: Path) -> None:
    """Negative: with ``--regex`` the pattern is not a plain identifier
    the agent typed by hand, so no did-you-mean is offered."""
    src = tmp_path / "mod.py"
    src.write_text("def calculate_total():\n    pass\n")
    out = _run_cli("grep", "--regex", "calculate_totl", str(src))
    assert "did you mean" not in out


def test_cli_did_you_mean_composes_with_keyword_strip(tmp_path: Path) -> None:
    """``grep "class MissSortPiles"`` strips the keyword, finds nothing
    for the (plural) identifier, then suggests the real singular class —
    both heuristics fire on one call. The strip is silent in text (it
    rides the JSON notes); the did-you-mean hint is the text nudge."""
    src = tmp_path / "store.py"
    src.write_text("class MissSortPile:\n    pass\n")
    out = _run_cli("grep", "class MissSortPiles", str(src))
    assert "stripped leading" not in out
    assert "# hint: did you mean:" in out
    assert "MissSortPile" in out
    # The strip still rode the JSON notes — proof it fired on this call.
    import json
    data = json.loads(_run_cli("grep", "class MissSortPiles", str(src), "--json"))
    assert any("stripped leading 'class'" in n for n in data["notes"])


def test_cli_did_you_mean_reports_count_for_repeated_name(tmp_path: Path) -> None:
    """When several declarations share a near-miss name, the count is
    surfaced so the agent knows the symbol's footprint."""
    src1 = tmp_path / "a.py"
    src1.write_text("def handle():\n    pass\n")
    src2 = tmp_path / "b.py"
    src2.write_text("def handle():\n    pass\n")
    out = _run_cli("grep", "handlee", str(tmp_path))
    assert "# hint: did you mean:" in out
    # Two declarations named ``handle`` → count rendered.
    assert "handle (" in out and "×2" in out


# --- coverage gap-fillers: multi-pattern, JSON, adapter contract ----------


def test_cli_strip_keyword_not_triggered_with_multiple_patterns(
    tmp_path: Path,
) -> None:
    """Negative: keyword-stripping only handles the single-pattern form —
    with multiple ``-e`` patterns an auto narrow-to-def would be
    ambiguous, so the leading keyword is left intact."""
    src = tmp_path / "mod.py"
    src.write_text("class MailSpec:\n    pass\n")
    out = _run_cli("grep", "class MailSpec", "-e", "other", str(src))
    assert "stripped leading" not in out


def test_cli_strip_keyword_note_rides_json_envelope(tmp_path: Path) -> None:
    """The keyword-strip ``# note:`` documents a query rewrite, so unlike
    the interactive hints it must also ride the JSON ``notes`` array
    (both output modes searched the same identifier)."""
    import json
    src = tmp_path / "mod.py"
    src.write_text("class MailSpec:\n    pass\n")
    out = _run_cli("grep", "class MailSpec", str(src), "--json")
    data = json.loads(out)
    assert any("stripped leading 'class'" in n for n in data["notes"])
    assert any("as a definition" in n for n in data["notes"])


def test_cli_did_you_mean_absent_in_json_mode(tmp_path: Path) -> None:
    """did-you-mean is an interactive text-mode nudge — the JSON
    empty-result envelope returns early, so the suggestion must never
    leak into JSON (where a consumer would not expect prose hints)."""
    import json
    src = tmp_path / "store.py"
    src.write_text("class MissSortPile:\n    pass\n")
    out = _run_cli("grep", "MissSortPiles", str(src), "--json")
    data = json.loads(out)
    assert data["files"] == []
    assert data["notes"] == []
    assert "did you mean" not in out


def test_every_adapter_declares_definition_keywords() -> None:
    """Contract guard: every registered adapter exposes a
    ``definition_keywords`` frozenset (code adapters non-empty, data /
    markup adapters empty). Catches a new adapter that forgets it before
    the keyword-strip union silently skips that language."""
    from ast_outline.adapters import ADAPTERS
    for adapter in ADAPTERS:
        kws = adapter.definition_keywords
        assert isinstance(kws, frozenset), (
            f"{type(adapter).__name__}.definition_keywords must be a frozenset"
        )
        assert all(isinstance(k, str) and k for k in kws)


def test_every_adapter_declares_lexical_and_render_traits() -> None:
    """Contract guard for the adapter-owned per-language facts that
    used to live in central tables: every registered adapter must
    declare ``comment_line_prefixes`` / ``import_line_prefixes``
    (tuples of strings; empty allowed) and a ``render_family`` from
    the closed set core's renderers branch on. Catches a new adapter
    that forgets them before grep / digest silently misbehave for
    that language."""
    from ast_outline.adapters import ADAPTERS

    valid_families = {"code", "markdown", "yaml", "css", "html"}
    for adapter in ADAPTERS:
        name = type(adapter).__name__
        for attr in ("comment_line_prefixes", "import_line_prefixes"):
            value = getattr(adapter, attr)
            assert isinstance(value, tuple), f"{name}.{attr} must be a tuple"
            assert all(isinstance(s, str) and s for s in value), (
                f"{name}.{attr} entries must be non-empty strings"
            )
        family = adapter.render_family
        assert family in valid_families, (
            f"{name}.render_family {family!r} not in {sorted(valid_families)}"
        )


def test_cli_did_you_mean_path_returns_exit_zero(tmp_path: Path) -> None:
    """The did-you-mean path does extra work (it parses the scope to
    gather candidate names) — that work must never break the CLI exit-0
    invariant. Assert the return code directly, not just stdout."""
    from ast_outline.cli import main
    import io
    import contextlib
    src = tmp_path / "store.py"
    src.write_text("class MissSortPile:\n    pass\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["grep", "MissSortPiles", str(tmp_path)])
    assert rc == 0
    assert "did you mean" in buf.getvalue()


def test_grep_rust_lifetime_quote_does_not_open_string(tmp_path: Path) -> None:
    """Rust lifetimes (``'a``, ``'static``) are UNPAIRED single quotes —
    counting them as string delimiters flips the in-string state for the
    rest of the line, so every match after ``&'a`` on a signature line
    silently classified as ``[string]`` and vanished under the default
    noise filter. Both ``str`` occurrences on the lifetime-carrying line
    must stay visible."""
    src = tmp_path / "lt.rs"
    src.write_text(
        "pub fn first_word<'a>(s: &'a str) -> &'a str {\n"
        "    s\n"
        "}\n",
        encoding="utf-8",
    )
    results = grep("str", [src]).files
    assert results, "expected visible matches on the lifetime line"
    line1 = [m for m in results[0].matches if m.line == 1]
    assert len(line1) == 2, (
        f"both `str` matches after lifetimes must survive, got {line1}"
    )
    assert all(m.kind != KIND_STRING for m in line1)
    assert results[0].filtered_count == 0


def test_grep_rust_char_literal_still_classifies_string(tmp_path: Path) -> None:
    """The lifetime exception must NOT swallow real Rust char literals:
    ``'x'`` closes within two chars, so a match inside it still counts
    as ``[string]`` and is noise-filtered by default."""
    src = tmp_path / "ch.rs"
    src.write_text(
        "fn f() {\n"
        "    let c = 'x'; let y = 1;\n"
        "}\n",
        encoding="utf-8",
    )
    results = grep("x", [src], include_noise=True).files
    kinds = [m.kind for m in results[0].matches]
    assert KIND_STRING in kinds, (
        f"char-literal content must classify as string, got {kinds}"
    )


def test_grep_word_match_finds_cyrillic_identifier(tmp_path: Path) -> None:
    """``-w`` wraps the pattern in ``\\b`` boundaries. In bytes mode
    ``\\b``/``\\w`` are ASCII-only: every UTF-8 continuation byte is a
    non-word byte, no boundary is ever found, and a whole-word search
    for a Cyrillic identifier silently returned "no matches". The slow
    path must match against the decoded str so ``\\b`` is
    Unicode-aware."""
    src = tmp_path / "uni.py"
    src.write_text(
        "привет = 1\n"
        "def привет_мир():\n"
        "    return привет\n",
        encoding="utf-8",
    )
    results = grep("привет", [src], word_match=True).files
    assert results, "whole-word Cyrillic search must find matches"
    lines = sorted(m.line for m in results[0].matches)
    # L1 and L3 are standalone words; привет_мир on L2 must NOT match
    # (underscore is a word char — no boundary inside the identifier).
    assert lines == [1, 3], f"expected word matches on L1+L3, got {lines}"


def test_grep_case_insensitive_folds_cyrillic(tmp_path: Path) -> None:
    """``-i`` in bytes mode can't case-fold non-ASCII — ``ПРИВЕТ``
    never matched ``привет``. str-mode IGNORECASE folds Unicode."""
    src = tmp_path / "uni.py"
    src.write_text("привет = 1\n", encoding="utf-8")
    results = grep("ПРИВЕТ", [src], case_insensitive=True).files
    assert results and results[0].matches, (
        "case-insensitive Cyrillic search must find the lowercase form"
    )


def test_grep_regex_offsets_exact_after_multibyte_prefix(tmp_path: Path) -> None:
    """The str-mode slow path converts codepoint spans back to byte
    offsets. A match AFTER a multi-byte prefix must land on the right
    line/column — an offset skew would shift every downstream
    annotation (line, column, enclosing scope)."""
    src = tmp_path / "uni.py"
    src.write_text(
        "# привет мир\n"
        "def target():\n"
        "    pass\n",
        encoding="utf-8",
    )
    results = grep("target", [src], is_regex=True).files
    m = results[0].matches[0]
    assert (m.line, m.kind) == (2, KIND_DEF), (m.line, m.kind)
    assert m.column == 5


def test_grep_regex_offsets_survive_invalid_utf8(tmp_path: Path) -> None:
    """Files with invalid UTF-8 must not skew byte offsets in the
    str-mode slow path: ``surrogateescape`` round-trips each bad byte
    1:1 (``replace`` would substitute a 3-byte replacement char and
    shift every following match)."""
    src = tmp_path / "bad.py"
    src.write_bytes(b"x = b'\xff\xfe'\ntarget = 2\n")
    results = grep("target", [src], is_regex=True).files
    assert results and results[0].matches, "match after invalid bytes lost"
    m = results[0].matches[0]
    assert (m.line, m.column) == (2, 1), (m.line, m.column)
