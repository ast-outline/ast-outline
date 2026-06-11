"""Shebang-based language detection for extensionless explicit file inputs.

Unix-convention CLI scripts (``#!/usr/bin/env python3`` in a file named
``tg``, not ``tg.py``) are a routine explicit input for LLM agents.
Before detection existed, agents symlinked such files to ``/tmp/x.py``
just to give them an extension — these tests pin the contract that the
sniff resolves the adapter directly, stays scoped to extensionless
explicit inputs (directory walks never pay for it), and that a failed
sniff explains itself instead of hiding behind the generic
supported-extensions note.
"""
from __future__ import annotations

import json
from pathlib import Path

from ast_outline.adapters import (
    get_adapter_for,
    shebang_interpreter,
    supported_shebang_programs,
)
from ast_outline.cli import main


def _script(p: Path, shebang: str, body: str = "def f(): pass\n") -> Path:
    p.write_text(f"{shebang}\n{body}")
    return p


# --- shebang_interpreter: line parsing ------------------------------------


def test_direct_interpreter_path(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/python3")
    assert shebang_interpreter(p) == "python"


def test_env_indirection(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env python3")
    assert shebang_interpreter(p) == "python"


def test_env_with_split_string_flag_and_interpreter_args(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env -S python3 -u")
    assert shebang_interpreter(p) == "python"


def test_env_with_var_assignment(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env -S PYTHONUNBUFFERED=1 python3")
    assert shebang_interpreter(p) == "python"


def test_uv_run_script_shebang(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env -S uv run --script")
    assert shebang_interpreter(p) == "uv"


def test_version_suffix_stripped(tmp_path):
    cases = {
        "#!/usr/bin/python3.13": "python",
        "#!/usr/bin/env lua5.4": "lua",
        "#!/usr/bin/env php8": "php",
        "#!/usr/bin/env ruby3.2": "ruby",
    }
    for shebang, expected in cases.items():
        p = _script(tmp_path / "tool", shebang)
        assert shebang_interpreter(p) == expected, shebang


def test_hyphenated_name_without_version_survives(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env ts-node")
    assert shebang_interpreter(p) == "ts-node"


def test_no_shebang_returns_none(tmp_path):
    p = tmp_path / "tool"
    p.write_text("plain text, no shebang\n")
    assert shebang_interpreter(p) is None


def test_empty_file_returns_none(tmp_path):
    p = tmp_path / "tool"
    p.write_text("")
    assert shebang_interpreter(p) is None


def test_binary_content_returns_none(tmp_path):
    p = tmp_path / "tool"
    p.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
    assert shebang_interpreter(p) is None


def test_unreadable_path_returns_none(tmp_path):
    # A directory raises OSError on open() — the helper must absorb it.
    assert shebang_interpreter(tmp_path) is None


def test_bare_env_with_nothing_after_returns_none(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env")
    assert shebang_interpreter(p) is None


def test_env_flag_with_separate_argument_is_skipped(tmp_path):
    # -u / -C / -P consume the next token — that token must not be
    # mistaken for the interpreter.
    cases = [
        "#!/usr/bin/env -u SOMEVAR python3",
        "#!/usr/bin/env -S -u SOMEVAR python3",
        "#!/usr/bin/env -C /tmp python3",
    ]
    for shebang in cases:
        p = _script(tmp_path / "tool", shebang)
        assert shebang_interpreter(p) == "python", shebang


def test_env_with_only_flags_and_no_program_returns_none(tmp_path):
    p = _script(tmp_path / "tool", "#!/usr/bin/env -i -u SOMEVAR")
    assert shebang_interpreter(p) is None


# --- get_adapter_for: resolution order and scope ---------------------------


def test_extensionless_python_script_resolves(tmp_path):
    p = _script(tmp_path / "tg", "#!/usr/bin/env python3")
    adapter = get_adapter_for(p)
    assert adapter is not None and adapter.language_name == "python"


def test_extensionless_node_script_resolves_to_typescript_adapter(tmp_path):
    p = _script(tmp_path / "cli", "#!/usr/bin/env node", "function f() {}\n")
    adapter = get_adapter_for(p)
    assert adapter is not None and adapter.language_name == "typescript"


def test_other_script_languages_resolve(tmp_path):
    cases = {
        "#!/usr/bin/env ruby": "ruby",
        "#!/usr/bin/env lua": "lua",
        "#!/usr/bin/env php": "php",
        "#!/usr/bin/env swift": "swift",
    }
    for shebang, language in cases.items():
        p = _script(tmp_path / "tool", shebang, "")
        adapter = get_adapter_for(p)
        assert adapter is not None and adapter.language_name == language, shebang


def test_unsupported_interpreter_returns_none(tmp_path):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo hi\n")
    assert get_adapter_for(p) is None


def test_extension_takes_precedence_over_shebang(tmp_path):
    # A .md file that happens to start with #! must stay markdown —
    # suffix is checked first, the sniff never runs.
    p = _script(tmp_path / "notes.md", "#!/usr/bin/env python3", "# Heading\n")
    adapter = get_adapter_for(p)
    assert adapter is not None and adapter.language_name == "markdown"


def test_unknown_suffix_blocks_the_sniff(tmp_path):
    # Detection is scoped to extensionless files: an unknown suffix
    # stays unknown even with a valid shebang inside.
    p = _script(tmp_path / "tool.weird", "#!/usr/bin/env python3")
    assert get_adapter_for(p) is None


def test_basename_branch_still_wins_for_rakefile(tmp_path):
    p = tmp_path / "Rakefile"
    p.write_text("task :default do\nend\n")
    adapter = get_adapter_for(p)
    assert adapter is not None and adapter.language_name == "ruby"


def test_supported_shebang_programs_is_sorted_union(tmp_path):
    programs = supported_shebang_programs()
    assert programs == sorted(programs)
    assert {"python", "uv", "node", "ruby"} <= set(programs)


# --- CLI end-to-end ---------------------------------------------------------


def test_outline_extensionless_python_script(tmp_path, capsys):
    p = _script(
        tmp_path / "tg", "#!/usr/bin/env python3",
        "class Router:\n    def route(self, msg): pass\n",
    )
    assert main(["outline", str(p)]) == 0
    out = capsys.readouterr().out
    assert "class Router" in out
    assert "# note:" not in out


def test_show_symbol_in_extensionless_script(tmp_path, capsys):
    p = _script(
        tmp_path / "tg", "#!/usr/bin/env python3",
        "class Router:\n    def route(self, msg): pass\n",
    )
    assert main(["show", str(p), "Router"]) == 0
    out = capsys.readouterr().out
    assert "def route" in out


def test_grep_in_extensionless_script(tmp_path, capsys):
    p = _script(
        tmp_path / "tg", "#!/usr/bin/env python3",
        "class Router:\n    def route(self, msg): pass\n",
    )
    assert main(["grep", "route", str(p)]) == 0
    out = capsys.readouterr().out
    assert "route" in out and "no matches" not in out


def test_digest_extensionless_script(tmp_path, capsys):
    p = _script(tmp_path / "tg", "#!/usr/bin/env python3")
    assert main(["digest", str(p)]) == 0
    out = capsys.readouterr().out
    assert "f()" in out


def test_outline_json_extensionless_script(tmp_path, capsys):
    p = _script(tmp_path / "tg", "#!/usr/bin/env python3")
    assert main(["outline", str(p), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"][0]["language"] == "python"


def test_directory_walk_does_not_pick_up_extensionless_scripts(tmp_path, capsys):
    # The sniff is for explicit file inputs only — walking a directory
    # must not start open()ing every extensionless file in it.
    _script(tmp_path / "tg", "#!/usr/bin/env python3", "def hidden(): pass\n")
    (tmp_path / "real.py").write_text("def visible(): pass\n")
    assert main(["outline", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "visible" in out
    assert "hidden" not in out


# --- CLI: self-explaining failure notes ------------------------------------


def test_outline_note_for_unsupported_interpreter(tmp_path, capsys):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo hi\n")
    assert main(["outline", str(p)]) == 0
    out = capsys.readouterr().out
    assert "shebang interpreter 'bash' is not supported" in out
    # All-extensionless input: the generic supported-extensions list
    # would only mislead, so the specific note rides alone.
    assert "supported extensions" not in out


def test_outline_note_for_missing_shebang(tmp_path, capsys):
    p = tmp_path / "tool"
    p.write_text("plain text\n")
    assert main(["outline", str(p)]) == 0
    out = capsys.readouterr().out
    assert "has no shebang line" in out
    assert "supported extensions" not in out


def test_outline_mixed_inputs_keep_generic_note(tmp_path, capsys):
    # A dir among the inputs means the extensions list is still useful
    # context — both notes should appear.
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo hi\n")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert main(["outline", str(p), str(empty_dir)]) == 0
    out = capsys.readouterr().out
    assert "supported extensions" in out
    assert "shebang interpreter 'bash' is not supported" in out


def test_digest_note_for_unsupported_interpreter(tmp_path, capsys):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo hi\n")
    assert main(["digest", str(p)]) == 0
    out = capsys.readouterr().out
    assert "shebang interpreter 'bash' is not supported" in out


def test_show_note_for_unsupported_interpreter(tmp_path, capsys):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo hi\n")
    assert main(["show", str(p), "anything"]) == 0
    out = capsys.readouterr().out
    assert "shebang interpreter 'bash' is not supported" in out


def test_grep_note_for_unsupported_interpreter(tmp_path, capsys):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo target\n")
    assert main(["grep", "target", str(p)]) == 0
    out = capsys.readouterr().out
    assert "no matches" in out
    assert "shebang interpreter 'bash' is not supported" in out


def test_grep_no_skip_note_when_script_was_searched(tmp_path, capsys):
    # A *recognized* script with zero matches is a plain empty result —
    # the skip-note would be a false claim that the file wasn't searched.
    p = _script(tmp_path / "tg", "#!/usr/bin/env python3")
    assert main(["grep", "nonexistent_symbol", str(p)]) == 0
    out = capsys.readouterr().out
    assert "no matches" in out
    assert "shebang" not in out


def test_grep_json_skip_note_for_unsupported_interpreter(tmp_path, capsys):
    p = _script(tmp_path / "tool", "#!/bin/bash", "echo target\n")
    assert main(["grep", "target", str(p), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("shebang" in n for n in payload.get("notes", []))
