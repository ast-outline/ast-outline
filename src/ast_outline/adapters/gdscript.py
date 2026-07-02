"""GDScript adapter — hand-written outline parser for .gd files.

Unlike every other adapter, this one does NOT use tree-sitter: no
maintained ``tree-sitter-gdscript`` wheel exists on PyPI (the upstream
grammar repo carries Python bindings but has never published them).
An outline needs declaration heads, nesting and body extents — not a
full expression grammar — so a purpose-built scanner is small enough
to keep correct by hand.

Grammar ground truth, in priority order:

- Godot 4 ``gdscript_tokenizer.cpp`` — string forms (``"`` / ``'``,
  raw newlines legal in ANY string, ``r`` / ``&`` / ``^`` prefixes), ``#``
  comments, backslash + open-bracket line continuation, tab/space
  indentation (consistent within a file, mixing is a Godot error).
- Godot 4 ``gdscript_parser.cpp`` — the annotation registry, the
  class-body member set (``var`` / ``const`` / ``signal`` / ``func`` /
  ``class`` / ``enum`` / ``static``), property ``get`` / ``set`` forms,
  ``@abstract`` bodyless functions, ``class_name X extends Y``.
- tree-sitter-gdscript grammar — the Godot 3 compatibility shapes
  (``export var`` / ``onready var`` / ``setget`` / rpc keywords).

How the scanner stays honest without an AST: every logical line is
kept in two aligned copies — ``display`` (source text, comments
stripped, strings verbatim) and ``shadow`` (same positions, string
*contents* blanked to spaces). All structural decisions (keyword
heads, depth-0 colons, semicolons, ``preload`` hits) read the shadow,
so a ``"func fake():"`` inside a string literal can never produce a
declaration; all rendered text (signatures, names) slices the display.

Design notes (how GDScript concepts map to the IR):

- ``class_name X`` + ``extends Y`` (the script's implicit-class
  header, in either order, possibly on one line) → ONE merged
  KIND_CLASS declaration with ``bases``. Members stay flat siblings —
  matches GDScript's flat script style, mirrors how tree-sitter
  models the file.
- bare ``extends Y`` (no ``class_name``) → KIND_CLASS named after the
  base with ``native_kind="extends"`` — lets an agent find every
  script extending a given type by symbol search.
- ``signal``    → KIND_EVENT (GDScript signals are C# events).
- ``enum``      → KIND_ENUM with KIND_ENUM_MEMBER children. Members
  inherit the enum's line/byte range — ``show`` on a member prints
  the whole enum, which is the useful context anyway.
- ``const``     → KIND_FIELD. ``var`` → KIND_FIELD, or KIND_PROPERTY
  when it carries property syntax: an indented ``get``/``set`` block,
  the inline ``get =`` / ``set =`` reference form, or legacy Godot 3
  ``setget``.
- ``func``      → KIND_FUNCTION at script level, KIND_METHOD inside
  inner classes, ``_init`` → KIND_CTOR. Lambdas (``func`` in
  expression position) are never captured: statement heads are only
  read at class-body scope, and ``var f = func(): ...`` keeps the
  ``var`` as the declaration while its body folds into the var's
  line range.
- annotations (``@export``, ``@onready``, ``@rpc(...)``, …) →
  ``Declaration.attrs`` (decorator model, like Python). Standalone-only
  annotations (``@export_group`` etc.) are dropped — they describe
  inspector layout, not the next declaration.
- ``## doc comments`` → ``docs[]``, rendered before the signature
  (C# ``///`` placement).
- ``const X = preload("res://...")`` and ``extends "res://..."`` →
  ``ParseResult.imports`` — ``preload`` IS GDScript's import.
  ``load(`` / ``preload(`` inside function bodies → counted in
  ``conditional_imports_count`` (PHP-adapter precedent for runtime
  deps).
- Visibility: leading ``_`` → private (GDScript convention), EXCEPT
  engine virtual callbacks (``_ready``, ``_process``, …) — they are
  the script's primary API surface and must survive digest's
  default private filter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_EVENT,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_PROPERTY,
    Declaration,
    ParseResult,
)


class GDScriptAdapter:
    language_name = "gdscript"
    display_name = "GDScript"
    extensions = {".gd"}
    definition_keywords = frozenset(
        {"func", "class", "class_name", "var", "const", "signal", "enum", "static"}
    )
    comment_line_prefixes = ("#",)
    # GDScript's import is `preload(...)` in a const/var initializer —
    # not line-shaped, so the prefix classifier can't catch it. The
    # adapter populates `import_regions` instead.
    import_line_prefixes = ()
    render_family = "code"

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        scan = _scan(src)
        decls, imports, import_regions, conditional = _build(scan.logicals, scan.docs)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=scan.errors,
            imports=imports,
            conditional_imports_count=conditional,
            noise_regions=sorted(scan.noise),
            import_regions=sorted(import_regions),
        )


# --- Logical-line scanner ---------------------------------------------------


@dataclass
class _Logical:
    """One statement after joining continuations.

    ``display`` and ``shadow`` are built from identical slices, so any
    character index valid in one is valid in the other — parse the
    shadow, slice the display.
    """

    indent: int       # leading whitespace chars of the first physical line
    display: str      # comment-stripped text, strings verbatim, lines joined by " "
    shadow: str       # same, but string contents blanked with spaces
    start_line: int   # 1-based, first physical line
    end_line: int     # 1-based, last physical line
    start_byte: int   # first physical line start + indent
    end_byte: int     # end of the last physical line (before its newline)


@dataclass
class _ScanResult:
    logicals: list[_Logical] = field(default_factory=list)
    # `## doc` lines: physical lineno → (raw stripped text, start byte).
    docs: dict[int, tuple[str, int]] = field(default_factory=dict)
    noise: list[tuple[int, int, str]] = field(default_factory=list)
    errors: int = 0


def _scan(src: bytes) -> _ScanResult:
    """Split source into logical lines (statements).

    A logical line continues across physical lines while (a) inside a
    string — Godot's tokenizer allows raw newlines in ANY string
    literal, not just triple-quoted ones, and only errors on EOF
    (verified against ``string()`` in gdscript_tokenizer.cpp; real
    projects ship plain ``"`` strings spanning lines) — (b) any
    bracket is open, since the tokenizer suppresses NEWLINE inside
    ``()`` / ``[]`` / ``{}``, or (c) the line ends with a backslash.
    Comments are stripped per physical line. Strings that span lines
    (and all triple-quoted ones) are recorded as noise regions (byte
    ranges) so ``grep`` can discount matches inside them.
    """
    res = _ScanResult()

    parts_d: list[str] = []
    parts_s: list[str] = []
    log_indent = 0
    log_start_line = 0
    log_start_byte = 0
    depth = 0

    str_delim = ""        # `"` / `'` / `"""` / `'''` while inside a string
    str_esc = False       # backslash escape pending across a line break
    str_start = 0         # byte offset of the open delimiter
    str_start_line = 0    # physical line the string opened on

    def flush(end_line: int, end_byte: int) -> None:
        segs_d: list[str] = []
        segs_s: list[str] = []
        # Trim each physical part with DISPLAY-derived bounds and apply
        # them to both copies — keeps the two strings aligned even when
        # a part begins or ends inside blanked string content.
        for dpart, spart in zip(parts_d, parts_s):
            a = len(dpart) - len(dpart.lstrip())
            b = len(dpart.rstrip())
            if b > a:
                segs_d.append(dpart[a:b])
                segs_s.append(spart[a:b])
        if segs_d:
            res.logicals.append(
                _Logical(
                    indent=log_indent,
                    display=" ".join(segs_d),
                    shadow=" ".join(segs_s),
                    start_line=log_start_line,
                    end_line=end_line,
                    start_byte=log_start_byte,
                    end_byte=end_byte,
                )
            )
        parts_d.clear()
        parts_s.clear()

    offset = 0
    lineno = 0
    line_end_byte = 0
    for raw in src.split(b"\n"):
        lineno += 1
        line_start = offset
        offset += len(raw) + 1
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        text = raw.decode("utf-8", errors="replace")
        # Deliberately computed AFTER the \r strip: a declaration's
        # end_byte must exclude the trailing CR so `show` slices never
        # end in a bare carriage return. `offset` above still advances
        # over the full \r\n — file positions stay exact.
        line_end_byte = line_start + len(raw)
        n = len(text)

        if not parts_d and not str_delim:
            stripped = text.lstrip(" \t")
            if not stripped:
                continue
            if stripped[0] == "#":
                if stripped.startswith("##"):
                    ws = len(text) - len(stripped)
                    res.docs[lineno] = (stripped.rstrip(), line_start + ws)
                continue
            log_indent = len(text) - len(stripped)
            log_start_line = lineno
            log_start_byte = line_start + log_indent
            i = log_indent
        else:
            i = 0

        d_chars: list[str] = []
        s_chars: list[str] = []
        while i < n:
            ch = text[i]
            if str_delim:
                # Inside a string. Backslash escapes the next char —
                # close enough for raw strings too (Godot's raw strings
                # still escape \" / \\).
                if str_esc:
                    str_esc = False
                    d_chars.append(ch)
                    s_chars.append(" ")
                    i += 1
                    continue
                if ch == "\\":
                    str_esc = True
                    d_chars.append(ch)
                    s_chars.append(" ")
                    i += 1
                    continue
                if ch == str_delim[0] and text[i : i + len(str_delim)] == str_delim:
                    d_chars.append(str_delim)
                    s_chars.append(str_delim)
                    i += len(str_delim)
                    # Noise regions are for cross-line matching only —
                    # record strings that span physical lines, plus all
                    # triple-quoted ones (their delimiters confuse
                    # single-line heuristics even on one line).
                    if len(str_delim) == 3 or str_start_line != lineno:
                        close = line_start + len(text[:i].encode("utf-8", "replace"))
                        res.noise.append((str_start, close, "string"))
                    str_delim = ""
                    continue
                d_chars.append(ch)
                s_chars.append(" ")
                i += 1
                continue
            if ch == "#":
                break  # comment runs to end of the physical line
            if ch in "\"'":
                # Godot allows raw newlines in ANY string literal, so a
                # quote that doesn't close on this physical line simply
                # continues the string (and the logical line) — the only
                # unterminated-string error is at EOF.
                if text[i : i + 3] == ch * 3:
                    str_delim = ch * 3
                else:
                    str_delim = ch
                str_esc = False
                str_start = line_start + len(text[:i].encode("utf-8", "replace"))
                str_start_line = lineno
                d_chars.append(str_delim)
                s_chars.append(str_delim)
                i += len(str_delim)
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    res.errors += 1
                else:
                    depth -= 1
            d_chars.append(ch)
            s_chars.append(ch)
            i += 1

        parts_d.append("".join(d_chars))
        parts_s.append("".join(s_chars))

        if str_delim:
            continue  # logical line continues inside the string
        s_line = parts_s[-1]
        trimmed = s_line.rstrip()
        if trimmed.endswith("\\"):
            # Explicit continuation — drop the backslash so the joined
            # statement reads naturally.
            cut = len(trimmed) - 1
            parts_d[-1] = parts_d[-1][:cut]
            parts_s[-1] = parts_s[-1][:cut]
            continue
        if depth > 0:
            continue
        flush(lineno, line_end_byte)

    if str_delim:
        res.errors += 1
        res.noise.append((str_start, len(src), "string"))
    if depth > 0:
        res.errors += 1
    flush(lineno, line_end_byte)
    return res


# --- Statement parsing / tree building --------------------------------------


# Engine virtual callbacks — underscore-named but they ARE the script's
# public surface (Godot calls them). Curated, not exhaustive: the common
# Object / Node / CanvasItem / Control / physics virtuals an agent
# expects to see in a digest. Anything else starting with `_` (helpers,
# `_on_*` signal handlers) keeps the private-by-convention reading.
_ENGINE_CALLBACKS = frozenset({
    "_init", "_static_init", "_ready", "_process", "_physics_process",
    "_enter_tree", "_exit_tree", "_notification", "_to_string",
    "_input", "_unhandled_input", "_unhandled_key_input",
    "_shortcut_input", "_gui_input", "_input_event",
    "_draw", "_get_configuration_warnings",
    "_get", "_set", "_get_property_list",
    "_property_can_revert", "_property_get_revert", "_validate_property",
    "_get_minimum_size", "_can_drop_data", "_drop_data", "_get_drag_data",
    "_make_custom_tooltip", "_integrate_forces", "_pressed", "_toggled",
})

# Annotations the Godot parser registers as STANDALONE — they describe
# inspector layout / warning scopes, not the next declaration, so
# attaching them as attrs would mislead.
_STANDALONE_ANNOTATIONS = frozenset({
    "export_category", "export_group", "export_subgroup",
    "warning_ignore_start", "warning_ignore_restore",
})

# Godot 3 declaration modifiers (Godot 4 replaced them with annotations)
# plus `static`, which both versions share. `export` may carry arguments.
_MOD_RE = re.compile(
    r"(static|onready|export|remote|master|puppet|remotesync|mastersync|sync)\b"
)

_ANNOTATION_RE = re.compile(r"@([^\W\d]\w*)")
_FUNC_RE = re.compile(r"func\s+([^\W\d]\w*)\s*\(")
_CLASS_NAME_RE = re.compile(r"class_name\s+([^\W\d]\w*)")
_CLASS_RE = re.compile(r"class\s+([^\W\d]\w*)")
_EXTENDS_RE = re.compile(r"extends\s+(\S)")
_SIGNAL_RE = re.compile(r"signal\s+([^\W\d]\w*)")
_ENUM_RE = re.compile(r"enum\s*([^\W\d]\w*)?\s*\{")
_CONST_RE = re.compile(r"const\s+([^\W\d]\w*)")
_VAR_RE = re.compile(r"var\s+([^\W\d]\w*)")
_IDENT_RE = re.compile(r"[^\W\d]\w*")
_GETSET_REF_RE = re.compile(r"(?:get|set)\s*=")
_EXTENDS_INLINE_RE = re.compile(r"\bextends\s+")
_SETGET_RE = re.compile(r"\bsetget\b")
_FUNC_OR_BRACKET_RE = re.compile(r"[([{)\]}]|\bfunc\b")
# `preload(` / `load(` as a call head — `(?<![\w.])` rejects
# `ResourceLoader.load(` (dotted method) and `download(` (identifier
# suffix). Matched against the shadow, so string contents can't hit.
_LOAD_RE = re.compile(r"(?<![\w.])(?:pre)?load\s*\(")


@dataclass
class _Head:
    """Parsed statement head — everything `_build` needs to make a
    Declaration and decide scope behavior."""

    kind: str
    name: str
    signature: str
    native: str
    bases: list[str] = field(default_factory=list)
    children: list[Declaration] = field(default_factory=list)
    push: bool = False        # an indented body follows (trailing depth-0 colon)
    captures: bool = False    # pushed scope captures member declarations
    is_value_decl: bool = False  # const / var — eligible for preload imports
    extends_target: str = ""  # set for bare `extends` statements


def _build(
    logicals: list[_Logical],
    docs: dict[int, tuple[str, int]],
) -> tuple[list[Declaration], list[str], list[tuple[int, int]], int]:
    top: list[Declaration] = []
    imports: list[str] = []
    import_regions: list[tuple[int, int]] = []
    conditional = 0

    @dataclass
    class _Scope:
        indent: int
        decl: Optional[Declaration]
        captures: bool
        children: list[Declaration]

    stack: list[_Scope] = [_Scope(-1, None, True, top)]
    prev: Optional[_Logical] = None

    pending_attrs: list[str] = []
    pending_line = 0
    pending_byte = 0
    pending_indent = -1

    def close(scope: _Scope, last: Optional[_Logical]) -> None:
        if scope.decl is not None and last is not None:
            scope.decl.end_line = last.end_line
            scope.decl.end_byte = last.end_byte

    def clear_pending() -> None:
        nonlocal pending_indent
        pending_attrs.clear()
        pending_indent = -1

    for L in logicals:
        while len(stack) > 1 and L.indent <= stack[-1].indent:
            close(stack.pop(), prev)
        # Pending annotations must sit at the same indent as the
        # declaration they decorate; a dedent/indent in between means
        # they belonged to something we didn't capture.
        if pending_attrs and L.indent != pending_indent:
            clear_pending()

        for d_seg, s_seg in _split_semicolons(L):
            container = stack[-1]
            anns, pos = _split_annotations(d_seg, s_seg)
            anns = [a for a in anns if _ann_name(a) not in _STANDALONE_ANNOTATIONS]
            if pos >= len(s_seg) or not s_seg[pos:].strip():
                # Annotation-only line — decorates the NEXT declaration.
                if anns and container.captures:
                    if not pending_attrs:
                        pending_line = L.start_line
                        pending_byte = L.start_byte
                        pending_indent = L.indent
                    pending_attrs.extend(anns)
                continue

            head = _parse_head(d_seg, s_seg, pos, inside_class=container.decl is not None)
            load_hits = len(_LOAD_RE.findall(s_seg))

            if head is None or not container.captures:
                clear_pending()
                conditional += load_hits
                continue

            # `extends` inside an inner-class body (Godot 3 layout) —
            # fold into the enclosing class instead of a new node.
            if head.native == "extends" and container.decl is not None:
                cls = container.decl
                if not cls.bases:
                    cls.bases = [head.extends_target]
                    if " extends " not in cls.signature:
                        cls.signature += f" extends {head.extends_target}"
                clear_pending()
                continue

            attrs = pending_attrs + anns
            if pending_attrs:
                start_line, start_byte = pending_line, pending_byte
            else:
                start_line, start_byte = L.start_line, L.start_byte
            decl_docs, doc_byte = _collect_docs(docs, start_line)

            decl = Declaration(
                kind=head.kind,
                name=head.name,
                signature=head.signature,
                bases=head.bases,
                attrs=attrs,
                docs=decl_docs,
                docs_inside=False,
                visibility=_visibility(head.name, head.kind),
                native_kind=head.native,
                start_line=start_line,
                # When annotations precede the declaration (`@export` /
                # `@rpc(...)` on their own line), `start_line` is pulled up
                # to the first annotation so `show` prints them. The name
                # token stays on the declaration's own line (`L.start_line`);
                # grep's def-classifier compares against `name_line`, so an
                # annotated declaration is tagged [def] on its real line,
                # not on an annotation. Equals `start_line` when unannotated.
                name_line=L.start_line,
                end_line=L.end_line,
                start_byte=start_byte,
                end_byte=L.end_byte,
                doc_start_byte=doc_byte if decl_docs else start_byte,
                children=head.children,
            )
            for child in head.children:
                child.start_line, child.end_line = decl.start_line, decl.end_line
                child.start_byte, child.end_byte = decl.start_byte, decl.end_byte
                child.doc_start_byte = decl.start_byte
            clear_pending()
            container.children.append(decl)

            if load_hits and head.is_value_decl:
                imports.append(_collapse_ws(d_seg))
                import_regions.append((L.start_byte, L.end_byte))
            elif head.native == "extends" and head.extends_target.startswith(
                ('"', "'")
            ):
                imports.append(_collapse_ws(d_seg))
                import_regions.append((L.start_byte, L.end_byte))
            elif load_hits:
                # e.g. a preload in a default argument — a real dependency,
                # but not a load-on-parse import statement.
                conditional += load_hits

            if head.push:
                stack.append(_Scope(L.indent, decl, head.captures, decl.children))
        prev = L

    while len(stack) > 1:
        close(stack.pop(), prev)

    _merge_script_header(top)
    return top, imports, import_regions, conditional


def _parse_head(d: str, s: str, pos: int, *, inside_class: bool) -> Optional[_Head]:
    """Parse one statement (annotations already consumed at ``pos``)."""
    mods, pos = _split_modifiers(d, s, pos)

    m = _FUNC_RE.match(s, pos)
    if m:
        name = m.group(1)
        colon = _find_depth0(s, pos, ":")
        if colon == -1:
            sig_end, push = len(s), False  # @abstract — bodyless by design
        else:
            sig_end = colon
            push = not s[colon + 1 :].strip()
        if name == "_init":
            kind = KIND_CTOR
        elif inside_class:
            kind = KIND_METHOD
        else:
            kind = KIND_FUNCTION
        return _Head(
            kind=kind,
            name=name,
            signature=_collapse_ws(d[pos:sig_end] if not mods else " ".join(mods) + " " + d[pos:sig_end]),
            native="func",
            push=push,
        )

    m = _CLASS_NAME_RE.match(s, pos)
    if m:
        name = m.group(1)
        bases: list[str] = []
        mext = _EXTENDS_INLINE_RE.search(s, m.end())
        if mext:
            end = _find_depth0(s, mext.end(), ",")
            if end == -1:
                end = len(s)
            base = _collapse_ws(d[mext.end() : end])
            if base:
                bases = [base]
        return _Head(
            kind=KIND_CLASS,
            name=name,
            signature=_collapse_ws(d[pos:]),
            native="class_name",
            bases=bases,
        )

    m = _CLASS_RE.match(s, pos)
    if m:
        colon = _find_depth0(s, pos, ":")
        header_end = colon if colon != -1 else len(s)
        bases = []
        mext = _EXTENDS_INLINE_RE.search(s, m.end(), header_end)
        if mext:
            base = _collapse_ws(d[mext.end() : header_end])
            if base:
                bases = [base]
        return _Head(
            kind=KIND_CLASS,
            name=m.group(1),
            signature=_collapse_ws(d[pos:header_end]),
            native="class",
            bases=bases,
            push=colon != -1 and not s[colon + 1 :].strip(),
            captures=True,
        )

    m = _EXTENDS_RE.match(s, pos)
    if m:
        target = _collapse_ws(d[m.start(1) :])
        return _Head(
            kind=KIND_CLASS,
            name=target.strip("\"'"),
            signature=_collapse_ws(d[pos:]),
            native="extends",
            extends_target=target,
        )

    m = _SIGNAL_RE.match(s, pos)
    if m:
        return _Head(
            kind=KIND_EVENT,
            name=m.group(1),
            signature=_collapse_ws(d[pos:]),
            native="signal",
        )

    m = _ENUM_RE.match(s, pos)
    if m:
        name = m.group(1) or "<anonymous>"
        open_idx = s.index("{", m.end() - 1)
        close_idx = _match_bracket(s, open_idx)
        members = _enum_members(d, s, open_idx + 1, close_idx)
        return _Head(
            kind=KIND_ENUM,
            name=name,
            signature=f"enum {name}" if m.group(1) else "enum",
            native="enum",
            children=members,
        )

    m = _CONST_RE.match(s, pos)
    if m:
        name = m.group(1)
        type_str, _ = _extract_type(d, s, m.end())
        if _LOAD_RE.search(s, pos):
            # Keep the full value — the preload path IS the const's payload.
            sig = _collapse_ws(d[pos:])
        else:
            sig = f"const {name}: {type_str}" if type_str else f"const {name}"
        return _Head(
            kind=KIND_FIELD,
            name=name,
            signature=sig,
            native="const",
            is_value_decl=True,
        )

    m = _VAR_RE.match(s, pos)
    if m:
        name = m.group(1)
        type_str, stop = _extract_type(d, s, m.end())
        is_property = False
        # Inline reference form after the type colon: `var x: int: get = _g`.
        if stop != -1 and _GETSET_REF_RE.match(s, _skip_ws(s, stop + 1)):
            is_property = True
        # Reference form without a type: `var x: get = _g` or
        # `var x = 0: get = _g, set = _s` — the marker is the first
        # depth-0 colon whose tail starts with `get =` / `set =`.
        ref_colon = _find_depth0(s, m.end(), ":")
        if ref_colon != -1 and _GETSET_REF_RE.match(s, _skip_ws(s, ref_colon + 1)):
            is_property = True
        if _SETGET_RE.search(s, m.end()):
            is_property = True  # Godot 3 legacy property
        block = s.rstrip().endswith(":")
        lambda_init = _has_depth0_func(s, m.end())
        if block and not lambda_init:
            is_property = True
        if _LOAD_RE.search(s, pos):
            sig = _collapse_ws(d[pos:]).rstrip(":").rstrip()
        else:
            sig = f"var {name}: {type_str}" if type_str else f"var {name}"
        if mods:
            sig = " ".join(mods) + " " + sig
        return _Head(
            kind=KIND_PROPERTY if is_property else KIND_FIELD,
            name=name,
            signature=sig,
            native="var",
            push=block,  # property body OR a block lambda initializer
            is_value_decl=True,
        )

    return None


# --- Statement-level helpers ------------------------------------------------


def _split_annotations(d: str, s: str) -> tuple[list[str], int]:
    """Consume leading ``@name`` / ``@name(args)`` tokens; return their
    display texts and the position where the statement proper begins."""
    out: list[str] = []
    i = 0
    n = len(s)
    while True:
        i = _skip_ws(s, i)
        m = _ANNOTATION_RE.match(s, i)
        if not m:
            return out, i
        j = m.end()
        # Godot tokenizes, so whitespace between the annotation name
        # and its argument parens is legal: `@export_enum ("A", "B")`
        # ships in real projects (material-maker).
        k = _skip_ws(s, j)
        if k < n and s[k] == "(":
            j = _match_bracket(s, k) + 1
        out.append(_collapse_ws(d[i:j]))
        i = j


def _ann_name(text: str) -> str:
    m = _ANNOTATION_RE.match(text)
    return m.group(1) if m else ""


def _split_modifiers(d: str, s: str, pos: int) -> tuple[list[str], int]:
    """Consume leading declaration modifiers (``static``, Godot 3
    ``export(...)`` / ``onready`` / rpc keywords)."""
    mods: list[str] = []
    while True:
        i = _skip_ws(s, pos)
        m = _MOD_RE.match(s, i)
        if not m:
            return mods, _skip_ws(s, pos)
        j = m.end()
        if m.group(1) == "export" and j < len(s) and _skip_ws(s, j) < len(s) and s[_skip_ws(s, j)] == "(":
            j = _match_bracket(s, _skip_ws(s, j)) + 1
        mods.append(_collapse_ws(d[i:j]))
        pos = j


def _enum_members(d: str, s: str, start: int, end: int) -> list[Declaration]:
    """Split ``{ A, B = 2, C }`` content on depth-0 commas into
    KIND_ENUM_MEMBER declarations. Line/byte ranges are filled in by
    the caller (they inherit the enum's)."""
    out: list[Declaration] = []
    depth = 0
    seg_start = start
    for i in range(start, end + 1):
        ch = s[i] if i < end else ","  # virtual trailing comma flushes the last entry
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            entry_d = d[seg_start:i].strip()
            seg_start = i + 1
            if not entry_d:
                continue
            m = _IDENT_RE.match(entry_d)
            if not m:
                continue
            out.append(
                Declaration(
                    kind=KIND_ENUM_MEMBER,
                    name=m.group(0),
                    signature=_collapse_ws(entry_d),
                )
            )
    return out


def _extract_type(d: str, s: str, pos: int) -> tuple[Optional[str], int]:
    """Read an optional ``: Type`` after a const/var name.

    Returns ``(type_text, stop)`` where ``stop`` is the index of the
    depth-0 ``:`` that ended the type (the property-body / property-ref
    colon), or ``-1`` when the type ended at ``=`` / end-of-statement.
    ``:=`` (inferred) and the bare ``: get = ...`` reference form yield
    no type.
    """
    i = _skip_ws(s, pos)
    if i >= len(s) or s[i] != ":":
        return None, -1
    if i + 1 < len(s) and s[i + 1] == "=":
        return None, -1  # := inferred assignment
    i = _skip_ws(s, i + 1)
    if _GETSET_REF_RE.match(s, i):
        return None, -1
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "=:":
            break
        j += 1
    type_text = _collapse_ws(d[i:j])
    # `setget` legacy may trail the type without an `=`
    type_text = re.sub(r"\bsetget\b.*$", "", type_text).strip()
    stop = j if j < len(s) and s[j] == ":" else -1
    return (type_text or None), stop


def _has_depth0_func(s: str, pos: int) -> bool:
    """True when a ``func`` token appears at bracket depth 0 after
    ``pos`` — i.e. the statement's trailing colon opens a *lambda*
    body, not a property body."""
    depth = 0
    for m in _FUNC_OR_BRACKET_RE.finditer(s, pos):
        t = m.group(0)
        if t in "([{":
            depth += 1
        elif t in ")]}":
            depth -= 1
        elif depth == 0:
            return True
    return False


def _split_semicolons(L: _Logical) -> list[tuple[str, str]]:
    """GDScript allows ``;`` as a statement separator — rare, but cheap
    to honor since the shadow makes depth-0 detection trivial."""
    if ";" not in L.shadow:
        return [(L.display, L.shadow)]
    out: list[tuple[str, str]] = []
    depth = 0
    start = 0
    s = L.shadow
    for i, ch in enumerate(s + ";"):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            seg_d = L.display[start:i]
            seg_s = s[start:i]
            a = len(seg_d) - len(seg_d.lstrip())
            b = len(seg_d.rstrip())
            if b > a:
                out.append((seg_d[a:b], seg_s[a:b]))
            start = i + 1
    return out


def _collect_docs(
    docs: dict[int, tuple[str, int]], first_line: int
) -> tuple[list[str], int]:
    """Contiguous ``##`` lines ending directly above ``first_line``."""
    out: list[str] = []
    byte = 0
    ln = first_line - 1
    while ln in docs:
        text, byte = docs[ln]
        out.append(text)
        ln -= 1
    out.reverse()
    return out, byte


def _merge_script_header(top: list[Declaration]) -> None:
    """Fuse the script's ``class_name`` and ``extends`` statements into
    one KIND_CLASS declaration — together they are the header of the
    file's implicit class, and two type nodes for one script would
    double-count it."""
    cn = next((t for t in top if t.native_kind == "class_name"), None)
    ext = next((t for t in top if t.native_kind == "extends"), None)
    if cn is None or ext is None:
        return
    if not cn.bases:
        cn.bases = [ext.name]
        if " extends " not in cn.signature:
            cn.signature += f" extends {ext.name}"
    first, second = (cn, ext) if cn.start_line <= ext.start_line else (ext, cn)
    cn.start_line, cn.end_line = first.start_line, second.end_line
    cn.start_byte, cn.end_byte = first.start_byte, second.end_byte
    cn.attrs = first.attrs + second.attrs
    # `if x.docs` (not `or`) — a doc block at byte 0 is a real offset,
    # and offset 0 must not read as "no docs".
    doc_bytes = [x.doc_start_byte for x in (cn, ext) if x.docs]
    cn.docs = cn.docs or ext.docs
    cn.doc_start_byte = min(doc_bytes) if doc_bytes else cn.start_byte
    top.remove(ext)


def _visibility(name: str, kind: str) -> str:
    if not name.startswith("_"):
        return ""
    if name in _ENGINE_CALLBACKS:
        return ""  # engine-called virtual — the script's public surface
    return "private"


# --- Low-level text helpers -------------------------------------------------


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _find_depth0(s: str, pos: int, target: str) -> int:
    depth = 0
    for i in range(pos, len(s)):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == target and depth == 0:
            return i
    return -1


def _match_bracket(s: str, open_idx: int) -> int:
    """Index of the bracket closing ``s[open_idx]``; ``len(s)`` when
    unbalanced (possible only in error-clamped lines)."""
    depth = 0
    for i in range(open_idx, len(s)):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return len(s)


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())
