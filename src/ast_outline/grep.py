"""Structural grep — find pattern in code, return scope-annotated matches.

What this is
------------
A grep variant that returns matches annotated with their enclosing
declaration scope (class/function chain) and a kind classification
(``def`` / ``call`` / ``ref`` / ``import`` / ``string``). Filters out
matches inside comments by default — the noise that raw grep can't
distinguish from real code. String literals ARE searched and tagged
``[string]``: strings are program data (dict / config / translation
keys, asset paths, reflection targets), and hiding a hit there would
hand the agent a silent false "not used".

The intended consumer is an LLM agent that today does:

    grep symbol → 20 hits → read 5 files to understand scope

and instead can do:

    ast-outline grep symbol → all hits with scope and kind in one call.

What this is NOT
----------------
Not an LSP, not a true symbol resolver. Classification is heuristic
and works at the lexical level (per-line for kind, per-byte for
scope). For polymorphism / type-aware "find references", use a real
LSP. For codemods with placeholder patterns, use ``ast-grep`` (a
separate Rust tool, unrelated to this).

Implementation overview
-----------------------
For each file collected via :func:`adapters.collect_files_with_stats`:

1. Read bytes, find every literal/regex match position.
2. Parse via the language adapter to get a Declaration IR with byte
   ranges.
3. For each match position, walk the IR to find the deepest
   declaration containing it (the enclosing scope).
4. Classify the match's kind by looking at line content (whether the
   line is a comment, an import statement, the declaration's own
   signature line, a call expression, etc.).
5. Group by file and render as a Markdown-flavored outline showing
   only the scopes that contain matches.

Limitations of v1
-----------------
- String detection is a quote-counting heuristic — fails on multi-line
  strings, escaped quotes, raw strings, f-strings.
- Comment detection is keyword-prefix only — no block-comment tracking.
- Definition detection assumes match content equals the declaration's
  ``name``; works for class/method names, less reliably for variables.
- No regex captures or replacement — pure search.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .adapters import collect_files_with_stats, get_adapter_for
from .core import Declaration, ParseResult, display_path


# --- Kind constants -------------------------------------------------------
#
# Kept short — they appear inline in every match line of the rendered
# output. Long names would inflate token cost on agent-facing outputs.

KIND_DEF = "def"
KIND_CALL = "call"
KIND_REF = "ref"
KIND_IMPORT = "import"
KIND_COMMENT = "comment"
KIND_STRING = "string"


# Kinds rendered with a visible ``[<kind>]`` tag in output. ``call`` and
# ``ref`` are intentionally omitted — agents trivially infer them from
# line shape (identifier-followed-by-``(`` is a call; otherwise a ref),
# and removing the tags removes both token overhead and the small
# theoretical risk of clashing with literal ``[call]``/``[ref]`` tokens
# in code (C# / Rust attributes, TS bracket-indexing). The remaining
# tags carry information that's NOT inferable from a single line:
# ``def`` is multi-syntax across languages (``def`` / ``fn`` /
# ``function`` / ``public void`` / …); ``import`` is similarly varied
# and groups visually under ``## imports``; ``string`` labels a hit
# inside a string literal (visible by default — strings are program
# data); ``comment`` is only ever emitted via ``--include-noise`` and
# labels why a match was filtered.
_VISIBLE_KIND_TAGS = frozenset({KIND_DEF, KIND_IMPORT, KIND_COMMENT, KIND_STRING})


def _kind_tag(kind: str) -> str:
    """Render the trailing ``[<kind>]`` annotation, or empty string."""
    return f" [{kind}]" if kind in _VISIBLE_KIND_TAGS else ""


# --- Leading definition-keyword stripping --------------------------------
#
# Agents habitually paste the source-language keyword they'd *write* in
# front of a symbol — ``enum ItemSoundFamily``, ``class MailSpec``,
# ``def foo``, ``fn bar``, ``type T`` — into the grep pattern. As a
# literal substring this rarely behaves as intended: on a declaration
# line like ``public enum ItemSoundFamily {`` the match starts on the
# keyword, not the name token, so the def-classifier (which requires the
# match column to fall inside the declaration's name) labels it ``ref``,
# not ``def`` — and ``--kind def`` then drops it, yielding a bare
# "no matches". Stripping the keyword and searching the identifier makes
# the match land on the name token → ``def`` → it survives the filter.
#
# The keyword set is owned by the language adapters (each declares its
# own ``definition_keywords``); ``_definition_keywords()`` unions them.
# The union — not a per-file lookup — is correct here: the strip happens
# once for the whole call, before any file is read, and the agent's
# intent ("I prepended a declaration keyword") is the same regardless of
# which language the matching file turns out to be. A new adapter that
# declares its keywords extends the behavior for free.


@lru_cache(maxsize=1)
def _definition_keywords() -> frozenset[str]:
    """Union of every registered adapter's ``definition_keywords``.

    Adapters that omit the attribute (data / markup languages with no
    leading declaration keyword) contribute nothing. Cached — the
    adapter registry is static for a process.
    """
    from .adapters import ADAPTERS

    kws: set[str] = set()
    for adapter in ADAPTERS:
        kws |= set(getattr(adapter, "definition_keywords", frozenset()))
    return frozenset(kws)


@lru_cache(maxsize=1)
def _def_keyword_strip_re() -> re.Pattern[str]:
    """Compile the ``"<keyword> Identifier"`` matcher from the adapter union.

    Anchored end-to-end so it rejects multi-token patterns
    (``enum Foo : byte``) and anything carrying separators (dots, parens,
    ``::``) — those are not the habitual "I typed the decl line" shape and
    stripping them would be guesswork. Keywords are alternated
    longest-first so e.g. ``function`` wins over ``func`` on ``function
    Foo``.
    """
    kws = _definition_keywords()
    if not kws:
        return re.compile(r"(?!)")  # matches nothing
    alternation = "|".join(
        re.escape(k) for k in sorted(kws, key=lambda k: (-len(k), k))
    )
    return re.compile(
        r"^(?P<kw>" + alternation + r")"
        r"\s+(?P<ident>[A-Za-z_][A-Za-z0-9_]*)$"
    )


def strip_definition_keyword(pattern: str) -> tuple[str, str] | None:
    """Detect the literal ``"<keyword> Identifier"`` shape.

    Returns ``(identifier, keyword)`` when ``pattern`` is exactly a
    leading definition keyword (the union of every adapter's
    ``definition_keywords``) followed by whitespace and a single bare
    identifier — e.g. ``"enum Foo"`` → ``("Foo", "enum")``. Returns
    ``None`` otherwise (single token, multiple tokens, qualified name,
    anything with separators). The caller is responsible for only
    invoking this on literal (non-regex) patterns.
    """
    m = _def_keyword_strip_re().match(pattern)
    if m is None:
        return None
    return m.group("ident"), m.group("kw")


# Regex-syntax fingerprints that have no literal-string interpretation in
# code search — when a pattern contains any of these, the user almost
# certainly meant regex, so auto-promoting saves a follow-up call:
#
#   \|, \d, \w, \s, \b (and uppercase variants) — escape-sequence
#       characters that are unambiguous regex syntax. Especially \| —
#       it's the BRE alternation form (``grep ``Magnet\|Container``),
#       no one types backslash-pipe expecting a literal byte sequence
#       in code search.
#   (?:, (?=, (?!, (?P<, (?< — non-capturing / lookaround / named
#       group constructs. Pure regex syntax.
#   bare ``|`` — ERE-style alternation. In code-symbol searches a
#       literal pipe is rare (mostly bitwise OR like ``READ | WRITE``,
#       which agents grep for differently), so promoting is the right
#       default for the 99% case.
#
# Less ambiguous metachars (``.``, ``*``, ``+``, ``?``, ``[``, ``^``,
# ``$``) are NOT in the fingerprint — they appear legitimately in code
# (qualified names with dots, Java/C# arrays ``T[]``, line-anchor
# uses), and auto-promoting would silently change matching semantics
# in surprising ways.
_REGEX_FINGERPRINT = re.compile(
    r"\\[|dwsbDWSB]"
    r"|\(\?[:=!P<]"
    r"|(?<!\\)\|"
)


def _looks_like_regex(pattern: str) -> bool:
    """True if ``pattern`` contains regex-only syntax that has no literal
    code-search interpretation. See ``_REGEX_FINGERPRINT`` for the
    full list of triggers and their rationale.
    """
    return bool(_REGEX_FINGERPRINT.search(pattern))


# Looser fingerprint — ambiguous metachars that might be regex but
# could also be literal code constructs. Used only for the
# warn-on-no-match safety net: when zero matches were found AND the
# pattern carries one of these, the agent likely typed regex but got
# literal interpretation, so we hint at ``--regex``. We do NOT
# auto-promote on these — the false-positive cost (silently changing
# matching semantics for code that legitimately contains ``[``, ``*``,
# ``\.``) is too high.
#
# Triggers:
#   \., \(, \), \[, \], \{, \} — escaped metachars (regex-only intent)
#   *, +, ? after a non-meta char — quantifiers
#   ., ?, +, * after ``.`` — wildcard-with-quantifier (``.*``, ``.+``,
#       ``.?``). Unlike a bare ``.`` (which legitimately appears in
#       qualified names ``foo.bar``), the ``.<quantifier>`` shape has no
#       literal-code interpretation: agents typing ``Bind.*SaveSystem``
#       expecting regex previously got bare "no matches" with no hint
#       (``.`` and ``*`` individually are too noisy to auto-promote,
#       but the *pair* is unambiguous).
#   ^, $ at edges — line anchors
_AMBIGUOUS_REGEX_FINGERPRINT = re.compile(
    r"\\[.()\[\]{}+*?]"
    r"|[A-Za-z_)\]][*+?]"
    r"|\.[*+?]"
    r"|^\^|\$$"
)


def looks_like_ambiguous_regex(pattern: str) -> bool:
    """True if ``pattern`` carries metachars that might mean regex but
    might also be literal code. Used for the zero-matches hint, NOT
    for auto-promotion. See ``_AMBIGUOUS_REGEX_FINGERPRINT``.
    """
    return bool(_AMBIGUOUS_REGEX_FINGERPRINT.search(pattern))


# Comment- and import-line prefixes are per-language facts and live on
# each adapter (``comment_line_prefixes`` / ``import_line_prefixes`` —
# see ``adapters.base.LanguageAdapter``), not in central tables here.
# The classifier receives the adapter for the file being annotated and
# reads them directly. We don't track block comments via prefixes — the
# adapters cover those through ``ParseResult.noise_regions``.
#
# TypeScript ``export`` is intentionally NOT an import prefix:
# ``export class`` / ``export function`` / ``export const`` are
# declarations, not imports. Re-exports of the form ``export { X } from
# '...'`` get misclassified as plain refs by the line heuristic —
# acceptable trade for not flooding every exported declaration with
# ``[import]``.


# --- Data classes ---------------------------------------------------------


@dataclass
class GrepMatch:
    """One match within a file, with scope and kind annotations."""

    line: int                       # 1-based
    column: int                     # 1-based codepoint offset on its line
    line_content: str               # the source line, with trailing newline stripped
    kind: str                       # one of KIND_* constants
    enclosing_path: list[Declaration] = field(default_factory=list)
    """Outer-to-inner chain of declarations containing this match.
    Empty if the match is at module level (top-level import, top-level
    field, etc.)."""


@dataclass
class GrepFileResult:
    """Matches found in one file, with bookkeeping for the renderer."""

    path: Path
    language: str
    matches: list[GrepMatch] = field(default_factory=list)
    filtered_count: int = 0
    """Matches hidden because they were comments (and
    ``include_noise`` was False). Surfaced in the rendered footer so
    the agent can opt in if relevant."""
    truncated_count: int = 0
    """Visible matches dropped because the per-file ``max_count`` cap
    was hit. Always surfaced in the rendered footer — silent truncation
    would let an agent conclude it found everything when it didn't."""


# --- Search ---------------------------------------------------------------


def grep(
    patterns: str | list[str],
    paths: list[Path],
    *,
    is_regex: bool = False,
    case_insensitive: bool = False,
    word_match: bool = False,
    include_noise: bool = False,
    no_ignore: bool = False,
    exclude: list[str] | None = None,
    max_count: int | None = None,
    kind_filter: set[str] | None = None,
) -> tuple[list[GrepFileResult], int, dict[str, int]]:
    """Find ``patterns`` across ``paths``, return per-file annotated results.

    ``patterns`` may be a single string (back-compat) or a list of
    strings. All patterns share the same mode (``is_regex``,
    ``case_insensitive``, ``word_match``); they are combined into one
    alternation and matches surface together — the consumer reads
    which pattern hit each line from the line content itself, no
    per-pattern grouping.

    ``word_match`` wraps each pattern in ``\\b...\\b`` regex word
    boundaries (POSIX ``grep -w`` semantics). Forces regex mode since
    ``\\b`` is a regex construct; literals are escaped first so dots /
    parens / brackets in the pattern still match literally.

    ``max_count`` caps visible matches per file (POSIX ``grep -m``
    semantics). Cap is applied AFTER noise-filtering, so ``-m 5`` on
    a file with 100 string matches and 3 real ones still returns 3 —
    the cap counts what the agent will actually see, not what was
    pre-filtered. ``None`` (default) disables capping; ``0`` returns
    no matches per file (degenerate but well-defined).

    ``kind_filter`` restricts results to matches of the given kinds
    (``{"def", "call", "ref", "import", "comment", "string"}``). Filtered-
    out matches are dropped silently — they're not "hidden noise" the
    agent might want, they're explicitly excluded by the user's narrowing.
    Caller is responsible for setting ``include_noise=True`` if the
    filter contains ``comment``; otherwise the noise filter runs first
    and would zero out that kind before this filter ever sees it.

    Returns a tuple of (file_results, ignored_dirs_count,
    kind_excluded_counts). When ``include_noise=False`` (the default),
    matches inside comments are counted but filtered out of the
    result; matches inside string literals are always returned, tagged
    ``[string]``.

    ``kind_excluded_counts`` aggregates, across all files, how many
    matches were silently dropped by the ``kind_filter`` narrow —
    keyed by kind (``"ref"``, ``"call"``, ...). Always empty when
    ``kind_filter is None``. The CLI uses this on the empty-result
    path to tell the agent which kinds DID match, so a 0-result
    "no matches" doesn't mask the fact that the symbol is present
    in a different role (e.g. ``EditorPrefs.GetString(...)`` under
    ``--kind call`` — the dot makes ``EditorPrefs`` a ``ref``, not
    a ``call``).

    Empty patterns return no results — ``bytes.find(b"")`` returns 0
    at every position and would fire a "match" on every byte of every
    file, which is never what the caller wants.
    """
    if isinstance(patterns, str):
        patterns = [patterns] if patterns else []
    patterns = [p for p in patterns if p]
    if not patterns:
        return [], 0, {}
    if word_match:
        # Wrap in word boundaries — for literals, escape first so the
        # pattern itself stays literal; for regex, wrap as a non-
        # capturing group so the boundaries bind to the whole pattern.
        if is_regex:
            patterns = [rf"\b(?:{p})\b" for p in patterns]
        else:
            patterns = [rf"\b{re.escape(p)}\b" for p in patterns]
            is_regex = True
    collected = collect_files_with_stats(
        paths, no_ignore=no_ignore, exclude=exclude
    )
    matcher = _build_matcher(patterns, is_regex=is_regex, case_insensitive=case_insensitive)

    out: list[GrepFileResult] = []
    kind_excluded_counts: dict[str, int] = {}
    for path in collected.files:
        adapter = get_adapter_for(path)
        if adapter is None:
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue

        spans = list(matcher(src))
        if not spans:
            continue

        # Parse only files with at least one positional match — pre-filter
        # eliminates ~95% of files in typical projects (large enough that
        # even a slow tree-sitter grammar isn't worth running on most).
        # ``import_regions`` are populated automatically by populating
        # adapters via piggyback on the existing imports walk — no
        # separate flag, near-zero cost.
        try:
            result = adapter.parse(path)
        except Exception:
            continue

        file_result, file_kind_excluded = _annotate_matches(
            result, spans, src,
            adapter=adapter,
            include_noise=include_noise,
            kind_filter=kind_filter,
        )
        for k, n in file_kind_excluded.items():
            kind_excluded_counts[k] = kind_excluded_counts.get(k, 0) + n
        if max_count is not None and len(file_result.matches) > max_count:
            file_result.truncated_count = len(file_result.matches) - max_count
            file_result.matches = file_result.matches[:max_count]
        if (
            file_result.matches
            or file_result.filtered_count
            or file_result.truncated_count
        ):
            out.append(file_result)

    return out, collected.ignored_dirs, kind_excluded_counts


# --- did-you-mean (empty-result recovery) ---------------------------------
#
# On a true no-match, the agent's next move is usually a blind retry with
# a permuted name (singular/plural, a typo, a wrong word) — the
# transcripts show chains of 5+ such misses. Surfacing the closest real
# symbol in scope collapses that loop to one corrected call. This is the
# only code path that parses files with NO positional match, so it is
# gated behind the empty-result branch in the CLI and bounded by
# ``_DYM_MAX_FILES`` — a no-match already cost the agent a wasted call,
# so a bounded parse pass to recover the intended symbol is a good trade,
# but a pathological monorepo must not turn a no-match into a long stall.

# Upper bound on files parsed to build the suggestion pool. The
# suggestion is best-effort: if the intended symbol lives past the cap we
# simply emit no hint (same as the pre-existing behavior), never a wrong
# one — difflib's cutoff already rejects anything not genuinely close.
_DYM_MAX_FILES = 2000

# Minimum pattern length to attempt a suggestion. Below this, edit
# distance is dominated by noise (every 2-3 char token is "close" to
# dozens of symbols) and the hint would be useless churn.
_DYM_MIN_PATTERN_LEN = 4

# difflib similarity cutoff (0..1). 0.6 is difflib's own default — high
# enough to reject blind-guess names that share no structure with real
# symbols (``JsonStore`` vs ``_atomic_write_json``), low enough to catch
# plural/singular and single-character typos.
_DYM_CUTOFF = 0.6

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _walk_declaration_names(
    decls: list[Declaration], acc: dict[str, list[str]]
) -> None:
    """Recursively collect ``name -> [kind, ...]`` over a declaration tree."""
    for d in decls:
        if d.name:
            acc.setdefault(d.name, []).append(d.kind)
        if d.children:
            _walk_declaration_names(d.children, acc)


def suggest_similar_symbols(
    pattern: str,
    paths: list[Path],
    *,
    no_ignore: bool = False,
    exclude: list[str] | None = None,
    limit: int = 3,
) -> list[tuple[str, str, int]]:
    """Best-effort "did you mean" candidates for an empty grep result.

    Parses the files in ``paths`` (capped at ``_DYM_MAX_FILES``), gathers
    every declaration name, and returns up to ``limit`` names closest to
    ``pattern`` by ``difflib`` similarity. Each entry is
    ``(name, kind, count)`` where ``kind`` is the declaration's canonical
    kind and ``count`` is how many declarations in scope carry that name.

    Returns an empty list when ``pattern`` is not a bare identifier, is
    too short, or nothing in scope is close enough — the CLI then emits
    no hint, exactly as before. Never raises: parse failures on
    individual files are skipped (mirrors ``grep`` itself).
    """
    if len(pattern) < _DYM_MIN_PATTERN_LEN or not _IDENTIFIER_RE.match(pattern):
        return []

    collected = collect_files_with_stats(paths, no_ignore=no_ignore, exclude=exclude)
    names: dict[str, list[str]] = {}
    for path in collected.files[:_DYM_MAX_FILES]:
        adapter = get_adapter_for(path)
        if adapter is None:
            continue
        try:
            result = adapter.parse(path)
        except Exception:
            continue
        _walk_declaration_names(result.declarations, names)

    # Don't suggest the pattern itself (it can appear as a declaration
    # name even when no *line* matched — e.g. filtered as noise).
    candidates = [n for n in names if n != pattern]
    close = difflib.get_close_matches(
        pattern, candidates, n=limit, cutoff=_DYM_CUTOFF
    )
    out: list[tuple[str, str, int]] = []
    for name in close:
        kinds = names[name]
        # Most common canonical kind wins the label; total carries the
        # count (overloads / same-name-across-files). Highest count first
        # (``-count``), ties broken by the alphabetically-first kind, so
        # the label is deterministic regardless of parse order or hash
        # seed.
        counts = Counter(kinds)
        top = min(counts, key=lambda k: (-counts[k], k))
        out.append((name, top, len(kinds)))
    return out


def _build_matcher(
    patterns: list[str], *, is_regex: bool, case_insensitive: bool
):
    """Return a callable ``src_bytes -> Iterable[(start, end)]`` (byte offsets).

    Both ends matter: ``end`` lets the classifier peek at the character
    immediately after the match to distinguish ``foo`` (ref) from
    ``foo(`` (call).

    Fast path — single literal, case-sensitive — uses ``bytes.find``
    in a loop. The C implementation (Crochemore-Perrin, ~1-3 GB/s)
    avoids regex compile/run overhead for the 90% of queries that are
    symbol-name lookups.

    Slow path — multiple patterns or regex or case-insensitive — uses
    ``re.finditer`` with the patterns combined via alternation. For
    literal mode the patterns are escaped first; for regex they're
    wrapped in non-capturing groups to keep precedence intact.

    The slow path matches against the **decoded str**, not the raw
    bytes. In bytes mode ``\\b`` / ``\\w`` / ``IGNORECASE`` are
    ASCII-only: a ``-w`` search for a Cyrillic identifier never finds a
    word boundary (every UTF-8 continuation byte is a non-word byte) and
    ``-i`` can't case-fold non-ASCII — both silently return "no matches"
    on source the project explicitly supports. Matching on str gives
    Unicode-correct semantics; the codepoint spans are converted back to
    byte offsets (the contract of this function) incrementally — matches
    arrive in ascending order, so the conversion is amortized O(n).
    ``surrogateescape`` keeps the round-trip byte-exact even for files
    with invalid UTF-8 (each bad byte maps to one surrogate codepoint
    and encodes back to the same byte; ``replace`` would substitute a
    3-byte replacement char and skew every following offset).
    """
    if not is_regex and not case_insensitive and len(patterns) == 1:
        needle = patterns[0].encode("utf-8")
        needle_len = len(needle)

        def _literal_iter(src: bytes) -> Iterable[tuple[int, int]]:
            idx = 0
            while True:
                pos = src.find(needle, idx)
                if pos < 0:
                    return
                yield pos, pos + needle_len
                # Advance by 1 (not len(needle)) so overlapping matches
                # like searching ``aa`` in ``aaa`` both surface.
                idx = pos + 1

        return _literal_iter

    flags = re.IGNORECASE if case_insensitive else 0
    if is_regex:
        # Wrap each pattern in a non-capturing group so an alternation
        # like ``a|b`` inside one of them doesn't bind the wrong way.
        combined = "|".join("(?:" + p + ")" for p in patterns)
    else:
        # Literals — escape each, then alternate.
        combined = "|".join(re.escape(p) for p in patterns)
    rx = re.compile(combined, flags)

    def _regex_iter(src: bytes) -> Iterable[tuple[int, int]]:
        text = src.decode("utf-8", errors="surrogateescape")
        last_cp = 0
        last_byte = 0
        for m in rx.finditer(text):
            s_cp, e_cp = m.start(), m.end()
            last_byte += len(
                text[last_cp:s_cp].encode("utf-8", "surrogateescape")
            )
            start_byte = last_byte
            last_byte += len(
                text[s_cp:e_cp].encode("utf-8", "surrogateescape")
            )
            last_cp = e_cp
            yield start_byte, last_byte

    return _regex_iter


# --- Annotation -----------------------------------------------------------


def _annotate_matches(
    result: ParseResult,
    spans: list[tuple[int, int]],
    src: bytes,
    *,
    adapter,
    include_noise: bool,
    kind_filter: set[str] | None = None,
) -> tuple[GrepFileResult, dict[str, int]]:
    """Annotate raw byte spans with scope and kind, and filter noise.

    Returns ``(file_result, kind_excluded_counts)``. The second item
    is a kind→count map of matches dropped by the ``kind_filter``
    narrow — populated only when ``kind_filter is not None``, and
    only with kinds that were actually excluded (so ``{"ref": 42}``,
    not ``{"call": 0, "ref": 42, ...}``). Caller aggregates these
    across files to power the "no <kind> matches; <K>=<N> excluded"
    hint emitted by the CLI on empty results.
    """

    # Pre-compute line offsets so we can map byte → line in O(log N) per match.
    line_offsets = _compute_line_offsets(src)
    # Multi-line string and block comment regions, populated by the
    # adapter (Python + others — see ParseResult.noise_regions).
    # Empty list means "rely on line-only heuristics" — the legacy
    # path for adapters that haven't been updated.
    noise_regions = result.noise_regions
    # Byte ranges that belong to import declarations (single-line OR
    # block form). Lets the classifier promote matches inside Go
    # ``import (...)`` blocks, Python ``from X import (\n ...)``,
    # TS / Rust / PHP multi-line import groups to ``[import]``
    # directly — the line-prefix heuristic in ``_classify_match``
    # only sees the opening line and would otherwise classify inner
    # lines as ``[string]`` or ``[ref]``. See ParseResult.import_regions.
    import_regions = result.import_regions
    file_result = GrepFileResult(path=result.path, language=result.language)
    kind_excluded: dict[str, int] = {}

    for pos, end in spans:
        line_no = _byte_to_line(line_offsets, pos)
        line_start = line_offsets[line_no - 1]
        next_line_start = (
            line_offsets[line_no] if line_no < len(line_offsets) else len(src)
        )
        line_bytes = src[line_start:next_line_start]
        # Strip trailing CR (Windows) and LF — the renderer adds its own.
        line_content = line_bytes.rstrip(b"\r\n").decode("utf-8", errors="replace")
        # ``pos`` / ``end`` are BYTE offsets into ``src``, but the
        # downstream classifier (``_classify_match`` and its callees
        # ``_column_inside_string`` / ``_next_call_paren_after`` /
        # ``_column_inside_name``) indexes ``line_content`` — a ``str``
        # whose elements are *codepoints*, not bytes. Decode the
        # prefix once per side and use its codepoint length, so a line
        # containing multi-byte UTF-8 (Cyrillic, CJK, emoji, accented
        # Latin, math symbols) doesn't shift every cursor past the
        # intended position. ASCII-only lines: byte offset equals
        # codepoint index, so the conversion is a no-op for the 99%
        # case but correct for the rest.
        column = (
            len(
                line_bytes[: pos - line_start].decode("utf-8", errors="replace")
            )
            + 1
        )
        match_end_column = (
            len(
                line_bytes[: end - line_start].decode("utf-8", errors="replace")
            )
            + 1
        )

        # Classification flow:
        #
        #  Step 1 — establish a baseline kind via the existing pipeline:
        #    a. ``noise_regions`` (string / comment ranges from the
        #       adapter, when populated) — authoritative for multi-line
        #       strings and block comments.
        #    b. ``_classify_match`` — line-based fallback covering
        #       comment-prefix, import-prefix, in-string, and
        #       call-vs-ref.
        #
        #  Step 2 — upgrade to ``KIND_IMPORT`` if the position falls
        #    inside an ``import_regions`` range AND the baseline isn't
        #    already a comment. The comment guard matters: a ``//``
        #    line *inside* an import block is still a comment to the
        #    reader, not an import. Strings inside import regions
        #    (Go's ``"fmt"`` package paths) DO upgrade — that's the
        #    whole point of the field.
        #
        # The def re-classification below can still upgrade the final
        # kind to ``KIND_DEF`` when the match lands on a declaration's
        # own name.
        region_kind = _kind_at_byte(noise_regions, pos)
        if region_kind is not None:
            kind = region_kind
        else:
            kind = _classify_match(
                line_content=line_content,
                column=column,
                match_end_column=match_end_column,
                adapter=adapter,
            )

        if kind != KIND_COMMENT and _pos_in_import_region(import_regions, pos):
            kind = KIND_IMPORT
        # Re-classify if the scope walk reveals this match is the
        # definition's own name. Three conditions must hold: (1) we are
        # inside some declaration; (2) the match line is that
        # declaration's name line; (3) the match's column falls inside
        # the declaration's name token on that line — guards against
        # marking `Handler` in `def run_forever(h: Handler)` as a `def`
        # of `run_forever` (the match isn't on the name token).
        #
        # The line compared is the declaration's `name_line`, not its
        # `start_line`: decorated / attributed / annotated declarations
        # extend `start_line` UP over the decorator lines (so `show`
        # prints them), which drops the name token onto a lower line.
        # Comparing against `start_line` would then tag [def] on a
        # decorator line — worst of all when the decorator's name equals
        # the definition's (`@bar\ndef bar`) — and miss the real
        # signature line. `name_line` points at the signature; it falls
        # back to `start_line` (0 default) for the common undecorated case.
        enclosing = _find_enclosing(result.declarations, pos)
        if (
            enclosing
            and (enclosing[-1].name_line or enclosing[-1].start_line) == line_no
            and _column_inside_name(line_content, column, enclosing[-1].name)
        ):
            kind = KIND_DEF

        # Only comments are noise-filtered by default. String literals
        # are searched and shown (tagged ``[string]``): strings are
        # program data — dict/config/translation keys, asset paths,
        # ``animator.Play("State")`` — and hiding a hit there produces
        # the worst agent failure mode, a silent false "not used".
        # Comments stay hidden: commented-out code is the classic false
        # positive that misleads an agent into treating dead code as
        # live.
        if not include_noise and kind == KIND_COMMENT:
            # Surface in ``filtered_count`` only when a future
            # ``--include-noise`` would actually make this visible. With
            # a ``kind_filter`` that excludes ``comment``, the "pass
            # --include-noise to see" hint would be misleading — the
            # kind narrow would still drop those matches even with
            # noise enabled. Suppress the count to keep the footer honest.
            if kind_filter is None or kind in kind_filter:
                file_result.filtered_count += 1
            elif kind_filter is not None:
                # Doubly hidden: noise filter would drop it, AND the
                # ``--kind`` narrow would too. Count toward
                # ``kind_excluded`` so the CLI's empty-result hint
                # surfaces "retry with --kind ..., comment" — that
                # retry auto-enables ``--include-noise`` in the CLI
                # layer, so the user's one-shot fix actually works.
                # Without this, a pattern that lives only in comments
                # vanishes silently under any non-comment ``--kind``
                # narrow (def/call/ref/import/string).
                kind_excluded[kind] = kind_excluded.get(kind, 0) + 1
            continue

        if kind_filter is not None and kind not in kind_filter:
            # Explicit user narrowing — don't bump ``filtered_count``
            # (that's reserved for the noise filter, which the user can
            # opt back into via ``--include-noise``). A ``--kind`` skip
            # is a deliberate exclusion, not noise to surface.
            #
            # But DO accumulate per-kind so the CLI can tell an agent
            # "you got 0 results because --kind <X> excluded 42 ref
            # and 3 def" instead of bare "no matches" — far more
            # actionable when the wrong kind narrow is the only thing
            # standing between the agent and a useful answer.
            kind_excluded[kind] = kind_excluded.get(kind, 0) + 1
            continue

        file_result.matches.append(
            GrepMatch(
                line=line_no,
                column=column,
                line_content=line_content,
                kind=kind,
                enclosing_path=enclosing,
            )
        )

    return file_result, kind_excluded


def _kind_at_byte(
    regions: list[tuple[int, int, str]], pos: int
) -> str | None:
    """Return the kind of the region containing ``pos``, or ``None``.

    Linear scan — fine because noise regions per file rarely exceed
    a few dozen. If profile reveals this matters, swap to bisect.
    """
    for start, end, kind in regions:
        if start <= pos < end:
            return kind
        if start > pos:
            return None
    return None


def _pos_in_import_region(
    regions: list[tuple[int, int]], pos: int
) -> bool:
    """True if ``pos`` falls inside any ``(start, end)`` import region.

    Same linear-scan rationale as :func:`_kind_at_byte` — import
    declarations per file are typically <50 even for heavy modules.
    """
    for start, end in regions:
        if start <= pos < end:
            return True
        if start > pos:
            return False
    return False


def _compute_line_offsets(src: bytes) -> list[int]:
    """Return a list where ``offsets[i]`` is the byte index of line i+1.

    Line numbers are 1-based; ``offsets[0] == 0`` is line 1's start.
    Line N+1 (one past the last line) is ``len(src)``, conceptually,
    but we don't append it — callers use the next-index lookup with a
    bounds check.
    """
    offsets = [0]
    for i, b in enumerate(src):
        if b == 0x0A:  # \n
            offsets.append(i + 1)
    return offsets


def _byte_to_line(offsets: list[int], pos: int) -> int:
    """Binary-search the line number (1-based) for a byte position."""
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _find_enclosing(decls: list[Declaration], pos: int) -> list[Declaration]:
    """Return the chain of declarations whose byte range contains ``pos``.

    Outer-most first. Returns ``[]`` if the position is at module
    level (e.g. a top-level import or assignment outside any class).

    Skips declarations whose byte range is unset (``end_byte == 0``)
    rather than testing ``start_byte`` truthily — ``start_byte == 0``
    is the legitimate value for a file-leading declaration and would
    be silently excluded by an `if d.start_byte` check.
    """
    for d in decls:
        if d.end_byte > d.start_byte and d.start_byte <= pos < d.end_byte:
            return [d] + _find_enclosing(d.children, pos)
    return []


# --- Kind classification --------------------------------------------------


def _classify_match(
    *, line_content: str, column: int, match_end_column: int, adapter
) -> str:
    """Heuristically classify a match as comment / string / import / call / ref.

    ``def`` is decided in :func:`_annotate_matches` after the scope
    walk — it requires knowing whether the match is on a declaration's
    own signature line.

    All per-language lexical facts come from ``adapter`` — the comment /
    import line prefixes plus the optional quirk attributes documented
    on ``adapters.base.LanguageAdapter`` (Rust lifetime quotes, Lua
    chain separators and sugar-call openers).
    """
    stripped = line_content.lstrip()

    # Comment check first — if the entire line is a comment, the match
    # is in a comment regardless of position.
    comment_prefixes = adapter.comment_line_prefixes
    for prefix in comment_prefixes:
        if stripped.startswith(prefix):
            return KIND_COMMENT

    # In-line trailing comment — match is past the comment marker on
    # this line. e.g. `x = 1  # call save here` — the `save` after `#`
    # is a comment match, not code.
    for prefix in comment_prefixes:
        idx = line_content.find(prefix)
        if 0 <= idx < column - 1:
            return KIND_COMMENT

    # Import-line check — also coarse, but imports rarely look like
    # other constructs in practice.
    for prefix in adapter.import_line_prefixes:
        if stripped.startswith(prefix):
            return KIND_IMPORT

    # String check — heuristic. Count unescaped quotes before the
    # match position. Odd → inside a string. This is wrong for raw
    # strings, multi-line strings, escaped quotes inside strings —
    # acknowledged in the module docstring.
    if _column_inside_string(
        line_content,
        column,
        single_quote_lifetimes=getattr(adapter, "single_quote_lifetimes", False),
    ):
        return KIND_STRING

    # Call vs. ref — peek at the first significant char immediately
    # after the match's last byte, skipping syntax that legitimately
    # appears between an identifier and its argument list:
    #   - generic type args: ``foo<T>()``, ``foo<T, U>()``
    #   - turbofish: ``foo::<T>()`` (Rust)
    #   - optional chain: ``foo?.()`` (TS)
    #   - non-null assertion: ``foo!()`` (TS)
    # Without this skip, a generic call like ``genericCall<string>()``
    # would land on ``<`` and fall through to KIND_REF — the most
    # painful misclassification for TS / Rust / Java / C# code.
    if _next_call_paren_after(
        line_content,
        match_end_column - 1,
        name_chain_separators=getattr(adapter, "name_chain_separators", ""),
        call_sugar_openers=getattr(adapter, "call_sugar_openers", ()),
    ):
        return KIND_CALL
    return KIND_REF


def _next_call_paren_after(
    line_content: str,
    start: int,
    *,
    name_chain_separators: str = "",
    call_sugar_openers: tuple[str, ...] = (),
) -> bool:
    """True if a ``(`` follows ``start``, after skipping call-prefixes.

    Walks past whitespace, generic-arg blocks (``<...>`` or ``[...]``
    balanced), bare generic closers ``>`` / ``]`` left over from a
    match that already consumed the opener, turbofish (``::``), TS
    optional-chain (``?.``), and TS non-null (``!``) until either
    ``(`` is found (→ True) or any other significant character is
    hit (→ False).

    Before any of those, a one-shot rest-of-identifier skip handles
    the case where the match ended *inside* a word. Two real
    scenarios feed this:

    1. Regex alternation ``foo|fooBar`` against ``fooBar(x)`` —
       Python ``re`` picks the leftmost alternative ``foo``, the
       match ends on ``B`` (mid-identifier ``Bar``).
    2. Literal substring search ``foo`` against ``fooBar(x)`` —
       same shape, different entry point (``bytes.find`` returns
       the match end at the same mid-identifier position).

    Without skipping the hidden tail every ``(`` is invisible to
    the walker and the call classifies as ``ref``. Symmetric to
    the bare-closer skip below — both cover matches whose end
    position doesn't align with a token boundary. Applies only at
    the entry position, not mid-walk, since legitimate intermediate
    identifiers (e.g. ``foo<T>bar()``) aren't a real-language shape.

    The bare-closer skip handles two real cases:

    1. ``ast-outline grep "Bind.*SaveSystem"`` (regex, greedy). The
       match ``Bind<SaveSystem`` ends on ``>`` — the walker starts
       on the closer with no matching opener to skip. Without the
       closer-skip, every generic call on the line classifies as
       ``ref`` instead of ``call``.
    2. ``ast-outline grep "Bind<SaveSystem>"`` (literal). The match
       ends past ``>``, on ``(`` — already handled by the trailing
       paren check; not affected by this branch.

    Covers ``<...>`` (C# / Java / Kotlin / Scala-types / TS / Rust /
    C++) and ``[...]`` (Go 1.18+ generics, Scala type-args).

    Caveats:
    - Bracket balancing is naive: ``a < b ? c : d`` could be misread
      as starting an unbalanced ``<`` block. In real code this rarely
      coincides with a function call shape; agents asking "is this a
      call?" are happy with ~99% precision.
    - Comparisons ``x = a < b > (c)`` look the same as a TS generic
      call. We err on the side of classifying as call — it's the
      rarer false positive in code-search contexts. The closer-skip
      extends the same bias to patterns like ``a > (c)`` where a
      match ending on ``>`` is now treated as a call if ``(`` follows;
      this is consistent policy across the walker.
    - Identifier skip is Unicode-aware (``isalpha()`` /
      ``isdecimal()`` / ``_``), so Cyrillic / CJK / accented-Latin
      identifiers compose with the same alternation-leftmost-wins
      shape as ASCII. Companion to the byte→codepoint conversion
      at the boundary in ``_annotate_matches`` — without that
      conversion the walker lands at the wrong codepoint and a
      Unicode-aware skip can't help; without the broader skip the
      conversion lands at the right codepoint but stops on the
      next identifier char. The two fixes are necessary together.
      Narrower than ``isalnum()`` on purpose: that wider check
      would also accept Unicode No-category numerics (``²``,
      ``¼``) which are not identifier chars in any supported
      language; treating them as identifier tail would extend
      the call-bias policy to contrived shapes that don't arise
      in real code. Known gap (not introduced by this patch):
      combining marks (Devanagari virama, vowel signs, Arabic
      diacritics — Mn/Mc categories) are XID_Continue in Python /
      Rust / JS / Swift but ``isalpha()`` returns False for them;
      a regex alternation whose shorter prefix ends right before
      a combining mark would stop the skip and classify as ref.
      Practical risk is low — combining-mark identifiers normalize
      to NFC and rarely surface in source code searches. The other char-class checks below (``<`` /
      ``>`` / ``::`` / ``?.`` / ``!``) stay ASCII — they're
      language-syntax markers, not identifier content.
    - Substring matches against a longer identifier now classify as
      call: ``foo`` against ``fooBar(x)`` → ``[call]`` (was ``[ref]``).
      This is intentional and consistent with the documented "bias
      toward call" — agents searching for a substring inside a
      called identifier usually want the call site surfaced. False
      positives (``foo`` against ``fooBar = 1``) still classify as
      ``ref`` because the trailing ``(`` check fails.
    """
    i = start
    n = len(line_content)
    # One-shot rest-of-identifier skip. See docstring above.
    # ``isalpha() or isdecimal()`` deliberately narrower than ``isalnum()``:
    # ``isalnum()`` also matches Unicode No-category numerics like
    # ``²`` / ``¼`` which aren't identifier chars in any supported
    # language. ``isalpha()`` covers ASCII / Cyrillic / CJK / accented
    # Latin letters; ``isdecimal()`` covers ASCII ``[0-9]`` plus
    # Unicode decimal digits (Arabic-Indic, Devanagari, etc.) that
    # languages with broader Unicode identifier rules do accept.
    while i < n:
        ch = line_content[i]
        if ch == "_" or ch.isalpha() or ch.isdecimal():
            i += 1
            continue
        break
    while i < n:
        ch = line_content[i]
        if ch in " \t":
            i += 1
            continue
        if ch == "?" and i + 1 < n and line_content[i + 1] == ".":
            i += 2
            continue
        if ch == "!":
            i += 1
            continue
        if ch == ":" and i + 1 < n and line_content[i + 1] == ":":
            i += 2
            continue
        # Adapter-declared lexical quirks. Placed BEFORE the ``<[``
        # generic-args block so a multi-char sugar opener like Lua's
        # ``[[`` long-string call wins over the would-be ``[...]``
        # generic-args balanced walk (languages that declare sugar
        # openers — Lua — have no generics, so nothing is lost).
        if name_chain_separators:
            # Method / field chain: ``<sep>identifier`` — skip the
            # separator and the entire identifier that follows, then
            # keep walking. Composes naturally: ``a.b.c(`` walks
            # ``.``→``b``→``.``→``c`` and lands on ``(``. Bias toward
            # call: agents searching for a name appearing as the
            # receiver of a chained call almost always want the call
            # site surfaced (same rationale as the one-shot
            # identifier-tail skip above).
            if ch in name_chain_separators and i + 1 < n:
                nxt = line_content[i + 1]
                if nxt == "_" or nxt.isalpha():
                    i += 1
                    while i < n:
                        c2 = line_content[i]
                        if c2 == "_" or c2.isalpha() or c2.isdecimal():
                            i += 1
                            continue
                        break
                    continue
        if call_sugar_openers:
            # Paren-less call argument (Lua ``f"x"`` / ``f'x'`` /
            # ``f{...}`` / ``f[[...]]``) — same call semantics as a
            # parenthesised call, same classification. Which tokens
            # qualify is the adapter's call: Lua deliberately lists
            # ``[[`` but not bare ``[`` (``f[key]`` is a subscript).
            if any(
                line_content.startswith(opener, i)
                for opener in call_sugar_openers
            ):
                return True
        if ch in "<[":
            close = ">" if ch == "<" else "]"
            depth = 1
            i += 1
            while i < n and depth > 0:
                if line_content[i] == ch:
                    depth += 1
                elif line_content[i] == close:
                    depth -= 1
                i += 1
            continue
        if ch in ">]":
            i += 1
            continue
        return ch == "("
    return False


def _column_inside_string(
    line_content: str, column: int, *, single_quote_lifetimes: bool = False
) -> bool:
    """True if column is inside an open string literal on this line.

    Counts unescaped single and double quotes preceding the column.
    Tracks state so a `"` inside a `'...'` string doesn't toggle the
    other quote's count. Imperfect — see module docstring caveats.

    ``single_quote_lifetimes`` (declared by the Rust adapter): a single
    quote followed by an identifier char with no closing quote right
    after is a lifetime (``'a``, ``'static``, ``'_``), not a string
    delimiter. Lifetimes are unpaired, so counting them flips the
    in-string state for the entire rest of the line — every match after
    ``&'a`` on a signature line would silently classify as ``[string]``
    and be noise-filtered. Char literals (``'x'``, ``'\\n'``) still
    toggle normally: they either close within two chars or start with a
    backslash escape.
    """
    in_single = False
    in_double = False
    i = 0
    target = column - 1
    while i < target and i < len(line_content):
        ch = line_content[i]
        if ch == "\\" and i + 1 < len(line_content):
            i += 2
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            if (
                single_quote_lifetimes
                and not in_single
                and i + 1 < len(line_content)
                and (line_content[i + 1].isalpha() or line_content[i + 1] == "_")
                and (i + 2 >= len(line_content) or line_content[i + 2] != "'")
            ):
                # Lifetime — skip the quote, let the identifier chars
                # run through the normal scan.
                i += 1
                continue
            in_single = not in_single
        i += 1
    return in_single or in_double


def _column_inside_name(line_content: str, column: int, name: str) -> bool:
    """True if ``column`` falls inside the first occurrence of ``name``.

    Used to verify that a match on a declaration's signature line is
    actually positioned on the declaration's own identifier — not on a
    type annotation or default value elsewhere on the same line. The
    "first occurrence" assumption matches how source files are written:
    ``def foo(...)`` always has the name immediately after the keyword,
    before any other identifier on the line.
    """
    if not name:
        return False
    name_start = line_content.find(name)
    if name_start < 0:
        return False
    match_start = column - 1
    return name_start <= match_start < name_start + len(name)


# --- Rendering ------------------------------------------------------------
#
# Output format (see CLI guide for full spec — repeated here for easy
# reference when modifying the renderer):
#
#   # path/to/file.py (N matches)
#
#   ## imports
#     > L1  from .foo import Bar [import]
#
#   ## matches
#   class FooHandler  L98-145
#       def update(...)  L100-115
#           > L108: bar.save() [call]
#
#   # 3 matches in comments hidden — pass --include-noise to see
#
# The `## imports` section is omitted when there are no import matches.
# The `## matches` section is always present when at least one non-import
# match exists. The trailing filtered note is printed only when matches
# were actually filtered.


def render_grep(file_results: list[GrepFileResult]) -> str:
    """Render the agent-facing grep output for a list of file results."""
    blocks: list[str] = []
    for fr in file_results:
        blocks.append(_render_file(fr))
    return "\n\n".join(blocks)


def _render_file(fr: GrepFileResult) -> str:
    """Render one file's worth of matches as a self-contained block."""
    visible = len(fr.matches)
    header = (
        f"# {display_path(fr.path)} "
        f"({visible} match{'es' if visible != 1 else ''})"
    )

    import_matches = [m for m in fr.matches if m.kind == KIND_IMPORT]
    code_matches = [m for m in fr.matches if m.kind != KIND_IMPORT]

    sections: list[str] = [header]

    if import_matches:
        sections.append("")
        sections.append("## imports")
        for m in import_matches:
            sections.append(_render_top_level_match(m))

    if code_matches:
        sections.append("")
        sections.append("## matches")
        sections.extend(_render_code_matches(code_matches))

    # Surface the hidden comment matches only when there are NO
    # visible matches in this file. Then the header reads "(0 matches)"
    # and, without this line, the agent would wrongly conclude the symbol
    # is absent — the note is the false-negative guard. When the file
    # already has visible matches, the "also N in comments" line is
    # low-value noise agents ignore (~0.7% act on it), so it's suppressed.
    # The count rides the JSON `filtered_count` field in either case.
    if fr.filtered_count and not fr.matches:
        sections.append("")
        sections.append(
            f"# {fr.filtered_count} match{'es' if fr.filtered_count != 1 else ''} "
            "in comments hidden — pass --include-noise to see"
        )

    if fr.truncated_count:
        sections.append("")
        sections.append(
            f"# truncated — {fr.truncated_count} more match"
            f"{'es' if fr.truncated_count != 1 else ''} in this file "
            "(raise --max-count to see)"
        )

    return "\n".join(sections)


def _render_top_level_match(m: GrepMatch) -> str:
    """Render a match that has no enclosing scope (module-level)."""
    return f"  > L{m.line}: {m.line_content.strip()}{_kind_tag(m.kind)}"


def _render_code_matches(matches: list[GrepMatch]) -> list[str]:
    """Render matches grouped by their enclosing declaration tree.

    Each declaration in any match's enclosing path is printed once with
    its signature and line range. Matches that are themselves the
    definition of an enclosing declaration are inlined into the
    signature line (with ``[def]`` annotation) instead of getting their
    own ``>`` line.
    """
    # Group matches by their enclosing path (tuple of Declaration ids).
    # Keep input order so the output is stable and deterministic.
    lines: list[str] = []
    rendered_decls: set[int] = set()

    # Collect the set of decl ids that are themselves a def-match — we
    # annotate their signature lines and skip emitting a separate `>`
    # line for them.
    def_target_ids: set[int] = set()
    for m in matches:
        if m.kind == KIND_DEF and m.enclosing_path:
            def_target_ids.add(id(m.enclosing_path[-1]))

    # Render each match in input order, emitting any new ancestor
    # declarations along the way.
    for m in matches:
        for depth, d in enumerate(m.enclosing_path):
            if id(d) in rendered_decls:
                continue
            rendered_decls.add(id(d))
            indent = "    " * depth
            tag = " [def]" if id(d) in def_target_ids else ""
            lines.append(f"{indent}{d.signature}{d.lines_suffix()}{tag}")

        # If this match is the def of its deepest enclosing declaration,
        # the signature line already carries [def] — don't add a `>` line.
        if m.kind == KIND_DEF and m.enclosing_path and id(m.enclosing_path[-1]) in def_target_ids:
            continue

        # `>` lines indent one level deeper than their enclosing scope.
        # Top-level matches (empty enclosing_path) get a 2-space indent
        # to align with the `## matches` section visually.
        depth = len(m.enclosing_path)
        indent = "    " * depth if depth else "  "
        line_text = m.line_content.strip()
        lines.append(f"{indent}> L{m.line}: {line_text}{_kind_tag(m.kind)}")

    return lines
