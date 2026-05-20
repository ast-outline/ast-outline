# ast-outline

[English](./README.md) · [Русский](./README.ru.md) · **简体中文**

> 一个无状态 CLI 工具，打印源码的**结构骨架** —— 类、方法、签名、行号范围 ——
> 但**不包含方法体**。同时提供带 scope 与 kind 注解的 AST 感知结构化
> code-grep。它的存在是为了让 LLM 编码代理不再为了回答 *"这个文件里都有什么？"*
> 而读取整个文件。

[![Code: Apache 2.0](https://img.shields.io/badge/code-Apache%202.0-blue.svg)](./LICENSE)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC%20BY%204.0-lightgrey.svg)](./LICENSE-DOCS)
[![PyPI](https://img.shields.io/pypi/v/ast-outline.svg)](https://pypi.org/project/ast-outline/)
![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)

📖 **完整文档:** <https://ast-outline.github.io/>

---

## 为什么需要它

LLM 编码代理（Claude Code、Cursor 的 Agent 模式、Aider、Codex CLI、Gemini CLI、
Copilot Chat）通过**直接读取文件**探索代码库。这种方式可靠，但是双重浪费：
为了回答 *"这里都有哪些方法？"*，一个 1200 行的文件就要花掉 1200 行的上下文 ——
而且这些噪声进入上下文之后，代理还得在其中翻找真正相关的部分。token 成本上升，
理解质量下降。

`ast-outline` 是一个预读层。代理先调用它，在 60–100 行内拿到文件的"骨架"，
只对真正需要的方法体再去打开源码。带来的好处是双重的：上下文里的
**token 更少**，而且**理解也更准** —— 不必在大段无关代码中打捞，代理能更快
锁定真正相关的部分。把精确的工具用在刀刃上，比"先把大文件全读进来再说"
更快得到答案。

**之前：**

```
Agent: Read Player.cs              # 1200 行，只为看看里面有什么
Agent: Read Enemy.cs               #  800 行，只为看看里面有什么
Agent: grep -rn TakeDamage src/    # 一堆扁平命中 → 每个文件都要打开看 scope
Agent: Read DamageSystem.cs        #  400 行，只为读一个方法
```

**用了 `ast-outline` 之后：**

```
Agent: ast-outline digest src/Combat         # 整个模块地图，~100 行
Agent: ast-outline Player.cs                 # 单文件骨架，2–10× 更小
Agent: ast-outline grep TakeDamage src/      # 用法 + scope，一次调用（无需后续读取）
Agent: ast-outline show Player.cs TakeDamage # 只取那一个方法体
```

理解更准（噪声更少），token 量和往返次数都是原来的零头。

---

## 适用对象

- 你在真实代码库上使用 LLM 编码代理，并对 token 成本有切身感受。
- 你想要一个 **drop-in CLI**，而不是又一个向量索引、MCP 服务器或 daemon。
- 你乐意让代理把 `ast-outline` 与 `grep`、`find`、`ast-grep` 用 unix 风格
  串起来用 —— 不必引入专门的 RAG 层。

只要符合其中任一项，本 README 的剩余部分就是为你写的。

---

## 安装

```bash
uv tool install ast-outline
```

将 `ast-outline` 全局安装。还没装 [`uv`](https://docs.astral.sh/uv/)？

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                                          # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"       # Windows
```

<details>
<summary>其他安装方式（pipx、pip、源码安装、一键脚本）</summary>

```bash
pipx install ast-outline
pip  install ast-outline                                          # 在已激活的 venv 中

# 安装 main 分支最新代码：
uv tool install git+https://github.com/ast-outline/ast-outline.git

# 一键安装脚本（如未安装 uv 会顺带安装）：
curl -LsSf https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.sh | bash    # macOS / Linux
iwr -useb https://raw.githubusercontent.com/ast-outline/ast-outline/main/scripts/install.ps1 | iex     # Windows
```

升级 / 卸载：`uv tool upgrade ast-outline` / `uv tool uninstall ast-outline`。

</details>

---

## 30 秒上手

```bash
# 单个文件的结构化大纲
ast-outline path/to/Player.cs

# 整个目录（递归，可混合多种语言）
ast-outline src/

# 模块的单页地图
ast-outline digest src/Services

# 提取某个方法的源码（也可一次提取多个）
ast-outline show Player.cs TakeDamage
ast-outline show Player.cs TakeDamage Heal Die

# 找到符号的所有出现位置，附带 scope + kind
ast-outline grep User.save src/

# 面向工具链 / CI 的机器可读 JSON（outline、digest、grep、show 都支持）
ast-outline digest src/Services --json

# 内置帮助
ast-outline help
```

---

## 接入你的编码代理

**这是本工具的主要使用场景。** 代理通过你的 `AGENTS.md` / `CLAUDE.md` /
`GEMINI.md` 中的一段提示语了解到 `ast-outline`。两种安装方式：

**自动（推荐）。** 在 Claude Code / Codex CLI / Gemini CLI / Cursor 中告诉代理：

> 运行 `ast-outline setup-prompt` 并按它的指示操作。

代理会验证安装、为你的工具栈挑选合适的上下文文件（跨工具默认值是
`AGENTS.md`；单一厂商使用 `CLAUDE.md` / `GEMINI.md`），把片段写入
`<!-- ast-outline:start --> ... <!-- ast-outline:end -->` 标记之间
（重复运行时是 diff-aware 的，不会覆盖你的手工编辑），并可选地为
`.claude/agents/` / `.codex/agents/` / `.gemini/agents/` 中的探索类
子代理打补丁。

**手动。** 把同一段片段管道写入需要的位置：

```bash
ast-outline prompt >> AGENTS.md
ast-outline prompt | pbcopy   # macOS 剪贴板
```

> **注意：Claude Code 的子代理。** `CLAUDE.md` / `AGENTS.md` 只能影响
> **主代理**。隔离的内置子代理（如 `Explore`）只能看到自己的 system prompt。
> 通过 `.claude/agents/Explore.md`（body 中放 `ast-outline prompt` 的输出）
> 来覆盖它们。Cursor、Aider 和直接调用 API 的客户端没有嵌套子代理 —— 在
> 那里 `CLAUDE.md` 就够了。

---

## 支持的语言

| 语言       | 扩展名 |
| ---        | --- |
| C#         | `.cs` |
| C++        | `.cpp`、`.cc`、`.cxx`、`.c++`、`.h`、`.hpp`、`.hh`、`.hxx`、`.h++`、`.ipp`、`.tpp`、`.inl`、`.cppm`、`.ixx` *（含 Unreal Engine `UCLASS` / `UFUNCTION` / `GENERATED_BODY`）* |
| Python     | `.py`、`.pyi` |
| TypeScript | `.ts`、`.tsx` |
| JavaScript | `.js`、`.jsx`、`.mjs`、`.cjs` *（由 TypeScript 语法解析）* |
| Java       | `.java` |
| Kotlin     | `.kt`、`.kts` |
| Scala      | `.scala`、`.sc` *(Scala 2 + Scala 3)* |
| Go         | `.go` |
| Rust       | `.rs` |
| PHP        | `.php`、`.phtml`、`.phps`、`.php8` *(PHP 8.x + 7.4 LTS；已在 WordPress 核心上验证)* |
| Ruby       | `.rb`、`.rake`、`.gemspec`、`.ru`、`Rakefile`、`Gemfile` *（含 Rails 关联）* |
| CSS        | `.css` |
| SCSS       | `.scss` |
| SQL        | `.sql` *（主要面向 PostgreSQL；MySQL / SQLite 可用）* |
| Markdown   | `.md`、`.markdown`、`.mdx`、`.mdown` |
| YAML       | `.yaml`、`.yml` *（自动识别 Kubernetes / OpenAPI / GitHub Actions）* |

各适配器识别的具体语法（识别哪些结构、如何渲染继承、什么会被收集到
imports，等等）可在文档站点查看：<https://ast-outline.github.io/>。

新增语言只需在
[`src/ast_outline/adapters/`](src/ast_outline/adapters/)
中加一个文件；配套清单见 [AGENTS.md](./AGENTS.md)。

---

## 命令

每个命令都接受一个或多个路径（文件和目录，可混合不同语言）。完整的
flag 列表与输出格式参考见
[文档](https://ast-outline.github.io/commands/)。

- **`outline <paths…>`** —— 默认命令。打印签名 + `L<start>-<end>` 行号范围，
  不含方法体。加 `--imports` 会在文件头多一行，按语言原生语法列出
  `import` / `use` / `using`。过滤选项：`--no-private`、`--no-fields`、
  `--no-docs`、`--no-attrs`。

- **`digest <paths…>`** —— 模块单页地图。每个文件带大小标签
  （`[tiny]` / `[medium]` / `[large]` / `[huge]`）和 token 估算；类型行
  携带继承（`: Base, Trait`）和装饰器（`@dataclass`、`[ApiController]`）。
  输出第一行是自描述的 legend，便于 LLM 在没有上下文时直接读懂。
  `[huge]` 文件（≥100k tokens）只会折叠成头部一行。

- **`show <file> <Symbol> [Symbol…]`** —— 按名字提取一个或多个方法体。
  代码采用后缀匹配（`Foo.Bar` 匹配 `*.Foo.Bar`）；Markdown 用大小写不
  敏感的标题子串；YAML 用点分键路径；CSS / SCSS 用选择器 token；SQL 用
  表名或 `table.column`。`--signature` 只返回头部。

- **`grep <pattern> <paths…>`** —— AST 感知的结构化搜索。匹配按所属
  class / function 分组，附带 kind 标签 `[def]` / `[import]`（calls 与
  refs 不带标签 —— 符号后是否有 `(` 已经一望而知）。注释和字符串噪声默认
  过滤。POSIX flag `-e`（一次遍历多模式）、`-w`、`-l`、`-c`、`-m`、`-i`
  与 `grep` / `rg` 行为一致。Regex 会自动识别。`--kind def|call|ref|import`
  按分类筛选。

- **`prompt`** —— 打印规范的代理上下文片段（`setup-prompt` 内部也用它）。
  手动安装路径：`ast-outline prompt >> AGENTS.md`。

- **`setup-prompt`** —— 打印一份 install-time 清单，让 LLM 代理引导你把
  `ast-outline` 接入你的工具栈。CLI 自身不写文件 —— 所有改动都是代理用
  自己的工具完成的，每一步都可审查。

- **`help [topic]`** —— 内置使用指南。

**机器可读输出（`--json`）。** 每个结构化命令 —— `outline`、`digest`、`grep`、
`show` —— 都接受 `--json`：用单个 JSON 文档替代文本输出，带固定的 envelope
（`tool` / `schema_version` / `command`）。它面向**程序化**消费方 —— 编辑器
插件、CI 闸门、脚本 —— 这些场景需要稳定、可解析的契约，而不是 token 密集的
文本格式（文本格式仍是人类与代理的默认输出）。JSON 模式是**无损的** —— 内容
过滤类 flag 会被忽略，始终输出完整 IR —— 错误也以 JSON `error` 对象输出，
因此 stdout *始终*是合法 JSON。完整的逐字段 schema 见
[输出格式 → JSON](https://ast-outline.github.io/output-format/#json-output)。

> **CLI 退出码约定。** 面向用户的错误（文件不存在、无匹配、参数错误）会
> 在 **stdout** 打印一行 `# note: …` 并返回 `0`。这是有意为之 ——
> 非零退出码会破坏代理 harness 中的并行 `bash` 批处理。真正的内部崩溃
> 仍按常规传播。

---

## 设计

- **无状态。** 无索引、无缓存、无 embeddings、无网络。按需解析、打印、
  退出。
- **AST，而不是正则。** 基于 [tree-sitter](https://tree-sitter.github.io/)
  —— 类型行带有真实的 `: Base, Trait` 继承关系，`show` 命中真正的符号，
  注释和字符串字面量不会引发误判。
- **不做 MCP 服务器。** 对一个无状态 CLI 来说，代理在 `bash` 里串管道、
  并行调用，比通过 MCP 包装层调用同一组命令更有杠杆。

命名灵感来自 [ast-grep](https://github.com/ast-grep/ast-grep) —— 两者
都基于 tree-sitter，但解决不同的问题：ast-grep 用结构化模式重写代码，
ast-outline 在符号层面对代码进行概览和搜索，方便人或代理阅读。

---

## 本地开发

```bash
git clone https://github.com/ast-outline/ast-outline.git
cd ast-outline
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                  # 完整测试套件
.venv/bin/ast-outline tests/sample.py
```

适配器位于 [`src/ast_outline/adapters/`](src/ast_outline/adapters/)；
fixture 位于 `tests/fixtures/<lang>/`；每个适配器的测试位于
`tests/unit/test_<lang>_adapter.py`。新行为应附带测试。新增语言时请参考
[AGENTS.md](./AGENTS.md)（共有五个文件需要联动修改）。

---

## 许可与署名

| 内容 | 许可证 |
| --- | --- |
| **源代码** v0.6.0+ | [Apache 2.0](./LICENSE) |
| **源代码** ≤ v0.5.3 | [MIT](./LICENSE-MIT) *(为下游 fork 保留)* |
| **文档与文字内容**（README、CLI 帮助、prompt 片段、digest 图例） | [CC BY 4.0](./LICENSE-DOCS) |

两类许可证都很宽松 —— 可以 fork、商用、移植到其他语言。拆分只是为了
让署名要求明确。如果你复用了文档中较实质的部分，CC BY 4.0 要求显式
署名：

> Based on [ast-outline](https://github.com/ast-outline/ast-outline) by
> Dmitrii Zaitsev (dim-s), licensed under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Copyright © 2026 **Dmitrii Zaitsev**（[dim-s](https://github.com/dim-s)）
与 ast-outline 贡献者。GitHub 组织 `ast-outline` 仅承担托管职责，并非
新的著作权持有者。

历史信息（发布、改名、许可证切换）请见
[CHANGELOG.md](./CHANGELOG.md) 与
[GitHub Releases](https://github.com/ast-outline/ast-outline/releases)。
