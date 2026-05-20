# ast-outline

**English** · [Русский](./README.ru.md) · [简体中文](./README.zh-CN.md)

> Stateless CLI that prints the **structural shape** of a source file — classes,
> methods, signatures, line ranges — without method bodies. Plus an AST-aware
> structural code-grep with scope and kind annotations. Built so LLM coding
> agents stop reading whole files just to answer *"what's in here?"*.

[![Code: Apache 2.0](https://img.shields.io/badge/code-Apache%202.0-blue.svg)](./LICENSE)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC%20BY%204.0-lightgrey.svg)](./LICENSE-DOCS)
[![PyPI](https://img.shields.io/pypi/v/ast-outline.svg)](https://pypi.org/project/ast-outline/)
![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)

📖 **Full documentation:** <https://ast-outline.github.io/>

---

## Why

LLM coding agents (Claude Code, Cursor agent mode, Aider, Codex CLI, Gemini CLI,
Copilot Chat) explore codebases by **reading files directly**. Reliable, but
wasteful in two ways: a 1200-line file costs 1200 lines of context just to answer
*"what methods are in here?"* — and once the noise is in context, the agent has
to wade through it to find the part that actually matters. Token cost goes up,
comprehension goes down.

`ast-outline` is a pre-reading layer. The agent calls it first, gets the file's
shape in 60–100 lines, and only opens the bodies it actually needs. The win is
double: **fewer tokens** in context, and **sharper comprehension** — less noise
to filter through means the agent locks onto the relevant code faster and
answers with less drift.

**Before:**

```
Agent: Read Player.cs              # 1200 lines, just to see what's here
Agent: Read Enemy.cs               #  800 lines, just to see what's here
Agent: grep -rn TakeDamage src/    # flat hits → open each file for scope
Agent: Read DamageSystem.cs        #  400 lines, all to read one method
```

**With `ast-outline`:**

```
Agent: ast-outline digest src/Combat         # whole module map, ~100 lines
Agent: ast-outline Player.cs                 # one file's shape, 2–10× smaller
Agent: ast-outline grep TakeDamage src/      # uses + scope, one call (no follow-ups)
Agent: ast-outline show Player.cs TakeDamage # just that one method body
```

Sharper understanding (less noise to filter), a fraction of the tokens, a fraction of the round-trips.

---

## Who this is for

- You use an LLM coding agent on a real codebase and feel the token cost.
- You want a **drop-in CLI**, not another vector index, MCP server, or daemon.
- You're happy with the agent chaining `ast-outline` with `grep`, `find`,
  `ast-grep` — Unix-style — instead of a bespoke RAG layer.

If you fit any of those, the rest of this README is for you.

---

## Install

```bash
uv tool install ast-outline
```

Installs the `ast-outline` CLI globally. No [`uv`](https://docs.astral.sh/uv/)?

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                                          # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"       # Windows
```

<details>
<summary>Other install paths (pipx, pip, source, bundled script)</summary>

```bash
pipx install ast-outline
pip  install ast-outline                                          # into an active venv

# Latest main instead of the PyPI release:
uv tool install git+https://github.com/ast-outline/ast-outline.git

# Bundled one-shot installer (also installs uv if missing):
curl -LsSf https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.sh | bash    # macOS / Linux
iwr -useb https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.ps1 | iex     # Windows
```

Update / uninstall: `uv tool upgrade ast-outline` / `uv tool uninstall ast-outline`.

</details>

---

## 30-second tour

```bash
# Structural outline of one file
ast-outline path/to/Player.cs

# Outline a whole directory (recursive, mixed languages OK)
ast-outline src/

# Compact one-page map of a module
ast-outline digest src/Services

# Pull the source of one method (or several at once)
ast-outline show Player.cs TakeDamage
ast-outline show Player.cs TakeDamage Heal Die

# Find every place a symbol appears, with scope + kind
ast-outline grep User.save src/

# Machine-readable JSON for tooling / CI (works on outline, digest, grep, show)
ast-outline digest src/Services --json

# Built-in guide
ast-outline help
```

---

## Wire it into your coding agent

This is the main use case. The agent learns about `ast-outline` from a snippet
in your `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`. Two paths to install it.

**Automatic (recommended).** Inside Claude Code / Codex CLI / Gemini CLI / Cursor,
ask the agent:

> Run `ast-outline setup-prompt` and follow its instructions.

The agent verifies the install, picks the right context file for your tooling
(`AGENTS.md` cross-tool default, `CLAUDE.md` / `GEMINI.md` for single-vendor),
appends the snippet inside `<!-- ast-outline:start --> ... <!-- ast-outline:end -->`
markers (diff-aware on re-run, won't overwrite your edits), and optionally patches
exploration subagents in `.claude/agents/` / `.codex/agents/` / `.gemini/agents/`.

**Manual.** Pipe the same snippet wherever you want:

```bash
ast-outline prompt >> AGENTS.md
ast-outline prompt | pbcopy   # macOS clipboard
```

> **Heads up — Claude Code subagents.** `CLAUDE.md` / `AGENTS.md` reach the
> *main* agent only. Built-in subagents like `Explore` see only their own system
> prompt; shadow them with `.claude/agents/Explore.md` containing the
> `ast-outline prompt` body. Cursor, Aider, and direct API clients have no
> isolated subagents — `CLAUDE.md` is enough there.

---

## Supported languages

| Language   | Extensions |
| ---        | --- |
| C#         | `.cs` |
| C++        | `.cpp`, `.cc`, `.cxx`, `.c++`, `.h`, `.hpp`, `.hh`, `.hxx`, `.h++`, `.ipp`, `.tpp`, `.inl`, `.cppm`, `.ixx` *(incl. Unreal Engine `UCLASS` / `UFUNCTION` / `GENERATED_BODY`)* |
| Python     | `.py`, `.pyi` |
| TypeScript | `.ts`, `.tsx` |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` *(parsed by the TypeScript grammar)* |
| Java       | `.java` |
| Kotlin     | `.kt`, `.kts` |
| Scala      | `.scala`, `.sc` *(Scala 2 + Scala 3)* |
| Go         | `.go` |
| Rust       | `.rs` |
| PHP        | `.php`, `.phtml`, `.phps`, `.php8` *(PHP 8.x + 7.4 LTS; tested on WordPress core)* |
| Ruby       | `.rb`, `.rake`, `.gemspec`, `.ru`, `Rakefile`, `Gemfile` *(incl. Rails associations)* |
| Lua        | `.lua`, `.wlua` *(vanilla 5.1–5.4; `function M:foo` → method, metamethods → operator; covers Neovim, LÖVE, OpenResty, Redis scripts)* |
| CSS        | `.css` |
| SCSS       | `.scss` |
| SQL        | `.sql` *(PostgreSQL primary; MySQL / SQLite usable)* |
| Markdown   | `.md`, `.markdown`, `.mdx`, `.mdown` |
| YAML       | `.yaml`, `.yml` *(Kubernetes / OpenAPI / GitHub Actions detected)* |

Per-adapter feature detail (which constructs each adapter recognises, how
inheritance is rendered, what's collected as imports, …) lives in the docs
site: <https://ast-outline.github.io/>.

Adding a new language is one new file under
[`src/ast_outline/adapters/`](src/ast_outline/adapters/) — see
[AGENTS.md](./AGENTS.md) for the lockstep checklist.

---

## Commands

Each command takes one or more paths (files or directories, mixed languages
fine). All flags and the full output format reference live in
[the docs](https://ast-outline.github.io/commands/).

- **`outline <paths…>`** — default. Signatures with `L<start>-<end>` line
  ranges, no bodies. Add `--imports` to surface each file's
  `import` / `use` / `using` line in native syntax. Filters: `--no-private`,
  `--no-fields`, `--no-docs`, `--no-attrs`.

- **`digest <paths…>`** — one-page module map. Each file gets a size label
  (`[tiny]` / `[medium]` / `[large]` / `[huge]`) and a token estimate; type
  headers carry inheritance (`: Base, Trait`) and decorators (`@dataclass`,
  `[ApiController]`). The first line of output is a self-describing legend so
  an LLM reads it cold. `[huge]` files (≥100k tokens) collapse to header-only.

- **`show <file> <Symbol> [Symbol…]`** — extract one or more bodies by name.
  Suffix matching for code (`Foo.Bar` matches `*.Foo.Bar`); case-insensitive
  substring for Markdown headings; dotted key path for YAML; selector token for
  CSS / SCSS; table or `table.column` for SQL. `--signature` returns header
  only.

- **`grep <pattern> <paths…>`** — AST-aware structural search. Matches grouped
  by enclosing class / function, with kind tags `[def]` / `[import]` (calls and
  refs render untagged — the `(` after the symbol makes them obvious). Comment
  / string noise filtered by default. POSIX flags `-e` (multi-pattern, one
  walk), `-w`, `-l`, `-c`, `-m`, `-i` work as in `grep` / `rg`. Regex is
  auto-detected. `--kind def|call|ref|import` narrows by classification.

- **`prompt`** — print the canonical agent-context snippet (used by
  `setup-prompt`). Manual install path:
  `ast-outline prompt >> AGENTS.md`.

- **`setup-prompt`** — emit an install-time checklist for an LLM agent to
  walk you through wiring `ast-outline` into your tooling. The CLI itself
  does no file I/O — every edit is performed by the agent using its own tools,
  so each change is reviewable.

- **`help [topic]`** — built-in usage guide.

**Machine-readable output (`--json`).** Every structural command — `outline`,
`digest`, `grep`, `show` — accepts `--json`, swapping the text format for a
single JSON document with a fixed envelope (`tool` / `schema_version` /
`command`). It's for **programmatic** consumers — editor plugins, CI gates,
scripts — that want a stable, parseable contract instead of the token-dense
text format (which stays the default for humans and agents). JSON mode is
**lossless** — content-filtering flags are ignored, the complete IR is always
emitted — and failures are emitted as a JSON `error` object too, so stdout is
*always* valid JSON. Full per-field schema:
[output format → JSON](https://ast-outline.github.io/output-format/#json-output).

> **CLI exit-code contract.** User-facing failures (file not found, no match,
> bad arg) print a `# note: …` line to **stdout** and exit `0`. This is
> deliberate — non-zero exits break parallel `bash` batches in agent harnesses.
> Real internal crashes still propagate normally.

---

## Design

- **Stateless.** No index, no cache, no embeddings, no network. Parse on
  demand, print, exit.
- **AST, not regex.** Built on
  [tree-sitter](https://tree-sitter.github.io/) — type headers carry real
  `: Base, Trait` inheritance, `show` finds the actual symbol, comments and
  string literals don't trigger false positives.
- **No MCP server.** For a stateless CLI an agent gets more leverage piping
  and parallelising it in `bash` than through an MCP shim wrapping the same
  calls.

Naming inspired by [ast-grep](https://github.com/ast-grep/ast-grep) — both
build on tree-sitter, but ast-grep rewrites code with structural patterns,
ast-outline maps and searches it for human / agent reading.

---

## Development

```bash
git clone https://github.com/ast-outline/ast-outline.git
cd ast-outline
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                  # full suite
.venv/bin/ast-outline tests/sample.py
```

Adapters live under [`src/ast_outline/adapters/`](src/ast_outline/adapters/);
fixtures under `tests/fixtures/<lang>/`; per-adapter tests under
`tests/unit/test_<lang>_adapter.py`. New behaviour ships with a test. Adding a
language? See [AGENTS.md](./AGENTS.md) for the checklist (five files change
together).

---

## License & attribution

| What | License |
| --- | --- |
| **Code** v0.6.0+ | [Apache 2.0](./LICENSE) |
| **Code** ≤ v0.5.3 | [MIT](./LICENSE-MIT) (preserved for downstream forks) |
| **Documentation & prose** (READMEs, CLI help, prompt snippet, digest legend) | [CC BY 4.0](./LICENSE-DOCS) |

Both licenses are permissive — fork, ship commercially, port. The split makes
attribution requirements explicit. If you reuse non-trivial prose from this
documentation, CC BY 4.0 asks for visible credit:

> Based on [ast-outline](https://github.com/ast-outline/ast-outline) by
> Dmitrii Zaitsev (dim-s), licensed under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Copyright © 2026 **Dmitrii Zaitsev** ([dim-s](https://github.com/dim-s)) and
ast-outline contributors. The `ast-outline` GitHub org is hosting only.

For history (releases, renames, license change), see
[CHANGELOG.md](./CHANGELOG.md) and
[GitHub Releases](https://github.com/ast-outline/ast-outline/releases).
