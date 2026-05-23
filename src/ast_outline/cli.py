"""CLI entry point for ast-outline.

Error-handling philosophy
-------------------------
This CLI is consumed primarily by LLM agents (Claude Code, Cursor, etc.).
In those harnesses, a non-zero exit code from one tool call can fail the
whole parallel batch of bash invocations. So we deliberately do NOT use
exit codes to signal "no match" or "file not found" — instead we print a
short ``# note: ...`` line on stdout (the channel the agent reads as the
answer) and return 0. Real internal crashes still propagate normally.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._prompt import AGENT_PROMPT
from ._setup_prompt import SETUP_PROMPT
from .adapters import (
    CollectResult,
    collect_files_with_stats,
    get_adapter_for,
    supported_extensions,
)
from .core import (
    DigestOptions,
    OutlineOptions,
    ParseResult,
    find_symbols,
    render_digest,
    render_outline,
    render_signature_view,
    strip_leading_doc,
)
from . import json_output


SUBCOMMANDS = {"outline", "show", "help", "digest", "prompt", "setup-prompt", "grep"}


class _LLMArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that doesn't ``sys.exit`` on bad args.

    Default ``argparse`` behavior on bad arguments is to print to stderr
    and call ``sys.exit(2)``. For an LLM-facing CLI that breaks parallel
    bash chains in Claude Code. Instead we raise a sentinel exception
    that ``main()`` turns into a short ``# note:`` line on stdout +
    ``return 0``.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgParseFail(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        # ``--help`` flows through ``exit(0, None)`` after print_help — let
        # those through. Anything else (status != 0) is an arg failure.
        if status == 0:
            raise SystemExit(0)
        raise _ArgParseFail(message or f"argument error (status={status})")


class _ArgParseFail(Exception):
    """Raised by _LLMArgumentParser instead of sys.exit on bad args."""


def _cross_command_flag_hint(
    parser: argparse.ArgumentParser, message: str, argv: list[str]
) -> str:
    """Suggest the right subcommand when an unknown flag belongs to another.

    LLM agents routinely confuse subcommand-scoped flags (e.g. ``--signature``
    is `show`-only but tempting to pair with `outline`). Argparse's default
    "unrecognized arguments: --signature" doesn't tell the agent where the
    flag actually lives. This walks all subparsers, looks up each unknown
    flag, and returns the bare hint text naming the right command. The
    caller wraps it — `(hint: ...)` for text mode, a JSON field for
    `--json` mode. Returns "" when no hint applies.
    """
    prefix = "unrecognized arguments: "
    if not message.startswith(prefix):
        return ""
    tokens = message[len(prefix):].split()
    flag_tokens = [t.split("=", 1)[0] for t in tokens if t.startswith("-")]
    if not flag_tokens:
        return ""
    invoked = next((a for a in argv if a in SUBCOMMANDS), None)
    sub_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if sub_action is None:
        return ""
    hints: list[str] = []
    for flag in flag_tokens:
        owners = [
            name
            for name, sub in sub_action.choices.items()
            if name != invoked
            and any(flag in act.option_strings for act in sub._actions)
        ]
        if owners:
            owner_list = " / ".join(f"`{o}`" for o in owners)
            tail = f", not `{invoked}`" if invoked else ""
            hints.append(f"`{flag}` is a flag of {owner_list}{tail}")
    if not hints:
        return ""
    return "; ".join(hints)


# Grep flags that consume a value as the next argv token. Used by
# ``_normalize_grep_argv`` to skip values when scanning for free
# positionals. Kept in lockstep with the ``p_grep.add_argument`` calls
# below — if a new value-taking flag is added there, add it here.
_GREP_VALUE_FLAGS = frozenset({
    "-e", "--expression",
    "-m", "--max-count",
    "--kind",
})


def _normalize_grep_argv(argv: list[str]) -> list[str]:
    """Promote the first ``-e PAT`` value into the positional pattern
    slot when the user didn't supply a positional pattern.

    This makes ``ast-outline grep -e PAT PATHS...`` work the same as
    ``ast-outline grep PAT PATHS...`` — matching POSIX ``grep -e`` and
    ``rg -e`` conventions. Argparse can't express this on its own
    because the positional ``pattern`` (nargs=1) plus ``paths``
    (nargs="+") plus repeatable ``-e`` (action="append") together would
    become ambiguous if ``pattern`` were optional.

    The rewrite only fires when:
      * ``-e``/``--expression`` is present, AND
      * no free positional appears before the first ``-e`` value.
    Otherwise argv is returned unchanged so existing call shapes — both
    the canonical ``grep PAT PATH`` and the multi-pattern
    ``grep PAT -e PAT2 PATH`` — keep their current semantics.
    """
    if not argv or argv[0] != "grep":
        return argv

    rest = argv[1:]
    first_e_flag_idx: int | None = None
    first_e_value_idx: int | None = None  # equal to flag idx for --expression=PAT form
    promoted_value: str | None = None
    has_positional_before_e = False

    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            # Everything after `--` is positional — argparse handles it
            # natively, and any pattern positional must come before it.
            break
        # Long-form ``--expression=PAT`` (and `-e=PAT`, which argparse
        # also accepts for short opts via `=`).
        if a.startswith("--expression=") or a.startswith("-e="):
            if first_e_flag_idx is None:
                first_e_flag_idx = i
                first_e_value_idx = i
                promoted_value = a.split("=", 1)[1]
            i += 1
            continue
        if a in ("-e", "--expression"):
            if first_e_flag_idx is None and i + 1 < len(rest):
                first_e_flag_idx = i
                first_e_value_idx = i + 1
                promoted_value = rest[i + 1]
            i += 2
            continue
        if a in _GREP_VALUE_FLAGS:
            # Skip the flag and its value so the value isn't mistaken
            # for a free positional.
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            # Short bool flag (or combined like ``-li``) — none of the
            # value-taking short flags above are bool-combinable.
            i += 1
            continue
        # Free positional.
        if first_e_flag_idx is None:
            has_positional_before_e = True
        i += 1

    if first_e_flag_idx is None or has_positional_before_e or promoted_value is None:
        return argv

    if first_e_value_idx == first_e_flag_idx:
        # ``--expression=PAT`` — drop the single token.
        new_rest = rest[:first_e_flag_idx] + rest[first_e_flag_idx + 1:]
    else:
        # Separate ``-e PAT`` — drop both tokens.
        new_rest = rest[:first_e_flag_idx] + rest[first_e_flag_idx + 2:]
    return [argv[0], promoted_value, *new_rest]


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        _print_guide()
        return 0
    # Standalone `--version` / `-V` follows the universal CLI convention
    # (`git --version`, `python --version`, `rg --version`). We handle it
    # before argparse subcommand dispatch so the user doesn't need to
    # spell out a subcommand for a one-line capability check.
    if argv[0] in ("--version", "-V"):
        return _cmd_version(None)
    if argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["outline", *argv]
    if argv and argv[0] == "grep":
        argv = _normalize_grep_argv(argv)

    parser = _LLMArgumentParser(
        # `prog` is intentionally left unset so argparse picks up the actual
        # invoked binary name from sys.argv[0]. That way `ast-outline foo.py`
        # surfaces `ast-outline: error: ...` and the backward-compat
        # `ast-outline foo.py` alias still shows its own name — zero
        # confusion for existing users during the rebrand window.
        description="AST-based structural outline for source files. Signatures with line numbers — no method bodies.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_outline = sub.add_parser("outline", help="Print structural outline (default)")
    _add_outline_args(p_outline)

    p_show = sub.add_parser("show", help="Print source of one or more symbols")
    p_show.add_argument("file", help="Source file")
    p_show.add_argument("symbols", nargs="+", help="Symbol name(s), e.g. `TakeDamage Heal`")
    p_show.add_argument("--no-doc", action="store_true", help="Strip leading doc comments from output")
    # --view dials output depth: `full` is the existing body-extraction
    # behavior; `signature` returns just docs + attrs + signature (no body),
    # for "what's the contract of this method" queries that don't need the
    # implementation. The mutex group exposes `--signature` / `--full` as
    # short aliases — agents reach for boolean-style flags first, so we
    # accept both forms but route to a single `args.view` value.
    view_group = p_show.add_mutually_exclusive_group()
    view_group.add_argument(
        "--view",
        choices=["signature", "full"],
        default="full",
        help="Output depth: `signature` (header only) or `full` (default)",
    )
    view_group.add_argument(
        "--signature",
        dest="view",
        action="store_const",
        const="signature",
        help="Alias for `--view signature` — print docs+attrs+signature, no body",
    )
    view_group.add_argument(
        "--full",
        dest="view",
        action="store_const",
        const="full",
        help="Alias for `--view full` — print full source body (default)",
    )
    p_show.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text. An encoding "
             "switch — --view / --no-doc still apply to each match's "
             "`source` field.",
    )

    p_digest = sub.add_parser("digest", help="Compact public-API map of a directory")
    p_digest.add_argument("paths", nargs="+", help="Directories or files")
    p_digest.add_argument(
        "--format",
        choices=["names", "compact", "default", "wide"],
        default="default",
        help=(
            "Output format preset (default: default). "
            "names = one line per file, top-level symbols only. "
            "compact = hierarchical, no blank lines, no line ranges, no per-file counters. "
            "default = current full output. "
            "wide = default + private + fields + no max-members cap."
        ),
    )
    p_digest.add_argument(
        "--oneline",
        action="store_true",
        help="Alias for `--format=names`",
    )
    # `default=None` sentinel for per-flag preset overrides: when a user
    # doesn't pass the flag, the value resolved from the chosen `--format`
    # preset applies. When they pass it explicitly, that value wins.
    p_digest.add_argument("--include-private", action="store_true", default=None)
    p_digest.add_argument("--include-fields", action="store_true", default=None)
    p_digest.add_argument("--max-members", type=int, default=None)
    p_digest.add_argument(
        "--imports",
        action="store_true",
        help="Show each file's import / use / using statements as a header line",
    )
    p_digest.add_argument(
        "--no-ignore",
        action="store_true",
        help="Disable .gitignore / .ignore / hardcoded defaults — walk every dir except by extension",
    )
    p_digest.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Exclude paths matching gitwildmatch (.gitignore-syntax) "
            "GLOB; repeatable. Patterns are anchored at the project "
            "root, so `--exclude src/generated/` works regardless of "
            "cwd. Supports `!` negation. Applies even with --no-ignore."
        ),
    )
    p_digest.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text. An encoding "
             "switch — --include-private/-fields still apply; the "
             "--format layout and --max-members cap don't.",
    )

    p_help = sub.add_parser("help", help="Show usage guide with examples")
    p_help.add_argument(
        "topic",
        nargs="?",
        choices=["outline", "show", "digest", "prompt", "setup-prompt", "grep"],
        help="Topic-specific help",
    )

    sub.add_parser(
        "prompt",
        help="Print the canonical copy-paste agent prompt snippet (English, universal)",
    )

    sub.add_parser(
        "setup-prompt",
        help="Print the agent-facing setup-prompt — instructs an LLM to wire ast-outline into the current repo",
    )

    p_grep = sub.add_parser(
        "grep",
        help="Find pattern in code with scope and kind annotations (def/call/ref/import)",
    )
    # Positional pattern is required at the argparse layer (nargs="?"
    # collides with paths=nargs="+" — argparse can't disambiguate a
    # trailing string as path vs stray positional). The POSIX-style
    # `grep -e PAT PATHS...` form (no positional pattern) is supported
    # via a pre-argparse rewrite in ``_normalize_grep_argv`` that
    # promotes the first -e value into the positional slot.
    p_grep.add_argument(
        "pattern",
        help="Primary pattern (literal substring by default; combine with -e for more)",
    )
    p_grep.add_argument(
        "-e",
        "--expression",
        dest="extra_patterns",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Additional pattern to search for (repeatable, like rg / git grep)",
    )
    p_grep.add_argument("paths", nargs="+", help="Files or directories to search")
    p_grep.add_argument(
        "--regex",
        action="store_true",
        help="Treat all patterns as regular expressions instead of literal substrings",
    )
    p_grep.add_argument(
        "-i",
        "--case-insensitive",
        action="store_true",
        help="Case-insensitive match",
    )
    p_grep.add_argument(
        "-w",
        "--word",
        action="store_true",
        dest="word_match",
        help="Match whole words only (\\bpattern\\b boundaries — POSIX grep -w)",
    )
    p_grep.add_argument(
        "--include-noise",
        action="store_true",
        help="Include matches inside comments and strings (filtered by default)",
    )
    p_grep.add_argument(
        "--no-ignore",
        action="store_true",
        help="Disable .gitignore / .ignore filtering — walk every dir except by extension",
    )
    p_grep.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Exclude paths matching gitwildmatch (.gitignore-syntax) "
            "GLOB; repeatable. Patterns are anchored at the project "
            "root. Supports `!` negation. Applies even with --no-ignore."
        ),
    )
    p_grep.add_argument(
        "-m",
        "--max-count",
        type=int,
        default=None,
        metavar="NUM",
        dest="max_count",
        help="Stop after NUM matches per file (POSIX grep -m). A "
             "truncation note is appended whenever the cap fires so the "
             "agent never silently sees a partial result set.",
    )
    p_grep.add_argument(
        "--kind",
        action="append",
        default=[],
        metavar="KIND",
        help="Filter matches by kind: def | call | ref | import | "
             "comment | string. Repeatable (--kind def --kind call) or "
             "comma-separated (--kind def,call). When 'comment' or "
             "'string' are included, --include-noise is auto-enabled.",
    )
    output_mode = p_grep.add_mutually_exclusive_group()
    output_mode.add_argument(
        "-l",
        "--files-with-matches",
        action="store_true",
        dest="files_only",
        help="Output only paths of files containing matches (POSIX grep -l)",
    )
    output_mode.add_argument(
        "-c",
        "--count",
        action="store_true",
        dest="count_only",
        help="Output only counts per file as 'path:N' (POSIX grep -c)",
    )
    p_grep.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text. An encoding "
             "switch — query flags still apply; the -l / -c output "
             "modes don't (paths and counts are derivable from the JSON).",
    )

    try:
        args = parser.parse_args(argv)
    except _ArgParseFail as e:
        # Bad CLI usage. Surface it as the LLM's response on stdout and
        # exit cleanly so a parallel batch isn't aborted by exit code 2.
        msg = str(e)
        hint = _cross_command_flag_hint(parser, msg, argv)
        # `args` doesn't exist — argparse failed before producing it.
        # Detect `--json` directly in argv so a malformed JSON-mode
        # invocation still gets a valid JSON error document.
        if "--json" in argv:
            cmd = argv[0] if argv and argv[0] in SUBCOMMANDS else None
            print(json_output.error_json(cmd, [msg], hint or None))
            return 0
        suffix = f" (hint: {hint})" if hint else ""
        print(f"# note: {msg}{suffix}")
        return 0

    if args.cmd == "help":
        _print_guide(getattr(args, "topic", None))
        return 0
    if args.cmd == "show":
        return _cmd_show(args)
    if args.cmd == "digest":
        return _cmd_digest(args)
    if args.cmd == "prompt":
        return _cmd_prompt(args)
    if args.cmd == "setup-prompt":
        return _cmd_setup_prompt(args)
    if args.cmd == "grep":
        return _cmd_grep(args)
    return _cmd_outline(args)


def _cmd_version(_args) -> int:
    """Print version + authorship in the standard `tool x.y.z` form
    plus a one-line author / project-URL block. Matches the convention
    used by `git --version`, `python --version`, `rg --version`, etc.,
    so an LLM (or human) can grep `ast-outline version` for the same
    fields without parsing prose."""
    from . import __version__
    print(f"ast-outline {__version__}")
    print("author: Dmitrii Zaitsev <zayceffdev@gmail.com>")
    print("homepage: https://github.com/ast-outline/ast-outline")
    print("license: Apache-2.0")
    return 0


def _cmd_prompt(_args) -> int:
    """Print the canonical copy-paste LLM-agent prompt snippet verbatim."""
    # AGENT_PROMPT already terminates with `\n`. `end=""` suppresses
    # `print`'s extra newline, so stdout receives exactly the snippet
    # text + a single trailing `\n`. Matches the shape expected by
    # shell pipelines (`ast-outline prompt >> AGENTS.md` appends one
    # newline; the user inserts a blank separator line by hand if they
    # want one between existing content and the snippet).
    print(AGENT_PROMPT, end="")
    return 0


def _cmd_setup_prompt(_args) -> int:
    """Print the canonical setup-prompt for an LLM-agent installer flow.

    Distinct from ``ast-outline prompt`` — that command emits the
    use-time snippet meant for AGENTS.md / CLAUDE.md (steers an agent
    to prefer ast-outline whenever it reads code). This command emits
    the install-time snippet meant for one-shot consumption by a coding
    agent: a checklist that performs version check, AGENTS.md
    create/update, and optional patching of existing exploration
    subagents — all idempotent via marker-wrapped blocks.
    """
    print(SETUP_PROMPT, end="")
    return 0


def _add_outline_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", help="Files or directories to outline")
    p.add_argument("--no-private", action="store_true")
    p.add_argument("--no-fields", action="store_true")
    p.add_argument("--no-docs", action="store_true")
    p.add_argument("--no-attrs", action="store_true")
    p.add_argument("--no-lines", action="store_true")
    p.add_argument(
        "--imports",
        action="store_true",
        help="Show each file's import / use / using statements as a header line",
    )
    p.add_argument("--glob", default=None, help="Custom glob for directory mode (default: all supported extensions)")
    p.add_argument(
        "--no-ignore",
        action="store_true",
        help="Disable .gitignore / .ignore / hardcoded defaults — walk every dir except by extension",
    )
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Exclude paths matching gitwildmatch (.gitignore-syntax) "
            "GLOB; repeatable. Patterns are anchored at the project "
            "root. Supports `!` negation. Applies even with --no-ignore."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text. An encoding "
             "switch — content flags (--no-private/-fields/-docs/-attrs) "
             "still apply; layout flags (--no-lines, --imports) don't.",
    )


def _parse_paths(
    paths: list[Path],
    glob: str | None = None,
    no_ignore: bool = False,
    exclude: list[str] | None = None,
) -> tuple[list[ParseResult], list[tuple[Path, Exception]], CollectResult]:
    """Parse every supported file under the given paths.

    Returns the parsed results, per-file errors, and the raw collection
    stats (so callers can surface how many files/dirs were filtered out
    by ``.gitignore`` + defaults).
    """
    collected = collect_files_with_stats(
        paths, glob=glob, no_ignore=no_ignore, exclude=exclude
    )
    results: list[ParseResult] = []
    errors: list[tuple[Path, Exception]] = []
    for f in collected.files:
        adapter = get_adapter_for(f)
        if adapter is None:
            continue  # silently skip unsupported extensions
        try:
            results.append(adapter.parse(f))
        except Exception as e:
            errors.append((f, e))
    return results, errors, collected


def _validate_exclude(patterns: list[str]) -> str | None:
    """Return an error ``# note:`` line if any pattern is malformed.

    ``GitIgnoreSpec.from_lines`` raises ``GitWildMatchPatternError`` on
    syntactically bad patterns (lone ``!``, trailing backslash, …).
    Many other shapes parse silently but don't match what the user
    expected — we can't catch those, but we can at least give the
    structural failures a useful one-line note instead of a stack
    trace. Honors the CLI ``# note: + return 0`` invariant.
    """
    if not patterns:
        return None
    from pathspec import GitIgnoreSpec
    from pathspec.patterns.gitwildmatch import GitWildMatchPatternError
    try:
        GitIgnoreSpec.from_lines(patterns)
    except GitWildMatchPatternError as e:
        return f"# note: invalid --exclude pattern: {e}"
    return None


_MAX_DIR_NAMES_IN_NOTE = 8


def _ignore_note(collected: CollectResult, exclude_active: bool = False) -> str | None:
    """Format the ``# note:`` line for ignored entries, or ``None``.

    Lists the unique **basenames** of pruned dirs (capped at
    ``_MAX_DIR_NAMES_IN_NOTE`` to keep the line readable in deep
    monorepos) so the agent can see *what* got skipped, not just *how
    many*. The dir count itself is informative when one basename
    (e.g. ``node_modules``) is pruned in multiple places across a
    monorepo — list-of-1 + count-of-12 conveys both shape and scale.

    ``exclude_active`` widens the "source" suffix from
    ``.gitignore/.ignore + defaults`` to ``.gitignore/.ignore +
    defaults + --exclude`` whenever the caller passed any exclude
    pattern — even when the actual prunes might have come purely from
    defaults. Surfacing the flag's participation matters when an agent
    is debugging "why is my folder gone" and needs to suspect its own
    pattern before suspecting the auto-filter.
    """
    if collected.ignored_dirs == 0:
        return None
    names = list(collected.ignored_dir_names)
    if len(names) > _MAX_DIR_NAMES_IN_NOTE:
        shown = (
            ", ".join(names[:_MAX_DIR_NAMES_IN_NOTE])
            + f", … +{len(names) - _MAX_DIR_NAMES_IN_NOTE} more"
        )
    else:
        shown = ", ".join(names)
    word = "dir" if collected.ignored_dirs == 1 else "dirs"
    source = ".gitignore/.ignore + defaults"
    if exclude_active:
        source += " + --exclude"
    return (
        f"# note: ignored {collected.ignored_dirs} {word} ({shown}) "
        f"via {source} — pass --no-ignore to disable"
    )


def _strip_note_prefix(s: str) -> str:
    """Drop a leading `# note: ` / `# hint: ` marker if present.

    Some `# note:` producers (`_validate_exclude`, `_ignore_note`)
    return strings with the marker already baked in. JSON error
    objects carry the bare message, so we strip the marker when
    routing such a string into `error_json`.
    """
    for prefix in ("# note: ", "# hint: "):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _emit_error(
    json_mode: bool,
    command: str | None,
    notes: list[str],
    hint: str | None = None,
) -> int:
    """Print a user-facing failure, then return 0 (CLI exit-0 invariant).

    In `--json` mode the failure becomes a JSON error document so the
    consumer can always `json.loads()` stdout. In text mode each note
    is printed as a `# note:` line (a note that already carries the
    marker is printed verbatim).
    """
    if json_mode:
        clean = [_strip_note_prefix(n) for n in notes]
        clean_hint = _strip_note_prefix(hint) if hint else None
        print(json_output.error_json(command, clean, clean_hint))
    else:
        for note in notes:
            print(note if note.startswith("#") else f"# note: {note}")
        if hint:
            print(hint if hint.startswith("#") else f"# hint: {hint}")
    return 0


def _cmd_outline(args) -> int:
    json_mode = getattr(args, "json", False)
    paths_raw = getattr(args, "paths", None) or []
    if not paths_raw:
        # `# note:` lines go on stdout because they ARE the response to the
        # agent — there's no successful outline to keep clean of warnings.
        return _emit_error(
            json_mode, "outline",
            ["no input files. try: ast-outline Player.cs"],
        )

    paths = [Path(p) for p in paths_raw]
    missing = [p for p in paths if not p.exists()]
    if missing:
        return _emit_error(
            json_mode, "outline",
            [f"path not found: {p}" for p in missing],
        )

    exclude = getattr(args, "exclude", []) or []
    bad = _validate_exclude(exclude)
    if bad:
        return _emit_error(json_mode, "outline", [bad])

    opts = OutlineOptions(
        include_private=not args.no_private,
        include_fields=not args.no_fields,
        include_xml_doc=not args.no_docs,
        include_attributes=not args.no_attrs,
        include_line_numbers=not args.no_lines,
        show_imports=args.imports,
    )

    results, errors, collected = _parse_paths(
        paths, glob=args.glob, no_ignore=args.no_ignore, exclude=exclude
    )
    if not results:
        # All-failure path: surface parse errors on stdout as `# note:`
        # lines so the LLM agent (which only reads stdout) sees what
        # happened. Without this, an all-failed batch would print
        # nothing on stdout and the agent shows "(no output)".
        if errors:
            return _emit_error(
                json_mode, "outline",
                [f"parse error in {f}: {e}" for f, e in errors],
            )
        note = _ignore_note(collected, exclude_active=bool(exclude))
        if note:
            # Empty result + something ignored is the classic "the file
            # you wanted was filtered" trap — surface the filter so the
            # agent doesn't think the path is empty.
            return _emit_error(json_mode, "outline", [note])
        exts = sorted(supported_extensions())
        return _emit_error(
            json_mode, "outline",
            [f"no files found matching supported extensions: {exts}"],
        )

    note = _ignore_note(collected, exclude_active=bool(exclude))
    if json_mode:
        advisory = [_strip_note_prefix(note)] if note else []
        print(json_output.outline_json(results, opts=opts, notes=advisory))
        for f, e in errors:
            print(f"# WARN processing {f}: {e}", file=sys.stderr)
        return 0
    if note:
        print(note)
    for i, r in enumerate(results):
        if i > 0:
            print()
        print(render_outline(r, opts))
    # Per-file parse errors are warnings inside a successful batch — we
    # keep them on stderr so stdout (the LLM's primary channel) holds
    # only the actual outline content.
    for f, e in errors:
        print(f"# WARN processing {f}: {e}", file=sys.stderr)
    return 0


def _cmd_show(args) -> int:
    json_mode = getattr(args, "json", False)
    path = Path(args.file)
    if not path.is_file():
        return _emit_error(json_mode, "show", [f"file not found: {path}"])
    adapter = get_adapter_for(path)
    if adapter is None:
        return _emit_error(
            json_mode, "show", [f"no adapter for extension {path.suffix}"]
        )
    try:
        result = adapter.parse(path)
    except Exception as e:
        return _emit_error(
            json_mode, "show", [f"parse error in {path}: {e}"]
        )

    if json_mode:
        # One entry per queried name: not-found → empty matches list,
        # ambiguous → several matches. `--view` / `--no-doc` carry
        # through to each match's `source`, same as the text output.
        query_results = [(s, find_symbols(result, s)) for s in args.symbols]
        print(json_output.show_json(
            str(path), query_results, view=args.view, no_doc=args.no_doc
        ))
        return 0

    first = True
    for symbol in args.symbols:
        matches = find_symbols(result, symbol)
        if not matches:
            # Each requested symbol gets its own line. We use stdout — the
            # LLM is iterating over these to assemble its answer; it should
            # see "not found" inline next to the matches that did succeed.
            print(f"# note: symbol not found: {symbol} in {path}")
            continue
        if len(matches) > 1:
            # Disambiguation summary — informational, but still useful for
            # the agent to see alongside the bodies it's about to read.
            print(f"# {len(matches)} matches for '{symbol}' in {path}:", file=sys.stderr)
            for m in matches:
                print(f"#   {m.qualified_name}  L{m.start_line}-{m.end_line}  ({m.kind})", file=sys.stderr)
            print(file=sys.stderr)
        for m in matches:
            if not first:
                print()
            first = False
            print(f"# {path}:{m.start_line}-{m.end_line}  {m.qualified_name}  ({m.kind})")
            # Breadcrumb: show the enclosing namespace/class chain so the agent
            # knows what the extracted body is nested inside — without having
            # to call `outline` separately. Skipped for top-level symbols.
            if m.ancestor_signatures:
                chain = " → ".join(m.ancestor_signatures)
                print(f"# in: {chain}")
            if args.view == "signature":
                # Header-only view: docs + attrs + signature, no body. The
                # agent uses this when it knows the symbol name (post-digest)
                # and wants the contract — not the implementation. Falls back
                # to full source if the back-reference isn't populated, so
                # the caller never sees an empty body.
                sig = render_signature_view(m)
                if sig:
                    if args.no_doc:
                        sig = strip_leading_doc(sig)
                    print(sig)
                else:
                    src = m.source
                    if args.no_doc:
                        src = strip_leading_doc(src)
                    print(src)
            else:
                src = m.source
                if args.no_doc:
                    src = strip_leading_doc(src)
                print(src)
    return 0


def _cmd_grep(args) -> int:
    """Find pattern with scope and kind annotations.

    The intended consumer is an LLM agent that today does ``grep
    symbol → 20 hits → read 5 files``; this collapses that to one
    call by returning matches grouped under their enclosing
    class/function and labelled with kind (``def`` / ``call`` /
    ``ref`` / ``import``).
    """
    from .grep import grep, render_grep, _looks_like_regex, looks_like_ambiguous_regex

    json_mode = getattr(args, "json", False)
    # Non-fatal advisories (e.g. regex auto-promotion) shown alongside a
    # successful result. In text mode they print as `# note:` lines; in
    # JSON mode they ride in the envelope's `notes` field.
    advisory_notes: list[str] = []

    paths = [Path(p) for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        return _emit_error(
            json_mode, "grep", [f"path not found: {p}" for p in missing]
        )

    exclude = getattr(args, "exclude", []) or []
    bad = _validate_exclude(exclude)
    if bad:
        return _emit_error(json_mode, "grep", [bad])

    # Collect all patterns from the positional slot + every ``-e``
    # flag. Order is preserved (positional first, then -e in CLI
    # order) so the agent can predict how alternations bind. Empty
    # strings are filtered — they'd never produce useful matches.
    patterns: list[str] = []
    if args.pattern:
        patterns.append(args.pattern)
    patterns.extend(p for p in args.extra_patterns if p)
    if not patterns:
        return _emit_error(
            json_mode, "grep",
            ["no pattern — provide one as positional argument "
             "or via -e PATTERN (repeatable for multiple)"],
        )

    # Auto-promote to regex when any pattern carries unambiguous regex
    # syntax (``\|``, ``\d``, bare ``|``, ``(?:`` etc.). Agents fluent
    # in basic grep / rg often type ``Magnet\|Container`` expecting
    # alternation, and getting "no matches" on a literal interpretation
    # forces a wasted retry. The note documents the promotion so the
    # behavior isn't silent magic.
    #
    # BRE→ERE conversion: ``\|`` is alternation in basic regex (grep,
    # sed) but Python's ``re`` reads it as escaped literal pipe — the
    # opposite semantic. When auto-promoting we replace ``\|`` with
    # ``|``, matching the user's clear intent. Explicit ``--regex``
    # mode skips this conversion so power users keep raw Python regex
    # semantics.
    is_regex = args.regex
    if not is_regex:
        regex_like = [p for p in patterns if _looks_like_regex(p)]
        if regex_like:
            is_regex = True
            original = regex_like[0]
            patterns = [p.replace(r"\|", "|") for p in patterns]
            converted = original.replace(r"\|", "|")
            if converted != original:
                promo = (
                    f"{original!r} → {converted!r} "
                    f"(auto-promoted to regex; \\| as alternation; "
                    f"pass --regex for raw Python regex semantics)"
                )
            else:
                promo = (
                    f"pattern {original!r} contains regex syntax — "
                    f"auto-promoted to regex (pass --regex to silence)"
                )
            if json_mode:
                advisory_notes.append(promo)
            else:
                print(f"# note: {promo}")

    # ``--max-count`` validation: must be a positive integer. Zero and
    # negative values have no useful semantics — ``-m 0`` would render
    # empty ``(0 matches)`` headers via the truncation path; agents that
    # want a "did anything match" probe use ``-l`` directly without ``-m``.
    max_count = args.max_count
    if max_count is not None and max_count < 1:
        return _emit_error(
            json_mode, "grep", [f"--max-count must be ≥ 1 (got {max_count})"]
        )

    # ``--kind`` parsing: accept both repeated (``--kind def --kind call``)
    # and comma-separated (``--kind def,call``) forms — agents fluent in
    # ``rg --type`` reach for either, and supporting both costs nothing.
    # Normalize, validate, then auto-enable ``--include-noise`` when the
    # filter explicitly asks for ``comment``/``string`` (otherwise the
    # noise filter zeroes them out before the kind filter ever sees them
    # — silently giving the user empty results).
    kind_filter: set[str] | None = None
    include_noise = args.include_noise
    if args.kind:
        from .grep import (
            KIND_DEF, KIND_CALL, KIND_REF,
            KIND_IMPORT, KIND_COMMENT, KIND_STRING,
        )
        valid = {KIND_DEF, KIND_CALL, KIND_REF, KIND_IMPORT, KIND_COMMENT, KIND_STRING}
        kinds: set[str] = set()
        for entry in args.kind:
            for k in entry.split(","):
                k = k.strip().lower()
                if k:
                    kinds.add(k)
        invalid = kinds - valid
        if invalid:
            return _emit_error(
                json_mode, "grep",
                [f"invalid --kind value(s): {sorted(invalid)}; "
                 f"valid: {sorted(valid)}"],
            )
        kind_filter = kinds
        if kinds & {KIND_COMMENT, KIND_STRING}:
            include_noise = True

    file_results, _ignored_dirs, kind_excluded_counts = grep(
        patterns,
        paths,
        is_regex=is_regex,
        case_insensitive=args.case_insensitive,
        word_match=args.word_match,
        include_noise=include_noise,
        no_ignore=args.no_ignore,
        exclude=exclude,
        max_count=max_count,
        kind_filter=kind_filter,
    )
    if not file_results:
        # Zero matches is a valid empty result, not an error: JSON mode
        # emits a normal envelope with an empty `files` array. The text
        # `# hint:` nudges below are interactive guidance, omitted here.
        if json_mode:
            print(json_output.grep_json([], notes=advisory_notes))
            return 0
        shown = patterns[0] if len(patterns) == 1 else f"{len(patterns)} patterns"
        print(f"# note: no matches for {shown!r}")
        # Universal kind-filter hint: when ``--kind`` was the only thing
        # standing between the agent and a real result, tell them so
        # they can fix it in one retry instead of binary-searching.
        # Wording mirrors the existing `# hint:` style — what was
        # dropped, what to do about it. Suppressed if the regex-syntax
        # hint will fire below; one hint per empty result keeps the
        # output scannable.
        regex_hint_pending = (
            not is_regex
            and any(looks_like_ambiguous_regex(p) for p in patterns)
        )
        if kind_excluded_counts and kind_filter is not None and not regex_hint_pending:
            # Stable order: highest-count kind first (most likely what
            # the agent actually wanted), ties broken alphabetically.
            ranked = sorted(
                kind_excluded_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
            # Word the breakdown as natural counts ("4 ref, 1 def"),
            # not key=value pairs ("ref=4, def=1") — the latter reads
            # as a flag-value form (cf. ``--kind=ref``) and obscures
            # the fact that the numbers ARE the counts. Total prefix
            # gives the magnitude at a glance before the parens.
            breakdown = ", ".join(f"{n} {k}" for k, n in ranked)
            total = sum(kind_excluded_counts.values())
            kind_shown = ",".join(sorted(kind_filter))
            extend = ",".join(sorted(kind_filter | set(kind_excluded_counts.keys())))
            plural = "es" if total != 1 else ""
            print(
                f"# hint: --kind {kind_shown} excluded {total} match{plural} "
                f"({breakdown}) — retry with --kind {extend} or drop --kind"
            )
        # Warn-on-no-match: if any pattern carries metachars that might
        # have been intended as regex, hint at --regex. The strict
        # auto-detect already promoted the unambiguous cases, so we only
        # reach this hint for genuinely ambiguous patterns where literal
        # interpretation might have been wrong.
        if regex_hint_pending:
            ambiguous = [p for p in patterns if looks_like_ambiguous_regex(p)]
            print(
                f"# hint: pattern {ambiguous[0]!r} contains regex-like syntax "
                f"(escaped metachar, quantifier, or anchor) — if you meant "
                f"regex, retry with --regex"
            )
        return 0
    # `-l` / `-c` are output-mode selectors, not query filters — the
    # full structured document is emitted regardless (the consumer
    # derives the files list and per-file counts from it).
    if json_mode:
        print(json_output.grep_json(file_results, notes=advisory_notes))
        return 0
    # Output-mode dispatch — ``-l`` and ``-c`` short-circuit the
    # default scope-annotated render with grep-style compact formats
    # familiar from POSIX (``grep -l`` / ``grep -c``). Mutually
    # exclusive at the argparse level. Files with zero visible
    # matches are already absent from ``file_results`` (only files
    # with at least one visible or filtered match are returned), so
    # ``-c`` skips zero-files naturally — matches ``rg``'s default,
    # which excludes empty files unless ``--include-zero`` is set.
    if args.files_only:
        for fr in file_results:
            if fr.matches:
                print(fr.path)
        return 0
    if args.count_only:
        for fr in file_results:
            if fr.matches:
                print(f"{fr.path}:{len(fr.matches)}")
        return 0
    print(render_grep(file_results))
    return 0


def _cmd_digest(args) -> int:
    json_mode = getattr(args, "json", False)
    paths = [Path(p) for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        return _emit_error(
            json_mode, "digest",
            [f"path not found: {p}" for p in missing],
        )
    exclude = getattr(args, "exclude", []) or []
    bad = _validate_exclude(exclude)
    if bad:
        return _emit_error(json_mode, "digest", [bad])
    # `--oneline` is an alias for `--format=names`. If both are passed
    # they agree on `names`; if only `--oneline` is passed it overrides
    # whatever `--format` defaults to. Keeps the two-knob surface friendly
    # without a contradiction error path users have to read.
    fmt = "names" if args.oneline else args.format
    # Preset defaults — applied only for flags the user did NOT pass
    # explicitly (sentinel `None`). When the user passes the flag, their
    # value wins over the preset default (`kubectl`-style silent override).
    # `max_members` for `wide` is effectively unlimited; we use a large
    # int instead of `math.inf` to keep `DigestOptions.max_members_per_type`
    # a plain `int` (currently `dataclass` field typed as int).
    _PRESET_DEFAULTS = {
        "names":   {"include_private": False, "include_fields": False, "max_members": 50},
        "compact": {"include_private": False, "include_fields": False, "max_members": 50},
        "default": {"include_private": False, "include_fields": False, "max_members": 50},
        "wide":    {"include_private": True,  "include_fields": True,  "max_members": 10**9},
    }
    preset = _PRESET_DEFAULTS[fmt]
    opts = DigestOptions(
        include_private=(
            preset["include_private"] if args.include_private is None else args.include_private
        ),
        include_fields=(
            preset["include_fields"] if args.include_fields is None else args.include_fields
        ),
        max_members_per_type=(
            preset["max_members"] if args.max_members is None else args.max_members
        ),
        show_imports=args.imports,
        format=fmt,
    )
    results, errors, collected = _parse_paths(
        paths, no_ignore=args.no_ignore, exclude=exclude
    )
    if not results:
        # See `_cmd_outline` for rationale — an all-failure batch needs
        # the parse errors visible on stdout (the LLM's channel), not
        # only on stderr, otherwise the agent sees `# no files` (from
        # `render_digest([])`) and is misled into thinking the paths
        # had no source files.
        if errors:
            return _emit_error(
                json_mode, "digest",
                [f"parse error in {f}: {e}" for f, e in errors],
            )
        note = _ignore_note(collected, exclude_active=bool(exclude))
        if note:
            return _emit_error(json_mode, "digest", [note])
        return _emit_error(json_mode, "digest", ["no supported files found"])
    note = _ignore_note(collected, exclude_active=bool(exclude))
    if json_mode:
        advisory = [_strip_note_prefix(note)] if note else []
        print(json_output.digest_json(results, opts=opts, notes=advisory))
        for f, e in errors:
            print(f"# WARN processing {f}: {e}", file=sys.stderr)
        return 0
    if note:
        print(note)
    print(render_digest(results, opts), end="")
    # Per-file parse errors are warnings on a successful batch — stderr.
    for f, e in errors:
        print(f"# WARN processing {f}: {e}", file=sys.stderr)
    return 0


GUIDE_GENERAL = """\
ast-outline — structural outline for source files

WHAT IT DOES
    Prints class/method/function/field signatures with line numbers,
    WITHOUT method bodies. Typical output is 2–10× smaller than the source.
    Designed for LLM agents that need to understand a file's shape before
    reading (or editing) specific parts.

SUPPORTED LANGUAGES
    C#          .cs
    Python      .py, .pyi
    TypeScript  .ts, .tsx, .js, .jsx
    Java        .java
    Kotlin      .kt, .kts
    Scala       .scala, .sc
    Go          .go
    Lua         .lua, .wlua
    Swift       .swift
    HTML        .html, .htm
    Markdown    .md
    YAML        .yaml, .yml

COMMANDS
    ast-outline outline <paths...>          Print outline of files or dirs
    ast-outline show <file> <symbols...>    Print source of one or more symbols
    ast-outline digest <paths...>           Compact public-API map of a dir
    ast-outline grep <pattern> <paths...>   Find pattern with scope+kind annotations
    ast-outline prompt                      Print the canonical agent prompt snippet
    ast-outline setup-prompt                Print the install-time setup-prompt for an LLM agent
    ast-outline --version                   Print version + author
    ast-outline help [topic]                Show this guide (or topic-specific)

QUICK EXAMPLES
    ast-outline Player.cs
    ast-outline services/user_service.py
    ast-outline Assets/Scripts --no-private --no-fields
    ast-outline show Player.cs TakeDamage Heal
    ast-outline show user_service.py UserService.get_by_id
    ast-outline digest Assets/Scripts
    ast-outline digest scripts/

OUTPUT FORMAT
    # path/to/File.cs (N lines)
    namespace X.Y
        public class Foo : IBar  L10-120
            public int Count { get; private set; }  L15
            public void Do(int x)  L30-48

    For Python:
    # path/to/service.py (N lines)
    class UserService:  L8-120
        <docstring: "Handles user CRUD and auth flows.">
        def __init__(self, repo)  L12-14
        async def get_by_id(self, id: UUID) -> User  L18-28

    Each declaration shows: signature + line range `L<start>-<end>` (or `L<n>`
    for single-line items).

TIPS FOR LLM AGENTS
    1. Start broad → narrow:
         ast-outline digest <dir>        # architecture map of the module
         ast-outline <file>              # one file in detail
         ast-outline show <file> <Name>  # body of a specific symbol
    2. Symbol matching is suffix-based: `Foo.Bar` matches `*.Foo.Bar`.
    3. Use `--no-private --no-fields` for a pure public-API view.
"""

GUIDE_OUTLINE = """\
ast-outline outline — structural overview of source files

USAGE
    ast-outline outline <paths...> [flags]
    ast-outline <paths...> [flags]

SUPPORTED
    C# (.cs), Python (.py, .pyi), TypeScript/JavaScript (.ts/.tsx/.js/.jsx),
    Java (.java), Kotlin (.kt, .kts), Scala (.scala, .sc), Go (.go),
    Lua (.lua, .wlua), Swift (.swift), HTML (.html, .htm), Markdown (.md),
    YAML (.yaml, .yml)

FLAGS
    --no-private    Hide private members (Python: names starting with _)
    --no-fields     Hide field / variable declarations
    --no-docs       Hide doc comments (/// XML-doc or docstrings)
    --no-attrs      Hide [Attributes] / @decorators
    --no-lines      Hide line number suffixes
    --imports       Show file's imports (source-true, language-native)
    --glob PATTERN  Custom glob for directory mode (default: all supported)
    --exclude GLOB  Skip paths matching gitwildmatch GLOB (.gitignore
                    syntax; repeatable; anchored at project root;
                    `!` negates; applies even with --no-ignore)
    --no-ignore     Disable .gitignore / .ignore / hardcoded defaults
    --json          Emit machine-readable JSON instead of text. An
                    encoding switch — content flags (--no-private etc.)
                    still apply; layout flags (--no-lines, --imports) don't

EXAMPLES
    ast-outline Foo.cs
    ast-outline service.py
    ast-outline src/ --no-private --no-fields --no-attrs
    ast-outline service.py --imports     # add `# imports: ...` header
    ast-outline Foo.cs Bar.py   # mixed languages at once
    ast-outline User.swift      # Swift file
    ast-outline src/ --exclude tests/ --exclude '*.gen.*'   # skip tests + generated
"""

GUIDE_SHOW = """\
ast-outline show — extract source of one or more symbols

USAGE
    ast-outline show <file> <symbols...> [--no-doc] [--signature | --full]

SYMBOL SYNTAX
    Short name:      TakeDamage        get_by_id
    Class-scoped:    PlayerController.TakeDamage      UserService.get_by_id
    Fully-qualified: Game.Player.PlayerController.TakeDamage
    Matching is suffix-based — short name works unless ambiguous.

MARKDOWN HEADINGS — substring matching
    For .md files, headings match by case-insensitive substring of every
    dotted part. So `"current analysis"` finds
    `"1. CURRENT ANALYSIS (Feb 2026)"`, and `"intro.usage"` finds the
    nested heading `"Usage"` under any parent containing "intro".
    If the substring matches multiple headings, all are printed and a
    disambiguation summary lands on stderr — tighten the query to narrow.

MULTIPLE SYMBOLS
    Pass several names in one call:
        ast-outline show Player.cs TakeDamage Heal Die
        ast-outline show user_service.py get_by_id create update

BEHAVIOR
    - One match: prints its source (including preceding doc).
    - Multiple matches for a name (overloads, same name in different classes,
      or a markdown substring spanning several headings): all are printed,
      summary on stderr.
    - Always exits 0 — "not found" is printed as `# note: ...` on stdout
      so the LLM agent's parallel batch isn't aborted by an exit code.

FLAGS
    --no-doc        Strip leading /// or docstring block from output
    --signature     Header only: docs + attrs + signature line, no body.
                    Use after `digest` when you have the symbol name and
                    need the contract, not the implementation. Composes
                    with --no-doc to leave the bare signature.
    --full          Full source body (the default). Mutually exclusive
                    with --signature.
    --view {signature,full}
                    Long form of the depth selector. Equivalent to the
                    --signature / --full short flags.
    --json          Emit machine-readable JSON instead of text. One entry
                    per requested symbol (not-found = empty match list).
                    --view / --no-doc carry through to each match's source.
"""

GUIDE_DIGEST = """\
ast-outline digest — compact public-API map of a directory

USAGE
    ast-outline digest <paths...> [flags]

WHAT IT DOES
    Walks directory, lists every source file as:
      # legend: name()=callable, name [kind]=non-callable, ...
      <file>  (N lines, ~tokens)
        [Attr] <modifiers> <kind> <Name> [deprecated][ : <bases>]  L<start>-<end>
          <marker> method1(), method2(), property [property], ...
    The legend line is dynamic — only entries whose token shape
    actually appears in the rendered body are listed, so a YAML- or
    markdown-only batch (whose digest contains no callables, kinds,
    markers, or inheritance) emits no legend at all. Code batches
    nearly always carry a legend explaining whichever subset of
    tokens they use; the only exception is a batch whose every file
    contains nothing but empty type declarations, in which case
    `L<a>-<b>` is the sole token shape and the legend is dropped (a
    one-entry legend documenting line ranges adds noise without
    insight).
    Callable names carry `()`; properties / fields / events show
    `[kind]`. Method markers (`async`, `static`, `abstract`,
    `override`, `virtual`, Kotlin `open` / `suspend`, Python
    `@staticmethod` / `@classmethod` / `@abstractmethod`, Java
    `@Override`) prefix the name source-true so each language reads
    in its own idiom. Same-name overloads collapse to
    `name() [N overloads]`. Type headers carry their decorators /
    attributes verbatim (`@dataclass`, `[Serializable]`,
    `#[derive(Debug)]`) plus semantic modifiers (`abstract`,
    `sealed`, `static`, `final`, `open`, `partial`). Anything
    marked deprecated / obsolete gets a trailing `[deprecated]` tag.
    Members are joined by `, `. Types with bodies get a trailing
    blank line; empty types stack tight.

FLAGS
    --include-private   Include private members (Python: `_`-prefixed)
    --include-fields    Include fields / module-level assignments
    --max-members N     Truncate long member lists (default: 50)
    --imports           Show each file's imports (source-true, language-native)
    --exclude GLOB      Skip paths matching gitwildmatch GLOB
                        (.gitignore syntax; repeatable; anchored at
                        project root; `!` negates; applies even with
                        --no-ignore)
    --no-ignore         Disable .gitignore / .ignore / hardcoded defaults
    --json              Emit machine-readable JSON instead of text. An
                        encoding switch — --include-private/-fields still
                        apply; --format layout and --max-members don't

EXAMPLES
    ast-outline digest Assets/Scripts
    ast-outline digest scripts/
    ast-outline digest src/Services src/Domain
    ast-outline digest src/ --imports        # see what each file depends on
    ast-outline digest src/ --exclude tests/ --exclude '*.gen.*'   # skip tests + generated
"""

GUIDE_PROMPT = """\
ast-outline prompt — print the canonical agent prompt snippet

USAGE
    ast-outline prompt

WHAT IT DOES
    Prints the copy-paste-ready markdown snippet that steers an LLM
    coding agent (Claude, Cursor, etc.) to prefer `ast-outline` over
    full-file reads. English, universal — calibrated to work across
    Claude Opus 4.7 / Sonnet 4.6 / Haiku 4.5 out of the box.

    The snippet ships with the tool so `ast-outline prompt` always
    emits the current recommended version, not a stale copy someone
    saved a year ago.

EXAMPLES
    # Append straight into a project's agent config
    ast-outline prompt >> AGENTS.md
    ast-outline prompt >> .claude/CLAUDE.md

    # Pipe into clipboard
    ast-outline prompt | pbcopy          # macOS
    ast-outline prompt | xclip -sel c    # Linux
"""

GUIDE_SETUP_PROMPT = """\
ast-outline setup-prompt — print the install-time setup-prompt

USAGE
    ast-outline setup-prompt

WHAT IT DOES
    Prints a checklist meant for one-shot consumption by a coding
    agent (Claude Code, Codex CLI, Gemini CLI, Cursor). The agent
    follows it to wire ast-outline into the current repo:

      1. Verify `ast-outline --version` and best-effort check PyPI
         for a newer release.
      2. Append (or in-place upgrade) the canonical agent snippet
         to ./AGENTS.md, wrapped in markers so re-runs don't
         duplicate.
      3. Optionally patch existing exploration-oriented subagent
         files in .claude/agents/ / .codex/agents/ / .gemini/agents/
         (only with explicit user approval, per agent).

    Universal — same instruction works across Claude Opus 4.7 /
    Sonnet 4.6 / Haiku 4.5, OpenAI GPT-5.x, and Gemini 3.x.

    Distinct from `ast-outline prompt`:
      - `prompt`        — use-time snippet for AGENTS.md / CLAUDE.md
                          (steers code-reading behavior on every turn).
      - `setup-prompt`  — install-time checklist; one-shot integration.

EXAMPLES
    # In a Claude Code / Codex CLI / Gemini CLI session, ask the
    # agent to wire ast-outline into this repo:
    #     "Run `ast-outline setup-prompt` and follow its instructions."

    # Or pipe directly:
    ast-outline setup-prompt | pbcopy          # macOS clipboard
    ast-outline setup-prompt | xclip -sel c    # Linux clipboard
"""

GUIDE_GREP = """\
ast-outline grep — find pattern with scope and kind annotations

USAGE
    ast-outline grep <pattern> <paths...> [flags]
    ast-outline grep -e PATTERN [-e PATTERN]... <paths...> [flags]

WHAT IT DOES
    Like ripgrep, but each match is annotated with:
      - the enclosing class/function chain (where it sits structurally),
      - a kind tag for definitions ([def]) and imports ([import]).
        Calls and refs are unmarked — the line shape (identifier
        followed by `(` or not) makes them obvious.
    Matches inside comments and strings are filtered by default;
    when surfaced via --include-noise they carry [comment]/[string].
    Designed for LLM agents asking "where is X used", "who calls Y",
    "is Z dead code" — answers them in one call without follow-up
    file reads.

Not a replacement for ripgrep on non-symbol patterns (TODO comments,
log strings, regex queries) — fall back to `rg` for those.

FLAGS
    -e, --expression PAT    Additional pattern (repeatable; combines
                            with the positional pattern via OR. Use
                            multiple -e to search several symbols
                            in one walk — saves N startup costs)
    -w, --word              Whole-word match (POSIX grep -w; wraps
                            patterns in \\b boundaries — `save`
                            no longer matches `save_user` / `_save`)
    -l, --files-with-matches  Output only paths of files containing
                            matches (POSIX grep -l) — compact mode
                            for "where does X exist" queries
    -c, --count             Output `path:N` per file (POSIX grep -c) —
                            compact mode for distribution checks
    -m, --max-count NUM     Cap visible matches per file at NUM
                            (POSIX grep -m). Truncated files get a
                            `# truncated — N more...` footer so the
                            agent never silently sees a partial set
    --kind KIND             Filter matches by classification:
                            def | call | ref | import | comment | string.
                            Repeatable (--kind def --kind call) or
                            comma-separated (--kind def,call). When
                            comment/string included, --include-noise
                            is auto-enabled.
    --regex                 Treat all patterns as regular expressions
                            instead of literal substrings
    -i, --case-insensitive  Case-insensitive match
    --include-noise         Include matches inside comments / strings
                            (filtered by default)
    --no-ignore             Disable .gitignore / .ignore filtering
    --exclude GLOB          Skip paths matching gitwildmatch GLOB
                            (.gitignore syntax; repeatable; anchored
                            at project root; `!` negates; applies
                            even with --no-ignore)
    --json                  Emit machine-readable JSON instead of text. An
                            encoding switch — query flags still apply;
                            -l / -c don't (derivable from the JSON)

EXAMPLES
    ast-outline grep User.save src/
    ast-outline grep User.save -e User.load -e User.delete src/
    ast-outline grep -w save src/                   # whole word only
    ast-outline grep -l User src/                   # files containing User
    ast-outline grep -c TODO src/                   # count per file
    ast-outline grep -m 5 User src/                 # cap 5 matches per file
    ast-outline grep --kind def User src/           # only definitions of User
    ast-outline grep --kind call,ref save src/      # calls + refs (skip defs/imports)
    ast-outline grep --regex '\\.save\\(' src/
    ast-outline grep -i todo src/                   # case-insensitive
    ast-outline grep --include-noise FIXME src/
    ast-outline grep User src/ --exclude tests/ --exclude '*.gen.*'   # skip tests + generated

OUTPUT FORMAT
    # path/to/file.py (N matches)

    ## imports
      > L1: from .models import User [import]

    ## matches
    class Handler  L98-145
        def update(...)  L100-115
            > L108: user.save()

    Match line:  > L<line>: <code>[ <kind-tag>]
    Tagged kinds: [def] (function/class/variable definition),
    [import] (import statement). Calls and refs are untagged
    (inferable from line shape). [comment] and [string] only
    appear with --include-noise. Multi-pattern searches combine
    matches into a single output — read the line content to see
    which pattern hit.

NOT TO BE CONFUSED WITH
    `ast-grep` — a separate Rust tool for structural codemods using
    placeholder patterns ($_.save()). `ast-outline grep` is a
    scope-annotated symbol search, not a codemod tool.
"""


def _print_guide(topic: str | None = None) -> None:
    if topic == "outline":
        print(GUIDE_OUTLINE)
    elif topic == "show":
        print(GUIDE_SHOW)
    elif topic == "digest":
        print(GUIDE_DIGEST)
    elif topic == "prompt":
        print(GUIDE_PROMPT)
    elif topic == "setup-prompt":
        print(GUIDE_SETUP_PROMPT)
    elif topic == "grep":
        print(GUIDE_GREP)
    else:
        print(GUIDE_GENERAL)


if __name__ == "__main__":
    raise SystemExit(main())
