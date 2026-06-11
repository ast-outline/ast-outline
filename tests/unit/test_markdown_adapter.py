"""Tests for the Markdown adapter."""
from __future__ import annotations

import pytest

from ast_outline.adapters.markdown import MarkdownAdapter
from ast_outline.core import (
    KIND_CODE_BLOCK,
    KIND_HEADING,
    Declaration,
    DigestOptions,
    OutlineOptions,
    render_digest,
    render_outline,
)
from ast_outline.grep import grep


def _find(decls, kind=None, name=None):
    for d in decls:
        if (kind is None or d.kind == kind) and (name is None or d.name == name):
            return d
        hit = _find(d.children, kind=kind, name=name)
        if hit is not None:
            return hit
    return None


def _find_all(decls, kind=None, name=None):
    out: list[Declaration] = []
    for d in decls:
        if (kind is None or d.kind == kind) and (name is None or d.name == name):
            out.append(d)
        out.extend(_find_all(d.children, kind=kind, name=name))
    return out


# --- Parse smoke ---------------------------------------------------------


def test_parse_populates_metadata(md_dir):
    path = md_dir / "readme_style.md"
    r = MarkdownAdapter().parse(path)
    assert r.path == path
    assert r.language == "markdown"
    assert r.line_count > 0
    assert r.declarations


def test_empty_prose_has_no_decls(md_dir):
    r = MarkdownAdapter().parse(md_dir / "empty.md")
    assert r.declarations == []


# --- Heading hierarchy ---------------------------------------------------


def test_h1_wraps_subsections(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    # Single H1 at the top
    assert len(r.declarations) == 1
    h1 = r.declarations[0]
    assert h1.kind == KIND_HEADING
    assert h1.name == "Sample Project"
    assert h1.signature == "# Sample Project"
    # H2s live as children of the H1
    h2_names = {c.name for c in h1.children if c.kind == KIND_HEADING}
    assert {"Installation", "Usage", "Contributing", "License"}.issubset(h2_names)


def test_h2_wraps_h3(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    install = _find(r.declarations, kind=KIND_HEADING, name="Installation")
    h3_names = {c.name for c in install.children if c.kind == KIND_HEADING}
    assert {"From source", "Via pipx"}.issubset(h3_names)


def test_deeper_nesting_preserved(md_dir):
    r = MarkdownAdapter().parse(md_dir / "article.md")
    arg = _find(r.declarations, kind=KIND_HEADING, name="The Argument")
    second = _find(arg.children, kind=KIND_HEADING, name="Second Point")
    # H4 nested under H3 nested under H2
    h4 = _find(second.children, kind=KIND_HEADING)
    assert h4 is not None
    assert h4.signature.startswith("#### ")
    assert h4.name == "A nested detail under the second point"


def test_signature_reflects_heading_level(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    h1 = r.declarations[0]
    install = _find(h1.children, name="Installation")
    from_src = _find(install.children, name="From source")
    assert h1.signature.startswith("# ")
    assert install.signature.startswith("## ")
    assert from_src.signature.startswith("### ")


def test_line_ranges_span_the_whole_section(md_dir):
    """A section's end_line should be at or before the next same-level heading."""
    path = md_dir / "readme_style.md"
    r = MarkdownAdapter().parse(path)
    h1 = r.declarations[0]
    install = _find(h1.children, name="Installation")
    usage = _find(h1.children, name="Usage")
    # Installation ends before Usage begins
    assert install.end_line <= usage.start_line
    # Installation actually covers several lines
    assert install.end_line > install.start_line


# --- Setext headings -----------------------------------------------------


def test_setext_h1_and_h2_detected(md_dir):
    r = MarkdownAdapter().parse(md_dir / "setext_and_codes.md")
    # Setext H1 is the first top-level heading
    h1 = r.declarations[0]
    assert h1.kind == KIND_HEADING
    assert h1.name == "Setext H1 Title"
    assert h1.signature.startswith("# ")  # level 1
    # Setext H2 should exist as a child
    setext_h2 = _find(h1.children, name="Setext H2 Subheading")
    assert setext_h2 is not None
    assert setext_h2.signature.startswith("## ")


# --- Fenced code blocks --------------------------------------------------


def test_info_string_becomes_code_block_name(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    install = _find(r.declarations, kind=KIND_HEADING, name="Installation")
    code_blocks = [c for c in install.children if c.kind == KIND_CODE_BLOCK]
    # The `bash` block at the top of Installation (before any H3)
    assert any(cb.name == "bash" for cb in code_blocks)


def test_unlabelled_fence_gets_default_name(md_dir):
    """A ``` fence with no info string still becomes a KIND_CODE_BLOCK."""
    r = MarkdownAdapter().parse(md_dir / "setext_and_codes.md")
    all_blocks = _find_all(r.declarations, kind=KIND_CODE_BLOCK)
    # One of them has no info string
    assert any(cb.name == "code" for cb in all_blocks)
    # And the labelled ones are also detected
    names = {cb.name for cb in all_blocks}
    assert {"python", "typescript"}.issubset(names)


def test_code_blocks_attached_to_nearest_heading(md_dir):
    """A code fence inside `### From source` should be a child of that H3,
    not of the enclosing `## Installation`."""
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    install = _find(r.declarations, kind=KIND_HEADING, name="Installation")
    from_src = _find(install.children, name="From source")
    # The bash block inside "From source"
    from_src_blocks = [c for c in from_src.children if c.kind == KIND_CODE_BLOCK]
    assert len(from_src_blocks) == 1
    assert from_src_blocks[0].name == "bash"


def test_code_block_line_range_matches_fence(md_dir):
    r = MarkdownAdapter().parse(md_dir / "setext_and_codes.md")
    py_block = next(cb for cb in _find_all(r.declarations, kind=KIND_CODE_BLOCK) if cb.name == "python")
    src_lines = (md_dir / "setext_and_codes.md").read_text().splitlines()
    # The line at start_line should be the opening ```python fence
    assert src_lines[py_block.start_line - 1].strip().startswith("```python")
    # The line at end_line should be the closing fence
    assert src_lines[py_block.end_line - 1].strip() == "```"


# --- Rendering (outline + digest) ----------------------------------------


def test_outline_prints_hierarchical_structure(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    out = render_outline(r, OutlineOptions())
    # Headings appear with their `#` prefix
    assert "# Sample Project" in out
    assert "## Installation" in out
    assert "### From source" in out
    # Code blocks appear too (default options include fields=True)
    assert "bash code block" in out


def test_digest_renders_markdown_as_toc(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    out = render_digest([r], DigestOptions())
    # TOC lines with indented `#` signatures
    assert "# Sample Project" in out
    assert "## Installation" in out
    # By default code blocks are NOT shown in TOC digest
    assert "bash code block" not in out


def test_digest_respects_max_heading_depth(md_dir):
    r = MarkdownAdapter().parse(md_dir / "article.md")
    # Default max_heading_depth = 3 → H4 is hidden
    default_out = render_digest([r], DigestOptions())
    assert "#### A nested detail" not in default_out
    # Raise cap → H4 shows up
    deep_out = render_digest([r], DigestOptions(max_heading_depth=4))
    assert "#### A nested detail" in deep_out


def test_digest_code_blocks_only_with_include_fields(md_dir):
    r = MarkdownAdapter().parse(md_dir / "readme_style.md")
    with_blocks = render_digest([r], DigestOptions(include_fields=True))
    assert "bash code block" in with_blocks


def test_digest_marks_empty_file(md_dir):
    r = MarkdownAdapter().parse(md_dir / "empty.md")
    out = render_digest([r], DigestOptions())
    assert "# empty" in out


# --- noise_regions for fenced code blocks --------------------------------


def test_noise_regions_populated_for_fenced_blocks(md_dir):
    """Each fenced code block contributes one (start, end, "string") region."""
    r = MarkdownAdapter().parse(md_dir / "grep_noise.md")
    string_regions = [(s, e, k) for s, e, k in r.noise_regions if k == "string"]
    # Two fenced blocks in grep_noise.md — javascript + python.
    assert len(string_regions) == 2


def test_noise_regions_exclude_fence_delimiters_and_info_string(md_dir):
    r"""Region bytes cover the body — never the ``\`\`\`python`` line or
    the closing ``\`\`\```. That keeps the info-string searchable so an
    agent can grep for ``python`` to find code blocks by language."""
    src = (md_dir / "grep_noise.md").read_bytes()
    r = MarkdownAdapter().parse(md_dir / "grep_noise.md")
    for start, end, kind in r.noise_regions:
        if kind != "string":
            continue
        body = src[start:end]
        # No fence delimiter inside the masked region.
        assert b"```" not in body
        # No language token at the very start (info string lives on the
        # opening fence line, before the body).
        assert not body.startswith(b"javascript")
        assert not body.startswith(b"python")


def test_grep_shows_fenced_code_block_matches_tagged_string(md_dir):
    """Matches inside fenced code blocks surface, tagged ``string``.

    For an agent searching docs, the example code is usually exactly
    the answer ("how is useState called") — fences follow the
    strings-visible default. HTML comments stay hidden.
    """
    path = md_dir / "grep_noise.md"
    results, _ignored, _excluded = grep("useState", [path])
    assert results
    fr = results[0]
    by_line = {m.line: m for m in fr.matches}
    src_lines = path.read_text().splitlines()
    # Fence hits are visible and tagged string.
    fence_lines = [
        n for n, line in enumerate(src_lines, 1)
        if "useState(0)" in line or 'useState = "this is python' in line
    ]
    for n in fence_lines:
        assert n in by_line, f"fence line {n} should be visible"
        assert by_line[n].kind == "string"
    # HTML comments remain hidden.
    for n, line in enumerate(src_lines, 1):
        if "TODO: revisit useState" in line or line.lstrip().startswith("<!--"):
            assert n not in by_line
    assert fr.filtered_count > 0


def test_grep_include_noise_surfaces_html_comment_matches(md_dir):
    """``include_noise=True`` re-surfaces the HTML-comment hits — the
    only category still hidden by default."""
    path = md_dir / "grep_noise.md"
    visible_default, _, _ = grep("useState", [path])
    all_with_noise, _, _ = grep("useState", [path], include_noise=True)
    # Exactly the comment hits are the difference.
    delta = (
        sum(len(fr.matches) for fr in all_with_noise)
        - sum(len(fr.matches) for fr in visible_default)
    )
    assert delta == visible_default[0].filtered_count > 0


# --- noise_regions for HTML block comments -------------------------------


def test_noise_regions_html_comment_marked_as_comment(md_dir):
    """``<!-- ... -->`` block-level comments contribute a region with
    kind ``"comment"`` so grep classifies them with [comment]."""
    r = MarkdownAdapter().parse(md_dir / "grep_noise.md")
    comment_regions = [(s, e, k) for s, e, k in r.noise_regions if k == "comment"]
    # Two HTML comments in the fixture: one multi-line, one single-line.
    assert len(comment_regions) == 2
    src = (md_dir / "grep_noise.md").read_bytes()
    for start, end, _ in comment_regions:
        body = src[start:end]
        assert body.startswith(b"<!--")
        # ``-->`` may carry a trailing newline; the marker is what matters.
        assert b"-->" in body


def test_grep_filters_html_comment_matches(md_dir):
    """useState mentions inside ``<!-- ... -->`` vanish under default
    noise filter — the agent doesn't need draft / TODO annotations
    surfacing alongside real prose."""
    path = md_dir / "grep_noise.md"
    results, _, _ = grep("useState", [path])
    fr = results[0]
    src_lines = path.read_text().splitlines()
    for m in fr.matches:
        line = src_lines[m.line - 1]
        assert "TODO: revisit useState" not in line
        assert "useState: shorter inline comment" not in line


def test_grep_html_comment_surfaces_as_comment_kind_with_include_noise(md_dir):
    """When ``--include-noise`` is on, HTML-comment matches surface
    tagged ``[comment]`` (not ``[string]``) — the kind annotation
    helps the agent triage why a match was previously hidden."""
    path = md_dir / "grep_noise.md"
    results, _, _ = grep("useState", [path], include_noise=True)
    fr = results[0]
    # Find the match on the multi-line comment's TODO line.
    matches_with_todo = [m for m in fr.matches if "TODO: revisit useState" in m.line_content]
    assert matches_with_todo, "expected the TODO comment match to surface"
    for m in matches_with_todo:
        assert m.kind == "comment"


def test_grep_div_block_keeps_html_searchable(md_dir):
    """Raw ``<div>`` blocks are NOT noise-filtered — embedded HTML
    carries searchable signal (data attrs, IDs) and the user might
    legitimately grep for it."""
    path = md_dir / "grep_noise.md"
    results, _, _ = grep("useState", [path])
    fr = results[0]
    matches_in_div = [m for m in fr.matches if "<div data-tag=" in m.line_content]
    assert matches_in_div, "div-block useState should remain visible"


# --- end-to-end snapshot of grep behavior on grep_noise.md ---------------


def test_grep_noise_fixture_snapshot_default(md_dir):
    """Pin down the default-mode grep behavior on grep_noise.md.

    The fixture mentions ``useState`` in 6 distinct contexts; each
    line below is asserted to be visible OR filtered with the exact
    rationale, so a regression on any single classification path
    (fenced-block / HTML-comment / `<div>` / line-string heuristic)
    fails this test instead of slipping through.
    """
    path = md_dir / "grep_noise.md"
    results, _ignored, _excluded = grep("useState", [path])
    assert len(results) == 1
    fr = results[0]
    visible = {(m.line, m.column): m for m in fr.matches}

    # Visible: line numbers only — column checks would couple too
    # tightly to fixture wording. The shape we care about is "this
    # LINE was kept", not "this column survived". Strings-visible
    # default: prose, fence content and string-heuristic lines all
    # surface; only the HTML comments stay hidden.
    visible_lines = {line for line, _col in visible}
    expected_visible_lines = {
        3,   # H1-blurb prose [ref]
        7,   # ## Basic usage prose [ref]
        10,  # JS fence import line [string]
        13,  # JS fence useState(0) call [string]
        18,  # prose with apostrophe — line-heuristic [string]
        22,  # prose with apostrophe — line-heuristic [string]
        26,  # Python fence assignment [string]
        27,  # Python fence print [string]
        30,  # closing prose [ref]
        39,  # <div>: data-attr value [string] + prose mention [ref]
    }
    assert visible_lines == expected_visible_lines
    # 11 matches total (two on L39); only the 2 HTML-comment hits
    # (multi-line TODO + single-line inline) are swallowed.
    assert len(fr.matches) == 11
    assert fr.filtered_count == 2


def test_grep_noise_fixture_snapshot_include_noise(md_dir):
    """Pin down the include-noise breakdown — every category surfaces
    with the kind tag designed for it (string vs comment vs ref)."""
    path = md_dir / "grep_noise.md"
    results, _, _ = grep("useState", [path], include_noise=True)
    fr = results[0]
    # 11 visible by default + 2 hidden comment hits = 13 total.
    assert len(fr.matches) == 13

    kinds: dict[str, int] = {}
    for m in fr.matches:
        kinds[m.kind] = kinds.get(m.kind, 0) + 1

    # 2 [comment] = the multi-line TODO line (one useState) + the
    # single-line ``<!-- useState: ... -->`` block-level comment.
    assert kinds.get("comment", 0) == 2

    # 7 [string] = 2 in JS fence (L10 import + L13 useState call) +
    # 2 in Python fence (L26 assign + L27 print) + 2 line-heuristic
    # apostrophe false-positives (L18 "That's", L22 "Don't") + 1
    # data-attr value on L39 (``data-tag="useState"`` is inside
    # double quotes by the per-line scanner). The two apostrophe and
    # data-attr classifications are pre-existing line-heuristic
    # behavior; pinning them here so a future tweak to the heuristic
    # surfaces the impact loud and clear instead of silently shifting
    # noise totals.
    assert kinds.get("string", 0) == 7

    # 4 [ref] = the four prose mentions visible by default (L3, L7,
    # L30, and the second match on L39 — ``keeps useState searchable``).
    assert kinds.get("ref", 0) == 4

    # No other kinds should appear — markdown grep never produces
    # def/import/call for prose patterns.
    assert set(kinds) == {"comment", "string", "ref"}
