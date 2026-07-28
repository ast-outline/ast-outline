"""Regression tests for how grammar handles reach `tree_sitter.Language`.

Issue #8: on 64-bit Windows `ast-outline help` died at import time with
``OverflowError: Python int too large to convert to C unsigned long``.
tree-sitter-scss 1.0.0 hands out its `TSLanguage *` as a Python `int`,
and tree-sitter 0.26.0 converts that int back through
`PyLong_AsUnsignedLong` — 32 bits on Win64, so any real pointer
overflows. The capsule path has no such conversion.

The overflow itself only fires where `sizeof(long) < sizeof(void *)`,
so these tests assert the property that holds on every platform: we
never take the deprecated int path in the first place. `Language()`
emits a DeprecationWarning on that path, which makes it observable
here.
"""
from __future__ import annotations

import subprocess
import sys
import warnings

import pytest
import tree_sitter_scss as tssscss
from tree_sitter import Parser

from ast_outline.adapters.base import load_language


def test_load_language_wraps_raw_int_handle_in_a_capsule():
    handle = tssscss.language()
    if not isinstance(handle, int):
        pytest.skip("tree-sitter-scss now returns a capsule; int path is moot")

    with warnings.catch_warnings():
        # The deprecated int path warns; the capsule path does not. Any
        # warning here means we handed `Language()` the bare int again.
        warnings.simplefilter("error", DeprecationWarning)
        language = load_language(handle)

    # The capsule has to carry the *same* pointer, not just any pointer —
    # parsing real SCSS proves we wrapped the grammar and not garbage.
    tree = Parser(language).parse(b"@mixin m($a) { color: $a; }")
    assert not tree.root_node.has_error
    assert tree.root_node.type == "stylesheet"


def test_load_language_passes_capsule_handles_through():
    import tree_sitter_css as tscss

    handle = tscss.language()
    assert not isinstance(handle, int), "expected tree-sitter-css to return a capsule"
    tree = Parser(load_language(handle)).parse(b"a { color: red; }")
    assert not tree.root_node.has_error


def test_no_adapter_imports_a_language_via_the_deprecated_int_path():
    """Sweep every adapter, not just SCSS.

    Runs in a subprocess so the adapters are imported fresh under
    ``-W error::DeprecationWarning`` — in-process they are already
    cached by the time any test runs. If some other grammar package
    starts returning a raw int, this fails instead of shipping the
    Win64 crash again.
    """
    proc = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning",
         "-c", "import ast_outline.adapters"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
