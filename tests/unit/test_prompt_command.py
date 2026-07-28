"""Tests for the `ast-outline prompt` subcommand and the underlying
AGENT_PROMPT constant that lives in `ast_outline._prompt`.

This subcommand prints the canonical copy-paste LLM-agent snippet used
to wire up coding agents (Claude, Cursor, etc.) to prefer `ast-outline`
over full-file reads. Tests check:

- The CLI command exits 0 and prints the snippet to stdout.
- Output is pure markdown (no ANSI colour codes, no extra banners) so
  `ast-outline prompt >> AGENTS.md` produces a clean append.
- `help prompt` renders a topic-specific guide mentioning the subcommand.
- The constant's content is well-formed: hits every major rule that the
  prompt-tuner review deemed load-bearing. If someone edits
  AGENT_PROMPT and accidentally drops one of these, the test catches
  the drift before it reaches a release.
"""
from __future__ import annotations

from ast_outline._prompt import AGENT_PROMPT
from ast_outline.cli import main


# --- CLI end-to-end ------------------------------------------------------


def test_prompt_command_exits_zero(capsys):
    rc = main(["prompt"])
    assert rc == 0


def test_prompt_command_prints_snippet(capsys):
    rc = main(["prompt"])
    out = capsys.readouterr().out
    assert rc == 0
    # The printed text must equal the constant (possibly with a single
    # trailing newline; we use `print(AGENT_PROMPT, end="")` which
    # preserves the string's own trailing `\n`).
    assert out == AGENT_PROMPT


def test_prompt_output_is_plain_markdown(capsys):
    """No ANSI escape codes — piping into a file should produce clean
    markdown, not a terminal-coloured blob."""
    main(["prompt"])
    out = capsys.readouterr().out
    assert "\x1b[" not in out          # no ANSI colour escapes
    assert "\x1b]" not in out          # no OSC sequences


def test_prompt_output_is_idempotent(capsys):
    """Running the command twice produces the same output."""
    main(["prompt"])
    first = capsys.readouterr().out
    main(["prompt"])
    second = capsys.readouterr().out
    assert first == second


# --- help prompt ---------------------------------------------------------


def test_help_prompt_topic(capsys):
    rc = main(["help", "prompt"])
    out = capsys.readouterr().out
    assert rc == 0
    # Topic-specific guide, not the general one
    assert "ast-outline prompt" in out
    assert "USAGE" in out
    assert "EXAMPLES" in out


def test_general_help_mentions_prompt_command(capsys):
    main(["help"])
    out = capsys.readouterr().out
    assert "ast-outline prompt" in out


# --- Snippet content checks (catch drift, don't over-assert wording) ----


def test_snippet_starts_with_markdown_h2():
    """Snippet must open with an `## ...` heading so it slots cleanly
    into an existing CLAUDE.md / AGENTS.md hierarchy."""
    first_line = AGENT_PROMPT.splitlines()[0]
    assert first_line.startswith("## ")


def test_snippet_mentions_every_supported_file_extension():
    """Every registered adapter must have at least one of its file
    extensions named in the snippet — otherwise an LLM agent reading
    `ast-outline prompt` on that language never learns the tool applies.

    Derived from ``ADAPTERS``, not a hardcoded list: the previous
    hardcoded-list version of this test silently missed the Swift
    adapter (`.swift` shipped in v0.9.4 with no prompt entry). Adding a
    new adapter without naming an extension in the snippet now fails
    here loudly."""
    from ast_outline.adapters import ADAPTERS

    for adapter in ADAPTERS:
        assert any(f"`{ext}`" in AGENT_PROMPT for ext in adapter.extensions), (
            f"{type(adapter).__name__}: snippet names none of "
            f"{sorted(adapter.extensions)}"
        )


def test_snippet_covers_all_subcommands():
    """Stop-at-the-step-that-answers workflow must reference every user-
    facing exploration subcommand. Dropping one would regress the
    snippet's completeness."""
    for cmd in ("digest", "show"):
        assert f"ast-outline {cmd}" in AGENT_PROMPT, f"snippet missing {cmd!r}"
    # And the default outline invocation (bare `ast-outline <paths…>`)
    assert "`ast-outline <paths…>`" in AGENT_PROMPT


def test_snippet_contains_parse_error_safety_clause():
    """The parse-error warning is a load-bearing safety clause — it
    tells the agent NOT to trust partial outlines silently."""
    assert "# WARNING:" in AGENT_PROMPT
    assert "parse error" in AGENT_PROMPT
    assert "partial" in AGENT_PROMPT


def test_snippet_has_scope_guardrail_against_over_execution():
    """The "menu, not a sequence" framing is the scope guardrail that
    stops literal models (Opus 4.7) and step-prescription-averse
    models (GPT-5.5) from mechanically running every workflow step.
    Without it, Opus over-executes; without it, GPT-5.5 reads the
    1/2/3 list as process prescription and degrades. Phrasing may
    evolve — what matters is that *some* explicit guard is present."""
    assert "menu, not a sequence" in AGENT_PROMPT
    assert "skip straight to" in AGENT_PROMPT


def test_snippet_avoids_emphasis_overuse():
    """Universal prompts should avoid ALL-CAPS / CRITICAL / YOU MUST
    stacks — they cause overtriggering on 4.5+ models."""
    # Genuine safety invariant gets one WARNING mention (the header text
    # agents look for). Everything else should be prose.
    upper_words = [w for w in AGENT_PROMPT.split() if len(w) >= 4 and w.isupper()]
    # One acceptable occurrence: the literal WARNING: token agents
    # search for. Anything more is emphasis overuse.
    assert len(upper_words) <= 2, f"unexpected all-caps words: {upper_words}"


def test_signature_flag_anchored_to_show_only():
    """Regression for the v0.8.2 fix. Earlier wording was
    "Add `--signature` to any of the above" inside the `show` section,
    which literal models (Opus 4.7, GPT-5.5) read globally and applied
    to `outline` and `digest`. The fix anchors the flag to `show`
    explicitly. Guard the wording so future edits don't reintroduce
    the ambiguous form."""
    assert "any of the above" not in AGENT_PROMPT, (
        "AGENT_PROMPT regressed to the ambiguous 'any of the above' wording "
        "for --signature. That phrasing caused agents to pass --signature "
        "to outline/digest where the flag does not exist."
    )
    assert "Add `--signature` to `show`" in AGENT_PROMPT, (
        "AGENT_PROMPT must anchor --signature to `show` explicitly."
    )


def test_snippet_ends_with_newline():
    """Appending with `ast-outline prompt >> AGENTS.md` must leave a
    clean newline so the next line doesn't concatenate."""
    assert AGENT_PROMPT.endswith("\n")


# --- Redirect-append use case (integration-ish) -------------------------


def test_appending_snippet_produces_valid_markdown(tmp_path, capsys):
    """Simulate `ast-outline prompt >> ~/AGENTS.md` flow: append output
    to an existing markdown file, ensure the result is parseable by eye
    (has required headers, no binary noise)."""
    existing = tmp_path / "AGENTS.md"
    existing.write_text("# Project instructions\n\nSome prior content.\n", encoding="utf-8")

    main(["prompt"])
    snippet = capsys.readouterr().out

    # User would redirect; we simulate by appending directly.
    with existing.open("a", encoding="utf-8") as f:
        f.write(snippet)

    combined = existing.read_text(encoding="utf-8")
    assert "# Project instructions" in combined          # prior content preserved
    assert "## Code exploration" in combined              # snippet appended
    # No null bytes / binary corruption
    assert "\x00" not in combined
