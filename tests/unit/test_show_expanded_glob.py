"""`show` must recover when the shell expanded an unquoted glob.

`ast-outline show *.cs Sym` (unquoted) is expanded by the shell *before*
argparse, so `show` receives `first.cs [rest.cs... Sym]`: the extra files
land in `args.symbols` and were searched as symbol names — producing bogus
`# note: symbol not found: Foo.cs` lines and looking the real symbol up only
in the first file. These tests pin the recovery: >=2 source-file positionals
are treated as an expanded glob and the symbol is searched across all of
them, the same intent as a quoted glob / directory target.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

from ast_outline.cli import main


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _cs_class(name: str, has_isempty: bool) -> str:
    body = "        public bool IsEmpty => true;\n" if has_isempty else "        public void Noop() {}\n"
    return f"namespace App {{\n    public class {name} {{\n{body}    }}\n}}\n"


def test_expanded_glob_searches_all_files(tmp_path, capsys):
    """The symbol is found across every file, not just the first, and no
    filename is echoed back as a missing symbol."""
    a = _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    b = _write(tmp_path / "B.cs", _cs_class("B", has_isempty=False))
    c = _write(tmp_path / "C.cs", _cs_class("C", has_isempty=True))
    assert main(["show", str(a), str(b), str(c), "IsEmpty"]) == 0
    out = capsys.readouterr().out
    # No filename mistaken for a symbol.
    assert "not found: B.cs" not in out
    assert "B.cs in" not in out
    # Both definitions (A and C) are surfaced — the first-file-only miss is gone.
    assert "A.cs" in out and "C.cs" in out
    # The advisory about the expansion is present.
    assert "expanded by the shell" in out


def test_expanded_glob_multiple_symbols(tmp_path, capsys):
    """files>=2 + several real symbols — a combination the old single-file
    path never supported. Every symbol resolves across the files."""
    a = _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    b = _write(
        tmp_path / "B.cs",
        "namespace App {\n    public class B {\n        public void Foo() {}\n    }\n}\n",
    )
    assert main(["show", str(a), str(b), "IsEmpty", "Foo"]) == 0
    out = capsys.readouterr().out
    assert "IsEmpty" in out and "Foo" in out
    assert "not found: B.cs" not in out


def test_expanded_glob_no_symbol_is_honest_note(tmp_path, capsys):
    """`show a.cs b.cs` (glob expanded, no symbol) → clear note, exit 0."""
    a = _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    b = _write(tmp_path / "B.cs", _cs_class("B", has_isempty=False))
    assert main(["show", str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "no symbol given" in out
    assert "quote it" in out


def test_single_file_two_symbols_not_regressed(tmp_path, capsys):
    """`show file.cs Sym1 Sym2` (one file, two symbols) stays the ordinary
    file-mode path — NOT treated as an expanded glob."""
    a = _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    assert main(["show", str(a), "IsEmpty", "Nope"]) == 0
    out = capsys.readouterr().out
    # Body printed for the real symbol.
    assert "public bool IsEmpty => true;" in out
    # The missing one gets the normal file-mode note (not the expansion note).
    assert "symbol not found: Nope" in out
    assert "expanded by the shell" not in out


def test_symbol_colliding_with_bare_file_no_misfire(tmp_path, capsys):
    """A symbol name that collides with an extension-less file in the cwd
    must NOT be reclassified as a file — the adapter-extension gate holds."""
    a = _write(tmp_path / "A.cs", "namespace App {\n    public class A {\n        public void IsEmpty() {}\n    }\n}\n")
    # A bare file literally named like the symbol, no recognized extension.
    _write(tmp_path / "IsEmpty", "not source code\n")
    assert main(["show", str(a), str(tmp_path / "IsEmpty")]) == 0
    out = capsys.readouterr().out
    # Only one real source file → not an expanded glob.
    assert "expanded by the shell" not in out


def test_expanded_glob_empty_common_root(tmp_path, capsys, monkeypatch):
    """commonpath('' case): files sharing only the cwd (relative bare names)
    still get a non-empty locator and valid JSON."""
    _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    _write(tmp_path / "B.cs", _cs_class("B", has_isempty=True))
    monkeypatch.chdir(tmp_path)
    assert main(["show", "--json", "A.cs", "B.cs", "IsEmpty"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    # Locator never empty; XOR of directory/glob holds (glob stays "").
    assert payload["directory"] != ""
    assert payload["glob"] == ""
    assert any("expanded by the shell" in n for n in payload["notes"])


def test_expanded_glob_dedups_repeated_file(tmp_path, capsys):
    """A file named twice (overlapping globs / repeated arg) must not turn
    one definition into a phantom '2 definitions' ambiguity."""
    a = _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    b = _write(tmp_path / "B.cs", _cs_class("B", has_isempty=False))
    # A.cs appears twice among the positionals.
    assert main(["show", str(a), str(a), str(b), "IsEmpty"]) == 0
    out = capsys.readouterr().out
    # Single real definition → body printed, NOT a "2 definitions" pointer.
    assert "2 definitions" not in out
    assert "public bool IsEmpty => true;" in out


def test_directory_target_not_hijacked(tmp_path, capsys, monkeypatch):
    """`show DIR a.cs b.cs` with a real directory first arg stays a
    directory-target search — the expanded-glob detector must not fire."""
    _write(tmp_path / "src" / "M.cs", "namespace App {\n    public class M {\n        public void Widget() {}\n    }\n}\n")
    # Two real files in cwd whose names could be mistaken for the file list.
    _write(tmp_path / "a.cs", _cs_class("A", has_isempty=True))
    _write(tmp_path / "b.cs", _cs_class("B", has_isempty=True))
    monkeypatch.chdir(tmp_path)
    assert main(["show", "src", "a.cs", "b.cs"]) == 0
    out = capsys.readouterr().out
    # Directory target, not an expanded-glob rescue.
    assert "expanded by the shell" not in out


def test_expanded_glob_mixed_abs_rel_no_crash(tmp_path, capsys, monkeypatch):
    """Mixed absolute/relative paths (commonpath raises ValueError) must not
    crash — exit 0, note printed."""
    other = _write(tmp_path / "other" / "B.cs", _cs_class("B", has_isempty=True))
    _write(tmp_path / "A.cs", _cs_class("A", has_isempty=True))
    monkeypatch.chdir(tmp_path)
    # A.cs relative, other/B.cs absolute → mixed.
    assert main(["show", "A.cs", str(other.resolve()), "IsEmpty"]) == 0
    out = capsys.readouterr().out
    assert "expanded by the shell" in out
