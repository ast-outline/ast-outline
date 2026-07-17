---
id: L-0001
addressee: executor
trigger: [docs-sync, cross-repo, user-facing-string, cli]
target: "user-facing string literal edited in this repo's code (error/note/help text); its verbatim example in the sibling docs-site repo (../ast-outline.github.io/docs/) is left stale — caught late by human review, never by CI"
status: candidate
evidence:
  hits: 2
  situations:
    - id: s-2025-9de1594
      provenance: "commit 9de1594 — README's AGENT_PROMPT snippet drifted from the canonical prompt in code; fixed by adding a drift guard, which was later retired in commit 5cece35 ('sync contract is now docs-site agents.md only') once the README copy was dropped"
    - id: s-2026-07-07-show-glob
      provenance: "session 2026-07-07, src/ast_outline/cli.py — `show` glob-expansion `lead_note` text (\"glob expanded by the shell to N file(s) ...\") was reworded for tone during the `show`-glob-detection fix; the literal example of this note in the sibling repo's docs/commands.md was left with the old wording. Caught by the reviewing manager cross-checking the f-string against the doc example — not by the owner, not by any automated check."
  helped: null
  refuted: 0
born: 2026-07-07
last-touched: 2026-07-07
---
Правило: при изменении текста ЛЮБОЙ user-facing строки-литерала в этом репо (текст ноты, error-сообщения, help) — в том же проходе `grep` отличительный фрагмент этой строки по `../ast-outline.github.io/docs/*.md`. Если находится буквальный пример со старым текстом — обнови его в docs-репо в этом же проходе, не полагаясь только на таблицу "when to also update the docs site" в AGENTS.md (её строки — про категории изменений: новый флаг, новый marker, новый адаптер; точечная переформулировка уже существующей строки под эти категории не подпадает, и обе зафиксированные ситуации проскочили именно через этот зазор).

Почему: механизм провала — карта (буквальный пример в docs) расходится с территорией (f-string в коде) при переформулировке текста, потому что источник правды в этом репо, а копия примера — в отдельном, не авто-подтягиваемом репо. AGENTS.md уже фиксирует межрепо-структуру и категории синхронизации, но не покрывает "поменялась формулировка существующей строки" как триггер для grep конкретно докс-сайта. Два независимых попадания одним механизмом (README-снимок промпта в 2025, note-текст `show` в этом сеансе) — оба словлены проверкой человека постфактум, а не превентивно.
