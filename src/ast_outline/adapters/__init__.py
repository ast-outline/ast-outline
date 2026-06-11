"""Language adapters — parse source into Declaration IR.

Each adapter knows: a set of file extensions it handles, and how to convert
tree-sitter AST nodes for its language into the `core.Declaration` tree.

Directory traversal also lives here. ``collect_files`` walks input dirs,
filters out junk (``.gitignore`` patterns + a small hardcoded fallback list
covering ``.git`` / ``node_modules`` / ``__pycache__`` / ``.venv`` / ``venv``),
and prunes ignored directories at walk time so we don't pay the cost of
descending into ``node_modules`` just to throw the files away.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pathspec import GitIgnoreSpec

from .base import LanguageAdapter
from .cpp import CppAdapter
from .csharp import CSharpAdapter
from .css import CssAdapter
from .gdscript import GDScriptAdapter
from .go import GoAdapter
from .html import HtmlAdapter
from .java import JavaAdapter
from .kotlin import KotlinAdapter
from .lua import LuaAdapter
from .markdown import MarkdownAdapter
from .php import PhpAdapter
from .python import PythonAdapter
from .ruby import RubyAdapter
from .rust import RustAdapter
from .scala import ScalaAdapter
from .scss import ScssAdapter
from .sql import SqlAdapter
from .swift import SwiftAdapter
from .typescript import TypeScriptAdapter
from .yaml import YamlAdapter


ADAPTERS: list[LanguageAdapter] = [
    CSharpAdapter(),
    CppAdapter(),
    PythonAdapter(),
    TypeScriptAdapter(),
    JavaAdapter(),
    KotlinAdapter(),
    ScalaAdapter(),
    GoAdapter(),
    RustAdapter(),
    PhpAdapter(),
    RubyAdapter(),
    LuaAdapter(),
    GDScriptAdapter(),
    CssAdapter(),
    ScssAdapter(),
    SqlAdapter(),
    SwiftAdapter(),
    HtmlAdapter(),
    MarkdownAdapter(),
    YamlAdapter(),
]


# Each entry must have an unambiguous name with no realistic conflict
# with hand-written source dirs across our supported languages — the
# rationale for inclusion / exclusion lives in CHANGELOG and docs.
_DEFAULT_IGNORE_PATTERNS: list[str] = [
    # VCS metadata
    ".git/",
    ".svn/",
    ".hg/",
    # JS / TS — package manager
    "node_modules/",
    # Python — bytecode, virtual envs, tool caches, build metadata
    "__pycache__/",
    ".venv/",
    "venv/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".eggs/",
    "*.egg-info/",
    # JVM — Gradle's local build cache (NOT ``gradle/`` which is wrapper scripts)
    ".gradle/",
    # IDE / editor metadata — none of these contain files we'd parse (JSON
    # / XML configs), so pruning is mostly cosmetic for the output, but it
    # keeps the walk fast and the ``# note: ignored`` line informative when
    # present in deep monorepos.
    ".idea/",
    ".vs/",
    ".vscode/",
    ".cursor/",
    ".zed/",
    ".fleet/",
    # JS test infra & hooks
    "__snapshots__/",
    ".husky/",
    # JS framework build caches — these regenerate ``.ts``/``.tsx`` files
    # that look like real source, so tree-sitter would happily parse them
    # if we descended.
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".turbo/",
    ".parcel-cache/",
    ".vite/",
    # Infra tooling
    ".terraform/",
    # Minified web bundles — single-line generated artifacts that
    # tree-sitter can parse but for which an outline is meaningless
    # (one giant rule / one giant function with no semantic structure).
    # The ``.min.`` infix is an unambiguous build-output signal in JS /
    # CSS pipelines (UglifyJS, Terser, cssnano, postcss-minify); the
    # extension filter alone would still let them through because the
    # final ``.js`` / ``.css`` is real. Source maps (``.map``) get
    # filtered too — they're JSON, but since no adapter claims ``.map``
    # the existing extension filter already drops them; pattern is
    # listed for clarity.
    "*.min.js",
    "*.min.mjs",
    "*.min.cjs",
    "*.min.css",
    "*.min.html",
    "*.map",
]


# A shebang line the kernel would honor is short; 256 bytes covers even
# long ``env -S`` forms with room to spare.
_SHEBANG_READ_BYTES = 256
# Trailing version run in an interpreter name: ``python3`` / ``python3.13``
# / ``lua5.4`` / ``php8`` → strip to the bare program. The suffix must
# start with a digit (optionally preceded by ``-`` / ``.``), so names
# like ``ts-node`` survive untouched.
_SHEBANG_VERSION_SUFFIX = re.compile(r"[-.]?\d[\d.]*$")

# ``env`` options that consume the NEXT token as their argument — the
# argument must be skipped too, or ``#!/usr/bin/env -u VAR python3``
# would read ``VAR`` as the interpreter. ``=``-joined long forms
# (``--unset=VAR``) are covered by the generic ``=`` skip instead.
_ENV_FLAGS_WITH_ARG = frozenset({"-u", "-C", "-P", "--unset", "--chdir"})


def shebang_interpreter(path: Path) -> Optional[str]:
    """Interpreter program named by ``path``'s shebang line, normalized
    (basename, lowercase, version suffix stripped) — or None when the
    file has no shebang or can't be read. ``env`` indirection is
    unwrapped: flags (``-S`` & co.) and ``VAR=value`` assignments are
    skipped, the next token is the real program."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_SHEBANG_READ_BYTES)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", errors="replace")
    tokens = line.split()
    if not tokens:
        return None
    program = os.path.basename(tokens[0])
    if program == "env":
        program = ""
        skip_next = False
        for t in tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if t in _ENV_FLAGS_WITH_ARG:
                skip_next = True
                continue
            if t.startswith("-") or "=" in t:
                continue
            program = os.path.basename(t)
            break
        if not program:
            return None
    return _SHEBANG_VERSION_SUFFIX.sub("", program.lower())


def supported_shebang_programs() -> list[str]:
    """Sorted union of interpreter names the adapters claim — single
    source of truth for the "no files found" hint, same no-drift
    rationale as ``supported_languages``."""
    out: set[str] = set()
    for a in ADAPTERS:
        out.update(getattr(a, "shebang_programs", frozenset()))
    return sorted(out)


def get_adapter_for(path: Path) -> Optional[LanguageAdapter]:
    """Resolve the adapter for ``path`` by suffix first, then by exact
    basename, then — for extensionless files only — by shebang. The
    basename branch covers convention-named extensionless files like
    ``Rakefile`` and ``Gemfile`` — Ruby projects routinely ship them,
    and treating them as "unknown" would force the agent into a full
    read for what is in practice plain Ruby. The shebang branch covers
    unix-convention CLI scripts (``#!/usr/bin/env python3`` & co.):
    agents hit these as explicit arguments, and without the sniff they
    were reduced to symlinking the file to ``/tmp/x.py`` first. Only
    explicit file inputs ever reach here without a recognized suffix —
    the directory walker filters on suffix/basename and never pays the
    open() this branch costs."""
    ext = path.suffix.lower()
    for a in ADAPTERS:
        if ext in a.extensions:
            return a
    name = path.name
    for a in ADAPTERS:
        if name in getattr(a, "basenames", set()):
            return a
    if not ext and path.is_file():
        program = shebang_interpreter(path)
        if program:
            for a in ADAPTERS:
                if program in getattr(a, "shebang_programs", frozenset()):
                    return a
    return None


def supported_extensions() -> set[str]:
    out: set[str] = set()
    for a in ADAPTERS:
        out.update(a.extensions)
    return out


def supported_languages() -> list[tuple[str, list[str]]]:
    """``(display_name, sorted extensions)`` per adapter, in ``ADAPTERS``
    order. Single source of truth for the help text's language table —
    a new adapter shows up there automatically, so the list can't drift
    out of sync with what the tool actually parses."""
    return [(a.display_name, sorted(a.extensions)) for a in ADAPTERS]


def supported_basenames() -> set[str]:
    """Convention-named extensionless files that some adapter claims
    by exact basename match. See :func:`get_adapter_for` rationale."""
    out: set[str] = set()
    for a in ADAPTERS:
        out.update(getattr(a, "basenames", set()))
    return out


@dataclass(frozen=True)
class CollectResult:
    """Result of a directory walk: matched files + ignore-filter stats.

    ``ignored_dir_names`` holds the **unique basenames** of pruned dirs
    (sorted), not full paths — agents reading the ``# note:`` line want
    to see "what kind of thing got skipped" (``node_modules``,
    ``.gradle``, …), not every nested occurrence.

    File-level gitignore matches (e.g. a top-level file matching
    ``*.generated.py``) are still filtered out, just not counted —
    surfacing a bare "+ N files" without their names is more confusing
    than informative (the agent can't tell whether they're inside the
    listed dirs or somewhere else).
    """

    files: list[Path]
    ignored_dirs: int = 0
    ignored_dir_names: tuple[str, ...] = ()


def _find_project_root(start: Path) -> Path:
    """Walk up from ``start`` to the directory containing ``.git``.

    Falls back to ``start`` itself if no git root is found in the ancestors.
    Used to anchor ``.gitignore`` pattern matching — gitignore patterns are
    relative to the directory containing the ``.gitignore`` file, which we
    approximate as the project root for the common single-gitignore case.
    """
    cur = start.resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return start.resolve()
        cur = cur.parent


# Order matters: later files in the tuple win on conflict via
# gitignore's last-pattern-wins semantics. ``.ignore`` (the
# search-tool convention from ripgrep / fd / ast-grep) overrides
# ``.gitignore`` so users can hide a tracked dir from outline / digest
# without affecting git tracking — and conversely, un-hide something
# their ``.gitignore`` excludes.
_IGNORE_FILE_NAMES: tuple[str, ...] = (".gitignore", ".ignore")


def _read_ignore_lines(dirpath: Path) -> list[str]:
    """Read every ``.gitignore`` / ``.ignore`` in ``dirpath`` as one line list.

    Files are concatenated in ``_IGNORE_FILE_NAMES`` order so the last
    one (``.ignore``) gets the final say on conflicts. Missing or
    unreadable files are skipped silently — a permission error on one
    config file shouldn't kill the whole walk.
    """
    out: list[str] = []
    for name in _IGNORE_FILE_NAMES:
        f = dirpath / name
        if not f.is_file():
            continue
        try:
            out.extend(f.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            pass
    return out


def _build_root_spec(project_root: Path) -> GitIgnoreSpec:
    """Build the root frame's spec.

    Combines, in priority order: hardcoded defaults < project-root
    ``.gitignore`` < project-root ``.ignore``. Defaults come first so
    a user pattern (``!node_modules/our-fork/``) can override them.
    """
    lines = list(_DEFAULT_IGNORE_PATTERNS) + _read_ignore_lines(project_root)
    return GitIgnoreSpec.from_lines(lines)


def _read_nested_spec(dirpath: Path) -> Optional[GitIgnoreSpec]:
    """Build a spec from any ``.gitignore`` / ``.ignore`` in ``dirpath``.

    Returns ``None`` if neither file exists, so the caller can skip
    pushing an empty frame.
    """
    lines = _read_ignore_lines(dirpath)
    if not lines:
        return None
    return GitIgnoreSpec.from_lines(lines)


def _is_ignored(
    full_path: Path,
    is_dir: bool,
    frames: list[tuple[Path, GitIgnoreSpec]],
) -> bool:
    """Decide whether a path is ignored using a stack of gitignore frames.

    Frames are ordered shallowest → deepest. We check **deepest-first**
    and return on the first frame that gives a definitive answer
    (``include=True`` for ignore, ``include=False`` for explicit
    un-ignore via ``!`` negation). Mirrors git's actual semantics
    where a more-specific ``.gitignore`` overrides patterns from a
    parent ``.gitignore``.
    """
    for anchor, spec in reversed(frames):
        try:
            rel = full_path.relative_to(anchor)
        except ValueError:
            continue
        rel_str = str(rel).replace(os.sep, "/")
        if is_dir:
            rel_str += "/"
        result = spec.check_file(rel_str)
        if result.include is True:
            return True
        if result.include is False:
            return False
    return False


def collect_files(
    paths: list[Path],
    glob: Optional[str] = None,
    no_ignore: bool = False,
    exclude: Optional[list[str]] = None,
) -> list[Path]:
    """Gather all source files under ``paths`` that any adapter handles.

    Convenience wrapper around :func:`collect_files_with_stats` for callers
    that don't need ignore-filter statistics. ``.gitignore`` and the
    hardcoded fallback list are still applied unless ``no_ignore=True``.
    """
    return collect_files_with_stats(
        paths, glob=glob, no_ignore=no_ignore, exclude=exclude
    ).files


def collect_files_with_stats(
    paths: list[Path],
    glob: Optional[str] = None,
    no_ignore: bool = False,
    exclude: Optional[list[str]] = None,
) -> CollectResult:
    """Walk input paths and return matched files plus ignore-filter stats.

    Filtering uses a stack of gitignore frames mimicking ``git`` (with
    a small extension borrowed from ripgrep / fd / ast-grep):

    * The **root frame** combines, in priority order: hardcoded
      defaults < project-root ``.gitignore`` < project-root ``.ignore``.
      Project root is located by walking up from the input path until
      ``.git`` is found, falling back to the input dir.
    * Nested ``.gitignore`` and ``.ignore`` files encountered during
      the walk push additional frames anchored at their containing
      dir (combined into one spec per dir).
    * Matching is **deepest-first** — a nested ignore file can
      override a parent's rule via ``!`` negation. Within a single
      frame, ``.ignore`` patterns sit after ``.gitignore`` patterns,
      so they override on conflict. Defaults sit before the project
      ``.gitignore``, so a user pattern like ``!node_modules/our-fork/``
      un-ignores something the defaults would have pruned.

    ``.ignore`` is the search-tool convention shared with ripgrep /
    fd / ast-grep — a way to hide files from search-style tools
    without affecting git tracking.

    ``exclude`` is a list of gitwildmatch (``.gitignore``-syntax)
    patterns supplied via the CLI ``--exclude`` flag. They form an
    extra frame that:

    * Is anchored at the **project root** so users write
      ``src/generated/`` and it resolves the same regardless of cwd.
    * Applies in BOTH the normal walk and the ``no_ignore=True`` raw
      walk — ``--exclude`` is the user's explicit voice, while
      ``--no-ignore`` only silences automatic filters.
    * Contributes to ``ignored_dirs`` / ``ignored_dir_names`` exactly
      like the other ignore frames — visibility helps agents notice
      when their own pattern was the reason a folder is empty.

    Explicit single-file inputs continue to bypass filtering — same
    rule as ``.gitignore``, since pointing at a file is an explicit
    intent.

    Matching directories are pruned at walk time so we never descend
    into them. Files are filtered by supported extension (or by
    ``glob`` if provided).
    """
    out: list[Path] = []
    ignored_dirs = 0
    ignored_dir_basenames: set[str] = set()
    exts = supported_extensions()
    basenames = supported_basenames()

    exclude_spec: Optional[GitIgnoreSpec] = None
    if exclude:
        exclude_spec = GitIgnoreSpec.from_lines(exclude)

    for p in paths:
        if p.is_file():
            out.append(p)
            continue
        if not p.is_dir():
            continue

        # Anchor the user's ``--exclude`` frame at the project root
        # when available, otherwise at the input dir itself. Matches
        # how the root ``.gitignore`` frame is anchored — keeps the
        # mental model consistent regardless of cwd.
        anchor_root = _find_project_root(p).resolve() if exclude_spec else None
        exclude_frame: Optional[tuple[Path, GitIgnoreSpec]] = (
            (anchor_root, exclude_spec) if exclude_spec and anchor_root else None
        )

        if no_ignore:
            # Raw walk — no defaults, no .gitignore, no .ignore. Only
            # the extension (or ``glob``) filter applies. Used when the
            # agent / user explicitly opts out of smart filtering, e.g.
            # to outline a vendored fork inside ``node_modules`` without
            # editing any ignore files. ``--exclude`` still applies
            # here — it's the user's explicit narrowing, distinct from
            # the auto-filter that ``--no-ignore`` disables. Output
            # paths preserve the caller's input shape (no ``.resolve()``
            # applied) to stay back-compat with existing tests that do
            # ``f.relative_to(input_dir)``; matching against the exclude
            # frame uses a separate resolved ``dpath`` since the frame
            # anchor is resolved.
            no_ignore_frames: list[tuple[Path, GitIgnoreSpec]] = (
                [exclude_frame] if exclude_frame else []
            )
            for dirpath, dirs, files in os.walk(p):
                dpath = Path(dirpath)
                if no_ignore_frames:
                    match_dpath = dpath.resolve()
                    kept: list[str] = []
                    for d in dirs:
                        if _is_ignored(
                            match_dpath / d,
                            is_dir=True,
                            frames=no_ignore_frames,
                        ):
                            ignored_dirs += 1
                            ignored_dir_basenames.add(d)
                            continue
                        kept.append(d)
                    dirs[:] = sorted(kept)
                for fname in sorted(files):
                    f = dpath / fname
                    if glob:
                        if not f.match(glob):
                            continue
                    else:
                        if (
                            f.suffix.lower() not in exts
                            and f.name not in basenames
                        ):
                            continue
                    if no_ignore_frames and _is_ignored(
                        match_dpath / fname,
                        is_dir=False,
                        frames=no_ignore_frames,
                    ):
                        continue
                    out.append(f)
            continue

        project_root = _find_project_root(p).resolve()
        # Frame stack: shallowest → deepest. The root frame includes
        # hardcoded defaults + project-root .gitignore. ``--exclude``
        # (if any) sits ABOVE the root frame so its patterns override
        # the defaults — agents pass ``!node_modules/our-fork/`` and
        # it works without crafting the three-line escape idiom.
        # Nested ``.gitignore`` files encountered during the walk add
        # their own frames anchored at their containing dir (per git
        # semantics — a nested gitignore's patterns are relative to
        # that nested dir, not the project root).
        frames: list[tuple[Path, GitIgnoreSpec]] = [
            (project_root, _build_root_spec(project_root))
        ]
        if exclude_frame:
            frames.append(exclude_frame)

        for dirpath, dirs, files in os.walk(p):
            dpath = Path(dirpath).resolve()

            # Drop frames whose anchor is no longer an ancestor of the
            # current dir (we backed up the tree to a sibling). The
            # root frame is always kept — it covers every path. The
            # ``--exclude`` frame is anchored at the project root too,
            # so the same guard keeps it alive for the whole walk.
            frames = [
                (anchor, spec)
                for anchor, spec in frames
                if anchor == project_root or _is_ancestor_or_self(anchor, dpath)
            ]

            # If this dir has its own ``.gitignore`` and we haven't
            # already pushed a frame for it, add one now. The root's
            # ``.gitignore`` is already folded into the root frame —
            # don't double-load it.
            if dpath != project_root and not any(a == dpath for a, _ in frames):
                nested = _read_nested_spec(dpath)
                if nested is not None:
                    frames.append((dpath, nested))

            # Prune ignored subdirectories in place — git matches
            # directories with a trailing slash, so ``_is_ignored``
            # appends one for is_dir=True paths.
            kept = []
            for d in dirs:
                if _is_ignored(dpath / d, is_dir=True, frames=frames):
                    ignored_dirs += 1
                    ignored_dir_basenames.add(d)
                    continue
                kept.append(d)
            dirs[:] = sorted(kept)

            for fname in sorted(files):
                f = dpath / fname
                if glob:
                    if not f.match(glob):
                        continue
                else:
                    if (
                        f.suffix.lower() not in exts
                        and f.name not in basenames
                    ):
                        continue
                # File-level gitignore matches are filtered silently
                # (no count) — see CollectResult docstring.
                if _is_ignored(f, is_dir=False, frames=frames):
                    continue
                out.append(f)

    return CollectResult(
        files=out,
        ignored_dirs=ignored_dirs,
        ignored_dir_names=tuple(sorted(ignored_dir_basenames)),
    )


def _is_ancestor_or_self(anchor: Path, descendant: Path) -> bool:
    """True if ``anchor == descendant`` or ``anchor`` is a parent of it.

    Both paths must already be resolved. Used to keep gitignore
    frames whose subtree still encloses the dir being walked.
    """
    try:
        descendant.relative_to(anchor)
        return True
    except ValueError:
        return False
