"""Tests for ``.gitignore`` + default-fallback filtering in collect_files.

ast-outline is invoked blind by LLM agents, so the file walker must
exclude obvious junk (``node_modules``, ``__pycache__``, ``.git``,
``.venv``) by default and respect a project's ``.gitignore`` without
requiring any flags. These tests pin that contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from ast_outline.adapters import (
    CollectResult,
    collect_files,
    collect_files_with_stats,
)
from ast_outline.cli import main


def _write(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# --- Default / hardcoded patterns ----------------------------------------


def test_node_modules_pruned_without_gitignore(tmp_path):
    """Even without a ``.gitignore``, ``node_modules`` is treated as junk."""
    _write(tmp_path / "src" / "main.py", "def f(): pass\n")
    _write(tmp_path / "node_modules" / "lib" / "junk.py", "def junk(): pass\n")

    result = collect_files_with_stats([tmp_path])

    names = {f.name for f in result.files}
    assert "main.py" in names
    assert "junk.py" not in names
    assert result.ignored_dirs >= 1
    assert "node_modules" in result.ignored_dir_names


def test_pycache_pruned(tmp_path):
    _write(tmp_path / "a.py", "def f(): pass\n")
    _write(tmp_path / "__pycache__" / "a.cpython-312.pyc", "")
    # Stash a .py inside __pycache__ to verify directory-level pruning,
    # not just suffix-level — pyc is already filtered by extension.
    _write(tmp_path / "__pycache__" / "stale.py", "def stale(): pass\n")

    files = collect_files([tmp_path])
    names = {f.name for f in files}
    assert "a.py" in names
    assert "stale.py" not in names


def test_egg_info_glob_pruned(tmp_path):
    """``*.egg-info/`` is a glob pattern — the package name varies."""
    _write(tmp_path / "src" / "mypkg" / "__init__.py", "")
    _write(tmp_path / "mypkg.egg-info" / "PKG-INFO", "")
    _write(tmp_path / "mypkg.egg-info" / "stale.py", "def stale(): pass\n")
    _write(tmp_path / "another_pkg.egg-info" / "stale2.py", "def stale2(): pass\n")

    files = collect_files([tmp_path])
    names = {f.name for f in files}
    assert "stale.py" not in names
    assert "stale2.py" not in names


def test_dot_git_pruned(tmp_path):
    """``.git/`` is always pruned — agents should never recurse into it."""
    _write(tmp_path / "src" / "a.py", "def f(): pass\n")
    # Mimic a git repo's internal hooks dir holding sample .py scripts.
    _write(tmp_path / ".git" / "hooks" / "post-commit.py", "def hook(): pass\n")

    files = collect_files([tmp_path])
    assert all(".git" not in f.parts for f in files)


def test_venv_dirs_pruned(tmp_path):
    _write(tmp_path / "main.py", "def f(): pass\n")
    _write(tmp_path / ".venv" / "lib" / "x.py", "def venv(): pass\n")
    _write(tmp_path / "venv" / "lib" / "y.py", "def venv2(): pass\n")

    files = collect_files([tmp_path])
    names = {f.name for f in files}
    assert "main.py" in names
    assert "x.py" not in names
    assert "y.py" not in names


def test_default_junk_dirs_are_pruned(tmp_path):
    """Pin the cross-language junk-dir coverage.

    Each name listed here must remain in the hardcoded fallback list —
    these are dirs whose contents tree-sitter would happily parse but
    which never contain user-authored source. If any of these stop
    being filtered, agents start seeing generated code in their
    digests.
    """
    junk_dirs = [
        ".svn",
        ".hg",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        ".gradle",
        ".idea",
        ".vs",
        "__snapshots__",
        ".husky",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".vite",
        ".terraform",
    ]
    _write(tmp_path / "real.py", "def real(): pass\n")
    for d in junk_dirs:
        _write(tmp_path / d / "stale.py", "def stale(): pass\n")

    files = collect_files([tmp_path])
    names = {f.name for f in files}
    assert "real.py" in names
    # Every staged junk file should be filtered out.
    for d in junk_dirs:
        for f in files:
            assert d not in f.parts, f"{d}/ leaked through: {f}"


# --- .gitignore semantics -----------------------------------------------


def test_gitignore_respected(tmp_path):
    """A project ``.gitignore`` adds patterns on top of the defaults."""
    # Need .git/ so _find_project_root anchors here, otherwise pathspec
    # patterns won't be relative to the right root.
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "build/\n*.generated.py\n")
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "build" / "out.py", "def out(): pass\n")
    _write(tmp_path / "src" / "schema.generated.py", "def gen(): pass\n")

    result = collect_files_with_stats([tmp_path])

    names = {f.name for f in result.files}
    assert "real.py" in names
    assert "out.py" not in names  # pruned at dir level (build/)
    assert "schema.generated.py" not in names  # pruned at file level (silently)
    assert result.ignored_dirs >= 1
    assert "build" in result.ignored_dir_names


def test_gitignore_in_ancestor_is_used(tmp_path):
    """``.gitignore`` at git root applies even when scanning a subdir."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "ignored/\n")
    _write(tmp_path / "src" / "code" / "a.py", "def a(): pass\n")
    _write(tmp_path / "src" / "ignored" / "junk.py", "def junk(): pass\n")

    files = collect_files([tmp_path / "src"])
    names = {f.name for f in files}
    assert "a.py" in names
    assert "junk.py" not in names


def test_nonexistent_gitignore_does_not_break(tmp_path):
    """No ``.git`` and no ``.gitignore`` → only defaults apply, no errors."""
    _write(tmp_path / "a.py", "def f(): pass\n")
    files = collect_files([tmp_path])
    assert any(f.name == "a.py" for f in files)


# --- collect_files contract ---------------------------------------------


def test_collect_files_with_stats_returns_collect_result(tmp_path):
    _write(tmp_path / "a.py", "def f(): pass\n")
    _write(tmp_path / "node_modules" / "x.js", "")  # not supported ext, but dir is pruned anyway

    result = collect_files_with_stats([tmp_path])
    assert isinstance(result, CollectResult)
    assert isinstance(result.files, list)
    assert isinstance(result.ignored_dir_names, tuple)
    assert result.ignored_dirs == 1
    assert result.ignored_dir_names == ("node_modules",)


def test_collect_files_returns_plain_list(tmp_path):
    """The convenience wrapper still returns a list (backward-compat)."""
    _write(tmp_path / "a.py", "def f(): pass\n")
    files = collect_files([tmp_path])
    assert isinstance(files, list)
    assert all(isinstance(f, Path) for f in files)


def test_single_file_input_is_not_filtered(tmp_path):
    """Explicit file paths bypass directory-walk filtering entirely.

    If the user points at ``some/.venv/foo.py`` directly, they meant it
    — the ignore rules only kick in for directory traversal.
    """
    p = _write(tmp_path / ".venv" / "x.py", "def x(): pass\n")
    files = collect_files([p])
    assert files == [p]


# --- CLI surfacing ------------------------------------------------------


def test_cli_emits_note_when_ignoring(tmp_path, capsys):
    _write(tmp_path / "main.py", "def f(): pass\n")
    _write(tmp_path / "node_modules" / "junk.py", "def junk(): pass\n")

    # Text mode: the digest body renders, but on a successful batch the
    # ignore-note is JSON-only now (agents ignore the text line) — not
    # emitted to stdout text.
    rc = main(["digest", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# note: ignored" not in out
    assert "main.py" in out
    assert "junk" not in out

    # JSON mode: the note rides the `notes` array, basenames inline so the
    # agent can see *what* got skipped (prefix stripped in JSON).
    rc = main(["digest", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(
        "ignored" in n and "node_modules" in n
        and ".gitignore/.ignore + defaults" in n
        for n in data["notes"]
    )


def test_cli_note_lists_multiple_unique_dir_names(tmp_path, capsys):
    """The note shows distinct basenames sorted, not duplicates per location."""
    _write(tmp_path / "a.py", "def a(): pass\n")
    _write(tmp_path / "node_modules" / "x.py", "")
    _write(tmp_path / "sub" / "node_modules" / "y.py", "")  # repeated basename
    _write(tmp_path / "__pycache__" / "z.py", "")

    rc = main(["digest", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    note = next(n for n in data["notes"] if "ignored" in n)
    # Both unique basenames appear; the duplicate `node_modules` collapses.
    assert "node_modules" in note
    assert "__pycache__" in note
    # Count reflects scale (3 pruned dirs across the tree).
    assert "ignored 3 dirs" in note


def test_cli_no_note_on_clean_dir(tmp_path, capsys):
    """Clean directories with no junk produce no ignore note."""
    _write(tmp_path / "a.py", "def f(): pass\n")

    rc = main(["digest", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ignored" not in out


def test_cli_note_on_outline_command(tmp_path, capsys):
    """The note appears on ``outline`` too, not just ``digest`` — JSON-only
    on a successful batch, same as ``digest``."""
    _write(tmp_path / "main.py", "def f(): pass\n")
    _write(tmp_path / "__pycache__" / "stale.py", "def stale(): pass\n")

    # Text: body renders, no ignore-note line.
    rc = main(["outline", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# note: ignored" not in out
    assert "main.py" in out
    assert "stale" not in out

    # JSON: the note rides the `notes` array.
    rc = main(["outline", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any("ignored" in n and "__pycache__" in n for n in data["notes"])


# --- Nested .gitignore files --------------------------------------------


def test_nested_gitignore_adds_patterns_in_subtree(tmp_path):
    """A ``.gitignore`` in a subdir applies to that subdir + descendants.

    Patterns there are RELATIVE to the subdir — not the project root.
    """
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "")
    _write(tmp_path / "src" / ".gitignore", "*.gen.py\n")
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "src" / "schema.gen.py", "def gen(): pass\n")
    # Same name outside the nested gitignore's scope — should still be picked up.
    _write(tmp_path / "schema.gen.py", "def gen_outer(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "src/real.py" in rels
    assert "src/schema.gen.py" not in rels  # filtered by nested gitignore
    assert "schema.gen.py" in rels  # outside nested scope, kept


def test_nested_gitignore_can_unignore_parent_pattern(tmp_path):
    """Deeper ``.gitignore`` overrides parent via ``!`` negation.

    Mirrors git's actual behavior: more-specific rules win.
    """
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "*.skip.py\n")
    _write(tmp_path / "keep" / ".gitignore", "!*.skip.py\n")
    _write(tmp_path / "drop.skip.py", "def drop(): pass\n")
    _write(tmp_path / "keep" / "rescued.skip.py", "def rescued(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "drop.skip.py" not in rels
    assert "keep/rescued.skip.py" in rels  # un-ignored by nested negation


def test_user_can_unignore_default_dir_with_git_correct_pattern(tmp_path):
    """User can un-ignore a default-pruned dir using git's standard idiom.

    Git semantics (which we mirror): a negation alone can't re-include
    paths under an excluded parent dir — the parent itself must be
    un-excluded first. The standard three-line idiom is:

        !node_modules/             # un-exclude the dir (overrides default)
        node_modules/*             # exclude its top-level contents again
        !node_modules/our-fork/    # un-exclude one specific subdir

    This is the **monorepo escape hatch** for our hardcoded defaults.
    """
    (tmp_path / ".git").mkdir()
    _write(
        tmp_path / ".gitignore",
        "!node_modules/\nnode_modules/*\n!node_modules/our-fork/\n",
    )
    _write(tmp_path / "node_modules" / "third-party" / "x.py", "def junk(): pass\n")
    _write(tmp_path / "node_modules" / "our-fork" / "real.py", "def real(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "node_modules/third-party/x.py" not in rels
    assert "node_modules/our-fork/real.py" in rels


def test_sibling_subdirs_dont_leak_gitignore(tmp_path):
    """A ``.gitignore`` in dir A must not affect files in sibling dir B.

    Frame stack must pop A's frame before processing B.
    """
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "a" / ".gitignore", "shared.py\n")
    _write(tmp_path / "a" / "shared.py", "def in_a(): pass\n")  # ignored
    _write(tmp_path / "a" / "kept.py", "def kept(): pass\n")
    _write(tmp_path / "b" / "shared.py", "def in_b(): pass\n")  # NOT ignored

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "a/shared.py" not in rels
    assert "a/kept.py" in rels
    assert "b/shared.py" in rels


def test_deeply_nested_gitignores_chain(tmp_path):
    """Multiple levels of nested gitignore stack correctly."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "a.skip\n")
    _write(tmp_path / "lvl1" / ".gitignore", "*.b.py\n")
    _write(tmp_path / "lvl1" / "lvl2" / ".gitignore", "*.c.py\n")
    _write(tmp_path / "lvl1" / "lvl2" / "real.py", "def real(): pass\n")
    _write(tmp_path / "lvl1" / "lvl2" / "x.b.py", "def b(): pass\n")  # parent rule
    _write(tmp_path / "lvl1" / "lvl2" / "x.c.py", "def c(): pass\n")  # local rule

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "lvl1/lvl2/real.py" in rels
    assert "lvl1/lvl2/x.b.py" not in rels  # filtered by lvl1's gitignore
    assert "lvl1/lvl2/x.c.py" not in rels  # filtered by lvl2's gitignore


def test_nested_gitignore_works_when_starting_below_root(tmp_path):
    """Walk anchored below project root still picks up nested gitignores."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "")  # empty root gitignore
    _write(tmp_path / "pkg" / ".gitignore", "private/\n")
    _write(tmp_path / "pkg" / "public.py", "def pub(): pass\n")
    _write(tmp_path / "pkg" / "private" / "secret.py", "def secret(): pass\n")

    files = collect_files([tmp_path / "pkg"])
    rels = {f.name for f in files}
    assert "public.py" in rels
    assert "secret.py" not in rels


def test_unreadable_gitignore_does_not_crash(tmp_path, monkeypatch):
    """Read errors on a ``.gitignore`` shouldn't blow up the walk."""
    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".gitignore"
    gi.write_text("*.log\n")
    _write(tmp_path / "a.py", "def f(): pass\n")
    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self == gi:
            raise OSError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    # Should still return the .py file rather than raising.
    files = collect_files([tmp_path])
    assert any(f.name == "a.py" for f in files)


# --- .ignore (ripgrep / fd / ast-grep convention) -----------------------


def test_ignore_file_is_respected_alongside_gitignore(tmp_path):
    """``.ignore`` patterns are filtered just like ``.gitignore`` ones."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".ignore", "secret/\n")
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "secret" / "stash.py", "def stash(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "src/real.py" in rels
    assert "secret/stash.py" not in rels


def test_ignore_file_overrides_gitignore_on_conflict(tmp_path):
    """``.ignore`` has higher priority than ``.gitignore`` (ripgrep semantics).

    The classic case: ``.gitignore`` has ``vendor/`` (don't track) but
    ``.ignore`` has ``!vendor/our-fork/`` so the user can outline that
    one curated subdir without touching git's view.
    """
    (tmp_path / ".git").mkdir()
    # Use the standard escape idiom because git can't re-include past
    # an excluded parent directory — same constraint we test for the
    # hardcoded-default override case above.
    _write(tmp_path / ".gitignore", "vendor/\n")
    _write(
        tmp_path / ".ignore",
        "!vendor/\nvendor/*\n!vendor/our-fork/\n",
    )
    _write(tmp_path / "vendor" / "thirdparty" / "x.py", "def junk(): pass\n")
    _write(tmp_path / "vendor" / "our-fork" / "real.py", "def real(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "vendor/thirdparty/x.py" not in rels
    assert "vendor/our-fork/real.py" in rels  # rescued by .ignore


def test_ignore_file_can_hide_a_tracked_dir(tmp_path):
    """``.ignore`` can hide files from outline that ``.gitignore`` keeps tracked.

    Real use case: a generated `schema.gen.ts` is committed to git
    (so consumers don't need a build step), but you don't want it in
    your outline / digest.
    """
    (tmp_path / ".git").mkdir()
    # No .gitignore — the file is tracked.
    _write(tmp_path / ".ignore", "*.gen.py\n")
    _write(tmp_path / "real.py", "def real(): pass\n")
    _write(tmp_path / "schema.gen.py", "def gen(): pass\n")

    files = collect_files([tmp_path])
    names = {f.name for f in files}
    assert "real.py" in names
    assert "schema.gen.py" not in names


def test_nested_ignore_file_works(tmp_path):
    """``.ignore`` works nested in subdirs same as ``.gitignore``."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "pkg" / ".ignore", "private/\n")
    _write(tmp_path / "pkg" / "public.py", "def pub(): pass\n")
    _write(tmp_path / "pkg" / "private" / "secret.py", "def secret(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.name for f in files}
    assert "public.py" in rels
    assert "secret.py" not in rels


def test_nested_gitignore_and_ignore_compose(tmp_path):
    """``.gitignore`` and ``.ignore`` at different nesting levels compose.

    Pins the contract that the root frame (defaults + root files), a
    nested-A frame (gitignore-only), and a nested-B frame
    (ignore-only) all stack via the same deepest-first machinery —
    no conflicts, no bleed across siblings.
    """
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".gitignore", "*.bak\n")
    _write(tmp_path / "a" / ".gitignore", "drafts/\n")  # nested gitignore
    _write(tmp_path / "b" / ".ignore", "scratch/\n")     # nested ignore
    _write(tmp_path / "a" / "real.py", "def a(): pass\n")
    _write(tmp_path / "a" / "drafts" / "wip.py", "def wip(): pass\n")
    _write(tmp_path / "a" / "old.bak", "")  # excluded by root .gitignore
    _write(tmp_path / "b" / "real.py", "def b(): pass\n")
    _write(tmp_path / "b" / "scratch" / "tmp.py", "def tmp(): pass\n")

    files = collect_files([tmp_path])
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert "a/real.py" in rels
    assert "b/real.py" in rels
    assert "a/drafts/wip.py" not in rels  # nested .gitignore in a/
    assert "b/scratch/tmp.py" not in rels  # nested .ignore in b/
    assert "a/old.bak" not in rels  # root .gitignore (also: not a supported ext)


# --- IDE / editor dirs ---------------------------------------------------


def test_modern_ide_dirs_pruned(tmp_path):
    """Newer / popular IDE metadata dirs are filtered alongside ``.idea``."""
    _write(tmp_path / "real.py", "def real(): pass\n")
    for ide in [".vscode", ".cursor", ".zed", ".fleet", ".vs"]:
        _write(tmp_path / ide / "stale.py", "def stale(): pass\n")

    result = collect_files_with_stats([tmp_path])
    names = {f.name for f in result.files}
    assert "real.py" in names
    assert "stale.py" not in names
    for ide in [".vscode", ".cursor", ".zed", ".fleet", ".vs"]:
        assert ide in result.ignored_dir_names


# --- # note: line cap & dedup -------------------------------------------


def test_note_caps_dir_name_list_in_deep_monorepo(tmp_path, capsys):
    """Deep monorepos with >8 distinct dir names get a ``+N more`` tail."""
    _write(tmp_path / "main.py", "def f(): pass\n")
    # Stage 10 distinct ignore-default basenames.
    for d in [
        ".idea",
        ".vscode",
        ".cursor",
        ".zed",
        ".vs",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    ]:
        _write(tmp_path / d / "x.py", "")

    rc = main(["digest", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    note = next(n for n in data["notes"] if "ignored" in n)
    assert "ignored 10 dirs" in note
    assert "more" in note  # cap suffix surfaces


# --- --no-ignore flag --------------------------------------------------


def test_no_ignore_flag_disables_all_filtering(tmp_path, capsys):
    """``--no-ignore`` walks every dir, only filtering by extension."""
    _write(tmp_path / "src" / "main.py", "def main(): pass\n")
    _write(tmp_path / "node_modules" / "lib" / "vendored.py", "def vend(): pass\n")
    _write(tmp_path / "__pycache__" / "stale.py", "def stale(): pass\n")

    rc = main(["digest", str(tmp_path), "--no-ignore"])
    out = capsys.readouterr().out
    assert rc == 0
    # All three files must be in the digest output.
    assert "main.py" in out
    assert "vendored.py" in out
    assert "stale.py" in out
    # No "ignored" note since nothing was filtered.
    assert "ignored" not in out


def test_no_ignore_flag_at_collect_files_level(tmp_path):
    """The flag is wired through ``collect_files_with_stats`` too."""
    _write(tmp_path / "src" / "real.py", "def f(): pass\n")
    _write(tmp_path / "node_modules" / "x.py", "")
    _write(tmp_path / ".git" / "hooks" / "h.py", "")
    _write(tmp_path / ".gitignore", "vendored/\n")
    _write(tmp_path / "vendored" / "extra.py", "def e(): pass\n")

    result = collect_files_with_stats([tmp_path], no_ignore=True)
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    assert "src/real.py" in rels
    assert "node_modules/x.py" in rels
    assert ".git/hooks/h.py" in rels
    assert "vendored/extra.py" in rels
    # Nothing was treated as ignored, so stats stay zero.
    assert result.ignored_dirs == 0
    assert result.ignored_dir_names == ()


def test_note_includes_no_ignore_hint(tmp_path, capsys):
    """Note must teach the agent about the escape hatch (in the JSON note)."""
    _write(tmp_path / "main.py", "def f(): pass\n")
    _write(tmp_path / "node_modules" / "x.py", "")

    rc = main(["digest", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    note = next(n for n in data["notes"] if "ignored" in n)
    assert "--no-ignore" in note
    assert "disable" in note


def test_cli_note_when_only_ignored_dirs_match(tmp_path, capsys):
    """Empty result + ignored content surfaces the filter, not 'no files'.

    Without this, an agent pointed at a folder that's entirely ignored
    would see 'no supported files found' and conclude wrongly that the
    folder is empty.
    """
    _write(tmp_path / "node_modules" / "x.py", "def x(): pass\n")
    _write(tmp_path / ".venv" / "y.py", "def y(): pass\n")

    rc = main(["digest", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# note: ignored" in out
    assert "no supported files" not in out


# --- --exclude flag -----------------------------------------------------


def test_exclude_dir_at_collect_level(tmp_path):
    """``exclude`` argument prunes directories at the walker level."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "tests" / "test_a.py", "def test_a(): pass\n")

    result = collect_files_with_stats([tmp_path], exclude=["tests/"])
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    assert "src/real.py" in rels
    assert "tests/test_a.py" not in rels
    assert result.ignored_dirs >= 1
    assert "tests" in result.ignored_dir_names


def test_exclude_file_glob_at_collect_level(tmp_path):
    """File-level glob patterns work too — silent file drop, no dir count."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "src" / "schema.gen.py", "def gen(): pass\n")

    result = collect_files_with_stats([tmp_path], exclude=["*.gen.py"])
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    assert "src/real.py" in rels
    assert "src/schema.gen.py" not in rels


def test_exclude_supports_negation(tmp_path):
    """``!pattern`` re-includes something an earlier ``--exclude`` would drop."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "tests" / "test_a.py", "def a(): pass\n")
    _write(tmp_path / "tests" / "kept.py", "def kept(): pass\n")

    # Two patterns — second one un-ignores ``tests/kept.py``. Git
    # negation can't cross an excluded parent dir without re-including
    # it first, so we use the three-line escape idiom (same shape as
    # the existing test for ``.gitignore`` defaults).
    result = collect_files_with_stats(
        [tmp_path], exclude=["!tests/", "tests/*", "!tests/kept.py"]
    )
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    assert "tests/test_a.py" not in rels
    assert "tests/kept.py" in rels


def test_exclude_repeatable(tmp_path):
    """Multiple ``--exclude`` patterns combine (additive)."""
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "tests" / "t.py", "def t(): pass\n")
    _write(tmp_path / "src" / "g.gen.py", "def g(): pass\n")

    result = collect_files_with_stats(
        [tmp_path], exclude=["tests/", "*.gen.py"]
    )
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    assert "src/real.py" in rels
    assert "tests/t.py" not in rels
    assert "src/g.gen.py" not in rels


def test_exclude_anchored_at_project_root(tmp_path):
    """Patterns resolve against the project root, not the input dir.

    Agents pass ``--exclude src/generated/`` from anywhere and it
    applies the same — matches how a top-level ``.gitignore`` works,
    not how ad-hoc cwd-relative patterns would.
    """
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "src" / "generated" / "stub.py", "def stub(): pass\n")
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")

    # Walk a subdir, not the project root — pattern is still
    # interpreted relative to the root.
    result = collect_files_with_stats(
        [tmp_path / "src"], exclude=["src/generated/"]
    )
    rels = {f.name for f in result.files}
    assert "real.py" in rels
    assert "stub.py" not in rels


def test_exclude_applies_under_no_ignore(tmp_path):
    """``--exclude`` is an explicit narrowing — survives ``--no-ignore``.

    ``--no-ignore`` silences the auto-filter (``.gitignore`` + defaults).
    ``--exclude`` is the user's voice and must keep applying.
    """
    _write(tmp_path / "src" / "real.py", "def real(): pass\n")
    _write(tmp_path / "node_modules" / "lib.py", "def lib(): pass\n")
    _write(tmp_path / "secret" / "key.py", "def key(): pass\n")

    result = collect_files_with_stats(
        [tmp_path], no_ignore=True, exclude=["secret/"]
    )
    rels = {f.relative_to(tmp_path).as_posix() for f in result.files}
    # ``--no-ignore`` bypassed the defaults, so node_modules survives.
    assert "src/real.py" in rels
    assert "node_modules/lib.py" in rels
    # But the user's own ``--exclude`` still trims ``secret/``.
    assert "secret/key.py" not in rels


def test_exclude_does_not_filter_explicit_file_input(tmp_path):
    """Explicit file paths bypass ``--exclude`` too — pointing at a file
    is an explicit intent, same rule as ``.gitignore`` (see
    test_single_file_input_is_not_filtered).
    """
    p = _write(tmp_path / "tests" / "x.py", "def x(): pass\n")
    files = collect_files([p], exclude=["tests/"])
    assert files == [p]


def test_cli_exclude_works_on_digest(tmp_path, capsys):
    _write(tmp_path / "src" / "main.py", "def f(): pass\n")
    _write(tmp_path / "tests" / "test_main.py", "def t(): pass\n")

    rc = main(["digest", str(tmp_path), "--exclude", "tests/"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "main.py" in out
    assert "test_main.py" not in out


def test_cli_exclude_works_on_outline(tmp_path, capsys):
    _write(tmp_path / "src" / "main.py", "def f(): pass\n")
    _write(tmp_path / "tests" / "test_main.py", "def t(): pass\n")

    rc = main(["outline", str(tmp_path), "--exclude", "tests/"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "main.py" in out
    assert "test_main.py" not in out


def test_cli_exclude_works_on_grep(tmp_path, capsys):
    _write(tmp_path / "src" / "main.py", "def hit_me(): pass\n")
    _write(tmp_path / "tests" / "test_main.py", "def hit_me(): pass\n")

    rc = main(["grep", "hit_me", str(tmp_path), "--exclude", "tests/"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "src/main.py" in out
    assert "tests/test_main.py" not in out


def test_cli_exclude_repeatable_flag(tmp_path, capsys):
    _write(tmp_path / "src" / "main.py", "def f(): pass\n")
    _write(tmp_path / "tests" / "test_main.py", "def t(): pass\n")
    _write(tmp_path / "build" / "gen.py", "def g(): pass\n")

    rc = main([
        "digest",
        str(tmp_path),
        "--exclude", "tests/",
        "--exclude", "build/",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "main.py" in out
    assert "test_main.py" not in out
    assert "gen.py" not in out


def test_cli_exclude_note_mentions_exclude_source(tmp_path, capsys):
    """The ignored-dirs note widens its source list when ``--exclude``
    contributed, so an agent debugging "where did my folder go" sees
    its own flag named alongside ``.gitignore`` + defaults."""
    _write(tmp_path / "src" / "main.py", "def f(): pass\n")
    _write(tmp_path / "tests" / "test_main.py", "def t(): pass\n")

    rc = main(["digest", str(tmp_path), "--exclude", "tests/", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    note = next(n for n in data["notes"] if "ignored" in n)
    assert "+ --exclude" in note


def test_cli_exclude_bad_pattern_emits_note(tmp_path, capsys):
    """A malformed gitwildmatch pattern surfaces as ``# note: ...`` and
    returns 0 — preserves the CLI batch-friendliness invariant."""
    _write(tmp_path / "main.py", "def f(): pass\n")

    rc = main(["digest", str(tmp_path), "--exclude", "!"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# note: invalid --exclude pattern" in out


def test_cli_exclude_negation_re_includes_default_filtered_dir(tmp_path, capsys):
    """Single-line ``!node_modules/`` from ``--exclude`` overrides the
    hardcoded default — the ``--exclude`` frame is layered ABOVE the
    root frame, so its negation wins without crafting the three-line
    git escape idiom."""
    _write(tmp_path / "main.py", "def f(): pass\n")
    _write(
        tmp_path / "node_modules" / "our-fork" / "real.py",
        "def real(): pass\n",
    )
    _write(
        tmp_path / "node_modules" / "junk" / "stale.py",
        "def stale(): pass\n",
    )

    # ``!node_modules/`` alone un-excludes the dir. ``--exclude`` adds
    # its frame on top of the defaults, so a bare negation works here
    # — different shape from ``.gitignore`` where the parent dir's
    # exclusion stops the walk before the negation can apply. We then
    # add a positive pattern to keep ``junk`` filtered.
    rc = main([
        "digest",
        str(tmp_path),
        "--exclude", "!node_modules/",
        "--exclude", "node_modules/junk/",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "real.py" in out
    assert "stale.py" not in out
