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

import json
from pathlib import Path, PureWindowsPath

import pytest

from ast_outline.adapters import get_adapter_for
from ast_outline.adapters.go import GoAdapter
from ast_outline.cli import main
from ast_outline.core import (
    Declaration,
    OutlineOptions,
    ParseResult,
    _strip_cr,
    display_path,
    render_outline,
)
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


def test_no_backslash_paths_anywhere_in_json_envelopes(tmp_path, capsys):
    """Sweep the JSON output for native separators.

    Per-field assertions kept missing individual call sites — the `file`
    entries in a `show` candidate list and the envelope's own `root` were
    each overlooked once, and the pre-existing tests passed anyway
    because they matched with `endswith()`. Asserting over the whole
    payload covers the fields nobody thought to name.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
    (pkg / "b.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

    for argv in (
        ["outline", str(pkg), "--json"],
        ["digest", str(pkg), "--json"],
        ["grep", "Widget", str(pkg), "--json"],
        ["show", str(pkg), "Widget", "--json"],
    ):
        capsys.readouterr()
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        found = list(_every_path_value(payload))
        # Without this the sweep would pass vacuously if the envelope
        # ever renamed its path fields.
        assert found, f"{argv[0]}: no path fields found to check"
        for path_value in found:
            assert "\\" not in path_value, (argv[0], path_value)


def _every_path_value(node):
    """Yield every string under a key that carries a path."""
    path_keys = {"file", "path", "root", "directory"}
    if isinstance(node, dict):
        for key, value in node.items():
            if key in path_keys and isinstance(value, str) and value:
                yield value
            else:
                yield from _every_path_value(value)
    elif isinstance(node, list):
        for item in node:
            yield from _every_path_value(item)


def test_strip_cr_never_introduces_a_line_break():
    """A lone `\\r` is deleted, not turned into `\\n`.

    Turning it into a newline reads like the kinder choice — a classic
    pre-OS-X Mac file terminates lines with a bare `\\r` — but these
    strings render one per output line and some are single-line by
    contract, so a newline here truncates them at the break. See
    `test_crlf_yaml_block_scalar_still_renders_inline` for the case that
    caught it.
    """
    assert _strip_cr("// Greet says hi.\r") == "// Greet says hi."
    assert _strip_cr("first\r\nsecond\r\n") == "first\nsecond\n"
    assert _strip_cr("one\rtwo") == "onetwo"
    assert _strip_cr("no returns here") == "no returns here"


def test_crlf_yaml_block_scalar_still_renders_inline(tmp_path):
    """A CRLF YAML file must not lose its block-scalar content.

    The YAML adapter flattens a block scalar's newlines to spaces so the
    value fits one outline line. With CRLF input the carriage returns
    survive that flattening, and anything that later turns them into
    newlines cuts the rendered line short — the value vanished entirely,
    leaving a bare `config.yaml: |`.
    """
    path = tmp_path / "config.yaml"
    path.write_bytes(
        b"config.yaml: |\r\n  nested: value\r\n  multiline: yes\r\n"
    )
    rendered = render_outline(get_adapter_for(path).parse(path), OutlineOptions())

    line = next(ln for ln in rendered.splitlines() if "config.yaml:" in ln)
    assert "nested: value" in line
    assert "multiline: yes" in line
