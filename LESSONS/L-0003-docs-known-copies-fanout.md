---
id: L-0003
addressee: executor
trigger: [docs-sync, cross-repo, readme-translation, supported-languages-table, language-adapter, digest-marker]
target: "docs-sync for a user-visible capability change (new language adapter, new digest marker/kind, format change) is declared complete against the file(s) explicitly named by the task/ticket or by AGENTS.md's per-category sync-table row alone — silently skips this repo's translated READMEs (README.ru.md, README.zh-CN.md) and/or docs-site pages not named for that row (docs/index.md, docs/history.md), because AGENTS.md's 'when to also update the docs site' table is scoped per change-category and does not enumerate every known copy for every category, and it never mentions this repo's own translated READMEs at all"
status: candidate
evidence:
  hits: 4
  situations:
    - id: s-2026-05-17-lua-readme-miss
      provenance: "commit f963bb7 (2026-05-17) — 'docs(README): add Lua row ... missed in the v0.9.0 release pass'; README.md's supported-languages table left stale at the v0.9.0 release, caught and fixed after the fact."
    - id: s-2026-07-03-elixir-main-readme-miss
      provenance: "commit 297aaa8 (v1.8.0 Elixir adapter, 2026-07-03) shipped without updating README.md; commit 35f4beb (same day) — 'Follow-up to v1.8.0 — the Elixir adapter shipped but was missing from the README language table.'"
    - id: s-2026-07-03-translated-readme-batch-drift
      provenance: "commit a7c9f80 (2026-07-03) — 'README.ru.md and README.zh-CN.md had drifted behind the English table — missing Elixir, GDScript, HTML and Vue.' Four separate prior language releases (v1.0.0 HTML, v1.5.0 GDScript, v1.7.0 Vue, v1.8.0 Elixir) each updated README.md but not the translated copies; caught and batch-fixed in one sweep, not per-release."
    - id: s-2026-07-11-ao3-docs-sync-partial-scope
      provenance: "session 2026-07-11, AO-3 markdown-frontmatter/digest-marker task, r5 per dmitry-manager ledger: 'докс-синк AO-3 заявлен полным, реально закрыв только прямо названные в задаче места (README.md, output-format.md, commands.md), пропустив параллельные копии той же таблицы/факта (index.md, history.md, README.ru/zh-CN)'. Matches AGENTS.md's table exactly: the 'digest marker' row names only docs/output-format.md, not docs/index.md or docs/history.md — the task's scope was already AGENTS.md-table-consistent and still incomplete. Caught by dmitry-manager review, not by the task's definition-of-done."
  helped: null
  refuted: 0
born: 2026-07-11
last-touched: 2026-07-11
---
Правило: при любом изменении user-visible capability-факта этого проекта (новый языковой адаптер, новый digest marker/kind, изменение формата вывода) — прежде чем считать docs-sync завершённым, сверься с ПОЛНЫМ известным списком копий этого факта, а не только с файлами, явно названными в задаче, и не только со строкой таблицы AGENTS.md «when to also update the docs site» для этой категории (она бьёт по категориям и не покрывает все копии для каждой категории). Известные копии в этом проекте:
- `README.md`, `README.ru.md`, `README.zh-CN.md` (этот репо) — таблица supported-languages/features дрейфует в переводах НЕЗАВИСИМО от английской версии; AGENTS.md вообще не упоминает `README.ru.md`/`README.zh-CN.md` ни в одной строке таблицы;
- `../ast-outline.github.io/docs/index.md`, `docs/output-format.md`, `docs/commands.md`, `docs/history.md` (докс-сайт) — не все перечислены для каждой категории (напр. строка «digest marker» называет только `output-format.md`).

Проверь факт против каждого файла из этого списка явно (checklist, не полагание на память/тикет), а не только против тех, что названы в конкретной строке таблицы AGENTS.md для этой категории изменения или в тексте задачи.

Почему: механизм провала — один факт существует в N физических копиях (переводы README + несколько страниц докс-сайта), а и тикет, и таблица синхронизации в AGENTS.md называют лишь ПОДМНОЖЕСТВО этих копий для данной категории изменения; оба источника молчаливо занижают полноту одинаковым образом. Минимум 4 независимых случая за историю проекта одним и тем же механизмом (v0.9.0 Lua, v1.8.0 Elixir main README, батч-дрейф 4 языков в переводах, AO-3 2026-07-11) — не импортированный троп, а хронический паттерн именно этого проекта из-за его специфичной cross-repo + мультиязычной docs-структуры. Та же форма провала, что и L-0002 (полнота проверяется по явно перечисленному, а не по полному обходу известных копий/консьюмеров факта), но на слое документации, а не code-consumer-dispatch — держим раздельно, т.к. лекарство разной формы (fixed file-checklist здесь, а не grep-по-фрагменту как в L-0001, и не enum consumers как в L-0002). `helped: null` — список копий выше формулируется впервые этим уроком; ещё не проверялся как чек-лист на новом docs-sync.
