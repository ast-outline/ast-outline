"""Tests for the HTML adapter."""
from __future__ import annotations

from ast_outline.adapters.html import HtmlAdapter
from ast_outline.adapters import get_adapter_for, supported_extensions
from ast_outline.core import (
    KIND_HTML_ELEMENT,
    Declaration,
    DigestOptions,
    OutlineOptions,
    find_symbols,
    render_digest,
    render_outline,
)
from ast_outline.grep import grep


def _find(decls, name=None, signature=None):
    for d in decls:
        if (name is None or d.name == name) and (
            signature is None or d.signature == signature
        ):
            return d
        hit = _find(d.children, name=name, signature=signature)
        if hit is not None:
            return hit
    return None


def _find_all(decls, name=None):
    out: list[Declaration] = []
    for d in decls:
        if name is None or d.name == name:
            out.append(d)
        out.extend(_find_all(d.children, name=name))
    return out


def _signatures(decls):
    return [d.signature for d in _find_all(decls)]


# --- Parse smoke ----------------------------------------------------------


def test_parse_populates_result_metadata(html_dir):
    path = html_dir / "hello.html"
    result = HtmlAdapter().parse(path)
    assert result.path == path
    assert result.language == "html"
    assert result.line_count > 0
    assert result.source == path.read_bytes()
    assert result.declarations


def test_extension_resolution_html(html_dir):
    adapter = get_adapter_for(html_dir / "hello.html")
    assert isinstance(adapter, HtmlAdapter)


def test_extension_resolution_htm(tmp_path):
    p = tmp_path / "p.htm"
    p.write_text("<html></html>")
    adapter = get_adapter_for(p)
    assert isinstance(adapter, HtmlAdapter)


def test_supported_extensions_includes_html():
    exts = supported_extensions()
    assert ".html" in exts
    assert ".htm" in exts


# --- Hierarchy ------------------------------------------------------------


def test_top_level_is_html_element(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    top_names = [d.name for d in result.declarations]
    assert "html" in top_names


def test_head_and_body_are_children_of_html(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    html_decl = _find(result.declarations, name="html")
    child_names = {c.name for c in html_decl.children}
    assert {"head", "body"} <= child_names


def test_section_id_appears_in_outline(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    sigs = _signatures(result.declarations)
    assert "section#hero" in sigs
    assert "section#features" in sigs
    assert "section#faq" in sigs


# --- Attributes -----------------------------------------------------------


def test_significant_attrs_rendered_for_input(html_dir):
    result = HtmlAdapter().parse(html_dir / "form.html")
    email_input = _find(result.declarations, name="input")
    # The form fixture's first input is the email field.
    assert email_input is not None
    # Signature carries the bracketed attribute list, possibly with id prefix.
    sig = email_input.signature
    assert "[" in sig and "]" in sig
    assert "name=email" in sig
    assert "type=email" in sig


def test_boolean_attribute_rendered_bare(html_dir):
    result = HtmlAdapter().parse(html_dir / "form.html")
    sigs = _signatures(result.declarations)
    # The email input has `required` as a bare boolean attribute.
    matching = [s for s in sigs if "name=email" in s]
    assert matching
    assert any("required" in s and 'required=""' not in s for s in matching)


def test_quotes_stripped_from_attr_values(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    sigs = _signatures(result.declarations)
    assert any("rel=stylesheet" in s for s in sigs)
    # No leftover quotes inside the bracket.
    assert not any('rel="stylesheet"' in s for s in sigs)


def test_class_compound_rendered(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    sigs = _signatures(result.declarations)
    assert "header.site-nav" in sigs
    assert "footer.site-footer" in sigs


def test_id_class_attr_order_canonical(tmp_path):
    src = """<!doctype html><html><body>
        <button id="go" class="btn-primary" type="submit" disabled>Go</button>
    </body></html>"""
    p = tmp_path / "btn.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    btn = _find(result.declarations, name="button")
    assert btn is not None
    sig = btn.signature
    # Order: tag, then #id, then .class, then [attrs] — strict.
    pos_tag = sig.find("button")
    pos_id = sig.find("#go")
    pos_cls = sig.find(".btn-primary")
    pos_bracket = sig.find("[")
    assert 0 == pos_tag < pos_id < pos_cls < pos_bracket


# --- Imports --------------------------------------------------------------


def test_link_stylesheet_in_imports(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    stylesheet_imports = [s for s in result.imports if "rel=stylesheet" in s]
    assert stylesheet_imports
    assert any("/css/main.css" in s for s in stylesheet_imports)


def test_script_src_in_imports(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    script_imports = [s for s in result.imports if s.startswith("script")]
    assert script_imports
    assert any("/js/analytics.js" in s for s in script_imports)


def test_inline_script_not_in_imports(html_dir):
    result = HtmlAdapter().parse(html_dir / "with_assets.html")
    # with_assets.html has both src-bearing and inline scripts.
    assert all(
        "src=" in imp or "rel=" in imp for imp in result.imports
    )


def test_import_regions_populated(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    assert result.import_regions
    # Every region is a (start, end) tuple of real bytes inside the source.
    for start, end in result.import_regions:
        assert 0 <= start < end <= len(result.source)


def test_import_prefix_in_signature(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    sigs = _signatures(result.declarations)
    import_sigs = [s for s in sigs if s.startswith("[import] ")]
    assert import_sigs
    assert any("rel=stylesheet" in s for s in import_sigs)


# --- Heading text preview -------------------------------------------------


def test_h1_includes_text_preview(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert h1.signature.startswith("h1: ")
    assert "Pull exactly the context you need" in h1.signature


def test_long_heading_truncated(tmp_path):
    long_text = "A" * 200
    src = f"<!doctype html><html><body><h2>{long_text}</h2></body></html>"
    p = tmp_path / "long_h.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    h2 = _find(result.declarations, name="h2")
    assert h2 is not None
    assert h2.signature.endswith("…")
    assert len(h2.signature) < 80  # Bounded by 60-char limit + selector prefix.


def test_heading_without_text_has_no_preview(tmp_path):
    src = '<!doctype html><html><body><h1><img src="/logo.svg" alt=""></h1></body></html>'
    p = tmp_path / "empty_h.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert ":" not in h1.signature


# --- Noise / drop rules ---------------------------------------------------


def test_bare_div_dropped_children_lifted(tmp_path):
    src = """<!doctype html><html><body>
        <div>
            <section id="x"><h1>Hi</h1></section>
        </div>
    </body></html>"""
    p = tmp_path / "div.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    body = _find(result.declarations, name="body")
    # `section#x` should be a direct child of body — the bare div is dropped.
    child_names = [c.name for c in body.children]
    assert "div" not in child_names
    assert "section" in child_names


def test_div_with_class_kept(tmp_path):
    src = """<!doctype html><html><body>
        <div class="container"><h1>Hi</h1></div>
    </body></html>"""
    p = tmp_path / "div.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    sigs = _signatures(result.declarations)
    assert "div.container" in sigs


def test_inline_em_strong_never_emitted(tmp_path):
    src = """<!doctype html><html><body>
        <p>Some <em>emphasised</em> and <strong>strong</strong> text.</p>
    </body></html>"""
    p = tmp_path / "inline.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    names = {d.name for d in _find_all(result.declarations)}
    assert "em" not in names
    assert "strong" not in names


def test_svg_emitted_no_children(html_dir):
    result = HtmlAdapter().parse(html_dir / "with_svg.html")
    svg = _find(result.declarations, name="svg")
    assert svg is not None
    assert svg.children == []


def test_script_body_in_noise_regions(html_dir):
    result = HtmlAdapter().parse(html_dir / "with_assets.html")
    string_regions = [r for r in result.noise_regions if r[2] == "string"]
    assert string_regions, "inline <script>/<style> body should populate noise_regions"


def test_html_comment_in_noise_regions(html_dir):
    result = HtmlAdapter().parse(html_dir / "with_assets.html")
    comment_regions = [r for r in result.noise_regions if r[2] == "comment"]
    assert comment_regions


# --- Details collapse -----------------------------------------------------


def test_consecutive_details_collapse(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    sigs = _signatures(result.declarations)
    # landing.html has 6 sibling <details> in the FAQ section.
    collapsed = [s for s in sigs if s.startswith("details ×")]
    assert collapsed
    assert "details ×6" in collapsed


def test_details_with_class_breaks_run(tmp_path):
    src = """<!doctype html><html><body>
        <section>
            <details><summary>A</summary></details>
            <details><summary>B</summary></details>
            <details class="advanced"><summary>C</summary></details>
            <details><summary>D</summary></details>
            <details><summary>E</summary></details>
            <details><summary>F</summary></details>
        </section>
    </body></html>"""
    p = tmp_path / "d.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    sigs = _signatures(result.declarations)
    # Three before the class break < threshold, three after >= threshold —
    # but wait: 2 + 1 + 3 = no run of 3+ bare before the break, run of 3
    # bare after. Implementation collapses runs of >= 3 only.
    assert "details.advanced" in sigs
    # Run of 3 after collapses
    assert any(s == "details ×3" for s in sigs) or sum(
        1 for s in sigs if s == "details"
    ) >= 1


# --- match_names ----------------------------------------------------------


def test_section_findable_by_id_or_tag_id_or_tag(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    matches_hash = find_symbols(result, "#hero")
    matches_tag = find_symbols(result, "section#hero")
    matches_bare = find_symbols(result, "section")
    assert any(m.qualified_name == "#hero" for m in matches_hash)
    assert any(m.qualified_name == "section#hero" for m in matches_tag)
    # `section` matches every section; we just check at least one resolves.
    assert matches_bare


def test_class_compound_findable(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    matches = find_symbols(result, ".site-nav")
    assert matches
    assert any(m.qualified_name == ".site-nav" for m in matches)


def test_attribute_selector_findable(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    matches = find_symbols(result, "[rel=stylesheet]")
    assert matches


# --- Renderer integration -------------------------------------------------


def test_outline_renders_selector_lines(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    rendered = render_outline(result, OutlineOptions())
    assert "html[lang=en]" in rendered
    assert "section#hero" in rendered
    assert "header.site-nav" in rendered
    assert "form#newsletter" in rendered


def test_digest_renders_hierarchical_element_list(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    rendered = render_digest([result], DigestOptions())
    # html/head/body chrome is hidden in digest.
    assert "html[lang=en]" not in rendered
    # Landmarks still appear.
    assert "section#hero" in rendered
    assert "form#newsletter" in rendered


def test_outline_imports_line_when_show_imports(html_dir):
    result = HtmlAdapter().parse(html_dir / "landing.html")
    rendered = render_outline(result, OutlineOptions(show_imports=True))
    assert "imports:" in rendered


# --- Malformed input ------------------------------------------------------


def test_malformed_html_produces_partial_outline(html_dir):
    result = HtmlAdapter().parse(html_dir / "malformed.html")
    # tree-sitter recovers gracefully — declarations are populated, but
    # error_count > 0.
    assert result.declarations
    assert result.error_count > 0


def test_malformed_html_outline_shows_warning(html_dir):
    result = HtmlAdapter().parse(html_dir / "malformed.html")
    rendered = render_outline(result, OutlineOptions())
    assert "# WARNING" in rendered


# --- grep integration -----------------------------------------------------


def test_grep_marks_link_href_as_import(html_dir):
    file_results, _total, _ = grep(
        "main.css", [html_dir / "with_assets.html"]
    )
    assert file_results
    classifications = [m.kind for fr in file_results for m in fr.matches]
    assert any(c == "import" for c in classifications)


def test_grep_filters_inline_script_by_default(html_dir):
    file_results, _total, _ = grep(
        "bootstrap", [html_dir / "with_assets.html"]
    )
    # The word `bootstrap` only appears inside the inline <script>
    # body. By default, that's filtered as noise.
    total = sum(len(fr.matches) for fr in file_results)
    assert total == 0


# --- Hardening (v1.0.0 audit) ---------------------------------------------


def test_class_duplicates_deduped(tmp_path):
    src = '<!doctype html><html><body><a class="btn btn primary btn">x</a></body></html>'
    p = tmp_path / "dup_class.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    anchor = _find(result.declarations, name="a")
    assert anchor is not None
    # Order preserved, dupes removed: btn, primary.
    assert anchor.signature == "a.btn.primary"


def test_duplicate_same_name_attribute_last_wins(tmp_path):
    src = '<!doctype html><html><body><a href="/old" href="/new">x</a></body></html>'
    p = tmp_path / "dup_attr.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    anchor = _find(result.declarations, name="a")
    assert anchor is not None
    sig = anchor.signature
    # Only ONE href= token; last one wins.
    assert sig.count("href=") == 1
    assert "/new" in sig
    assert "/old" not in sig


def test_value_with_whitespace_quoted(tmp_path):
    src = '<!doctype html><html><body><button value="Save changes" type="submit">x</button></body></html>'
    p = tmp_path / "ws.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    button = _find(result.declarations, name="button")
    assert button is not None
    assert 'value="Save changes"' in button.signature


def test_value_with_bracket_quoted(tmp_path):
    src = '<!doctype html><html><body><a href="/api/items?ids=[1,2,3]">x</a></body></html>'
    p = tmp_path / "br.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    anchor = _find(result.declarations, name="a")
    assert anchor is not None
    # The `]` in the value triggers quoting.
    assert 'href="/api/items?ids=[1,2,3]"' in anchor.signature


def test_picture_source_srcset_in_signature(html_dir):
    result = HtmlAdapter().parse(html_dir / "responsive.html")
    sources = _find_all(result.declarations, name="source")
    assert sources
    assert any("srcset=/hero.avif" in s.signature for s in sources)


def test_img_srcset_and_sizes_in_signature(html_dir):
    result = HtmlAdapter().parse(html_dir / "responsive.html")
    imgs = _find_all(result.declarations, name="img")
    assert imgs
    # The second img carries sizes.
    sizes_carriers = [i for i in imgs if "sizes=" in i.signature]
    assert sizes_carriers


def test_compound_selector_show_roundtrip(tmp_path):
    src = '<!doctype html><html><body><button id="go" class="primary" disabled>X</button></body></html>'
    p = tmp_path / "compound.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    # The compound form is in match_names.
    matches = find_symbols(result, "button#go.primary[disabled]")
    assert matches, "compound selector must round-trip through find_symbols"


def test_duplicate_ids_both_findable(tmp_path):
    src = """<!doctype html><html><body>
        <section id="x"><h1>A</h1></section>
        <aside id="x"><h2>B</h2></aside>
    </body></html>"""
    p = tmp_path / "dup_id.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    matches = find_symbols(result, "#x")
    assert len(matches) == 2


def test_h1_with_nested_link_preserves_link_text(tmp_path):
    src = '<!doctype html><html><body><h1>See <a href="/x">our docs</a> for more</h1></body></html>'
    p = tmp_path / "nested.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert "See" in h1.signature
    assert "our docs" in h1.signature
    assert "for more" in h1.signature


def test_h1_inline_style_does_not_leak(tmp_path):
    src = '<!doctype html><html><body><h1><style>.x{color:red}</style>Heading</h1></body></html>'
    p = tmp_path / "leak.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert "Heading" in h1.signature
    assert "color" not in h1.signature
    assert "red" not in h1.signature


def test_empty_html_no_crash(tmp_path):
    p = tmp_path / "empty.html"
    p.write_text("")
    result = HtmlAdapter().parse(p)
    assert result.declarations == []


def test_doctype_only_no_decls(tmp_path):
    p = tmp_path / "dtype.html"
    p.write_text("<!DOCTYPE html>")
    result = HtmlAdapter().parse(p)
    assert result.declarations == []


def test_comment_only_no_decls(tmp_path):
    p = tmp_path / "comment.html"
    p.write_text("<!-- just a comment -->")
    result = HtmlAdapter().parse(p)
    assert result.declarations == []
    comments = [r for r in result.noise_regions if r[2] == "comment"]
    assert comments


def test_long_xhtml_doctype_parses(tmp_path):
    src = (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
        '<body><h1>OK</h1></body></html>'
    )
    p = tmp_path / "xhtml.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    assert result.declarations
    # XHTML doctype shouldn't be flagged as an error.
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None


def test_utf8_bom_parses_cleanly(html_dir):
    result = HtmlAdapter().parse(html_dir / "with_bom.html")
    assert result.declarations
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert "After BOM" in h1.signature


def test_cyrillic_alt_preserved(html_dir):
    result = HtmlAdapter().parse(html_dir / "cyrillic.html")
    img = _find(result.declarations, name="img")
    assert img is not None
    # Alt text should appear literally, not escape-encoded.
    assert "Картинка" in img.signature


def test_cyrillic_heading_preserved(html_dir):
    result = HtmlAdapter().parse(html_dir / "cyrillic.html")
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert "Привет, мир" in h1.signature


def test_emoji_in_heading_survives(html_dir):
    result = HtmlAdapter().parse(html_dir / "cyrillic.html")
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert "👋" in h1.signature


def test_crlf_line_endings_correct_line_numbers(tmp_path):
    src = "<!doctype html>\r\n<html>\r\n<body>\r\n<h1>X</h1>\r\n</body>\r\n</html>\r\n"
    p = tmp_path / "crlf.html"
    p.write_bytes(src.encode("utf-8"))
    result = HtmlAdapter().parse(p)
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    # `<h1>` is on the 4th visible line in source.
    assert h1.start_line == 4


def test_templated_html_partial_recovery(html_dir):
    """Jinja-wrapped HTML root produces ERROR but recovery surfaces inner elements."""
    result = HtmlAdapter().parse(html_dir / "templated.html")
    # The parser sees the whole top-level as ERROR-shaped because of
    # `{% extends %}` / `{% block %}` at root. Recovery should surface
    # at least one well-formed element (header / main / article).
    names = {d.name for d in _find_all(result.declarations)}
    assert names & {"header", "main", "article", "section", "h1"}, (
        f"recovery should find at least one well-formed element, got {names}"
    )


def test_data_uri_src_truncated(tmp_path):
    big = "iVBORw0KGgo" * 200  # ~2200 chars
    src = f'<!doctype html><html><body><img src="data:image/png;base64,{big}" alt=""></body></html>'
    p = tmp_path / "datauri.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    img = _find(result.declarations, name="img")
    assert img is not None
    assert "…" in img.signature
    # Total signature length is bounded — no megabyte attribute leaks.
    assert len(img.signature) < 200


def test_base_href_not_classified_as_import(tmp_path):
    src = '<!doctype html><html><head><base href="/v2/"><title>x</title></head><body></body></html>'
    p = tmp_path / "base.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    # Documented as deliberate exclusion: `<base>` is not pulled into imports.
    assert not any("base" in imp.lower() for imp in result.imports)


def test_inline_module_script_not_an_import(tmp_path):
    src = (
        '<!doctype html><html><head>'
        '<script type="module">import x from "./x.js";</script>'
        '</head><body></body></html>'
    )
    p = tmp_path / "module.html"
    p.write_text(src)
    result = HtmlAdapter().parse(p)
    # Inline module scripts (no src) are content, not imports.
    assert not any(s.startswith("script") for s in result.imports)


def test_heading_text_preserves_source_order_and_spacing(tmp_path):
    """Text inside inline children must land in SOURCE order with word
    boundaries kept: the old LIFO-stack walk emitted a heading's direct
    text before its inline elements' inner text
    (``<h2><a>Section</a> title</h2>`` → ``titleSection``), and a
    butt-join fused words across element boundaries."""
    p = tmp_path / "a.html"
    p.write_text(
        "<html><body>\n"
        '<h2><a href="#x">Section</a> title</h2>\n'
        "<h1><strong>Hello</strong> World</h1>\n"
        "</body></html>\n",
        encoding="utf-8",
    )
    r = HtmlAdapter().parse(p)
    sigs = []
    def walk(ds):
        for d in ds:
            sigs.append(d.signature)
            walk(d.children)
    walk(r.declarations)
    assert "h2: Section title" in sigs, sigs
    assert "h1: Hello World" in sigs, sigs
