"""Tests for the `--json` machine-readable output mode.

Two layers, mirroring `test_digest_format_presets.py`:
- library-level — call the `json_output` builders directly,
- CLI-level — `cli.main([..., "--json"])` and parse captured stdout.

The cross-adapter parametrized tests below run JSON serialization
against every registered language adapter so a new adapter cannot
silently break the schema.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ast_outline import json_output
from ast_outline.adapters import ADAPTERS, get_adapter_for
from ast_outline.cli import main
from ast_outline.core import (
    KIND_CLASS,
    KIND_FIELD,
    KIND_METHOD,
    KIND_VARIABLE,
    Declaration,
    filter_declarations,
)
from ast_outline.json_output import SCHEMA_VERSION


# Every fixture file an adapter can parse — used by the per-file sweep
# below. Computed at import time (module-level parametrize can't take a
# pytest fixture). `parents[1]` is the `tests/` dir.
_FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
_ALL_FIXTURE_FILES = sorted(
    p for p in _FIXTURES_ROOT.rglob("*")
    if p.is_file() and get_adapter_for(p) is not None
)
_BROKEN_FIXTURES = sorted(
    p for p in _ALL_FIXTURE_FILES if "broken" in p.name
)


# --- helpers --------------------------------------------------------------


def _run_json(argv: list[str], capsys) -> dict:
    """Run the CLI in JSON mode, assert exit 0, return parsed stdout."""
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, f"expected exit 0, got {rc}"
    return json.loads(out)  # raises if stdout is not valid JSON


def _walk(decls: list[dict]):
    """Yield every declaration dict in a JSON declaration tree."""
    for d in decls:
        yield d
        yield from _walk(d["children"])


def _all_decls(obj: dict) -> list[dict]:
    """Flatten every declaration across every file in a JSON document."""
    return [
        d for f in obj.get("files", []) for d in _walk(f["declarations"])
    ]


def _parse(path):
    adapter = get_adapter_for(path)
    assert adapter is not None
    return adapter.parse(path)


# --- envelope -------------------------------------------------------------


def test_outline_envelope(python_dir, capsys):
    obj = _run_json(["outline", str(python_dir), "--json"], capsys)
    assert obj["tool"] == "ast-outline"
    assert obj["schema_version"] == SCHEMA_VERSION
    assert obj["command"] == "outline"


def test_digest_envelope(python_dir, capsys):
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    assert obj["tool"] == "ast-outline"
    assert obj["schema_version"] == SCHEMA_VERSION
    assert obj["command"] == "digest"


def test_grep_envelope(python_dir, capsys):
    obj = _run_json(["grep", "def", str(python_dir), "--json"], capsys)
    assert obj["tool"] == "ast-outline"
    assert obj["schema_version"] == SCHEMA_VERSION
    assert obj["command"] == "grep"


def test_show_envelope(python_dir, capsys):
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"), "BaseEntity", "--json"],
        capsys,
    )
    assert obj["tool"] == "ast-outline"
    assert obj["schema_version"] == SCHEMA_VERSION
    assert obj["command"] == "show"


# --- payload shape --------------------------------------------------------


def test_outline_payload_shape(python_dir, capsys):
    obj = _run_json(["outline", str(python_dir), "--json"], capsys)
    assert isinstance(obj["files"], list) and obj["files"]
    assert isinstance(obj["notes"], list)
    f = obj["files"][0]
    for key in (
        "path", "language", "line_count", "error_count",
        "tokens_estimate", "size", "counts", "imports",
        "conditional_imports_count", "import_regions",
        "noise_regions", "declarations",
    ):
        assert key in f, f"missing file key: {key}"
    assert f["size"] in ("tiny", "medium", "large", "huge")
    assert set(f["counts"]) == {
        "types", "methods", "fields", "headings", "code_blocks", "elements",
    }


def test_digest_payload_shape(python_dir, capsys):
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    assert "root" in obj
    assert set(obj["summary"]) == {"files", "types", "methods", "fields"}
    assert obj["summary"]["files"] == len(obj["files"])


def test_grep_payload_shape(python_dir, capsys):
    obj = _run_json(["grep", "def", str(python_dir), "--json"], capsys)
    assert set(obj["summary"]) == {
        "total_matches", "files_with_matches",
        "filtered_count", "truncated_count", "kind_counts",
    }
    if obj["files"]:
        fr = obj["files"][0]
        for key in ("path", "language", "matches",
                    "filtered_count", "truncated_count"):
            assert key in fr
        if fr["matches"]:
            m = fr["matches"][0]
            for key in ("line", "column", "line_content",
                        "kind", "enclosing_path"):
                assert key in m


def test_show_payload_shape(python_dir, capsys):
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"), "BaseEntity", "--json"],
        capsys,
    )
    assert obj["file"].endswith("domain_model.py")
    assert len(obj["results"]) == 1
    entry = obj["results"][0]
    assert entry["query"] == "BaseEntity"
    assert entry["matches"], "BaseEntity should be found"
    m = entry["matches"][0]
    for key in ("qualified_name", "kind", "start_line", "end_line",
                "ancestor_signatures", "signature", "source"):
        assert key in m


# --- Declaration serialization -------------------------------------------


def test_declaration_to_dict_all_fields(python_dir):
    result = _parse(python_dir / "domain_model.py")
    assert result.declarations
    d = json_output.declaration_to_dict(result.declarations[0])
    for key in (
        "kind", "name", "signature", "visibility", "native_kind",
        "bases", "attrs", "docs", "docs_inside", "start_line",
        "end_line", "start_byte", "end_byte", "doc_start_byte",
        "match_names", "children",
    ):
        assert key in d, f"missing declaration key: {key}"
    assert isinstance(d["children"], list)


def test_declaration_to_dict_recurses_children(python_dir):
    result = _parse(python_dir / "domain_model.py")
    # domain_model.py has classes with methods — find one type with children.
    nested = [
        d for d in result.declarations if d.children
    ]
    assert nested, "fixture expected to have a type with members"
    d = json_output.declaration_to_dict(nested[0])
    assert d["children"]
    child = d["children"][0]
    assert "kind" in child and "name" in child


# --- content filters apply (rg-model) -------------------------------------


def test_digest_json_default_hides_private(python_dir, capsys):
    """`digest --json` carries the same content as `digest` text — its
    default is the public-API map, so private declarations are absent."""
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    visibilities = {d["visibility"] for d in _all_decls(obj)}
    assert "private" not in visibilities, visibilities


def test_digest_json_include_private_shows_private(python_dir, capsys):
    """`--include-private` brings private declarations into digest JSON."""
    obj = _run_json(
        ["digest", str(python_dir), "--include-private", "--json"], capsys
    )
    assert any(d["visibility"] == "private" for d in _all_decls(obj))


def test_digest_json_format_layout_does_not_affect_json(python_dir, capsys):
    """`--format` layout presets that share the same content settings
    (names / compact / default) produce identical JSON — JSON has no
    layout, only content."""
    a = _run_json(["digest", str(python_dir), "--json"], capsys)
    b = _run_json(
        ["digest", str(python_dir), "--json", "--format=names"], capsys
    )
    c = _run_json(
        ["digest", str(python_dir), "--json", "--format=compact"], capsys
    )
    assert a == b == c


def test_digest_json_format_wide_adds_private_and_fields(python_dir, capsys):
    """`--format=wide` resolves to include-private + include-fields, so
    its content — and thus its JSON — differs from the default preset."""
    default = _run_json(["digest", str(python_dir), "--json"], capsys)
    wide = _run_json(
        ["digest", str(python_dir), "--json", "--format=wide"], capsys
    )
    assert wide != default
    assert any(d["visibility"] == "private" for d in _all_decls(wide))


# --- Unicode --------------------------------------------------------------


def test_unicode_identifiers_not_escaped(tmp_path, capsys):
    src = tmp_path / "u.py"
    src.write_text("def привет():\n    pass\n", encoding="utf-8")
    rc = main(["outline", str(src), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    # ensure_ascii=False — the identifier stays human-readable.
    assert "привет" in out
    assert "\\u" not in out
    obj = json.loads(out)
    assert obj["files"][0]["declarations"][0]["name"] == "привет"


# --- error objects --------------------------------------------------------


def test_error_object_path_not_found(capsys):
    obj = _run_json(["outline", "/no/such/path", "--json"], capsys)
    assert "error" in obj
    assert obj["command"] == "outline"
    assert obj["error"]["notes"]
    assert "not found" in obj["error"]["notes"][0]


def test_error_object_bad_argument(capsys):
    """A malformed argument in JSON mode still yields valid JSON."""
    obj = _run_json(["digest", "--bogus-flag", "--json"], capsys)
    assert "error" in obj
    assert obj["error"]["notes"]


def test_error_object_unsupported_extension(tmp_path, capsys):
    bad = tmp_path / "data.xyz"
    bad.write_text("nothing", encoding="utf-8")
    obj = _run_json(["show", str(bad), "Sym", "--json"], capsys)
    assert "error" in obj


# --- zero results are valid, not errors -----------------------------------


def test_grep_no_match_is_empty_not_error(python_dir, capsys):
    obj = _run_json(
        ["grep", "zzz_no_such_symbol_zzz", str(python_dir), "--json"], capsys
    )
    assert "error" not in obj
    assert obj["files"] == []
    assert obj["summary"]["total_matches"] == 0


def test_show_symbol_not_found_is_empty_match_list(python_dir, capsys):
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"),
         "NoSuchSymbol", "--json"],
        capsys,
    )
    assert "error" not in obj
    assert obj["results"][0]["query"] == "NoSuchSymbol"
    assert obj["results"][0]["matches"] == []


# --- determinism ----------------------------------------------------------


def test_output_is_deterministic(python_dir):
    result = _parse(python_dir / "domain_model.py")
    a = json_output.outline_json([result])
    b = json_output.outline_json([result])
    assert a == b


# --- cross-adapter coverage ----------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.language_name)
def test_outline_json_valid_for_every_adapter(adapter, fixtures_dir, capsys):
    lang_dir = fixtures_dir / adapter.language_name
    assert lang_dir.is_dir(), f"no fixture dir for {adapter.language_name}"
    obj = _run_json(["outline", str(lang_dir), "--json"], capsys)
    assert obj["command"] == "outline"
    assert obj["schema_version"] == SCHEMA_VERSION
    assert obj["files"], f"{adapter.language_name}: expected parsed files"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.language_name)
def test_digest_json_valid_for_every_adapter(adapter, fixtures_dir, capsys):
    lang_dir = fixtures_dir / adapter.language_name
    assert lang_dir.is_dir(), f"no fixture dir for {adapter.language_name}"
    obj = _run_json(["digest", str(lang_dir), "--json"], capsys)
    assert obj["command"] == "digest"
    assert obj["files"], f"{adapter.language_name}: expected parsed files"
    total_decls = sum(len(f["declarations"]) for f in obj["files"])
    assert total_decls > 0, (
        f"{adapter.language_name}: fixture dir produced no declarations"
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.language_name)
def test_grep_json_valid_for_every_adapter(adapter, fixtures_dir, capsys):
    """grep JSON exercises a distinct IR path — `GrepMatch` and its
    `enclosing_path` scope chain. `e` is a near-universal letter, so
    this produces real matches to serialize across every adapter."""
    lang_dir = fixtures_dir / adapter.language_name
    assert lang_dir.is_dir(), f"no fixture dir for {adapter.language_name}"
    obj = _run_json(["grep", "e", str(lang_dir), "--json"], capsys)
    assert obj["command"] == "grep"
    assert obj["schema_version"] == SCHEMA_VERSION
    # Every match (and its enclosing scope chain) must serialize cleanly.
    for fr in obj["files"]:
        for m in fr["matches"]:
            assert isinstance(m["enclosing_path"], list)


# --- per-file sweep -------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _ALL_FIXTURE_FILES,
    ids=[str(p.relative_to(_FIXTURES_ROOT)) for p in _ALL_FIXTURE_FILES],
)
def test_every_fixture_file_json_valid(fixture, capsys):
    """Run all three structural commands in JSON mode over *every*
    individual fixture file. A directory-level sweep can mask a single
    file that crashes serialization; this catches it at file
    granularity across the whole corpus."""
    out_obj = _run_json(["outline", str(fixture), "--json"], capsys)
    assert out_obj["command"] == "outline"
    assert "error" not in out_obj, f"outline errored on {fixture}"
    assert len(out_obj["files"]) == 1

    dig_obj = _run_json(["digest", str(fixture), "--json"], capsys)
    assert dig_obj["command"] == "digest"
    assert "error" not in dig_obj, f"digest errored on {fixture}"

    grep_obj = _run_json(["grep", "e", str(fixture), "--json"], capsys)
    assert grep_obj["command"] == "grep"
    assert "error" not in grep_obj, f"grep errored on {fixture}"


# --- broken / empty files -------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _BROKEN_FIXTURES,
    ids=[str(p.relative_to(_FIXTURES_ROOT)) for p in _BROKEN_FIXTURES],
)
def test_broken_fixture_reports_error_count(fixture, capsys):
    """A file with syntax errors still serializes to valid JSON — the
    partial outline is kept and `error_count` is surfaced above zero."""
    obj = _run_json(["outline", str(fixture), "--json"], capsys)
    assert "error" not in obj
    assert obj["files"][0]["error_count"] > 0, (
        f"{fixture}: expected error_count > 0"
    )


def test_empty_file_json(lua_dir, capsys):
    """A near-empty source file produces a valid, all-zero document."""
    obj = _run_json(
        ["outline", str(lua_dir / "empty.lua"), "--json"], capsys
    )
    f = obj["files"][0]
    assert f["declarations"] == []
    assert f["imports"] == []
    assert f["counts"] == {
        "types": 0, "methods": 0, "fields": 0,
        "headings": 0, "code_blocks": 0, "elements": 0,
    }


# --- IR field fidelity ----------------------------------------------------


def test_imports_serialized(python_dir, capsys):
    obj = _run_json(
        ["outline", str(python_dir / "domain_model.py"), "--json"], capsys
    )
    imports = obj["files"][0]["imports"]
    assert imports, "domain_model.py has import statements"
    assert all(isinstance(s, str) for s in imports)


def test_import_regions_serialized(python_dir, capsys):
    obj = _run_json(
        ["outline", str(python_dir / "domain_model.py"), "--json"], capsys
    )
    regions = obj["files"][0]["import_regions"]
    assert regions, "domain_model.py has import_regions"
    for r in regions:
        assert set(r) == {"start", "end"}
        assert isinstance(r["start"], int) and isinstance(r["end"], int)
        assert r["end"] >= r["start"]


def test_noise_regions_serialized(lua_dir, capsys):
    obj = _run_json(
        ["outline", str(lua_dir / "module_pattern.lua"), "--json"], capsys
    )
    regions = obj["files"][0]["noise_regions"]
    assert regions, "module_pattern.lua has noise_regions"
    for r in regions:
        assert set(r) == {"start", "end", "kind"}
        assert r["kind"] in ("string", "comment")
        assert r["end"] >= r["start"]


def test_conditional_imports_count_serialized(php_dir, capsys):
    obj = _run_json(
        ["outline", str(php_dir / "legacy_includes.php"), "--json"], capsys
    )
    assert obj["files"][0]["conditional_imports_count"] > 0


def test_bases_serialized(csharp_dir, capsys):
    obj = _run_json(
        ["outline", str(csharp_dir / "hierarchy.cs"), "--json"], capsys
    )
    with_bases = [d for d in _all_decls(obj) if d["bases"]]
    assert with_bases, "hierarchy.cs has a type with base classes"
    assert all(isinstance(b, str) for d in with_bases for b in d["bases"])


def test_attrs_serialized(csharp_dir, capsys):
    obj = _run_json(
        ["outline", str(csharp_dir / "unity_behaviour.cs"), "--json"], capsys
    )
    assert any(d["attrs"] for d in _all_decls(obj)), (
        "unity_behaviour.cs has a declaration carrying [Attributes]"
    )


def test_native_kind_serialized(cpp_dir, capsys):
    obj = _run_json(
        ["outline", str(cpp_dir / "cpp20_features.h"), "--json"], capsys
    )
    assert any(d["native_kind"] for d in _all_decls(obj)), (
        "cpp20_features.h has a declaration with a native_kind keyword"
    )


def test_line_and_byte_offsets_consistent(python_dir, capsys):
    obj = _run_json(["outline", str(python_dir), "--json"], capsys)
    for d in _all_decls(obj):
        assert d["end_line"] >= d["start_line"]
        assert d["end_byte"] >= d["start_byte"]


# --- grep — deeper cases --------------------------------------------------


def test_grep_enclosing_path_populated(python_dir, capsys):
    """A match inside a class/method carries a non-empty scope chain,
    each entry an object with `kind` and `name`."""
    obj = _run_json(["grep", "self", str(python_dir), "--json"], capsys)
    scoped = [
        m for fr in obj["files"]
        for m in fr["matches"] if m["enclosing_path"]
    ]
    assert scoped, "`self` matches should sit inside declarations"
    for entry in scoped[0]["enclosing_path"]:
        assert set(entry) == {"kind", "name"}


def test_grep_multi_pattern_json(python_dir, capsys):
    """`-e` multi-pattern search produces a single combined JSON doc."""
    obj = _run_json(
        ["grep", "get", "-e", "put", str(python_dir), "--json"], capsys
    )
    assert obj["command"] == "grep"
    assert obj["summary"]["total_matches"] > 0


def test_grep_max_count_truncation_json(python_dir, capsys):
    """`-m` caps matches per file; `truncated_count` surfaces the drop."""
    obj = _run_json(
        ["grep", "self", str(python_dir / "domain_model.py"),
         "-m", "1", "--json"],
        capsys,
    )
    assert obj["summary"]["truncated_count"] > 0
    for fr in obj["files"]:
        assert len(fr["matches"]) <= 1


def test_grep_include_noise_json(python_dir, capsys):
    """--include-noise never reduces the match count — comment/string
    hits are added, not removed."""
    target = str(python_dir / "domain_model.py")
    plain = _run_json(["grep", "repository", target, "--json"], capsys)
    noisy = _run_json(
        ["grep", "repository", target, "--include-noise", "--json"], capsys
    )
    assert noisy["summary"]["total_matches"] >= 1
    assert (noisy["summary"]["total_matches"]
            >= plain["summary"]["total_matches"])


def test_grep_files_only_ignored_in_json(python_dir, capsys):
    """`-l` is an output-shaping flag — ignored in JSON mode, which
    always emits the full document."""
    obj = _run_json(
        ["grep", "def", str(python_dir), "-l", "--json"], capsys
    )
    assert obj["command"] == "grep"
    assert "summary" in obj and "files" in obj


def test_grep_count_ignored_in_json(python_dir, capsys):
    """`-c` is likewise ignored — the full document is emitted."""
    obj = _run_json(
        ["grep", "def", str(python_dir), "-c", "--json"], capsys
    )
    assert obj["command"] == "grep"
    assert "summary" in obj and "files" in obj


def test_grep_unicode_column_is_codepoint_offset(tmp_path, capsys):
    """grep `column` in JSON is a 1-based *codepoint* offset, correct
    even after multi-byte characters earlier on the line (cf. v0.8.13)."""
    src = tmp_path / "u.py"
    src.write_text("тест = привет(1)\n", encoding="utf-8")
    obj = _run_json(["grep", "привет", str(src), "--json"], capsys)
    matches = [m for fr in obj["files"] for m in fr["matches"]]
    assert matches
    m = matches[0]
    # `привет` begins at codepoint index 7 (0-based) → 1-based column 8.
    assert m["column"] == 8
    assert m["line_content"] == "тест = привет(1)"


# --- show — deeper cases --------------------------------------------------


def test_show_multiple_symbols_json(python_dir, capsys):
    """One result entry per requested symbol, in request order."""
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"),
         "Repository", "BaseEntity", "--json"],
        capsys,
    )
    assert [r["query"] for r in obj["results"]] == ["Repository", "BaseEntity"]
    for r in obj["results"]:
        assert r["matches"], f"{r['query']} should resolve"


def test_show_source_field_contains_body(python_dir, capsys):
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"), "BaseEntity", "--json"],
        capsys,
    )
    m = obj["results"][0]["matches"][0]
    assert isinstance(m["source"], str) and m["source"].strip()
    assert "BaseEntity" in m["source"]


def test_show_mixed_found_and_not_found(python_dir, capsys):
    obj = _run_json(
        ["show", str(python_dir / "domain_model.py"),
         "BaseEntity", "TotallyMissing", "--json"],
        capsys,
    )
    found, missing = obj["results"]
    assert found["query"] == "BaseEntity" and found["matches"]
    assert missing["query"] == "TotallyMissing" and missing["matches"] == []


# --- non-code languages ---------------------------------------------------


def test_markdown_headings_in_json(md_dir, capsys):
    obj = _run_json(["outline", str(md_dir), "--json"], capsys)
    total_headings = sum(f["counts"]["headings"] for f in obj["files"])
    assert total_headings > 0
    assert "heading" in {d["kind"] for d in _all_decls(obj)}


def test_yaml_keys_in_json(yaml_dir, capsys):
    obj = _run_json(["outline", str(yaml_dir), "--json"], capsys)
    assert _all_decls(obj), "yaml fixtures should produce declarations"


# --- digest specifics -----------------------------------------------------


def test_digest_root_and_relative_paths(python_dir, capsys):
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    assert obj["root"]
    for f in obj["files"]:
        # Paths are relative to `root` — no absolute-path leakage.
        assert not f["path"].startswith("/")


def test_digest_summary_aggregates_counts(python_dir, capsys):
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    s = obj["summary"]
    assert s["types"] == sum(f["counts"]["types"] for f in obj["files"])
    assert s["methods"] == sum(f["counts"]["methods"] for f in obj["files"])
    assert s["fields"] == sum(f["counts"]["fields"] for f in obj["files"])


# --- flag interaction -----------------------------------------------------


def test_imports_flag_does_not_affect_json(python_dir, capsys):
    """--imports is layout-only (it adds a text header line); the JSON
    `imports` field is present regardless, so --imports is a no-op here."""
    a = _run_json(["outline", str(python_dir), "--json"], capsys)
    b = _run_json(["outline", str(python_dir), "--imports", "--json"], capsys)
    assert a == b


def test_no_lines_flag_does_not_affect_json(python_dir, capsys):
    """--no-lines is layout-only (it hides the `L12` text suffix); JSON
    line numbers are structural fields, so --no-lines is a no-op here."""
    a = _run_json(["outline", str(python_dir), "--json"], capsys)
    b = _run_json(["outline", str(python_dir), "--no-lines", "--json"], capsys)
    assert a == b


def test_outline_content_flags_filter_json(python_dir, capsys):
    """outline content flags (--no-private/-fields/-docs/-attrs) filter
    the JSON declaration tree, same as they filter the text output."""
    full = _run_json(["outline", str(python_dir), "--json"], capsys)
    filtered = _run_json(
        ["outline", str(python_dir),
         "--no-private", "--no-fields", "--no-docs", "--no-attrs", "--json"],
        capsys,
    )
    assert filtered != full
    decls = _all_decls(filtered)
    assert all(d["visibility"] != "private" for d in decls)
    assert all(d["kind"] != "field" for d in decls)
    assert all(d["docs"] == [] for d in decls)
    assert all(d["attrs"] == [] for d in decls)
    # full output still carries all of it.
    full_decls = _all_decls(full)
    assert any(d["visibility"] == "private" for d in full_decls)
    assert any(d["kind"] == "field" for d in full_decls)


def test_show_view_signature_trims_json_source(python_dir, capsys):
    """`--view signature` carries through to the JSON `source` field —
    it holds the header-only contract, not the full body."""
    base = ["show", str(python_dir / "domain_model.py"), "BaseEntity", "--json"]
    full = _run_json(base, capsys)
    sig = _run_json(base + ["--view", "signature"], capsys)
    full_src = full["results"][0]["matches"][0]["source"]
    sig_src = sig["results"][0]["matches"][0]["source"]
    assert len(sig_src.splitlines()) < len(full_src.splitlines())
    # The standalone `signature` field is unaffected by --view.
    assert (full["results"][0]["matches"][0]["signature"]
            == sig["results"][0]["matches"][0]["signature"])


# --- multi-file & determinism --------------------------------------------


def test_outline_multiple_files_json(python_dir, capsys):
    f1 = str(python_dir / "domain_model.py")
    f2 = str(python_dir / "hierarchy.py")
    obj = _run_json(["outline", f1, f2, "--json"], capsys)
    assert len(obj["files"]) == 2
    paths = [f["path"] for f in obj["files"]]
    assert any("domain_model.py" in p for p in paths)
    assert any("hierarchy.py" in p for p in paths)


def test_digest_and_grep_deterministic(python_dir, capsys):
    d1 = _run_json(["digest", str(python_dir), "--json"], capsys)
    d2 = _run_json(["digest", str(python_dir), "--json"], capsys)
    assert d1 == d2
    g1 = _run_json(["grep", "def", str(python_dir), "--json"], capsys)
    g2 = _run_json(["grep", "def", str(python_dir), "--json"], capsys)
    assert g1 == g2


def test_envelope_field_types(python_dir, capsys):
    obj = _run_json(["outline", str(python_dir), "--json"], capsys)
    assert isinstance(obj["tool"], str)
    assert isinstance(obj["schema_version"], int)
    assert isinstance(obj["command"], str)


# --- filter_declarations — the shared content-filter pass -----------------
#
# `core.filter_declarations` is the single definition of the content
# filter that both the JSON serializer and (indirectly) the text
# renderers rely on. These exercise it directly on hand-built trees.


def _decl(kind, name, *, visibility="", docs=None, attrs=None, children=None):
    return Declaration(
        kind=kind,
        name=name,
        signature=f"{kind} {name}",
        visibility=visibility,
        docs=list(docs or []),
        attrs=list(attrs or []),
        children=list(children or []),
    )


def test_filter_declarations_drops_private():
    tree = [
        _decl(KIND_CLASS, "Pub"),
        _decl(KIND_CLASS, "Priv", visibility="private"),
    ]
    out = filter_declarations(tree, include_private=False, include_fields=True)
    assert [d.name for d in out] == ["Pub"]


def test_filter_declarations_keeps_private_when_included():
    tree = [_decl(KIND_CLASS, "Priv", visibility="private")]
    out = filter_declarations(tree, include_private=True, include_fields=True)
    assert [d.name for d in out] == ["Priv"]


def test_filter_declarations_drops_fields():
    tree = [_decl(KIND_METHOD, "m"), _decl(KIND_FIELD, "f")]
    out = filter_declarations(tree, include_private=True, include_fields=False)
    assert [d.name for d in out] == ["m"]


def test_filter_declarations_custom_field_kinds():
    """`field_kinds` widens what counts as field-like — digest passes a
    set that also covers SCSS `$variable` and markdown code blocks."""
    tree = [_decl(KIND_FIELD, "f"), _decl(KIND_VARIABLE, "v")]
    # Default field_kinds is {KIND_FIELD} → the variable survives.
    out = filter_declarations(tree, include_private=True, include_fields=False)
    assert [d.name for d in out] == ["v"]
    # Widened set → the variable is dropped too.
    out2 = filter_declarations(
        tree,
        include_private=True,
        include_fields=False,
        field_kinds=frozenset({KIND_FIELD, KIND_VARIABLE}),
    )
    assert out2 == []


def test_filter_declarations_clears_docs_and_attrs():
    tree = [_decl(KIND_CLASS, "C", docs=["doc line"], attrs=["@deco"])]
    kept = filter_declarations(tree, include_private=True, include_fields=True)
    assert kept[0].docs == ["doc line"] and kept[0].attrs == ["@deco"]
    stripped = filter_declarations(
        tree,
        include_private=True,
        include_fields=True,
        include_docs=False,
        include_attrs=False,
    )
    assert stripped[0].docs == [] and stripped[0].attrs == []


def test_filter_declarations_recurses_into_children():
    tree = [
        _decl(KIND_CLASS, "C", children=[
            _decl(KIND_METHOD, "pub"),
            _decl(KIND_METHOD, "priv", visibility="private"),
            _decl(KIND_FIELD, "fld"),
        ]),
    ]
    out = filter_declarations(tree, include_private=False, include_fields=False)
    assert [c.name for c in out[0].children] == ["pub"]


def test_filter_declarations_does_not_mutate_input():
    tree = [
        _decl(KIND_CLASS, "C", docs=["d"], children=[_decl(KIND_FIELD, "f")]),
    ]
    filter_declarations(
        tree, include_private=True, include_fields=False, include_docs=False
    )
    # The original tree is untouched — filtering returns a copy.
    assert tree[0].docs == ["d"]
    assert [c.name for c in tree[0].children] == ["f"]


def test_filter_declarations_identity_when_all_included():
    tree = [_decl(KIND_CLASS, "C", children=[_decl(KIND_FIELD, "f")])]
    out = filter_declarations(tree, include_private=True, include_fields=True)
    assert [d.name for d in out] == ["C"]
    assert [c.name for c in out[0].children] == ["f"]


def test_filter_declarations_empty_input():
    assert filter_declarations(
        [], include_private=False, include_fields=False
    ) == []


# --- content-filter coverage: digest fields, SCSS, show --no-doc ----------


def test_digest_json_default_hides_fields(python_dir, capsys):
    """digest's public-API default drops fields from JSON too."""
    obj = _run_json(["digest", str(python_dir), "--json"], capsys)
    assert all(d["kind"] != "field" for d in _all_decls(obj))


def test_digest_json_include_fields_shows_fields(python_dir, capsys):
    obj = _run_json(
        ["digest", str(python_dir), "--include-fields", "--json"], capsys
    )
    assert any(d["kind"] == "field" for d in _all_decls(obj))


def test_digest_json_scss_variables_are_field_like(scss_dir, capsys):
    """SCSS `$variable` bindings are field-like for digest — hidden by
    default, surfaced by --include-fields (the widened field_kinds set)."""
    default = _run_json(["digest", str(scss_dir), "--json"], capsys)
    assert all(d["kind"] != "variable" for d in _all_decls(default))
    with_fields = _run_json(
        ["digest", str(scss_dir), "--include-fields", "--json"], capsys
    )
    assert any(d["kind"] == "variable" for d in _all_decls(with_fields))


def test_show_no_doc_strips_docs_from_json_source(python_dir, capsys):
    """--no-doc removes the leading doc block from each match's source."""
    base = ["show", str(python_dir / "domain_model.py"),
            "BaseEntity", "--json"]
    with_doc = _run_json(base, capsys)["results"][0]["matches"][0]["source"]
    no_doc = _run_json(
        base + ["--no-doc"], capsys
    )["results"][0]["matches"][0]["source"]
    assert len(no_doc) < len(with_doc)
    assert "BaseEntity" in no_doc  # the declaration itself is still there


def test_counts_stay_full_under_content_filters(python_dir, capsys):
    """Header `counts` describe the whole file — a stat, not part of the
    filtered view — so content flags must not change them."""
    full = _run_json(["outline", str(python_dir), "--json"], capsys)
    filtered = _run_json(
        ["outline", str(python_dir),
         "--no-private", "--no-fields", "--no-docs", "--no-attrs", "--json"],
        capsys,
    )
    assert ([f["counts"] for f in full["files"]]
            == [f["counts"] for f in filtered["files"]])


def test_outline_json_filter_drops_nested_private(python_dir, capsys):
    """Content filtering reaches nested declarations, not just top level."""
    obj = _run_json(
        ["outline", str(python_dir), "--no-private", "--json"], capsys
    )
    # _all_decls recurses children — every node, at any depth, is public.
    assert all(d["visibility"] != "private" for d in _all_decls(obj))
