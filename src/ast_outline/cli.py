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
import glob
import re
import sys
import textwrap
from pathlib import Path

from ._prompt import AGENT_PROMPT
from ._setup_prompt import SETUP_PROMPT
from .adapters import (
    CollectResult,
    collect_files_with_stats,
    get_adapter_for,
    shebang_interpreter,
    supported_basenames,
    supported_extensions,
    supported_languages,
    supported_shebang_programs,
)
from .core import (
    DigestOptions,
    OutlineOptions,
    ParseResult,
    display_path,
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


# `show`-only flags stripped when a symbol-less `show` is repaired into
# `outline`. Value-taking ones (--view) drop their value token too.
_SHOW_ONLY_FLAGS = frozenset({"--signature", "--full", "--no-doc"})
_SHOW_ONLY_VALUE_FLAGS = frozenset({"--view"})


def _repair_argv(message: str, argv: list[str]) -> tuple[list[str], str] | None:
    """One-shot repair for arg failures with a single obvious reading.

    The cross-command hint (above) tells the agent what it got wrong —
    but acting on it still costs the agent a full extra turn, and
    usage-history shows these exact confusions are frequent (83×
    ``outline --format``, 42× symbol-less ``show``, habitual grep
    ``-r``/``-n``). When the intent is unambiguous, run it instead of
    explaining it; a ``# note:`` documents the substitution so nothing
    happens silently. Repairs are deliberately limited to the observed
    cases — anything else falls through to the normal failure note.
    Returns ``(new_argv, note)`` or ``None``.
    """
    # By the time parsing fails, ``main`` has already normalized the
    # bare default-outline form (``ast-outline FILE …`` → ``outline
    # FILE …``), so ``argv[0]`` here is always a subcommand.
    invoked = argv[0] if argv and argv[0] in SUBCOMMANDS else None
    is_outline = invoked == "outline"
    prefix = "unrecognized arguments: "
    if message.startswith(prefix):
        unknown = message[len(prefix):].split()
        flags = {t.split("=", 1)[0] for t in unknown if t.startswith("-")}
        if not flags:
            return None
        rest = argv[1:]
        if is_outline and flags <= {"--format", "--oneline"}:
            # The agent asked outline for a digest format preset — the
            # preset names the output it wants, so give it that output.
            return (
                ["digest"] + rest,
                "`--format` / `--oneline` are `digest` flags — ran "
                "`digest` on the same paths instead (outline has no "
                "format presets)",
            )
        if is_outline and flags == {"--signature"}:
            new_argv = [a for a in argv if a != "--signature"]
            return (
                new_argv,
                "`--signature` is a `show` flag; outline output is "
                "already signature-level — flag ignored",
            )
        if invoked == "grep" and flags <= {"-r", "-n", "-rn", "-nr"}:
            # rg / POSIX grep habits: recursion and line numbers are
            # both always on here, the flags change nothing.
            new_argv = [a for a in argv if a not in ("-r", "-n", "-rn", "-nr")]
            return (
                new_argv,
                "`-r` / `-n` are implicit in ast-outline grep "
                "(directories always walked recursively, line numbers "
                "always shown) — ignored",
            )
        return None
    if (
        invoked == "show"
        and "the following arguments are required: symbols" in message
    ):
        # `show FILE` without a symbol: the agent wants to see what's in
        # the file — that's `outline`. Show-only flags are stripped so
        # the repaired call can't fail a second time on them.
        rest: list[str] = []
        skip_next = False
        for a in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if a.split("=", 1)[0] in _SHOW_ONLY_VALUE_FLAGS:
                skip_next = "=" not in a
                continue
            if a in _SHOW_ONLY_FLAGS:
                continue
            rest.append(a)
        return (
            ["outline"] + rest,
            "`show` needs a symbol name — printed the file's outline "
            "instead; pick a symbol from it and re-run "
            "`show <file> <symbol>` for its body",
        )
    return None


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


def _force_utf8_io() -> None:
    """Make stdout/stderr emit UTF-8 regardless of the platform code page.

    On Windows the console streams inherit a legacy code page (e.g. cp1251);
    printing any non-ASCII character then dies with ``UnicodeEncodeError``.
    Output legitimately carries arbitrary Unicode from the user's own source
    files (identifiers, string defaults) plus our own ``→ — …`` notes, so we
    can't sanitise it away — we reconfigure the streams to UTF-8 instead.
    Agent harnesses already read the stdout pipe as UTF-8 (``json_output``
    serialises with ``ensure_ascii=False``), so this just formalises the
    existing assumption. Never raises: a stream without ``reconfigure``
    (e.g. pytest's capture object) or a closed pipe is skipped silently.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if encoding == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
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
    p_show.add_argument(
        "file",
        help="Source file, or a directory / quoted glob to search for "
             "the symbol's definition(s) and show the body in one call",
    )
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
    # `--no-ignore` / `--exclude` only bite when the target is a directory
    # (a single-file `show` reads exactly the file given). They mirror the
    # `grep` / `digest` flags so the directory search can reach an ignored
    # folder or prune extra paths.
    p_show.add_argument(
        "--no-ignore",
        action="store_true",
        help="Directory target only: disable .gitignore / .ignore / "
             "hardcoded defaults when searching for the symbol",
    )
    p_show.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "Directory target only: exclude paths matching gitwildmatch "
            "(.gitignore-syntax) GLOB; repeatable. Anchored at the project "
            "root. Supports `!` negation. Applies even with --no-ignore."
        ),
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
        help="Include matches inside comments (hidden by default; string literals are always searched)",
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
             "comma-separated (--kind def,call). When 'comment' is "
             "included, --include-noise is auto-enabled.",
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

    repair_note: str | None = None
    try:
        args = parser.parse_args(argv)
    except _ArgParseFail as e:
        # Bad CLI usage. Surface it as the LLM's response on stdout and
        # exit cleanly so a parallel batch isn't aborted by exit code 2.
        msg = str(e)
        hint = _cross_command_flag_hint(parser, msg, argv)

        def _emit_parse_failure() -> int:
            # `args` doesn't exist — argparse failed before producing
            # it. Detect `--json` directly in argv so a malformed
            # JSON-mode invocation still gets a valid JSON error
            # document.
            if "--json" in argv:
                cmd = argv[0] if argv and argv[0] in SUBCOMMANDS else None
                print(json_output.error_json(cmd, [msg], hint or None))
                return 0
            suffix = f" (hint: {hint})" if hint else ""
            print(f"# note: {msg}{suffix}")
            return 0

        # Forgiveness layer: when the mistaken invocation has exactly
        # one sensible reading, run that reading and note the
        # substitution instead of bouncing the agent for a retry turn.
        # Text mode only — a repair note line before a JSON document
        # would break consumers parsing stdout as JSON, and threading
        # it into every command's envelope isn't worth the plumbing.
        repaired = None if "--json" in argv else _repair_argv(msg, argv)
        if repaired is None:
            return _emit_parse_failure()
        new_argv, repair_note = repaired
        try:
            args = parser.parse_args(new_argv)
        except _ArgParseFail:
            # The repair didn't parse either (extra unknown flags in the
            # same call) — report the ORIGINAL failure, one repair
            # attempt only.
            return _emit_parse_failure()

    if repair_note:
        print(f"# note: {repair_note}")

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


def _extensionless_skip_note(paths: list[Path]) -> str | None:
    """Explain why an explicit extensionless file input produced
    nothing, or ``None`` when no input fits.

    Reached only from the empty-result branches, where every input is
    already known to have been skipped — so any extensionless regular
    file among them is one that shebang detection just failed on.
    Without this line the agent sees only the supported-extensions
    list, concludes the file class is unsupported, and reinvents the
    symlink-to-``/tmp/x.py`` workaround instead of fixing the actual
    problem (no/foreign shebang).
    """
    recognized = supported_shebang_programs()
    for p in paths:
        if not p.is_file() or p.suffix:
            continue
        program = shebang_interpreter(p)
        if program in recognized:
            # Detection succeeded — whatever emptied the result, it
            # wasn't this file's language (e.g. grep simply found no
            # matches in a recognized script).
            continue
        supported = ", ".join(recognized)
        if program:
            return (
                f"'{p}' is extensionless and its shebang interpreter "
                f"'{program}' is not supported (recognized: {supported})"
            )
        return (
            f"'{p}' is extensionless and has no shebang line — "
            f"extensionless files are detected via shebang "
            f"(recognized interpreters: {supported})"
        )
    return None


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
        skip_note = _extensionless_skip_note(paths)
        if skip_note and all(p.is_file() and not p.suffix for p in paths):
            # Every input is an extensionless file — the supported-
            # extensions list would only mislead (the problem is the
            # shebang, not the suffix), so the specific note rides alone.
            return _emit_error(json_mode, "outline", [skip_note])
        exts = sorted(supported_extensions())
        notes = [f"no files found matching supported extensions: {exts}"]
        if skip_note:
            notes.append(skip_note)
        return _emit_error(json_mode, "outline", notes)

    note = _ignore_note(collected, exclude_active=bool(exclude))
    if json_mode:
        advisory = [_strip_note_prefix(note)] if note else []
        print(json_output.outline_json(results, opts=opts, notes=advisory))
        for f, e in errors:
            print(f"# WARN processing {f}: {e}", file=sys.stderr)
        return 0
    # On a SUCCESSFUL batch (files found, some dirs pruned) the ignore-note
    # rides the JSON `notes` only — agents ignore the text line (~5.8% act
    # on it) and the pruned dirs are almost always junk (node_modules /
    # build / …). The empty-result path above keeps it in text: there it's
    # the "your folder was filtered" guard, not noise.
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


def _print_show_body(args, path, m) -> None:
    """Print one resolved symbol: header + breadcrumb + body (or signature).

    Shared by the file-mode and directory-mode `show` paths so the body
    rendering — line-range header, enclosing-scope breadcrumb, and the
    `--view` / `--no-doc` toggles — stays identical regardless of how the
    symbol was located. Emits no leading blank line; the caller owns the
    inter-body spacing.
    """
    print(
        f"# {display_path(path)}:{m.start_line}-{m.end_line}  "
        f"{m.qualified_name}  ({m.kind})"
    )
    # Breadcrumb: show the enclosing namespace/class chain so the agent
    # knows what the extracted body is nested inside — without having
    # to call `outline` separately. Skipped for top-level symbols.
    if m.ancestor_signatures:
        print(f"# in: {' → '.join(m.ancestor_signatures)}")
    if args.view == "signature":
        # Header-only view: docs + attrs + signature, no body. The agent
        # uses this when it knows the symbol name (post-digest) and wants
        # the contract — not the implementation. Falls back to full
        # source if the back-reference isn't populated, so the caller
        # never sees an empty body.
        rendered = render_signature_view(m)
        body = rendered if rendered else m.source
    else:
        body = m.source
    if args.no_doc:
        body = strip_leading_doc(body)
    print(body)


def _symbol_search_name(symbol: str) -> str:
    """The bare name token to pre-filter a directory by.

    `find_symbols` resolves a dotted / bracketed query by *suffix* — so a
    definition's own name token is the query's last path component
    (`Player.TakeDamage` → `TakeDamage`, `containers[0].image` →
    `image`). We pre-filter the directory with a `grep <name> --kind def`
    on that token, then let `find_symbols` re-apply the full path on the
    candidate files. Bare names pass through unchanged.
    """
    tail = re.split(r"[.\[]", symbol)[-1].rstrip("]")
    return tail or symbol


def _looks_like_glob(s: str) -> bool:
    """True if ``s`` carries shell-glob metacharacters (`*`, `?`, `[`).

    Used to decide whether a `show` path that is neither a file nor a
    directory should be treated as a quoted glob to expand, versus a
    plain missing path that still earns the precise "file not found"
    note. Conservative: a literal filename containing one of these is
    rare, and expanding it simply matches that one file. Edge: a
    *non-existent* path that happens to contain `[` (e.g. `foo[1].cs`)
    routes here and yields "no files match glob" rather than "file not
    found" — an acceptable trade for the common case.
    """
    return any(ch in s for ch in "*?[")


def _resolve_one_symbol(symbol, search_paths, *, no_ignore, exclude):
    """Locate one symbol's definition(s) across ``search_paths``.

    ``search_paths`` is the list handed to ``grep`` — a directory, the
    files a glob expanded to, or an explicit sibling-file list; ``grep``
    walks directories and takes explicit files alike. Reuses the grep
    file-walk + ``def`` classifier as a cheap pre-filter (it reads every
    file but only parses the few with a positional hit), then runs the
    authoritative ``find_symbols`` resolver on just those candidate
    files. Returns ``(found, suggestions)``, where ``found`` is a list
    of ``(file_path, SymbolMatch)`` and ``suggestions`` is the
    did-you-mean pool (only populated when ``found`` is empty).
    """
    from .grep import grep, suggest_similar_symbols, KIND_DEF

    name = _symbol_search_name(symbol)
    # `grep <name> <paths> --kind def` — the exact pattern an agent
    # would otherwise type as its second call. Literal (no
    # word_match): we mirror that command's default, and
    # `find_symbols` below exact-matches the name token, so a
    # substring candidate like `MailSpecHelper` is collected cheaply
    # then dropped precisely.
    file_results, _ignored, _excluded = grep(
        [name], search_paths,
        kind_filter={KIND_DEF},
        no_ignore=no_ignore,
        exclude=exclude,
    )
    found = []
    for fr in file_results:
        if not fr.matches:
            continue
        adapter = get_adapter_for(fr.path)
        if adapter is None:
            continue
        try:
            parsed = adapter.parse(fr.path)
        except Exception:
            continue
        for m in find_symbols(parsed, symbol):
            found.append((fr.path, m))
    suggestions = []
    if not found:
        suggestions = suggest_similar_symbols(
            symbol, search_paths, no_ignore=no_ignore, exclude=exclude
        )
    return found, suggestions


def _resolve_symbols_across(args, search_paths, *, no_ignore, exclude):
    """`_resolve_one_symbol` over each requested symbol, in input order.

    Returns a list of ``(symbol, found, suggestions)`` tuples.
    """
    return [
        (symbol, *_resolve_one_symbol(
            symbol, search_paths, no_ignore=no_ignore, exclude=exclude,
        ))
        for symbol in args.symbols
    ]


def _sibling_files(path: Path) -> list[Path]:
    """Supported files sharing ``path``'s directory — the not-found
    rescue scope for file-mode `show`.

    One level only, no recursion: the dominant miss in real usage is
    "right class, wrong file" — the agent guessed `ThingData.cs` while
    the symbol lives in `ThingIdGenerator.cs` next to it — and the
    same-directory scan resolves that without the cost or surprise of
    walking a tree the agent explicitly did not ask about. Filtering is
    by suffix/basename only (no shebang sniff): a script's directory
    like ``~/.local/bin`` can hold hundreds of extensionless files, and
    paying an ``open()`` per file to rescue a typo'd symbol would be
    wildly out of proportion.
    """
    exts = supported_extensions()
    basenames = supported_basenames()
    try:
        entries = sorted(path.parent.iterdir())
        # Canonical identity, not name comparison: the queried path may
        # be a symlink whose name differs from its target sitting in
        # the same directory — searching that target twice (and never
        # excluding it) would be wrong both ways.
        target = path.resolve()
        return [
            f for f in entries
            if f.is_file() and f.resolve() != target
            and (f.suffix.lower() in exts or f.name in basenames)
        ]
    except OSError:
        return []


def _render_did_you_mean(suggestions) -> str:
    """Format a did-you-mean pool the same way `grep` does."""
    return ", ".join(
        f"{n} ({k})" if c == 1 else f"{n} ({k} ×{c})"
        for n, k, c in suggestions
    )


def _render_show_candidates(found, *, absolute: bool = False) -> str:
    """Format the candidate locations for an ambiguous directory/glob `show`.

    ``found`` is the list of ``(file_path, SymbolMatch)`` the resolver
    returned for one symbol. Each candidate is rendered as
    ``path:start-end (kind)`` — the same shape as the body header
    `show` itself prints — so the agent can re-run `show <file>
    <symbol>` against exactly one of them, and the range lets it judge
    body size before choosing (or slice the lines directly).

    Path form follows the channel convention: the **text** note uses
    cwd-relative paths (``display_path``), matching the rest of the text
    output; the **JSON** note passes ``absolute=True`` so the path matches
    the structured ``file`` field in the same envelope and the
    JSON-is-absolute convention (a JSON consumer gets one resolvable form,
    not a prose path in a different shape than the structured one).
    """
    render = str if absolute else display_path
    return ", ".join(
        f"{render(fpath)}:{m.start_line}-{m.end_line} ({m.kind})"
        for fpath, m in found
    )


def _show_across(args, search_paths, *, directory, glob_pattern, json_mode: bool) -> int:
    """Resolve symbols across several files, then show or point to them.

    Collapses the agent's recurring two-call pattern — `grep <symbol>
    <scope> --kind def` to find the file, then `show <file> <symbol>` to
    read the body — into a single `show <scope> <symbol>`. ``scope`` is a
    directory (`show DIR sym`) or a glob (`show "src/**/*.cs" sym`); the
    walk is identical, only the displayed locator differs.

    `show` keeps a single-shape contract: when it prints *content* that
    content is always source code; when it can't (an ambiguous symbol
    defined in several places), it prints a `# note:` instead — never a
    mix. So a symbol resolved to one definition prints its body; one
    resolved to several prints a note listing the candidate locations and
    asking the agent to re-run against one file (it does not dump every
    body).

    Exactly one of ``directory`` / ``glob_pattern`` is the non-empty
    user-facing locator string; the other is ``""``. Both ride the JSON
    envelope so a consumer can tell which scope form produced the result.
    """
    locator = directory or glob_pattern
    no_ignore = getattr(args, "no_ignore", False)
    exclude = getattr(args, "exclude", []) or []
    resolved = _resolve_symbols_across(
        args, search_paths, no_ignore=no_ignore, exclude=exclude
    )

    if json_mode:
        notes = []
        for symbol, found, suggestions in resolved:
            if not found:
                notes.append(f"symbol not found: {symbol} in {locator}")
                if suggestions:
                    notes.append(
                        f"did you mean (for {symbol}): "
                        f"{_render_did_you_mean(suggestions)}?"
                    )
            elif len(found) > 1:
                # Ambiguous: the JSON mirrors the text contract — no code
                # bodies, just the candidate locations. The note repeats the
                # re-run guidance; the per-result `ambiguous` flag + body-less
                # matches carry the structured form (see show_dir_json).
                notes.append(
                    f"{len(found)} definitions of '{symbol}' "
                    f"— re-run with one of: "
                    f"{_render_show_candidates(found, absolute=True)}"
                )
        print(json_output.show_dir_json(
            directory,
            [(symbol, found) for symbol, found, _ in resolved],
            glob=glob_pattern, view=args.view, no_doc=args.no_doc, notes=notes,
        ))
        return 0

    first_block = True
    for symbol, found, suggestions in resolved:
        if not first_block:
            print()
        first_block = False
        if not found:
            print(f"# note: symbol not found: {symbol} in {locator}")
            if suggestions:
                print(f"# hint: did you mean: {_render_did_you_mean(suggestions)}?")
            continue
        if len(found) == 1:
            # Unambiguous: print the body, exactly as file-mode would. The
            # note names the file (the value the agent's second `grep` call
            # existed to get).
            fpath, m = found[0]
            print(f"# note: found '{symbol}' ({m.kind}) in {display_path(fpath)}")
            _print_show_body(args, fpath, m)
        else:
            # Ambiguous — the symbol is defined in several places. `show`
            # prints source code *or* a pointer note, never a mix: dumping
            # every body would make the output shape polymorphic (a parser
            # would have to branch on "is this code or a list?"). So we list
            # the candidate locations and ask the agent to re-run against
            # one. This also mirrors how agents disambiguate in practice —
            # they pick one definition (or a named subset), never read all N.
            print(
                f"# note: {len(found)} definitions of '{symbol}' "
                f"— re-run with one of: {_render_show_candidates(found)}"
            )
    return 0


def _cmd_show(args) -> int:
    json_mode = getattr(args, "json", False)
    path = Path(args.file)
    # Directory target → locate the symbol's definition(s) ourselves and
    # show the body, instead of the old "path is not a file" dead end.
    if path.is_dir():
        return _show_across(
            args, [path], directory=str(path), glob_pattern="",
            json_mode=json_mode,
        )
    # Glob target (quoted so the shell didn't expand it) → expand it
    # ourselves and search the matched files the same way. Only attempt
    # this when the string actually carries glob metacharacters and isn't
    # a real file, so a plain missing path still gets the precise
    # "file not found" note.
    if not path.is_file() and _looks_like_glob(args.file):
        matched = sorted(
            Path(p) for p in glob.glob(args.file, recursive=True)
        )
        if not matched:
            return _emit_error(
                json_mode, "show", [f"no files match glob: {args.file}"]
            )
        return _show_across(
            args, matched, directory="", glob_pattern=args.file,
            json_mode=json_mode,
        )
    if not path.is_file():
        return _emit_error(json_mode, "show", [f"file not found: {path}"])
    adapter = get_adapter_for(path)
    if adapter is None:
        if not path.suffix:
            skip_note = _extensionless_skip_note([path])
            # The fallback can't fire today (a recognized shebang would
            # have resolved an adapter above) — it guards the exit-0
            # invariant against future drift between the two checks.
            return _emit_error(
                json_mode, "show",
                [skip_note or f"no adapter for {path.name}"],
            )
        return _emit_error(
            json_mode, "show", [f"no adapter for extension {path.suffix}"]
        )
    try:
        result = adapter.parse(path)
    except Exception as e:
        return _emit_error(
            json_mode, "show", [f"parse error in {path}: {e}"]
        )

    no_ignore = getattr(args, "no_ignore", False)
    exclude = getattr(args, "exclude", []) or []
    # Listed once per `show` call, lazily on the first miss — the list
    # is identical for every missed symbol, and a fully-successful call
    # never pays the iterdir().
    sibling_cache: list[list[Path]] = []

    def _rescue_nearby(symbol):
        """Same-directory search for a symbol the requested file lacks.

        The requested file rides along in the search list: its own
        declaration names feed the did-you-mean pool (the typo case —
        `onLand` for `OnLand` — is usually a typo against THIS file),
        while `find_symbols`' exact matching guarantees it can't
        re-surface as a "found" hit for the very name that just missed.
        """
        if not sibling_cache:
            sibling_cache.append(_sibling_files(path))
        siblings = sibling_cache[0]
        if not siblings:
            return [], []
        return _resolve_one_symbol(
            symbol, [path] + siblings, no_ignore=no_ignore, exclude=exclude,
        )

    if json_mode:
        # One entry per queried name: not-found → empty matches list,
        # ambiguous → several matches. `--view` / `--no-doc` carry
        # through to each match's `source`, same as the text output.
        # A not-found symbol that exists in a sibling file is pointed to
        # via `notes` only: the structured `matches` are scoped to the
        # requested `file` field, and a match from another file inside
        # them would lie about its location.
        query_results = [(s, find_symbols(result, s)) for s in args.symbols]
        notes = []
        for symbol, matches in query_results:
            if matches:
                continue
            found, suggestions = _rescue_nearby(symbol)
            if found:
                tail = "it" if len(found) == 1 else "one of them"
                notes.append(
                    f"'{symbol}' is not in {path} but is defined in the "
                    f"same directory: "
                    f"{_render_show_candidates(found, absolute=True)} "
                    f"— re-run show against {tail}"
                )
            elif suggestions:
                notes.append(
                    f"did you mean (for {symbol}): "
                    f"{_render_did_you_mean(suggestions)}?"
                )
        print(json_output.show_json(
            str(path), query_results, view=args.view, no_doc=args.no_doc,
            notes=notes,
        ))
        return 0

    first = True
    for symbol in args.symbols:
        matches = find_symbols(result, symbol)
        if not matches:
            # Each requested symbol gets its own line. We use stdout — the
            # LLM is iterating over these to assemble its answer; it should
            # see "not found" inline next to the matches that did succeed.
            print(f"# note: symbol not found: {symbol} in {display_path(path)}")
            # Same-directory rescue: the dominant real-world miss is a
            # right-class-wrong-file guess, and the agent's next move was
            # a generic grep over the parent dir. The rescue only ever
            # POINTS — `path:start-end (kind)` candidates, never a body from a
            # file the agent didn't ask for: the agent requested THIS
            # file, and silently substituting another's source would put
            # unasked-for code where it expects its target.
            found, suggestions = _rescue_nearby(symbol)
            if found:
                tail = "it" if len(found) == 1 else "one of them"
                print(
                    f"# hint: defined in the same directory: "
                    f"{_render_show_candidates(found)} "
                    f"— re-run show against {tail}"
                )
            elif suggestions:
                print(
                    f"# hint: did you mean: "
                    f"{_render_did_you_mean(suggestions)}?"
                )
            continue
        if len(matches) > 1:
            # Disambiguation summary — informational, but still useful for
            # the agent to see alongside the bodies it's about to read.
            print(
                f"# {len(matches)} matches for '{symbol}' in {display_path(path)}:",
                file=sys.stderr,
            )
            for m in matches:
                print(f"#   {m.qualified_name}  L{m.start_line}-{m.end_line}  ({m.kind})", file=sys.stderr)
            print(file=sys.stderr)
        for m in matches:
            if not first:
                print()
            first = False
            _print_show_body(args, path, m)
    return 0


def _cmd_grep(args) -> int:
    """Find pattern with scope and kind annotations.

    The intended consumer is an LLM agent that today does ``grep
    symbol → 20 hits → read 5 files``; this collapses that to one
    call by returning matches grouped under their enclosing
    class/function and labelled with kind (``def`` / ``call`` /
    ``ref`` / ``import``).
    """
    from .grep import (
        grep, render_grep, _looks_like_regex, looks_like_ambiguous_regex,
        strip_definition_keyword, suggest_similar_symbols, KIND_DEF,
    )

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

    # Strip a leading definition keyword from a single literal pattern.
    # Agents habitually type the source-language keyword in front of the
    # name they want (``enum ItemSoundFamily``, ``class MailSpec``, ``def
    # foo``). As a literal substring that match starts on the keyword,
    # not the name token, so the def-classifier labels it ``ref`` and a
    # ``--kind def`` narrow drops it — a bare "no matches". Search the
    # identifier instead (so the match lands on the name → ``def``) and,
    # when the user gave no explicit ``--kind``, auto-narrow to ``def``
    # (the pattern was definitionally a declaration). Explicit ``--regex``
    # disables this — power users writing regex mean exactly what they
    # typed. Only the single-pattern form is handled: with multiple ``-e``
    # patterns an auto narrow-to-def would be ambiguous.
    auto_kind_def = False
    if not args.regex and len(patterns) == 1:
        stripped = strip_definition_keyword(patterns[0])
        if stripped is not None:
            ident, kw = stripped
            patterns = [ident]
            auto_kind_def = not args.kind
            # Record the strip for ``--json`` consumers, but keep it out
            # of the text output: agents ignore the FYI line and it only
            # spends tokens on every call (see the auto-promote note below
            # for the same rationale).
            if json_mode:
                if auto_kind_def:
                    advisory_notes.append(
                        f"searched {ident!r} as a definition "
                        f"(stripped leading {kw!r})"
                    )
                else:
                    advisory_notes.append(
                        f"searched {ident!r} (stripped leading {kw!r})"
                    )

    # Auto-promote to regex when any pattern carries unambiguous regex
    # syntax (``\|``, ``\d``, bare ``|``, ``(?:`` etc.). Agents fluent
    # in basic grep / rg often type ``Magnet\|Container`` expecting
    # alternation; a literal interpretation gives "no matches" and forces
    # a wasted retry, so we promote and just return the matches. The
    # promotion is recorded in ``advisory_notes`` (surfaced under
    # ``--json``) but kept out of the text output — agents reliably ignore
    # the FYI line and it only spends tokens on every call.
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
            if json_mode:
                converted = original.replace(r"\|", "|")
                if converted != original:
                    advisory_notes.append(
                        f"{original!r} → {converted!r} "
                        f"(auto-promoted to regex; \\| as alternation; "
                        f"pass --regex for raw Python regex semantics)"
                    )
                else:
                    advisory_notes.append(
                        f"pattern {original!r} contains regex syntax — "
                        f"auto-promoted to regex (pass --regex to silence)"
                    )

    # Validate regex compiles before walking the filesystem. The matcher
    # combines patterns and compiles again, but a failure there raises
    # ``re.error`` out of ``grep()`` as an uncaught traceback — violates
    # the CLI exit-0 invariant. Typical trigger: BRE→Python auto-promote
    # converts ``\|`` to ``|`` for alternation but leaves a literal ``(``
    # in the source pattern (e.g. ``foo\|bar\.method(``) — that bare
    # paren now opens an unterminated group in Python regex.
    if is_regex:
        bad: list[tuple[str, re.error]] = []
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                bad.append((p, exc))
        if bad:
            notes = [f"invalid regex {p!r}: {exc}" for p, exc in bad]
            hint = None
            if not args.regex:
                hint = (
                    "auto-promoted from BRE — if ``(`` or ``)`` were "
                    "meant literally, escape as ``\\(`` / ``\\)``; or "
                    "pass --regex to write a Python regex directly"
                )
            return _emit_error(json_mode, "grep", notes, hint=hint)

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
    # filter explicitly asks for ``comment`` (otherwise the noise filter
    # zeroes it out before the kind filter ever sees it — silently
    # giving the user empty results).
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
        if KIND_COMMENT in kinds:
            include_noise = True
    elif auto_kind_def:
        # Keyword-strip set no explicit ``--kind`` but the pattern was a
        # declaration by construction — narrow to ``def`` so the result
        # is the definition the agent asked for, not its call sites.
        kind_filter = {KIND_DEF}

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
        # An explicit extensionless input whose language couldn't be
        # detected was silently skipped by the search — without this
        # note "no matches" reads as "searched and found nothing",
        # which is a different (and misleading) claim.
        skip_note = _extensionless_skip_note(paths)
        # Zero matches is a valid empty result, not an error: JSON mode
        # emits a normal envelope with an empty `files` array. The text
        # `# hint:` nudges below are interactive guidance, omitted here.
        if json_mode:
            notes = advisory_notes + ([skip_note] if skip_note else [])
            print(json_output.grep_json([], notes=notes))
            return 0
        shown = patterns[0] if len(patterns) == 1 else f"{len(patterns)} patterns"
        print(f"# note: no matches for {shown!r}")
        if skip_note:
            print(f"# note: {skip_note}")
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
        kind_hint_fired = (
            bool(kind_excluded_counts)
            and kind_filter is not None
            and not regex_hint_pending
        )
        if kind_hint_fired:
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
        # did-you-mean fallback: only when neither the kind-filter hint
        # nor the regex hint fired (those already explain the empty
        # result) and the pattern is a single literal identifier. A true
        # no-match on a bare name is the blind-guess case — agents retry
        # permuted names (plural/singular, typo); surfacing the closest
        # real symbol in scope collapses that loop to one corrected call.
        if (
            not kind_hint_fired
            and not regex_hint_pending
            and len(patterns) == 1
            and not is_regex
        ):
            suggestions = suggest_similar_symbols(
                patterns[0], paths,
                no_ignore=args.no_ignore, exclude=exclude,
            )
            if suggestions:
                rendered = ", ".join(
                    f"{name} ({kind})" if count == 1
                    else f"{name} ({kind} ×{count})"
                    for name, kind, count in suggestions
                )
                print(f"# hint: did you mean: {rendered}?")
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
                print(display_path(fr.path))
        return 0
    if args.count_only:
        for fr in file_results:
            if fr.matches:
                print(f"{display_path(fr.path)}:{len(fr.matches)}")
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
        skip_note = _extensionless_skip_note(paths)
        if skip_note and all(p.is_file() and not p.suffix for p in paths):
            # See _cmd_outline: all-extensionless input gets the
            # specific shebang note alone, without the generic line.
            return _emit_error(json_mode, "digest", [skip_note])
        notes = ["no supported files found"]
        if skip_note:
            notes.append(skip_note)
        return _emit_error(json_mode, "digest", notes)
    note = _ignore_note(collected, exclude_active=bool(exclude))
    if json_mode:
        advisory = [_strip_note_prefix(note)] if note else []
        print(json_output.digest_json(results, opts=opts, notes=advisory))
        for f, e in errors:
            print(f"# WARN processing {f}: {e}", file=sys.stderr)
        return 0
    # Successful batch: the ignore-note rides the JSON `notes` only (see
    # `_cmd_outline` for the rationale — agents ignore the text line and
    # the pruned dirs are almost always junk). The empty-result path above
    # keeps it in text as the "your folder was filtered" guard.
    print(render_digest(results, opts), end="")
    # Per-file parse errors are warnings on a successful batch — stderr.
    for f, e in errors:
        print(f"# WARN processing {f}: {e}", file=sys.stderr)
    return 0


# The two help sections that list supported languages are built from the
# adapter registry, not hand-maintained — so a newly added adapter appears
# in the help automatically and the list can't drift out of sync with what
# the tool actually parses. Sentinels below are substituted via `.replace`
# (brace-safe — the guides contain literal `{ get; private set; }`).
def _render_supported_languages_table() -> str:
    """Aligned ``name  ext, ext`` rows for the general help, ADAPTERS order."""
    langs = supported_languages()
    width = max(len(name) for name, _ in langs)
    return "\n".join(
        f"    {name:<{width}}  {', '.join(exts)}" for name, exts in langs
    )


def _render_supported_languages_line() -> str:
    """Compact ``Name (ext, ext), …`` paragraph, wrapped and indented."""
    items = ", ".join(
        f"{name} ({', '.join(exts)})" for name, exts in supported_languages()
    )
    return textwrap.fill(
        items, width=76, initial_indent="    ", subsequent_indent="    "
    )


GUIDE_GENERAL = """\
ast-outline — structural outline for source files

WHAT IT DOES
    Prints class/method/function/field signatures with line numbers,
    WITHOUT method bodies. Typical output is 2–10× smaller than the source.
    Designed for LLM agents that need to understand a file's shape before
    reading (or editing) specific parts.

SUPPORTED LANGUAGES
%%SUPPORTED_LANGUAGES_TABLE%%

COMMANDS
    ast-outline outline <paths...>          Print outline of files or dirs
    ast-outline show <file|dir|glob> <symbols...> Print source of symbols (dir/glob → find + show)
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
    ast-outline show Assets/Scripts/App/Mail MailSpec   # find + show in a dir
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
         ast-outline show <file|dir|glob> <Name>  # body of a symbol (dir/glob → finds it)
    2. Symbol matching is suffix-based: `Foo.Bar` matches `*.Foo.Bar`.
    3. Use `--no-private --no-fields` for a pure public-API view.
"""

GUIDE_OUTLINE = """\
ast-outline outline — structural overview of source files

USAGE
    ast-outline outline <paths...> [flags]
    ast-outline <paths...> [flags]

SUPPORTED
%%SUPPORTED_LANGUAGES_LINE%%

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
    ast-outline show <file|dir|glob> <symbols...> [--no-doc] [--signature | --full]

SYMBOL SYNTAX
    Short name:      TakeDamage        get_by_id
    Class-scoped:    PlayerController.TakeDamage      UserService.get_by_id
    Fully-qualified: Game.Player.PlayerController.TakeDamage
    Matching is suffix-based — short name works unless ambiguous.

DIRECTORY / GLOB TARGET — find + show in one call
    Point `show` at a directory (or a quoted glob) and it locates the
    symbol's definition(s) itself — no separate `grep <symbol> DIR
    --kind def` first:
        ast-outline show Assets/Scripts/App/Mail MailSpec
        ast-outline show "Assets/Scripts/**/*.cs" MailSpec   # quote the glob
    - Defined in one file → prints the body, with
      `# note: found 'MailSpec' (class) in <path>`.
    - Defined in several files → prints NO body; lists the candidate
      locations instead: `# note: N definitions of 'MailSpec' — re-run
      with one of: a.cs:12-40 (class), b.cs:5-19 (class)`. (`show`
      prints code OR a pointer, never both — re-run against one file
      to read it.)
    - Not found → `# note: symbol not found`. When the definition lives
      in another file of the same directory, a hint points to it
      (`# hint: defined in the same directory: <path>:<start>-<end>
      (<kind>)`); otherwise a did-you-mean hint fires when a close name
      exists. Always exits 0.
    A DIRECTORY search honors .gitignore/.ignore like `grep`/`digest`
    (use --no-ignore / --exclude to adjust). A GLOB is expanded
    literally — it shows exactly the files the pattern matches, with no
    ignore-filtering (you already narrowed via the pattern), so quote it
    to keep the shell from expanding it first (especially `**`). All
    flags below apply to the located file(s).

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
    - Multiple matches in a FILE target (overloads, same name in different
      classes, or a markdown substring spanning several headings): all are
      printed, summary on stderr. (For a DIR / GLOB target the rule is the
      opposite — see DIRECTORY / GLOB TARGET above: N>1 prints no body, just
      a candidate list. `show` prints code OR a pointer, never both.)
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
                    DIR / GLOB target: each result carries an `ambiguous`
                    flag — `ambiguous: true` (N>1) results list body-less
                    candidate locators (`file` / `kind` / `qualified_name` /
                    `start_line` / `end_line`, no `source`), and the re-run
                    guidance is echoed in `notes`; re-run `show <file>
                    <symbol>` against one to read it.
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
      - a kind tag for definitions ([def]), imports ([import]) and
        string-literal hits ([string]). Calls and refs are unmarked —
        the line shape (identifier followed by `(` or not) makes
        them obvious.
    Matches inside comments are hidden by default; --include-noise
    surfaces them tagged [comment]. String literals are always
    searched — strings are program data (dict/config/translation
    keys, asset paths, reflection targets), so a hit there is a real
    answer, not noise.
    Designed for LLM agents asking "where is X used", "who calls Y",
    "is Z dead code" — answers them in one call without follow-up
    file reads.

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
                            comment is included, --include-noise is
                            auto-enabled.
    --regex                 Treat all patterns as regular expressions
                            instead of literal substrings
    -i, --case-insensitive  Case-insensitive match
    --include-noise         Include matches inside comments (hidden
                            by default; strings always searched)
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
    [import] (import statement), [string] (hit inside a string
    literal). Calls and refs are untagged (inferable from line
    shape). [comment] only appears with --include-noise.
    Multi-pattern searches combine matches into a single output —
    read the line content to see which pattern hit.

NOT TO BE CONFUSED WITH
    `ast-grep` — a separate Rust tool for structural codemods using
    placeholder patterns ($_.save()). `ast-outline grep` is a
    scope-annotated symbol search, not a codemod tool.
"""


# Substitute the language-list sentinels once, at import — keeps the
# guides as plain strings for consumers while their content stays derived
# from the adapter registry.
GUIDE_GENERAL = GUIDE_GENERAL.replace(
    "%%SUPPORTED_LANGUAGES_TABLE%%", _render_supported_languages_table()
)
GUIDE_OUTLINE = GUIDE_OUTLINE.replace(
    "%%SUPPORTED_LANGUAGES_LINE%%", _render_supported_languages_line()
)


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
