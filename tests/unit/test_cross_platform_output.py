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

from ast_outline.adapters.base import read_source
from ast_outline.adapters.go import GoAdapter
from ast_outline.core import display_path
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


def test_read_source_normalises_crlf(tmp_path):
    crlf = _write(tmp_path / "crlf.go", "\r\n")
    assert b"\r" not in read_source(crlf)


def test_crlf_source_yields_the_same_outline_as_lf(tmp_path):
    """The parse result must not remember which newline the file used."""
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
