---
id: L-0002
addressee: executor
trigger: [new-kind, adapter, consumer-fanout, silent-bug, markdown]
target: "adding a new Declaration.kind (esp. synthetic/structural kind in a markup/non-code adapter) without auditing ALL independent consumers of `.kind` across the codebase — plan's definition-of-done exercised only a subset (outline + digest-names), missing the digest-TOC default renderer and the multi-file show-resolver's grep-prefilter path; both failures were silent (wrong/missing output, no crash), not caught by tests passing"
status: candidate
evidence:
  hits: 2
  situations:
    - id: s-2026-07-11-digest-markdown-frontmatter
      provenance: "src/ast_outline/core.py:1806 (_digest_markdown) — round 2 of the markdown-frontmatter task: KIND_FRONTMATTER wired into _render_decl (outline) and _render_digest_names (digest --format=names) but not into _digest_markdown (TOC renderer used by default `digest`); metadata-only files (the primary use case for this kind) printed `[empty]` in digest. Caught by code-reviewer/manager review, not by the plan's definition-of-done."
    - id: s-2026-07-11-show-resolver-frontmatter
      provenance: "src/ast_outline/cli.py:1092-1102 (_resolve_one_symbol / multi-file show resolver) — round 3 of the same task: the grep-def-prefilter + `if not found` gate let an unrelated heading that merely contained the synthetic name's substring mask real frontmatter blocks in other files (silent data loss, not a crash). Fixed by running the synthetic-symbol scan unconditionally and unioning results instead of gating on prefilter emptiness."
  helped: null
  refuted: 0
born: 2026-07-11
last-touched: 2026-07-11
---
Правило: перед тем как считать реализацию нового `Declaration.kind` (особенно синтетического/структурного — не обычный AST-def, как markdown `frontmatter`) готовой, `grep` кодовую базу на все места, диспетчирующие по `.kind`, и явно обнови каждое — не полагайся на то, что definition-of-done плана перечислил всех консьюмеров: в этом проекте он дважды недосчитал. Известные независимые консьюмеры `Declaration.kind` в этом репо: `_render_decl` (outline), `_digest_markdown` (digest TOC, default/compact/wide), `_render_digest_names` (`digest --format=names`), `find_symbols`/`_search_walk` + `_resolve_one_symbol` (show, включая grep-def-prefilter для multi-file/синтетических имён), `_collect_counts` (счётчики), grep-путь, JSON-вывод. Проверь новый kind против каждого из этого списка, а не только против тех двух-трёх, что покрыты тестами конкретного фикса.

Почему: механизм провала — фан-аут одного enum-значения на много независимых потребителей, каждый из которых нужно явно обновить; пропуск не роняет тесты и не падает — он тихо неверно рендерит (round 2: metadata-only файл печатался как `[empty]`) или тихо теряет данные (round 3: реальные frontmatter-блоки маскировались посторонним совпадением по подстроке). Оба раза поймал ревьюер/менеджер постфактум, план готовности их не перечислял. См. также L-0003 — та же generalized форма провала (полнота проверяется по явно перечисленному, а не по полному обходу известных копий/консьюмеров факта), но на слое документации, а не code-consumer-dispatch; держим уроки раздельно, т.к. лекарства разной формы (enum consumers здесь, fixed file-checklist там). `helped: null` — список консьюмеров ещё не проверялся как чек-лист на НОВОМ добавлении kind; текущие два хита — это сам провал, а не подтверждение того, что явный аудит-по-списку предотвращает повтор.
