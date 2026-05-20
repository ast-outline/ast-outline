# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the complete history before v0.6.0, see `git log` and the
[GitHub release page](https://github.com/ast-outline/ast-outline/releases).

## [0.9.1] — 2026-05-20

Patch release closing [issue #3](https://github.com/ast-outline/ast-outline/issues/3)
— the TypeScript/JavaScript adapter's blind spot for structure
expressed through **callback arguments**. Test files (`describe(...)` /
`it(...)`) outlined as empty; Pinia setup-stores
(`defineStore('id', () => {...})`) dumped the entire callback body into
one signature line. Both are now recognised as `block` declarations
and descended into. The recognition rule is structural — no hard-coded
list of framework names — so it covers vitest / jest / mocha / jasmine
/ ava / tape / node:test, Pinia `defineStore`, and any in-house DSL of
the same `name('label', callback)` shape, and survives new frameworks
without a code change.

### Added

- **`KIND_BLOCK` — a structural container kind** (`src/ast_outline/core.py`).
  Groups child declarations the source nests inside a *callback
  argument* rather than a language-level body. Neither a type nor a
  callable: in `outline` it renders its signature with indented
  children (class-like); in `digest` it renders as a `block <label>`
  header with member tokens; it is not counted in the
  `types` / `methods` / `fields` header counters.
- **Callback-DSL block recognition (TypeScript adapter).** A
  `call_expression` becomes a block when (1) its callee is a plain
  identifier, (2) its last argument is a function literal, and (3) its
  first argument is a string-literal label. Clause 3 separates a named
  container (`describe('suite', fn)`, `defineStore('id', fn)`) from a
  bare function wrapper (`action(fn)`) or a property-definition wrapper
  (`defineGetter(obj, 'name', fn)`). Bare statements
  (`describe('...', () => {...})`) and assignments
  (`const useStore = defineStore('...', () => {...})`) are both
  handled; blocks nest (`describe` → `it` → inner declarations).
- All four commands understand blocks: `outline` (indented tree),
  `digest` (`block <label>` header + member tokens), `show` (extract
  by label, including labels with spaces / colons / dots), `grep`
  (blocks join the scope chain; a test description matches as `[def]`).

### Fixed

- **Empty outline for callback-only files** (issue #3, example 2).
  A file whose entire structure lives inside `describe`/`it` callbacks
  now produces a full declaration tree instead of nothing.
- **Callback body dumped into a signature** (issue #3, example 1).
  A `defineStore('id', () => {...})` setup-store no longer collapses
  its whole implementation onto the signature line.
- **Function bodies no longer leak into field signatures.** A field
  whose value embeds a function/method body
  (`const inc = action((d) => { ...stmts... })`) renders with the body
  elided to `{...}` — an outline shows structure, never implementation
  code.
- **Commented-out code no longer renders as documentation.** A leading
  run of `//` comments that is disabled code (braces, arrow bodies,
  declaration keywords — by majority vote) is dropped instead of being
  shown as doc lines on the following declaration. Genuine prose, and
  any `/** ... */` JSDoc block, are kept.
- **`show` finds declarations whose name contains a literal dot.**
  `find_symbols` reads a dot as a hierarchy separator (`Class.method`);
  a callback-DSL block named after a free-text label
  (`it('parses 3.14 correctly')`) would be missed. When a multi-part
  query finds nothing, the whole string is retried as a single name.

## [0.9.0] — 2026-05-17

Minor release — first new language adapter since the Ruby drop:
**Lua** (`.lua`, `.wlua`) joins the supported set, with full outline /
digest / grep coverage. v1 targets vanilla Lua 5.1–5.4 via
`tree-sitter-lua`. Luau (Roblox's typed dialect, `.luau`) is
deferred — same symbol semantics, different parser dependency, less
clear user signal in 2026; planned for v0.9.1+ if pulled.

### Added

- **Lua adapter (`src/ast_outline/adapters/lua.py`).** Maps Lua's
  flat-file / convention-based shape onto the existing IR without
  inventing synthetic kinds. Decision matrix:
  - `function foo()` (top-level global) → KIND_FUNCTION public.
  - `local function foo()` → KIND_FUNCTION private. `local` IS
    the language's private scope.
  - `function M.foo()` → KIND_FUNCTION on `M.foo`. Dotted name is
    preserved verbatim (`M.foo`, `ns.deep.nested.helper`) so the
    qualifier survives into the outline / digest.
  - `function M:bar()` → **KIND_METHOD** on `M:bar`. The colon is
    Lua's source-true marker for implicit `self`, so it's the
    cheapest and most honest signal for "instance method"; we
    don't infer class-ness from `setmetatable` patterns (DSL-
    specific, deferred). Composes with deeply nested namespaces:
    `function ns.deep:methodHere()` → `ns.deep:methodHere`.
  - Metamethods (`__add`, `__sub`, `__mul`, `__div`, `__mod`,
    `__pow`, `__unm`, `__idiv`, `__band`, `__bor`, `__bxor`,
    `__bnot`, `__shl`, `__shr`, `__eq`, `__lt`, `__le`,
    `__concat`, `__len`, `__call`, `__index`, `__newindex`,
    `__tostring`, `__metatable`, `__pairs`, `__name`, `__close`,
    `__gc`, `__mode`) → **KIND_OPERATOR**, regardless of whether
    declared method-style (`function C:__tostring()`), assignment-
    style (`C.__add = function() end`), or with a non-function
    value (`C.__index = C`). The metamethod name dominates the
    classification — `--kind operator` in grep isolates every
    protocol declaration in a Lua codebase regardless of the
    syntactic shape it took. Arithmetic and protocol hooks
    share the bucket on purpose; splitting would force a
    renderer-level distinction the IR doesn't currently make.
  - `M.foo = function() end` (assignment of function value to
    table member) → KIND_FUNCTION on `M.foo`, signature rendered
    as `function M.foo(params)` (not `M.foo = function`) so the
    digest reads as a function decl rather than a field-with-
    function-value. Same shape with metamethod names →
    KIND_OPERATOR.
  - `M.CONSTANT = 42` → KIND_FIELD public.
  - `local x = 1` → KIND_FIELD private.
  - `local x = function() end` → KIND_FUNCTION private.
  - Direct-return-table module shape (`return { foo = function()
    end, CONST = 1 }`) walks the returned table and surfaces each
    named field as a top-level declaration. The second canonical
    Lua module pattern besides `local M = {} ... return M`. Non-
    identifier keys (`["weird"] = 1`, `[10] = "x"`) are skipped —
    no stable name to render, agent can find them via grep.
- **Visibility heuristic (Lua).** Single source of truth in
  `_visibility_for`:
  - `local` scope → "private".
  - Metamethod (`__add`, `__index`, …) → "" public; the
    underscore prefix is part of a language-level protocol, not
    a private-name convention (Python-dunder analogue).
  - Last segment of a qualified name starts with `_` and isn't a
    metamethod → "private". Applies to `M._helper` (private
    helper on a public module) but not to `_M.foo` (which isn't
    a real Lua idiom). Matches the underscore convention in
    Neovim / LÖVE / Roblox-Lua codebases.
  - Anything else → "" public.
- **Lua 5.4 `<const>` / `<close>` attributes** surface in
  `Declaration.attrs` as source-true text (`<const>`, `<close>`)
  so the digest renders them next to the field name without
  inventing a synthetic kind. The grammar parents the `attribute`
  node inside the inner `variable_list`, not at the
  `variable_declaration` level — adapter accesses it positionally
  because tree-sitter-lua doesn't register named fields for these
  slots.
- **`require` imports.** Both surface forms — bare string-arg
  (`require "x"`) and parenthesised (`require("x")`) — register
  as static imports with source-true text preserved. The
  `local Y = require("x")` shape (common Lua idiom) is also
  static: the adapter writes the WHOLE `local X = require(...)`
  byte range into `import_regions` so the grep classifier
  promotes the full line to `[import]` even though the line
  prefix is `local` (too broad a whitelist on its own). `require`
  calls inside function bodies / `if` / loops bump
  `conditional_imports_count` and never appear in the static
  list — Lua's `require` is a runtime function call, so only
  file-top-level ones are statically guaranteed to load.
- **Long-bracket comments and strings in `noise_regions`.**
  `--[[ ... ]]`, `--[==[ ... ]==]` (arbitrary `=` level), and
  long-bracket string literals (`[[ ... ]]`, `[=[ ... ]=]`) all
  span multiple lines. The line-prefix heuristics in `grep.py`
  can't classify matches inside them on their own, so the
  adapter writes their byte ranges into `noise_regions`. Matches
  for symbol names that appear only inside such blocks classify
  as KIND_COMMENT / KIND_STRING and get filtered out of the
  default code view.
- **Leading `---` doc comments** attached to the following decl.
  Contiguous `comment` siblings preceding a declaration are
  accumulated into `Declaration.docs` (Ruby-style leading-docs
  pattern); accumulator clears on each non-comment statement so
  two blank-line-separated comment blocks don't bleed into the
  next decl. `docs_inside=False` — Lua comments live BEFORE the
  decl, not inside the body (Python docstring is the only
  language so far that goes the other way).
- **Grep classifier — Lua chain skipping and sugar calls.**
  `_next_call_paren_after` now takes an optional `language`
  kwarg. For `language="lua"` only, two new skips run BEFORE the
  generic-args block:
  - `.identifier` / `:identifier` chain skipping. `obj:method(x)`
    matched on `obj` walks past `:method` and lands on `(`, so
    it classifies as KIND_CALL. Deep chains (`a.b.c.d(x)`)
    compose naturally. Matches the v0.8.12 "bias toward call"
    policy — agents searching for a name appearing as the
    receiver of a chained call almost always want the call site
    surfaced. Symmetric negative case holds: `obj.field` (no
    trailing call) still classifies as KIND_REF.
  - Sugar-call shapes (`f"x"`, `f'x'`, `f{...}`, `f[[...]]`)
    classify as KIND_CALL. Lua treats them as syntactic sugar
    for the parenthesised call — same call semantics, same
    classification. Bare `[` is intentionally NOT a call marker:
    `f[key]` is a subscript, only the doubled `[[` opens a
    long-string call arg. Strictly opt-in: in TypeScript / Rust
    the same shapes still classify as KIND_REF, because tagged-
    template-literals / struct-literals / array subscripts
    aren't function calls in those languages and broadening the
    walker would mis-classify them.
- **Comment / import line-prefix entries for Lua.**
  `_COMMENT_PREFIXES_BY_LANG["lua"] = ("--",)` and
  `_IMPORT_PREFIXES_BY_LANG["lua"] = ("require ", "require(")`
  in `grep.py`. The `local ` prefix is intentionally NOT
  whitelisted (too broad — would mis-classify every local
  variable binding); `local X = require(...)` lines are caught
  by `import_regions` instead.
- **`AGENT_PROMPT` snippet** (`ast-outline prompt`) lists `.lua`
  alongside the other supported extensions, so any LLM reads it
  on first pass without studying `--help`.
- **CLI help guides** (`GUIDE_DEFAULT`, `GUIDE_OUTLINE`) list Lua
  in their SUPPORTED LANGUAGES tables.

### Out of scope (v1)

- **Luau (Roblox's typed dialect, `.luau`).** Vanilla
  `tree-sitter-lua` produces ERROR nodes on Luau type annotations
  (`x: number`, `function f<T>(): U`, `export type`), and Roblox
  developers mostly live inside Studio anyway. Symbol semantics
  are the same — would slot in as a suffix-dispatch branch on a
  separate `tree-sitter-luau` parser. Planned for v0.9.1+ if
  user signal materialises.
- **`setmetatable`-based inheritance detection.** No syntactic
  anchor in vanilla Lua — only convention (`Child.__index =
  Parent`, `setmetatable(Child, {__index = Parent})`,
  `class("Foo", Bar)` via middleclass / 30log). v1 leaves
  `bases = []` for every Lua decl; agents can still see the
  metamethod assignments (`__index` lands as KIND_OPERATOR) and
  infer the class structure from them. Planned for v0.9.2+ if
  pulled.
- **Module-shape detection.** Each declaration is emitted at the
  file's top level, with the qualifier baked into `name`
  (`M.foo`, `M:bar`) — flat like the Python adapter. Wrapping
  members under a synthetic KIND_NAMESPACE would need a
  heuristic ("which `local X = {}` is THE module?") and would
  fail on the many real files that hold several module-shaped
  tables. Agents reading the file's `return X` statement
  understand the structure themselves.

### Tests

42 regression tests in `tests/unit/test_lua_adapter.py` covering:
parse smoke; classic `local M = {} ... return M` shape (dotted
function decl, local helper, module-table field, leading docs);
colon-vs-dot method discrimination (`function A:b` → METHOD,
`function A.b` → FUNCTION); metamethods in all three syntactic
shapes (method-style, assignment-style, non-function value) →
KIND_OPERATOR; metamethod-name visibility (`__add` is public
protocol, NOT private despite the underscore prefix); direct-
return-table module shape; Neovim plugin `M.setup(opts)`; LÖVE
callbacks (`function love.load()`); `require` in all forms (bare
string-arg, parenthesised, `local X = require(...)`, dotted
module path); conditional require inside function body; static-
require populates `import_regions`; Lua 5.4 `<const>` / `<close>`
attributes; deeply-nested dotted names (`ns.deep.nested.helper`,
`ns.deep:methodOnNested`); long-bracket block comments (level 0
and level 2) and long-bracket strings in `noise_regions`; broken
syntax surfaces error_count and partial decls; empty file; and
Lua-specific grep classifier (chain skipping, sugar calls, bare
`[` not promoted to call, classifier doesn't leak to other
languages — `f"x"` is still KIND_REF in TypeScript / Rust).
1493 tests green (up from 1451).

### Fixtures

10 new fixtures in `tests/fixtures/lua/`: `module_pattern.lua`
(canonical `local M = {}` shape), `mt_class.lua` (`setmetatable`
class with constructor + instance methods + metamethods),
`direct_return.lua` (return-table module shape with metamethod),
`neovim_plugin.lua` (typical `M.setup(opts)` plugin shape),
`love_callbacks.lua` (LÖVE callback pattern — global table
assignment), `requires_and_attrs.lua` (every `require` form + Lua
5.4 attributes + conditional require), `nested_names.lua`
(multi-level dotted namespaces), `strings_and_comments.lua`
(long-bracket comments and strings across levels for
`noise_regions` coverage), `broken_syntax.lua`, and `empty.lua`
for edge cases.

## [0.8.13] — 2026-05-17

Patch release — `ast-outline grep` now classifies calls / strings /
comments correctly on source lines that contain multi-byte UTF-8
characters (Cyrillic, CJK, accented Latin, emoji). The classifier
was passing byte offsets to a codepoint-indexed `str`, so every
line carrying a non-ASCII character before the match position
walked past its intended cursor and produced the wrong kind.

### Fixed

- **Match classification on Unicode source lines.** Before:
  `ast-outline grep` resolved the line column as a 1-based byte
  offset, then indexed `line_content` (a Python `str`) with it.
  For ASCII-only lines the two indices coincide, so the bug was
  silent in the 99% case; for any line containing a multi-byte
  UTF-8 character — Cyrillic (2 bytes/codepoint), CJK (3 bytes),
  emoji / supplementary plane (4 bytes), accented Latin / math
  symbols — the byte offset over-shoots the codepoint index for
  everything after the first multi-byte char. Three downstream
  classifiers consumed the wrong column: `_next_call_paren_after`
  (the call/ref walker) started past the intended position and
  either landed on a later character or ran off the end of the
  line; `_column_inside_string` counted unescaped quotes against a
  string-length cap that wasn't its bound, returning even/odd
  parity for the wrong span; `_classify_match`'s inline-comment
  check compared `#` index against `column - 1` so a comment
  match could be mis-claimed as code on Cyrillic-prefixed lines.
  Net effect: locale-sensitive false negatives across `--kind`
  filtering, leaked string/comment content into the default code
  view, and degraded behavior for any project with non-ASCII
  identifiers, comments, or string literals — Russian / Ukrainian
  / Chinese / Japanese / Korean projects, math-heavy code with
  `≤` / `α` / `β`, emoji in test names. After: the loop that
  converts per-match byte spans to columns decodes the
  line-prefix bytes once per side (`pos - line_start`,
  `end - line_start`) and uses the codepoint length, so every
  downstream check receives a codepoint-correct cursor regardless
  of the multi-byte content ahead of the match. The conversion is
  a no-op for ASCII-only lines (`len(decoded_prefix) ==
  len(prefix_bytes)`), so no performance regression in the common
  path. Repro that surfaced the gap:
  `grep('прив|привет', is_regex=True)` against
  `obj.привет(x, y)` returns `[ref]` (should be `[call]`). The
  fix also pairs with a complementary one in
  `_next_call_paren_after`'s rest-of-identifier skip (next item).
- **Identifier-skip in `_next_call_paren_after` is now
  Unicode-aware.** Companion to the byte→codepoint conversion
  above. The skip introduced in v0.8.12 walked past the trailing
  bytes of an identifier when the match ended mid-word
  (`foo|fooBar` against `fooBar(x)` → leftmost-wins picks `foo`,
  match ends on `B`). It accepted only ASCII `[A-Za-z0-9_]`, so
  a Cyrillic-tail case like `прив|привет` against `привет(x)`
  still returned `[ref]` even after the byte→codepoint
  conversion landed the cursor on `е`. Now the skip uses
  `str.isalpha()` / `str.isdecimal()` / `_`, which covers Unicode
  letter and decimal-digit categories — matching what Python /
  Rust / TypeScript / Swift accept as identifier characters
  beyond the first. Deliberately narrower than `str.isalnum()`:
  that wider check also accepts Unicode No-category numerics
  (`²`, `¼`) which aren't identifier chars in any supported
  language; treating them as identifier tail would extend the
  call-bias policy to shapes that don't arise in real code. The
  rest of the walker (the `<` / `>` / `[` / `]` / `?.` / `::` /
  `!` skip cases) stays ASCII; those are language-syntax
  markers, not identifier content, and broadening them would
  muddle generic-block balance for no real-language gain.
  Negative cases hold: Cyrillic identifier in a non-call shape
  (`привет = 1`, `привет.метод()`, `привет<T> = x`) still
  classifies as `[ref]` because the trailing `(` check fails —
  symmetric with the ASCII negative case the v0.8.12 fix
  documented.

### Behavior change

- `GrepMatch.column` is now documented as a 1-based codepoint
  offset (was: "1-based — byte offset on its line, +1"). On
  ASCII-only lines the values are identical, so no consumer of
  the column field observes a change in the common path. For
  lines with multi-byte UTF-8 before the match position the
  reported column is now the codepoint index — which is the
  number an editor / language server / IDE reports and is what
  agents that pass the column to `Read --offset` or similar
  expect.

### Tests

7 new regression tests in `tests/unit/test_grep.py`:
- `_next_call_paren_after` walker-unit tests for Cyrillic / CJK /
  accented-Latin identifier-tail skipping + composition with
  generics + negative cases.
- End-to-end Python grep tests pinning Cyrillic and CJK call
  classification, ASCII call AFTER a Unicode prefix, in-string
  detection on a Cyrillic-prefixed line, inline-comment detection
  past a Cyrillic-prefixed comment marker, and the
  `GrepMatch.column` codepoint-offset contract.

## [0.8.12] — 2026-05-17

Patch release — `ast-outline grep` now correctly classifies a call
site as `[call]` even when the match position ends mid-identifier,
closing a quietly-painful regex-alternation pitfall that caused
`--kind def,call` to hide real call sites.

### Fixed

- **Match ending mid-identifier no longer demotes calls to `[ref]`.**
  Repro that surfaced the gap: agent runs
  `ast-outline grep "TryAssembleFragments|TryAssembleFragmentsNear" Assets/Scripts/App --kind def,call`
  against a C# Unity codebase to find every definition + caller of a
  method; the three `[def]` matches surface correctly, but the
  caller at `ThingDragNDropController.cs:1208` —
  `controller.TryAssembleFragmentsNear(thingData, currentPosition);` —
  is missing. Plain `grep -rn` confirms the call exists. Cause:
  Python `re` resolves alternation by picking the leftmost
  alternative that matches at a given position (NOT the longest), so
  `TryAssembleFragments|TryAssembleFragmentsNear` matches the
  shorter `TryAssembleFragments` against `TryAssembleFragmentsNear(...)`
  and the match ends on `N` mid-identifier. The call/ref classifier
  in `_next_call_paren_after` (`grep.py`) walks the line right of
  the match-end looking for `(`, already skipping whitespace,
  generic blocks `<...>` / `[...]`, bare closers `>` / `]` (v0.8.8),
  turbofish `::`, TS `?.`, TS `!`, but never the trailing bytes of
  an identifier — `N` was treated as "other significant char",
  returned False, classified as `[ref]`, filtered out by
  `--kind def,call`. Bug was language-agnostic — the walker is
  shared across all adapters, so any language with `name(args)`
  call syntax was affected. Verified on Python, TypeScript, Go,
  Rust, and C#; same regression on all.

  Fix is a one-shot rest-of-identifier skip at the entry of the
  walker: if `start` lands on `[A-Za-z0-9_]`, advance past the
  remaining word bytes, then run the existing skip loop. Applied
  only at entry (not mid-walk) since legitimate intermediate
  identifiers in real-language line shapes don't arise — after the
  existing generic-block balance / `::` / `?.` skips, an identifier
  char mid-walk indicates a different identifier whose call shape
  shouldn't be attributed to the original match. Pinned by
  `test_next_call_paren_after_identifier_skip_one_shot_not_recursive`:
  `foo<T>bar()` matched at `foo` returns False, not True.

  Symmetric to the bare-closer skip from v0.8.8 — both cover the
  same root cause (match end position doesn't align with a token
  boundary), just on different sides of the syntactic boundary.

  Behavior change worth flagging: the fix also affects literal
  (non-regex) substring searches by the same path — `foo` against
  `fooBar(x)` now classifies as `[call]` (was `[ref]`). Consistent
  with the walker's documented "bias toward call" — agents
  searching for a substring inside a called identifier almost
  always want the call site surfaced. Non-call sites (`foo` against
  `fooBar = 1`) still classify as `[ref]` because the trailing `(`
  check fails. Pinned by
  `test_grep_literal_substring_of_called_identifier_classifies_call`
  and `test_grep_alternation_short_first_non_call_still_ref`.

  Identifier-skip uses ASCII `[A-Za-z0-9_]` only, matching the
  rest of the walker's ASCII-only character checks. Non-ASCII
  identifiers (Cyrillic / CJK in Python / Rust / JS) are bypassed
  by a separate pre-existing byte-vs-char index mismatch higher
  in the pipeline — same outcome as before the fix
  (`[ref]` for Unicode identifier calls), orthogonal to this
  change.

  Workaround for users on older versions: write the longer
  alternative first (`TryAssembleFragmentsNear|TryAssembleFragments`)
  or anchor the shorter one (`TryAssembleFragments\b|TryAssembleFragmentsNear`).

  Adds 10 regression tests in `tests/unit/test_grep.py`: 5 unit-level
  tests of `_next_call_paren_after` (basic identifier skip, generic-
  block composition with `<...>` / `[...]` / turbofish, one-shot
  semantics in two configurations) + 5 CLI-integration tests
  across Python / TypeScript / Go / Rust / C# pinning the
  alternation regression on each adapter + 1 negative test for
  non-call sites + 1 explicit literal-substring test. 1444 tests
  green.

## [0.8.11] — 2026-05-16

Minor release — `ast-outline outline / digest / grep` gain
`--exclude <glob>` (repeatable, gitwildmatch / `.gitignore` syntax) so
agents can narrow the walk inline without editing `.gitignore` or
`.ignore` files.

### Added

- **`--exclude <glob>` on `outline`, `digest`, and `grep`** — repeatable
  flag, `.gitignore`-syntax patterns, anchored at the project root so
  `--exclude src/generated/` resolves the same regardless of cwd.
  Supports `!pattern` negation. Layered ABOVE the auto-filter frame,
  so a bare `--exclude '!node_modules/'` re-includes a default-filtered
  dir without the three-line git escape idiom required inside
  `.gitignore`. Still applies under `--no-ignore` — that flag silences
  the auto-filter (`.gitignore` + hardcoded defaults), `--exclude` is
  the user's explicit voice and survives. Explicit single-file inputs
  continue to bypass filtering (same rule as `.gitignore`). When the
  flag contributes to ignored-dir pruning, the existing
  `# note: ignored N dirs (...) via …` line widens its source list
  from `.gitignore/.ignore + defaults` to
  `.gitignore/.ignore + defaults + --exclude`, so an agent debugging
  "where did my folder go" sees its own flag named. Malformed patterns
  (lone `!`, trailing backslash) surface as
  `# note: invalid --exclude pattern: …` and return 0 — honors the
  CLI batch-friendly invariant. Implemented as a single extra
  `GitIgnoreSpec` frame in `collect_files_with_stats`, reusing the
  existing pathspec dependency; no new deps. Agent prompt snippet
  (`ast-outline prompt`) gains one universal sentence covering all
  three subcommands so any LLM picks the flag up on first read. 14
  new regression tests covering each subcommand × negation × CLI vs
  unit level × no_ignore composition × bad-pattern note × explicit-file
  bypass × deeply-nested anchoring.

## [0.8.10] — 2026-05-16

Patch release — `ast-outline show` now resolves markdown headings whose
title contains inline-markdown decoration (`` `inline-code` ``,
`*emphasis*`, `_emphasis_`, `~~strike~~`) when the query drops those
characters, closing the `outline` → `show` round-trip for the common
case where an agent treats inline-md markup as formatting it can strip.

### Fixed

- **Inline-markdown decoration stripped from both sides when matching
  headings.** `ast-outline outline` prints headings verbatim, so an H2
  with inline code appears as ``## `useState` — when to reach for it``;
  agents (and humans) copying the title for `ast-outline show` routinely
  drop the backticks as if they were rendering hints, then `show`
  returned `# note: symbol not found` because the substring matcher
  compared raw heading text — backticks broke continuity. The matcher now
  strips `` ` ``, `*`, `_`, `~` from BOTH the heading title and the
  query before the case-insensitive `in` test. Symmetric: passing the
  decorated form verbatim (``" `useState` — when to reach for it "``)
  still resolves the same heading, so agents that DO preserve the
  decoration don't regress. Scoped to the `substring=True` branch of
  `_trail_matches`, which only fires for `KIND_HEADING` (markdown-only)
  — code-symbol matching keeps strict equality, so a Python `_foo` or a
  Rust `*const T` is never silently broadened. Composes with the
  existing numbered-prefix short-circuit (`2. \`Bar\` setup` resolves
  from `Bar setup`). Structural markup — links `[text](url)`, autolinks
  `<url>`, raw HTML — is intentionally NOT stripped: char-wise removal
  would corrupt the visible label rather than peel decoration, and
  proper handling needs paired-bracket parsing which belongs in a
  separate change. Closes
  [#4](https://github.com/ast-outline/ast-outline/issues/4); adds 8
  regression tests covering each decoration class, the symmetric
  with-markup query, composition with numbered prefixes, the negative
  case (extra words still fail), and a sanity check that code-symbol
  matching is unaffected.

## [0.8.9] — 2026-05-13

Minor release — `ast-outline digest` gains a `--format=` preset selector
with four levels (`names`, `compact`, `default`, `wide`) plus the
`--oneline` alias for `--format=names`. Backwards-compatible: the bare
`digest <paths>` invocation produces byte-identical output to v0.8.8.

### Motivation

Agents reading digest output for unfamiliar repos repeatedly hit the
same friction: the default per-file detail (line ranges, per-class
counters, blank-line paragraph breaks) is exactly right when the next
step is `Read --offset N`, and exactly *wrong* when the next step is
"pick a file to drill into" — at which point the agent only needs file
+ headline symbols. We saw multiple skill traces where agents reached
for `head -N digest` or piped the output through `awk` to throw away
detail they didn't need. The right answer isn't truncation (loses
files at the tail); it's letting the caller name the level of detail
that matches the task at hand.

Four levels match four agent task archetypes:

- **`names`** (one line per file, only top-level type / function
  names): orientation in an unfamiliar repo. The next call is
  `outline <chosen_file>` or `grep <symbol>`. Tightest output.
- **`compact`** (full hierarchy minus visual padding): "explain this
  module's structure" without further calls. Drops per-file counters,
  line ranges, blank lines between types, and the `# no declarations`
  marker. Files with no declarations are hidden entirely.
- **`default`** (unchanged): surgical Read-by-range workflow needs
  `L<a>-<b>`, full counters, paragraph breaks.
- **`wide`** (default + private + fields + no max-members cap): deep
  one-file questions (does this class have field `_cache`?) without
  opening the file.

### Added

- **`digest --format={names,compact,default,wide}`.** The renderer
  dispatches on `DigestOptions.format`. `names` goes through a new
  `_render_digest_names` path that emits `  name.py [label]: A, B, C`
  per file (top-level types + free functions joined by `, `). Markdown
  files surface their top-level headings; YAML files surface their
  top-level keys (or doc separators for multi-doc); CSS / SCSS files
  surface their flat selector list. Files with no public top-level
  symbols are hidden; a directory whose every file was filtered is
  also hidden. When the whole batch is filtered, an explicit
  `# note: all files hidden (no top-level symbols under current
  filters)` line replaces the empty stream so agents don't conflate
  "filtered" with "no files found".

- **`digest --oneline`.** Alias for `--format=names` — picked to match
  `git log --oneline`'s established meaning ("one entry per line").
  Byte-identical output to the explicit form.

- **`compact` rendering rules.** Same hierarchy as `default` but:
  per-file `, X types, Y methods, Z fields` breakdown dropped from the
  header (line + token totals remain — those drive routing); `L<a>-<b>`
  suffix dropped from class headers; the blank paragraph break after a
  type-with-members dropped; files with no declarations hidden
  (default still shows `# no declarations` marker). Inheritance
  (`: Base`), decorators (`@dataclass`), modifiers (`abstract`,
  `sealed`), and the size label (`[large]`) all survive — they carry
  semantic weight, not visual padding. The legend stays at the top of
  the output (compact still uses `()` / `: Base` / `[kind]` tokens, so
  the legend is still load-bearing).

- **`wide` as a CLI-side preset.** No new rendering branch — the CLI
  simply sets `include_private=True`, `include_fields=True`, and
  `max_members_per_type=10**9` when `--format=wide` is passed. The
  existing default-format renderer already shows everything when those
  toggles are on. Implementation choice: keeps the renderer surface
  small, and "wide is default with the knobs cranked" is the right
  mental model.

- **Preset override (`kubectl`-style silent override).** Explicit
  `--include-private` / `--include-fields` / `--max-members` win over
  the preset's defaults. Implemented by switching those three flags to
  argparse `default=None` sentinels — when the user passes the flag,
  their value applies; when they don't, the format preset's default
  applies. Lets the caller fine-tune any preset without learning a new
  flag (`--format=wide --max-members 5` works, gives wide's private +
  fields with a small cap).

- **`--imports` composes with every format, including `names`.** Names
  format emits a 1-line summary per file by default; when `--imports`
  is passed it adds an indented second line per file carrying the
  `imports: …` annotation. Preserves the invariant "if you asked for
  imports, you see them in every format" — an earlier draft silently
  dropped imports under `--oneline` / `--format=names`, which would
  have misled agents into thinking "this file has no imports".

### Design decisions worth pinning

- **Four levels, not three or five.** Each level maps to a distinct
  agent task archetype. A fifth `grep`-shape flat format
  (`path:line:qualname` per symbol, pipe-ready) was considered and
  dropped: it would have collided with the existing `ast-outline grep`
  subcommand's output shape, and no concrete agent task was unmet by
  the four-level set. Will revisit if a real pipe-shape need emerges.

- **No new `--no-X` flags.** A targeted `--no-stats` /
  `--no-bases` / `--no-blanks` family was considered for fine-grained
  tuning between presets. Dropped on YAGNI grounds: the four preset
  levels cover the five agent task archetypes (A — orientation,
  B — module structure, C — surgical Read, D — pipe-filter falls back
  to the existing `grep` subcommand with `--kind def`, E — deep dive),
  and every in-between case is reachable by picking the adjacent
  preset plus the three existing override flags. Adding `--no-X` flags
  speculatively bloats `--help` and locks in a contract with agents
  for behavior that may never be exercised.

- **`: Base` stays the inheritance notation.** A Python-native
  `Class(Base)` alternative was considered for self-documenting
  legend-less reading. Dropped because `: Base` is already
  language-native for the OOP-heavy languages digest most cares about
  (C# `class Foo : IBar`, Kotlin `class Foo : Bar()`, TS / Scala
  via `extends`), and `Class(Base)` in C# 12 reads as a primary
  constructor — would actively mislead.

- **`L40-58` stays as ` L40-58` (double-space prefix).** A
  `[L40-58]`-in-brackets alternative was considered to unify with
  `[large]` / `[broken]`. Dropped: bracket labels and line-range
  references are semantically different (category vs location), the
  current shape is already unambiguous (regex `L\d+-\d+`), and changing
  the shape would break every existing agent skill parsing v0.8.x
  output for no functional gain.

### Test surface

- New file `tests/unit/test_digest_format_presets.py` (26 tests)
  pinning per-format rendering rules: names emits one line per file
  with no methods / no `()` / no inheritance / no ranges; compact
  drops counters / ranges / blanks / `# no declarations` markers;
  wide is exactly default with private + fields + no max-members cap;
  default is byte-identical to omitting the flag. Plus edge-case
  pins: names renders markdown headings / yaml keys / yaml multi-doc
  separators / css selectors; `[huge]` files emit a header-only line
  with no trailing colon; `[broken]` marker preserved across all
  formats; multi-directory output keeps the blank-line separator;
  `--imports` composes with names (2 lines per file); compact still
  applies `--max-members` truncation; compact hides empty markdown
  files (consistent with the empty-code-file rule).

- 8 new CLI-integration tests in `tests/unit/test_cli.py` covering the
  `--format=` and `--oneline` flags through `main()` end-to-end,
  including the kubectl-style override (`--oneline --include-private`
  forces private symbols even though the names preset defaults to
  public-only; `--format=wide --max-members 1` honors the explicit
  cap over wide's `10**9` default) and the LLM-friendly invalid-arg
  path (`--format=verbose` returns rc=0 with a `# note:` line).

- All existing tests still pass.

## [0.8.8] — 2026-05-12

Patch release — `ast-outline grep` now correctly classifies
generic-call invocations (`Bind<SaveSystem>()`, `Map[K, V](...)`,
`parse::<i32>()`) as `[call]` regardless of how the match lands
relative to the generic closer, and surfaces the `--regex` hint on
the `.*` / `.+` / `.?` shape that previously fell through to bare
"no matches".

### Fixed

- **Generic-call invocations no longer misclassify as `[ref]` when the
  match ends on the closer (`>` or `]`).** Repro: agent grepping a C#
  Unity codebase with `ast-outline grep Bind.*SaveSystem --regex
  --kind call` got 0 hits and fell back to `rg "Bind<SaveSystem>"`,
  even though the line `c.Bind<SaveSystem>();` is an invocation.
  Root cause: `_next_call_paren_after` only knew how to skip an
  *opener* (`<` / `[`) preceding the cursor — it balanced the block
  to the matching closer and resumed the search for `(`. But the
  same call-vs-ref decision lands on a *closer* in two routine
  shapes: greedy regex `Bind.*SaveSystem` whose match ends on `>`,
  and literal `parse::<i32` / `Map[String, Int` patterns agents
  type to disambiguate generic overloads. With no closer-skip the
  walker hit `>` / `]`, fell through `return ch == "("` → False, and
  classified the whole invocation as `[ref]` — making `--kind call`
  return 0 across C# / Java / Kotlin / Scala / TypeScript / Rust
  (turbofish-with-explicit-type) / Go (1.18+ `Foo[int]()`) / C++.
  The walker now skips bare leading `>` / `]` the same way it skips
  whitespace, `?.`, `!`, and `::` — consistent with the existing
  bias documented in the walker's caveat (when the line shape is
  ambiguous, classify as call; ref false positives in code-search
  contexts are more painful than call false positives). Adds 8
  per-language tests pinning the matrix.

- **`Foo.*Bar` no longer fails silently with 0 results and no hint.**
  Companion gap to the above — even after a user adds `--regex`, the
  classifier bug above blocked them; even before, the hint that
  *would* have surfaced `--regex` didn't fire on `.*` patterns.
  The warn-on-no-match hint required the pattern to carry either an
  escaped metachar (`\.`, `\(`), a *letter*-or-`)` / `]` followed by
  a quantifier (`d*`, `)*`), or an edge anchor (`^`, `$`). The
  `.<quantifier>` shape — where `.` is the char before `*` / `+` /
  `?` — slipped through both the strict auto-promote fingerprint
  (intentionally excluded — `.` and `*` individually appear in
  literal code as qualified names and array types) AND the
  warn-on-no-match fingerprint, so the agent saw no signal that
  `--regex` would have helped. The ambiguous-regex fingerprint now
  treats `.[*+?]` as unambiguous regex intent: bare `.` in qualified
  names (`User.save`) is still skipped, but the *pair* `.*` / `.+` /
  `.?` has no literal-code interpretation worth protecting. Extends
  the same hint-coverage principle as v0.8.4 (kind-filter zero-results
  hint) to the regex-mode blind spot.

## [0.8.7] — 2026-05-12

Patch release — block-level HTML comments (`<!-- ... -->`) in markdown
are now classified as `[comment]` in `ast-outline grep`, so hidden
TODO / NOTE / draft annotations no longer surface as prose mentions.

### Fixed

- **`<!-- ... -->` in markdown noise-filtered as comments.** A
  multi-line `<!-- TODO: revisit useState patterns -->` block in
  README.md previously surfaced under `ast-outline grep useState`
  as a regular `[ref]` match alongside real prose mentions — agents
  reading the result couldn't tell signal from author-private
  annotation. The markdown adapter now appends `(start, end,
  "comment")` regions for every block-level `html_block` whose first
  four bytes are `<!--`, which the existing noise filter handles
  identically to source-language comments. Surfaces with `[comment]`
  under `--include-noise` so the agent can opt back in. Inline
  `<!-- -->` *inside a paragraph* is NOT covered — tree-sitter-markdown
  fragments those into single-character punctuation nodes with no
  clean byte range; block-level is the 95% case for hidden
  annotations. Other `html_block` content (raw `<div>`, `<table>`)
  intentionally stays visible — embedded HTML carries searchable
  signal (component names, data attrs) that an agent may legitimately
  grep for.

## [0.8.6] — 2026-05-12

Patch release — `ast-outline grep` no longer drowns YAML searches in
shell lines lifted from `run: |` block scalars in CI workflows / Helm
charts / K8s ConfigMaps.

### Fixed

- **Block scalars (`|`, `>`) are now noise-filtered in YAML grep.**
  `ast-outline grep npm .github/workflows/` previously returned every
  `npm install`, `npm run lint`, `npm run build` line lifted from
  `run: |` step bodies — the structural mentions (job names, step
  names) drowned in the shell-line noise. The YAML adapter now
  populates `ParseResult.noise_regions` with the byte ranges of every
  `block_scalar` node (kind `string`, so the existing `--noise-filter`
  / `--include-noise` path handles it without a new flag). Plain
  scalars stay visible — `image: registry.example.com/api`,
  `replicas: 3`, single-line `run: npm publish` are exactly what
  agents grep YAML for; only the multi-line block forms (`|` literal,
  `>` folded) — which YAML authors reach for specifically to embed
  opaque scripts / templates — get masked. Mirrors the v0.8.5
  fenced-code-block treatment in markdown.

## [0.8.5] — 2026-05-12

Patch release — `ast-outline grep` no longer drowns markdown searches
in matches lifted from fenced code examples.

### Fixed

- **Fenced code block bodies are now noise-filtered in markdown grep.**
  `ast-outline grep useState docs/` against a tutorial site previously
  surfaced every example-code occurrence of `useState` alongside the
  prose mentions, making the result useless on docs-heavy repositories.
  The markdown adapter now populates `ParseResult.noise_regions` with
  the byte ranges of each fenced block's `code_fence_content` (kind
  `string`, so the existing `--noise-filter` / `--include-noise` path
  handles it without a new flag). Fence delimiters and the info string
  itself (`` ```python ``) stay searchable, so language-by-fence
  queries still work. Indented (4-space) code blocks are intentionally
  not masked yet — rare in modern markdown and almost always paired
  with a fenced equivalent. Repro that surfaced the gap:
  `ast-outline grep useState README.md` returning identical hits from
  prose and from the JSX example below it.

## [0.8.4] — 2026-05-11

Patch release — `grep --kind X` now tells the agent which kinds were
excluded when the result is empty, so a wrong-kind narrow no longer
masks the symbol's presence.

### Added

- **Kind-filter hint on empty `grep` results.** When `grep --kind X`
  returns zero matches but the pattern would have matched under
  other kinds, the CLI now prints a second line under the `# note:`
  with a breakdown and a retry suggestion:

  ```
  # note: no matches for 'EditorPrefs'
  # hint: --kind call excluded 4 matches (4 ref) — retry with --kind call,ref or drop --kind
  ```

  Motivation: agents reading `EditorPrefs.GetString(...)` see a call,
  pass `--kind call`, and get nothing — because the match lands on
  the type name `EditorPrefs` (a `ref`, since `.` follows it), not on
  the called method `GetString`. The bare "no matches" hid the fact
  that the symbol was present in a different role; one extra line
  collapses what was previously a binary-search through `--kind`
  values into a single retry. Universal across all six kinds (`def`,
  `call`, `ref`, `import`, `comment`, `string`) and works with
  multi-kind filters (`--kind def,import`). Suppressed when the
  regex-syntax hint fires for the same empty result — one hint per
  call keeps the output scannable. Internal `grep()` API gained a
  third tuple element `kind_excluded_counts: dict[str, int]`.

## [0.8.3] — 2026-05-11

Patch release — fix `show` lookup for markdown headings carrying a
numeric prefix (the form `outline` prints), restoring round-trip
`outline` → `show`.

### Fixed

- **`show "3. Foo"` / `show "4.2 Foo"` now resolve.** Markdown
  headings like `## 3. Numbered Heading` are stored with their
  numeric prefix intact (and `outline` prints them that way), but the
  `find_symbols` query tokenizer was reading the dot in `3.` as a
  path separator and splitting the query into `["3", " Foo"]`. That
  forced the matcher to look for a two-segment trail that never
  exists — markdown headings are single declarations whose `name`
  carries the prefix — so the lookup silently returned `symbol not
  found`. `_split_query` now short-circuits queries shaped like a
  numbered-heading prefix (`\d+(\.\d+)*\.?\s+<text>`) into a single
  opaque token, letting the existing substring-matching path resolve
  them. Bare dotted-numeric queries without trailing text (`"1.2"`,
  `"1.foo"`) keep the previous split behaviour so non-markdown
  lookups are unaffected. Fixes
  [#2](https://github.com/ast-outline/ast-outline/issues/2).

## [0.8.2] — 2026-05-10

Patch release — agent-prompt clarity + cross-command flag hints to
prevent LLM agents from misrouting subcommand-scoped flags.

### Fixed

- **Agent-prompt: `--signature` scope disambiguated.** The canonical
  agent prompt (`ast-outline prompt`) previously read
  *"Add `--signature` to any of the above"* inside the `show` section.
  Literal models (Opus 4.7, GPT-5.5) read the menu globally and reached
  for `--signature` on `outline` or `digest`, where the flag does not
  exist. Reworded to *"Add `--signature` to `show` (only there)"* —
  preserves the workflow hint, adds an explicit anchor.

### Added

- **Cross-command flag hint on unrecognized arguments.** When an unknown
  flag passed to one subcommand is recognized by another, the
  `# note: unrecognized arguments: --flag` line now carries
  `(hint: \`--flag\` is a flag of \`<other-cmd>\`, not \`<this-cmd>\`)`.
  Truly unknown flags get no hint. Helps LLM agents self-correct in one
  retry instead of guessing or asking the user.

## [0.8.1] — 2026-05-08

Patch release — usability fix for the `grep` subcommand.

### Fixed

- **`ast-outline grep -e PATTERN PATHS...` now works** without a
  separate positional pattern, matching POSIX `grep -e` and `rg -e`
  conventions. Previously the command failed with
  `the following arguments are required: paths` because argparse
  couldn't disambiguate the trailing string. Implemented as a
  pre-argparse rewrite: when no positional pattern appears before the
  first `-e`, the first `-e PATTERN` value is promoted into the
  positional slot. All existing call shapes (`grep PAT PATH`,
  `grep PAT -e PAT2 PATH`) keep their current semantics. Long-form
  `--expression PAT` and equals-form `--expression=PAT` are accepted
  the same way.

## [0.8.0] — 2026-05-08

Minor version bump — adds the `grep` subcommand (a structural,
AST-aware code search), plus the `--kind` filter and several
smaller flags. The `grep` command is now stable; all behavior
documented below is part of the public surface.

### Added

- **`ast-outline grep` — AST-aware structural search.**
  New subcommand returning matches grouped by
  enclosing class/function. Tagged kinds: `[def]` (definitions),
  `[import]` (import statements); calls and refs render untagged
  (inferable from `(` after symbol). `[comment]` / `[string]`
  surface only via `--include-noise`. Output line shape:
  `> L<n>: <code>[ <tag>]` (mirrors `grep -n` / compiler-error
  convention). Designed for "where is X defined / called / used"
  agent workflows — one structured call replaces the typical
  grep + multiple file-reads chain. Substring pre-filter via
  `bytes.find()` skips parse for files with no positional match;
  tree-sitter walks `string` / `comment` nodes (Python adapter;
  others fall back to per-line heuristics) so docstring matches
  are filtered. Generic-call-aware classification handles
  `genericCall<T>()`, turbofish `::<T>()`, optional chain
  `?.()`, TS non-null `!()`. Typical perf: ~120ms per project,
  ~250ms on 800+ file monorepos.

- **POSIX-style flags** — `grep`/`rg` muscle memory:
  `-e/--expression PATTERN` (repeatable, multi-pattern via
  alternation — one walk for N symbols), `-w/--word`
  (whole-word `\b...\b` — eliminates `save_user`/`unsave` noise),
  `-l/--files-with-matches` (paths only, for exists-checks),
  `-c/--count` (`path:N`, skips zero-count files),
  `-m/--max-count NUM` (per-file cap; emits explicit
  `# truncated — N more...` footer so partial results are never
  silent — `-c` reports the capped count, matching POSIX),
  `-i/--case-insensitive`, `--regex`, `--include-noise`,
  `--no-ignore`. All operate on the AST-aware base, so counts
  and file lists exclude docstring noise.

- **`--kind` filter** — narrow matches by classification:
  `def | call | ref | import | comment | string`. Accepts
  repeated (`--kind def --kind call`) or comma-separated
  (`--kind def,call`) forms. Auto-enables `--include-noise`
  when the filter contains `comment`/`string` (the noise filter
  would otherwise zero them out before the kind filter sees
  them). Composes naturally with `-c` (capped counts) and `-l`
  (only files with matching kinds). Eliminates the most common
  agent post-filter step ("show me only definitions of X" /
  "only call sites").

- **Regex auto-detect.** Patterns with unambiguous regex syntax
  (`\|`, `\d`, `\w`, `\s`, `\b`, `(?:`, bare `|`) auto-promote
  with a `# note:` documenting the promotion; `\|` is normalized
  to `|` before compile (BRE→ERE — Python's `re` reads `\|` as
  literal pipe, opposite of grep). Ambiguous metachars (`.`,
  `*`, `+`, `?`, `[`, `^`, `$`) never auto-promote — they have
  legitimate literal interpretations in code — but emit a
  `# hint:` on zero matches suggesting `--regex` retry.

- **`ParseResult.noise_regions`** — optional adapter-populated
  `(start_byte, end_byte, kind)` tuples for multi-line strings
  and block comments. Used by `grep` to reliably filter
  docstring matches (a regex pre-pass gets confused by code
  containing `('"""', "'''")`-style triple-quote literals).
  Python adapter populates; others opt in over time.

- **`ParseResult.import_regions`** — adapter-populated
  `(start_byte, end_byte)` byte ranges of import declarations.
  Lets `grep` classify inner symbols inside multi-line / block-
  form imports as `[import]` instead of `[ref]`/`[string]` —
  fixes Go `import (\n  "fmt"\n)`, Python `from X import (\n A,\n)`,
  TS `import {\n  A,\n} from 'y'`, Rust `use foo::{\n  Bar,\n}`,
  PHP `use App\{\n  Foo,\n}`. Tree-sitter is the authoritative
  source — line-prefix heuristic only saw the opening line and
  classified inner package names as strings/refs, semantically
  wrong for any reader. Adapters populating: Python, TS, Go,
  Rust, PHP, C++ (`using namespace` / `using X::Y` — but NOT
  `using A = B;` type aliases). Single-line-only languages
  (Java/Kotlin/Scala) work unchanged via the line-prefix path.
  **Zero-cost:** byte ranges piggyback on the existing
  `_collect_imports` walk (one tree traversal, two outputs) —
  no separate pass, no measurable overhead vs not collecting
  them at all.

- **C# `global using`** — modern .NET 6+ file-scoped using
  directive (`global using System;`) now classifies as
  `[import]`. Line stripped starts with `global ` not `using `,
  so a separate `"global using "` prefix entry was added to the
  csharp import dict.

- **C++ `using namespace` / `using X::Y`** — bring-into-scope
  directives now classify as `[import]`. AST-level distinction
  via tree-sitter (`using_directive` / `using_declaration` vs
  `alias_declaration`) means type aliases (`using my_int = int;`)
  stay correctly classified as declarations, not misread as
  imports.

- **Scala multi-line braced imports** — `import foo.{\n  Bar,\n}`
  now classifies inner symbols as `[import]`. Scala adapter
  joins the import_regions populating set via piggyback on
  the existing `_collect_imports` walk.

- **Python lazy imports inside function / class bodies** —
  multi-line `def foo(): from x import (\n  a,\n  b,\n)` now
  classifies inner symbols as `[import]`. Achieved by extending
  `_count_conditional_imports` (which already walks the whole
  tree to count scoped imports) to ALSO collect their byte
  ranges in the same pass — zero extra traversal cost.

- **Agent-facing prompt snippet** updated with `grep` as the
  fourth menu option, with `-e` example for batched lookups.

## [0.7.7] — 2026-05-06

### Added

- **Setup-prompt — Claude-Code-only Explore-shadow sub-step.**
  Claude Code's built-in `Explore` subagent runs in an isolated
  context — it does not inherit `CLAUDE.md` / `AGENTS.md`, so the
  snippet written in Step 2 of `setup-prompt` does not reach
  `Explore` invocations on its own. Setup-prompt now detects when
  the user has Claude Code (`~/.claude/` exists) AND no
  `.claude/agents/Explore.md` shadow file exists yet, and asks
  once whether to create the shadow. If approved, writes a
  ready-to-go `Explore` definition that **embeds the full fresh
  canonical** from Step 2.1 verbatim (not a short pointer — the
  shadow is a brand-new file and embedding avoids forcing every
  `Explore` invocation to re-run `ast-outline prompt`), wrapped
  in the standard `<!-- ast-outline:start -->` /
  `<!-- ast-outline:end -->` markers; offers project-local
  (`.claude/agents/Explore.md`) vs global
  (`~/.claude/agents/Explore.md`) scope. On future re-runs the
  shadow falls under the diff-aware patch flow and gets refreshed
  on `ast-outline` upgrades. Codex and Gemini subagents are
  user-defined files only — they fall under the existing
  diff-aware patch logic, no shadow concept needed. Skipped in
  headless mode and when a shadow already exists. Closes the gap
  where ast-outline integration via AGENTS.md silently failed to
  reach the most-used Claude Code subagent.

### Changed

- **Setup-prompt — Step 2 detects user-written ast-outline content
  outside markers.** Previously the "markers absent" branch
  silently appended a fresh marker block at the end of an existing
  AGENTS.md / CLAUDE.md / GEMINI.md. If the user had hand-written
  ast-outline content (perhaps from an old `ast-outline prompt >>
  AGENTS.md` run that they later edited, or notes in their own
  words), this left two competing references in the same file.
  Setup-prompt now scans for `ast-outline` mentions in the target
  file before any write. If found outside markers, it shows the
  user the offending lines and asks which path to take: (1) wrap
  the existing content in markers verbatim — leaves text exactly
  as written, future re-runs fall under the diff-aware branch;
  (2) replace the hand-written block with the fresh canonical;
  (3) append the fresh canonical anyway, accepting the
  duplication; (4) skip Step 2 entirely. Default is to **ask**;
  silent append on top of existing user content is no longer
  reachable. Protects manual customizations across CLI upgrades
  even when the user never used the `setup-prompt` flow before.

## [0.7.6] — 2026-05-06

### Added

- **`setup-prompt` subcommand** — `ast-outline setup-prompt` prints
  an install-time checklist meant for one-shot consumption by a
  coding agent (Claude Code, Codex CLI, Gemini CLI, Cursor). Tell
  the agent "run `ast-outline setup-prompt` and follow its
  instructions" and it walks the user through three idempotent
  steps: (1) **verify the CLI** — runs `ast-outline --version`. If
  missing, detects what install tooling is on PATH
  (`uv` / `pipx` / `pip`) and whether a Python venv is active
  (`VIRTUAL_ENV`, `.venv/`), presents both install paths — global
  isolated (`uv tool install ast-outline`, recommended) or project
  venv (`pip install ast-outline`) — and may install on the user's
  behalf with explicit consent. Best-effort PyPI version check
  surfaces the matching upgrade command (`uv tool upgrade` /
  `pipx upgrade` / `pip install -U`) per install path; never
  auto-upgrades. (2) **persistent-context file write** is
  system-aware and diff-aware. Target file adapts to the user's
  tooling: `./AGENTS.md` cross-tool default (covers Codex CLI
  natively, Claude Code via `@AGENTS.md` import, Gemini CLI with
  `~/.gemini/settings.json` `context.fileName` config, and Cursor),
  or the native single-vendor file (`./CLAUDE.md` for Claude Code
  only, `./GEMINI.md` for Gemini CLI only — detected from which
  `~/.<tool>/` directories exist). Project-local vs global scope
  choice (`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` /
  `~/.gemini/GEMINI.md`); Codex `AGENTS.override.md` precedence
  handling so writes are not silently shadowed. Snippet wrapped in
  `<!-- ast-outline:start -->` / `<!-- ast-outline:end -->`
  markers so re-runs find the existing block. On re-run, if the
  block content differs from the fresh `ast-outline prompt` output
  (likely a CLI upgrade or a user-edited block), the agent shows
  the diff and offers replace / keep / show-full-diff — never
  overwrites customizations silently. (3) **optional subagent
  patches** — finds exploration-oriented subagent files under
  `.claude/agents/` / `.codex/agents/` / `.gemini/agents/` and, with
  per-agent permission, inserts a small `## Tooling — ast-outline`
  block. Built-in subagents (Claude Code's `Explore`,
  `codebase-scout`, etc.) are out of scope — not file-based.
  Headless harnesses (`codex exec`, `claude -p`, Gemini
  non-interactive, scheduled CI) restrict execution to Steps 1
  and 2 at project-local scope; skip Step 3 entirely; list every
  skipped optional decision in the final report. The agent mirrors
  the user's conversation language (Russian, Chinese, etc.) for
  spoken replies and any free-form prose written into a freshly-
  created `./AGENTS.md` / `./CLAUDE.md` (top headings, brief
  section labels); two exceptions stay English regardless — the
  marker-wrapped snippet (cross-vendor LLM reliability) and
  subagent files entirely (system prompts). Cross-OS by design —
  the agent translates `which`, `$VIRTUAL_ENV`, `curl` examples to
  the user's shell (`where.exe` / `Get-Command`,
  `%VIRTUAL_ENV%` / `$env:VIRTUAL_ENV`). User-facing questions
  are short and beginner-friendly, one decision at a time, with
  the exact command shown before any run. The CLI itself is
  `print(SETUP_PROMPT, end="")` — file I/O is delegated to the
  agent's native edit tools, so encoding / permission / merge-
  conflict edge cases are handled in the agent's context, not in
  Python. Distinct from `ast-outline prompt`, which emits the
  use-time snippet that lives inside AGENTS.md and steers every
  code-reading turn; `setup-prompt` is the install-time checklist
  that puts that snippet there. Cross-vendor universal —
  calibrated to work across Claude Opus 4.7 / Sonnet 4.6 / Haiku
  4.5, OpenAI GPT-5.x (5.3-codex / 5.4 / 5.5), and Gemini 3.x (Pro
  / Flash / Flash-Lite): outcome-first phrasing under markdown
  headings, no persona, no chain-of-thought scaffolding, no
  aggressive emphasis, no model-name pinning. Reachable via
  `ast-outline help setup-prompt` for a topic-specific guide.

## [0.7.5] — 2026-05-06

### Added

- **SQL adapter** — parses `.sql`, powered by DerekStride's
  `tree-sitter-sql` plus a regex fallback for grammar-unsupported
  constructs. Targets DDL: every `CREATE TABLE` is a `KIND_TABLE`
  whose columns surface as `KIND_FIELD` children with the full
  source-true column line as signature
  (`email TEXT NOT NULL UNIQUE`). Run `outline schema.sql` and an
  agent reading a multi-thousand-line `pg_dump` artefact gets the
  entire schema shape — table list + column types + inline
  constraints — without loading the file; `digest` shows the table
  list plus a column count per file (use `--include-fields` to see
  the columns themselves). `CREATE VIEW` and
  `CREATE MATERIALIZED VIEW` → `KIND_VIEW` (distinguished via
  `native_kind`); `CREATE TYPE foo AS (…)` → `KIND_RECORD` with
  field children; `CREATE TYPE foo AS ENUM (…)` → `KIND_ENUM` with
  member children; `CREATE FUNCTION` and `CREATE TRIGGER` →
  `KIND_FUNCTION` (triggers carry `native_kind="trigger"`);
  `CREATE INDEX` and `CREATE SEQUENCE` → `KIND_FIELD` +
  `native_kind`; `CREATE SCHEMA` → `KIND_NAMESPACE`.
  `CREATE EXTENSION` collected into imports. `--` line and
  `/* … */` block comments immediately preceding a statement
  attach as `docs` (multi-line block comments preserve source
  line order). PL/pgSQL bodies inside `AS $$ … $$` parse as
  opaque dollar-quoted strings — the function header (name,
  parameters, return type) extracts cleanly, and parse errors
  inside function bodies are excluded from `error_count` so a file
  using `:=` assignment or `IF…THEN…END IF` doesn't get
  mis-reported as broken. **Regex fallback** recovers six constructs
  the upstream grammar can't parse:
  `CREATE [OR REPLACE] PROCEDURE` → `KIND_FUNCTION` +
  `native_kind="procedure"`; `CREATE DOMAIN` → `KIND_FIELD` +
  `native_kind="domain"`; `CREATE TABLE … PARTITION OF parent` →
  `KIND_TABLE` (modern PG declarative partitioning; the parent's
  columns are inherited so child decls have no `children`);
  `CREATE FUNCTION … SECURITY DEFINER` and similar exotic modifier
  orderings the grammar errors on → `KIND_FUNCTION`, with a
  byte-range guard so cleanly-parsed functions aren't
  double-extracted; `LOAD 'lib'` and
  `IMPORT FOREIGN SCHEMA … FROM SERVER … INTO …` → imports list.
  The fallback is line-anchored, gated by AST-derived skip ranges
  (`comment` / `marginalia` / `literal` / `block` subtrees) — red
  herrings like `CREATE PROCEDURE` text inside a comment, a string
  literal, or an outer function's PL/pgSQL body don't surface as
  spurious declarations — and short-circuited by a bytes-level
  fingerprint check, so the typical schema-dump file with no
  fallback constructs pays nothing. Verified working end-to-end:
  `CREATE TABLE IF NOT EXISTS / TEMP / UNLOGGED`,
  `CREATE TABLE AS SELECT`, generated columns
  (`GENERATED ALWAYS AS … STORED`), identity columns, indexes with
  `USING gin / gist`, partial indexes (`WHERE …`), expression
  indexes, `CREATE OR REPLACE FUNCTION`, `RETURNS TABLE(…)` /
  `RETURNS SETOF`, function modifiers (`IMMUTABLE` / `STABLE` /
  `VOLATILE` / `SECURITY DEFINER`), reserved-word and Unicode
  quoted identifiers (`"user"`, `"Пользователи"`), CRLF line
  endings, files with no trailing semicolon, empty files, and
  comment-only files. Dialect coverage: PostgreSQL is the primary
  target (every modern construct works); MySQL and SQLite schemas
  extract tables / columns / indexes / views cleanly with some
  `error_count > 0` noise on dialect-specific table options
  (`ENGINE=InnoDB`, foreign-key clauses, `AUTOINCREMENT`); MSSQL,
  T-SQL, Oracle PL/SQL, and BigQuery have partial coverage —
  bracketed identifiers, `GO` separators, Oracle types like
  `VARCHAR2`, and BigQuery `STRUCT<>` / backtick names degrade.
- **TypeScript / JavaScript adapter** — dynamic `import('...')` calls
  inside a function, method, control-flow block (`if` / `switch` /
  `try` / loops) or class body now contribute to
  `ParseResult.conditional_imports_count`, rendered as
  `[+ N conditional includes]` next to the imports line in outline /
  digest. Brings TS/JS in line with Python, Ruby and PHP — an agent
  reading the outline of a file with lazy module loading no longer
  treats it as dependency-closed by its top-level `import` statements.
  Top-level `await import('./x')` is NOT counted (it executes
  unconditionally on module load, mirroring PHP's rule for top-level
  assignment-wrapped includes). `require(...)` calls remain
  deliberately out of scope (no dedicated AST node — pattern-matching
  by callee identifier is fragile). Applies to `.ts`, `.tsx`, `.js`,
  `.jsx`, `.mjs`, `.cjs`.

## [0.7.4] — 2026-05-06

### Added

- **CSS adapter** — parses `.css`. Each rule (`.foo, .bar { ... }`) is one
  `KIND_RULE` declaration carrying the bare simple-selector tokens it
  styles (`match_names = [".foo", ".bar"]`); `find_symbols(".btn-primary")`
  returns every rule whose selectors include `.btn-primary` in any
  position (direct, descendant, with pseudo-class, with attribute
  filter), so an agent debugging the cascade gets every relevant
  definition in one query, with the wrapping at-rule (`@media`,
  `@supports`, `@layer`, …) in the breadcrumb. Strips pseudo-class and
  attribute-filter decorations for matching — `.btn-primary:hover` and
  `.btn-primary[disabled]` both match `.btn-primary`. `:is(...)` and
  `:where(...)` arguments recurse (additive); `:not(...)` and `:has(...)`
  do not (different semantics). Rules nested via CSS native nesting
  resolve `&` against the parent. `@keyframes`, `@media`, `@supports`,
  `@layer`, `@container`, `@font-face` surface as `KIND_AT_RULE`.
  `@import` collected into the imports list.
- **SCSS adapter** — parses `.scss`. Builds on the CSS model and adds
  SCSS first-class symbols: `@mixin name($args)` → `KIND_MIXIN` with
  parameter list in the signature (callable, gets `()` in digest);
  `@function name($args)` → `KIND_FUNCTION`; top-level
  `$variable: value` → `KIND_VARIABLE` (full assignment in signature,
  including `!default`); `%placeholder` → `KIND_PLACEHOLDER`. `@use`,
  `@forward`, and legacy `@import` collected into the imports list.
  Sass privacy convention applied — names with leading `_` or `-` are
  marked `visibility="private"` (so `--include-private=False` hides
  them in outline / digest, mirroring what Sass itself doesn't export).
  Nested rules with `&` resolve against each parent simple selector,
  so `.card { &__header { } }` is findable as `.card__header`.
  Multi-selector parents propagate (`a, .link { &:hover { } }` makes
  the `&:hover` block findable as both `a` and `.link`).
- **`Declaration.match_names`** — new optional field. When non-empty,
  the search walker matches any of these names instead of `Declaration.name`,
  with the matched entry used as `qualified_name` (no path-joining
  with parents — these names are absolute). Empty default means
  existing code adapters work unchanged. Mechanism for the CSS
  selector-list and SCSS nesting-resolution shapes; available to any
  future adapter where one declaration is reachable under several
  identifiers.
- **`*.min.js` / `*.min.mjs` / `*.min.cjs` / `*.min.css` / `*.min.html` /
  `*.map`** — added to the default ignore patterns. Minified bundles
  parse to one giant rule / one giant function with no semantic
  structure; outlining them is meaningless and tree-sitter spends real
  time on the single mega-line. Source maps are JSON, no adapter
  claims `.map`, but pattern listed for clarity. Override with
  `--no-ignore` if a project deliberately edits a minified file by
  hand.
- **`[huge]` size label** — fourth size bucket alongside
  `[tiny]` / `[medium]` / `[large]`, with a behavioral twist: in `digest`
  only, files at or above 100 000 estimated tokens collapse to
  header-only. The agg counters in parens (lines, tokens, types,
  methods, fields) still appear, so the agent has enough to decide
  whether to drill in via `ast-outline outline <path>`. The body is
  omitted to keep digest of a directory full of generated SDKs / vendored
  mega-files (TS compiler internals, large checkers, autogenerated
  protobuf classes) under control — measured 31× output reduction on a
  pathological 50-file batch (1 602 → 52 lines).

  `outline` and `show` are unchanged: when the agent explicitly opens
  one file by path, it gets the full structure regardless of size. The
  label is the at-a-glance signal; the collapse is a digest-only
  policy. Aligned with the existing principle in `_size_label` —
  *"information beats instruction"* — except that `[huge]` is the one
  label that informs about a size class AND signals omitted content.

  Threshold rationale: 100 000 tokens ≈ 7-15 k lines of typical code
  (20× the `[large]` floor). Catches `checker.ts` (~742 k tokens),
  generated SDK clients (~250 k+), and `pandas/core/frame.py`
  (~112 k, 229 methods — borderline but its 941-line digest body
  was real noise). Stays out of legitimate domain classes ≤ 5 k
  lines (~30-50 k tokens). Constant `_SIZE_LABEL_HUGE_FLOOR` in
  `core.py` — tunable per-deployment without touching the rendering
  path.
- **Dynamic legend entry for `[huge]`** — when any huge file appears in
  the rendered batch, the legend gets one extra clause:
  `[huge]=body omitted (use \`ast-outline outline <path>\`)`. The agent
  reads the digest cold and immediately knows what the marker means
  and how to expand. Other size labels (`[tiny]` / `[medium]` /
  `[large]`) stay out of the legend — they're plain English, no
  lookup needed; only `[huge]` is documented because it ALSO changes
  what gets rendered.

### Changed

- **Agent-prompt snippet** lists `.css` / `.scss` alongside the rest
  and adds a tight 3-line note on selector-token query semantics — for
  css/scss the symbol is a selector token (`.btn-primary`, `$var`),
  pseudos and attribute filters are stripped, so `.btn-primary` finds
  the rule even when it carries `:hover` or nests in `.modal`. Snippet
  also mentions `[huge]` in the size-label list and tells the agent
  to call `outline <path>` to expand. Length budget bumped from 3200
  to 3600 chars to accommodate both additions.

## [0.7.3] — 2026-05-06

### Changed

- **Outline header now carries the size label** — `[tiny]` / `[medium]`
  / `[large]`, the same categorical bucket digest stamps next to each
  filename. An agent calling `outline` directly (skipping `digest`)
  gets the at-a-glance size signal in plain English alongside the
  precise `~N tokens` count, instead of having to map the raw token
  number onto a bucket from memory. Header reads
  `# /abs/path.py [medium] (95 lines, ~1,200 tokens, 5 types, 12 methods)`.
  Digest output is unchanged.

## [0.7.2] — 2026-05-06

### Added

- **Ruby language adapter** — parses `.rb`, `.rake`, `.gemspec`, `.ru`,
  plus `Rakefile` / `Gemfile` resolved by exact basename (the first
  adapter to ship basename-matching). Covers:
  - `module Foo::Bar` qualified-form modules + old-style nested-module
    collapse to `A::B::C`, applying the HIGH-fix from the C++ adapter:
    only NAMED children (excluding comments) count when deciding
    whether a wrapper is single-child.
  - Classes with `< Super` superclass plus `include` / `extend` /
    `prepend` mixins surfaced on the digest type header as
    `: Super, include Mod, extend Mod2`. Signature line stays
    Ruby-true (`class Foo < Bar`) so the outline doesn't synthesise
    non-Ruby syntax.
  - Methods, `def self.foo` singleton methods, and entire
    `class << self` blocks — singleton/class-method methods carry a
    `[static]` marker (mirroring how Python's `@staticmethod` is
    surfaced).
  - Full operator coverage as `KIND_OPERATOR` (arithmetic `+`/`-`/`*`/
    `/`/`%`/`**`, comparison `==`/`!=`/`<`/`>`/`<=`/`>=`/`<=>`/`===`,
    bitwise `&`/`|`/`^`/`~`, shift `<<`/`>>`, indexing `[]`/`[]=`,
    unary `-@`/`+@`/`!`).
  - `attr_accessor` / `attr_reader` / `attr_writer` — multi-symbol
    calls split into one `KIND_FIELD` per symbol with `[accessor]` /
    `[reader]` / `[writer]` marker, so each name stays grep-able.
    `alias` / `alias_method` surface as `KIND_FIELD` with `[alias]`
    marker and `new → old` signature.
  - Class- and top-level constants (`MAX_NAME_LENGTH = 64`) as fields.
  - Visibility tracked as a state machine across the class body —
    bare `private` / `public` / `protected` flips subsequent
    declarations; targeted `private :foo, :bar` and
    `private_class_method :baz` retroactively mark named methods
    (forward + backward references both supported); `private()` with
    explicit empty parens parses as a `call` node and still flips
    state correctly.
  - `require` / `require_relative` / `load` / `autoload` collected
    as imports verbatim. Lazy loads inside method / block / lambda
    bodies counted into `conditional_imports_count` so the digest's
    `[+ N conditional includes]` annotation reflects them.
- **Rails associations recognised by default** — `has_many` /
  `has_one` / `belongs_to` / `has_and_belongs_to_many` calls inside
  a class body produce one `KIND_FIELD` per symbol with
  `[has_many]` / `[has_one]` / `[belongs_to]` / `[habtm]` markers.
  Direct analogue to how the C++ adapter recognises Unreal Engine
  `UPROPERTY` macros — these name real model-to-model edges that
  dominate the value of digesting a Rails model file. Other Rails
  DSL (`validates`, `scope`, `before_action`) intentionally NOT
  recognised — the line is drawn at relations because they describe
  model-to-model edges, not behaviour.
- Adapter selection now honours an optional `basenames` attribute so
  convention-named extensionless files like `Rakefile` and `Gemfile`
  are routed to the right adapter without hijacking suffix matching.
  `get_adapter_for(path)` checks suffix first, then basename;
  `collect_files()` walker filters on either match.

### Notes

- New dependency: `tree-sitter-ruby>=0.23`.
- 55 unit tests added for the Ruby adapter; full suite now at 1039
  tests, all passing.

## [0.7.1] — 2026-05-05

### Fixed

- Canonical agent-prompt snippet (`ast-outline prompt`, the three
  README copies, `docs/agents.md`) now lists C++ extensions
  (`.cpp`/`.cc`/`.cxx`/`.h`/`.hpp`/`.hh`) alongside the rest of
  the supported set. v0.7.0 shipped the C++ adapter but the
  agent-facing snippet still claimed the tool only handled C# /
  Python / TS / Java / Kotlin / Scala / Go / Rust / PHP /
  Markdown / YAML — agents reading that snippet on a UE or
  general C++ project would skip ast-outline. Russian + Chinese
  READMEs also got the C++ row in their supported-languages
  table that was previously only in the English copy.

## [0.7.0] — 2026-05-05

### Added

- C++ adapter — parses `.cpp`, `.cc`, `.cxx`, `.c++`, `.h`, `.hpp`,
  `.hh`, `.hxx`, `.h++`, `.ipp`, `.tpp`, `.inl`, `.cppm`, `.ixx`
  via `tree-sitter-cpp`. Surfaces classes, structs, unions, enums
  (both classic and `enum class`), namespaces, free functions,
  methods, ctors, dtors, operators (including conversion operators
  like `operator bool()`), templates (header preserved as signature
  prefix), out-of-class member definitions (`Widget::draw`), and
  `#include` directives as imports. Tracks `public:` / `protected:`
  / `private:` access blocks so member visibility matches source,
  with C++-correct defaults (`class` → `private`, `struct`/`union`
  → `public`).
- Namespace collapse for C++ — single-child
  `namespace a { namespace b { … } }` chains fold into one
  `namespace a::b` declaration so the outline reads identically
  whether the source uses C++17 nested-namespace syntax or the old
  multi-level form. Multiple siblings at one level break the chain
  and stay nested in the IR. Anonymous namespaces render as
  `namespace <anonymous>`; inline namespaces keep the keyword in
  the name (`namespace inline v1`).
- Unreal Engine reflection macros recognised by default. `UCLASS(...)`
  / `USTRUCT(...)` / `UENUM(...)` / `UINTERFACE(...)` attach as
  attrs on the next type declaration; `UPROPERTY(...)` /
  `UFUNCTION(...)` attach to the next member. The
  `GENERATED_BODY()` family is stripped from the source before
  parsing (length-preserving — line numbers stay aligned) so
  tree-sitter can recover from UHT's missing-semicolon convention
  and the rest of the file outlines cleanly. Synthetic
  MISSING-`;` parse errors that tree-sitter inserts after every
  UE macro are subtracted from the reported error count, so
  valid UE headers no longer surface as `[broken]` in the digest.

## [0.6.8] — 2026-05-05

### Added

- Directory walks now respect `.gitignore` and prune a hardcoded list
  of universally non-source dirs out of the box — no flag, no config.
  Defaults cover VCS metadata (`.git/`, `.svn/`, `.hg/`), Node
  (`node_modules/`), Python caches / venvs / build metadata
  (`__pycache__/`, `.venv/`, `venv/`, `.tox/`, `.mypy_cache/`,
  `.pytest_cache/`, `.ruff_cache/`, `.eggs/`, `*.egg-info/`), JVM
  (`.gradle/`), IDE metadata (`.idea/`, `.vs/`, `.vscode/`,
  `.cursor/`, `.zed/`, `.fleet/`), JS test infra & hooks
  (`__snapshots__/`, `.husky/`), JS framework build caches
  (`.next/`, `.nuxt/`, `.svelte-kit/`, `.turbo/`, `.parcel-cache/`,
  `.vite/`), and Terraform plugin cache (`.terraform/`). Conflict-prone
  names like `build/`, `bin/`, `dist/`, `target/`, `vendor/`, `out/`,
  `obj/` are intentionally NOT in the hardcoded list — they're
  legitimate source/data dirs in some projects, so we delegate to
  `.gitignore` per-project. Ignored dirs are pruned at walk time so
  we never descend into `node_modules` just to throw the files away.
- **Nested `.gitignore` files** are now respected. A
  `.gitignore` in a subdir applies to that subtree only (patterns
  inside it are resolved relative to the subdir, mirroring
  `git`'s behavior), and a deeper `.gitignore` can override
  parent rules via `!` negation (e.g. a top-level `*.skip.py`
  un-ignored at `keep/.gitignore` with `!*.skip.py`). The
  monorepo escape hatch for hardcoded defaults works via
  git's standard three-line idiom — `!node_modules/` to
  un-exclude the dir, `node_modules/*` to re-exclude its
  contents, then a specific `!node_modules/our-fork/` to keep
  one subtree.
- **`--no-ignore` flag** on `outline` and `digest` — single
  switch to disable the entire filter pipeline (hardcoded
  defaults, `.gitignore`, `.ignore`, all of it). Walks every dir
  and filters only by supported extension. Use case: outline a
  vendored fork inside `node_modules` without editing any
  ignore files; debug "why isn't file X in my digest?". The
  `# note: ignored …` line now reads `via .gitignore/.ignore +
  defaults — pass --no-ignore to disable` so agents see both
  consulted sources and discover the flag from output rather
  than docs.
- **`.ignore` files** (the search-tool convention from `ripgrep`
  / `fd` / `ast-grep`) are now supported alongside `.gitignore`,
  with **higher priority** — patterns in `.ignore` override
  conflicting `.gitignore` patterns. Use case: hide a
  committed generated file like `schema.gen.ts` from outline /
  digest without removing it from git tracking. Or invert: keep
  a `vendor/` dir tracked but rescue one curated fork via
  `.ignore` containing `!vendor/`, `vendor/*`,
  `!vendor/our-fork/`. Nested `.ignore` files in subdirs work
  the same as nested `.gitignore` files.
- When the walker prunes any dirs, `outline` and `digest` now print
  a leading `# note: ignored N dirs (name1, name2, …) via .gitignore
  + defaults` line so an agent reading the output knows filtering
  happened **and which dir basenames got skipped** (otherwise a
  missing file looks like a bug). Unique basenames are deduplicated
  across nested occurrences (one `node_modules` listed once even if
  pruned in 12 places); the count reflects total prunes. The list is
  capped at 8 names with a `… +N more` tail in deep monorepos.
  File-level gitignore matches (e.g. a top-level file matching
  `*.generated.py`) are still filtered, just not counted in the note
  — bare "+N files" without names was confusing and rarely
  informative. On a clean directory the note is omitted.

### Internal

- New `pathspec>=0.12` dependency (MPL-2.0). Apache-2.0 compatible
  for use as a transitive dep — no relicensing impact on
  ast-outline's own code.
- `adapters.collect_files_with_stats(...)` returns a
  `CollectResult(files, ignored_dirs, ignored_dir_names)` —
  `ignored_dir_names` is a sorted tuple of unique basenames so
  callers don't have to dedupe.
  `collect_files(...)` is preserved as a thin wrapper returning just
  the list, so existing test callers don't change.
- Filter implementation uses a stack of `(anchor_dir,
  GitIgnoreSpec)` frames built top-down during `os.walk`. The stack
  is queried **deepest-first** via `pathspec.GitIgnoreSpec.check_file`
  — first frame returning `include={True,False}` decides, mirroring
  git's "more-specific gitignore overrides parent" rule. Frames
  whose anchor is no longer an ancestor of the current dir are
  pruned each iteration so sibling subtrees don't bleed
  gitignore patterns into each other.

### Tests

- New `test_ignore_filtering.py` covers hardcoded defaults
  (`node_modules`, `__pycache__`, `.git`, `.venv`/`venv`),
  `*.egg-info/` glob patterns, `.gitignore` semantics (project-root
  + ancestor fallback), cross-language junk-dir coverage (`.tox`,
  `.mypy_cache`, `.next`, `.svelte-kit`, …), modern IDE dirs
  (`.vscode`, `.cursor`, `.zed`, `.fleet`, `.vs`), nested
  `.gitignore` files (subtree-scoped patterns, sibling isolation,
  multi-level chains, the un-ignore-default monorepo idiom),
  `.ignore` file behavior (basic filtering, override of
  conflicting `.gitignore` rules, hiding tracked files, nested
  in subdirs, mixed with nested `.gitignore` at different levels),
  unreadable-`.gitignore` graceful handling, CLI surfacing of
  the `# note: ignored …` line including dedup of repeated
  basenames and `+N more` cap in deep monorepos, and the
  all-ignored-content edge case (where the agent would otherwise
  see "no files" and be misled).

## [0.6.7] — 2026-05-04

### Changed

- `ast-outline digest` legend is now **dynamic** — only entries whose
  token shape actually appears in the rendered body are listed. A
  YAML-only or markdown-only batch (whose digest contains no
  callables, kinds, markers, or inheritance) emits no legend at all,
  since none of the legend tokens apply. A code batch keeps a legend
  pruned to whichever subset of `name()=callable`,
  `name [kind]=non-callable`, `marker name()=method modifier`,
  `[N overloads]`, `[deprecated]`, `L<a>-<b>=line range`, and
  `: Base, …=inheritance` actually surfaces. Drops ~200 bytes of
  noise from yaml/md digests and shrinks per-language digests
  proportional to which tokens they don't use. Saves prompt budget
  when digest output is piped into LLM context. The omission rule
  also drops the legend when only `line_range` would fire (e.g. a
  code batch of pure marker classes with no members) — a one-entry
  legend documenting line ranges is more overhead than insight when
  the `L<n>` form is already obvious from the trailing suffixes.
- `ast-outline help digest` (`GUIDE_DIGEST`) updated to describe the
  dynamic legend behavior so agents reading the guide understand
  why a yaml-only digest has no legend line.

### Internal

- New `core._LegendFlags` dataclass + `_LEGEND_ENTRIES` table +
  `_build_legend()`. Render functions thread an optional
  `_LegendFlags` through `_digest_one`, `_member_token`,
  `_digest_yaml`, `_digest_markdown`; flags are set inline on the
  existing render pass — no second pass over data, no parsing of
  rendered output. `_DIGEST_LEGEND` constant removed.
- `_member_token` gains an optional `flags` keyword argument with a
  `None` default — backward-compatible with existing call sites
  (e.g. unit tests using `_member_token(method, count=1)`).

### Tests

- 36 new tests in `test_digest_format.py` covering: omission rules
  for pure yaml / pure markdown / yaml+markdown-mixed / empty batch /
  no-declarations / empty-types-only code; per-flag triggers
  (drop-and-include) for each of `callable`, `kind`, `marker`,
  `overloads`, `deprecated`, `inheritance`, `line_range`;
  cross-language legend-presence sweep across Python, C#, Java,
  Kotlin, Scala, Go, Rust, TypeScript, PHP; canonical entry
  ordering; comma-space separator; builder unit test (no flags /
  line-range-only / single flag / all flags); type-level
  deprecation; C# `[Obsolete]`; Rust `#[deprecated]`; regression
  guard that legend-absent digest still has a directory-header line
  at index 0.

## [0.6.6] — 2026-05-04

### Added

- `ast-outline show <file> <Symbol> --view signature` (and shorthand
  `--signature`) — header-only output: docs + attributes + the
  signature line, no method body. The mutex-grouped `--full` /
  `--view full` aliases continue to produce the existing body-extraction
  behavior; default is unchanged. Closes the gap between `digest` (just
  symbol names) and `show` (full body) for the common "after digest I
  know the name and want the contract, not the implementation" case,
  and removes the temptation for agents to pipe `show` through
  `head -80` to peek at signatures of large methods. Doc placement
  matches `outline`: `///` / JSDoc / Rust doc-comment lines render
  before the signature; Python docstrings render after the signature
  with +1 indent (`docs_inside`). Composes with the existing `--no-doc`
  flag — `--signature --no-doc` returns the bare contract line.
- `core.render_signature_view(match)` — public helper that produces the
  same header output for a `SymbolMatch`. Available for downstream
  integrations that build their own `show`-like surfaces.
- `SymbolMatch.decl: Declaration | None` — back-reference to the matched
  `Declaration`, populated by `find_symbols`. Lets callers reuse the
  adapter-extracted `docs` / `attrs` / `signature` fields without
  re-parsing the source slice. Optional with `None` default so existing
  call sites that build `SymbolMatch` by hand keep working.

### Agent-prompt snippet

- `ast-outline prompt` (and the README snippet copies — English,
  Russian, simplified Chinese, plus the docs-site `agents.md`
  `??? quote` block) get a closing line on step 3 documenting
  `--signature` as a modifier across every `show` form: "Add
  `--signature` to any of the above to return header only — useful
  after `digest`, when you have the name and want the contract, not
  the implementation." Cross-vendor invariants (no aggressive
  emphasis, no persona, outcome-first phrasing, no model-name
  pinning) all preserved per the file docstring.
- `tests/unit/test_prompt_command.py::test_snippet_fits_rough_length_budget`
  raises the soft length cap from 3000 → 3100 chars to accommodate
  the new clause; the test docstring now documents the bump and the
  bar for further growth (compress existing wording first).

## [0.6.5] — 2026-05-03

### Changed

- Agent-prompt snippet (`ast-outline prompt`, README copy, localized
  READMEs) is now explicitly cross-vendor — Claude Opus 4.7 / Sonnet
  4.6 / Haiku 4.5 **and** OpenAI GPT-5.x (5.3-codex / 5.4 / 5.5).
  Reworded the lead-in of the broad-to-narrow command list from
  "Stop at the step that answers the question" to "Pick the smallest
  of these that answers your question — they're a menu, not a
  sequence; skip straight to `show` when you already know the
  symbol." OpenAI's GPT-5.5 prompt-guide flags numbered step lists
  as interference; the new wording keeps the same meaning while
  reading as a decision tree on both vendors. Module docstring of
  `_prompt.py` now lists the cross-vendor invariants future edits
  must keep intact (no aggressive emphasis, no persona, no "think
  step by step", explicit fallbacks, no model-name pinning).

## [0.6.4] — 2026-05-03

### Fixed

- `outline` and `digest` no longer produce empty stdout when **every**
  file in a batch fails to parse. Previously per-file `# WARN`
  diagnostics went only to stderr, so an LLM agent (which reads
  stdout) saw `(no output)` and had no idea what happened. The CLI
  now prints one `# note: parse error in <path>: <err>` line per
  failed file on stdout — matching the existing convention for
  user-facing failures (path-not-found, no-adapter, etc.) — while
  the detailed `# WARN` lines still go to stderr for humans.
  Partial-failure batches (some files succeed) keep the existing
  behavior: clean outline on stdout, warnings on stderr.

## [0.6.3] — 2026-05-03

### Added

- `conditional_imports_count` is now populated by the **Python**,
  **Rust**, and **Scala** adapters in addition to PHP. The counter
  surfaces imports the adapter intentionally skipped because they
  live outside the file's static top level:
  - **Python**: `import` / `from ... import` inside a
    `function_definition` / `async_function_definition` /
    `class_definition` body. Lazy imports for circular-deps
    avoidance and class-namespace-scoped imports both count.
    Top-level `if TYPE_CHECKING:` and `try/except` import-fallbacks
    are still surfaced as static (unchanged).
  - **Rust**: `use` and `extern crate` inside `fn` bodies and
    closures. Nested `mod foo { use ... }` is NOT counted — that
    `use` belongs to the inner module's surface, not the parent
    file's.
  - **Scala**: `import` inside `function_definition` bodies (the
    concrete `def m = ...` variant; abstract `def m: T` has no body
    and can't host an `import`). Imports inside `object` / `class`
    / `trait` bodies are NOT counted — they're scoped but eager
    (load-time), not runtime.
- Java, Go, Kotlin, C#, and TypeScript leave the counter at `0` —
  their import grammars allow only top-level imports (or, for
  TypeScript ES modules, top-level only by spec; CommonJS
  `require()` is a separate concern not yet handled).

## [0.6.2] — 2026-05-03

### Added

- **PHP language adapter** (`tree-sitter-php`). Targets modern PHP 8.x
  and the still-widely-deployed 7.4 line. Recognised constructs:
  - `namespace Foo;` (file-scoped) and `namespace Foo { ... }`
    (bracketed, including the unnamed global block);
  - `class` / `abstract class` / `final class` / `readonly class` /
    `final readonly class` / `abstract readonly class`;
  - `interface` (with multi-`extends`), `trait` (mapped to
    `KIND_INTERFACE` with `native_kind="trait"` for cross-language
    search uniformity, mirroring Scala / Rust);
  - PHP 8.1 `enum` — both pure (`enum Color`) and backed
    (`enum Status: string`);
  - methods, abstract methods, `__construct` → `KIND_CTOR`,
    `__destruct` → `KIND_DTOR`;
  - PHP 8.0 constructor property promotion — promoted parameters are
    surfaced as implicit `KIND_FIELD` entries on the enclosing type
    (mirrors Kotlin's primary-ctor `val`/`var` promotion);
  - properties (single + multi-variable: `public $a, $b;`);
  - PHP 7.1+ class constants with explicit visibility, PHP 8.3 typed
    class constants (`public const string FOO = "bar"`);
  - top-level `function` and `const` declarations;
  - PHP 8.0 attributes (`#[Attr]` / `#[Attr(args)]`) collected into
    `attrs` and stripped from rendered signatures.
- File extensions handled: `.php`, `.phtml`, `.phps`, `.php8`.
- Imports: `use App\Foo`, `use App\Foo as Bar`, `use function f`,
  `use const C`, and grouped imports `use App\{A, B as Bb}` —
  group form is expanded so each `imports` entry is a single
  source-true `use ...` statement. `use` declarations inside
  bracketed namespaces are also collected.
- Pre-Composer / WordPress / Drupal-7-style legacy code uses
  `require[_once]` / `include[_once]` as the only dependency
  mechanism, so top-level `include` / `include_once` / `require` /
  `require_once` statements are also emitted as imports — preserving
  source-true expression text including computed paths
  (`require_once ABSPATH . 'wp-config.php'`). Collection is
  deliberately limited to **direct top-level statements** (children
  of `program` plus the body of bracketed namespaces). Conditional
  includes inside `if` / `try` / `switch` / `match` / loop bodies are
  out of scope, matching how every other adapter handles conditional
  imports (Python `try/except` fallbacks, `if TYPE_CHECKING`, etc.).
  `use` and `require` entries appear in source order.

### Added — common IR

- `ParseResult.conditional_imports_count: int` (default `0`) — count
  of imports the adapter intentionally skipped because they live
  outside the file's static top level. Renderers append
  `[+ 1 conditional include]` (singular) or
  `[+ N conditional includes]` (plural) to the `imports:` line when
  this is non-zero, so an agent reading a WordPress `wp-load.php`
  (whose every `require` lives in an `if`/`else` chain) sees
  `imports: [+ 6 conditional includes]` instead of an empty imports
  list — clear signal that more dependencies exist but require
  reading the file directly. Currently populated only by the PHP
  adapter; other adapters leave it at `0` (their imports are
  top-level by spec — Java, Go, C#, Kotlin, Scala — or already
  collect conditional cases like Python's `try/except` fallback).

## [0.6.1] — 2026-05-03

### Changed

- **Repository transferred** from `dim-s/ast-outline` to the
  [`ast-outline`](https://github.com/ast-outline) GitHub Organization.
  Old `dim-s/ast-outline` URLs continue to redirect.
- All canonical project URLs in `pyproject.toml`, CLI `--version`
  output, install scripts, README × 3 attribution templates, NOTICE,
  and LICENSE-DOCS updated to the new home.
- No code or behavior changes. This release exists solely to refresh
  PyPI metadata (which is frozen per-version on upload).

### Unchanged

- **Copyright** remains with Dmitrii Zaitsev (dim-s) and ast-outline
  contributors. The GitHub Organization is hosting infrastructure,
  not a new copyright holder.
- **Trademark** `ast-outline™` continues to be held by Dmitrii Zaitsev.
- Priority date for the project (2026-04-22, first public commit at
  `dim-s/ast-outline`) is unaffected and recorded in `NOTICE`.

## [0.6.0] — 2026-05-03

### License change

This release **relicenses the project from MIT to Apache License,
Version 2.0**. Documentation is separately licensed under CC BY 4.0.

| What | License | File |
| --- | --- | --- |
| Source code (v0.6.0+) | [Apache 2.0](./LICENSE) | `LICENSE` |
| Source code (v0.5.3 and earlier) | [MIT](./LICENSE-MIT) | `LICENSE-MIT` |
| Documentation & prose | [CC BY 4.0](./LICENSE-DOCS) | `LICENSE-DOCS` |

The previous MIT text is retained in `LICENSE-MIT` for compatibility
with downstream forks of the 0.5.x tree. Versions 0.5.3 and earlier
remain available on PyPI under their original MIT terms.

### Why

Apache License 2.0 provides three protections that MIT does not:

- **§4(b)** — modified files must carry a notice that they were changed,
  which makes ports and derivative works traceable;
- **§4(c)** — redistributions must include the `NOTICE` file, which
  carries forward original attribution;
- **§6** — explicit exclusion of any trademark grant. The project name
  *ast-outline* is now an unregistered trademark (™) of Dmitrii Zaitsev
  (dim-s); forks, language ports, and rebranded distributions must use a
  different name.

The change also adds an explicit patent grant (§3) which auto-terminates
on patent litigation against the project.

### Impact on existing users

- **End users running `ast-outline` from PyPI / brew:** none. CLI
  behavior is unchanged.
- **Forks / packagers:** the 0.5.x tree remains under MIT and is fully
  forkable; v0.6.0+ requires Apache 2.0 compliance (carry `LICENSE` and
  `NOTICE`).
- **Downstream projects copying code:** Apache 2.0 is compatible with
  most permissive workflows (MIT, BSD, ISC); upstreaming Apache code into
  GPLv2-only projects is not allowed by FSF, but GPLv3 is fine.

### Other changes

- Added `LICENSE-MIT` (preserves prior MIT text).
- Added `LICENSE-DOCS` (CC BY 4.0 for prose).
- Added `NOTICE` (Apache-style attribution + trademark statement).
- `pyproject.toml`: `license = { text = "Apache-2.0" }`,
  `License :: OSI Approved :: Apache Software License` classifier,
  Dmitrii Zaitsev added to `authors`, sdist now ships `LICENSE-MIT`,
  `LICENSE-DOCS`, `NOTICE`, `CHANGELOG.md`.
- README (en/ru/zh-CN): added "Project history" section, "Licensing &
  attribution" section with the three-license matrix, trademark notice.

[0.6.0]: https://github.com/ast-outline/ast-outline/releases/tag/v0.6.0
[0.6.1]: https://github.com/ast-outline/ast-outline/releases/tag/v0.6.1
