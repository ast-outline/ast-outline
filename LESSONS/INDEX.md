# LESSONS index — ast-outline

Born 2026-07-07 · first extract pass, 2 signals delivered → 1 lesson recorded, 1 held in quarantine.

## active

_(none yet)_

## candidate

| id | адресат | триггеры | мишень (кратко) |
| --- | --- | --- | --- |
| [L-0001](L-0001-docs-site-string-literal-drift.md) | executor | docs-sync, cross-repo, user-facing-string, cli | user-facing строка изменена в коде, буквальный пример в docs-сайте (сиблинг-репо) не пересинхронизирован |
| [L-0002](L-0002-declaration-kind-consumer-audit.md) | executor | new-kind, adapter, consumer-fanout, silent-bug, markdown | новый `Declaration.kind` не проаудичен по всем независимым консьюмерам (outline/digest-TOC/digest-names/show-resolver/counts/grep/JSON) — тихий (не падающий) баг |
| [L-0003](L-0003-docs-known-copies-fanout.md) | executor | docs-sync, cross-repo, readme-translation, supported-languages-table, language-adapter, digest-marker | docs-sync для user-visible факта (языковой адаптер, digest marker) закрыт только по явно названным в задаче/AGENTS.md-строке местам, пропущены переводы README и не поименованные для категории страницы докс-сайта |

## retired

_(none yet)_

## Отклонено / в карантине

- Дубль-хелпер `_common_root(files)` в `cli.py` vs уже существующий `_common_root(paths)` в `json_output.py:92` (2026-07-07, пойман code-reviewer + dmitry-programmer, HIGH) — hits=1, разовый случай; станет уроком при повторе того же механизма (новый helper написан без grep существующих примитивов проекта) на другой ситуации.
