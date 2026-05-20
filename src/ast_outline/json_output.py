"""Machine-readable JSON serialization of the structural IR.

Pure functions converting the `core` / `grep` IR into JSON strings.
Consumed by the CLI `--json` flag. Kept separate from the text
renderers in `core.py` / `grep.py` on purpose: the text format is free
to change every release, the JSON schema is a **stable contract**.

`core` and `grep` never import this module — the dependency runs one
way (`json_output` → `core`, `grep`), so there is no import cycle.

Schema contract
---------------
Every document is wrapped in a fixed envelope::

    {"tool": "ast-outline", "schema_version": N, "command": "<cmd>", ...}

On a user-facing failure the payload is a single `error` object; the
process still exits 0 (the CLI's parallel-batch invariant). Every
field is always present — empty lists as ``[]``, empty strings as
``""`` — so consumers never need defensive ``.get()`` calls.

In `--json` mode the output is **lossless**: content-filtering flags
(``--no-private``, ``--format``, ``--max-members``, ``--view``, …) are
ignored and the complete IR is emitted. The consumer filters itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .core import (
    Declaration,
    ParseResult,
    SymbolMatch,
    _collect_counts,
    _estimate_tokens,
    _size_label,
)
from .grep import GrepFileResult, GrepMatch

# Bump only on a breaking schema change — a renamed, removed or
# retyped field. Additive optional fields do not require a bump.
SCHEMA_VERSION = 1

_TOOL = "ast-outline"


def _emit(obj: dict) -> str:
    """Serialize `obj` to a JSON string.

    ``ensure_ascii=False`` keeps Unicode identifiers (Cyrillic / CJK)
    human-readable instead of ``\\uXXXX`` escapes — the project
    explicitly supports non-ASCII source. Dicts are built in a
    deliberate, stable field order, so no key sorting is applied.
    """
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _envelope(command: str | None, payload: dict) -> dict:
    return {
        "tool": _TOOL,
        "schema_version": SCHEMA_VERSION,
        "command": command or "",
        **payload,
    }


def _rel_path(path: Path, root: Path | None) -> str:
    """Path as a string, relative to `root` when possible."""
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return str(path)


def _common_root(paths: list[Path]) -> Path | None:
    """Common ancestor directory of `paths`, or None when empty.

    Computed over each path's parent so a single-file input yields its
    containing directory rather than the file itself.
    """
    if not paths:
        return None
    try:
        return Path(os.path.commonpath([str(p.parent) for p in paths]))
    except ValueError:
        # Mixed drives on Windows — no shared root.
        return paths[0].parent


# --- IR node serializers --------------------------------------------------


def declaration_to_dict(d: Declaration) -> dict:
    """Serialize a `Declaration` node, recursing through `children`."""
    return {
        "kind": d.kind,
        "name": d.name,
        "signature": d.signature,
        "visibility": d.visibility,
        "native_kind": d.native_kind,
        "bases": list(d.bases),
        "attrs": list(d.attrs),
        "docs": list(d.docs),
        "docs_inside": d.docs_inside,
        "start_line": d.start_line,
        "end_line": d.end_line,
        "start_byte": d.start_byte,
        "end_byte": d.end_byte,
        "doc_start_byte": d.doc_start_byte,
        "match_names": list(d.match_names),
        "children": [declaration_to_dict(c) for c in d.children],
    }


def parse_result_to_dict(r: ParseResult, *, root: Path | None = None) -> dict:
    """Serialize one parsed file. `source` bytes are omitted — the
    whole-file content has no place in a structural document."""
    tokens = _estimate_tokens(r.source)
    return {
        "path": _rel_path(r.path, root),
        "language": r.language,
        "line_count": r.line_count,
        "error_count": r.error_count,
        "tokens_estimate": tokens,
        "size": _size_label(tokens).strip("[]"),
        "counts": _collect_counts(r.declarations),
        "imports": list(r.imports),
        "conditional_imports_count": r.conditional_imports_count,
        "import_regions": [
            {"start": s, "end": e} for (s, e) in r.import_regions
        ],
        "noise_regions": [
            {"start": s, "end": e, "kind": k}
            for (s, e, k) in r.noise_regions
        ],
        "declarations": [declaration_to_dict(d) for d in r.declarations],
    }


def grep_match_to_dict(m: GrepMatch) -> dict:
    return {
        "line": m.line,
        "column": m.column,
        "line_content": m.line_content,
        "kind": m.kind,
        # Compact scope chain — full declaration dicts per match would
        # bloat the document; kind + name reconstructs the breadcrumb.
        "enclosing_path": [
            {"kind": d.kind, "name": d.name} for d in m.enclosing_path
        ],
    }


def grep_file_to_dict(fr: GrepFileResult, *, root: Path | None = None) -> dict:
    return {
        "path": _rel_path(fr.path, root),
        "language": fr.language,
        "matches": [grep_match_to_dict(m) for m in fr.matches],
        "filtered_count": fr.filtered_count,
        "truncated_count": fr.truncated_count,
    }


def symbol_match_to_dict(m: SymbolMatch) -> dict:
    return {
        "qualified_name": m.qualified_name,
        "kind": m.kind,
        "start_line": m.start_line,
        "end_line": m.end_line,
        "ancestor_signatures": list(m.ancestor_signatures),
        "signature": m.decl.signature if m.decl is not None else "",
        "source": m.source,
    }


# --- Top-level command builders -------------------------------------------


def outline_json(
    results: list[ParseResult], *, notes: list[str] | None = None
) -> str:
    payload = {
        "notes": list(notes or []),
        "files": [parse_result_to_dict(r) for r in results],
    }
    return _emit(_envelope("outline", payload))


def digest_json(
    results: list[ParseResult],
    root: Path | None = None,
    *,
    notes: list[str] | None = None,
) -> str:
    if root is None:
        root = _common_root([r.path for r in results])
    files = [parse_result_to_dict(r, root=root) for r in results]
    summary = {
        "files": len(files),
        "types": sum(f["counts"]["types"] for f in files),
        "methods": sum(f["counts"]["methods"] for f in files),
        "fields": sum(f["counts"]["fields"] for f in files),
    }
    payload = {
        "root": str(root) if root is not None else "",
        "notes": list(notes or []),
        "summary": summary,
        "files": files,
    }
    return _emit(_envelope("digest", payload))


def grep_json(
    file_results: list[GrepFileResult], *, notes: list[str] | None = None
) -> str:
    root = _common_root([fr.path for fr in file_results])
    files = [grep_file_to_dict(fr, root=root) for fr in file_results]
    kind_counts: dict[str, int] = {}
    for fr in file_results:
        for m in fr.matches:
            kind_counts[m.kind] = kind_counts.get(m.kind, 0) + 1
    summary = {
        "total_matches": sum(len(fr.matches) for fr in file_results),
        "files_with_matches": sum(1 for fr in file_results if fr.matches),
        "filtered_count": sum(fr.filtered_count for fr in file_results),
        "truncated_count": sum(fr.truncated_count for fr in file_results),
        "kind_counts": dict(sorted(kind_counts.items())),
    }
    payload = {
        "root": str(root) if root is not None else "",
        "notes": list(notes or []),
        "summary": summary,
        "files": files,
    }
    return _emit(_envelope("grep", payload))


def show_json(
    file: str,
    query_results: list[tuple[str, list[SymbolMatch]]],
    *,
    notes: list[str] | None = None,
) -> str:
    """Serialize `show` output.

    `query_results` pairs each requested symbol name with its matches.
    A symbol that was not found is an entry with an empty `matches`
    list; an ambiguous name is an entry with several matches.
    """
    payload = {
        "file": file,
        "notes": list(notes or []),
        "results": [
            {
                "query": query,
                "matches": [symbol_match_to_dict(m) for m in matches],
            }
            for (query, matches) in query_results
        ],
    }
    return _emit(_envelope("show", payload))


def error_json(
    command: str | None, notes: list[str], hint: str | None = None
) -> str:
    """Serialize a user-facing failure as a JSON error object."""
    error: dict = {"notes": list(notes)}
    if hint:
        error["hint"] = hint
    return _emit(_envelope(command, {"error": error}))
