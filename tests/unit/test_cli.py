"""End-to-end CLI integration tests.

These invoke `ast_outline.cli.main` directly and capture stdout/stderr,
so we don't need to spawn a subprocess.
"""
from __future__ import annotations

from ast_outline.cli import main


# --- Default / guide -----------------------------------------------------


def test_main_with_no_args_prints_guide(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ast-outline" in out
    assert "COMMANDS" in out


def test_help_command(capsys):
    rc = main(["help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "COMMANDS" in out


def test_help_topic_specific(capsys):
    rc = main(["help", "show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "show" in out.lower()
    assert "symbols" in out.lower()


def test_version_flag_long(capsys):
    """`--version` follows the universal CLI convention and prints
    version + author on dedicated lines so a script can grep one
    field without prose-parsing."""
    from ast_outline import __version__

    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"ast-outline {__version__}" in out
    assert "Dmitrii Zaitsev" in out
    assert "github.com/ast-outline/ast-outline" in out


def test_version_flag_short(capsys):
    """`-V` short form mirrors `git --version` / `rg --version` —
    both spellings produce identical output."""
    from ast_outline import __version__

    rc = main(["-V"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"ast-outline {__version__}" in out


# --- outline -------------------------------------------------------------


def test_outline_implicit_subcommand(csharp_dir, capsys):
    """`ast-outline path.cs` with no subcommand should default to `outline`."""
    rc = main([str(csharp_dir / "unity_behaviour.cs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HeroController" in out
    assert "TakeDamage" in out


def test_outline_explicit_subcommand(csharp_dir, capsys):
    rc = main(["outline", str(csharp_dir / "unity_behaviour.cs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HeroController" in out


def test_outline_directory_mixed_languages(fixtures_dir, capsys):
    rc = main([str(fixtures_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    # Both C# and Python symbols appear in one pass
    assert "HeroController" in out
    assert "UserService" in out


def test_outline_no_private_flag(csharp_dir, capsys):
    rc = main(["outline", str(csharp_dir / "unity_behaviour.cs"), "--no-private"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Die" not in out


def test_outline_no_lines_flag(csharp_dir, capsys):
    rc = main(["outline", str(csharp_dir / "unity_behaviour.cs"), "--no-lines"])
    out = capsys.readouterr().out
    assert rc == 0
    # Header is exempt; check signature lines
    body = "\n".join(out.splitlines()[1:])
    assert "  L" not in body


def test_outline_missing_file_returns_zero_with_note(tmp_path, capsys):
    """LLM-friendly mode: rc=0 + short ``# note:`` line on stdout so a
    parallel batch in Claude Code doesn't abort the whole chain."""
    nope = tmp_path / "nope.cs"
    rc = main(["outline", str(nope)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "# note:" in captured.out
    assert "path not found" in captured.out.lower()
    assert str(nope) in captured.out


# --- show ----------------------------------------------------------------


def test_show_single_symbol(csharp_dir, capsys):
    rc = main(["show", str(csharp_dir / "unity_behaviour.cs"), "HeroController.TakeDamage"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "public void TakeDamage" in out
    assert "OnHealthChanged" in out  # part of the method body


def test_show_prints_ancestor_breadcrumb(csharp_dir, capsys):
    """The `# in:` line lists enclosing namespace/type so the agent knows
    what the extracted body is nested inside, without a second `outline`."""
    rc = main(["show", str(csharp_dir / "unity_behaviour.cs"), "HeroController.TakeDamage"])
    out = capsys.readouterr().out
    assert rc == 0
    # Breadcrumb line starts with `# in:` and contains both ancestor signatures
    in_lines = [ln for ln in out.splitlines() if ln.startswith("# in:")]
    assert len(in_lines) == 1
    assert "namespace" in in_lines[0]
    assert "HeroController" in in_lines[0]
    assert "→" in in_lines[0]  # separator between outer and inner


def test_show_multiple_symbols(csharp_dir, capsys):
    rc = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "HeroController.Die",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "TakeDamage" in out
    assert "Die" in out


def test_show_ambiguous_symbol_prints_all_matches(csharp_dir, capsys):
    rc = main(["show", str(csharp_dir / "unity_behaviour.cs"), "TakeDamage"])
    captured = capsys.readouterr()
    assert rc == 0
    # Both definitions present
    assert "public void TakeDamage" in captured.out
    assert "void TakeDamage(int amount);" in captured.out
    # Stderr mentions multiple matches
    assert "matches" in captured.err.lower()


def test_show_not_found_returns_zero_with_note(csharp_dir, capsys):
    """LLM-friendly mode: missing symbol yields rc=0 + ``# note:`` on stdout."""
    rc = main(["show", str(csharp_dir / "unity_behaviour.cs"), "NoSuchThing"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "not found" in captured.out.lower()


def test_show_no_doc_strips_leading_doc(csharp_dir, capsys):
    rc = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--no-doc",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The ///-comment block is stripped
    assert "/// <summary>Apply damage" not in out
    assert "public void TakeDamage" in out


def test_show_python_method_with_docstring(python_dir, capsys):
    rc = main(["show", str(python_dir / "domain_model.py"), "UserService.get"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "def get" in out
    assert "Look up a user by id" in out


def test_show_python_strips_docstring_with_no_doc(python_dir, capsys):
    rc = main(
        ["show", str(python_dir / "domain_model.py"), "UserService.get", "--no-doc"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Look up a user by id" not in out
    assert "def get" in out


# --- show --view signature -----------------------------------------------


def test_show_signature_view_csharp_omits_body(csharp_dir, capsys):
    """`--view signature` returns docs + attrs + signature, no method body.

    The agent's "I want the contract, not the implementation" view: useful
    after `digest` when the symbol name is known but the body would burn
    context. Body lines like the `{` / `}` and statements inside MUST NOT
    appear; the signature line and its leading XML doc MUST."""
    rc = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--view",
            "signature",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # XML doc + signature are present
    assert "/// <summary>Apply damage" in out
    assert "public void TakeDamage(int amount)" in out
    # Body content is NOT
    assert "CurrentHealth -=" not in out
    assert "OnHealthChanged" not in out


def test_show_signature_alias_equals_view_signature(csharp_dir, capsys):
    """`--signature` is a flag alias for `--view signature` — both should
    produce byte-identical output. If they ever diverge the agent gets a
    confusing UX where two equivalent forms behave differently."""
    rc1 = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--signature",
        ]
    )
    out1 = capsys.readouterr().out
    rc2 = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--view",
            "signature",
        ]
    )
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert out1 == out2


def test_show_full_alias_equals_default(csharp_dir, capsys):
    """`--full` is a flag alias for `--view full` (the default). Output must
    match a no-flag invocation byte-for-byte — guard against accidental
    divergence in the depth-routing branch."""
    rc1 = main(
        ["show", str(csharp_dir / "unity_behaviour.cs"), "HeroController.TakeDamage"]
    )
    out1 = capsys.readouterr().out
    rc2 = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--full",
        ]
    )
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert out1 == out2


def test_show_view_aliases_are_mutually_exclusive(csharp_dir, capsys):
    """argparse's mutex group rejects `--signature --full` so the agent can
    never accidentally pass both. The CLI's LLM-friendly error path turns
    the parse failure into a `# note:` on stdout with rc=0."""
    rc = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--signature",
            "--full",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "not allowed" in captured.out.lower()


def test_show_signature_view_python_keeps_docstring_after_sig(python_dir, capsys):
    """Python docstrings live INSIDE the body in source, but `outline` and
    signature-view both render them AFTER the signature with +1 indent —
    same `docs_inside` placement as the outline render. Verifies signature
    view tracks outline's doc placement, not C#'s."""
    rc = main(
        [
            "show",
            str(python_dir / "domain_model.py"),
            "UserService.get",
            "--signature",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # Signature comes first
    sig_idx = out.find("def get")
    doc_idx = out.find("Look up a user by id")
    assert sig_idx >= 0 and doc_idx >= 0
    assert sig_idx < doc_idx
    # No method body content
    assert "return self" not in out
    assert "raise " not in out


def test_show_signature_view_strips_docs_with_no_doc(csharp_dir, capsys):
    """`--no-doc` composes with `--signature`: the XML doc lines disappear,
    only attrs+signature remain. Useful when the agent already has the doc
    elsewhere and just wants the bare contract line."""
    rc = main(
        [
            "show",
            str(csharp_dir / "unity_behaviour.cs"),
            "HeroController.TakeDamage",
            "--signature",
            "--no-doc",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "/// <summary>" not in out
    assert "public void TakeDamage(int amount)" in out


# --- show <dir> <symbol> -------------------------------------------------
#
# When `show` is pointed at a directory it locates the symbol's
# definition(s) itself (the `grep <symbol> DIR --kind def` an agent would
# otherwise run as a second call) and shows the body in one call.


def _write(dir_path, name, text):
    p = dir_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_show_dir_single_definition_prints_body_and_note(tmp_path, capsys):
    """A symbol defined in exactly one file under the dir → its body, plus
    a `# note: found ... in <file>` naming where it was located."""
    _write(tmp_path, "noise.cs", "namespace N { class Other { } }\n")
    _write(
        tmp_path,
        "mail.cs",
        "namespace N {\n"
        "    public class MailSpec {\n"
        "        public int Id;\n"
        "    }\n"
        "}\n",
    )
    rc = main(["show", str(tmp_path), "MailSpec"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    # Body is present
    assert "public class MailSpec" in out
    assert "public int Id;" in out
    # Note names the file the definition was found in
    assert "# note: found 'MailSpec'" in out
    assert "mail.cs" in out
    # The unrelated file's class is not shown
    assert "class Other" not in out


def test_show_dir_multiple_definitions_lists_candidates(tmp_path, capsys):
    """A symbol defined in several files → `show` prints a `# note:` listing
    the candidate locations and asking the agent to re-run against one — it
    does NOT dump every body. `show` prints code OR a pointer, never both."""
    _write(
        tmp_path,
        "a.cs",
        "namespace A { public class Widget { int A; } }\n",
    )
    _write(
        tmp_path,
        "b.cs",
        "namespace B { public class Widget { int B; } }\n",
    )
    rc = main(["show", str(tmp_path), "Widget"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    # The note states the count and asks the agent to re-run against one.
    assert "# note: 2 definitions of 'Widget'" in out
    assert "re-run with one of:" in out
    # Both candidate files are named in the list (with line/kind locators).
    assert "a.cs" in out
    assert "b.cs" in out
    assert "(class)" in out
    # No code body is printed — neither definition's distinctive field leaks.
    assert "int A;" not in out
    assert "int B;" not in out


def test_show_dir_symbol_not_found_returns_zero_with_note(tmp_path, capsys):
    """No definition under the dir → exit 0 + `# note:` (LLM-friendly)."""
    _write(tmp_path, "x.cs", "namespace N { class Present { } }\n")
    rc = main(["show", str(tmp_path), "Absent"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "not found" in captured.out.lower()
    assert "Absent" in captured.out


def test_show_dir_not_found_suggests_similar(tmp_path, capsys):
    """A near-miss name → did-you-mean hint, reusing grep's suggester."""
    _write(
        tmp_path,
        "mail.cs",
        "namespace N { public class MailSpec { } }\n",
    )
    rc = main(["show", str(tmp_path), "MailSpecc"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# hint: did you mean:" in captured.out
    assert "MailSpec" in captured.out


def test_show_dir_substring_collision_does_not_leak(tmp_path, capsys):
    """`MailSpec` must not resolve to a `MailSpecHelper` definition — the
    grep pre-filter may collect that file (substring), but `find_symbols`
    exact-matches the name token and drops it."""
    _write(
        tmp_path,
        "helper.cs",
        "namespace N { public class MailSpecHelper { int H; } }\n",
    )
    rc = main(["show", str(tmp_path), "MailSpec"])
    captured = capsys.readouterr()
    assert rc == 0
    # No real `MailSpec`, only `MailSpecHelper` → not found, body not leaked
    assert "not found" in captured.out.lower()
    assert "int H;" not in captured.out


def test_show_dir_respects_signature_flag(tmp_path, capsys):
    """`--signature` (and other show flags) apply to the dir-located file:
    contract only, no body."""
    _write(
        tmp_path,
        "svc.cs",
        "namespace N {\n"
        "    public class Service {\n"
        "        public void Run() { DoWork(); }\n"
        "    }\n"
        "}\n",
    )
    rc = main(["show", str(tmp_path), "Run", "--signature"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "public void Run()" in captured.out
    # Body statement is omitted in signature view
    assert "DoWork();" not in captured.out


def test_show_dir_respects_no_doc_flag(tmp_path, capsys):
    """`--no-doc` strips the leading doc block from a dir-located symbol,
    same as in file mode (the body renderer is shared)."""
    _write(
        tmp_path,
        "doc.cs",
        "namespace N {\n"
        "    /// <summary>Important.</summary>\n"
        "    public class Documented { int X; }\n"
        "}\n",
    )
    rc = main(["show", str(tmp_path), "Documented", "--no-doc"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "public class Documented" in captured.out
    assert "/// <summary>" not in captured.out


def test_show_dir_no_ignore_reaches_ignored_dir(tmp_path, capsys):
    """By default the directory search honors .gitignore; `--no-ignore`
    lets it reach a symbol defined in an ignored folder."""
    (tmp_path / ".gitignore").write_text("hidden/\n", encoding="utf-8")
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    _write(
        hidden,
        "buried.cs",
        "namespace N { public class Buried { int Z; } }\n",
    )
    # Default: the symbol's file is ignored → not found.
    rc = main(["show", str(tmp_path), "Buried"])
    out_default = capsys.readouterr().out
    assert rc == 0
    assert "not found" in out_default.lower()
    # With --no-ignore: the symbol is located and its body shown.
    rc = main(["show", str(tmp_path), "Buried", "--no-ignore"])
    out_no_ignore = capsys.readouterr().out
    assert rc == 0
    assert "public class Buried" in out_no_ignore
    assert "int Z;" in out_no_ignore


def test_show_dir_multiple_symbols(tmp_path, capsys):
    """`show DIR sym1 sym2` resolves each symbol independently."""
    _write(
        tmp_path,
        "two.cs",
        "namespace N {\n"
        "    public class Alpha { int A; }\n"
        "    public class Beta { int B; }\n"
        "}\n",
    )
    rc = main(["show", str(tmp_path), "Alpha", "Beta"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "class Alpha" in captured.out
    assert "class Beta" in captured.out


def test_show_dir_mixed_arity_per_symbol_independent(tmp_path, capsys):
    """`show DIR a b` applies the print-code-or-pointer rule per symbol:
    a symbol with one definition prints its body, a symbol with several
    prints the candidate list — both in the same output."""
    _write(
        tmp_path,
        "uniq.cs",
        "namespace N { public class Solo { int Only; } }\n",
    )
    _write(
        tmp_path,
        "a.cs",
        "namespace A { public class Dup { int A; } }\n",
    )
    _write(
        tmp_path,
        "b.cs",
        "namespace B { public class Dup { int B; } }\n",
    )
    rc = main(["show", str(tmp_path), "Solo", "Dup"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    # `Solo` (N=1) prints its body with a `found … in` note.
    assert "# note: found 'Solo'" in out
    assert "public class Solo" in out
    assert "int Only;" in out
    # `Dup` (N=2) prints a candidate list, no body.
    assert "# note: 2 definitions of 'Dup'" in out
    assert "re-run with one of:" in out
    assert "int A;" not in out
    assert "int B;" not in out


def test_show_dir_json_ambiguous_lists_candidates_without_bodies(tmp_path, capsys):
    """`--json` over a directory with N>1 definitions: valid JSON, a
    `directory` locator, the result flagged `ambiguous`, each candidate
    tagged with its own `file` — and NO `source` body on any match (the
    JSON mirrors the text contract: a list, not code)."""
    import json

    _write(
        tmp_path,
        "a.cs",
        "namespace A { public class Widget { int A; } }\n",
    )
    _write(
        tmp_path,
        "b.cs",
        "namespace B { public class Widget { int B; } }\n",
    )
    rc = main(["show", str(tmp_path), "Widget", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["command"] == "show"
    assert "directory" in doc
    results = doc["results"]
    assert len(results) == 1
    assert results[0]["query"] == "Widget"
    assert results[0]["ambiguous"] is True
    matches = results[0]["matches"]
    assert len(matches) == 2
    # Each candidate carries its own file + a line/kind locator.
    files = {m["file"] for m in matches}
    assert any(f.endswith("a.cs") for f in files)
    assert any(f.endswith("b.cs") for f in files)
    for m in matches:
        assert {"file", "kind", "start_line", "end_line"} <= set(m)
        # No code body anywhere — the whole point of the ambiguous branch.
        assert "source" not in m
    # The re-run guidance is also echoed in the envelope notes.
    assert any("re-run with one of" in n for n in doc["notes"])
    # And the dumped field values must not appear anywhere in the JSON text.
    assert "int A;" not in captured.out
    assert "int B;" not in captured.out


def test_show_dir_json_not_found_carries_did_you_mean(tmp_path, capsys):
    """A miss in `--json` mode → empty matches + a did-you-mean note."""
    import json

    _write(
        tmp_path,
        "mail.cs",
        "namespace N { public class MailSpec { } }\n",
    )
    rc = main(["show", str(tmp_path), "MailSpecc", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["results"][0]["matches"] == []
    assert any("did you mean" in n.lower() for n in doc["notes"])


# --- show <glob> <symbol> ------------------------------------------------
#
# A quoted glob (the shell didn't expand it) is expanded by `show` itself
# and the matched files are searched like a directory.


def test_show_glob_finds_symbol(tmp_path, capsys):
    """`show "<dir>/*.cs" <symbol>` expands the glob and shows the body,
    with the same `# note: found … in <file>`."""
    _write(tmp_path, "a.cs", "namespace A { class Other { } }\n")
    _write(
        tmp_path,
        "mail.cs",
        "namespace N { public class MailSpec { int Id; } }\n",
    )
    rc = main(["show", str(tmp_path / "*.cs"), "MailSpec"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "public class MailSpec" in captured.out
    assert "# note: found 'MailSpec'" in captured.out
    assert "mail.cs" in captured.out


def test_show_glob_recursive_double_star(tmp_path, capsys):
    """A recursive `**` glob reaches nested directories."""
    nested = tmp_path / "deep" / "deeper"
    nested.mkdir(parents=True)
    _write(nested, "buried.cs", "namespace N { public class Buried { int Z; } }\n")
    rc = main(["show", str(tmp_path / "**" / "*.cs"), "Buried"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "public class Buried" in captured.out
    assert "int Z;" in captured.out


def test_show_glob_no_files_match_returns_zero(tmp_path, capsys):
    """A glob that matches nothing → `# note: no files match glob`, exit 0."""
    _write(tmp_path, "a.cs", "namespace A { class A1 { } }\n")
    rc = main(["show", str(tmp_path / "*.py"), "Anything"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no files match glob" in captured.out.lower()


def test_show_glob_symbol_absent_reports_glob_scope(tmp_path, capsys):
    """Glob matches files but the symbol is absent → not-found note naming
    the glob as the scope; exit 0."""
    _write(tmp_path, "a.cs", "namespace A { public class Present { } }\n")
    pattern = str(tmp_path / "*.cs")
    rc = main(["show", pattern, "Absent"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "not found" in captured.out.lower()
    assert pattern in captured.out


def test_show_glob_json_sets_glob_not_directory(tmp_path, capsys):
    """`--json` over a glob: `directory` empty, `glob` carries the pattern,
    each match carries its own `file`."""
    import json

    _write(tmp_path, "mail.cs", "namespace N { public class MailSpec { } }\n")
    pattern = str(tmp_path / "*.cs")
    rc = main(["show", pattern, "MailSpec", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["command"] == "show"
    assert doc["directory"] == ""
    assert doc["glob"] == pattern
    matches = doc["results"][0]["matches"]
    assert matches and matches[0]["file"].endswith("mail.cs")


def test_show_missing_plain_path_still_file_not_found(tmp_path, capsys):
    """A non-glob path that doesn't exist keeps the precise `file not
    found` note — the glob branch must not swallow it."""
    rc = main(["show", str(tmp_path / "nope.cs"), "Foo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "file not found" in captured.out.lower()
    assert "no files match glob" not in captured.out.lower()


def test_show_file_mode_unaffected_by_dir_branch(csharp_dir, capsys):
    """Negative guard: a plain `show <file> <symbol>` must not enter the
    directory branch — same body, same `# note:`-free success as before."""
    rc = main(
        ["show", str(csharp_dir / "unity_behaviour.cs"), "HeroController.TakeDamage"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "public void TakeDamage" in captured.out
    # The dir-mode "found ... in" note must NOT appear for a file target
    assert "# note: found" not in captured.out


# --- digest --------------------------------------------------------------


def test_digest_directory(csharp_dir, capsys):
    rc = main(["digest", str(csharp_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HeroController" in out
    # Callables render with `()` suffix and no `+` prefix.
    assert "TakeDamage()" in out


def test_digest_missing_path_returns_zero_with_note(tmp_path, capsys):
    """LLM-friendly mode: missing path yields rc=0 + ``# note:`` on stdout."""
    rc = main(["digest", str(tmp_path / "nope")])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "not found" in captured.out.lower()


def test_digest_include_private(csharp_dir, capsys):
    rc = main(["digest", str(csharp_dir), "--include-private"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Die()" in out


# --- digest --format presets --------------------------------------------


def test_digest_format_names_one_line_per_file(csharp_dir, capsys):
    """`--format=names` collapses each file to one comma-separated line —
    no methods, no `()`, no line ranges, no `: Base`."""
    rc = main(["digest", str(csharp_dir), "--format=names"])
    out = capsys.readouterr().out
    assert rc == 0
    # HeroController appears as a top-level type name, but its method
    # `TakeDamage()` must not appear (no callables in names format).
    assert "HeroController" in out
    assert "TakeDamage" not in out
    assert "()" not in out


def test_digest_oneline_alias_matches_format_names(csharp_dir, capsys):
    """`--oneline` is the CLI alias for `--format=names`. Output is
    byte-identical to the explicit form."""
    rc = main(["digest", str(csharp_dir), "--oneline"])
    out_alias = capsys.readouterr().out
    assert rc == 0

    rc = main(["digest", str(csharp_dir), "--format=names"])
    out_explicit = capsys.readouterr().out
    assert rc == 0
    assert out_alias == out_explicit


def test_digest_format_compact_drops_line_ranges_and_blanks(csharp_dir, capsys):
    """`--format=compact` removes `L<a>-<b>` suffixes and the blank
    paragraph break between types-with-members."""
    rc = main(["digest", str(csharp_dir), "--format=compact"])
    out = capsys.readouterr().out
    assert rc == 0
    import re
    assert not re.search(r"L\d+-\d+", out), \
        f"compact must not emit line ranges, got:\n{out}"
    # Per-file counters dropped: header must not carry `, X types`.
    header_lines = [
        line for line in out.splitlines()
        if "lines" in line and "tokens" in line
    ]
    for line in header_lines:
        assert "types" not in line
        assert "methods" not in line
        assert "fields" not in line


def test_digest_format_wide_preset_enables_private_and_fields(csharp_dir, capsys):
    """`--format=wide` is a CLI preset that turns on `--include-private`
    and `--include-fields` (and lifts max-members). Private methods like
    `Die()` and field tokens must surface."""
    rc = main(["digest", str(csharp_dir), "--format=wide"])
    out = capsys.readouterr().out
    assert rc == 0
    # Same private-method check as the explicit `--include-private` test
    # above — wide must produce equivalent output here.
    assert "Die()" in out


def test_digest_format_default_back_compat_unchanged(csharp_dir, capsys):
    """`--format=default` and the bare `digest` invocation must produce
    byte-identical output — back-compat anchor for every existing skill
    that parses digest stdout."""
    rc = main(["digest", str(csharp_dir)])
    out_omitted = capsys.readouterr().out
    assert rc == 0

    rc = main(["digest", str(csharp_dir), "--format=default"])
    out_explicit = capsys.readouterr().out
    assert rc == 0
    assert out_omitted == out_explicit


def test_digest_explicit_include_private_overrides_names_preset(python_dir, capsys):
    """`--oneline --include-private` must include private symbols even
    though the `names` preset defaults `include_private` to False. This
    pins the kubectl-style override: explicit flag wins over preset."""
    rc = main(["digest", str(python_dir / "domain_model.py"), "--oneline", "--include-private"])
    out = capsys.readouterr().out
    assert rc == 0
    # `_encode` is a private free function in domain_model.py — must
    # appear because the explicit `--include-private` overrides the
    # names preset's default of False.
    assert "_encode" in out


def test_digest_explicit_max_members_overrides_wide_preset(csharp_dir, capsys):
    """`--format=wide --max-members 1` must use the explicit cap of 1
    instead of wide's `10**9`. Same override rule as include-private."""
    rc = main(["digest", str(csharp_dir), "--format=wide", "--max-members", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    # The `... (N more)` suffix is the proof that the cap applied.
    assert "more)" in out


def test_digest_invalid_format_value_rejected_with_note(csharp_dir, capsys):
    """An unknown `--format=` value is caught by argparse's `choices=`.
    Same LLM-friendly error path as every other bad arg: rc=0 with a
    `# note:` line on stdout. This pins the contract — adding a new
    format must extend the `choices` list rather than relying on a
    free-form string."""
    rc = main(["digest", str(csharp_dir), "--format=verbose"])  # not a valid choice
    out = capsys.readouterr().out
    assert rc == 0
    assert "# note:" in out
    assert "invalid choice" in out.lower() or "--format" in out


# --- LLM-friendly error handling -----------------------------------------


def test_bad_subcommand_returns_zero_with_note(capsys):
    """A bogus subcommand must NOT call ``sys.exit`` with a non-zero code —
    that breaks parallel bash chains in Claude Code. Instead we expect a
    ``# note:`` line on stdout and rc=0."""
    rc = main(["help", "doesnotexist"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out


def test_cross_command_flag_hint_signature_on_outline(tmp_path, capsys):
    """When an LLM passes a flag belonging to a sibling subcommand, the note
    should name the right command instead of just "unrecognized arguments"."""
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n")
    rc = main(["outline", str(f), "--signature"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "`--signature` is a flag of `show`" in captured.out
    assert "not `outline`" in captured.out


def test_cross_command_flag_hint_absent_for_truly_unknown_flag(tmp_path, capsys):
    """A flag that exists nowhere should NOT get a hint — only flags that
    legitimately live on a different subcommand qualify."""
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n")
    rc = main(["outline", str(f), "--definitely-not-a-flag"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "hint:" not in captured.out


def test_cross_command_flag_hint_with_equals_form(tmp_path, capsys):
    """``--flag=value`` form should be recognized — the hint extractor
    must strip the ``=value`` suffix before looking up the flag."""
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n")
    rc = main(["outline", str(f), "--view=signature"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "`--view` is a flag of `show`" in captured.out
    assert "not `outline`" in captured.out


def test_cross_command_flag_hint_short_flag(tmp_path, capsys):
    """Short POSIX-style flags from another subcommand also get a hint —
    `-l` is a `grep` flag (files-with-matches), not an `outline` flag."""
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n")
    rc = main(["outline", str(f), "-l"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "`-l` is a flag of `grep`" in captured.out


def test_cross_command_flag_hint_lists_all_owners(tmp_path, capsys):
    """When a flag lives on multiple sibling commands (``--imports`` is on
    both ``outline`` and ``digest``), the hint names all owners — agents
    can pick whichever fits their workflow."""
    f = tmp_path / "sample.py"
    f.write_text("def foo():\n    pass\n")
    rc = main(["show", str(f), "foo", "--imports"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "`--imports` is a flag of" in captured.out
    assert "`outline`" in captured.out
    assert "`digest`" in captured.out
    assert "not `show`" in captured.out


def test_show_missing_file_returns_zero_with_note(tmp_path, capsys):
    rc = main(["show", str(tmp_path / "absent.cs"), "Foo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "file not found" in captured.out.lower()


def test_show_unsupported_extension_returns_zero_with_note(tmp_path, capsys):
    """A file with an unsupported extension is a no-op, not a crash."""
    f = tmp_path / "hello.txt"
    f.write_text("not source code")
    rc = main(["show", str(f), "anything"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# note:" in captured.out
    assert "no adapter" in captured.out.lower()


# --- All-files-fail visibility -------------------------------------------
#
# Regression: if every file in a batch raised during `adapter.parse`,
# stdout used to be empty (warnings went only to stderr) and an LLM
# harness reading stdout saw `(no output)`. The CLI promises
# rc=0 + a `# note:` line on stdout for any user-facing failure, so an
# all-failure batch must surface the parse errors there too.


class _BoomAdapter:
    """Adapter stub that claims `.yml` and always raises on parse."""
    language_name = "yaml"
    extensions = {".yml", ".yaml"}

    def parse(self, path):
        raise RuntimeError(f"boom on {path}")


def test_outline_all_files_fail_emits_notes_on_stdout(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    a.write_text("k: 1\n")
    b.write_text("k: 2\n")
    monkeypatch.setattr("ast_outline.adapters.ADAPTERS", [_BoomAdapter()])

    rc = main(["outline", str(a), str(b)])
    captured = capsys.readouterr()
    assert rc == 0
    # Both files surface as `# note:` lines on stdout — the channel the
    # LLM agent reads. No silent empty stdout.
    assert captured.out.count("# note: parse error in") == 2
    assert str(a) in captured.out
    assert str(b) in captured.out


def test_digest_all_files_fail_emits_notes_on_stdout(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    a.write_text("k: 1\n")
    b.write_text("k: 2\n")
    monkeypatch.setattr("ast_outline.adapters.ADAPTERS", [_BoomAdapter()])

    rc = main(["digest", str(a), str(b)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.count("# note: parse error in") == 2
    # Should NOT print the misleading `# no files` line from
    # `render_digest([])` when files were present but all failed.
    assert "# no files" not in captured.out


