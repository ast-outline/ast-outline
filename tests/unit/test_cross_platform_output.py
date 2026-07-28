"""Output shape that must not depend on the platform we run on.

Two habits used to leak the host OS into results: CRLF source files left
a stray `\\r` on every line lifted out as text, and paths were rendered
with the native separator, so a Windows run emitted `src\\foo.py` where
every other platform emitted `src/foo.py`.

Both are exercised here without needing Windows: CRLF is a property of
the file, not the OS, and `PureWindowsPath` gives the Windows path
flavour on any host.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from ast_outline.adapters.go import GoAdapter
from ast_outline.core import Declaration, ParseResult, display_path
from ast_outline.json_output import _rel_path


GO_SOURCE_LINES = [
    "// Package doc.",
    "package main",
    "",
    "// Greet says hi.",
    "func Greet(name string) string {",
    '\treturn "hi"',
    "}",
]


def _write(path: Path, newline: str) -> Path:
    path.write_bytes(newline.join(GO_SOURCE_LINES).encode() + newline.encode())
    return path


def _all_declarations(decls):
    for d in decls:
        yield d
        yield from _all_declarations(d.children)


# --- CRLF -----------------------------------------------------------


def test_crlf_source_stays_byte_equal_to_the_file(tmp_path):
    """The cleanup must not touch the bytes.

    `ParseResult.source` is byte-equal to the file and `start_byte` /
    `end_byte` index into it — that is what `show` and the `--json`
    offsets promise, and the per-adapter metadata tests assert it. The
    carriage returns come off the rendered text, not the source.
    """
    crlf = _write(tmp_path / "crlf.go", "\r\n")
    result = GoAdapter().parse(crlf)
    assert result.source == crlf.read_bytes()

    decl = next(d for d in _all_declarations(result.declarations) if d.name == "Greet")
    on_disk = crlf.read_bytes()[decl.start_byte : decl.end_byte]
    assert on_disk.decode().startswith("func Greet")


def test_crlf_source_yields_the_same_rendered_shape_as_lf(tmp_path):
    """What we render must not remember which newline the file used."""
    lf = GoAdapter().parse(_write(tmp_path / "lf.go", "\n"))
    crlf = GoAdapter().parse(_write(tmp_path / "crlf.go", "\r\n"))

    def shape(result):
        return [
            (d.name, d.signature, tuple(d.docs), d.start_line, d.end_line)
            for d in _all_declarations(result.declarations)
        ]

    assert shape(crlf) == shape(lf)


def test_crlf_docs_carry_no_carriage_return(tmp_path):
    """The regression itself: `// Greet says hi.\\r` in the rendered doc."""
    result = GoAdapter().parse(_write(tmp_path / "crlf.go", "\r\n"))
    docs = [doc for d in _all_declarations(result.declarations) for doc in d.docs]
    assert docs, "expected the fixture's doc comment to be collected"
    assert not any("\r" in doc for doc in docs)


def test_crlf_cleanup_covers_text_assigned_after_construction():
    """Why the cleanup hangs off `ParseResult` and not `Declaration`.

    Several adapters keep rewriting a declaration after building it —
    Ruby and Elixir prepend pending docs, C++ and Scala prefix the
    signature. A hook on `Declaration.__post_init__` would run before
    those writes and miss them. Assembling the result last is what makes
    the cleanup total, so that ordering is pinned here directly rather
    than through whichever adapter happens to exercise it today.
    """
    decl = Declaration(kind="function", name="greet", signature="def greet()")
    child = Declaration(kind="function", name="inner", signature="def inner()")
    decl.children.append(child)
    # Written after construction, exactly as the adapters do it.
    decl.docs = ["# Greets a person.\r"]
    child.docs = ["# Nested.\r"]
    decl.signature = "def greet()\r"

    result = ParseResult(
        path=Path("greeter.rb"),
        language="ruby",
        source=b"# Greets a person.\r\n",
        line_count=1,
        declarations=[decl],
    )

    top = result.declarations[0]
    assert top.docs == ["# Greets a person."]
    assert top.signature == "def greet()"
    assert top.children[0].docs == ["# Nested."]


# --- path separators ------------------------------------------------


def test_display_path_uses_forward_slashes_outside_cwd():
    """A Windows-flavoured path renders posix-style on any host."""
    assert display_path(PureWindowsPath(r"C:\proj\src\foo.py")) == "C:/proj/src/foo.py"


def test_display_path_uses_forward_slashes_under_cwd():
    # The patch is lifted before asserting: pytest itself calls
    # `Path.cwd()` to build a failure report, and a Windows-flavoured cwd
    # crashes that machinery — a failing assert would surface as an
    # INTERNALERROR instead of the comparison.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "cwd", classmethod(lambda cls: PureWindowsPath(r"C:\proj")))
        rendered = display_path(PureWindowsPath(r"C:\proj\src\foo.py"))
    assert rendered == "src/foo.py"


def test_json_rel_path_uses_forward_slashes():
    root = PureWindowsPath(r"C:\proj")
    assert _rel_path(PureWindowsPath(r"C:\proj\src\foo.py"), root) == "src/foo.py"
    # Outside the root the path is kept whole — still posix-spelled.
    assert _rel_path(PureWindowsPath(r"D:\other\bar.py"), root) == "D:/other/bar.py"
