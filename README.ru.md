# ast-outline

[English](./README.md) · **Русский** · [简体中文](./README.zh-CN.md)

> Твой AI-агент читает файлы целиком, чтобы понять код. **`ast-outline` даёт ему
> ровно то, что нужно** — скелет всего репозитория, структуру одного файла или
> тело одного символа — вместо чтения файлов целиком. Один stateless CLI на базе
> tree-sitter: карта репозитория, аутлайн файла, извлечение символа, структурный
> grep по использованиям, трассировка импортов. Прогоны агента дешевле, быстрее
> и не тонут в больших кодовых базах.

[![tests](https://github.com/ast-outline/ast-outline/actions/workflows/tests.yml/badge.svg)](https://github.com/ast-outline/ast-outline/actions/workflows/tests.yml)
[![Code: Apache 2.0](https://img.shields.io/badge/code-Apache%202.0-blue.svg)](./LICENSE)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC%20BY%204.0-lightgrey.svg)](./LICENSE-DOCS)
[![PyPI](https://img.shields.io/pypi/v/ast-outline.svg)](https://pypi.org/project/ast-outline/)
![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)

📖 **Полная документация:** <https://ast-outline.github.io/>

---

## Зачем

LLM-агенты (Claude Code, agent-режим Cursor, Aider, Codex CLI, Gemini CLI,
Copilot Chat) исследуют код **читая файлы напрямую**. Подход надёжный, но
расточительный сразу с двух сторон: на 1200-строчном файле агент платит
1200 строк контекста только чтобы ответить *«какие здесь методы?»* — и
дальше ему ещё надо продираться через этот шум, чтобы найти реально нужный
кусок. Токенов больше, понимание хуже.

`ast-outline` — это слой-предчтение. Агент сначала зовёт его, получает
форму файла в 60–100 строк, а тела открывает только там, где они реально
нужны. Эффект двойной: **меньше токенов** в контексте — и **острее
понимание**, потому что меньше шума, через который надо продираться,
чтобы зацепиться за релевантный код. Точечный инструмент быстрее ведёт
к ответу, чем загрузка огромных файлов «на всякий случай».

**Без `ast-outline`:**

```
Агент: Read Player.cs              # 1200 строк, просто посмотреть что внутри
Агент: Read Enemy.cs               #  800 строк, просто посмотреть что внутри
Агент: grep -rn TakeDamage src/    # плоские хиты → каждый файл открывать ради scope
Агент: Read DamageSystem.cs        #  400 строк, всё ради одного метода
```

**С `ast-outline`:**

```
Агент: ast-outline digest src/Combat         # карта модуля, ~100 строк
Агент: ast-outline Player.cs                 # форма одного файла, в 2–10× меньше
Агент: ast-outline grep TakeDamage src/      # вхождения + scope, один вызов (без follow-up)
Агент: ast-outline show Player.cs TakeDamage # только тело нужного метода
```

Понимание острее (шума меньше), токенов в разы меньше, раундов в разы меньше.

---

## Кому это надо

- Ты используешь LLM-кодового агента на реальной кодовой базе и чувствуешь
  цену токенов.
- Тебе нужен **drop-in CLI**, а не очередной vector-индекс, MCP-сервер
  или daemon.
- Ты не против, чтобы агент цеплял `ast-outline` с `grep`, `find`,
  `ast-grep` по unix-style — без отдельного RAG-слоя.

Если хоть один пункт про тебя — остаток README для тебя.

---

## Установка

```bash
uv tool install ast-outline
```

Поставит `ast-outline` глобально. Нет [`uv`](https://docs.astral.sh/uv/)?

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                                          # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"       # Windows
```

<details>
<summary>Другие способы установки (pipx, pip, из исходников, скрипт-обёртка)</summary>

```bash
pipx install ast-outline
pip  install ast-outline                                          # в активный venv

# Свежий main вместо последнего PyPI-релиза:
uv tool install git+https://github.com/ast-outline/ast-outline.git

# Скрипт-обёртка (поставит и uv, если его нет):
curl -LsSf https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.sh | bash    # macOS / Linux
iwr -useb https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.ps1 | iex     # Windows
```

Обновить / удалить: `uv tool upgrade ast-outline` / `uv tool uninstall ast-outline`.

</details>

---

## За 30 секунд

```bash
# Структура одного файла
ast-outline path/to/Player.cs

# Целая папка (рекурсивно, можно мешать языки)
ast-outline src/

# Карта модуля на одной странице
ast-outline digest src/Services

# Тело одного метода (или нескольких сразу)
ast-outline show Player.cs TakeDamage
ast-outline show Player.cs TakeDamage Heal Die

# Найти все вхождения символа со scope и kind
ast-outline grep User.save src/

# Машиночитаемый JSON для тулинга / CI (работает с outline, digest, grep, show)
ast-outline digest src/Services --json

# Встроенный help
ast-outline help
```

---

## Подключение к кодовому агенту

**Это основной кейс.** Агент узнаёт про `ast-outline` из сниппета в
`AGENTS.md` / `CLAUDE.md` / `GEMINI.md`. Два пути установки.

**Автоматически (рекомендуется).** Внутри Claude Code / Codex CLI / Gemini CLI /
Cursor скажи агенту:

> Запусти `ast-outline setup-prompt` и следуй инструкциям.

Агент проверит установку, выберет правильный context-файл (`AGENTS.md` —
кросс-инструментальный дефолт; `CLAUDE.md` / `GEMINI.md` — для одного
вендора), допишет сниппет в маркеры
`<!-- ast-outline:start --> ... <!-- ast-outline:end -->` (diff-aware на
повторных запусках, твои правки не затирает) и опционально пропатчит
exploration-субагенты в `.claude/agents/` / `.codex/agents/` / `.gemini/agents/`.

**Вручную.** Запиши тот же сниппет куда нужно:

```bash
ast-outline prompt >> AGENTS.md
ast-outline prompt | pbcopy   # буфер обмена в macOS
```

> **Важно — субагенты Claude Code.** `CLAUDE.md` / `AGENTS.md` доходят
> только до **главного агента**. Встроенные субагенты (`Explore` и т. п.)
> видят только собственный system prompt. Перекрой их файлом
> `.claude/agents/Explore.md` с выводом `ast-outline prompt` в body.
> Cursor, Aider и прямые API-клиенты вложенных субагентов не имеют —
> там `CLAUDE.md` достаточно.

---

## Поддерживаемые языки

| Язык       | Расширения |
| ---        | --- |
| C#         | `.cs` |
| C++        | `.cpp`, `.cc`, `.cxx`, `.c++`, `.h`, `.hpp`, `.hh`, `.hxx`, `.h++`, `.ipp`, `.tpp`, `.inl`, `.cppm`, `.ixx` *(вкл. Unreal Engine `UCLASS` / `UFUNCTION` / `GENERATED_BODY`)* |
| Python     | `.py`, `.pyi` |
| TypeScript | `.ts`, `.tsx` |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` *(парсится TS-грамматикой)* |
| Java       | `.java` |
| Kotlin     | `.kt`, `.kts` |
| Scala      | `.scala`, `.sc` *(Scala 2 + Scala 3)* |
| Go         | `.go` |
| Rust       | `.rs` |
| PHP        | `.php`, `.phtml`, `.phps`, `.php8` *(PHP 8.x + 7.4 LTS; протестировано на ядре WordPress)* |
| Ruby       | `.rb`, `.rake`, `.gemspec`, `.ru`, `Rakefile`, `Gemfile` *(вкл. Rails-ассоциации)* |
| Elixir     | `.ex`, `.exs` *(`defmodule` → вложенные пространства имён, `defprotocol` / `defimpl`, маркеры `defmacro` / `defguard` / `defdelegate`, поля `defstruct`, `@type` / `@callback`, DSL-блоки ExUnit / Phoenix как именованные контейнеры)* |
| Lua        | `.lua`, `.wlua` *(чистый Lua 5.1–5.4; `function M:foo` → метод, метаметоды → оператор; покрывает Neovim, LÖVE, OpenResty, Redis-скрипты)* |
| GDScript   | `.gd` *(Godot 4 + Godot 3; рукописный парсер — готового tree-sitter-грамматики нет; `class_name`/`extends` объединяются в неявный класс скрипта, signals → события, `@export`/`@onready` → атрибуты, свойства с `get`/`set`/`setget`, `preload` → импорты)* |
| Swift      | `.swift` *(структуры, enum, протоколы, extension, actor; дженерики и соответствие протоколам)* |
| CSS        | `.css` |
| SCSS       | `.scss` |
| SQL        | `.sql` *(основной target — PostgreSQL; MySQL / SQLite — рабочие)* |
| HTML       | `.html`, `.htm` *(элементы рендерятся как CSS-селекторные токены — `section#hero`, `form[action=/x]`; `<link rel=stylesheet>` / `<script src>` собираются как импорты; `<script>` / `<style>` / `<!-- -->` по умолчанию отфильтрованы из grep)* |
| Vue        | `.vue` *(Single-File Components — `<template>` парсится как HTML, `<script>` как TypeScript, `<style>` как CSS; три секции сливаются в один outline; без доп. зависимости — переиспользует существующие грамматики)* |
| Markdown   | `.md`, `.markdown`, `.mdx`, `.mdown` *(заголовки → оглавление, fenced-блоки кода по заголовкам; YAML-frontmatter показывается как узел `--- frontmatter` и хэндл `show <file> frontmatter`)* |
| YAML       | `.yaml`, `.yml` *(автоопределение Kubernetes / OpenAPI / GitHub Actions)* |

Подробности по каждому адаптеру (какие конструкции распознаются, как
рендерится наследование, что собирается в imports, …) — на docs-сайте:
<https://ast-outline.github.io/>.

Добавление нового языка — один новый файл в
[`src/ast_outline/adapters/`](src/ast_outline/adapters/); чек-лист — в
[AGENTS.md](./AGENTS.md).

---

## Команды

Каждая команда принимает один или несколько путей (файлы и папки, разные
языки вперемешку — OK). Полный справочник флагов и формата вывода —
[в документации](https://ast-outline.github.io/commands/).

- **`outline <paths…>`** — команда по умолчанию: всё, что не является
  подкомандой, запускает её, вместе с флагами (`ast-outline --no-docs x.java`
  работает). Сигнатуры с диапазонами
  `L<start>-<end>`, без тел. Флаг `--imports` добавит в шапку строку с
  `import` / `use` / `using` в нативном синтаксисе языка. Фильтры:
  `--no-private`, `--no-fields`, `--no-docs`, `--no-attrs`.

- **`digest <paths…>`** — карта модуля на одной странице. У каждого файла
  size-метка (`[tiny]` / `[medium]` / `[large]` / `[huge]`) и оценка
  токенов; в шапках типов — наследование (`: Base, Trait`) и декораторы
  (`@dataclass`, `[ApiController]`). Первая строка вывода — самообъясняющая
  легенда: LLM читает её cold. Файлы `[huge]` (≥100k токенов) сворачиваются
  до строки заголовка.

- **`show <file> <Symbol> [Symbol…]`** — достать одно или несколько тел
  по имени. Для кода — суффиксный матчинг (`Foo.Bar` найдёт `*.Foo.Bar`);
  для Markdown — регистронезависимая подстрока в заголовке; для YAML —
  точечный путь по ключам; для CSS/SCSS — токен селектора; для SQL —
  имя таблицы или `table.column`. `--signature` вернёт только заголовок.
  Первым аргументом можно передать **каталог или glob** — `show src/ User`
  сам найдёт файл, отдельный поиск не нужен.

- **`grep <pattern> <paths…>`** — AST-aware структурный поиск. Матчи
  сгруппированы по enclosing class / function, с тегами `[def]` /
  `[import]` (calls и refs идут без тега — наличие `(` после имени и так
  делает их очевидными). Шум комментариев и строк фильтруется по
  умолчанию. POSIX-флаги `-e` (многопаттерн за один обход), `-w`, `-l`,
  `-c`, `-m`, `-i` работают как в `grep` / `rg`. Regex автоопределяется.
  `--kind def|call|ref|import` сужает по классификации — деление
  синтаксическое: `call` — это идентификатор перед `(`, `ref` — всякое
  другое упоминание (у метода почти все использования попадут в `call`).
  Чтобы посчитать все использования, не задавайте `--kind`, а читайте
  `kind_counts` из `--json`.

- **`prompt`** — печатает каноничный сниппет для агент-контекста (его
  использует и `setup-prompt`). Ручной путь установки:
  `ast-outline prompt >> AGENTS.md`.

- **`setup-prompt`** — печатает install-time чек-лист, по которому LLM-агент
  проводит тебя через подключение `ast-outline` к твоему стеку. Сама
  CLI ничего не пишет на диск — все правки делает агент своими родными
  инструментами, каждое изменение можно отревьюить.

- **`help [topic]`** — встроенный справочник.

**Машиночитаемый вывод (`--json`).** Каждая структурная команда — `outline`,
`digest`, `grep`, `show` — принимает `--json`: вместо текста печатается один
JSON-документ с фиксированным envelope (`tool` / `schema_version` /
`command`). Это для **программных** потребителей — editor-плагинов, CI-гейтов,
скриптов — которым нужен стабильный парсимый контракт, а не токен-плотный
текстовый формат (он остаётся дефолтом для людей и агентов). `--json` — чистый
**переключатель кодирования**: content-фильтрующие флаги (`--no-private`,
`--include-fields`, `--view`, …) действуют так же, как и на текстовый вывод;
JSON-эквивалента нет только у layout-флагов (`--no-lines`, пресеты `--format`,
`-l` / `-c`). Ошибки тоже печатаются JSON-объектом `error`, так что stdout
*всегда* валидный JSON. Полная схема по полям:
[формат вывода → JSON](https://ast-outline.github.io/output-format/#json-output).

> **CLI-контракт по exit-кодам.** User-facing ошибки (файл не найден, нет
> совпадений, кривой аргумент) печатают строку `# note: …` в **stdout** и
> возвращают `0`. Это сделано осознанно: ненулевые коды ломают параллельные
> bash-батчи в агент-харнессах. Реальные внутренние крэши проходят как
> обычно.

---

## Дизайн

- **Stateless.** Никакого индекса, кеша, embeddings, сети. Парсит по
  запросу, печатает, выходит.
- **AST, не regex.** Поверх
  [tree-sitter](https://tree-sitter.github.io/) — заголовки типов несут
  реальное `: Base, Trait` наследование, `show` находит настоящий символ,
  упоминания в комментариях и строковых литералах не дают ложных
  срабатываний.
- **MCP-сервера нет.** Для stateless CLI у агента больше рычагов, когда
  он пайпит и параллелит её в `bash`, чем через MCP-обёртку с теми же
  вызовами.

Название вдохновлено [ast-grep](https://github.com/ast-grep/ast-grep) —
оба инструмента поверх tree-sitter, но решают разное: ast-grep переписывает
код по структурным паттернам, ast-outline — карта и поиск по символам для
чтения человеком или агентом.

---

## Разработка

```bash
git clone https://github.com/ast-outline/ast-outline.git
cd ast-outline
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                  # весь сьют
.venv/bin/ast-outline tests/sample.py
```

Адаптеры — в [`src/ast_outline/adapters/`](src/ast_outline/adapters/);
фикстуры — в `tests/fixtures/<lang>/`; per-adapter тесты — в
`tests/unit/test_<lang>_adapter.py`. Новое поведение приходит с тестом.
Добавляешь язык? Чек-лист в [AGENTS.md](./AGENTS.md) (вместе меняются
пять файлов).

---

## Лицензии и атрибуция

| Что | Лицензия |
| --- | --- |
| **Код** v0.6.0+ | [Apache 2.0](./LICENSE) |
| **Код** ≤ v0.5.3 | [MIT](./LICENSE-MIT) *(сохранён для downstream-форков)* |
| **Документация и проза** (README, CLI-помощь, prompt-сниппет, легенда digest) | [CC BY 4.0](./LICENSE-DOCS) |

Обе лицензии пермиссивные — форк, коммерческое использование, портирование.
Разделение делает требования атрибуции явными. Если ты переиспользуешь
нетривиальные фрагменты этой документации, CC BY 4.0 просит видимой
ссылки:

> Based on [ast-outline](https://github.com/ast-outline/ast-outline) by
> Dmitrii Zaitsev (dim-s), licensed under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Copyright © 2026 **Dmitrii Zaitsev** ([dim-s](https://github.com/dim-s))
и контрибьюторы ast-outline. GitHub-организация `ast-outline` — это
хостинговая инфраструктура, а не правообладатель.

История (релизы, переименования, смена лицензии) —
[CHANGELOG.md](./CHANGELOG.md) и
[GitHub Releases](https://github.com/ast-outline/ast-outline/releases).
