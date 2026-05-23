"""Language-agnostic intermediate representation + renderers.

Adapters parse source files into `ParseResult` containing a tree of
`Declaration` nodes. This module renders that IR to human-readable
outputs (outline, digest) and runs symbol search (find_symbols).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional


# Canonical kinds used across languages. Adapters map their native node
# types onto these. Consumers (renderers/search) reason about these.
KIND_NAMESPACE = "namespace"
KIND_CLASS = "class"
KIND_STRUCT = "struct"
KIND_INTERFACE = "interface"
KIND_RECORD = "record"
KIND_ENUM = "enum"
KIND_ENUM_MEMBER = "enum_member"

KIND_METHOD = "method"
KIND_FUNCTION = "function"
KIND_CTOR = "ctor"
KIND_DTOR = "dtor"
KIND_PROPERTY = "property"
KIND_INDEXER = "indexer"
KIND_FIELD = "field"
KIND_EVENT = "event"
KIND_DELEGATE = "delegate"
KIND_OPERATOR = "operator"

# A named structural block that is neither a type nor a callable: it
# groups child declarations the source nests inside a *callback
# argument* rather than a language-level body. Used by the TypeScript
# adapter for callback-DSL containers — test suites (`describe(...)`,
# `it(...)`), setup-stores (`defineStore('id', () => {...})`) and the
# like. In `outline` it renders its signature followed by indented
# children (same shape as a class); in `digest` it surfaces as a single
# `name [block]` token. It is deliberately NOT counted in the header
# `types` / `methods` / `fields` counters (`_collect_counts` skips it,
# like `KIND_NAMESPACE`) — it is structural grouping, not a code
# declaration the agent would `show`.
KIND_BLOCK = "block"

# Non-code (narrative) document kinds — currently used by the markdown adapter
KIND_HEADING = "heading"
KIND_CODE_BLOCK = "code_block"

# Data/config document kinds — used by the YAML adapter. One canonical
# kind covers every YAML construct (mapping key, sequence item, scalar
# leaf): the renderer differentiates by whether the node has children
# and what shape the signature has, not by sub-kinds. This keeps the
# IR uniform — same way `KIND_HEADING` covers every markdown heading
# regardless of level.
KIND_YAML_KEY = "yaml_key"
KIND_YAML_DOC = "yaml_doc"

# Stylesheet kinds — used by the CSS / SCSS adapters. Rules are the
# atomic block (`.foo { ... }`); at-rules cover named conditional
# wrappers and grouped blocks (`@media`, `@supports`, `@layer`,
# `@keyframes`, `@container`, `@font-face`, …). SCSS adds named
# top-level constructs: `@mixin` and `@function` are callable
# (`KIND_MIXIN` joins `CALLABLE_KINDS`), `$variable` is a value
# binding, `%placeholder` is an extend-only selector.
KIND_RULE = "rule"
KIND_AT_RULE = "at_rule"
KIND_MIXIN = "mixin"
KIND_VARIABLE = "variable"
KIND_PLACEHOLDER = "placeholder"

# SQL kinds. Only TABLE and VIEW need their own canonical kind — the
# rest of SQL maps onto existing kinds via ``native_kind``: schemas
# reuse ``KIND_NAMESPACE``, composite types reuse ``KIND_RECORD``,
# enums reuse ``KIND_ENUM``, functions/procedures/triggers all reuse
# ``KIND_FUNCTION`` (already in ``CALLABLE_KINDS`` so digest gets
# ``()`` automatically), indexes/sequences reuse ``KIND_FIELD``.
# Tables and views stay separate because they're the dominant SQL
# constructs and an agent reading a schema dump wants distinct
# counters and digest tags for them.
KIND_TABLE = "table"
KIND_VIEW = "view"

# HTML kind — used by the HTML adapter. One canonical kind covers every
# element (block, inline, void, self-closing). The renderer differentiates
# by the selector form in `signature` (`tag`, `tag#id`, `tag.cls`,
# `tag[attr=val]`, `[import] tag[…]`, heading-preview `h1: text`), not by
# sub-kinds — same uniform-IR pattern as `KIND_YAML_KEY` covering every
# YAML construct or `KIND_HEADING` covering every heading level.
KIND_HTML_ELEMENT = "html_element"

TYPE_KINDS = {KIND_CLASS, KIND_STRUCT, KIND_INTERFACE, KIND_RECORD, KIND_ENUM}
CALLABLE_KINDS = {KIND_METHOD, KIND_FUNCTION, KIND_CTOR, KIND_DTOR, KIND_OPERATOR, KIND_MIXIN}


@dataclass
class Declaration:
    kind: str               # canonical kind (see constants above)
    name: str               # identifier (not qualified)
    signature: str          # rendered signature line (no body)
    bases: list[str] = field(default_factory=list)      # for types
    attrs: list[str] = field(default_factory=list)      # decorators/attributes (inlined-ready)
    docs: list[str] = field(default_factory=list)       # doc-comment lines as-is (/// or """)
    docs_inside: bool = False  # True → emit docs AFTER signature with +1 indent (Python docstrings)
    visibility: str = ""    # "public"/"protected"/"private"/"internal"/"" (unknown)
    # Native source-language keyword for the declaration's kind, when it
    # diverges from the canonical `kind`. Empty string → digest falls
    # back to `kind`. Used by `render_digest` to print the source-true
    # keyword (e.g. Rust `trait` / Java `@interface` / Scala `trait`)
    # while the IR keeps a unified canonical kind for search.
    native_kind: str = ""
    start_line: int = 0     # 1-based, inclusive
    end_line: int = 0       # 1-based, inclusive
    start_byte: int = 0     # for `show` — source slice
    end_byte: int = 0
    doc_start_byte: int = 0 # if there's leading doc, slice starts here
    children: list["Declaration"] = field(default_factory=list)
    # Alternative names a `find_symbols` query can match this declaration
    # by, used when one declaration is reachable under several
    # identifiers — e.g. a CSS rule whose selector list is
    # `.btn-primary, .btn-secondary` is one block but matches either
    # name. Empty list means "match by `name` only" (the common case
    # for code adapters). When non-empty, the search walker uses the
    # matched entry as the qualified name in the resulting `SymbolMatch`.
    match_names: list[str] = field(default_factory=list)

    # Convenience: rendered line suffix for line-range display
    def lines_suffix(self) -> str:
        if not self.start_line:
            return ""
        if self.start_line == self.end_line:
            return f"  L{self.start_line}"
        return f"  L{self.start_line}-{self.end_line}"


@dataclass
class ParseResult:
    path: Path
    language: str           # "csharp" / "python" / etc.
    source: bytes
    line_count: int
    declarations: list[Declaration]  # top-level (namespaces or types)
    # Count of tree-sitter ERROR + MISSING nodes encountered during parse.
    # Non-zero means the IR for some region of the file may be incomplete;
    # renderers surface this as a warning line so the consuming agent
    # knows the outline isn't authoritative for that file.
    error_count: int = 0
    # Import / use / using statements as **source-true language-native
    # text**, one statement per entry. The adapter is responsible for
    # un-grouping multi-line imports (Go `import (\n "a"\n "b"\n)` →
    # ["import \"a\"", "import \"b\""]), expanding nested forms (Rust
    # `use a::{b, c}` → ["use a::b", "use a::c"]), and collapsing line
    # continuations into single statements. The strings read like the
    # language they came from — Python `from .core import X`,
    # TypeScript `import { X } from './core'`, Rust `use crate::utils::X`,
    # Go `import "fmt"`. Any LLM that knows the source language reads
    # the imports list without learning a synthetic format.
    #
    # Empty list when the language has no imports (markdown, yaml) or
    # the file has none. Renderers ignore this unless the caller opts in
    # via `OutlineOptions.show_imports` / `DigestOptions.show_imports`.
    imports: list[str] = field(default_factory=list)
    # Count of imports the adapter intentionally skipped because they're
    # not at the file's static top level — e.g. PHP `require_once` inside
    # an `if`/`try`/`switch`/loop body, or inside a function/method/
    # closure. We don't list them (it'd mislead the agent into thinking
    # they always load), but a counter lets the agent see "this file has
    # additional dynamic dependencies — read it directly if you care."
    # Renderers append `[+ N conditional includes]` to the imports line
    # when this is > 0. Currently populated only by the PHP adapter; for
    # most languages imports are top-level by spec (Java, Go, C#, …) and
    # the field stays at 0.
    conditional_imports_count: int = 0
    # Byte ranges of multi-line strings and block comments. Each entry
    # is ``(start, end, kind)`` where kind is ``"string"`` or
    # ``"comment"``. Used by ``ast_outline.grep`` to filter matches
    # that fall inside docstrings or block comments — the per-line
    # heuristics in grep.py can't see across line boundaries, so this
    # list is the authoritative source for multi-line noise.
    #
    # Populated by adapters opportunistically: only languages where
    # multi-line strings or block comments are common enough to cause
    # false positives in agent searches need to bother. Empty list
    # means "fall back to single-line heuristics" — backwards
    # compatible for adapters that haven't been updated.
    noise_regions: list[tuple[int, int, str]] = field(default_factory=list)

    # Byte ranges of import declarations as the language grammar sees
    # them — single-line OR block-form (Go ``import ( ... )``, Python
    # ``from X import (\n A,\n B,\n)``, TypeScript ``import { A, B }
    # from '...'`` spanning multiple lines, Rust ``use foo::{...}``,
    # PHP ``use App\{...}``).
    #
    # The line-prefix heuristic in ``grep._classify_match`` only sees
    # the OPENING line of a block ("import (") and misses the inner
    # package paths. Adapters that populate this list let the grep
    # classifier promote those inner matches to ``[import]`` directly,
    # mirroring how ``noise_regions`` provides authoritative string /
    # comment ranges.
    #
    # Populated opportunistically: languages with single-line imports
    # only (Java / Kotlin / Scala / C# / C++) don't need to bother —
    # the line-prefix path already classifies their imports correctly.
    # Empty list means "rely on line-only heuristics" — backwards-
    # compatible for adapters that haven't been updated.
    import_regions: list[tuple[int, int]] = field(default_factory=list)


# --- Options --------------------------------------------------------------


@dataclass
class OutlineOptions:
    include_private: bool = True
    include_fields: bool = True
    include_xml_doc: bool = True
    include_attributes: bool = True
    include_line_numbers: bool = True
    max_doc_lines: int = 6
    # When True, the renderer prints an `# imports: ...` annotation line
    # under the file header, listing every import normalized by the
    # adapter. Off by default — most outline calls don't need
    # dependency direction, and adding the line unconditionally would
    # bloat output for a signal that only some agent tasks consume.
    show_imports: bool = False


@dataclass
class DigestOptions:
    include_private: bool = False
    include_fields: bool = False
    max_members_per_type: int = 50
    # Markdown-only: cap heading depth in TOC digest (1=H1 only, 3=H1..H3, …).
    # Code blocks are shown only when include_fields is True.
    max_heading_depth: int = 3
    # See `OutlineOptions.show_imports`. When True, each file block in
    # the digest gets an `imports: ...` line indented under the file
    # header.
    show_imports: bool = False
    # Output format preset — controls rendering density.
    #   "names"   — one line per file, only top-level type / function names.
    #               Files with no public top-level symbols are hidden.
    #   "compact" — hierarchical (same shape as default) but with: no blank
    #               lines between types, no `L<a>-<b>` line ranges, no
    #               per-file `, X types, Y methods, Z fields` counters,
    #               and files with no declarations are hidden.
    #   "default" — current behavior (unchanged for backwards compatibility).
    #   "wide"    — same shape as default. The CLI raises `include_private`,
    #               `include_fields`, and `max_members_per_type` in the
    #               preset, so the renderer itself doesn't need to branch on
    #               "wide" — the existing default-format code already shows
    #               everything when those toggles are on.
    format: str = "default"


# --- Match types ----------------------------------------------------------


@dataclass
class SymbolMatch:
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    source: str
    # Signatures of the enclosing ancestor declarations, outer → inner
    # (e.g. ["namespace Foo.Bar", "public class Player : MonoBehaviour"] for
    # a method on `Player`). Rendered by `show` as a breadcrumb so the agent
    # knows what the extracted body is nested inside. Empty for top-level
    # symbols. Attributes/annotations are already stripped from each signature
    # (adapters store the bare signature in Declaration.signature).
    ancestor_signatures: list[str] = field(default_factory=list)
    # The matched Declaration itself — exposed so signature-view rendering
    # (`show --view signature`) can reuse the adapter-populated `docs`,
    # `attrs`, `signature`, and `docs_inside` fields without re-parsing the
    # source slice. Optional because legacy callers that constructed
    # `SymbolMatch` by hand (tests, downstream tools) shouldn't break; the
    # built-in search walker always populates it.
    decl: "Declaration | None" = None


# --- Declaration filtering ------------------------------------------------


def filter_declarations(
    decls: list[Declaration],
    *,
    include_private: bool,
    include_fields: bool,
    include_docs: bool = True,
    include_attrs: bool = True,
    field_kinds: frozenset[str] = frozenset({KIND_FIELD}),
) -> list[Declaration]:
    """Return a pruned deep copy of a declaration tree.

    A pure IR→IR transform. The JSON serializer routes through it so its
    content filtering matches what the text renderers apply inline:

    - ``include_fields`` False → drop declarations whose kind is in
      ``field_kinds`` (value bindings — fields / variables / code blocks).
    - ``include_private`` False → drop ``private``-visibility declarations.
    - ``include_docs`` / ``include_attrs`` False → clear the ``docs`` /
      ``attrs`` list on every surviving declaration.

    Filtering recurses through ``children``. The input is never mutated —
    each surviving node is copied via ``dataclasses.replace``.
    """
    out: list[Declaration] = []
    for d in decls:
        if not include_fields and d.kind in field_kinds:
            continue
        if not include_private and d.visibility == "private":
            continue
        out.append(replace(
            d,
            docs=list(d.docs) if include_docs else [],
            attrs=list(d.attrs) if include_attrs else [],
            children=filter_declarations(
                d.children,
                include_private=include_private,
                include_fields=include_fields,
                include_docs=include_docs,
                include_attrs=include_attrs,
                field_kinds=field_kinds,
            ),
        ))
    return out


# --- Renderers ------------------------------------------------------------


def render_outline(result: ParseResult, opts: OutlineOptions) -> str:
    # Same `[tiny]` / `[medium]` / `[large]` label digest stamps next to
    # each filename — emitted here too so an agent calling `outline`
    # directly (skipping digest) gets the categorical size signal in
    # plain English alongside the precise `~N tokens` count. The
    # English label triggers more reliably than parsing the raw token
    # number when the agent reads cold without having seen the digest.
    label = _size_label(_estimate_tokens(result.source))
    lines: list[str] = [_format_file_header(f"# {result.path} {label}", result)]
    warn = _format_error_warning(result)
    if warn:
        lines.append(warn)
    if opts.show_imports and (
        result.imports or result.conditional_imports_count > 0
    ):
        lines.append(
            "# " + _format_imports_line(
                result.imports, result.conditional_imports_count
            )
        )
    for decl in result.declarations:
        _render_decl(decl, opts, indent=0, out=lines)
    return "\n".join(lines)


def _format_imports_line(imports: list[str], conditional_count: int = 0) -> str:
    """Render the imports annotation as `imports: <stmt>; <stmt>; ...`,
    optionally followed by `[+ N conditional includes]` when the
    adapter skipped some dynamic ones.

    Callers must ensure at least one of (`imports` non-empty,
    `conditional_count > 0`) is true — calling with both empty
    produces a degenerate `"imports:"` line. The two renderers
    (`render_outline`, `render_digest`) both gate the call on this
    condition, so the degenerate output is unreachable in normal use.

    Each entry is already a complete, source-true import statement in
    the file's native language (`from .core import X`,
    `use foo::Bar as Q`, `import { Foo } from './core'`,
    `import "fmt"`). We join with `; ` — semicolon is a statement
    separator in nearly every supported language, so the joined line
    reads like a sequence of sibling import statements that just
    happen to be on one line. No synthetic format for the LLM to
    learn; any agent that recognises the source language recognises
    the output.

    The `[+ N conditional includes]` suffix is a deliberate signal
    that more imports exist in the file but are not statically
    enumerable — they live inside `if`/`try`/loop branches or
    function bodies. We keep the marker bracketed and short so an
    LLM doesn't conflate it with a real import. When the static
    list is empty but `conditional_count > 0` (e.g. WordPress
    `wp-load.php` whose every `require` lives in an `if`/`else`
    chain), the line reads `imports: [+ 6 conditional includes]` —
    still signalling "this file has dependencies" rather than
    looking dependency-less.

    Caveat: a TypeScript / JS module specifier could theoretically
    contain a literal `; ` inside a string (`import x from "./a;b"`).
    That would make the joined line ambiguous to a strict
    string-splitter. Real-world impact: zero — module paths almost
    never carry a semicolon, and this output is consumed by LLMs that
    parse statements semantically, not by `split("; ")`. We accept the
    edge case rather than escape or switch separators.
    """
    suffix = ""
    if conditional_count > 0:
        plural = "s" if conditional_count != 1 else ""
        suffix = f" [+ {conditional_count} conditional include{plural}]"
    if not imports:
        # No static imports but the adapter saw conditional ones —
        # emit the bracketed counter on its own so the line still
        # signals "this file has dependencies".
        return "imports:" + suffix
    return "imports: " + "; ".join(imports) + suffix


# --- Header helpers (shared between outline + digest) --------------------


def _format_file_header(
    prefix: str, result: ParseResult, include_counters: bool = True
) -> str:
    """Build the `# path (N lines, ~N tokens, ...)` header for one file.

    `prefix` is the leading marker + path (e.g. `"# /abs/path.py"` for
    outline or `"  name.py"` for digest). Appended in parentheses:
    line count, an approximate token count (so the agent can size up
    the file before deciding between Read / outline / show), and any
    non-zero category counters appropriate for the language family —
    types/methods/fields for code, headings/code blocks for markdown.
    Zero-valued categories are skipped so a trivial file reads
    `(42 lines, ~310 tokens)` not `(42 lines, ~310 tokens, 0 types, 0 methods)`.

    `include_counters=False` drops the category counters entirely
    (`types`, `methods`, `fields`, `headings`, `code blocks`, `docs`),
    keeping only `N lines, ~N tokens`. Used by the `compact` digest
    format where the breakdown adds visual weight without changing the
    sizing decision (the line+token totals are what drive routing).

    Token estimate is ``len(source_bytes) // 4`` — a coarse BPE-style
    approximation, deliberately not exact (no tiktoken dep). The ``~``
    prefix and the rule-of-thumb in the digest legend make it explicit
    this is an order-of-magnitude figure.
    """
    counts = _collect_counts(result.declarations)
    parts = [
        f"{result.line_count} lines",
        f"~{_estimate_tokens(result.source):,} tokens",
    ]
    if not include_counters:
        order: list[tuple[str, str]] = []
    elif result.language == "markdown":
        order = [("headings", "headings"), ("code_blocks", "code blocks")]
    elif result.language == "yaml":
        # YAML files report their document count when multi-doc — for
        # k8s manifests this is the primary "what's in here" signal.
        # Single-doc files skip the counter (`1 doc` would be noise).
        n_docs = sum(1 for d in result.declarations if d.kind == KIND_YAML_DOC)
        if n_docs > 1:
            parts.append(f"{n_docs} docs")
        order = []
    elif result.language == "html":
        order = [("elements", "elements")]
    else:
        order = [("types", "types"), ("methods", "methods"), ("fields", "fields")]
    for key, label in order:
        n = counts.get(key, 0)
        if n > 0:
            parts.append(f"{n} {label}")

    # Format-detect annotation — appended after the closing paren with
    # an em-dash so the eye visually separates the "what is this file"
    # signal from the size/counter parens. Only fires for single-doc
    # YAML where a clear format is detectable; multi-doc shows per-doc
    # annotations in each `--- doc N of M` separator line instead.
    suffix = ""
    if result.language == "yaml":
        suffix = _yaml_format_suffix(result.declarations)
    return f"{prefix} ({', '.join(parts)}){suffix}"


def _yaml_format_suffix(decls: list[Declaration]) -> str:
    """Generate the `— OpenAPI 3.0.0, 23 paths` style annotation. Empty
    string when no specific format is detected, or when the file is
    multi-document (per-doc annotations live in the separator lines)."""
    n_docs = sum(1 for d in decls if d.kind == KIND_YAML_DOC)
    if n_docs > 1:
        return ""
    # Single-doc — detect on the top-level decls directly
    from .adapters.yaml import _format_for_doc
    hint = _format_for_doc(decls)
    if hint:
        return f" — {hint}"
    return ""


def _estimate_tokens(source: bytes) -> int:
    """Approximate the BPE token count of `source`.

    Counts **characters**, not bytes — for Cyrillic / CJK content one
    byte ≠ one char in UTF-8, and a byte-based estimate would inflate
    the count by 30-50% for those files. ``chars // 4`` matches Claude
    and GPT BPE tokenizers within ±15-20% on real code/YAML/markdown,
    which is more than enough for the size-hint heuristic.
    """
    return len(source.decode("utf-8", errors="replace")) // 4


# --- Size label ----------------------------------------------------------
#
# Categorical descriptors of file size, displayed next to each filename
# in `digest`. Deliberately **descriptive**, not prescriptive — the
# label tells the agent how big the file is, the agent picks Read /
# outline / show based on the task at hand. A directive-style label
# (`[Read]` / `[outline]`) would override the agent's judgment in cases
# where the file IS small but the agent still wants the structural
# overview, or vice versa. Information beats instruction.
#
# The thresholds are the only "magic numbers" in the project, calibrated
# against typical code/config sizes where outline-vs-Read trade-offs
# meaningfully shift.

_SIZE_LABEL_MEDIUM_FLOOR = 500       # below this — outline shrinks little vs full read
_SIZE_LABEL_LARGE_FLOOR = 5000       # above this — outline alone may be long; show helps
_SIZE_LABEL_HUGE_FLOOR = 100_000     # above this — even the outline becomes unwieldy;
                                     # digest collapses the body (header-only) so a
                                     # directory full of generated SDKs / vendored mega-
                                     # files doesn't bloat the digest.


def _size_label(token_count: int) -> str:
    """One of four descriptive size labels.

    - ``[tiny]`` — under ~500 tokens. Outline returns roughly the same
      content as Read, with light structural overlay.
    - ``[medium]`` — 500-5000 tokens. Outline meaningfully compresses
      (2-10× typical) while staying compact enough to consume whole.
    - ``[large]`` — 5000-100k tokens. Outline output itself can run long;
      ``show`` for specific sections is the surgical follow-up.
    - ``[huge]`` — 100k+ tokens. The file's outline alone would crowd the
      digest; ``digest`` collapses it to the header line only (counters
      still shown). ``outline`` and ``show`` work normally — the label is
      informational there, behavior changes only in ``digest``.

    The label is descriptive: in ``outline`` and ``show`` it just signals
    size. In ``digest`` ``[huge]`` is also a behavioral marker — the
    body of that one file is omitted; agent uses ``ast-outline outline
    <path>`` to expand on demand.
    """
    if token_count < _SIZE_LABEL_MEDIUM_FLOOR:
        return "[tiny]"
    if token_count < _SIZE_LABEL_LARGE_FLOOR:
        return "[medium]"
    if token_count < _SIZE_LABEL_HUGE_FLOOR:
        return "[large]"
    return "[huge]"


def _format_error_warning(result: ParseResult) -> Optional[str]:
    """Second header line warning about parse errors — only when
    `error_count > 0`. Agents should treat the file's outline as partial.
    """
    if result.error_count <= 0:
        return None
    plural = "s" if result.error_count != 1 else ""
    return (
        f"# WARNING: {result.error_count} parse error{plural} — "
        f"output may be incomplete"
    )


# Tables and views count as "types" for digest header / member
# extraction / counter purposes, but stay OUT of ``TYPE_KINDS`` itself
# — that set drives qualified-name prefixing for class-like containers
# (``namespace.Class.member``), and SQL agents query column names
# unqualified (``email``, not ``public.users.email``). The
# ``_DIGEST_TYPE_KINDS`` superset is used wherever digest needs a
# "type-like container with members" predicate.
_DIGEST_TYPE_KINDS = TYPE_KINDS | {KIND_TABLE, KIND_VIEW}
_TYPE_COUNT_KINDS = _DIGEST_TYPE_KINDS
_METHOD_COUNT_KINDS = CALLABLE_KINDS
_FIELD_COUNT_KINDS = {KIND_FIELD, KIND_PROPERTY, KIND_EVENT, KIND_INDEXER}


def _collect_counts(decls: list[Declaration]) -> dict[str, int]:
    """Walk the declaration tree once, counting by category. Namespaces
    are transparent containers — not counted, but their children are.
    Enum members aren't counted (they're not "fields" semantically).
    """
    out = {
        "types": 0,
        "methods": 0,
        "fields": 0,
        "headings": 0,
        "code_blocks": 0,
        "elements": 0,
    }
    stack: list[Declaration] = list(decls)
    while stack:
        d = stack.pop()
        k = d.kind
        if k in _TYPE_COUNT_KINDS:
            out["types"] += 1
        elif k in _METHOD_COUNT_KINDS:
            out["methods"] += 1
        elif k in _FIELD_COUNT_KINDS:
            out["fields"] += 1
        elif k == KIND_HEADING:
            out["headings"] += 1
        elif k == KIND_CODE_BLOCK:
            out["code_blocks"] += 1
        elif k == KIND_HTML_ELEMENT:
            out["elements"] += 1
        # namespace / enum_member / delegate → not counted at top level
        stack.extend(d.children)
    return out


def _render_decl(decl: Declaration, opts: OutlineOptions, indent: int, out: list[str]) -> None:
    if decl.kind == KIND_FIELD and not opts.include_fields:
        return
    if decl.visibility == "private" and not opts.include_private:
        return

    prefix = "    " * indent
    suffix = decl.lines_suffix() if opts.include_line_numbers else ""

    # YAML multi-document separator. The doc itself is a logical group
    # but visually it's a flat horizontal slice — its children render
    # at the SAME indent level as the separator line, not indented one
    # level deeper. This keeps the YAML body looking like the actual
    # YAML it represents, instead of double-indenting everything inside
    # a multi-doc file.
    if decl.kind == KIND_YAML_DOC:
        out.append(prefix + decl.signature + suffix)
        for child in decl.children:
            _render_decl(child, opts, indent, out)
        out.append("")  # blank line between docs for visual separation
        return

    # Docs BEFORE signature (C# /// XML-doc style)
    if opts.include_xml_doc and decl.docs and not decl.docs_inside:
        for d in _clip_docs(decl.docs, opts.max_doc_lines):
            out.append(prefix + d)

    # Attributes inlined
    attrs_prefix = ""
    if opts.include_attributes and decl.attrs:
        attrs_prefix = " ".join(decl.attrs) + " "

    # Namespace gets special rendering
    if decl.kind == KIND_NAMESPACE:
        out.append(prefix + f"namespace {decl.name}")
    else:
        out.append(prefix + attrs_prefix + decl.signature + suffix)

    # Docs AFTER signature (Python docstring style — they're inside the body)
    if opts.include_xml_doc and decl.docs and decl.docs_inside:
        inner_prefix = "    " * (indent + 1)
        for d in _clip_docs(decl.docs, opts.max_doc_lines):
            out.append(inner_prefix + d)

    # Recurse into children
    for child in decl.children:
        _render_decl(child, opts, indent + 1, out)

    # Blank line after top-level types and namespaces for readability.
    # YAML keys are intentionally excluded — top-level YAML keys
    # (`apiVersion`, `kind`, `metadata`, `spec`) sit on adjacent lines
    # in the source file, and inserting blanks between them would make
    # the outline look unlike the YAML it represents.
    if decl.kind == KIND_YAML_KEY:
        return
    if indent == 0 or decl.kind in TYPE_KINDS or decl.kind == KIND_NAMESPACE:
        out.append("")


def _clip_docs(docs: list[str], limit: int) -> list[str]:
    if len(docs) <= limit:
        return docs
    return docs[:limit] + ["..."]


def render_signature_view(match: SymbolMatch, *, max_doc_lines: int = 6) -> str:
    """Render JUST the header of a matched symbol — docs + attrs + signature,
    without the body. Used by `show --view signature`.

    The output is intentionally a strict subset of what `outline` would emit
    for the same declaration: same doc placement (before sig for C#/JSDoc/Rust
    style, after sig with +1 indent for Python docstring style), same
    attribute inlining, same signature line. The only differences vs outline
    are (1) no line-range suffix on the signature itself — the caller (`show`)
    already prints `# path:start-end` as a header line, so duplicating the
    range here would be noise — and (2) no recursion into children, since the
    consumer asked for THIS symbol's contract, not the file map.

    Returns an empty string if `match.decl` is None (legacy `SymbolMatch`
    constructed without the back-reference). The caller should fall back to
    `match.source` in that case so the output is never silently empty.
    """
    decl = match.decl
    if decl is None:
        return ""
    lines: list[str] = []
    # Docs BEFORE signature (C# /// style, JSDoc, Rust ///, etc.)
    if decl.docs and not decl.docs_inside:
        for d in _clip_docs(decl.docs, max_doc_lines):
            lines.append(d)
    # Attributes inlined onto the signature line, matching outline's render.
    attrs_prefix = ""
    if decl.attrs:
        attrs_prefix = " ".join(decl.attrs) + " "
    # Namespace gets the same special-case rendering as outline.
    if decl.kind == KIND_NAMESPACE:
        lines.append(f"namespace {decl.name}")
    else:
        lines.append(attrs_prefix + decl.signature)
    # Docs AFTER signature (Python docstring style — they live INSIDE the body
    # in the source, so render them at +1 indent the way outline does).
    if decl.docs and decl.docs_inside:
        for d in _clip_docs(decl.docs, max_doc_lines):
            lines.append("    " + d)
    return "\n".join(lines)


def strip_leading_doc(src: str) -> str:
    """Strip the doc block from a `show` source slice.

    Two shapes we handle:
    - C# style: one or more ``///`` lines at the top of the slice.
    - Python style: a ``def`` / ``class`` / decorator header followed by a
      triple-quoted docstring as the first body statement.
    """
    lines = src.splitlines()

    # C#: strip any leading /// comment lines.
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("///"):
        i += 1
    if i > 0:
        return "\n".join(lines[i:])

    # Python: skip decorators + def/class header, then drop the docstring if
    # it's the first body statement.
    j = 0
    while j < len(lines) and lines[j].lstrip().startswith("@"):
        j += 1
    if j < len(lines):
        header = lines[j].lstrip()
        if header.startswith(("def ", "async def ", "class ")):
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines):
                doc_line = lines[k].lstrip()
                for delim in ('"""', "'''"):
                    if doc_line.startswith(delim):
                        rest = doc_line[3:]
                        if delim in rest:
                            # Single-line docstring
                            return "\n".join(lines[:k] + lines[k + 1 :])
                        # Multi-line: find closing delim
                        end = k + 1
                        while end < len(lines) and delim not in lines[end]:
                            end += 1
                        return "\n".join(lines[:k] + lines[end + 1 :])
    return src


# --- Digest ---------------------------------------------------------------


# Dynamic legend — built per-render from a `_LegendFlags` set during the
# digest pass. Only tokens that actually appear in the rendered body get
# documented, so a YAML- or markdown-only digest emits no legend at all
# (their output uses no `name()`, no `[kind]`, no markers, no inheritance —
# nothing the legend would explain). A mixed batch shrinks the legend to
# just the tokens its languages contributed. The full legend below is
# what gets emitted when every flag fires (a code batch with overloads,
# deprecated members, and inheritance).
#
# Why dynamic: the legend is overhead on every digest call; documenting
# tokens the body doesn't contain is noise for the LLM consuming the
# output. We pay only for what's there.
#
# Why size labels (`[tiny]`/`[medium]`/`[large]`) and `[broken]` are
# never in the legend: they're plain English, instantly recognisable
# without a lookup. Keeping them out keeps the legend short for the
# common case. `[huge]` is the exception — unlike the other size labels
# it ALSO changes digest behavior (body suppressed), so it's documented
# in the legend whenever a huge file appears in the batch.

# All possible legend entries, in canonical display order. The order
# mirrors the order tokens visually appear in a typical digest line:
# callable kind first, then non-callable kinds, modifiers, overload tag,
# deprecation tag, line ranges, inheritance, then size-driven omission
# markers at the very end.
_LEGEND_ENTRIES: tuple[tuple[str, str], ...] = (
    ("callable", "name()=callable"),
    ("kind", "name [kind]=non-callable"),
    ("marker", "marker name()=method modifier (async/static/override/…)"),
    ("overloads", "[N overloads]=N callables share name"),
    ("deprecated", "[deprecated]=obsolete"),
    ("line_range", "L<a>-<b>=line range"),
    ("inheritance", ": Base, …=inheritance"),
    ("huge", "[huge]=body omitted (use `ast-outline outline <path>`)"),
)


@dataclass
class _LegendFlags:
    """Records which legend-worthy tokens actually appeared in the
    rendered digest body. Populated during the render pass and consumed
    once at the end to assemble the legend line.

    Each flag corresponds to one entry in `_LEGEND_ENTRIES`. The entry
    is included in the final legend iff its flag is True. Flags are
    sticky (set-only, never cleared) — once a token surfaces anywhere
    in the batch, it stays in the legend.
    """
    callable: bool = False        # any `name()` callable token
    kind: bool = False            # any `name [kind]` non-callable token
    marker: bool = False          # any method modifier prefix (async/static/…)
    overloads: bool = False       # any `[N overloads]` annotation
    deprecated: bool = False      # any `[deprecated]` tag
    line_range: bool = False      # any `L<n>` / `L<a>-<b>` suffix
    inheritance: bool = False     # any `: Base[, Base…]` clause
    huge: bool = False            # any file rendered as header-only `[huge]` collapse


def _build_legend(flags: _LegendFlags) -> str:
    """Assemble the `# legend: ...` line from the recorded flags.

    Returns the empty string in two cases — both producing a digest
    with no legend at all:
    - No flags set (a degenerate empty batch).
    - Only `line_range` is set. Line ranges are intrinsically obvious
      from the trailing `L<n>` / `L<a>-<b>` form alongside line counts
      in the file header — emitting a one-entry legend just to document
      them would be more overhead than insight. This is the case for
      YAML-only and markdown-only digests, where line ranges are the
      sole "non-English" token.
    """
    entries = [text for key, text in _LEGEND_ENTRIES if getattr(flags, key)]
    if not entries:
        return ""
    # Derive the line-range entry text from `_LEGEND_ENTRIES` instead of
    # repeating the literal here — keeps the omission rule in lockstep
    # with however the entry text is worded.
    line_range_text = next(text for key, text in _LEGEND_ENTRIES if key == "line_range")
    if entries == [line_range_text]:
        return ""
    return "# legend: " + ", ".join(entries)


# Type-modifier whitelist — keywords that meaningfully change how a
# type is USED (instantiation, subclassing, scope), not visibility. We
# already filter by visibility separately, so `public` / `private` /
# `pub` / `pub(crate)` are intentionally NOT in this list — adding them
# would duplicate a signal and inflate every header line.
_TYPE_MODIFIERS = frozenset({
    "abstract", "sealed", "final", "static", "open",
    "partial", "inner", "inline", "value",
})


# Substring-match patterns for deprecation. Case-insensitive: covers
# `@Deprecated`, `@deprecated`, `[Obsolete]`, `[Obsolete("...")]`,
# `#[deprecated]`, `#[deprecated(since="...")]`, etc. across every
# language's annotation / attribute syntax.
_DEPRECATED_PATTERNS = ("deprecated", "obsolete")


def _is_deprecated(d: Declaration) -> bool:
    if not d.attrs:
        return False
    return any(
        p in a.lower()
        for a in d.attrs
        for p in _DEPRECATED_PATTERNS
    )


def _filter_non_deprecation_attrs(attrs: list[str]) -> list[str]:
    """Drop deprecation attrs from the list rendered before a type
    header. Their content is replaced by the compact `[deprecated]`
    tag so the same signal isn't shown twice."""
    return [
        a for a in attrs
        if not any(p in a.lower() for p in _DEPRECATED_PATTERNS)
    ]


# Method-marker whitelist — keywords that meaningfully change how a
# member is CALLED. Each appears in the source signature before the
# method name (or function keyword) across the supported languages:
# - `async` — C# / TS / Python / JS / Rust (universal coroutine marker)
# - `suspend` — Kotlin's coroutine equivalent of async
# - `static` — C# / Java / TS / Kotlin (companion-object members)
# - `abstract` — C# / Java / Kotlin / Scala
# - `virtual` — C# (declares overridable)
# - `override` — C# / Kotlin / Scala
# - `open` — Kotlin's "may be overridden" (the dual of `final`)
# - `sealed` — C# 8 sealed override / Kotlin sealed
# - `unsafe` — Rust (callee may violate memory safety; high signal)
# - `const` — Rust `const fn` (compile-time evaluable callable; not
#   the same word as the C# `const` field modifier — that's only
#   valid on fields, never enters this code path)
_METHOD_MARKERS = frozenset({
    "async", "suspend",
    "static", "abstract", "virtual", "override",
    "open", "sealed",
    "unsafe", "const",
})


# Decorator / annotation names that act as method markers. We render
# these source-true (with `@` / `[` / `#[]` decoration intact) rather
# than translating to a canonical English keyword — preserves the
# language-native form, stays grep-friendly against the source, and
# keeps language-specific distinctions visible (e.g. Python
# `@staticmethod` vs `@classmethod` are semantically different and
# both surface in the digest).
_MARKER_DECORATORS = frozenset({
    # Python — call-style decorators
    "staticmethod", "classmethod",
    "abstractmethod", "abstractclassmethod", "abstractstaticmethod",
    # Java — annotations
    "Override",
})


def _bare_attr_name(attr: str) -> str:
    """Reduce an attr string to the bare identifier so we can match
    against `_MARKER_DECORATORS` regardless of decoration form.

    Accepts every common attr shape: `@deco`, `@deco(...)`, `@a.b.deco`,
    `[Attr]`, `[Attr(...)]`, `#[deco]`, `#[deco(...)]`.
    """
    bare = attr.strip().lstrip("@")
    if bare.startswith("#["):
        bare = bare[2:].rstrip("]")
    elif bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    paren = bare.find("(")
    if paren > 0:
        bare = bare[:paren]
    if "." in bare:
        bare = bare.rsplit(".", 1)[-1]
    return bare.strip()


def _decorator_marker_attr(attr: str) -> Optional[str]:
    """Return the source-true attr text if `attr` is a marker
    decorator / annotation, else None. The original decoration
    (`@`, `[]`, `#[]`) is preserved so the digest reads the same as
    the source — Python `@staticmethod`, Java `@Override`."""
    if _bare_attr_name(attr) in _MARKER_DECORATORS:
        return attr.strip()
    return None


# Classification helpers for skip rules — mapping from a marker render
# string back to its semantic category. Lets us suppress redundant
# `static`/`@staticmethod` on members of a static class and
# `abstract`/`@abstractmethod` on members of an interface, regardless
# of which surface form the source used.
_STATIC_FORMS = ("static", "staticmethod")
_ABSTRACT_FORMS = ("abstract", "abstractmethod")


def _is_static_marker(rendered: str) -> bool:
    return _bare_attr_name(rendered).lower() in _STATIC_FORMS


def _is_abstract_marker(rendered: str) -> bool:
    bare = _bare_attr_name(rendered).lower()
    return bare in _ABSTRACT_FORMS or "abstractmethod" in bare


def _method_markers(
    decl: Declaration, parent: Optional[Declaration]
) -> list[str]:
    """Extract canonical method markers for a callable declaration.

    Two sources are merged:
    - Signature tokens before the method name (everything before the
      `(` arg list, last token dropped as the name itself). Whitelist
      filtered against `_METHOD_MARKERS`.
    - Decorator / annotation attrs translated via `_ATTR_TO_MARKER`.

    Skip rules avoid redundant noise:
    - `static` is dropped when the parent type is itself `static`
      (every member of a static class is implicitly static).
    - `abstract` is dropped when the parent is an interface
      (every interface member is implicitly abstract).

    Returns markers in insertion order so the rendering reads close
    to source-natural — `async override LoadAsync()` rather than
    alphabetically reordered.
    """
    markers: list[str] = []
    sig = decl.signature or ""
    paren = sig.find("(")
    if paren > 0:
        tokens = sig[:paren].split()
        # Find where the method name lives in the token sequence.
        # Direct match first; on miss (e.g. C# `Foo<T>` token vs name
        # `Foo`) fall back to the last token. This matters for JS-
        # shaped signatures like `export const add = (a, b) => …`
        # where the last token before `(` is `=`, not the name —
        # using `decl.name` as the anchor stops modifier scanning at
        # `add` and avoids leaking markers from past the name.
        try:
            name_idx = tokens.index(decl.name)
        except ValueError:
            name_idx = max(0, len(tokens) - 1)
        # `const` is meaningful only when applied to a function
        # definition (Rust `const fn ...`). In JS/TS the same word
        # introduces a const-bound variable (`const x = …`), which
        # in the arrow-function case otherwise looks like a callable
        # signature. Require the `fn` keyword to disambiguate.
        has_fn_keyword = "fn" in tokens
        for tok in tokens[:name_idx]:
            if tok not in _METHOD_MARKERS:
                continue
            if tok == "const" and not has_fn_keyword:
                continue
            if tok in markers:
                continue
            markers.append(tok)
    for a in decl.attrs:
        m = _decorator_marker_attr(a)
        if m and m not in markers:
            markers.append(m)
    skip_static = (
        parent is not None and "static" in _type_modifiers(parent)
    )
    skip_abstract = (
        parent is not None and parent.kind == KIND_INTERFACE
    )
    return [
        m for m in markers
        if not (skip_static and _is_static_marker(m))
        and not (skip_abstract and _is_abstract_marker(m))
    ]


def _type_modifiers(d: Declaration) -> list[str]:
    """Extract whitelisted modifier keywords from a type's signature.

    Walks the tokens BEFORE the kind keyword (`class`, `struct`,
    `trait`, etc.) and keeps only those in `_TYPE_MODIFIERS`. Anything
    else (visibility, language-specific noise) is dropped.

    Falls back to the canonical kind if the source-true keyword
    (`native_kind`) isn't present in the signature — covers cases
    like Scala `case class` where the canonical kind (RECORD) and the
    keyword in the signature don't match. Returns the empty list when
    no recognisable kind anchor is found.
    """
    sig = d.signature or ""
    if not sig:
        return []
    for keyword in (d.native_kind, d.kind):
        if not keyword:
            continue
        idx = sig.find(keyword + " ")
        if idx < 0:
            # Try without trailing space — keyword might be at end of sig.
            if sig.endswith(keyword):
                idx = len(sig) - len(keyword)
            else:
                continue
        prefix_tokens = sig[:idx].split()
        return [t for t in prefix_tokens if t in _TYPE_MODIFIERS]
    return []


def render_digest(results: list[ParseResult], opts: DigestOptions, root: Optional[Path] = None) -> str:
    """Compact per-directory public-API map across a batch of parsed files.

    A self-describing legend line is prepended only when the rendered
    body actually uses non-English tokens (callable parens, kind tags,
    method markers, overload counts, deprecation tags, inheritance).
    Pure YAML or markdown batches — whose digest contains only plain
    keys / heading text and line-range suffixes — get no legend, since
    there is nothing in the body that needs explaining. See
    `_build_legend` for the exact omission rule.

    `opts.format` selects the rendering preset:
    - ``"names"`` — one line per file, top-level symbols only. Routed
      through `_render_digest_names`; bypasses the legend / per-type
      hierarchy entirely.
    - ``"compact"`` — same hierarchy as default but with file-header
      counters, line-range suffixes, blank lines between types, and
      no-declarations files all dropped. Empty directories collapse.
    - ``"default"`` — current behavior (unchanged).
    - ``"wide"`` — same as default; the CLI's preset just cranks the
      include-private / include-fields / max-members knobs, so the
      renderer itself doesn't need to branch.
    """
    if not results:
        return "# no files\n"
    # Common root for relative paths
    if root is None:
        try:
            import os
            root = Path(os.path.commonpath([str(r.path) for r in results]))
        except ValueError:
            root = results[0].path.parent

    if opts.format == "names":
        return _render_digest_names(results, opts, root)

    grouped: dict[Path, list[ParseResult]] = {}
    for r in results:
        grouped.setdefault(r.path.parent, []).append(r)

    # Render the body first, populating `flags` as we go, then build
    # the legend from only the tokens we actually emitted.
    flags = _LegendFlags()
    body: list[str] = []
    for directory in sorted(grouped.keys(), key=str):
        try:
            rel = str(directory.relative_to(root))
        except ValueError:
            rel = str(directory)
        # Render each file first; in `compact` mode files with no
        # declarations return an empty block, so a directory composed
        # entirely of such files should not surface a header.
        file_blocks: list[list[str]] = []
        for r in grouped[directory]:
            block = _digest_one(r, opts, flags)
            if block:
                file_blocks.append(block)
        if not file_blocks:
            continue
        body.append(f"{rel}/")
        for block in file_blocks:
            body.extend(block)
        body.append("")
    # `compact` hides declaration-less files; if every input file was
    # hidden, emit an explicit note so the caller doesn't confuse
    # "all-empty" with "no files found" (the latter is the
    # `results == []` case handled at the top of `render_digest`).
    if not body and opts.format == "compact":
        return "# note: all files hidden (no declarations under current filters)\n"
    legend = _build_legend(flags)
    lines = ([legend] if legend else []) + body
    return "\n".join(lines).rstrip() + "\n"


def _render_digest_names(
    results: list[ParseResult],
    opts: DigestOptions,
    root: Path,
) -> str:
    """One line per file: ``  name.py [label]: SymA, SymB, SymC``.

    Format design:
    - Only top-level types and free functions surface — methods,
      fields, line ranges, inheritance, decorators are all dropped.
      The next agent step after `names` is typically `outline <file>`
      or `grep <symbol>`, neither of which benefits from members
      being pre-listed in the digest.
    - Files with no headline symbols (after applying include_private /
      include_fields filters) are hidden entirely; the agent doesn't
      need to know about declaration-empty `__init__.py` shims when
      picking what to drill into.
    - ``[huge]`` files emit just ``  name.py [huge]`` (no colon, no
      symbol list) since the digest never collected their bodies. The
      size label alone tells the agent to skip the in-line read.
    - Markdown surfaces top-level (H1) headings; YAML surfaces top-
      level keys; CSS/SCSS surface their flat selector list. Same
      shape as code, different "symbol" semantics per language.
    - No legend — none of the legend tokens (`()`, `[kind]`,
      `[N overloads]`, `: Base`, `L<a>-<b>`) appear in this format.
    """
    grouped: dict[Path, list[ParseResult]] = {}
    for r in results:
        grouped.setdefault(r.path.parent, []).append(r)

    body: list[str] = []
    for directory in sorted(grouped.keys(), key=str):
        try:
            rel = str(directory.relative_to(root))
        except ValueError:
            rel = str(directory)
        dir_lines: list[str] = []
        for r in grouped[directory]:
            file_block = _digest_one_names(r, opts)
            if file_block:
                dir_lines.extend(file_block)
        if not dir_lines:
            continue
        body.append(f"{rel}/")
        body.extend(dir_lines)
        body.append("")
    # All input files filtered out (no top-level symbols under the
    # current visibility/private rules). Distinguish from the truly
    # no-input case (handled at the top of `render_digest`).
    if not body:
        return "# note: all files hidden (no top-level symbols under current filters)\n"
    return "\n".join(body).rstrip() + "\n"


def _digest_one_names(result: ParseResult, opts: DigestOptions) -> list[str]:
    """Render one file in names format. Returns a 1- or 2-line block
    (the file's symbol line plus an optional indented imports line when
    ``opts.show_imports`` is set), or an empty list to skip the file
    entirely.

    The `--imports` flag adds a second indented line so the invariant
    "if you ask for imports you see them in every format" holds. This
    softens the "one line per file" promise of names format but only
    when the caller explicitly opts in by passing `--imports`.
    """
    label = _size_label(_estimate_tokens(result.source))
    integrity = " [broken]" if result.error_count > 0 else ""
    prefix = f"  {result.path.name} {label}{integrity}"

    def with_imports(file_line: str) -> list[str]:
        out = [file_line]
        if opts.show_imports and (
            result.imports or result.conditional_imports_count > 0
        ):
            out.append(
                "    " + _format_imports_line(
                    result.imports, result.conditional_imports_count
                )
            )
        return out

    # [huge] files have no collected body in digest — emit just the
    # header so the agent still sees the file exists, but with no
    # trailing colon / symbol list (there are none to list).
    if label == "[huge]":
        return with_imports(prefix)

    if result.language == "markdown":
        # Top-level headings only (children are nested H2+ — not
        # listed here to keep the line tight).
        headlines = [
            d.signature.lstrip("# ").strip() or d.name
            for d in result.declarations
            if d.kind == KIND_HEADING
        ]
    elif result.language == "yaml":
        # Single-doc YAML: top-level keys. Multi-doc: doc separator
        # captions (e.g. `--- doc 1 of 3 — ConfigMap prod/api-config`)
        # — the signature carries the descriptive label, name alone is
        # just an index.
        if any(d.kind == KIND_YAML_DOC for d in result.declarations):
            headlines = [
                d.signature for d in result.declarations if d.kind == KIND_YAML_DOC
            ]
        else:
            headlines = [
                d.name for d in result.declarations if d.kind == KIND_YAML_KEY
            ]
    elif result.language in ("css", "scss"):
        flat = _flatten_css(result.declarations, opts)
        headlines = [d.name for d in flat]
    elif result.language == "html":
        # Skip the outer html → head/body chrome (always one of each) and
        # surface the landmark elements directly beneath them. Names
        # format is "what's on this page at a glance"; ``html / head /
        # body`` repeated every entry buys nothing.
        landmarks = _flatten_html_landmarks(result.declarations)
        headlines = [_html_names_signature(d) for d in landmarks]
    else:
        types = _flatten_types(result.declarations)
        if not opts.include_private:
            types = [t for t in types if t.visibility != "private"]
        blocks = _flatten_top_blocks(result.declarations)
        free_functions = _flatten_free_functions(result.declarations, opts)
        headlines = (
            [t.name for t in types]
            # Block labels are quoted — they are free-text and may carry
            # commas, which would otherwise collide with the `, ` that
            # joins the headline list.
            + [f"'{b.name}'" for b in blocks]
            + [f.name for f in free_functions]
        )

    if not headlines:
        return []
    return with_imports(f"{prefix}: {', '.join(headlines)}")


def _digest_one(
    result: ParseResult,
    opts: DigestOptions,
    flags: Optional[_LegendFlags] = None,
) -> list[str]:
    # Inject the descriptive size label between the filename and the
    # parenthesised counters: `  name.py [medium] (95 lines, ...)`.
    # The bracket lands right after the filename so the agent reads
    # the size class first, then the precise counters second.
    #
    # If the parse hit ERROR/MISSING nodes, append a `[broken]` marker —
    # plain English, instantly recognisable, no legend lookup needed.
    # Agent scanning digest sees `name.py [tiny] [broken]` and knows
    # the file's syntax is malformed somewhere, so the outline below
    # may be incomplete. The full `# WARNING:` line still appears
    # beneath the filename for those who want the count of errors.
    label = _size_label(_estimate_tokens(result.source))
    integrity = " [broken]" if result.error_count > 0 else ""
    prefix = f"  {result.path.name} {label}{integrity}"
    is_compact = opts.format == "compact"
    # `compact` drops the per-file `, X types, Y methods, Z fields`
    # breakdown — the totals (`N lines, ~N tokens`) drive routing
    # decisions, the breakdown only weighs the visual.
    lines = [_format_file_header(prefix, result, include_counters=not is_compact)]
    warn = _format_error_warning(result)
    if warn:
        # Indent under the file line so the warning lives with its file.
        lines.append("  " + warn)
    if opts.show_imports and (
        result.imports or result.conditional_imports_count > 0
    ):
        # 4-space indent matches type-header indent so the imports line
        # sits visually inside the file's block.
        lines.append(
            "    " + _format_imports_line(
                result.imports, result.conditional_imports_count
            )
        )

    # `[huge]` files (>100k tokens) collapse to header-only in digest. The
    # parenthesised counters (`51232 lines, ~742k tokens, 21 types, 25
    # methods, 47 fields`) already give the agent enough to decide whether
    # to drill in — expanding the body adds 30+ lines per file and inflates
    # the digest of a directory with many generated/vendored mega-files
    # (TS compiler internals, generated SDKs, large checkers) by 30×. A
    # second `ast-outline outline <path>` call recovers the full structure
    # on demand. The label is the agent's signal — documented in the
    # legend whenever a huge file appears in the batch.
    #
    # Outline / show are unchanged: when the agent explicitly opens a
    # single file by path, they wanted its full structure regardless of
    # size. Only digest (the broad-survey command) collapses.
    if label == "[huge]":
        if flags is not None:
            flags.huge = True
        return lines

    # Markdown files digest as a hierarchical TOC, not a type/member list.
    if result.language == "markdown":
        toc = _digest_markdown(result.declarations, opts, indent=4, depth=1, flags=flags)
        if not toc:
            if is_compact:
                return []
            lines[-1] += "  # empty"
            return lines
        lines.extend(toc)
        return lines

    # YAML files digest as either:
    # - top-level keys (single-doc files: `+apiVersion +kind +metadata +spec`)
    # - per-doc separator lines (multi-doc: each `--- doc N of M — ...`)
    # No `[yaml_key]` / `[yaml_doc]` annotations — the tag would be
    # uniform-and-noisy for every entry.
    if result.language == "yaml":
        body = _digest_yaml(result.declarations, opts, flags=flags)
        if not body:
            if is_compact:
                return []
            lines[-1] += "  # empty"
            return lines
        lines.extend(body)
        return lines

    # CSS / SCSS files digest as a flat token list — the existing
    # types/free-functions split assumes class-with-members shape, which
    # CSS doesn't have (rules don't have "members" the same way; nested
    # rules in SCSS are siblings under a parent block). Rendering rules
    # as types would produce header-per-rule clutter for files with 50+
    # selectors; rendering as a flat token list mirrors how the file
    # actually reads. Outline still shows the full hierarchy for an
    # agent that needs structure.
    if result.language in ("css", "scss"):
        body = _digest_css(result.declarations, opts, flags=flags)
        if not body:
            if is_compact:
                return []
            lines[-1] += "  # empty"
            return lines
        lines.extend(body)
        return lines

    # HTML files digest as a hierarchical map (similar to markdown TOC):
    # top-level elements first, two depth levels of children, deeper
    # subtrees collapse to a parent line only. ``html`` / ``head`` /
    # ``body`` count as "free" wrappers — they don't consume depth
    # budget, so a page rendered as ``html > body > main > section#hero``
    # still shows ``section#hero`` even though that's four levels deep.
    if result.language == "html":
        body = _digest_html(
            result.declarations, opts, indent=4, depth=1, flags=flags
        )
        if not body:
            if is_compact:
                return []
            lines[-1] += "  # empty"
            return lines
        lines.extend(body)
        return lines

    types = _flatten_types(result.declarations)
    blocks = _flatten_top_blocks(result.declarations)
    free_functions = _flatten_free_functions(result.declarations, opts)

    if not types and not blocks and not free_functions:
        # `compact` hides declaration-less files entirely (the empty
        # marker is noise when scanning a map); other formats keep the
        # header with an explicit `# no declarations` annotation.
        if is_compact:
            return []
        lines[-1] += "  # no declarations"
        return lines

    # Visual grouping rule: insert a blank line AFTER a type whose body
    # rendered at least one member row. Empty types (no body lines)
    # stack tightly so digest stays compact for declaration-heavy files.
    # The blank serves as a paragraph break between a "type + its
    # members" block and whatever comes next, mirroring how prose uses
    # blank lines to separate paragraphs. The trailing blank after the
    # last type is removed by `render_digest`'s final `rstrip`.
    for t in types:
        # Header layout: [attrs] [modifiers] keyword Name [deprecated]
        # [: bases]  L<a>-<b>. Each piece is opt-in — most types carry
        # only `keyword Name`, and the optional pieces are appended
        # only when they exist so plain types stay terse.
        keyword = t.native_kind or t.kind
        header_parts: list[str] = []

        # Attributes / decorators that aren't deprecation markers.
        # Adapters store each attr pre-formatted (`@dataclass`,
        # `[Serializable]`, `#[derive(Debug)]`), so a simple space-join
        # already reads right.
        visible_attrs = _filter_non_deprecation_attrs(t.attrs)
        if visible_attrs:
            header_parts.append(" ".join(visible_attrs))

        # Whitelisted type modifiers (`sealed`, `abstract`, …) parsed
        # out of the signature line. Visibility keywords are excluded
        # — already conveyed by the visibility filter.
        mods = _type_modifiers(t)
        if mods:
            header_parts.append(" ".join(mods))

        # Source-language native keyword + qualified type name.
        header_parts.append(f"{keyword} {t.name}")

        # Compact deprecation tag — replaces the visible deprecation
        # attr that we filtered out above.
        if _is_deprecated(t):
            header_parts.append("[deprecated]")
            if flags is not None:
                flags.deprecated = True

        # Inheritance, kept at the end of the declarative part so the
        # eye finds bases predictably (and a long base list doesn't
        # push other tags off-screen).
        if t.bases:
            header_parts.append(": " + ", ".join(t.bases))
            if flags is not None:
                flags.inheritance = True

        # `compact` drops the `L<a>-<b>` line range — Read-routing
        # workflows step up to `--format=default` when ranges matter.
        if is_compact:
            suffix = ""
        else:
            suffix = t.lines_suffix()
            if suffix and flags is not None:
                flags.line_range = True
        header = "    " + " ".join(header_parts) + suffix
        lines.append(header)
        members = _digest_members(t, opts)
        if members:
            collapsed = _collapse_overloads(members)
            shown = collapsed[: opts.max_members_per_type]
            tokens = [_member_token(m, count, parent=t, flags=flags) for m, count in shown]
            lines.extend(_wrap_tokens(tokens, width=100, indent="      "))
            if len(collapsed) > len(shown):
                lines.append(f"      ... ({len(collapsed) - len(shown)} more)")
            # `compact` skips the inter-type paragraph break — types
            # stack tightly to maximise information density per line.
            if not is_compact:
                lines.append("")

    # Callback-DSL blocks (test suites, setup-stores). Rendered like a
    # type: a `block <label>` header followed by member tokens for the
    # cases / nested blocks / functions declared inside. This keeps the
    # digest body consistent with the header counters (which already
    # recurse into a block's children) and mirrors how a class renders.
    for b in blocks:
        if is_compact:
            suffix = ""
        else:
            suffix = b.lines_suffix()
            if suffix and flags is not None:
                flags.line_range = True
        lines.append("    block " + b.name + suffix)
        members = _digest_members(b, opts)
        if members:
            collapsed = _collapse_overloads(members)
            shown = collapsed[: opts.max_members_per_type]
            tokens = [_member_token(m, c, parent=b, flags=flags) for m, c in shown]
            lines.extend(_wrap_tokens(tokens, width=100, indent="      "))
            if len(collapsed) > len(shown):
                lines.append(f"      ... ({len(collapsed) - len(shown)} more)")
            if not is_compact:
                lines.append("")

    # Module-level functions / fields (common in Python). No parent —
    # skip rules don't apply, free functions render with whatever
    # markers their signature carries (e.g. `async run_forever()`).
    if free_functions:
        collapsed = _collapse_overloads(free_functions)
        shown = collapsed[: opts.max_members_per_type]
        tokens = [_member_token(f, count, parent=None, flags=flags) for f, count in shown]
        lines.extend(_wrap_tokens(tokens, width=100, indent="    "))
    return lines


def _collapse_overloads(decls: list[Declaration]) -> list[tuple[Declaration, int]]:
    """Group consecutive same-name callables under a single representative.

    Returns a list of `(decl, count)` pairs in original order, where
    `count` is the number of callables sharing the name (1 = no
    overloads). Non-callables always pass through with `count=1` and
    are NOT merged even if names happen to repeat — same name across
    different non-callable kinds (e.g. a property and a field) is rare
    and would be a meaningful source-level distinction worth preserving.

    The first occurrence "wins" — its source position represents the
    group, so the digest still points to one concrete declaration the
    agent can `show`. Order across groups is the order in which the
    first occurrence appeared.
    """
    out: list[tuple[Declaration, int]] = []
    index_by_name: dict[str, int] = {}
    for d in decls:
        if d.kind in CALLABLE_KINDS and d.name in index_by_name:
            i = index_by_name[d.name]
            rep, count = out[i]
            out[i] = (rep, count + 1)
            continue
        if d.kind in CALLABLE_KINDS:
            index_by_name[d.name] = len(out)
        out.append((d, 1))
    return out


def _member_token(
    d: Declaration,
    count: int,
    parent: Optional[Declaration] = None,
    flags: Optional[_LegendFlags] = None,
) -> str:
    """Render a single digest token for a member or free declaration.

    - Callable kinds get a `()` suffix — universally understood as
      "this is a function" in programming-doc convention, no legend
      needed for an LLM reading cold.
    - Method markers (`async`, `static`, `abstract`, `virtual`,
      `override`, `open`, `suspend`, `sealed`) prefix the name in
      source order: `async fetch()`, `override Update()`,
      `abstract Render()`. Keeps the digest line close to how the
      source literally reads.
    - When `count > 1` the token carries an `[N overloads]` annotation
      so the agent knows the name resolves to multiple callables.
    - Non-callable kinds keep their `[kind]` tag so the type stays
      inferable without any other signal.
    - Deprecation surfaces as a trailing `[deprecated]` tag, the same
      compact form used on type headers. We don't show the underlying
      `@deprecated` / `[Obsolete]` attr here — member tokens are
      length-sensitive (many on one wrap-line) and the tag carries
      the actionable signal.

    `parent` is the enclosing type declaration (or None for free
    functions). It's used to apply skip rules — `static` member of
    `static class` and `abstract` member of `interface` are
    redundant signals, so we suppress them.

    No leading `+` marker — earlier digest revisions used `+name` as a
    member tag, but that visually collides with diff syntax and adds no
    semantic information beyond what `()` / `[kind]` already convey.
    """
    if d.kind in CALLABLE_KINDS:
        markers = _method_markers(d, parent)
        prefix = (" ".join(markers) + " ") if markers else ""
        if count > 1:
            base = f"{prefix}{d.name}() [{count} overloads]"
        else:
            base = f"{prefix}{d.name}()"
        if flags is not None:
            flags.callable = True
            if markers:
                flags.marker = True
            if count > 1:
                flags.overloads = True
    else:
        # A block's name is a free-text label (a test description) that
        # can contain commas — and `_wrap_tokens` joins member tokens
        # with `", "`. Quote the label so an internal comma stays inside
        # the token instead of reading as a token separator. Quotes also
        # mark it as a string label, matching the `it('...')` signature.
        label = f"'{d.name}'" if d.kind == KIND_BLOCK else d.name
        base = f"{label} [{d.kind}]"
        if flags is not None:
            flags.kind = True
    if _is_deprecated(d):
        base += " [deprecated]"
        if flags is not None:
            flags.deprecated = True
    return base


def _digest_yaml(
    decls: list[Declaration],
    opts: DigestOptions,
    flags: Optional[_LegendFlags] = None,
) -> list[str]:
    """Render YAML declarations for the digest body.

    Two shapes:
    - Multi-document files: emit each ``KIND_YAML_DOC`` separator line
      verbatim with its line range, so the agent sees `--- doc 1 of 3
      — ConfigMap prod/api-config  L1-8` for every doc in the file.
    - Single-document files: emit top-level keys as flat tokens
      (``apiVersion, kind, metadata, spec``). No ``[yaml_key]``
      annotation — every entry would carry it, pure noise. Tokens
      use the same comma-space separator and bare-name form as the
      code-digest body so YAML and code files share one mental model.

    Records `flags.line_range` only — YAML digest never emits any of
    the other legend tokens (no parens, no kind tags, no markers, no
    inheritance, no deprecation). For single-doc YAML it sets nothing,
    leaving the legend empty when the batch is YAML-only.
    """
    if any(d.kind == KIND_YAML_DOC for d in decls):
        out: list[str] = []
        for d in decls:
            if d.kind == KIND_YAML_DOC:
                suffix = d.lines_suffix()
                if suffix and flags is not None:
                    flags.line_range = True
                out.append("    " + d.signature + suffix)
        return out
    tokens = [d.name for d in decls if d.kind == KIND_YAML_KEY]
    if not tokens:
        return []
    return _wrap_tokens(tokens, width=100, indent="    ")


def _digest_css(
    decls: list[Declaration],
    opts: DigestOptions,
    flags: Optional["_LegendFlags"] = None,
) -> list[str]:
    """Render CSS / SCSS declarations for the digest body.

    Flattens the declaration tree to a single token list — rules nested
    under at-rules (`@media .btn { }`), under each other (CSS native
    nesting), or under SCSS `&` parents all surface as siblings here.
    Hierarchy is intentionally lost in digest, since digest is the
    catalog view (`what's defined here?`); the agent uses `outline`
    when it wants structure.

    Each token uses `_member_token` so callable kinds (`@mixin`,
    `@function`) get the universal `()` suffix and non-callable kinds
    get a `[kind]` tag. The kind tag is signal for the agent — `[rule]`
    vs `[at_rule]` vs `[variable]` vs `[placeholder]` answer different
    agent questions, even though the sigil prefix already disambiguates
    most cases.
    """
    flat = _flatten_css(decls, opts)
    if not flat:
        return []
    tokens = [_member_token(d, 1, parent=None, flags=flags) for d in flat]
    return _wrap_tokens(tokens, width=100, indent="    ")


def _flatten_css(
    decls: list[Declaration], opts: DigestOptions
) -> list[Declaration]:
    """Walk the declaration tree depth-first and return every entry as a
    flat list — parents first, then their resolved children. Respects
    `include_private` (skips `_`-prefixed SCSS mixins/functions/vars
    flagged as private by the adapter) and `include_fields` (skips
    SCSS `$variables`)."""
    out: list[Declaration] = []
    for d in decls:
        if d.kind == KIND_VARIABLE and not opts.include_fields:
            continue
        if d.visibility == "private" and not opts.include_private:
            # Skip the whole subtree — a filtered parent shouldn't leak
            # its children into the digest. For SCSS this matters most
            # for private `%placeholder` blocks: they have nested rules
            # whose own visibility is empty, so without the `continue`
            # the children would surface even when the parent was
            # explicitly hidden.
            continue
        out.append(d)
        if d.children:
            out.extend(_flatten_css(d.children, opts))
    return out


def _digest_markdown(
    decls: list[Declaration],
    opts: DigestOptions,
    indent: int,
    depth: int,
    flags: Optional[_LegendFlags] = None,
) -> list[str]:
    """Render markdown declarations as a hierarchical TOC.

    Respects `opts.max_heading_depth`. Code blocks are shown only when
    `opts.include_fields` is True (they're treated as noise in a TOC by
    default).

    Records only `flags.line_range` — every emitted heading carries a
    line-range suffix and that's the only legend-worthy token markdown
    digest emits. Pure-markdown batches therefore drop the legend
    entirely (line_range alone is dropped — see `_build_legend`).
    """
    out: list[str] = []
    if depth > opts.max_heading_depth:
        return out
    pad = " " * indent
    for d in decls:
        if d.kind == KIND_HEADING:
            suffix = d.lines_suffix()
            if suffix and flags is not None:
                flags.line_range = True
            out.append(pad + d.signature + suffix)
            out.extend(_digest_markdown(d.children, opts, indent + 2, depth + 1, flags=flags))
        elif d.kind == KIND_CODE_BLOCK and opts.include_fields:
            suffix = d.lines_suffix()
            if suffix and flags is not None:
                flags.line_range = True
            out.append(pad + d.signature + suffix)
    return out


# Tags treated as "chrome" by HTML digest — wrappers every page has so
# listing them in the digest adds nothing. The digest walker descends
# through them transparently (their depth doesn't count against
# ``opts.max_heading_depth``).
_HTML_DIGEST_CHROME_TAGS = frozenset({"html", "head", "body"})


def _digest_html(
    decls: list[Declaration],
    opts: DigestOptions,
    indent: int,
    depth: int,
    flags: Optional[_LegendFlags] = None,
) -> list[str]:
    """Render HTML declarations as a depth-limited hierarchical map.

    Top-level ``html`` / ``head`` / ``body`` wrappers are transparent —
    they neither consume depth budget nor get a line of their own (they
    appear in every page; the digest is about what's *unique* on this
    page). Other elements render their signature + line range, and
    recurse one depth level deeper (matching ``opts.max_heading_depth``
    semantics).

    Sub-trees that bottom out under the depth cap collapse silently —
    the parent's line range already covers them, and a per-leaf line
    would defeat the digest's "page-at-a-glance" purpose.
    """
    out: list[str] = []
    if depth > opts.max_heading_depth:
        return out
    pad = " " * indent
    for d in decls:
        if d.kind != KIND_HTML_ELEMENT:
            continue
        if d.name in _HTML_DIGEST_CHROME_TAGS:
            # Pass through — don't print, don't charge depth.
            out.extend(_digest_html(d.children, opts, indent, depth, flags=flags))
            continue
        suffix = d.lines_suffix()
        if suffix and flags is not None:
            flags.line_range = True
        out.append(pad + d.signature + suffix)
        out.extend(
            _digest_html(d.children, opts, indent + 2, depth + 1, flags=flags)
        )
    return out


def _flatten_html_landmarks(decls: list[Declaration]) -> list[Declaration]:
    """Top-level landmarks for ``--format=names`` HTML digests.

    Descends transparently through ``html`` / ``head`` / ``body``
    wrappers and returns their immediate children. The result is a
    flat list of "what's on the page" — header / main / nav / footer /
    sections / forms — without the chrome that's identical on every
    page. Non-chrome top-level elements are returned as-is.
    """
    out: list[Declaration] = []
    for d in decls:
        if d.kind != KIND_HTML_ELEMENT:
            continue
        if d.name in _HTML_DIGEST_CHROME_TAGS:
            out.extend(_flatten_html_landmarks(d.children))
        else:
            out.append(d)
    return out


def _html_names_signature(d: Declaration) -> str:
    """Strip the ``[import] `` prefix and ``: heading-text`` suffix from
    an HTML declaration's signature for the ``--format=names`` digest.
    Names format puts every entry on one comma-joined line, so heading
    previews and import markers would bloat it; they're recoverable from
    the full outline."""
    sig = d.signature
    if sig.startswith("[import] "):
        sig = sig[len("[import] "):]
    cut = sig.find(": ")
    if cut >= 0:
        sig = sig[:cut]
    return sig.strip()


def _flatten_free_functions(decls: list[Declaration], opts: DigestOptions) -> list[Declaration]:
    """Module-level callables/fields that aren't inside a type. Dives through namespaces."""
    out: list[Declaration] = []
    for d in decls:
        if d.kind == KIND_NAMESPACE:
            out.extend(_flatten_free_functions(d.children, opts))
        elif d.kind in _DIGEST_TYPE_KINDS:
            continue
        elif d.kind == KIND_BLOCK:
            # Callback-DSL blocks are rendered by their own digest pass
            # (`_flatten_top_blocks` + the blocks loop). Skip without
            # recursing — a function declared inside a block belongs to
            # that block as a member, not to the module's free-function
            # list.
            continue
        else:
            if d.kind == KIND_FIELD and not opts.include_fields:
                continue
            if d.visibility == "private" and not opts.include_private:
                continue
            out.append(d)
    return out


def _flatten_top_blocks(decls: list[Declaration]) -> list[Declaration]:
    """Top-level ``KIND_BLOCK`` containers (test suites, setup-stores).

    Dives through namespaces, but does NOT recurse into a block's own
    children — a nested block (`it` inside `describe`) stays a member of
    its parent and is rendered as a member token, not hoisted to its own
    digest header. Mirrors how `_flatten_types` treats nested types as
    rows but `_digest_members` handles the one-level member expansion.
    """
    out: list[Declaration] = []
    for d in decls:
        if d.kind == KIND_NAMESPACE:
            out.extend(_flatten_top_blocks(d.children))
        elif d.kind == KIND_BLOCK:
            out.append(d)
    return out


def _flatten_types(decls: list[Declaration], prefix: str = "") -> list[Declaration]:
    """Dive through namespaces to collect types with qualified names.

    Tables and views are included in the digest "types" section even
    though they're not in ``TYPE_KINDS`` — they're type-like containers
    (named bag of fields) for digest-rendering purposes. The qualified
    -name prefix only chains through namespaces and through *true* type
    nesting (class-in-class), not through tables — SQL doesn't nest
    tables, and agents query column names unqualified.
    """
    out: list[Declaration] = []
    for d in decls:
        if d.kind == KIND_NAMESPACE:
            out.extend(_flatten_types(d.children, prefix=(prefix + d.name + "." if prefix else d.name + ".")))
        elif d.kind in _DIGEST_TYPE_KINDS:
            qualified = Declaration(
                kind=d.kind,
                name=(prefix + d.name) if prefix else d.name,
                signature=d.signature,
                bases=d.bases,
                attrs=d.attrs,
                docs=d.docs,
                visibility=d.visibility,
                native_kind=d.native_kind,
                start_line=d.start_line,
                end_line=d.end_line,
                start_byte=d.start_byte,
                end_byte=d.end_byte,
                doc_start_byte=d.doc_start_byte,
                children=d.children,
                match_names=d.match_names,
            )
            out.append(qualified)
            # Also flatten nested types (they appear as their own rows).
            # Skip nested-type recursion for tables/views — their
            # children are columns, not types-within-types.
            if d.kind in TYPE_KINDS:
                out.extend(_flatten_types(d.children, prefix=qualified.name + "."))
    return out


def _digest_members(type_decl: Declaration, opts: DigestOptions) -> list[Declaration]:
    members: list[Declaration] = []
    for c in type_decl.children:
        if c.kind in _DIGEST_TYPE_KINDS or c.kind == KIND_NAMESPACE:
            continue
        if c.kind == KIND_ENUM_MEMBER:
            continue
        if c.kind == KIND_FIELD and not opts.include_fields:
            continue
        if c.visibility == "private" and not opts.include_private:
            continue
        members.append(c)
    return members


def _wrap_tokens(tokens: list[str], width: int, indent: str) -> list[str]:
    """Wrap a flat list of digest tokens into width-bounded lines.

    Uses `", "` (comma-space) as the inter-token separator — universally
    understood as a list separator across programming docs, English
    prose, and CSV. Stronger LLM signal than double-space: BPE
    tokenisers split commas into discrete tokens, and natural-language
    training data overwhelmingly uses commas to delimit list items, so
    attention reliably treats each piece as a separate element.

    Identifier tokens never contain commas (kind tags are single words
    like `property` / `field`), so the separator is unambiguous.
    """
    if not tokens:
        return []
    out: list[str] = []
    cur = indent
    for tok in tokens:
        piece = (", " if cur != indent else "") + tok
        if len(cur) + len(piece) > width and cur != indent:
            out.append(cur)
            cur = indent + tok
        else:
            cur += piece
    if cur != indent:
        out.append(cur)
    return out


# --- Search ---------------------------------------------------------------


def find_symbols(result: ParseResult, symbol: str) -> list[SymbolMatch]:
    """Find declarations matching a dotted symbol path.

    Matching is suffix-based — `TakeDamage` matches `Foo.Bar.TakeDamage`
    and `PlayerController.TakeDamage` also matches.

    A dot is a hierarchy separator, so a declaration whose own name
    contains a literal dot (a callback-DSL block named after a free-text
    label — e.g. a test `it('parses 3.14 correctly')`) would be missed
    by the dotted walk. As a fallback, when a multi-part query finds
    nothing the whole string is retried as a single name.

    Markdown headings get a relaxed contract: case-insensitive **substring**
    containment per dotted part, with inline-markdown punctuation (backticks,
    asterisks, underscores, tildes) stripped from BOTH the heading title and
    the query before comparing. Heading text routinely carries decoration
    an LLM agent can't be expected to remember verbatim — number prefixes
    (``1.`` ``2.1``), trailing qualifiers (``(февраль 2026)``,
    ``(Уверенность: 70%)``), inline-code/emphasis markup. So
    ``"ТЕКУЩИЙ АНАЛИЗ"`` matches ``"1. ТЕКУЩИЙ АНАЛИЗ (февраль 2026)"`` and
    ``"useState — when to reach"`` matches ``"`useState` — when to reach for it"``
    for headings, even though neither would for a code symbol.
    """
    parts = _split_query(symbol)
    matches: list[SymbolMatch] = []
    _search_walk(result.declarations, result.source, [], [], parts, matches)
    # Fallback for free-text declaration names that contain a literal
    # dot. `_split_query` reads every `.` as a hierarchy separator, which
    # is right for code symbols (`Class.method`) but wrong for a
    # callback-DSL block named after a free-text label — a test
    # `it('parses 3.14 correctly')` becomes a block named `parses 3.14
    # correctly`. The dotted-trail walk above then hunts for a nesting
    # that doesn't exist and finds nothing. When a multi-part query
    # yields zero hits, retry once treating the whole string as a single
    # name. Gated on `not matches` so a successful dotted lookup never
    # pays for the second walk; gated on `len(parts) > 1` so a query
    # with no separator (already a single token) isn't walked twice.
    if not matches and len(parts) > 1:
        _search_walk(result.declarations, result.source, [], [], [symbol], matches)
    return matches


import re as _re

# Regex tokenizer for dotted/bracketed queries.
# Matches either a `[…]` bracketed segment (sequence-index path
# component, used by YAML) or a stretch of chars that are neither
# a dot nor an opening bracket.
# Examples:
#   "Foo.Bar"               → ["Foo", "Bar"]
#   "containers[0].image"   → ["containers", "[0]", "image"]
#   "matrix[2][3]"          → ["matrix", "[2]", "[3]"]
#   "[0].image"             → ["[0]", "image"]
_QUERY_TOKEN_RE = _re.compile(r"\[[^\]]*\]|[^.\[]+")

# Matches a ``[non-digit…]`` bracket — the discriminator between an HTML
# attribute selector (``[name=email]``, ``[required]``) and a YAML
# sequence index (``[0]``, ``[12]``). Used by ``_split_query`` to decide
# whether a tag-prefixed query like ``tag[…]`` should short-circuit as
# a single token (HTML) or stay multi-token (legacy YAML behaviour).
_HTML_ATTR_BRACKET_RE = _re.compile(r"\[[^\d\]]")

# Markdown-numbered-heading short-circuit. Recognises queries shaped
# like ``"3. Foo"``, ``"4.2 Foo"``, ``"1.2.3. Foo"`` — a numeric
# prefix (optionally decimal-dotted, optionally trailed by a period)
# followed by whitespace and at least one non-whitespace char. See
# ``_split_query`` for why we keep these as a single token.
_LEADING_NUMBER_PREFIX_RE = _re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")

# Char-level inline-markdown decoration stripped from both sides of a
# heading substring compare. Covers the symmetric, char-level CommonMark
# markers: inline-code (`` ` ``), emphasis / strong (``*``, ``_``), and
# strikethrough (``~``). Structural markup — links ``[text](url)``,
# autolinks ``<url>``, HTML tags — is NOT included: char-wise stripping
# would corrupt the visible label rather than peel decoration. See
# ``_trail_matches`` and ``find_symbols`` for the matching contract.
_INLINE_MD_PUNCT_RE = _re.compile(r"[`*_~]+")


def _split_query(symbol: str) -> list[str]:
    """Tokenise a dotted (and possibly bracketed) query into trail parts.

    Code symbols use plain dots: ``Foo.Bar.method`` → ``["Foo", "Bar", "method"]``.

    YAML / data-shaped queries can include JSONPath-style sequence
    indices: ``spec.containers[0].image`` →
    ``["spec", "containers", "[0]", "image"]``. The bracket part lands
    as its OWN trail entry (matching how the YAML adapter emits
    sequence items as ``Declaration(name="[0]")``), so suffix-matching
    works without special cases in the walker.

    CSS-style queries — anything starting with ``.``, ``#``, ``%``,
    ``&``, ``@``, or ``:`` (a CSS sigil) — are returned as a single
    token. The dots inside `.btn.primary` are NOT path separators
    here: they're part of the CSS compound-selector syntax. Without
    this short-circuit, querying `.btn-primary` would split into
    `["btn-primary"]` (the leading dot stripped) and miss the rule
    whose `match_names` contains the literal `.btn-primary`.

    HTML-style compound selectors that start with a tag name —
    ``section#hero``, ``button.primary``, ``input[name=email]``,
    ``section#hero.primary[disabled]`` — are also returned as a single
    token. The marker is "starts with a letter AND contains ``#`` or
    ``[`` AND has no whitespace" — the no-whitespace clause prevents
    swallowing descendant-combinator queries like ``"section .item"``
    that an agent might paste from CSS muscle memory (those still split
    naturally on the space and won't match anything, by design — combinators
    aren't supported). Code symbols are unaffected because they don't
    contain ``#`` or ``[``.

    Markdown numbered-heading queries — ``"3. Foo Bar"``,
    ``"4.2 Decimal Foo"`` — are also returned as a single token.
    ``outline`` prints these headings with the prefix intact (the
    prefix is part of the heading's ``name``), so an agent pasting
    the printed form back into ``show`` arrives here verbatim.
    Without this short-circuit the dot in ``3.`` reads as a path
    separator → ``["3", " Foo Bar"]`` → matching demands a
    two-segment trail that doesn't exist (markdown headings are
    single declarations). We require ``\\s+\\S`` after the numeric
    prefix so pure numeric / dotted-numeric queries without trailing
    text (``"1.2"``, ``"1.foo"``) keep the existing token-split
    behaviour — only the natural numbered-heading shape is
    short-circuited."""
    if symbol[:1] in ".#%&@:":
        return [symbol]
    if _LEADING_NUMBER_PREFIX_RE.match(symbol):
        return [symbol]
    # HTML compound selector starting with a tag name
    # (`section#hero`, `input[name=email]`, `section#hero.primary[disabled]`).
    # Markers: leading letter + at least one `#` OR an attribute-style
    # ``[non-digit…]`` bracket + no whitespace. The non-digit clause keeps
    # YAML sequence-index paths like ``spec.containers[0].image`` on the
    # legacy multi-token path (YAML uses numeric brackets, HTML uses named
    # attribute brackets). The no-whitespace clause prevents swallowing
    # descendant-combinator queries like ``"section .item"`` that an agent
    # might paste from CSS muscle memory.
    if (
        symbol[:1].isalpha()
        and not any(ch.isspace() for ch in symbol)
        and ("#" in symbol or _HTML_ATTR_BRACKET_RE.search(symbol))
    ):
        return [symbol]
    return _QUERY_TOKEN_RE.findall(symbol)


def _join_trail(trail: list[str]) -> str:
    """Build a ``qualified_name`` from a trail of declaration names.

    Bracketed parts (``[0]``, ``[12]``) attach to the previous part
    WITHOUT a dot separator, so a YAML sequence path renders as
    ``containers[0].image`` (JSONPath-natural) instead of
    ``containers.[0].image`` (clunky, would also need special parsing
    on the agent side)."""
    if not trail:
        return ""
    out = trail[0]
    for part in trail[1:]:
        if part.startswith("[") and part.endswith("]"):
            out += part
        else:
            out += "." + part
    return out


def _search_walk(
    decls: list[Declaration],
    src: bytes,
    trail: list[str],
    ancestors: list[Declaration],
    parts: list[str],
    out: list[SymbolMatch],
) -> None:
    for d in decls:
        # Markdown headings opt in to substring matching — see find_symbols
        # docstring. Other kinds keep strict suffix-equality semantics so
        # code-symbol lookups stay precise.
        substring = d.kind == KIND_HEADING
        # `match_names` is the opt-in mechanism for declarations
        # reachable under multiple identifiers (e.g. CSS rule with a
        # selector list `.a, .b, .c`, or an SCSS nested rule with `&`
        # resolved against each parent). When set, the entries are the
        # canonical *absolute* names — they aren't path-joined with the
        # parent trail, since they already carry whatever scope they
        # have. When empty, fall back to `[d.name]` and the standard
        # path-joining semantics — code adapters get unchanged behavior.
        candidates = d.match_names or ([d.name] if d.name else [])
        new_trail = trail + [d.name] if d.name else trail
        matched = False
        for cand in candidates:
            if matched:
                break
            cand_trail = trail + [cand] if cand else trail
            if cand and _trail_matches(cand_trail, parts, substring=substring):
                start = d.doc_start_byte or d.start_byte
                end = d.end_byte
                # Absolute names from `match_names` represent themselves
                # as `qualified_name`; everything else is path-joined.
                qual = cand if d.match_names else _join_trail(cand_trail)
                out.append(
                    SymbolMatch(
                        qualified_name=qual,
                        kind=d.kind,
                        start_line=d.start_line,
                        end_line=d.end_line,
                        source=src[start:end].decode("utf8", errors="replace"),
                        ancestor_signatures=[a.signature for a in ancestors if a.signature],
                        decl=d,
                    )
                )
                matched = True
        if d.children:
            _search_walk(d.children, src, new_trail, ancestors + [d], parts, out)


def _trail_matches(trail: list[str], parts: list[str], *, substring: bool = False) -> bool:
    if len(parts) > len(trail):
        return False
    tail = trail[-len(parts):]
    if substring:
        # Case-insensitive containment per element. ``casefold`` (not
        # ``lower``) so non-ASCII titles match correctly — German ß,
        # Turkish dotted/dotless I, etc. Inline-markdown punctuation
        # is stripped from BOTH sides so an agent that drops backticks
        # / asterisks from the heading text it copied out of ``outline``
        # still resolves the section. Markdown-only — ``substring`` is
        # only True for ``KIND_HEADING``. A query part that consists
        # entirely of inline-md punctuation (`` "*" ``, ``"~~~"``)
        # strips to the empty string; without an explicit guard,
        # ``"" in any_title`` returns True and a purely-decoration query
        # would match every heading. Require the stripped query part to
        # be non-empty.
        for p, t in zip(parts, tail):
            sp = _INLINE_MD_PUNCT_RE.sub("", p)
            if not sp:
                return False
            if sp.casefold() not in _INLINE_MD_PUNCT_RE.sub("", t).casefold():
                return False
        return True
    return tail == parts


