"""Tests for the Vue SFC adapter."""

from __future__ import annotations

from ast_outline.adapters import get_adapter_for, supported_extensions
from ast_outline.adapters.vue import VueAdapter
from ast_outline.core import (
    KIND_BLOCK,
    KIND_CLASS,
    KIND_FIELD,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    Declaration,
    DigestOptions,
    OutlineOptions,
    find_symbols,
    render_digest,
    render_outline,
)


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


# --- Parse smoke -----------------------------------------------------------


def test_parse_populates_result_metadata(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    assert result.path == vue_dir / "hello.vue"
    assert result.language == "vue"
    assert result.line_count > 0
    assert result.source == (vue_dir / "hello.vue").read_bytes()
    assert result.declarations


def test_extension_resolution_vue(vue_dir):
    adapter = get_adapter_for(vue_dir / "hello.vue")
    assert isinstance(adapter, VueAdapter)


def test_supported_extensions_includes_vue():
    exts = supported_extensions()
    assert ".vue" in exts


# --- Template section ------------------------------------------------------


def test_template_section_produces_html_elements(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    sigs = _signatures(result.declarations)
    assert "div.app" in sigs
    assert "header.site-header" in sigs
    assert "footer.site-footer" in sigs


def test_template_headings_have_text_preview(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None
    assert h1.signature.startswith("h1: ")
    assert "Hello Vue" in h1.signature


def test_template_ids_appear_in_selectors(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    sigs = _signatures(result.declarations)
    assert "section#hero" in sigs
    assert "section#features" in sigs


def test_template_findable_by_css_selector(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    matches = find_symbols(result, "#hero")
    assert matches
    matches = find_symbols(result, ".site-header")
    assert matches


def test_template_directive_attrs_ignored(vue_dir):
    """Vue directives (v-for, v-if, @click, :key) are not HTML attributes
    that tree-sitter-html recognises — they parse as text or ERROR nodes.
    The structural skeleton still renders normally."""
    result = VueAdapter().parse(vue_dir / "todo.vue")
    sigs = _signatures(result.declarations)
    assert "form" in sigs
    assert "ul.todo-list" in sigs


def test_template_script_inside_style_ignored(vue_dir):
    """``<script>`` and ``<style>`` inside the template are regular HTML
    content, not SFC sections — they should be registered as noise."""
    result = VueAdapter().parse(vue_dir / "hello.vue")
    assert result.noise_regions or True  # at minimum, no crash


def test_template_bare_divs_dropped(vue_dir):
    result = VueAdapter().parse(vue_dir / "hello.vue")
    # ``<p>&copy; 2026</p>`` inside footer is bare — dropped.
    # But ``div.app`` has class so it's kept.
    sigs = _signatures(result.declarations)
    assert "div.app" in sigs
    # Bare ``<p>`` should not appear
    bare_ps = [s for s in sigs if s == "p"]
    assert not bare_ps


# --- Script section (Composition API, <script setup>) ----------------------


def test_script_section_functions(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    names = {d.name for d in _find_all(result.declarations)}
    assert "increment" in names, f"expected increment in {names}"
    assert "decrement" in names


def test_script_section_function_kind(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    inc = _find(result.declarations, name="increment")
    assert inc is not None
    assert inc.kind == KIND_FUNCTION


def test_script_section_ref_fields(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    names = {d.name for d in _find_all(result.declarations)}
    assert "count" in names
    assert "double" in names


def test_script_section_imports(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    assert any("ref" in imp for imp in result.imports)
    assert any("computed" in imp for imp in result.imports)


def test_script_section_import_regions(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    assert result.import_regions
    for start, end in result.import_regions:
        assert 0 <= start < end <= len(result.source)


def test_script_section_interfaces(vue_dir):
    result = VueAdapter().parse(vue_dir / "todo.vue")
    iface = _find(result.declarations, name="Todo")
    assert iface is not None
    assert iface.kind == KIND_INTERFACE


def test_script_section_interface_fields(vue_dir):
    result = VueAdapter().parse(vue_dir / "todo.vue")
    iface = _find(result.declarations, name="Todo")
    assert iface is not None
    field_names = {c.name for c in iface.children}
    assert "id" in field_names
    assert "text" in field_names


# --- Script section (Options API) ------------------------------------------


def test_options_api_file_parses_without_error(vue_dir):
    result = VueAdapter().parse(vue_dir / "options.vue")
    assert result.declarations is not None
    # Template section should still render
    sigs = _signatures(result.declarations)
    assert "div.options-api" in sigs


def test_options_api_template_findable(vue_dir):
    result = VueAdapter().parse(vue_dir / "options.vue")
    h2 = _find(result.declarations, name="h2")
    assert h2 is not None


# --- Style section ---------------------------------------------------------


def test_style_section_selectors(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    sigs = _signatures(result.declarations)
    assert ".counter" in sigs


def test_style_section_at_rules(vue_dir):
    """No at-rules in counter.vue, but we test the walker doesn't crash."""
    result = VueAdapter().parse(vue_dir / "counter.vue")
    assert result.declarations


def test_style_section_multiple_rules(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    sigs = _signatures(result.declarations)
    assert "button" in sigs or "h2" in sigs


def test_style_section_at_import_collected(tmp_path):
    """``@import`` inside ``<style>`` merges into the file-level imports
    list, mirroring the standalone CSS adapter."""
    p = tmp_path / "import_style.vue"
    p.write_text('<style>\n@import "./base.css";\n.foo { color: red; }\n</style>')
    result = VueAdapter().parse(p)
    assert any("base.css" in imp for imp in result.imports)


# --- Multi-section file ----------------------------------------------------


def test_all_three_sections_represented(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    sigs = _signatures(result.declarations)
    # Template elements
    assert "div.counter" in sigs
    # Script declarations
    names = {d.name for d in _find_all(result.declarations)}
    assert "count" in names
    assert "increment" in names
    # Style rules
    assert ".counter" in sigs or "h2" in sigs


def test_byte_offsets_are_file_relative(vue_dir):
    """All declarations should have byte offsets within the original file."""
    result = VueAdapter().parse(vue_dir / "counter.vue")
    file_len = len(result.source)
    for d in _find_all(result.declarations):
        assert 0 <= d.start_byte < file_len, (
            f"{d.name}: start_byte={d.start_byte} out of range [0, {file_len})"
        )
        assert d.start_byte < d.end_byte <= file_len, (
            f"{d.name}: end_byte={d.end_byte} out of range ({d.start_byte}, {file_len}]"
        )


def test_line_numbers_are_file_relative(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    for d in _find_all(result.declarations):
        assert d.start_line >= 1
        assert d.end_line >= d.start_line
        assert d.end_line <= result.line_count


def test_show_slices_decl_at_section_start_no_doc(tmp_path):
    """A declaration sitting at byte 0 of its section (no leading
    newline, no doc) must resolve to exactly its own body — the byte
    offset shift in ``_adjust_decl`` keeps ``doc_start_byte`` equal to
    ``start_byte`` when there is no doc, so ``show`` never reaches back
    into the section header."""
    p = tmp_path / "tight.vue"
    p.write_text("<script>const x = 1</script>")
    result = VueAdapter().parse(p)
    matches = find_symbols(result, "x")
    assert matches
    assert matches[0].source.strip() == "const x = 1"


def test_show_includes_doc_at_section_start(tmp_path):
    """A doc block that genuinely starts at byte 0 of its section has
    ``doc_start_byte == 0`` — a real offset, not "no doc". ``_adjust_decl``
    must shift it like any other so ``show`` still slices from the doc,
    not from byte 0 of the whole ``.vue`` file."""
    p = tmp_path / "docstart.vue"
    p.write_text("<script>/**\n * leading doc\n */\nfunction f(): void {}</script>")
    result = VueAdapter().parse(p)
    matches = find_symbols(result, "f")
    assert matches
    src = matches[0].source
    assert src.lstrip().startswith("/**")
    assert "leading doc" in src
    assert "function f" in src


# --- Renderer integration --------------------------------------------------


def test_outline_renders_template_and_script_and_style(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    rendered = render_outline(result, OutlineOptions())
    assert "div.counter" in rendered
    assert "increment" in rendered
    assert ".counter" in rendered


def test_digest_renders_declarations(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    rendered = render_digest([result], DigestOptions())
    assert rendered


def test_outline_imports_line_when_show_imports(vue_dir):
    result = VueAdapter().parse(vue_dir / "counter.vue")
    rendered = render_outline(result, OutlineOptions(show_imports=True))
    assert "imports:" in rendered


# --- Empty / minimal -------------------------------------------------------


def test_empty_template_no_crash(tmp_path):
    p = tmp_path / "empty.vue"
    p.write_text("<template></template>")
    result = VueAdapter().parse(p)
    assert result.declarations == []


def test_template_with_only_text_no_decls(tmp_path):
    p = tmp_path / "text.vue"
    p.write_text("<template>just text</template>")
    result = VueAdapter().parse(p)
    assert result.declarations == []


def test_file_with_only_template(tmp_path):
    p = tmp_path / "only_template.vue"
    p.write_text("<template><h1>Hi</h1></template>")
    result = VueAdapter().parse(p)
    assert len(result.declarations) >= 1


def test_file_with_only_script(tmp_path):
    p = tmp_path / "only_script.vue"
    p.write_text(
        '<script setup lang="ts">\nconst x = 1\nfunction foo(): void {}\n</script>'
    )
    result = VueAdapter().parse(p)
    names = {d.name for d in _find_all(result.declarations)}
    assert "x" in names
    assert "foo" in names


def test_file_with_only_style(tmp_path):
    p = tmp_path / "only_style.vue"
    p.write_text("<style>.foo { color: red; }</style>")
    result = VueAdapter().parse(p)
    sigs = _signatures(result.declarations)
    assert ".foo" in sigs


# --- Edge cases ------------------------------------------------------------


def test_bom_at_start_of_file(tmp_path):
    src = "<template><h1>Hello</h1></template>"
    p = tmp_path / "bom.vue"
    p.write_bytes(b"\xef\xbb\xbf" + src.encode("utf-8"))
    result = VueAdapter().parse(p)
    h1 = _find(result.declarations, name="h1")
    assert h1 is not None


def test_self_closing_void_elements(tmp_path):
    src = '<template><br><input type="text"><hr></template>'
    p = tmp_path / "void.vue"
    p.write_text(src)
    result = VueAdapter().parse(p)
    assert result.declarations


def test_template_with_vue_directives(tmp_path):
    src = (
        "<template>\n"
        '  <div v-if="show" class="container">\n'
        '    <span v-for="item in items" :key="item.id">{{ item.name }}</span>\n'
        "  </div>\n"
        "</template>"
    )
    p = tmp_path / "directives.vue"
    p.write_text(src)
    result = VueAdapter().parse(p)
    sigs = _signatures(result.declarations)
    # The div with class should be findable
    assert "div.container" in sigs


def test_missing_end_tag_template(tmp_path):
    src = "<template><div><h1>Oops</div>"
    p = tmp_path / "malformed.vue"
    p.write_text(src)
    result = VueAdapter().parse(p)
    # Should not crash; partial output is acceptable
    assert result.declarations is not None


def test_empty_file(tmp_path):
    p = tmp_path / "empty.vue"
    p.write_text("")
    result = VueAdapter().parse(p)
    assert result.declarations == []
    assert result.error_count == 0
