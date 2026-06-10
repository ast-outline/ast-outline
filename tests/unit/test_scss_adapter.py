"""Tests for the SCSS adapter."""
from __future__ import annotations

from ast_outline.adapters.scss import ScssAdapter
from ast_outline.adapters import get_adapter_for
from ast_outline.core import (
    KIND_AT_RULE,
    KIND_FUNCTION,
    KIND_MIXIN,
    KIND_PLACEHOLDER,
    KIND_RULE,
    KIND_VARIABLE,
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


# --- Parse smoke ----------------------------------------------------------


def test_parse_populates_result_metadata(scss_dir):
    path = scss_dir / "_components.scss"
    result = ScssAdapter().parse(path)
    assert result.path == path
    assert result.language == "scss"
    assert result.line_count > 0
    assert result.declarations


def test_extension_resolution(scss_dir):
    adapter = get_adapter_for(scss_dir / "_components.scss")
    assert isinstance(adapter, ScssAdapter)


# --- SCSS-specific symbols ------------------------------------------------


def test_top_level_variables_extracted(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    primary = _find(result.declarations, kind=KIND_VARIABLE, name="$primary")
    assert primary is not None
    assert "#007bff" in primary.signature
    assert "!default" in primary.signature


def test_underscore_variable_marked_private(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    private = _find(
        result.declarations, kind=KIND_VARIABLE, name="$_default-padding"
    )
    assert private is not None
    assert private.visibility == "private"


def test_dash_prefixed_variable_marked_private(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    dashed = _find(
        result.declarations, kind=KIND_VARIABLE, name="$-also-private"
    )
    assert dashed is not None
    assert dashed.visibility == "private"


def test_placeholder_extracted(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    placeholder = _find(
        result.declarations, kind=KIND_PLACEHOLDER, name="%button-base"
    )
    assert placeholder is not None
    assert "%button-base" in placeholder.match_names


def test_private_placeholder_marked_private(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    p = _find(
        result.declarations,
        kind=KIND_PLACEHOLDER,
        name="%_internal-reset",
    )
    assert p is not None
    assert p.visibility == "private"


def test_mixin_extracted_with_parameter_list_in_signature(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    button = _find(result.declarations, kind=KIND_MIXIN, name="button")
    assert button is not None
    assert "@mixin button" in button.signature
    assert "$bg" in button.signature
    assert "$primary" in button.signature  # default value


def test_mixin_without_parameters_renders_without_parens(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    helper = _find(
        result.declarations, kind=KIND_MIXIN, name="_internal-helper"
    )
    assert helper is not None
    assert helper.signature == "@mixin _internal-helper"
    assert helper.visibility == "private"


def test_function_extracted_with_kind_function(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    f = _find(result.declarations, kind=KIND_FUNCTION, name="strip-unit")
    assert f is not None
    assert "@function strip-unit" in f.signature


# --- Nested rules with `&` resolution -------------------------------------


def test_nested_amp_class_resolved(scss_dir):
    """`&__header` under `.card` should produce a rule findable as
    `.card__header`."""
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    matches = find_symbols(result, ".card__header")
    assert matches  # at least one definition
    # It's a child of the .card rule, surfaced in ancestor_signatures.
    assert any(".card" in a for m in matches for a in m.ancestor_signatures)


def test_doubly_nested_amp_resolved(scss_dir):
    """`&--featured` under `&__header` (which is under `.card`) — should
    NOT exist in fixture (different shape), but the deep-nested
    `.card__header` under `.card--featured` should resolve."""
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    matches = find_symbols(result, ".card--featured")
    assert matches


def test_amp_with_pseudo_resolves_to_parent(scss_dir):
    """`a, .link { &:hover { ... } }` — the &:hover rule is findable as
    both `a` and `.link` (parent's match_names). Pseudo `:hover` is
    stripped for matching but visible in signature."""
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    a_matches = find_symbols(result, "a")
    # Two matches: the parent `a, .link` rule itself, plus the &:hover and
    # &:visited inside.
    assert len(a_matches) >= 1


def test_multi_selector_parent_propagates_to_nested(scss_dir):
    """`a, .link { &:hover { ... } }` — `&:hover` should be findable as
    BOTH `a` and `.link` (after & resolves against each parent)."""
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    # Look at the `a, .link` rule's children for &:hover and &:visited.
    parent = _find(result.declarations, kind=KIND_RULE, name="a")
    assert parent is not None
    hover_children = [
        c for c in parent.children
        if any(":hover" in s for s in [c.signature])
    ]
    assert hover_children
    # match_names should include both `a` and `.link` resolved variants.
    assert "a" in hover_children[0].match_names
    assert ".link" in hover_children[0].match_names


# --- Imports --------------------------------------------------------------


def test_use_forward_and_legacy_import_collected(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    joined = "; ".join(result.imports)
    assert "@use" in joined
    assert "@forward" in joined
    assert "@import" in joined


# --- At-rules + nested resolution ----------------------------------------


def test_media_query_wraps_nested_rule(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    media = _find_all(result.declarations, kind=KIND_AT_RULE)
    media = [d for d in media if "@media" in d.signature]
    assert media
    inner_card = _find(media[0].children, kind=KIND_RULE, name=".card")
    assert inner_card is not None


# --- Privacy filtering ---------------------------------------------------


def test_outline_hides_private_when_include_private_false(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    text = render_outline(result, OutlineOptions(include_private=False))
    assert "$_default-padding" not in text
    assert "_internal-helper" not in text
    assert "%_internal-reset" not in text


def test_digest_hides_private_when_include_private_false(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    text = render_digest(
        [result], DigestOptions(include_private=False, include_fields=True)
    )
    assert "_internal-helper" not in text


def test_outline_shows_private_when_include_private_true(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    text = render_outline(result, OutlineOptions(include_private=True))
    assert "_internal-helper" in text


# --- Renderers ------------------------------------------------------------


def test_digest_callable_form_for_mixins_and_functions(scss_dir):
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    text = render_digest(
        [result],
        DigestOptions(include_private=True, include_fields=True),
    )
    assert "button()" in text
    assert "strip-unit()" in text


def test_outline_collapses_multiline_selectors(scss_dir):
    """`&__header,\\n        &__body` should render as one line."""
    result = ScssAdapter().parse(scss_dir / "_components.scss")
    text = render_outline(result, OutlineOptions())
    # No outline line should contain raw newline-followed-indent
    # inside a signature (i.e., a literal `,\n        `).
    assert ",\n        " not in text


def test_digest_does_not_leak_children_of_filtered_private_node():
    """A private SCSS placeholder's nested rules must not surface in
    digest when `include_private=False` — the whole subtree is hidden,
    not just the parent. Regression guard for a former leak where
    `_flatten_css` always recursed regardless of whether the parent
    was filtered."""
    import tempfile
    from pathlib import Path
    from ast_outline.adapters.scss import ScssAdapter
    from ast_outline.core import DigestOptions, render_digest

    src = (
        "%_internal-base {\n"
        "    padding: 1rem;\n"
        "    .leak-me { color: red; }\n"
        "}\n"
    )
    with tempfile.NamedTemporaryFile(
        suffix=".scss", delete=False, mode="w"
    ) as f:
        f.write(src)
        path = Path(f.name)

    result = ScssAdapter().parse(path)
    text = render_digest(
        [result], DigestOptions(include_private=False, include_fields=True)
    )
    # The private parent must be hidden.
    assert "%_internal-base" not in text
    # The nested child of that private parent must also be hidden.
    assert ".leak-me" not in text


def test_nested_compound_class_keeps_dot_on_resolution(tmp_path):
    """`&.active` and `&__header` share one grammar shape
    (nesting_selector + class_name); only the literal `.` in the source
    tells them apart. The old extractor dropped the dot, resolving
    `.card { &.active {} }` to `.cardactive` — unfindable by the real
    compound `.card.active`. BEM `&__header` → `.card__header` must
    keep working."""
    from ast_outline.core import find_symbols
    p = tmp_path / "a.scss"
    p.write_text(
        ".card { &.active { font-weight: bold; } &__header { color: red; } }\n",
        encoding="utf-8",
    )
    r = ScssAdapter().parse(p)
    assert find_symbols(r, ".card.active"), "compound `&.active` lost"
    assert find_symbols(r, ".card__header"), "BEM `&__header` regressed"
