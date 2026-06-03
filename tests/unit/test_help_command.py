"""Tests for the supported-languages listing in `ast-outline help`.

Two help sections list the languages the tool parses: the table in the
general guide (`ast-outline help`) and the compact line in the outline
guide (`ast-outline help outline`). Both are built from the adapter
registry, not hand-maintained — these tests are the guard that keeps
them honest:

- Every registered adapter has a non-empty ``display_name``. The
  attribute is declared on the ``LanguageAdapter`` Protocol, but a
  Protocol annotation is not enforced at runtime — without this check a
  new adapter could ship missing the field and the help renderer would
  raise (or, worse, the field could be an empty string and slip by).
- Both help sections name every adapter — by display name *and* by at
  least one of its extensions. A prior hand-written version of this
  table silently dropped C++, Rust, PHP, Ruby, CSS, SCSS and SQL; the
  registry-derived listing plus this test make that drift impossible.
"""
from __future__ import annotations

import pytest

from ast_outline.adapters import ADAPTERS
from ast_outline.cli import main


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
def test_every_adapter_has_display_name(adapter):
    assert getattr(adapter, "display_name", ""), (
        f"{type(adapter).__name__} is missing a non-empty display_name"
    )


def _help_output(capsys, *args) -> str:
    rc = main(["help", *args])
    assert rc == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
def test_general_help_lists_every_adapter(capsys, adapter):
    out = _help_output(capsys)
    assert adapter.display_name in out, (
        f"general help omits display name {adapter.display_name!r}"
    )
    assert any(ext in out for ext in adapter.extensions), (
        f"general help names none of {sorted(adapter.extensions)} "
        f"for {adapter.display_name}"
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
def test_outline_help_lists_every_adapter(capsys, adapter):
    out = _help_output(capsys, "outline")
    assert adapter.display_name in out, (
        f"outline help omits display name {adapter.display_name!r}"
    )
    assert any(ext in out for ext in adapter.extensions), (
        f"outline help names none of {sorted(adapter.extensions)} "
        f"for {adapter.display_name}"
    )
