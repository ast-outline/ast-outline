"""Same-directory rescue when file-mode `show` misses a symbol.

Usage-history analysis showed the dominant `show` failure is a
right-class-wrong-file guess: the agent asks for `ThingIdGenerator` in
`ThingData.cs` while the definition sits in `ThingIdGenerator.cs` next
to it — and its next move was a generic grep over the parent dir.
These tests pin the rescue contract: the rescue only ever POINTS —
``path:start-end (kind)`` candidates, never a body from a file the agent
didn't ask for; no hit falls back to a did-you-mean pool; the scan
never leaves the file's own directory.
"""
from __future__ import annotations

import json
from pathlib import Path

from ast_outline.cli import main


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _make_neighborhood(tmp_path: Path) -> Path:
    """A file to query + siblings holding the symbols it lacks."""
    target = _write(
        tmp_path / "ThingData.cs",
        "public class ThingData {\n  public int Id;\n}\n",
    )
    _write(
        tmp_path / "ThingIdGenerator.cs",
        "public class ThingIdGenerator {\n"
        "  public static int Next() { return 1; }\n}\n",
    )
    _write(
        tmp_path / "Multi.cs",
        "public class Other {\n  public void TargetMethod() {}\n}\n"
        "public class Another {\n  public void TargetMethod() {}\n}\n",
    )
    return target


def test_single_sibling_hit_points_without_body(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "ThingIdGenerator"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found: ThingIdGenerator" in out
    assert "defined in the same directory:" in out
    assert "ThingIdGenerator.cs:1-3 (class)" in out
    # Pointer only — never a body from a file the agent didn't ask for.
    assert "public static int Next()" not in out


def test_multiple_sibling_hits_list_candidates_without_bodies(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "TargetMethod"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found: TargetMethod" in out
    assert "defined in the same directory:" in out
    assert "Multi.cs:2-2 (method)" in out
    assert "Multi.cs:5-5 (method)" in out
    # Ambiguous: pointer note only, no code bodies (single-shape contract).
    assert "void TargetMethod" not in out


def test_no_sibling_hit_falls_back_to_did_you_mean(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "thingidgenerator"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found" in out
    assert "did you mean: ThingIdGenerator (class)?" in out


def test_rescue_does_not_recurse_into_subdirs(tmp_path, capsys):
    target = _write(
        tmp_path / "ThingData.cs",
        "public class ThingData {}\n",
    )
    _write(
        tmp_path / "nested" / "Deep.cs",
        "public class DeepSymbol {}\n",
    )
    assert main(["show", str(target), "DeepSymbol"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found: DeepSymbol" in out
    # One level only: the nested definition must not be offered.
    assert "DeepSymbol (class)" not in out
    assert "Deep.cs" not in out


def test_found_symbols_are_untouched_by_rescue(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "ThingData"]) == 0
    out = capsys.readouterr().out
    assert "public class ThingData" in out
    assert "not found" not in out
    assert "same directory" not in out


def test_mixed_hit_and_rescued_symbols_in_one_call(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "ThingData", "ThingIdGenerator"]) == 0
    out = capsys.readouterr().out
    # The found symbol prints its body; the missing one only points.
    assert "public class ThingData" in out
    assert "ThingIdGenerator.cs:1-3 (class)" in out
    assert "public static int Next()" not in out


def test_lone_file_without_siblings_keeps_plain_note(tmp_path, capsys):
    target = _write(tmp_path / "Lonely.cs", "public class Lonely {}\n")
    assert main(["show", str(target), "Missing"]) == 0
    out = capsys.readouterr().out
    assert "symbol not found: Missing" in out
    assert "same directory" not in out


def test_json_rescue_rides_notes_only(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "ThingIdGenerator", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    # Structured matches stay scoped to the requested file — empty.
    assert payload["results"][0]["matches"] == []
    notes = payload["notes"]
    assert any(
        "defined in the same directory" in n and "ThingIdGenerator.cs:1-3" in n
        for n in notes
    )


def test_json_did_you_mean_note(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "thingidgenerator", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("did you mean" in n for n in payload["notes"])


def test_json_found_symbol_emits_no_rescue_notes(tmp_path, capsys):
    target = _make_neighborhood(tmp_path)
    assert main(["show", str(target), "ThingData", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["matches"]
    assert payload["notes"] == []
