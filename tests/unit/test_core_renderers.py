"""Tests for render_outline and render_digest."""
from __future__ import annotations

from pathlib import Path

from ast_outline.adapters.csharp import CSharpAdapter
from ast_outline.adapters.python import PythonAdapter
from ast_outline.core import (
    Declaration,
    DigestOptions,
    OutlineOptions,
    display_path,
    render_digest,
    render_outline,
)


# --- lines_suffix --------------------------------------------------------


def test_lines_suffix_single_line():
    d = Declaration(kind="field", name="x", signature="int x", start_line=42, end_line=42)
    assert d.lines_suffix() == "  L42"


def test_lines_suffix_multi_line():
    d = Declaration(kind="method", name="Foo", signature="void Foo()", start_line=42, end_line=58)
    assert d.lines_suffix() == "  L42-58"


def test_lines_suffix_empty_when_no_lines():
    d = Declaration(kind="class", name="X", signature="class X")
    assert d.lines_suffix() == ""


# --- render_outline: C# ---------------------------------------------------


def test_outline_header_has_path_and_line_count(csharp_dir):
    path = csharp_dir / "unity_behaviour.cs"
    r = CSharpAdapter().parse(path)
    out = render_outline(r, OutlineOptions())
    first = out.splitlines()[0]
    # Header shows the file path in display form (relative to cwd when the
    # file lives under it — so the absolute prefix isn't repeated per file).
    assert display_path(path) in first
    # Header wraps line count with summary counters: "(N lines, X types, ...)"
    assert f"{r.line_count} lines" in first


def test_display_path_relativizes_under_cwd():
    """`display_path` collapses a path under cwd to its relative form (so
    the absolute prefix isn't repeated per file), and returns paths
    elsewhere — and already-relative inputs — unchanged so they stay
    resolvable. Pure path arithmetic: the files need not exist."""
    cwd = Path.cwd()
    # Under cwd → relative.
    assert display_path(cwd / "src" / "foo.py") == "src/foo.py"
    # Absolute path outside cwd → unchanged (the A1 fallback).
    outside = Path("/nonexistent-root-xyz/bar.py")
    assert display_path(outside) == str(outside)
    # Already-relative input (the --no-ignore walk shape) → unchanged.
    assert display_path(Path("rel/baz.py")) == "rel/baz.py"


def test_outline_includes_line_suffixes_by_default(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_outline(r, OutlineOptions())
    # Signatures should have L<a>-<b> suffixes
    assert "  L" in out
    assert "HeroController" in out


def test_outline_no_lines_flag_suppresses_suffixes(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_outline(r, OutlineOptions(include_line_numbers=False))
    # No line-number suffixes anywhere in the output except the header's line count
    body = "\n".join(out.splitlines()[1:])
    assert "  L" not in body


def test_outline_no_private_hides_private_members(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    without = render_outline(r, OutlineOptions(include_private=False))
    with_priv = render_outline(r, OutlineOptions(include_private=True))
    assert "Die" not in without
    assert "Die" in with_priv


def test_outline_no_fields_hides_field_declarations(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_outline(r, OutlineOptions(include_fields=False))
    # _speed is a field; should vanish
    assert "_speed" not in out
    # But methods remain
    assert "TakeDamage" in out


def test_outline_no_docs_suppresses_xml_doc(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    with_docs = render_outline(r, OutlineOptions(include_xml_doc=True))
    without = render_outline(r, OutlineOptions(include_xml_doc=False))
    assert "<summary>" in with_docs
    assert "<summary>" not in without


def test_outline_no_attrs_suppresses_inline_attributes(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    with_attrs = render_outline(r, OutlineOptions(include_attributes=True))
    without = render_outline(r, OutlineOptions(include_attributes=False))
    assert "[RequireComponent" in with_attrs
    assert "[RequireComponent" not in without
    # Non-attribute tokens should still be present
    assert "HeroController" in without


# --- render_outline: Python docstring positioning ------------------------


def test_python_docstring_rendered_after_signature_with_indent(python_dir):
    r = PythonAdapter().parse(python_dir / "domain_model.py")
    out = render_outline(r, OutlineOptions())
    lines = out.splitlines()
    # Find the `class Repository` line
    class_idx = next(i for i, line in enumerate(lines) if "class Repository" in line)
    # Next non-empty line should be the docstring, indented deeper than the class
    next_line = lines[class_idx + 1]
    assert '"""' in next_line
    class_indent = len(lines[class_idx]) - len(lines[class_idx].lstrip())
    doc_indent = len(next_line) - len(next_line.lstrip())
    assert doc_indent > class_indent


# --- render_digest --------------------------------------------------------


def test_digest_groups_by_directory(fixtures_dir):
    paths = [
        fixtures_dir / "csharp" / "unity_behaviour.cs",
        fixtures_dir / "csharp" / "file_scoped_ns.cs",
        fixtures_dir / "python" / "domain_model.py",
    ]
    results = [
        CSharpAdapter().parse(paths[0]),
        CSharpAdapter().parse(paths[1]),
        PythonAdapter().parse(paths[2]),
    ]
    out = render_digest(results, DigestOptions())
    # Two dir headers: csharp/ and python/
    assert "csharp/" in out
    assert "python/" in out


def test_digest_lists_type_with_member_tokens(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    # HeroController header; callables render with `()` (LLM-native marker).
    assert "HeroController" in out
    assert "TakeDamage()" in out


def test_digest_excludes_private_by_default(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    # Die is private; should not appear in digest at all.
    assert "Die()" not in out


def test_digest_includes_private_when_asked(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions(include_private=True))
    assert "Die()" in out


def test_digest_free_module_functions_in_python(python_dir):
    r = PythonAdapter().parse(python_dir / "domain_model.py")
    out = render_digest([r], DigestOptions())
    # Module-level public def should be listed as a free function.
    assert "public_helper()" in out


def test_digest_truncates_with_max_members(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "file_scoped_ns.cs")
    out = render_digest([r], DigestOptions(max_members_per_type=1))
    # UserRepository has 4 public members; with max=1 we expect the
    # "... (N more)" trunc marker.
    import re
    assert re.search(r"\.\.\.\s+\(\d+\s+more\)", out), out


def test_digest_header_includes_line_range(csharp_dir):
    r = CSharpAdapter().parse(csharp_dir / "unity_behaviour.cs")
    out = render_digest([r], DigestOptions())
    # Each type header ends with L<a>-<b>. Names are qualified with the
    # namespace, so we search for "HeroController" (not the whole prefix).
    import re
    assert re.search(r"HeroController[^\n]*L\d+-\d+", out), out


def test_digest_handles_empty_file(tmp_path):
    empty = tmp_path / "empty.py"
    empty.write_text("# nothing here\n")
    r = PythonAdapter().parse(empty)
    out = render_digest([r], DigestOptions())
    assert "# no declarations" in out
