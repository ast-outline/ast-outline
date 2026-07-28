# LESSONS index — ast-outline

Born 2026-07-07 · 5 lessons (1 active, 4 candidate), 3 signals in quarantine. Full `consolidate` never run yet — not yet needed at this size.

## active

| id | адресат | триггеры | мишень (кратко) |
| --- | --- | --- | --- |
| [L-0003](L-0003-docs-known-copies-fanout.md) | executor | docs-sync, cross-repo, readme-translation, supported-languages-table, language-adapter, digest-marker | docs-sync для user-visible факта (языковой адаптер, digest marker) закрыт только по явно названным в задаче/AGENTS.md-строке местам, пропущены переводы README и не поименованные для категории страницы докс-сайта |

## candidate

| id | адресат | триггеры | мишень (кратко) |
| --- | --- | --- | --- |
| [L-0001](L-0001-docs-site-string-literal-drift.md) | executor | docs-sync, cross-repo, user-facing-string, cli | user-facing строка изменена в коде, буквальный пример в docs-сайте (сиблинг-репо) не пересинхронизирован |
| [L-0002](L-0002-declaration-kind-consumer-audit.md) | executor | new-kind, adapter, consumer-fanout, silent-bug, markdown | новый `Declaration.kind` не проаудичен по всем независимым консьюмерам (outline/digest-TOC/digest-names/show-resolver/counts/grep/JSON) — тихий (не падающий) баг |
| [L-0004](L-0004-source-text-shape-implicit-contracts.md) | executor | line-endings, crlf, source-bytes, normalize, windows, cross-platform, byte-offsets, parse-result-source, strip-cr, review-finding-low | правка формы прочитанного исходника (нормализация CRLF/CR) принята по общему принципу и локальной зелени одной ОС — ломает неявные контракты на форму (byte-identity `ParseResult.source` + однострочная сигнатура YAML) |
| [L-0005](L-0005-existing-primitive-grep-before-new.md) | executor | new-helper, new-constant, cli-py, duplicate-primitive, kind-set, path-helper, code-reviewer-finding | новый хелпер/константа в `cli.py` дословно дублирует существующий примитив `core.py`/`json_output.py` — искали по имени, а совпадает содержимое |

## retired

_(none yet)_

## Отклонено / в карантине

- ~~Дубль-хелпер `_common_root` (2026-07-07)~~ — повторился 2026-07-28 (`_CTOR_OWNER_KINDS` vs `core.TYPE_KINDS`), выпущен из карантина в [L-0005](L-0005-existing-primitive-grep-before-new.md).
- Предвалидация проверяет ЧАСТИЧНУЮ форму, а исполняется КОМБИНИРОВАННАЯ: два `-e` паттерна с одинаковым именем regex-группы роняли `re.error` наружу из `grep()` (exit 1, нарушение exit-0 инварианта AGENTS.md), т.к. валидатор компилировал паттерны по одному (s-5c0274, code-reviewer BUG/HIGH) — hits=1; станет уроком при повторе механизма «валидируется не тот объект, который потом собирает рантайм» на другой ситуации.
- Отчёт исполнителя разошёлся с территорией на фоновом гейте (заявлено «задачи не переведены в done», `done-check` показал все 6 `status:done`; перевод случился ПОСЛЕ отправки конверта, конверт не дописан) — s-5c0274, hits=1 внутри одной сессии; станет уроком при независимом повторе + выразимом лекарстве (сейчас формулируется как увещевание «дописывай конверт», §1.6).
