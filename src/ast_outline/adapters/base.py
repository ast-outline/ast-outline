"""Adapter protocol + shared helpers reused by every tree-sitter adapter."""
from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Protocol

from tree_sitter import Language, Node

from ..core import ParseResult


class LanguageAdapter(Protocol):
    language_name: str
    # Human-readable label shown in ``ast-outline help`` (``C#`` / ``C++``
    # / ``TypeScript/JavaScript`` …) — distinct from the lowercase machine
    # ``language_name``. A per-language fact, so it lives on the adapter
    # rather than a central map; ``adapters.supported_languages`` reads it
    # to build the help language table dynamically.
    display_name: str
    extensions: set[str]

    # Leading source-language keywords that introduce a declaration whose
    # name follows — ``class`` / ``struct`` / ``enum`` / ``def`` / ``fn``
    # / ``func`` / ``type`` … Consumed by ``ast_outline.grep`` to strip a
    # keyword an agent habitually prepended to a symbol it was searching
    # for (``grep "enum Foo"`` → search ``Foo`` as a definition). Adapters
    # for data / markup languages that have no such keyword (CSS, SCSS,
    # SQL, YAML, Markdown, HTML) declare an empty set.
    definition_keywords: frozenset[str]

    # Single-line comment markers — a stripped line starting with one of
    # these is a comment for ``grep``'s noise classifier. Languages whose
    # only comment form is block-style (HTML) or that have none
    # (Markdown) declare an empty tuple; block comments are covered by
    # ``ParseResult.noise_regions``, not these prefixes.
    comment_line_prefixes: tuple[str, ...]

    # Line prefixes that mark an import / use / include statement for
    # ``grep``'s ``[import]`` classifier (matched against the stripped
    # line start). Languages whose imports aren't line-shaped (HTML's
    # ``<link>`` / ``<script src>``) declare an empty tuple and rely on
    # ``ParseResult.import_regions`` instead.
    import_line_prefixes: tuple[str, ...]

    # Which digest / file-header rendering family this language belongs
    # to. ``core`` renders each family differently (markdown → TOC,
    # yaml → top-level keys / per-doc separators, css → flat selector
    # tokens, html → depth-capped element map, code → types + members);
    # the adapter declares its family so a new language never needs a
    # ``language == ...`` branch in core. One of: ``"code"``,
    # ``"markdown"``, ``"yaml"``, ``"css"``, ``"html"``.
    render_family: str

    # --- Optional attributes, read via ``getattr`` with a default -------
    #
    # Rare per-language lexical quirks consumed by ``grep``'s
    # classifiers. Only the adapters they apply to declare them:
    #
    # ``single_quote_lifetimes: bool`` (Rust) — a single quote followed
    #     by an identifier char with no closing quote right after is a
    #     lifetime (``'a``), not a string delimiter.
    # ``name_chain_separators: str`` (Lua ``".:"``) — separators whose
    #     ``<sep><identifier>`` runs are skipped when walking from a
    #     match toward a call paren (``a.b:c(...)`` classifies as call).
    # ``call_sugar_openers: tuple[str, ...]`` (Lua ``'"'``, ``"'"``,
    #     ``"{"``, ``"[["``) — tokens that open a paren-less call
    #     argument (``f"x"`` / ``f{...}`` / ``f[[...]]``).
    # ``file_format_hint(declarations) -> str`` (YAML) — short format
    #     annotation for the file header (``OpenAPI 3.0.0, 23 paths``);
    #     empty string when nothing is detected.
    # ``shebang_programs: frozenset[str]`` — interpreter program names
    #     (lowercase, version suffix stripped: ``python``, not
    #     ``python3.13``) that select this adapter when an extensionless
    #     explicit file input starts with a matching ``#!`` line.
    #     Declared only by adapters whose language routinely ships as
    #     extensionless unix CLI scripts; consumed by
    #     ``adapters.get_adapter_for``.
    # ``synthetic_symbol_names: frozenset[str]`` (Markdown ``frontmatter``)
    #     — ``show`` handles the adapter assigns whose text never appears
    #     literally in the source (a frontmatter block opens with ``---``,
    #     not the word ``frontmatter``). Multi-file ``show`` pre-filters
    #     candidate files with a ``grep`` def-scan on the name, which can
    #     never hit a synthetic name; ``cli._resolve_one_symbol`` reads
    #     this set to fall back to a direct scan for exactly these names.

    def parse(self, path: Path) -> ParseResult: ...


# PyCapsule_New does not copy the name — it stores the pointer we hand
# it, so the bytes object has to outlive every capsule we build. Module
# level keeps it alive for the life of the process.
_LANGUAGE_CAPSULE_NAME = b"tree_sitter.Language"

_PyCapsule_New = ctypes.pythonapi.PyCapsule_New
_PyCapsule_New.restype = ctypes.py_object
_PyCapsule_New.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)


def load_language(handle: object) -> Language:
    """Build a `Language` from a grammar package's `language()` handle.

    Current grammar packages return a typed `PyCapsule`, which
    `Language()` takes as-is. A few still return the raw `TSLanguage *`
    as a Python `int` (tree-sitter-scss 1.0.0 is the one we depend on) —
    a path `Language()` accepts but marks deprecated, and which
    tree-sitter 0.26.0 reads back through `PyLong_AsUnsignedLong`. On
    64-bit Windows `unsigned long` is 32 bits while pointers are 64, so
    every real address overflows and importing the adapter dies with

        OverflowError: Python int too large to convert to C unsigned long

    (our issue #8; upstream py-tree-sitter #469, fixed on master after
    0.26.0 but unreleased). Re-wrapping the int in a capsule ourselves
    keeps the pointer pointer-sized all the way down, so the outcome no
    longer depends on the installed tree-sitter or on `sizeof(long)`.

    Only the adapters that need it call this — a grammar that starts
    returning an int would still slip through, which is what
    `test_language_loading` guards against. When every grammar we depend
    on ships a capsule, this helper has no work left to do and should be
    deleted rather than left standing.
    """
    if not isinstance(handle, int):
        return Language(handle)
    capsule = _PyCapsule_New(ctypes.c_void_p(handle), _LANGUAGE_CAPSULE_NAME, None)
    return Language(capsule)


def read_source(path: Path) -> bytes:
    """Read a source file for parsing, with CRLF line endings normalised.

    Every adapter goes through this instead of `path.read_bytes()`. A
    file saved with Windows line endings otherwise leaves a stray `\\r`
    at the end of every line the adapters lift out as text — doc
    comments most visibly, which then render as `// Greet says hi.\\r`.
    That is not a Windows-only concern: a CRLF file checked out on any
    platform produces the same debris.

    Normalising before the parse rather than after keeps everything
    consistent: tree-sitter sees these bytes, `ParseResult.source` holds
    these bytes, and every `start_byte` / `end_byte` slice indexes back
    into them. Line numbers are unaffected — the `\\n` count is the same.
    The one thing that shifts is how byte offsets relate to the file
    *on disk* for a CRLF file; they describe the normalised text we
    report, which is also the text `show` prints.
    """
    return path.read_bytes().replace(b"\r\n", b"\n")


def count_parse_errors(root: Node) -> int:
    """Count `ERROR` and `MISSING` nodes anywhere in the tree.

    tree-sitter parsers always produce a tree — syntax they can't make
    sense of becomes `ERROR` nodes, and expected-but-absent tokens
    become synthetic `MISSING` nodes. Either one means the adapter's
    IR for that region is unreliable, so the outline header reports
    the combined count as a warning.

    Uses `root.has_error` as a fast-path — no walk when the tree is
    clean, which is the common case.
    """
    if not root.has_error:
        return 0
    total = 0
    stack: list[Node] = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            total += 1
        stack.extend(n.children)
    return total
