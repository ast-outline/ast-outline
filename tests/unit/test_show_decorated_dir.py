"""Dir-mode `show <dir> <symbol>` must resolve DECORATED definitions.

`show <dir> <sym>` pre-filters the directory with `grep <name> --kind
def` before running the authoritative resolver, so a decorated class /
function that the def-classifier failed to tag would produce a bare
"symbol not found" from dir-mode even though file-mode `show` and
`outline` see it. These tests pin the fixed behavior end-to-end through
the CLI.
"""
from __future__ import annotations

from pathlib import Path

from ast_outline.cli import main


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_dir_show_finds_decorated_class(tmp_path, capsys):
    _write(
        tmp_path / "models.py",
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class ModelEntry:\n"
        "    name: str\n",
    )
    assert main(["show", str(tmp_path), "ModelEntry"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found" not in out
    assert "found 'ModelEntry' (class)" in out
    # The body is printed, decorators included (file-mode parity).
    assert "@dataclass(frozen=True)" in out
    assert "class ModelEntry:" in out


def test_dir_show_finds_decorated_function(tmp_path, capsys):
    _write(
        tmp_path / "cli.py",
        "@app.command(name='start')\n"
        "def llamacpp_start(port):\n"
        "    return port\n",
    )
    assert main(["show", str(tmp_path), "llamacpp_start"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found" not in out
    assert "found 'llamacpp_start' (function)" in out
    assert "def llamacpp_start(port):" in out


def test_dir_show_undecorated_sibling_still_resolves(tmp_path, capsys):
    """Control: the fix doesn't regress plain (undecorated) resolution."""
    _write(
        tmp_path / "util.py",
        "def stop_router():\n"
        "    return True\n",
    )
    assert main(["show", str(tmp_path), "stop_router"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found" not in out
    assert "def stop_router():" in out
