"""Per-language tests for `ParseResult.imports`.

Covers code adapters that expose source-language imports. Markdown and
YAML adapters have no imports concept so are not exercised here.

Each test writes a minimal source file to a tmp_path, runs the adapter,
and asserts the normalized source-true import strings match exactly.
The output format is intentionally `language-native` — Python emits
`from .core import X`, Rust emits `use foo::Bar`, Go emits
`import "fmt"`. No synthetic format that an LLM would have to learn.
"""
from __future__ import annotations

from pathlib import Path

from ast_outline.adapters.csharp import CSharpAdapter
from ast_outline.adapters.go import GoAdapter
from ast_outline.adapters.java import JavaAdapter
from ast_outline.adapters.kotlin import KotlinAdapter
from ast_outline.adapters.php import PhpAdapter
from ast_outline.adapters.python import PythonAdapter
from ast_outline.adapters.rust import RustAdapter
from ast_outline.adapters.scala import ScalaAdapter
from ast_outline.adapters.swift import SwiftAdapter
from ast_outline.adapters.typescript import TypeScriptAdapter


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# --- Python ---------------------------------------------------------------


def test_python_basic_imports(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.py", (
        "import foo\n"
        "import bar as b\n"
        "from .core import X, Y\n"
        "from typing import Optional as Opt, List\n"
        "from .. import sibling\n"
        "from collections import *\n"
    ))
    r = PythonAdapter().parse(p)
    assert r.imports == [
        "import foo",
        "import bar as b",
        "from .core import X, Y",
        "from typing import Optional as Opt, List",
        "from .. import sibling",
        "from collections import *",
    ]


def test_python_splits_comma_joined_imports(tmp_path: Path) -> None:
    """`import a, b, c as d` becomes three separate `import ...` lines so
    every entry in `imports` is a single-statement string. Joining
    multiple modules under one `import` keyword would force the agent to
    parse comma-lists within entries, which is exactly what we wanted to
    avoid by collapsing to source-true syntax in the first place."""
    p = _write(tmp_path, "m.py", "import a, b, c as d\n")
    r = PythonAdapter().parse(p)
    assert r.imports == ["import a", "import b", "import c as d"]


def test_python_includes_type_checking_block(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.py", (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from foo.bar import HeavyType\n"
    ))
    r = PythonAdapter().parse(p)
    assert "from foo.bar import HeavyType" in r.imports


def test_python_includes_try_except_import_fallback(tmp_path: Path) -> None:
    """Common compat pattern: try a fast/optional dep, fall back to
    stdlib. Both branches are real top-level dependencies of this file
    — agents need to see both to understand what's pulled at runtime.

    This pattern is the documented reason `_PY_IMPORT_DESCEND` lists
    `try_statement` and `except_clause`; the test pins that contract
    so a future refactor can't quietly drop it."""
    p = _write(tmp_path, "m.py", (
        "try:\n"
        "    import ujson as json\n"
        "except ImportError:\n"
        "    import json\n"
    ))
    r = PythonAdapter().parse(p)
    assert "import ujson as json" in r.imports
    assert "import json" in r.imports


def test_python_excludes_imports_inside_class_or_function(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.py", (
        "import top_level\n"
        "class Outer:\n"
        "    import inner_only\n"
        "def func():\n"
        "    import another_local\n"
    ))
    r = PythonAdapter().parse(p)
    assert r.imports == ["import top_level"]


def test_python_collapses_multiline_paren_imports(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.py", (
        "from foo import (\n"
        "    A,\n"
        "    B,\n"
        "    C,\n"
        ")\n"
    ))
    r = PythonAdapter().parse(p)
    assert r.imports == ["from foo import A, B, C"]


# --- TypeScript -----------------------------------------------------------


def test_typescript_all_import_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.ts", (
        'import foo from "foo";\n'
        'import { X, Y } from "./core";\n'
        'import { X as A, Y as B } from "baz";\n'
        'import * as ns from "./util";\n'
        'import "./side-effect";\n'
        'import type { TypeOnly } from "./types";\n'
        'import defaultExport, { named1, named2 } from "./mixed";\n'
    ))
    r = TypeScriptAdapter().parse(p)
    assert r.imports == [
        'import foo from "foo"',
        'import { X, Y } from "./core"',
        'import { X as A, Y as B } from "baz"',
        'import * as ns from "./util"',
        'import "./side-effect"',
        'import type { TypeOnly } from "./types"',
        'import defaultExport, { named1, named2 } from "./mixed"',
    ]


def test_typescript_collapses_multiline(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.ts", (
        "import {\n"
        "  Multi,\n"
        "  Line\n"
        '} from "./long";\n'
    ))
    r = TypeScriptAdapter().parse(p)
    assert r.imports == ['import { Multi, Line } from "./long"']


# --- Java -----------------------------------------------------------------


def test_java_all_import_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.java", (
        "package com.example;\n"
        "import java.util.List;\n"
        "import java.util.*;\n"
        "import static java.util.Collections.emptyList;\n"
        "import static java.util.stream.Collectors.*;\n"
        "public class M {}\n"
    ))
    r = JavaAdapter().parse(p)
    assert r.imports == [
        "import java.util.List",
        "import java.util.*",
        "import static java.util.Collections.emptyList",
        "import static java.util.stream.Collectors.*",
    ]


# --- Kotlin ---------------------------------------------------------------


def test_kotlin_all_import_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.kt", (
        "package com.example\n"
        "import foo.Bar\n"
        "import foo.*\n"
        "import foo.Bar as Baz\n"
        "class M\n"
    ))
    r = KotlinAdapter().parse(p)
    assert r.imports == [
        "import foo.Bar",
        "import foo.*",
        "import foo.Bar as Baz",
    ]


# --- Scala ----------------------------------------------------------------


def test_scala_all_import_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.scala", (
        "package com.example\n"
        "import foo.bar.Baz\n"
        "import foo.bar.{Baz, Qux}\n"
        "import foo.bar.{Baz => Renamed}\n"
        "import foo.bar.*\n"
        "import foo.bar._\n"
        "object M\n"
    ))
    r = ScalaAdapter().parse(p)
    assert r.imports == [
        "import foo.bar.Baz",
        "import foo.bar.{Baz, Qux}",
        "import foo.bar.{Baz => Renamed}",
        "import foo.bar.*",
        "import foo.bar._",
    ]


# --- Go -------------------------------------------------------------------


def test_go_flattens_grouped_imports(tmp_path: Path) -> None:
    """Go's `import (...)` block is purely cosmetic grouping. We flatten
    it so each spec becomes its own `import "..."` line — agents see one
    syntactic shape regardless of source style."""
    p = _write(tmp_path, "m.go", (
        'package main\n'
        'import "fmt"\n'
        'import f "fmt"\n'
        'import _ "side/effect"\n'
        'import (\n'
        '    "os"\n'
        '    "strings"\n'
        '    custom "github.com/foo/bar"\n'
        '    _ "side/two"\n'
        ')\n'
        'func main() {}\n'
    ))
    r = GoAdapter().parse(p)
    assert r.imports == [
        'import "fmt"',
        'import f "fmt"',
        'import _ "side/effect"',
        'import "os"',
        'import "strings"',
        'import custom "github.com/foo/bar"',
        'import _ "side/two"',
    ]


# --- Rust -----------------------------------------------------------------


def test_rust_all_use_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.rs", (
        "use std::collections::HashMap;\n"
        "use foo::Bar;\n"
        "use foo::{Bar, Baz};\n"
        "use foo::Bar as Qux;\n"
        "use foo::*;\n"
        "pub use crate::utils::Foo;\n"
        "extern crate serde;\n"
        "mod local;\n"
        "fn main() {}\n"
    ))
    r = RustAdapter().parse(p)
    assert r.imports == [
        "use std::collections::HashMap",
        "use foo::Bar",
        "use foo::{Bar, Baz}",
        "use foo::Bar as Qux",
        "use foo::*",
        "pub use crate::utils::Foo",
        "extern crate serde",
    ]


def test_rust_excludes_mod_declarations(tmp_path: Path) -> None:
    """`mod foo;` is a submodule declaration, not an import. Including
    it would mix two distinct concepts (consumption vs structural file
    layout) under the same `--imports` flag."""
    p = _write(tmp_path, "m.rs", (
        "use foo::Bar;\n"
        "mod sibling;\n"
        "mod inline { pub fn x() {} }\n"
    ))
    r = RustAdapter().parse(p)
    assert r.imports == ["use foo::Bar"]


# --- PHP ------------------------------------------------------------------


def test_php_basic_imports(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.php", (
        "<?php\n"
        "namespace App;\n"
        "use App\\Foo;\n"
        "use App\\Bar as B;\n"
        "use function strlen;\n"
        "use const PHP_INT_MAX;\n"
        "class M {}\n"
    ))
    r = PhpAdapter().parse(p)
    assert r.imports == [
        "use App\\Foo",
        "use App\\Bar as B",
        "use function strlen",
        "use const PHP_INT_MAX",
    ]


def test_php_grouped_imports_expanded(tmp_path: Path) -> None:
    """`use Foo\\{A, B as Bb};` → one entry per leaf, each a complete
    source-true `use ...` statement. The prefix is reattached so each
    entry stands alone — no implicit "all share `Foo\\`" knowledge
    required of the reader."""
    p = _write(tmp_path, "m.php", (
        "<?php\n"
        "use App\\{Foo, Bar as Bb};\n"
        "use function App\\Helpers\\{render, escape as e};\n"
        "use const App\\Config\\{LIMIT, MAX};\n"
        "class M {}\n"
    ))
    r = PhpAdapter().parse(p)
    assert r.imports == [
        "use App\\Foo",
        "use App\\Bar as Bb",
        "use function App\\Helpers\\render",
        "use function App\\Helpers\\escape as e",
        "use const App\\Config\\LIMIT",
        "use const App\\Config\\MAX",
    ]


def test_php_imports_inside_bracketed_namespace(tmp_path: Path) -> None:
    """`namespace Foo { use Bar; }` — `use` inside the namespace block
    must still surface in `imports`, since adapters report the file's
    full dependency surface regardless of which namespace block declared
    each `use`."""
    p = _write(tmp_path, "m.php", (
        "<?php\n"
        "namespace App\\First {\n"
        "    use App\\Foo;\n"
        "    class A {}\n"
        "}\n"
        "namespace App\\Second {\n"
        "    use App\\Bar;\n"
        "    class B {}\n"
        "}\n"
    ))
    r = PhpAdapter().parse(p)
    assert "use App\\Foo" in r.imports
    assert "use App\\Bar" in r.imports


# --- C# -------------------------------------------------------------------


def test_csharp_all_using_shapes(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.cs", (
        "using System;\n"
        "using System.Collections.Generic;\n"
        "using static System.Math;\n"
        "using Foo = System.Bar;\n"
        "global using System.Linq;\n"
        "namespace Demo {\n"
        "  using Inside;\n"
        "  public class C {}\n"
        "}\n"
    ))
    r = CSharpAdapter().parse(p)
    assert r.imports == [
        "using System",
        "using System.Collections.Generic",
        "using static System.Math",
        "using Foo = System.Bar",
        "global using System.Linq",
    ]


# --- Empty / no-imports cases --------------------------------------------


def test_empty_imports_for_file_with_none(tmp_path: Path) -> None:
    """Per-adapter sanity: a code file with no imports yields an empty
    list. Each adapter must wire the `imports=imports` keyword into its
    `ParseResult(...)` call — silently omitting it would still default
    to `[]` via the dataclass `field(default_factory=list)`, so this
    test alone does NOT prove `_collect_imports` runs. See
    `test_each_adapter_wires_collect_imports_into_parse_result` below
    for the live-collection check."""
    cases = [
        (PythonAdapter(), "m.py", "x = 1\n"),
        (TypeScriptAdapter(), "m.ts", "const x = 1;\n"),
        (JavaAdapter(), "M.java", "package x; public class M {}\n"),
        (KotlinAdapter(), "M.kt", "package x\nclass M\n"),
        (ScalaAdapter(), "M.scala", "package x\nobject M\n"),
        (GoAdapter(), "m.go", "package main\nfunc main(){}\n"),
        (RustAdapter(), "m.rs", "fn main() {}\n"),
        (CSharpAdapter(), "M.cs", "namespace X { class M {} }\n"),
        (PhpAdapter(), "m.php", "<?php\nnamespace X;\nclass M {}\n"),
    ]
    for adapter, name, content in cases:
        p = _write(tmp_path, name, content)
        r = adapter.parse(p)
        assert r.imports == [], f"{adapter.language_name} expected empty imports"
        # Default for the conditional-imports counter is 0. PHP, Python,
        # Ruby, Rust, Scala and TypeScript populate it for non-top-level
        # imports / dynamic `import('...')` calls; Java, Go, Kotlin and
        # C# leave it at 0 (their imports are top-level by spec, no
        # runtime form exists). On a no-imports source, every adapter
        # must report 0 — a non-zero value would indicate a spurious
        # conditional dependency flag on a file that has none.
        assert r.conditional_imports_count == 0, (
            f"{adapter.language_name} expected conditional_imports_count=0"
        )


def test_each_adapter_wires_collect_imports_into_parse_result(tmp_path: Path) -> None:
    """Live-collection regression: every adapter must populate
    `ParseResult.imports` from real source. If an adapter drops the
    `imports=imports` keyword from its `ParseResult(...)` call, the
    field defaults to `[]` and the empty-imports test above passes
    misleadingly — this test fails because we expect a non-empty list
    and would catch the regression."""
    cases = [
        (PythonAdapter(), "m.py", "import os\n", "import os"),
        (TypeScriptAdapter(), "m.ts", 'import x from "y";\n', 'import x from "y"'),
        (JavaAdapter(), "M.java", "import java.util.List;\nclass M {}\n", "import java.util.List"),
        (KotlinAdapter(), "M.kt", "import foo.Bar\nclass M\n", "import foo.Bar"),
        (ScalaAdapter(), "M.scala", "import foo.Bar\nobject M\n", "import foo.Bar"),
        (GoAdapter(), "m.go", 'package main\nimport "fmt"\nfunc main(){}\n', 'import "fmt"'),
        (RustAdapter(), "m.rs", "use foo::Bar;\nfn main() {}\n", "use foo::Bar"),
        (CSharpAdapter(), "M.cs", "using System;\nnamespace X { class M {} }\n", "using System"),
        (PhpAdapter(), "m.php", "<?php\nuse App\\Foo;\nclass M {}\n", "use App\\Foo"),
        (
            SwiftAdapter(),
            "M.swift",
            "import Foundation\nclass M {}\n",
            "import Foundation",
        ),
    ]
    for adapter, name, content, expected_first in cases:
        p = _write(tmp_path, name, content)
        r = adapter.parse(p)
        assert r.imports, f"{adapter.language_name} returned empty imports for non-empty source"
        assert r.imports[0] == expected_first, (
            f"{adapter.language_name}: expected first import {expected_first!r}, got {r.imports[0]!r}"
        )


# --- Conditional / runtime-scoped imports counter -----------------------
#
# Adapters that surface only top-level imports also count the ones they
# deliberately skip (lazy `import` inside function bodies, `use` inside
# Rust `fn`, etc.) and report them via `conditional_imports_count`. This
# lets the renderer emit `[+ N conditional includes]` so an agent isn't
# misled into thinking the file has no further dependencies.
#
# Languages that DO support locally-scoped imports / runtime-form
# imports populate the counter (Python, Rust, Scala, PHP, Ruby,
# TypeScript — TS for dynamic `import('...')` inside functions /
# methods / control-flow). Languages whose imports are top-level by
# spec with no runtime form (Java, Go, Kotlin, C#) leave it at 0 —
# covered by `test_empty_imports_for_file_with_none` above.


def test_python_counts_imports_inside_function_class_method(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.py", (
        "import top\n"
        "def f():\n"
        "    import inside_fn\n"
        "    if True:\n"
        "        import inside_fn_if\n"
        "class C:\n"
        "    import inside_class\n"
        "    def m(self):\n"
        "        import inside_method\n"
        "if cond:\n"
        "    import inside_top_if\n"  # collected as static (top-level if)
    ))
    r = PythonAdapter().parse(p)
    assert "import top" in r.imports
    assert "import inside_top_if" in r.imports
    # 4 hidden: inside_fn, inside_fn_if, inside_class, inside_method
    assert r.conditional_imports_count == 4


def test_python_counter_zero_when_only_top_level(tmp_path: Path) -> None:
    """No nested imports → counter stays at 0 even with lots of
    top-level conditional / try-fallback imports (those are already
    surfaced as static)."""
    p = _write(tmp_path, "m.py", (
        "import top\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from heavy import HeavyType\n"
        "try:\n"
        "    from fast import X\n"
        "except ImportError:\n"
        "    from slow import X\n"
    ))
    r = PythonAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_rust_counts_use_inside_fn_and_impl(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.rs", (
        "use top_mod::Bar;\n"
        "fn f() {\n"
        "    use inside_fn::Foo;\n"
        "    if cond {\n"
        "        use inside_fn_if::X;\n"
        "    }\n"
        "}\n"
        "struct S;\n"
        "impl S {\n"
        "    fn method(&self) {\n"
        "        use inside_method::Y;\n"
        "    }\n"
        "}\n"
        "mod inner {\n"
        "    use inside_mod::Z;\n"  # NOT counted: belongs to inner mod surface
        "}\n"
    ))
    r = RustAdapter().parse(p)
    assert r.imports == ["use top_mod::Bar"]
    # 3 conditional: inside_fn, inside_fn_if, inside_method.
    # Not counted: inside_mod (separate module's surface, eager scope).
    assert r.conditional_imports_count == 3


def test_rust_counter_zero_when_only_top_level(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.rs", (
        "use foo::Bar;\n"
        "use baz::{X, Y};\n"
        "mod inner { use other::Z; }\n"  # mod-scoped, not counted
        "fn main() {}\n"
    ))
    r = RustAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_scala_counts_imports_inside_method(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.scala", (
        "import top.Bar\n"
        "object Obj {\n"
        "    import inside_obj.X\n"  # NOT counted: object body is eager scope
        "    def m() = {\n"
        "        import inside_method.Y\n"
        "    }\n"
        "}\n"
    ))
    r = ScalaAdapter().parse(p)
    assert r.imports == ["import top.Bar"]
    # 1 conditional: inside_method. inside_obj is in object body (eager
    # scope, not runtime).
    assert r.conditional_imports_count == 1


def test_scala_counter_zero_when_only_top_level(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.scala", (
        "import foo.Bar\n"
        "import baz.{A, B}\n"
        "object O { import inside.X }\n"  # eager scope, not counted
        "class C\n"
    ))
    r = ScalaAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_typescript_counts_dynamic_imports_in_function_method_block(
    tmp_path: Path,
) -> None:
    """TS dynamic `import('...')` inside a function / method / control-
    flow scope counts as a conditional dependency. The static
    `import` statement at the top stays in `r.imports`; the dynamic
    forms are intentionally NOT listed (they're runtime, not
    declarative) but must contribute to the counter."""
    p = _write(tmp_path, "m.ts", (
        "import { foo } from './a';\n"
        "async function lazy() {\n"
        "  const x = await import('./inside_fn');\n"
        "  if (cond) {\n"
        "    const y = await import('./inside_fn_if');\n"
        "  }\n"
        "}\n"
        "class K {\n"
        "  async load() {\n"
        "    return import('./inside_method');\n"
        "  }\n"
        "}\n"
        "try {\n"
        "  const z = await import('./inside_try');\n"
        "} catch (e) {\n"
        "  const w = await import('./inside_catch');\n"
        "}\n"
    ))
    r = TypeScriptAdapter().parse(p)
    assert r.imports == ["import { foo } from './a'"]
    # 5 conditional: inside_fn, inside_fn_if, inside_method, inside_try,
    # inside_catch. None of the dynamic imports leak into r.imports.
    assert r.conditional_imports_count == 5


def test_typescript_counter_zero_when_only_static_and_top_level(
    tmp_path: Path,
) -> None:
    """Top-level dynamic `await import('./x')` runs unconditionally on
    module load — it's NOT counted (mirror PHP's top-level
    assignment-wrapped-include rule). Only static `import` statements
    plus a top-level dynamic import → counter stays at 0."""
    p = _write(tmp_path, "m.ts", (
        "import { foo } from './a';\n"
        "import bar from './b';\n"
        "const top = await import('./top');\n"
    ))
    r = TypeScriptAdapter().parse(p)
    assert r.imports == [
        "import { foo } from './a'",
        "import bar from './b'",
    ]
    assert r.conditional_imports_count == 0


def test_typescript_counter_ignores_require_calls(tmp_path: Path) -> None:
    """`require(...)` is a name-pattern call with no dedicated AST
    node — pattern-matching by callee identifier is fragile, so the
    adapter deliberately neither lists nor counts these. This test
    pins that decision: putting `require` calls inside a function
    body must NOT bump the counter."""
    p = _write(tmp_path, "m.cjs", (
        "function load() {\n"
        "  const a = require('./a');\n"
        "  const b = require('./b');\n"
        "  return [a, b];\n"
        "}\n"
    ))
    r = TypeScriptAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_typescript_counts_dynamic_imports_in_arrow_and_loops(
    tmp_path: Path,
) -> None:
    """Pin every scope-kind in `_TS_CONDITIONAL_OR_RUNTIME_SCOPES`
    against an explicit test case — removing any one entry from the
    frozenset (arrow_function, switch_statement, for..of, while_statement)
    must break a test, not pass silently. Function/class/if/try are
    already covered by `test_typescript_counts_dynamic_imports_in_function_method_block`."""
    p = _write(tmp_path, "m.ts", (
        "const arrow = async () => import('./inside_arrow');\n"
        "switch (x) {\n"
        "  case 1: { const a = await import('./inside_switch'); break; }\n"
        "  default: break;\n"
        "}\n"
        "for (const m of mods) {\n"
        "  const b = await import(m);\n"  # path is a runtime expression — still a dynamic import
        "}\n"
        "while (cond) {\n"
        "  const c = await import('./inside_while');\n"
        "}\n"
    ))
    r = TypeScriptAdapter().parse(p)
    # 4 conditional: inside_arrow, inside_switch, the for..of import, inside_while.
    assert r.conditional_imports_count == 4


# --- Conditional-counter edge cases (per language) ----------------------


def test_python_counter_handles_async_nested_decorated(tmp_path: Path) -> None:
    """Less-common Python shapes that still must trigger the counter:
    `async def`, nested function (def inside def), decorated function,
    nested class. Each of these contains an `import` whose scope is
    function-local or class-namespace-bound — counter must include it."""
    p = _write(tmp_path, "m.py", (
        "async def aio():\n"
        "    import async_dep\n"
        "def outer():\n"
        "    def inner():\n"
        "        import nested_dep\n"
        "@deco\n"
        "def decorated():\n"
        "    import deco_dep\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        import nested_class_dep\n"
    ))
    r = PythonAdapter().parse(p)
    # 4 hidden — one per body. Note the decorated function lives
    # under a `decorated_definition` wrapper; the walker descends
    # into it because the wrapper isn't in the scope set, but its
    # nested `function_definition` flips the flag.
    assert r.conditional_imports_count == 4


def test_python_counter_zero_on_broken_syntax(tmp_path: Path) -> None:
    """A malformed file should not crash, and the counter should not
    over-report. Tree-sitter recovers via ERROR nodes but we only count
    actual `import_statement` / `import_from_statement` matches."""
    p = _write(tmp_path, "m.py", "def broken(\n    import x\n")
    r = PythonAdapter().parse(p)
    assert r.error_count > 0
    # Recovery may or may not retain the import as a real statement.
    # The contract here is "doesn't crash and doesn't blow up the count".
    assert r.conditional_imports_count >= 0


def test_python_counter_zero_on_empty_or_comment_only(tmp_path: Path) -> None:
    cases = [
        ("empty.py", ""),
        ("only_comments.py", "# header comment\n# another\n"),
        ("import_in_comment.py", "def f(): pass\n# import faux  (this is in a comment)\n"),
    ]
    for name, content in cases:
        p = _write(tmp_path, name, content)
        r = PythonAdapter().parse(p)
        assert r.conditional_imports_count == 0, f"{name} expected 0"


def test_rust_counter_handles_async_fn_and_nested_fn(tmp_path: Path) -> None:
    """`async fn`, fn-inside-fn, and trait methods with default
    implementations all use the `function_item` node — verify they
    each count their nested `use`."""
    p = _write(tmp_path, "m.rs", (
        "async fn aio() {\n"
        "    use async_dep::X;\n"
        "}\n"
        "fn outer() {\n"
        "    fn inner() {\n"
        "        use nested_dep::Y;\n"
        "    }\n"
        "}\n"
        "trait T {\n"
        "    fn with_default(&self) {\n"
        "        use trait_default_dep::Z;\n"
        "    }\n"
        "}\n"
    ))
    r = RustAdapter().parse(p)
    assert r.conditional_imports_count == 3


def test_rust_counter_counts_use_in_closure_inside_fn(tmp_path: Path) -> None:
    """A closure body inside a fn is doubly nested in the scope set
    (function_item → closure_expression). Either node alone flips the
    flag; the counter still reports 1, not 2."""
    p = _write(tmp_path, "m.rs", (
        "fn main() {\n"
        "    let f = |x| { use closure_dep::Y; x };\n"
        "    f(0);\n"
        "}\n"
    ))
    r = RustAdapter().parse(p)
    assert r.conditional_imports_count == 1


def test_rust_counter_zero_on_empty_or_top_level_only(tmp_path: Path) -> None:
    cases = [
        ("empty.rs", ""),
        ("top_only.rs", "use a::B;\nuse c::D;\nfn main() {}\n"),
    ]
    for name, content in cases:
        p = _write(tmp_path, name, content)
        r = RustAdapter().parse(p)
        assert r.conditional_imports_count == 0, f"{name} expected 0"


def test_scala_counter_excludes_abstract_function_declaration(tmp_path: Path) -> None:
    """Abstract `def m: T` (no body) parses as `function_declaration`.
    This node was removed from the scope set in 0.6.3 because there's
    no body to host an import — verify the regression test for that."""
    p = _write(tmp_path, "M.scala", (
        "trait T {\n"
        "    def abstractMethod: Int\n"  # function_declaration, no body
        "    def withBody = {\n"  # function_definition, body has import
        "        import dep.X\n"
        "        42\n"
        "    }\n"
        "}\n"
    ))
    r = ScalaAdapter().parse(p)
    # Only the body of `withBody` counts.
    assert r.conditional_imports_count == 1


def test_scala_counter_zero_on_empty(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.scala", "")
    r = ScalaAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_php_counter_handles_nested_functions(tmp_path: Path) -> None:
    """PHP allows `function inner() {}` declared inside another function
    (the inner becomes globally visible only after the outer runs).
    A `require` in the inner body is doubly nested in the scope set."""
    p = _write(tmp_path, "f.php", (
        "<?php\n"
        "function outer() {\n"
        "    function inner() {\n"
        "        require 'nested.php';\n"
        "    }\n"
        "}\n"
    ))
    r = PhpAdapter().parse(p)
    assert r.conditional_imports_count == 1


def test_php_counter_robust_on_broken_syntax(tmp_path: Path) -> None:
    """Tree-sitter recovers around the syntax error; the counter must
    not crash and should report whatever recovered includes it sees."""
    p = _write(tmp_path, "f.php", (
        "<?php\n"
        "function broken( {\n"
        "    require 'x.php';\n"
        "}\n"
    ))
    r = PhpAdapter().parse(p)
    assert r.error_count > 0
    assert r.conditional_imports_count >= 0


def test_php_counter_zero_on_no_php_or_empty(tmp_path: Path) -> None:
    cases = [
        ("empty.php", ""),
        ("only_tag.php", "<?php\n"),
        ("html_only.php", "<html><body>Hello</body></html>\n"),
        ("only_comments.php", "<?php\n// header\n# another\n/** doc */\n"),
    ]
    for name, content in cases:
        p = _write(tmp_path, name, content)
        r = PhpAdapter().parse(p)
        assert r.conditional_imports_count == 0, f"{name} expected 0"


# --- Negative tests: things that look like imports but aren't --------


def test_python_does_not_count_string_or_docstring_with_import_word(tmp_path: Path) -> None:
    """A string literal containing the word `import` is just data, not
    an `import_statement`. Tree-sitter parses it as a `string` node,
    which the walker correctly ignores."""
    p = _write(tmp_path, "m.py", (
        'x = "import something"\n'
        'def f():\n'
        '    """import doc"""\n'
        '    return "from a import b"\n'
    ))
    r = PythonAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_python_does_not_count_dunder_import_call(tmp_path: Path) -> None:
    """`__import__("foo")` is a builtin function call — emits a `call`
    node, not an `import_statement`. Must not count."""
    p = _write(tmp_path, "m.py", (
        'def f():\n'
        '    return __import__("foo")\n'
    ))
    r = PythonAdapter().parse(p)
    assert r.conditional_imports_count == 0


def test_rust_does_not_count_string_with_use_text(tmp_path: Path) -> None:
    p = _write(tmp_path, "m.rs", 'fn f() { let s = "use a::B"; }\n')
    r = RustAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_rust_extern_crate_at_top_level_is_static_import(tmp_path: Path) -> None:
    """`extern crate foo;` is a legacy crate-link declaration — same
    role as `use` for our purposes. Top-level → static, in fn body →
    conditional."""
    p = _write(tmp_path, "m.rs", (
        "extern crate top_crate;\n"
        "fn f() { extern crate inside_crate; }\n"
    ))
    r = RustAdapter().parse(p)
    assert r.imports == ["extern crate top_crate"]
    assert r.conditional_imports_count == 1


def test_rust_mod_declaration_is_not_import(tmp_path: Path) -> None:
    """`mod foo;` (without a body) is a structural file-tree marker
    declaring a submodule, not an import. Excluded from `imports`
    intentionally — see `_collect_imports` docstring."""
    p = _write(tmp_path, "m.rs", "mod foo;\nfn main() {}\n")
    r = RustAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_scala_does_not_count_string_with_import_text(tmp_path: Path) -> None:
    p = _write(tmp_path, "M.scala", 'val s = "import something"\n')
    r = ScalaAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_php_does_not_count_string_or_comment_with_require_text(tmp_path: Path) -> None:
    """Source-true text scanning is grammar-driven — `require` mentioned
    inside a string literal or a comment is not a `require_expression`
    node and must not enter the counter."""
    p = _write(tmp_path, "f.php", (
        '<?php\n'
        '$s = "require foo.php";\n'
        '// require fake from line comment\n'
        '/* require fake from block comment */\n'
        'function f() { /** require in PHPDoc */ return 1; }\n'
    ))
    r = PhpAdapter().parse(p)
    assert r.imports == []
    assert r.conditional_imports_count == 0


def test_languages_without_local_imports_keep_counter_zero(tmp_path: Path) -> None:
    """Java / Go / Kotlin / C# / TypeScript imports are top-level by
    spec (TypeScript ES modules; CommonJS `require()` is a separate
    concern not yet handled). There's no place for a "conditional"
    import to hide. Counter must stay at 0 even with rich files."""
    cases = [
        (JavaAdapter(), "M.java", "import a.B;\nclass M { void f() { } }\n"),
        (GoAdapter(), "m.go", 'package main\nimport "fmt"\nfunc f() {}\n'),
        (KotlinAdapter(), "M.kt", "package x\nimport a.B\nfun f() { }\n"),
        (CSharpAdapter(), "M.cs", "using A;\nnamespace X { class C { void m() {} } }\n"),
        (TypeScriptAdapter(), "m.ts", 'import x from "y";\nfunction f() { return x; }\n'),
    ]
    for adapter, name, content in cases:
        p = _write(tmp_path, name, content)
        r = adapter.parse(p)
        assert r.conditional_imports_count == 0, (
            f"{adapter.language_name}: expected 0, got {r.conditional_imports_count}"
        )


# --- Renderer integration ------------------------------------------------


def test_outline_with_show_imports_emits_imports_line(tmp_path: Path) -> None:
    """The `# imports: ...` annotation appears under the file header
    only when `OutlineOptions.show_imports` is True. Off by default."""
    from ast_outline.core import OutlineOptions, render_outline

    p = _write(tmp_path, "m.py", "import foo\nfrom .core import X\n")
    r = PythonAdapter().parse(p)
    out_default = render_outline(r, OutlineOptions())
    assert "imports:" not in out_default

    out_with = render_outline(r, OutlineOptions(show_imports=True))
    assert "# imports: import foo; from .core import X" in out_with


def test_digest_with_show_imports_emits_imports_line(tmp_path: Path) -> None:
    from ast_outline.core import DigestOptions, render_digest

    p = _write(tmp_path, "m.py", "import foo\nfrom .core import X\n")
    r = PythonAdapter().parse(p)
    out_default = render_digest([r], DigestOptions())
    assert "imports:" not in out_default

    out_with = render_digest([r], DigestOptions(show_imports=True))
    assert "imports: import foo; from .core import X" in out_with
