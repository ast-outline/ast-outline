"""Tests for the CSS adapter."""
from __future__ import annotations

from ast_outline.adapters.css import CssAdapter
from ast_outline.adapters import get_adapter_for
from ast_outline.core import (
    KIND_AT_RULE,
    KIND_RULE,
    Declaration,
    DigestOptions,
    OutlineOptions,
    find_symbols,
    render_digest,
    render_outline,
)


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


def _has_match_name(decls, value):
    return any(value in d.match_names for d in _find_all(decls, kind=KIND_RULE))


# --- Parse smoke ----------------------------------------------------------


def test_parse_populates_result_metadata(css_dir):
    path = css_dir / "styles.css"
    result = CssAdapter().parse(path)
    assert result.path == path
    assert result.language == "css"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extension_resolution(css_dir):
    adapter = get_adapter_for(css_dir / "styles.css")
    assert isinstance(adapter, CssAdapter)


# --- Rules + selectors ----------------------------------------------------


def test_rules_carry_match_names_for_each_simple_selector(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    # `.btn-primary, .btn-secondary { }` is one rule findable as either
    rule = _find(result.declarations, kind=KIND_RULE, name=".btn-primary")
    assert rule is not None
    assert ".btn-primary" in rule.match_names
    assert ".btn-secondary" in rule.match_names


def test_id_selector_extracted(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    assert _has_match_name(result.declarations, "#main-header")


def test_tag_selector_extracted(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    assert _has_match_name(result.declarations, "body")


def test_pseudo_class_stripped_for_matching(css_dir):
    """`#main-header > .nav .item:hover` should be findable as
    `.item` and `.nav` and `#main-header`, with `:hover` stripped."""
    result = CssAdapter().parse(css_dir / "styles.css")
    rule = _find(
        result.declarations,
        kind=KIND_RULE,
        name="#main-header",
    )
    assert rule is not None
    assert ".nav" in rule.match_names
    assert ".item" in rule.match_names


def test_attribute_selector_strips_filter(css_dir):
    """`.modal .btn-primary[disabled]` should be findable as
    `.modal` and `.btn-primary` (the [disabled] filter is stripped)."""
    result = CssAdapter().parse(css_dir / "styles.css")
    matches = find_symbols(result, ".btn-primary")
    # Four definitions in the fixture:
    # - `.btn-primary, .btn-secondary { ... }` — grouped declaration
    # - `.btn-primary { background: ... }` — separate top-level rule
    # - inside `@media (max-width: 768px)` — responsive override
    # - `.modal .btn-primary[disabled]` — descendant in modal scope
    assert len(matches) == 4
    # The descendant variant exists (from `.modal .btn-primary[disabled]`).
    assert any(".modal" in a for m in matches for a in m.ancestor_signatures) or \
        any(m.start_line >= 50 and m.end_line <= 56 for m in matches)


def test_is_pseudo_recurses_into_arguments(css_dir):
    """`:is(.alert, .warning, .error)` is additive — all three should match."""
    result = CssAdapter().parse(css_dir / "styles.css")
    assert _has_match_name(result.declarations, ".alert")
    assert _has_match_name(result.declarations, ".warning")
    assert _has_match_name(result.declarations, ".error")


def test_not_pseudo_does_not_recurse_into_arguments(css_dir):
    """`:not(.disabled)` should NOT make `.disabled` findable from this
    rule — the `:not(...)` body is excluded from match_names."""
    result = CssAdapter().parse(css_dir / "styles.css")
    matches = find_symbols(result, ".disabled")
    # `.disabled` only appears inside :not(.disabled) in the fixture.
    assert matches == []


def test_root_selector_findable(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    matches = find_symbols(result, ":root")
    assert len(matches) >= 1


# --- At-rules -------------------------------------------------------------


def test_media_queries_become_at_rules_with_inner_rules_as_children(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    media_decls = _find_all(result.declarations, kind=KIND_AT_RULE)
    media = [d for d in media_decls if d.signature.startswith("@media")]
    assert media
    # First @media wraps both .container and .btn-primary overrides.
    assert any(d.children for d in media)


def test_keyframes_signature_includes_name(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    fade = _find_all(result.declarations, kind=KIND_AT_RULE, name="@keyframes fadeIn")
    assert fade
    # @keyframes inner stops are NOT broken out — they're internal detail.
    assert fade[0].children == []


def test_layer_at_rule_wraps_inner_rules(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    layer_base = _find_all(result.declarations, name="@layer base")
    assert layer_base
    assert layer_base[0].kind == KIND_AT_RULE
    assert any(c.kind == KIND_RULE for c in layer_base[0].children)


def test_supports_at_rule_wraps_inner_rules(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    supports = [
        d for d in _find_all(result.declarations, kind=KIND_AT_RULE)
        if d.signature.startswith("@supports")
    ]
    assert supports
    # `.grid-layout` rule should be a child of the @supports
    inner = supports[0].children
    assert any(".grid-layout" in d.match_names for d in inner)


def test_font_face_renders_as_at_rule(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    ff = _find_all(result.declarations, kind=KIND_AT_RULE, name="@font-face")
    assert ff


def test_at_rule_signature_collapses_multiline_whitespace(css_dir):
    """`@media (min-width: 769px) and (max-width: 1024px)` — should
    render as one tidy line in signature even if source has newlines."""
    result = CssAdapter().parse(css_dir / "styles.css")
    at_rules = _find_all(result.declarations, kind=KIND_AT_RULE)
    # No at-rule signature should contain newlines.
    for d in at_rules:
        assert "\n" not in d.signature


# --- Find symbols + cascade view ------------------------------------------


def test_find_symbols_returns_all_definitions_in_cascade_order(css_dir):
    """All `.btn-primary` definitions surface — the cascade view. At
    least one match should sit inside `@media`, with the at-rule
    visible in ancestor_signatures so the agent reads the conditional
    context as breadcrumb."""
    result = CssAdapter().parse(css_dir / "styles.css")
    matches = find_symbols(result, ".btn-primary")
    assert len(matches) >= 3
    media_match = [m for m in matches if m.ancestor_signatures]
    assert media_match
    assert any(
        "@media" in a for m in media_match for a in m.ancestor_signatures
    )
    # Source order is preserved — line numbers monotonically increase.
    line_nums = [m.start_line for m in matches]
    assert line_nums == sorted(line_nums)


def test_find_symbols_qualified_name_is_absolute_for_rules(css_dir):
    """CSS rules carry absolute qualified_names — not parent.child paths
    (that would render `.modal..btn-primary` ugly). Parent context lives
    in ancestor_signatures."""
    result = CssAdapter().parse(css_dir / "styles.css")
    matches = find_symbols(result, ".btn-primary")
    for m in matches:
        assert m.qualified_name == ".btn-primary"


# --- Imports --------------------------------------------------------------


def test_imports_collected_as_source_true_text(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    assert any("reset-extended.css" in s for s in result.imports)
    assert any("normalize.css" in s for s in result.imports)


# --- Renderers ------------------------------------------------------------


def test_outline_renders_rules_and_at_rules(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    text = render_outline(result, OutlineOptions())
    assert ".btn-primary" in text
    assert "@media" in text
    assert "@keyframes fadeIn" in text


def test_outline_imports_line_when_show_imports(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    text = render_outline(result, OutlineOptions(show_imports=True))
    assert "imports:" in text


def test_digest_uses_flat_token_layout(css_dir):
    result = CssAdapter().parse(css_dir / "styles.css")
    text = render_digest([result], DigestOptions())
    # Rules render as `.name [rule]` tokens — no class-style headers.
    assert "[rule]" in text or ".btn-primary" in text


# --- Doc blocks -----------------------------------------------------------


def test_leading_comment_attached_as_doc(css_dir):
    """A `/* ... */` block immediately preceding a rule becomes its
    doc and is included in the source slice when `show` is called."""
    result = CssAdapter().parse(css_dir / "styles.css")
    rule = _find(result.declarations, kind=KIND_RULE, name=":root")
    assert rule is not None
    # The fixture has `/* Design tokens — single source of truth ... */`
    # immediately above `:root`.
    assert any("Design tokens" in line for line in rule.docs)
    # doc_start_byte should point to the comment, before the rule itself.
    assert rule.doc_start_byte < rule.start_byte


def test_compound_selectors_expose_every_part(tmp_path):
    """`#header.nav` / `.a.b` / `div.container` nest the qualifying
    selector INSIDE the outer class_selector — the old extractor kept
    only the final class and dropped `#header` / `.a` / `div` from
    `match_names`, making the rule unfindable by its other parts."""
    from ast_outline.core import find_symbols
    p = tmp_path / "a.css"
    p.write_text(
        ".a.b {}\n#header.nav {}\ndiv.container {}\n", encoding="utf-8"
    )
    r = CssAdapter().parse(p)
    for query in (".a", ".b", ".a.b", "#header", ".nav", "#header.nav",
                  "div", ".container", "div.container"):
        assert find_symbols(r, query), f"{query} must resolve"
