"""Python adapter — parses .py files via tree-sitter-python into Declaration IR.

Design notes (how Python concepts map to the IR):
- `class_definition`          → KIND_CLASS
- `function_definition`       → KIND_METHOD (when inside a class) or KIND_FUNCTION (top-level)
- `decorated_definition`      → wraps the above, attrs collected from decorators
- `@property` decorator       → KIND_PROPERTY (nicer digest output)
- `__init__`                  → KIND_CTOR
- Docstring (first string expr in body) → docs[]
- Module-level assignments with type hints → KIND_FIELD
- Class-body assignments → KIND_FIELD
- Visibility heuristic: name starts with `_` and not `__dunder__` → private
- Inheritance: `class Foo(Bar, Baz):` — parenthesized args
- No namespaces in Python (module = file)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspy

from .base import count_parse_errors
from ..core import (
    KIND_CLASS,
    KIND_CTOR,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_METHOD,
    KIND_PROPERTY,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tspy.language())
_PARSER = Parser(_LANGUAGE)


class PythonAdapter:
    language_name = "python"
    extensions = {".py", ".pyi"}
    definition_keywords = frozenset({"def", "class"})

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        _walk_module(tree.root_node, src, decls)
        imports: list[str] = []
        # Two-source region collection — both are zero-cost piggybacks
        # on existing walks, neither adds a tree traversal:
        #   1. ``_collect_imports`` (top-level + if/try) writes the
        #      module-scope regions while building the imports list.
        #   2. ``_count_conditional_imports`` (whole tree) writes the
        #      function/class-body regions while counting lazy imports.
        # Together they cover every import in the file without overlap
        # — the conditional walker only appends when ``in_scope`` is
        # True (i.e. inside a function/class), exactly the gap the
        # imports walk leaves uncovered.
        import_regions: list[tuple[int, int]] = []
        _collect_imports(tree.root_node, src, imports, import_regions)
        # Imports inside function / method / class bodies are runtime-
        # scoped (lazy `import` for circular-deps avoidance, etc.) or
        # class-namespaced (eager but bound to the class, not the
        # module). Either way they're not the file's module-level
        # public dependencies. We don't list them — but we count them
        # so the renderer can emit `[+ N conditional includes]` and
        # the agent isn't misled into thinking the file is dependency-
        # closed by its top-level imports alone.
        conditional_count = _count_conditional_imports(
            tree.root_node, import_regions
        )
        import_regions.sort()
        # Multi-line strings (docstrings) and any string/comment that
        # spans line boundaries — collected so the grep command can
        # reliably filter symbols mentioned in docstrings without
        # relying on a regex pre-pass that gets confused by ``"""`` /
        # ``'''`` literals appearing inside regular code (e.g. the
        # ``('"""', "'''")`` tuple in a doc-stripping helper).
        noise_regions = _collect_noise_regions(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            conditional_imports_count=conditional_count,
            noise_regions=noise_regions,
            import_regions=import_regions,
        )


def _collect_noise_regions(root: Node) -> list[tuple[int, int, str]]:
    """Walk the tree once, returning string and comment byte ranges.

    Tree-sitter is the authoritative source — it distinguishes a
    triple-quote that opens a docstring from one that appears as data
    inside another string, which a regex pre-pass cannot. Output is
    sorted by start_byte so byte-position lookup can early-exit.
    """
    out: list[tuple[int, int, str]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "string":
            out.append((node.start_byte, node.end_byte, "string"))
            # Don't descend — string contents (interpolation parts on
            # f-strings) are already covered by the outer range.
            continue
        if node.type == "comment":
            out.append((node.start_byte, node.end_byte, "comment"))
            continue
        stack.extend(node.children)
    out.sort()
    return out


# --- Imports --------------------------------------------------------------
#
# We collect imports from top-level statements PLUS the bodies of
# top-level `if` / `try` blocks. The `if TYPE_CHECKING:` pattern is the
# 99% case for non-top-level imports and keeping it visible in the
# overview matches what an agent expects ("this file imports X for
# typing only"). We deliberately do NOT descend into function or class
# bodies — local imports there are usually circular-dep workarounds, low
# signal for an architectural read.


# Block types we descend into, looking for additional imports. `block`
# itself is the suite-of-statements that follows a colon-introducer in
# Python (`if cond:` body, `try:` body, `else:` body, etc.) — descending
# here covers `if TYPE_CHECKING:`, `try: import foo / except ImportError: import bar`,
# and any conditionally-guarded imports without us hand-listing every
# clause type.
_PY_IMPORT_DESCEND = {
    "if_statement", "elif_clause", "else_clause",
    "try_statement", "except_clause", "finally_clause",
    "block",
}


def _collect_imports(
    root: Node,
    src: bytes,
    out: list[str],
    regions: list[tuple[int, int]] | None = None,
) -> None:
    """Walk the tree once, emitting normalized import strings into ``out``
    AND (if ``regions`` is supplied) appending each import declaration's
    byte range. Piggybacking the byte-range collection onto the existing
    imports walk avoids a second tree traversal — the standalone
    ``_collect_import_regions`` cost ~22% of post-parse time on a mixed
    100-file corpus, while doing this in the same loop is essentially
    free (one ``.append`` per import found).

    Scope intentionally matches what ``_collect_imports`` already covers:
    top-level + ``if`` / ``try`` blocks. Lazy imports inside function
    bodies are NOT walked — they're rare, and single-line ones still get
    ``[import]`` from the line-prefix classifier in ``grep`` (the
    stripped line starts with ``import ``). The only loss is the very
    rare ``def foo(): from x import (\\n a,\\n)`` pattern, where inner
    ``a`` lines would classify as ``[ref]`` instead of ``[import]``.
    """
    for child in root.named_children:
        kind = child.type
        if kind == "import_statement":
            _emit_import_statement(child, src, out)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))
        elif kind == "import_from_statement":
            _emit_import_from(child, src, out)
            if regions is not None:
                regions.append((child.start_byte, child.end_byte))
        elif kind in _PY_IMPORT_DESCEND:
            _collect_imports(child, src, out, regions)
        # function_definition / class_definition / decorated_definition →
        # not descended; their imports are local-scope and intentionally
        # excluded from the file-level overview.


# AST node types that take an import out of "module-level public
# dependency" status: function bodies (lazy imports for circular-deps
# avoidance) and class bodies (imports get bound to the class
# namespace, not the module).
_PY_RUNTIME_OR_SCOPED = frozenset({
    "function_definition",
    "async_function_definition",
    "class_definition",
})


def _count_conditional_imports(
    root: Node,
    regions: list[tuple[int, int]] | None = None,
) -> int:
    """Count `import` / `from ... import` statements that live inside
    a function / method / class body — i.e. NOT module-level.

    Iterative `(node, in_scope)` walk: once `in_scope` flips to True
    on a function/class boundary, every nested import (no matter how
    deep) counts. Top-level imports — including those inside an
    `if TYPE_CHECKING:` block or a `try/except` import-fallback,
    which `_collect_imports` already surfaces as static — are not
    counted.

    If ``regions`` is supplied, also appends the byte range of each
    in-scope import — extends grep's ``import_regions`` to cover lazy
    multi-line imports inside function / class bodies (e.g.
    ``def foo(): from x import (\\n    a,\\n)``). Top-level + if/try
    regions are already populated by ``_collect_imports``; this walker
    fills the function-body gap WITHOUT a separate tree pass — we're
    already walking everything to count, the ``.append`` is free.
    """
    count = 0
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, in_scope = stack.pop()
        t = node.type
        if t in ("import_statement", "import_from_statement"):
            if in_scope:
                count += 1
                if regions is not None:
                    regions.append((node.start_byte, node.end_byte))
            # No nested imports to find inside an import statement.
            continue
        new_in_scope = in_scope or t in _PY_RUNTIME_OR_SCOPED
        for c in node.children:
            stack.append((c, new_in_scope))
    return count


def _emit_import_statement(node: Node, src: bytes, out: list[str]) -> None:
    """`import a`, `import a as b`, `import a, b, c as d`.

    Each comma-separated entry becomes its own `import ...` line in the
    output. Splitting comma-joined imports keeps every line one
    statement, which makes counting and reading straightforward — the
    agent doesn't need to parse a comma-list within a single import to
    see what's pulled.
    """
    for c in node.named_children:
        if c.type == "dotted_name":
            out.append(f"import {_text(c, src)}")
        elif c.type == "aliased_import":
            name = c.child_by_field_name("name")
            alias = c.child_by_field_name("alias")
            if name is not None and alias is not None:
                out.append(f"import {_text(name, src)} as {_text(alias, src)}")


def _emit_import_from(node: Node, src: bytes, out: list[str]) -> None:
    """`from M import X, Y, Z`, including relative `from .M import X`,
    wildcard `from M import *`, and multi-line parenthesized lists.

    Names are joined with `, ` on a single line — that's how Python
    naturally writes a `from` import, and stays compact when the list
    is short. Long lists wrap in the source via parens; we collapse
    them to one comma-list, since the renderer joins all entries with
    `; ` anyway and an agent sees one long line either way.

    On any unexpected AST shape we fall back to the raw source text of
    the whole `import_from_statement` (with whitespace collapsed and
    trailing punctuation trimmed). Falling back to source text is
    safer than emitting a synthesised string built from partial fields,
    which could lose meaning silently if a future grammar revision
    moves things around.
    """
    module_node = node.child_by_field_name("module_name")
    module = _text(module_node, src) if module_node is not None else ""

    # Wildcard form: `from M import *`. Use named_children for
    # consistency with the rest of the function — wildcard_import is a
    # named node so it appears in either iterator.
    has_wildcard = any(c.type == "wildcard_import" for c in node.named_children)
    if has_wildcard:
        if module:
            out.append(f"from {module} import *")
        else:
            # Should not happen on valid Python (parser always sets
            # module_name for `from X import *`). Defensive fallback —
            # raw source preserves the original relative dots so we
            # don't silently emit `from . import *` when the source
            # said `from .. import *` or `from ... import *`.
            out.append(_collapse_ws(_text(node, src)).strip())
        return

    names: list[str] = []
    # `name` field appears multiple times — one per imported binding.
    for c in node.children_by_field_name("name"):
        if c.type == "dotted_name":
            names.append(_text(c, src))
        elif c.type == "aliased_import":
            n = c.child_by_field_name("name")
            a = c.child_by_field_name("alias")
            if n is not None and a is not None:
                names.append(f"{_text(n, src)} as {_text(a, src)}")
    if names:
        # Python's `from .` shape: `from . import sub` — module is just
        # dots, no name after them. Render as-is.
        out.append(f"from {module} import {', '.join(names)}")


# --- Walk -----------------------------------------------------------------


def _walk_module(root: Node, src: bytes, out: list[Declaration]) -> None:
    for child in root.named_children:
        decl = _node_to_decl(child, src, inside_class=False)
        if decl is not None:
            out.append(decl)


def _walk_class_body(block: Node, src: bytes) -> list[Declaration]:
    children: list[Declaration] = []
    for c in block.named_children:
        decl = _node_to_decl(c, src, inside_class=True)
        if decl is not None:
            children.append(decl)
    return children


def _node_to_decl(node: Node, src: bytes, *, inside_class: bool) -> Optional[Declaration]:
    # Decorators wrap the actual definition
    if node.type == "decorated_definition":
        decorators = [_collapse_ws(_text(c, src)) for c in node.children if c.type == "decorator"]
        definition = node.child_by_field_name("definition")
        if definition is None:
            return None
        decl = _node_to_decl(definition, src, inside_class=inside_class)
        if decl is None:
            return None
        # Record byte range INCLUDING decorators (so `show` prints them too)
        decl.attrs = decorators + decl.attrs
        decl.start_line = node.start_point[0] + 1
        decl.start_byte = node.start_byte
        decl.doc_start_byte = min(decl.doc_start_byte or node.start_byte, node.start_byte)
        # @property transforms a method into a "property" for nicer digests
        if inside_class and decl.kind == KIND_METHOD and any(
            d == "@property" or d.startswith("@property ") or d.startswith("@property\n")
            for d in decorators
        ):
            decl.kind = KIND_PROPERTY
        return decl

    if node.type == "class_definition":
        return _class_to_decl(node, src)

    if node.type == "function_definition":
        return _function_to_decl(node, src, inside_class=inside_class)

    if node.type == "expression_statement":
        # Module- and class-body assignments (`X: int = 1`, `X = "..."`) are
        # parsed as `expression_statement` wrapping an `assignment`. Docstrings
        # are also `expression_statement` but with a string child — those we
        # let `_docstring` handle from the parent.
        for inner in node.named_children:
            if inner.type == "assignment":
                return _assignment_to_decl(inner, src)
        return None

    if node.type == "assignment":
        # Module or class-body field declaration
        return _assignment_to_decl(node, src)

    return None


def _class_to_decl(node: Node, src: bytes) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    bases = _class_bases(node, src)
    body = node.child_by_field_name("body")
    docs = _docstring(body, src) if body is not None else []
    children = _walk_class_body(body, src) if body is not None else []

    sig = f"class {name}"
    if bases:
        sig += "(" + ", ".join(bases) + ")"

    return Declaration(
        kind=KIND_CLASS,
        name=name,
        signature=sig,
        bases=bases,
        attrs=[],
        docs=docs,
        docs_inside=True,
        visibility=_visibility_for_name(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
        children=children,
    )


def _function_to_decl(node: Node, src: bytes, *, inside_class: bool) -> Declaration:
    name = _field_text(node, "name", src) or "?"
    body = node.child_by_field_name("body")
    docs = _docstring(body, src) if body is not None else []
    sig = _function_signature(node, src)

    if inside_class and name == "__init__":
        kind = KIND_CTOR
    elif inside_class:
        kind = KIND_METHOD
    else:
        kind = KIND_FUNCTION

    return Declaration(
        kind=kind,
        name=name,
        signature=sig,
        docs=docs,
        docs_inside=True,
        visibility=_visibility_for_name(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        doc_start_byte=node.start_byte,
    )


def _assignment_to_decl(node: Node, src: bytes) -> Optional[Declaration]:
    """Treat typed / value-initialized module- or class-level assignments as fields.

    Only names we care about: simple `name = value` or `name: Type = value`.
    Skip anything complex (tuples, attribute targets).
    """
    left = node.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return None
    name = _text(left, src)

    type_node = node.child_by_field_name("type")
    type_str = _text(type_node, src).lstrip(": ").strip() if type_node is not None else None

    # Rendered signature: `name: Type` or `name = value`
    if type_str:
        sig = f"{name}: {type_str}"
    else:
        sig = name
    return Declaration(
        kind=KIND_FIELD,
        name=name,
        signature=sig,
        visibility=_visibility_for_name(name),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
    )


# --- Helpers --------------------------------------------------------------


def _function_signature(node: Node, src: bytes) -> str:
    """`def foo(a, b: int) -> X` — everything up to (but not including) the body block."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = src[node.start_byte : end].decode("utf8", errors="replace")
    text = _collapse_ws(text).rstrip(" :")
    return text


def _class_bases(node: Node, src: bytes) -> list[str]:
    """For `class Foo(Bar, Baz, metaclass=X):` return ['Bar', 'Baz'] — skip kwargs."""
    sup = node.child_by_field_name("superclasses")
    if sup is None:
        return []
    out: list[str] = []
    for c in sup.named_children:
        if c.type == "keyword_argument":
            continue  # skip metaclass=... etc
        t = _collapse_ws(_text(c, src))
        if t:
            out.append(t)
    return out


def _docstring(block: Optional[Node], src: bytes) -> list[str]:
    """Return the docstring (if any) of a class/function body, as a list of lines.

    Docstring = first statement in block is an expression_statement wrapping a string.
    """
    if block is None:
        return []
    for c in block.named_children:
        if c.type == "expression_statement":
            inner = c.named_children[0] if c.named_children else None
            if inner is not None and inner.type in ("string", "concatenated_string"):
                text = _text(inner, src)
                # Split on newlines and keep triple-quoted form as-is
                return text.splitlines()
        break  # only check the very first statement
    return []


def _visibility_for_name(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return ""  # dunder — conventionally public API (magic methods)
    if name.startswith("_"):
        return "private"
    return ""


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")


def _field_text(node: Node, field_name: str, src: bytes) -> Optional[str]:
    c = node.child_by_field_name(field_name)
    return _text(c, src) if c is not None else None
