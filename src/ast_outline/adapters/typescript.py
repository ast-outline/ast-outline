"""TypeScript / JavaScript adapter (handles .ts, .tsx, .js, .jsx, .mjs, .cjs).

Design notes (how JS/TS concepts map to the IR):
- `class_declaration` / `abstract_class_declaration`  → KIND_CLASS
- `interface_declaration`                             → KIND_INTERFACE
- `enum_declaration`                                  → KIND_ENUM
- `type_alias_declaration` (`type Foo = ...`)         → KIND_FIELD
- `function_declaration`                              → KIND_FUNCTION (top-level)
- `method_definition`                                 → KIND_METHOD, or KIND_CTOR for `constructor`
- `lexical_declaration` (`const`/`let`) with arrow /
  function expression on the right                    → KIND_FUNCTION
- `lexical_declaration` with a structural callback-DSL
  call on the right (`const s = defineStore('id',
  () => {...})`)                                      → KIND_BLOCK
- `lexical_declaration` with any other RHS            → KIND_FIELD
- `expression_statement` wrapping a structural
  callback-DSL call (`describe('...', () => {...})`,
  `it('...', () => {...})`)                           → KIND_BLOCK
- `public_field_definition` (class body)              → KIND_FIELD
- `property_signature` (interface body)               → KIND_FIELD
- `method_signature` (interface body)                 → KIND_METHOD
- `enum_assignment` / `property_identifier` (in enum) → KIND_ENUM_MEMBER
- `export_statement`                                  → transparent wrapper;
  unwrap the inner declaration and widen its byte range to include `export`

Callback-DSL blocks (the KIND_BLOCK rule)
-----------------------------------------
Modern TS/JS expresses a lot of structure through *function calls that
take a callback*, not through language-level declarations: test suites
(`describe`/`it`/`test`), Pinia setup-stores (`defineStore`), and any
in-house DSL of the same shape. tree-sitter parses these as ordinary
`call_expression` nodes, so a declaration-only walk misses them
entirely (empty outline) or dumps the whole callback body into one
signature line (garbage outline). See `_structural_call`.

The rule is structural, NOT a hard-coded list of library names — a
call is a block when (1) its callee is a plain identifier, (2) its
last argument is a function literal, and (3) its FIRST argument is a
string-literal label. Clause 3 is the discriminator between a *named
container* (`describe('suite', fn)`, `it('case', fn)`,
`defineStore('id', fn)` — the label IS the block's reason to exist)
and a *bare function wrapper* (`action(fn)`, `memoize(fn)`,
`tds(fn)` — the callback is just an implementation, its locals are
not an API surface). Requiring the label *first* also rejects
property-definition wrappers that take a string in a later slot, e.g.
`defineGetter(obj, 'name', fn)`. This catches vitest/jest/mocha,
`defineStore`, and unknown future DSLs of the same
`name(label, callback)` shape alike, and rejects noise like
`setTimeout(fn, 1000)` / `useEffect(fn, deps)` / `el.on('x', fn)` /
`action(fn)`.

Known structural blind spots (a pure shape rule cannot avoid these):
- `QUnit.test('...', fn)` / Playwright `test.describe(...)` — a
  member-expression callee, excluded by clause 1 the same way `.map` /
  `.forEach` / `.on` / `el.addEventListener` are. Bare-global test
  styles (mocha/jest/vitest/jasmine/ava/tape/node:test) are unaffected.
- A bare `addEventListener('load', fn)` (no `window.` / `document.`
  receiver) is structurally identical to `it('load', fn)` and is
  promoted to a block. Rare in practice; harmless when it happens.

Visibility:
- `accessibility_modifier` values: public / protected / private
- `#name` → private (TS 4.3+ true-private)
- Top-level types: "public" (TS has no `internal`)
- Class members without a modifier → "public" (unlike C#)

Docs: preceding `comment` siblings (JSDoc `/** ... */` or `//` lines) are
captured as docs and rendered before the signature (docs_inside=False),
matching TypeScript/JS convention. A leading run of `//` comments that
is actually *disabled code* (a commented-out block, not prose) is
dropped — see `_is_commented_out_code`; without this the outline would
render the disabled code as if it documented the next declaration.

Grammars:
- `.tsx` / `.jsx` use the TSX grammar (JSX-aware).
- `.ts` / `.js` / `.mjs` / `.cjs` use the TypeScript grammar (accepts plain
  JS as a subset; may reject angle-bracket type assertions mixed with JSX,
  but those shouldn't appear in non-.tsx files).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_CTOR,
    KIND_ENUM,
    KIND_ENUM_MEMBER,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    Declaration,
    ParseResult,
)


_LANG_TS = Language(tsts.language_typescript())
_LANG_TSX = Language(tsts.language_tsx())
_PARSER_TS = Parser(_LANG_TS)
_PARSER_TSX = Parser(_LANG_TSX)

_TSX_EXTS = {".tsx", ".jsx"}


class TypeScriptAdapter:
    language_name = "typescript"
    display_name = "TypeScript/JavaScript"
    extensions = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    definition_keywords = frozenset({
        "class", "interface", "type", "enum", "function", "namespace",
    })

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        parser = _PARSER_TSX if path.suffix.lower() in _TSX_EXTS else _PARSER_TS
        tree = parser.parse(src)
        decls: list[Declaration] = []
        _walk_module(tree.root_node, src, decls)
        imports: list[str] = []
        # Piggyback byte-range collection for grep's import classifier.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        import_regions.sort()
        # Count dynamic `import('...')` calls that live inside a
        # function / method / control-flow scope — i.e. NOT module-
        # level. Reported as `conditional_imports_count` so renderers
        # can append `[+ N conditional includes]` to the imports line
        # and the agent isn't misled into thinking the file's deps end
        # at its static `import` statements. Top-level dynamic imports
        # (e.g. `const x = await import('./a')` at module scope) execute
        # unconditionally on module load, so they're not counted — the
        # same way PHP skips top-level `$x = require 'a';`.
        conditional_count = _count_conditional_imports(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_count,
            import_regions=import_regions,
        )


# --- Imports --------------------------------------------------------------
#
# TS/JS `import` statements are top-level only (ESM rule). We collect the
# source text of every `import_statement` at module-scope verbatim,
# collapse internal whitespace (multi-line `import { X,\n Y } from ...`
# → one line), and strip the trailing semicolon. Source-true output is
# what any LLM agent already knows how to read; no synthetic format.
#
# Not listed in `--imports` (but counted, see below):
# - `import('...')` dynamic expressions inside functions / methods /
#   control-flow blocks. Runtime, not declarative — listing them as
#   static imports would mislead an agent into thinking they always
#   load. We *count* them in `conditional_imports_count` instead, so
#   the renderer can emit `[+ N conditional includes]` next to the
#   imports line. Top-level dynamic imports (rare; `const x = await
#   import('./a')` at module scope) execute unconditionally on module
#   load and are not counted, matching PHP's rule for top-level
#   assignment-wrapped includes.
#
# Out of scope entirely:
# - `require(...)` calls in .js/.cjs — a runtime function with no
#   dedicated AST node (just a `call_expression` whose callee is the
#   identifier "require"); pattern-matching by name is fragile and
#   noisy, so we neither list nor count these.
# - `export ... from '...'` re-exports — separate concern, would need a
#   sibling `--exports` flag.


def _collect_imports(
    root: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    """Walk top-level children once. Emits normalized import strings to
    ``out``; if ``regions`` is supplied, also collects each statement's
    byte range — piggybacked to avoid a second tree walk for grep's
    classifier (see Python adapter's ``_collect_imports`` for full
    rationale)."""
    for child in root.named_children:
        if child.type == "import_statement":
            text = _collapse_ws(_text(child, src)).rstrip(";").strip()
            if text:
                out.append(text)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))


# AST node types that take a dynamic `import(...)` out of "module-level
# unconditional" status. Mirrors PHP's `_CONDITIONAL_OR_RUNTIME_SCOPES`
# semantics: once the walk enters any of these on the parent chain,
# every dynamic import below counts no matter how deep nested.
# Sub-clauses like `else_clause`, `catch_clause`, `finally_clause`,
# `switch_case`, `switch_default` are all reachable only via their
# parent statement (which is already in this set), so listing the
# parent is enough.
_TS_CONDITIONAL_OR_RUNTIME_SCOPES = frozenset({
    # Function-like scopes (any import inside is per-call, not per-load)
    "function_declaration",
    "function_expression",
    "arrow_function",
    "generator_function_declaration",
    "generator_function",
    "method_definition",
    # Class body — instance field initializers run per-construction;
    # `static` field initializers run at class-evaluation time (which
    # for a top-level class IS module load), but they're class-scoped
    # not module-scoped. We count both conservatively rather than try
    # to distinguish — a `static` field still represents a dependency
    # the agent should know about even though it loads unconditionally.
    "class_body",
    # Control flow. `for_in_statement` covers BOTH `for..in` and
    # `for..of` — tree-sitter-typescript reuses one node type for both.
    "if_statement",
    "switch_statement",
    "try_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
})


def _count_conditional_imports(root: Node) -> int:
    """Count dynamic `import('...')` call expressions that live inside
    a function / method / class / control-flow scope.

    A dynamic import in tree-sitter-typescript is a `call_expression`
    whose `function` field is the special `import` node (not an
    `identifier` named "import" — the grammar emits a dedicated keyword
    node). This is the same shape for plain `import('./x')`,
    `await import('./x')`, and `import('./x').then(...)`.

    Iterative `(node, in_scope)` walk so deeply nested .ts files stay
    well within Python recursion limits.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "import":
                if in_scope:
                    count += 1
                # The argument list of an `import(...)` is a path
                # expression, not another import — skip its subtree.
                continue
        new_in_scope = in_scope or node.type in _TS_CONDITIONAL_OR_RUNTIME_SCOPES
        for c in node.children:
            stack.append((c, new_in_scope))
    return count


# --- Walk -----------------------------------------------------------------


def _walk_module(root: Node, src: bytes, out: list[Declaration]) -> None:
    for child in root.named_children:
        decl = _node_to_decl(child, src, inside_class=False, inside_interface=False)
        if decl is not None:
            out.append(decl)


def _node_to_decl(
    node: Node,
    src: bytes,
    *,
    inside_class: bool,
    inside_interface: bool,
) -> Optional[Declaration]:
    kind = node.type

    # `export ...` / `export default ...` — unwrap, then widen byte range
    # to include the `export` keyword so `show` prints it too.
    if kind in ("export_statement",):
        # TS puts class-level decorators as siblings of `class_declaration`
        # inside `export_statement`, not as children of the class itself.
        # Collect them here so we can hand them to the inner decl.
        export_decorators = [
            _collapse_ws(_text(c, src)) for c in node.named_children if c.type == "decorator"
        ]
        for inner in node.named_children:
            if inner.type in _HANDLED_TOP_LEVEL:
                decl = _node_to_decl(
                    inner,
                    src,
                    inside_class=inside_class,
                    inside_interface=inside_interface,
                )
                if decl is not None:
                    decl.start_byte = node.start_byte
                    decl.start_line = node.start_point[0] + 1
                    decl.doc_start_byte = _leading_doc_start_byte(node, src) or node.start_byte
                    decl.docs = _collect_docs(node, src)
                    if export_decorators:
                        decl.attrs = export_decorators + decl.attrs
                    # Recompute the signature from the widened range so the
                    # `export` / `export default` prefix shows up.
                    decl.signature = _signature_from_range(node, src, inner)
                    return decl
        return None

    if kind == "class_declaration" or kind == "abstract_class_declaration":
        return _class_to_decl(node, src)
    if kind == "interface_declaration":
        return _interface_to_decl(node, src)
    if kind == "enum_declaration":
        return _enum_to_decl(node, src)
    if kind == "type_alias_declaration":
        return _type_alias_to_decl(node, src)
    if kind == "function_declaration":
        return _function_to_decl(node, src, inside_class=False)

    # `describe('...', () => {...})` / `it('...', () => {...})` and other
    # callback-DSL blocks — a bare call statement whose trailing argument
    # is a function literal carrying structure. See `_structural_call`.
    # The block is named after its string label (`Testing: initial
    # state`), not the DSL keyword (`describe`) — the label is what a
    # reader, `digest`, and `show` actually want to reference.
    if kind == "expression_statement":
        call = _structural_call(node)
        if call is not None:
            return _call_block_to_decl(
                call,
                src,
                name=_label_text(call, src),
                start_byte=node.start_byte,
                start_line=node.start_point[0] + 1,
            )
        return None

    # `const foo = ...` / `let foo = ...` — one or more variable_declarators.
    # If the RHS is an arrow / function expression, treat as KIND_FUNCTION;
    # otherwise, KIND_FIELD.
    if kind in ("lexical_declaration", "variable_declaration"):
        return _lexical_to_decl(node, src)

    # Inside a class body
    if kind == "method_definition":
        return _method_to_decl(node, src)
    if kind == "public_field_definition":
        return _class_field_to_decl(node, src)

    # Inside an interface body
    if kind == "property_signature":
        return _property_signature_to_decl(node, src)
    if kind in ("method_signature", "construct_signature", "call_signature"):
        return _method_signature_to_decl(node, src)
    if kind == "index_signature":
        return None  # skip — rarely useful in an outline

    # Enum members
    if kind in ("property_identifier", "enum_assignment"):
        return _enum_member_to_decl(node, src)

    return None


# Top-level nodes we unwrap from `export_statement`.
# `expression_statement` is intentionally absent — `export <bare call>`
# is not valid JS/TS, so an exported callback-DSL block can only reach
# us as `export const x = describe(...)` (a `lexical_declaration`, which
# IS here). A standalone `export describe(...)` cannot exist.
_HANDLED_TOP_LEVEL = {
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "type_alias_declaration",
    "function_declaration",
    "lexical_declaration",
    "variable_declaration",
}


# --- Callback-DSL blocks --------------------------------------------------
#
# See the module docstring ("Callback-DSL blocks") for the rationale. The
# rule is intentionally structural — no hard-coded list of `describe` /
# `it` / `defineStore` names — so it survives new test frameworks and new
# DSL libraries without code changes, and applies the same way to an
# in-house DSL nobody outside one company has ever seen.

# Function-literal argument node types — the "callback" of a call.
_CALLBACK_NODE_TYPES = frozenset(
    {"arrow_function", "function_expression", "function"}
)

# Argument node types that count as a string-literal label. Plain and
# template strings both qualify — test names are sometimes written as
# template literals (`it(`handles ${x}`, fn)`).
_LABEL_ARG_TYPES = frozenset({"string", "template_string"})


def _structural_call(node: Node) -> Optional[Node]:
    """Return the `call_expression` if `node` is — or wraps — a call that
    qualifies as a structural callback-DSL block, else None.

    Accepts either a bare `call_expression` or an `expression_statement`
    wrapping one. A call qualifies on *shape* only:

    1. callee is a plain `identifier` — rejects `el.addEventListener`,
       `arr.map`, `promise.then`, `app.get` (member-expression callees);
    2. the LAST argument is a function literal — rejects `setTimeout(fn,
       1000)` (callback first), `useEffect(fn, deps)` (array last),
       `console.log('x')` (no callback at all).

    Whether the call actually carries structure (vs. an empty / noise
    callback) is decided later by `_call_block_to_decl`, which needs to
    descend the body to know — this function is the cheap pre-filter.
    """
    if node.type == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is None:
            return None
        node = inner
    if node.type != "call_expression":
        return None
    callee = node.child_by_field_name("function")
    if callee is None or callee.type != "identifier":
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    named = args.named_children
    if not named or named[-1].type not in _CALLBACK_NODE_TYPES:
        return None
    return node


def _has_label_arg(call: Node) -> bool:
    """True when `call`'s FIRST argument is a string-literal label.

    This is the second half of the block test — on top of
    `_structural_call`'s shape match it is what promotes a call from a
    bare function wrapper (`action(fn)`) to a named container
    (`describe('suite', fn)`).

    The label must be the *first* argument. Every BDD / store DSL puts
    the name first — `describe('suite', fn)`, `it('case', fn)`,
    `test('case', fn)`, `defineStore('id', fn)`. Requiring first
    position (not merely "a string somewhere in the args") rejects
    wrappers that take a string in a later slot, e.g. Express's
    `defineGetter(obj, 'protocol', fn)` — `obj` is first, so it is a
    property-definition wrapper, not a named container.

    Kept as a standalone helper so every site that decides "is this a
    block" — `_call_block_to_decl` and `_signature_from_range` — applies
    the identical rule.
    """
    args = call.child_by_field_name("arguments")
    named = args.named_children if args is not None else []
    return bool(named) and named[0].type in _LABEL_ARG_TYPES


def _label_text(call: Node, src: bytes) -> str:
    """The first argument's string-literal text with the surrounding
    quotes stripped — the human-readable name of a bare-call block
    (`describe('Testing: initial state', fn)` → `Testing: initial
    state`). This is what `digest` shows and what `show` / `find_symbols`
    match on, so it must be the label, not the DSL keyword (`describe`).

    Returns `"?"` if the first argument is not a string — unreachable
    for a real block, since `_has_label_arg` already gated it.
    """
    args = call.child_by_field_name("arguments")
    named = args.named_children if args is not None else []
    if named and named[0].type in _LABEL_ARG_TYPES:
        txt = _collapse_ws(_text(named[0], src))
        if len(txt) >= 2 and txt[0] in "'\"`" and txt[-1] == txt[0]:
            txt = txt[1:-1]
        return txt or "?"
    return "?"


def _block_signature(start_byte: int, call: Node, src: bytes) -> str:
    """Signature for a KIND_BLOCK — `<prefix>(<label>)`, with the callback
    body and any later arguments dropped.

    `describe('Testing: initial state', () => {...})` → ``describe('Testing:
    initial state')``; `const s = defineStore('id', () => {...})` →
    ``const s = defineStore('id')`` (when `start_byte` is the lexical
    declaration's start).

    Built from two parts — the prefix slice up to the label, then the
    label verbatim — rather than slicing-and-collapsing the whole head.
    A whole-head collapse turns a multi-line `describe(\\n  'name',` into
    `describe( 'name'` (spurious space after `(`), and a blunt `( `→`(`
    cleanup would also corrupt a `( ` that lives inside the label string.
    """
    args = call.child_by_field_name("arguments")
    named = args.named_children if args is not None else []
    if not named:  # defensive — a real block always has a label arg
        return _collapse_ws(
            src[start_byte:call.end_byte].decode("utf8", errors="replace")
        )
    # `_has_label_arg` guarantees the first argument is the string label.
    label = named[0]
    prefix = _collapse_ws(
        src[start_byte:label.start_byte].decode("utf8", errors="replace")
    )
    sig = prefix + _collapse_ws(_text(label, src)) + ")"
    if len(sig) > 140:
        sig = sig[:137] + "..."
    return sig


def _call_block_to_decl(
    call: Node,
    src: bytes,
    *,
    name: str,
    start_byte: int,
    start_line: int,
) -> Optional[Declaration]:
    """Build a KIND_BLOCK declaration from a structural call, or return
    None when the call is not a named container after all.

    A call passes `_structural_call`'s shape pre-filter but is only a
    real block when it ALSO carries a string-literal label argument —
    the `'test name'` of `describe`/`it`, the `'id'` of a store. That
    label is what separates a named container from a bare function
    wrapper like `action(fn)` / `memoize(fn)` / `tds(fn)`, whose
    callback is an implementation, not a group of declarations: those
    are dropped (None) and the caller treats them as ordinary code (a
    plain field for `const x = action(fn)`, nothing for a bare
    `tds(fn)` statement).

    The label check happens before the body descent, so a non-block
    call is rejected without walking its callback at all.
    """
    if not _has_label_arg(call):
        return None

    args = call.child_by_field_name("arguments")
    named = args.named_children if args is not None else []
    children: list[Declaration] = []
    for a in named:
        if a.type not in _CALLBACK_NODE_TYPES:
            continue
        body = a.child_by_field_name("body")
        if body is None or body.type != "statement_block":
            continue
        for c in body.named_children:
            d = _node_to_decl(
                c, src, inside_class=False, inside_interface=False
            )
            if d is not None:
                children.append(d)

    return Declaration(
        kind=KIND_BLOCK,
        name=name,
        signature=_block_signature(start_byte, call, src),
        visibility=_visibility_for_name(name),
        start_line=start_line,
        end_line=call.end_point[0] + 1,
        start_byte=start_byte,
        end_byte=call.end_byte,
        doc_start_byte=start_byte,
        children=children,
    )


# --- Type / class / interface / enum --------------------------------------


def _class_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    bases = _class_bases(node, src)
    attrs = _decorators(node, src)
    docs = _collect_docs(node, src)
    visibility = "public"

    signature = _class_signature(node, src)

    body = node.child_by_field_name("body")
    children: list[Declaration] = []
    if body is not None:
        for c in body.named_children:
            d = _node_to_decl(c, src, inside_class=True, inside_interface=False)
            if d is not None:
                children.append(d)

    return Declaration(
        kind=KIND_CLASS,
        name=name,
        signature=signature,
        bases=bases,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
        children=children,
    )


def _interface_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    bases = _interface_bases(node, src)
    docs = _collect_docs(node, src)
    body = node.child_by_field_name("body")
    children: list[Declaration] = []
    if body is not None:
        for c in body.named_children:
            d = _node_to_decl(c, src, inside_class=False, inside_interface=True)
            if d is not None:
                children.append(d)

    return Declaration(
        kind=KIND_INTERFACE,
        name=name,
        signature=_head_text(node, src, body),
        bases=bases,
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
        children=children,
    )


def _enum_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    docs = _collect_docs(node, src)
    body = node.child_by_field_name("body")
    children: list[Declaration] = []
    if body is not None:
        for c in body.named_children:
            d = _node_to_decl(c, src, inside_class=False, inside_interface=False)
            if d is not None:
                children.append(d)
    return Declaration(
        kind=KIND_ENUM,
        name=name,
        signature=_head_text(node, src, body),
        docs=docs,
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
        children=children,
    )


def _enum_member_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    # enum_assignment wraps `Foo = 1`; property_identifier is the bare `Foo`
    if node.type == "enum_assignment":
        name_node = node.child_by_field_name("name") or (
            node.named_children[0] if node.named_children else None
        )
        name = _text(name_node, src) if name_node is not None else None
    else:  # property_identifier
        name = _text(node, src)
    if not name:
        return None
    return Declaration(
        kind=KIND_ENUM_MEMBER,
        name=name,
        signature=_collapse_ws(_text(node, src)),
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


def _type_alias_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    sig = _collapse_ws(_text(node, src)).rstrip(";")
    return Declaration(
        kind=KIND_FIELD,  # no dedicated kind for type aliases; field is close enough
        name=name,
        signature=sig,
        docs=_collect_docs(node, src),
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


# --- Functions ------------------------------------------------------------


def _function_to_decl(node: Node, src: bytes, *, inside_class: bool) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    sig = _function_signature(node, src)
    docs = _collect_docs(node, src)

    return Declaration(
        kind=KIND_METHOD if inside_class else KIND_FUNCTION,
        name=name,
        signature=sig,
        docs=docs,
        visibility=_visibility_for_name(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


def _method_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    kind = KIND_CTOR if name == "constructor" else KIND_METHOD
    sig = _function_signature(node, src)
    docs = _collect_docs(node, src)
    # TS class members default to `public` when no modifier is given
    # (opposite of C#).
    visibility = (
        _visibility_from_modifiers(node, src)
        or _visibility_for_name(name)
        or "public"
    )
    attrs = _decorators(node, src)
    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        attrs=attrs,
        docs=docs,
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


def _method_signature_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Interface method signature (no body)."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    sig = _collapse_ws(_text(node, src)).rstrip(";")
    return Declaration(
        kind=KIND_METHOD,
        name=name,
        signature=sig,
        docs=_collect_docs(node, src),
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


# --- Fields / properties --------------------------------------------------


def _class_field_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    sig = _field_signature_text(node, src)
    visibility = (
        _visibility_from_modifiers(node, src)
        or _visibility_for_name(name)
        or "public"
    )
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        docs=_collect_docs(node, src),
        attrs=_decorators(node, src),
        visibility=visibility,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


def _property_signature_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    sig = _collapse_ws(_text(node, src)).rstrip(";,")
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        docs=_collect_docs(node, src),
        visibility="public",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Lexical declarations -------------------------------------------------


def _lexical_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """`const foo = ...` / `let foo = ...`.

    If the RHS is an arrow / function expression → KIND_FUNCTION.
    Otherwise → KIND_FIELD. Only the first variable_declarator is
    promoted to a declaration (the common case); multi-declarator
    assignments like `const a = 1, b = 2` still pick up `a`.
    """
    declarators = [c for c in node.named_children if c.type == "variable_declarator"]
    if not declarators:
        return None
    d = declarators[0]
    name_node = d.child_by_field_name("name")
    if name_node is None or name_node.type != "identifier":
        return None
    name = _text(name_node, src)
    value = d.child_by_field_name("value")
    docs = _collect_docs(node, src)

    # `const useStore = defineStore('id', () => {...})` — RHS is a
    # structural callback-DSL call. Emit as a KIND_BLOCK named after the
    # variable, with the callback body's declarations as children.
    if value is not None and value.type == "call_expression":
        call = _structural_call(value)
        if call is not None:
            block = _call_block_to_decl(
                call,
                src,
                name=name,
                start_byte=node.start_byte,
                start_line=node.start_point[0] + 1,
            )
            if block is not None:
                block.docs = docs
                block.doc_start_byte = (
                    _leading_doc_start_byte(node, src) or node.start_byte
                )
                return block

    if value is not None and value.type in ("arrow_function", "function_expression", "function"):
        sig = _arrow_signature(node, d, value, src)
        return Declaration(
            kind=KIND_FUNCTION,
            name=name,
            signature=sig,
            docs=docs,
            visibility=_visibility_for_name(name),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
        )

    # Plain field. `_elided_text` drops any embedded function/method
    # body so the signature stays a declaration, not a code dump (e.g.
    # `const inc = action((d) => {…})` rather than the truncated body).
    sig = _collapse_ws(_elided_text(node, src)).rstrip(";")
    if len(sig) > 140:
        sig = sig[:137] + "..."
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        docs=docs,
        visibility=_visibility_for_name(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=_leading_doc_start_byte(node, src) or node.start_byte,
    )


# --- Signature extraction -------------------------------------------------


def _function_signature(node: Node, src: bytes) -> str:
    """Text up to (but not including) the function body block."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_decorators(text)
    return _collapse_ws(text).rstrip(" {;").rstrip()


def _arrow_signature(lex_node: Node, declarator: Node, value: Node, src: bytes) -> str:
    """Signature for `const foo = (x): T => { ... }`.

    We emit the prefix (`const foo = ...`) up to the arrow body, so the
    signature reads naturally and the reader sees the name + parameters.
    """
    # Body of the arrow expression — slice everything before it.
    body = value.child_by_field_name("body")
    end = body.start_byte if body is not None else value.end_byte
    text = src[lex_node.start_byte:end].decode("utf8", errors="replace")
    text = _collapse_ws(text).rstrip(" {").rstrip()
    return text


def _field_signature_text(node: Node, src: bytes) -> str:
    """Class field signature — include type annotation, drop `= defaultValue`."""
    text = _text(node, src)
    # Cut at ` = ` to drop default-value assignment
    eq = text.find(" = ")
    if eq > 0:
        text = text[:eq]
    return _collapse_ws(text).rstrip(";")


def _class_signature(node: Node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    text = _strip_leading_decorators(text)
    return _collapse_ws(text).rstrip(" {").rstrip()


def _head_text(node: Node, src: bytes, body: Optional[Node]) -> str:
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte:end].decode("utf8", errors="replace")
    return _collapse_ws(text).rstrip(" {").rstrip()


def _strip_leading_decorators(text: str) -> str:
    s = text.lstrip()
    while s.startswith("@"):
        # Drop to end of this decorator line or until the next non-whitespace
        # line that doesn't start with @ — but the signature slice only has
        # one decorator line at most since decorators end in newlines.
        nl = s.find("\n")
        if nl == -1:
            break
        s = s[nl + 1:].lstrip()
    return s


# --- Bases / heritage ----------------------------------------------------


def _class_bases(node: Node, src: bytes) -> list[str]:
    """Collect both `extends X` and `implements Y, Z` into a flat bases list."""
    out: list[str] = []
    for child in node.children:
        if child.type == "class_heritage":
            for h in child.named_children:
                # extends_clause / implements_clause
                for inner in h.named_children:
                    t = _collapse_ws(_text(inner, src)).rstrip(",")
                    if t:
                        out.append(t)
    return out


def _interface_bases(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if child.type == "extends_type_clause":
            for inner in child.named_children:
                t = _collapse_ws(_text(inner, src)).rstrip(",")
                if t:
                    out.append(t)
    return out


# --- Modifiers / decorators / docs ---------------------------------------


def _decorators(node: Node, src: bytes) -> list[str]:
    """Collect decorator children of `node` AND decorator siblings that
    immediately precede it (tree-sitter-typescript places class-body
    decorators as siblings of the method, not children)."""
    out: list[str] = [
        _collapse_ws(_text(c, src)) for c in node.children if c.type == "decorator"
    ]
    preceding: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "decorator":
        preceding.append(_collapse_ws(_text(sib, src)))
        sib = sib.prev_sibling
    preceding.reverse()
    return preceding + out


def _signature_from_range(outer: Node, src: bytes, inner: Node) -> str:
    """Signature text when `inner` is a declaration nested inside `outer`
    (typically `export_statement` wrapping a class/function). Captures the
    full prefix (`export`, `export default`) up to the body of `inner`.
    """
    # `export const s = defineStore('id', () => {...})` — the inner is a
    # structural callback-DSL block. Slice up to the callback argument so
    # the `export` prefix shows but the callback body does NOT leak into
    # the signature (it would be the whole store implementation).
    # The full block test (shape AND label) must be applied here, not
    # just the shape pre-filter: `export const w = action(fn)` matches
    # the shape but is a plain field — emitting a `_block_signature` for
    # it would fabricate a `…)`-style signature on a non-block decl.
    if inner.type in ("lexical_declaration", "variable_declaration"):
        declarators = [
            c for c in inner.named_children if c.type == "variable_declarator"
        ]
        if declarators:
            value = declarators[0].child_by_field_name("value")
            if value is not None and value.type == "call_expression":
                call = _structural_call(value)
                if call is not None and _has_label_arg(call):
                    return _block_signature(outer.start_byte, call, src)

    body = inner.child_by_field_name("body")
    if body is not None:
        # Function / class inner — slice cleanly up to its body block.
        text = src[outer.start_byte:body.start_byte].decode("utf8", errors="replace")
        text = _strip_leading_decorators(text)
        return _collapse_ws(text).rstrip(" {;").rstrip()

    # A non-function / non-class inner (e.g. `export const x = <RHS>`)
    # has no `body` field. `_elided_text` drops any embedded executable
    # body so an `export const x = action((d) => {...})` renders as
    # `export const x = action((d) => {…})`, not a dumped body. Capped at
    # 140 the same way `_lexical_to_decl` caps a plain field.
    text = _strip_leading_decorators(_elided_text(outer, src))
    sig = _collapse_ws(text).rstrip(" {;").rstrip()
    if len(sig) > 140:
        sig = sig[:137] + "..."
    return sig


def _visibility_from_modifiers(node: Node, src: bytes) -> Optional[str]:
    """Look for an accessibility_modifier child (public/protected/private)."""
    for c in node.children:
        if c.type == "accessibility_modifier":
            t = _text(c, src).strip()
            if t in ("public", "protected", "private"):
                return t
    # TS 4.3+ private: `#name` prefix on the name itself
    name_node = node.child_by_field_name("name")
    if name_node is not None and name_node.type in ("private_property_identifier",):
        return "private"
    return None


def _visibility_for_name(name: str) -> str:
    # JS convention: a leading underscore signals "intended private".
    # Dunder names are library/framework-specific and aren't universally
    # public, so we don't treat them specially here.
    if name.startswith("_"):
        return "private"
    return ""


def _leading_comment_nodes(node: Node) -> list[Node]:
    """Contiguous `comment` siblings immediately preceding `node`, in
    source order (top-to-bottom)."""
    out: list[Node] = []
    sib = node.prev_sibling
    while sib is not None and sib.type == "comment":
        out.append(sib)
        sib = sib.prev_sibling
    out.reverse()
    return out


# A declaration keyword in leading position — a strong "this line is
# code" signal. Anchored at start so prose that merely mentions a word
# ("constant folding", "type theory") is not matched.
_CODE_DECL_RE = re.compile(
    r"^(export\s+)?(default\s+)?(async\s+)?"
    r"(const|let|var|function|class|interface|enum|type|import)\b"
)


def _looks_like_code_line(text: str) -> bool:
    """Heuristic: does one de-commented line look like source code rather
    than prose? Deliberately strict — only unambiguous structural shapes
    count, so a genuine doc line (even one that happens to start with a
    word like "if" or "for") is never misclassified."""
    s = text.strip()
    if not s:
        return False
    # Lines that are purely closing brackets — prose never starts here.
    if s[0] in "}])":
        return True
    # Statement / scope punctuation a prose sentence virtually never
    # ends on.
    if s.endswith(("{", ";", ",", "=>")):
        return True
    # A declaration keyword in leading position (`const x = ...`).
    if _CODE_DECL_RE.match(s):
        return True
    # An arrow function opening a body or paren-group (`=> {` / `=> (`).
    # The bare presence of `=>` is too weak a signal — prose legitimately
    # writes it ("maps key => value", "input => output"), and two such
    # lines would wrongly tip a doc block into "commented-out code".
    if re.search(r"=>\s*[{(]", s):
        return True
    return False


def _is_commented_out_code(nodes: list[Node], src: bytes) -> bool:
    """True when a run of leading comments is disabled code, not docs.

    Only runs made entirely of `//` line comments are judged — a JSDoc
    `/** ... */` or a `/* ... */` block in the run means "treat the
    whole run as documentation" (block comments are almost never used to
    disable code). A `//` run is classed as code when more of its
    non-blank lines look like code than like prose AND at least two
    lines look like code — a single suspicious line is too weak a signal
    to discard what might be a real one-line doc.
    """
    if not nodes:
        return False
    lines: list[str] = []
    for n in nodes:
        txt = _text(n, src).lstrip()
        if not txt.startswith("//"):
            return False  # JSDoc / block comment present → keep the run
        lines.append(txt[2:])
    code = sum(1 for ln in lines if _looks_like_code_line(ln))
    prose = sum(
        1 for ln in lines if ln.strip() and not _looks_like_code_line(ln)
    )
    return code >= 2 and code > prose


def _collect_docs(node: Node, src: bytes) -> list[str]:
    """Collect contiguous preceding comment siblings as docs, dropping a
    run that is actually commented-out code (see `_is_commented_out_code`)."""
    nodes = _leading_comment_nodes(node)
    if _is_commented_out_code(nodes, src):
        return []
    return [_text(n, src) for n in nodes]


def _leading_doc_start_byte(node: Node, src: bytes) -> Optional[int]:
    """Byte offset where the leading doc block starts — used by `show` to
    include docs in the source slice. Returns None when there is no doc
    block, or when the leading comments are commented-out code (which
    `_collect_docs` drops, so `show` must not slice them in either)."""
    nodes = _leading_comment_nodes(node)
    if not nodes or _is_commented_out_code(nodes, src):
        return None
    return nodes[0].start_byte


# --- Misc helpers --------------------------------------------------------


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _elided_text(node: Node, src: bytes) -> str:
    """Source text of `node` with every executable body collapsed to
    `{…}`.

    A field signature is the declaration's value verbatim — fine for a
    plain value (`const n = ref(0)`), but when the value embeds a
    function / method body (`const inc = action((d) => { ...stmts... })`,
    `defineStore('id', { m() { ...stmts... } })`) the raw text dumps
    implementation code into the outline, which is the one thing an
    outline must not do. Every `statement_block` node IS an executable
    body, so replacing each with `{…}` keeps the declaration's shape —
    parameters, object keys, type annotations — and drops only the code.
    """
    bodies: list[tuple[int, int]] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "statement_block":
            bodies.append((n.start_byte, n.end_byte))
            continue  # don't descend into a body being elided
        stack.extend(n.children)
    if not bodies:
        return _text(node, src)
    bodies.sort()
    parts: list[str] = []
    pos = node.start_byte
    for start, end in bodies:
        parts.append(src[pos:start].decode("utf8", errors="replace"))
        parts.append("{…}")
        pos = end
    parts.append(src[pos:node.end_byte].decode("utf8", errors="replace"))
    return "".join(parts)


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
