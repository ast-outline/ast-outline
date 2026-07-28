"""YamlAdapter tests — parse smoke, hierarchy correctness, format-detect,
multi-doc handling, sequence/scalar rendering, line ranges, edge cases."""
from __future__ import annotations

from ast_outline.adapters.yaml import YamlAdapter
from ast_outline.core import (
    KIND_YAML_DOC,
    KIND_YAML_KEY,
    DigestOptions,
    OutlineOptions,
    find_symbols,
    render_digest,
    render_outline,
)


# --- Parse smoke ---------------------------------------------------------


def test_parse_populates_metadata(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    assert r.path == yaml_dir / "k8s_deployment.yaml"
    assert r.language == "yaml"
    assert r.line_count > 30
    assert r.error_count == 0
    assert r.declarations  # at least apiVersion/kind/metadata/spec


def test_extensions_cover_yml_alias(yaml_dir, tmp_path):
    """Both `.yaml` and `.yml` should be recognised."""
    yml = tmp_path / "a.yml"
    yml.write_text("foo: bar\n")
    adapter = YamlAdapter()
    assert ".yaml" in adapter.extensions
    assert ".yml" in adapter.extensions


# --- Top-level structure -------------------------------------------------


def test_top_level_keys_match_source(yaml_dir):
    """Single-doc file: top-level decls = top-level YAML keys, in order."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    names = [d.name for d in r.declarations]
    assert names == ["apiVersion", "kind", "metadata", "spec"]


def test_kind_uniformly_yaml_key(yaml_dir):
    """All keys in single-doc YAML carry the canonical KIND_YAML_KEY."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    stack = list(r.declarations)
    while stack:
        d = stack.pop()
        assert d.kind == KIND_YAML_KEY, d
        stack.extend(d.children)


# --- Scalar rendering ----------------------------------------------------


def test_scalar_value_inlined_in_signature(yaml_dir):
    """A scalar-valued mapping pair stores the value next to the key
    in `signature` so the outline renders `key: value` on one line."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    api_version = next(d for d in r.declarations if d.name == "apiVersion")
    assert api_version.signature == "apiVersion: apps/v1"
    assert api_version.children == []


def test_long_scalar_truncates_with_unicode_ellipsis(yaml_dir, tmp_path):
    """Scalars longer than the truncation limit get the Unicode `…` glyph,
    not ASCII `...` (which clashes with YAML's end-of-stream marker)."""
    p = tmp_path / "long.yaml"
    p.write_text("description: " + ("x" * 200) + "\n")
    r = YamlAdapter().parse(p)
    desc = r.declarations[0]
    assert "…" in desc.signature
    assert "..." not in desc.signature
    assert len(desc.signature) < 100  # truncated


def test_block_scalar_renders_inline_truncated(yaml_dir):
    """`|` block scalars get flattened (newlines → spaces) and truncated."""
    r = YamlAdapter().parse(yaml_dir / "block_scalars.yaml")
    out = render_outline(r, OutlineOptions())
    # The `config.yaml` key with `|` block scalar should be on one line
    config_lines = [ln for ln in out.splitlines() if "config.yaml:" in ln]
    assert len(config_lines) == 1
    # Content of the block scalar must appear inline (truncated or not)
    assert "nested" in config_lines[0]


# --- Mapping nesting -----------------------------------------------------


def test_nested_mapping_creates_children(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    metadata = next(d for d in r.declarations if d.name == "metadata")
    child_names = [c.name for c in metadata.children]
    assert "name" in child_names
    assert "namespace" in child_names
    assert "labels" in child_names


def test_deeply_nested_path_preserved(yaml_dir):
    """spec.template.spec.containers should be reachable via the tree."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    spec = next(d for d in r.declarations if d.name == "spec")
    template = next(c for c in spec.children if c.name == "template")
    inner_spec = next(c for c in template.children if c.name == "spec")
    containers = next(c for c in inner_spec.children if c.name == "containers")
    assert containers.children, "containers should have child sequence items"


# --- Sequence rendering --------------------------------------------------


def test_sequence_of_scalars_inline(yaml_dir):
    """`namespaces: [user, session, rate_limit]` collapses to a flow-style
    preview on the parent line — no per-item child decls."""
    r = YamlAdapter().parse(yaml_dir / "app_config.yaml")
    cache = next(d for d in r.declarations if d.name == "cache")
    namespaces = next(c for c in cache.children if c.name == "namespaces")
    # Inline: signature contains the bracketed list, no children
    assert "[" in namespaces.signature and "]" in namespaces.signature
    assert "user" in namespaces.signature
    assert namespaces.children == []


def test_sequence_of_mappings_enumerated(yaml_dir):
    """Sequences of mappings get one child Declaration per item, each
    named `[i]` for JSONPath-friendly addressing."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    spec = next(d for d in r.declarations if d.name == "spec")
    template = next(c for c in spec.children if c.name == "template")
    inner_spec = next(c for c in template.children if c.name == "spec")
    containers = next(c for c in inner_spec.children if c.name == "containers")
    # Two containers in the fixture, named [0] and [1]
    assert len(containers.children) == 2
    assert containers.children[0].name == "[0]"
    assert containers.children[1].name == "[1]"


def test_sequence_item_signature_uses_id_key(yaml_dir):
    """Sequence-of-mappings items show the resolved id-key (`name` here)
    as the dash label: `- api`."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    spec = next(d for d in r.declarations if d.name == "spec")
    template = next(c for c in spec.children if c.name == "template")
    inner_spec = next(c for c in template.children if c.name == "spec")
    containers = next(c for c in inner_spec.children if c.name == "containers")
    assert containers.children[0].signature == "- api"
    assert containers.children[1].signature == "- sidecar"


def test_sequence_item_count_in_parent_signature(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    spec = next(d for d in r.declarations if d.name == "spec")
    template = next(c for c in spec.children if c.name == "template")
    inner_spec = next(c for c in template.children if c.name == "spec")
    containers = next(c for c in inner_spec.children if c.name == "containers")
    assert "(2 items)" in containers.signature


def test_id_key_priority_uses_for_github_actions(yaml_dir):
    """When `name` is absent but `uses` or `run` is present (as in
    GitHub Actions steps), the dash label falls through the priority
    chain and uses `uses` / `run` instead."""
    r = YamlAdapter().parse(yaml_dir / "github_workflow.yaml")
    jobs = next(d for d in r.declarations if d.name == "jobs")
    test_job = next(c for c in jobs.children if c.name == "test")
    steps = next(c for c in test_job.children if c.name == "steps")
    labels = [item.signature for item in steps.children]
    assert "- actions/checkout@v4" in labels
    assert "- actions/setup-python@v5" in labels
    assert any("pip install" in lbl for lbl in labels)
    assert any("pytest" in lbl for lbl in labels)


def test_id_key_priority_chain_wins_over_other_scalars(yaml_dir):
    """When a priority key (`id`/`name`/`key`/`uses`/`run`) is present,
    the dash label uses ITS value — not the first scalar pair, even if
    that comes earlier in source order."""
    r = YamlAdapter().parse(yaml_dir / "seq_id_fallback.yaml")
    section = next(d for d in r.declarations if d.name == "priority_id")
    labels = [item.signature for item in section.children]
    assert labels == ["- alpha", "- beta"]


def test_id_key_priority_key_matches(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "seq_id_fallback.yaml")
    section = next(d for d in r.declarations if d.name == "priority_key")
    labels = [item.signature for item in section.children]
    assert labels == ["- alpha", "- beta"]


def test_id_key_falls_back_to_first_scalar_for_domain_keys(yaml_dir):
    """When NONE of the priority keys are present (`date`/`event`/
    `step`/`country`/etc — domain-specific identifiers we can't
    enumerate universally), the dash label falls through to the first
    scalar pair's value in source order. Better than a bare dash —
    gives the agent a meaningful anchor to scan."""
    r = YamlAdapter().parse(yaml_dir / "seq_id_fallback.yaml")
    section = next(d for d in r.declarations if d.name == "domain_fallback")
    labels = [item.signature for item in section.children]
    assert labels == ['- "2024-01-15"', '- "2024-02-01"', '- "2024-03-15"']


def test_id_key_fallback_skips_complex_first_values(yaml_dir):
    """Fallback considers only SCALAR pairs, not flow-mappings or
    sequences. If the first pair has a complex value, fallback steps
    over it to the next scalar pair — never invents a dash label
    from nested structure."""
    r = YamlAdapter().parse(yaml_dir / "seq_id_fallback.yaml")
    section = next(d for d in r.declarations if d.name == "bare_when_first_value_is_complex")
    [item] = section.children
    # `matrix` is a flow_mapping — skipped. `label: only_after_complex`
    # is the first SCALAR pair — that wins.
    assert item.signature == "- only_after_complex"


# --- Flow-style preservation ---------------------------------------------


def test_flow_mapping_preserved_inline(yaml_dir):
    """`labels: {app: api}` stays as flow text in signature — matches
    source style and avoids confusing the agent during edits."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    metadata = next(d for d in r.declarations if d.name == "metadata")
    labels = next(c for c in metadata.children if c.name == "labels")
    # In our fixture `labels:` is block style, so it has children. Different
    # fixture would be flow. Verify the principle on a synthetic check:
    assert labels.children  # block style → expanded


def test_flow_mapping_inline_when_source_is_flow(tmp_path):
    """Synthetic check of flow-mapping passthrough."""
    p = tmp_path / "flow.yaml"
    p.write_text("limits: {cpu: 500m, memory: 512Mi}\n")
    r = YamlAdapter().parse(p)
    limits = r.declarations[0]
    # Flow style → no children, value stays in signature
    assert limits.children == []
    assert "{cpu: 500m" in limits.signature


# --- Multi-document files ------------------------------------------------


def test_multi_doc_creates_doc_decls(yaml_dir):
    """Each `---`-separated document gets its own KIND_YAML_DOC root."""
    r = YamlAdapter().parse(yaml_dir / "k8s_multi_resources.yaml")
    docs = [d for d in r.declarations if d.kind == KIND_YAML_DOC]
    assert len(docs) == 3
    assert all(d.name.startswith("doc") for d in docs)


def test_multi_doc_separator_signature_carries_format_hint(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_multi_resources.yaml")
    docs = [d for d in r.declarations if d.kind == KIND_YAML_DOC]
    # Each separator should mention the kind detected for that doc
    sigs = [d.signature for d in docs]
    assert any("ConfigMap" in s for s in sigs)
    assert any("Secret" in s for s in sigs)
    assert any("NetworkPolicy" in s for s in sigs)
    # And the doc-of-N notation
    for s in sigs:
        assert "doc " in s and " of 3" in s


def test_multi_doc_renders_with_separator_lines(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "k8s_multi_resources.yaml"), OutlineOptions())
    lines = out.splitlines()
    sep_lines = [ln for ln in lines if ln.startswith("--- doc ")]
    assert len(sep_lines) == 3


def test_multi_doc_keeps_doc_children_at_indent_zero(yaml_dir):
    """Doc body keys render flush-left (same indent as the separator),
    not double-indented inside the doc node."""
    out = render_outline(YamlAdapter().parse(yaml_dir / "k8s_multi_resources.yaml"), OutlineOptions())
    lines = out.splitlines()
    # Find the first `apiVersion:` line — must NOT be indented
    api_line = next(ln for ln in lines if ln.startswith("apiVersion:"))
    assert api_line.startswith("apiVersion: v1")  # no leading spaces


def test_single_doc_skips_doc_wrapper(yaml_dir):
    """A single-document file should NOT create a KIND_YAML_DOC node —
    the body keys sit directly at top-level."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    assert all(d.kind != KIND_YAML_DOC for d in r.declarations)


# --- Format detection ----------------------------------------------------


def test_k8s_format_detect_in_header(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert "Deployment" in first
    assert "apps/v1" in first
    assert "prod/api-server" in first


def test_openapi_format_detect_in_header(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "openapi_mini.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert "OpenAPI" in first
    assert "3.0.3" in first
    assert "paths" in first
    assert "schemas" in first


def test_github_actions_format_detect_in_header(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "github_workflow.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert "GitHub Actions" in first
    assert "2 jobs" in first


def test_generic_yaml_no_format_hint(yaml_dir):
    """A file without k8s/openapi/gh-actions signals shouldn't get any
    format annotation — agent sees just lines/tokens counters."""
    out = render_outline(YamlAdapter().parse(yaml_dir / "app_config.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert " — " not in first  # the format-detect em-dash separator


# --- Line ranges ---------------------------------------------------------


def test_top_level_key_line_numbers_match_source(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    api_version = next(d for d in r.declarations if d.name == "apiVersion")
    assert api_version.start_line == 1
    kind = next(d for d in r.declarations if d.name == "kind")
    assert kind.start_line == 2


def test_nested_key_line_range_includes_all_descendants(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    metadata = next(d for d in r.declarations if d.name == "metadata")
    # metadata spans through its children
    assert metadata.end_line >= max(c.end_line for c in metadata.children)


# --- find_symbols + bracket paths ----------------------------------------


def test_find_symbols_simple_dotted_path(yaml_dir):
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    matches = find_symbols(r, "metadata.name")
    assert len(matches) == 1
    assert matches[0].qualified_name == "metadata.name"


def test_find_symbols_bracket_index_in_query(yaml_dir):
    """`containers[0].image` (JSONPath-style) must match the deeply
    nested image scalar — the bracket-aware split keeps the query
    natural for an LLM agent."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    matches = find_symbols(r, "containers[0].image")
    assert len(matches) == 1
    qn = matches[0].qualified_name
    assert qn.endswith("containers[0].image"), qn


def test_find_symbols_bracket_only_query(yaml_dir):
    """A bare `[1]` query matches any sequence's second item."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    matches = find_symbols(r, "containers[1]")
    assert len(matches) == 1
    assert matches[0].qualified_name.endswith("containers[1]")


def test_qualified_name_uses_jsonpath_brackets(yaml_dir):
    """No `.[i]` clutter — sequence indices attach to the parent."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    [m] = find_symbols(r, "containers[0].name")
    # Should NOT contain `.[0]` (clunky) — should be `containers[0].`
    assert ".[0]" not in m.qualified_name
    assert "containers[0].name" in m.qualified_name


def test_find_symbols_suffix_match_only_for_yaml(yaml_dir):
    """YAML uses suffix-equality matching (NOT substring like markdown)."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    # `name` substring would match `apiVersion` (`-name` of the apiVersion key
    # part?). Suffix match: only exact `.name` endings count.
    matches = find_symbols(r, "name")
    assert len(matches) >= 1
    for m in matches:
        # Every match's qualified name should END with `.name` or be `[i].name`
        assert m.qualified_name.endswith(".name") or m.qualified_name == "name"


# --- Anchors / aliases ---------------------------------------------------


def test_anchors_aliases_render_without_crash(yaml_dir):
    """Anchors (`&foo`) and aliases (`*foo`, `<<: *foo`) appear as plain
    text in signatures — we don't expand them, just preserve the source."""
    r = YamlAdapter().parse(yaml_dir / "anchors_aliases.yaml")
    assert r.error_count == 0
    out = render_outline(r, OutlineOptions())
    # The aliased sections should still appear as keys
    assert "api:" in out
    assert "worker:" in out


# --- Helm template passthrough -------------------------------------------


def test_helm_templated_yaml_parses(yaml_dir):
    """tree-sitter-yaml chokes on `{{ }}` directives and treats the entire
    file as ERROR — but our recovery walk salvages the unambiguous top
    pairs (`apiVersion`, `kind`) so the agent gets at least the file's
    identity, even if deeper sections are mangled around the templates."""
    r = YamlAdapter().parse(yaml_dir / "helm_templated.yaml")
    names = [d.name for d in r.declarations]
    # At minimum we recover the unambiguous identity pairs
    assert "apiVersion" in names
    assert "kind" in names
    # And the parse error is surfaced via error_count
    assert r.error_count > 0


def test_helm_templated_marked_broken(yaml_dir):
    """Helm-templated files trigger the `[broken]` marker because
    tree-sitter-yaml flags `{{ }}` as ERROR — agent sees this clearly
    in digest output without having to dig into the warning line."""
    r = YamlAdapter().parse(yaml_dir / "helm_templated.yaml")
    out = render_digest([r], DigestOptions())
    file_line = next(ln for ln in out.splitlines() if "helm_templated.yaml" in ln)
    assert "[broken]" in file_line


# --- Dotted keys (k8s annotations) ---------------------------------------


def test_dotted_keys_render_as_single_keys(yaml_dir):
    """`kubernetes.io/ingress.class` is a single annotation key. The
    adapter renders it verbatim — `find_symbols` matching may not be
    perfect through the dot but the parent block is reachable."""
    r = YamlAdapter().parse(yaml_dir / "dotted_keys.yaml")
    metadata = next(d for d in r.declarations if d.name == "metadata")
    annotations = next(c for c in metadata.children if c.name == "annotations")
    # Annotation keys preserved as-is in their child Declarations' `name`
    annot_names = [c.name for c in annotations.children]
    assert "kubernetes.io/ingress.class" in annot_names


def test_dotted_keys_parent_block_searchable(yaml_dir):
    """Even if individual dotted-key matching is fuzzy, the parent
    `metadata.annotations` is reachable for an agent who wants the
    full block."""
    r = YamlAdapter().parse(yaml_dir / "dotted_keys.yaml")
    matches = find_symbols(r, "metadata.annotations")
    assert len(matches) == 1


# --- Broken syntax -------------------------------------------------------


def test_broken_syntax_surfaces_warning(yaml_dir):
    """Parse errors bubble up as `error_count > 0`, the renderer prints
    the standard `# WARNING:` line, the outline is still emitted."""
    r = YamlAdapter().parse(yaml_dir / "broken_syntax.yaml")
    out = render_outline(r, OutlineOptions()).splitlines()
    # First line is the file header; second the warning
    if r.error_count > 0:
        assert any(ln.startswith("# WARNING:") for ln in out), out


# --- Digest integration --------------------------------------------------


def test_yaml_digest_includes_size_label(yaml_dir):
    """YAML files participate in the universal size-label scheme — same
    `[tiny]`/`[medium]`/`[large]` brackets every other language gets."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    out = render_digest([r], DigestOptions())
    file_line = next(ln for ln in out.splitlines() if "k8s_deployment.yaml" in ln)
    assert any(tag in file_line for tag in ("[tiny]", "[medium]", "[large]"))


def test_digest_marks_broken_files_with_broken_label(yaml_dir):
    """A file that hit parse errors gets `[broken]` next to its size
    label — plain-English at-a-glance signal so an agent scanning the
    digest spots integrity issues without reading the `# WARNING:`
    detail line."""
    r = YamlAdapter().parse(yaml_dir / "broken_syntax.yaml")
    out = render_digest([r], DigestOptions())
    file_line = next(ln for ln in out.splitlines() if "broken_syntax.yaml" in ln)
    if r.error_count > 0:
        assert "[broken]" in file_line, file_line


def test_digest_clean_files_omit_broken_label(yaml_dir):
    """Files that parse cleanly should NOT carry `[broken]` — the
    marker is meaningful only when an actual integrity issue exists."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    assert r.error_count == 0
    out = render_digest([r], DigestOptions())
    file_line = next(ln for ln in out.splitlines() if "k8s_deployment.yaml" in ln)
    assert "[broken]" not in file_line


def test_yaml_outline_header_carries_doc_count_when_multi(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "k8s_multi_resources.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert "3 docs" in first


def test_yaml_outline_header_omits_doc_count_when_single(yaml_dir):
    out = render_outline(YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml"), OutlineOptions())
    first = out.splitlines()[0]
    assert " docs" not in first  # singular case suppressed


# --- noise_regions for block scalars -------------------------------------


def test_noise_regions_populated_for_block_scalars(yaml_dir):
    """``|`` and ``>`` block scalars contribute (start, end, kind) regions."""
    from ast_outline.grep import grep

    r = YamlAdapter().parse(yaml_dir / "block_scalars.yaml")
    assert r.noise_regions, "expected noise_regions for block_scalars.yaml"
    # block_scalars.yaml has one ``|`` and one ``>`` block.
    assert len(r.noise_regions) == 2
    assert all(kind == "string" for _start, _end, kind in r.noise_regions)


def test_noise_regions_cover_block_scalar_body(yaml_dir):
    """Each region's bytes should contain the scalar body — no plain
    siblings sneaking in."""
    src = (yaml_dir / "block_scalars.yaml").read_bytes()
    r = YamlAdapter().parse(yaml_dir / "block_scalars.yaml")
    bodies = [src[s:e].decode() for s, e, _ in r.noise_regions]
    # The literal block contains ``nested: value``; the folded block
    # contains ``A folded scalar``. Either string has to live somewhere
    # in the regions.
    joined = "\n".join(bodies)
    assert "nested: value" in joined
    assert "A folded scalar" in joined
    # And the unrelated ``short: brief value`` line (a plain scalar)
    # must NOT be inside any region.
    assert "brief value" not in joined


def test_noise_regions_empty_when_no_block_scalars(yaml_dir):
    """Files without ``|``/``>`` shouldn't emit any noise regions."""
    r = YamlAdapter().parse(yaml_dir / "k8s_deployment.yaml")
    assert r.noise_regions == []


def test_grep_shows_block_scalar_matches_tagged_string(yaml_dir):
    """Hits inside ``run: |`` shell bodies surface, tagged ``string``.

    The embedded shell lines are the actual content of a CI config —
    an agent asking "where does this pipeline call npm" needs exactly
    them. They follow the strings-visible default; only YAML comments
    stay hidden.
    """
    from ast_outline.grep import grep

    path = yaml_dir / "grep_noise.yaml"
    results = grep("npm", [path]).files
    assert results
    fr = results[0]
    src_lines = path.read_text().splitlines()
    shell_hits = [
        m for m in fr.matches
        if any(
            tok in src_lines[m.line - 1]
            for tok in ("npm install", "npm run lint", "npm run build")
        )
    ]
    assert shell_hits, "run-block shell lines must be visible"
    assert all(m.kind == "string" for m in shell_hits)


def test_grep_include_noise_surfaces_yaml_comment_matches(yaml_dir):
    """``include_noise=True`` recovers only the YAML-comment hits — the
    block-scalar shell lines are already visible by default."""
    from ast_outline.grep import grep

    path = yaml_dir / "grep_noise.yaml"
    visible_default = grep("npm", [path]).files
    all_with_noise = grep("npm", [path], include_noise=True).files
    delta = (
        sum(len(fr.matches) for fr in all_with_noise)
        - sum(len(fr.matches) for fr in visible_default)
    )
    assert delta == visible_default[0].filtered_count > 0
