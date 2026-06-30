"""HTML adapter (.html, .htm).

Produces an IR that maps the page's semantic structure for navigation,
rendering each element as a CSS-selector token so the same vocabulary
covers HTML and CSS / SCSS::

    # landing.html (53 lines, ~420 tokens, 18 elements)
    html[lang=en]
    head                                                    L2-10
        title                                               L7
        meta[name=description]                              L6
        [import] link[rel=stylesheet href=/css/main.css]    L8
        [import] script[defer src=/js/analytics.js]         L10
    body                                                    L12-53
        header.site-nav                                     L13-16
        main                                                L18-50
            section#hero                                    L19-23
                h1: Pull exactly the context you need
            section#features                                L25-30
                h2: Why teams switch
                h3 ×3: Faster reads · Sharper answers · No daemon
            section#faq                                     L37-43
                details ×6
        footer.site-footer                                  L45-48
        form#newsletter[action=/subscribe method=post]      L50-53
            input[name=email type=email required]
            button[type=submit]

Selector rendering follows the convention an LLM already knows from CSS:
``tag``, ``tag#id``, ``tag.cls1.cls2`` (compound), ``tag[attr=val
attr2=val2]`` (attribute selector with space-separated pairs and
quote-stripped values). Compound forms combine in the canonical order
``tag#id.cls[attrs]``. The ``show`` command accepts any of these tokens
— ``#hero``, ``.site-nav``, ``section#hero``, ``[rel=stylesheet]`` —
because ``match_names`` carries every reachable form per element.

Noise / drop rules (the outline shows structure, not the whole page):

- Bare ``<div>`` / ``<span>`` / ``<p>`` / ``<li>`` / ``<tr>`` etc. (no
  id, no class, no significant attribute) are NOT emitted, but their
  meaningful descendants float up to the parent's depth so hierarchy
  survives. Real-world templates have many wrapping containers; listing
  them every level inflates the outline to no signal.
- Inline text-styling tags (``b``, ``i``, ``em``, ``strong``, ``code``,
  ``span``-like decoration) are never emitted, even with id/class — they
  carry zero outline signal.
- ``<svg>`` / ``<math>`` render the root element but skip recursion into
  children (one inline-SVG icon is 30-50 ``<path>`` elements, none of
  them addressable via CSS the way HTML elements are).
- ``<script>`` and ``<style>`` bodies land in ``noise_regions`` (kind
  ``"string"``); HTML comments land there too (kind ``"comment"``).
  Under grep's strings-visible default the script/style hits surface
  tagged ``[string]`` (inline JS is code — hiding a hit there reads as
  a false "not used"); comment hits stay hidden.
- A run of 3+ consecutive sibling bare ``<details>`` collapses to one
  synthetic ``details ×N`` line (FAQ pages otherwise dominate the
  outline with identical leaf nodes).

Imports: ``<link rel=stylesheet|preload|prefetch|modulepreload|icon|
manifest …>`` and ``<script src=…>`` are surfaced both as a leading
``[import]`` token on the element's signature AND in
``ParseResult.imports`` (so ``--imports`` lists them) AND in
``import_regions`` (so ``grep`` promotes inner matches to ``[import]``).
Inline ``<script>`` (no ``src``) is content, not import.

Templating constructs (Jinja ``{% %}``, Vue ``{{ }}``, Handlebars,
PHP ``<?php ?>``) are not parsed by tree-sitter-html and land as plain
text or trigger ERROR nodes. The structural skeleton of everything
tree-sitter does recognise still renders normally; the header surfaces
``# WARNING: N parse errors`` so the agent knows the outline is partial
in those spots. When the top-level walk produces nothing because the
parser wrapped the whole document in a single ERROR (Jinja ``{% if %}``
at the root, raw Vue/Svelte template, PHP file starting with ``<?php``),
a one-pass recovery walks into the ERROR subtree to surface any
well-formed elements it can reach — a partial outline beats a blank one.

Out of scope (deliberate exclusions, may revisit in 1.x):

- ``<base href="…">`` is NOT classified as an import. ``<base>`` sets
  the document base URL for relative URLs — semantically different from
  pulling an external resource.
- Inline ``<script>`` (no ``src``) is content, not an import — even
  ``<script type="module">import './x.js'</script>``. Inline JS bodies
  go to ``noise_regions`` (kind ``"string"``, visible in grep output).
- ``data-*`` and ``aria-*`` attributes are not promoted to the
  significant-attribute whitelist (would over-inflate the bracketed
  selectors). ``role="…"`` similarly omitted — landmark elements are
  already covered by their semantic tag (``<nav>``, ``<main>``, …).
- ``<svg>`` / ``<math>`` subtrees show the root element only (children
  not recursed). Inline SVG icons typically contain 30-50 unaddressable
  paths and would dominate the outline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_html as tshtml
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ._css_base import first_named_child, line_for, text_of
from ..core import (
    KIND_HTML_ELEMENT,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(tshtml.language())
_PARSER = Parser(_LANGUAGE)


# Tags whose value carries the page title / section heading. Get a text
# preview suffix in the signature: ``h1: Pull exactly the context you need``.
# Truncation matches YAML's scalar limit (60 chars) for visual consistency
# across data-shaped outlines.
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HEADING_TEXT_LIMIT = 60

# Used for truncating long attribute values inside ``[…]`` brackets
# (``content``, ``href``, ``src``, ``alt``). The limit is intentionally
# tighter than headings — attributes are width-sensitive (they share a
# line with the selector and line range), while headings own their line.
_ATTR_VALUE_TRUNCATE_LIMIT = 40

# Per-tag whitelist of attributes promoted to the ``[attr=val …]``
# bracketed selector. Tight by design: HTML elements often carry ARIA,
# data-*, and styling attributes that add no structural signal and
# would bloat the outline. Add to the list when a user hits "I needed
# `data-testid` for this query."
_SIGNIFICANT_ATTRS_BY_TAG: dict[str, tuple[str, ...]] = {
    "input": (
        "type", "name", "required", "disabled", "checked", "value",
        "placeholder",
    ),
    "button": ("type", "name", "value", "disabled"),
    "form": ("action", "method", "name", "enctype"),
    "select": ("name", "multiple", "required", "disabled"),
    "textarea": ("name", "required", "disabled"),
    "option": ("value", "selected", "disabled"),
    "optgroup": ("label", "disabled"),
    "label": ("for",),
    "fieldset": ("name", "disabled"),
    "legend": (),
    "link": ("rel", "href", "media", "as", "type", "hreflang"),
    "script": ("src", "type", "async", "defer", "nomodule"),
    "meta": ("name", "property", "http-equiv", "charset", "content"),
    "a": ("href", "rel", "target"),
    "img": ("src", "srcset", "sizes", "alt", "loading", "decoding"),
    "iframe": ("src", "name", "loading", "sandbox"),
    "html": ("lang", "dir"),
    "video": ("src", "controls", "autoplay", "loop", "muted", "poster"),
    "audio": ("src", "controls", "autoplay", "loop", "muted"),
    "source": ("src", "srcset", "sizes", "type", "media"),
    "track": ("src", "kind", "srclang", "label", "default"),
    "object": ("data", "type"),
    "embed": ("src", "type"),
    "area": ("href", "alt", "shape"),
    "base": ("href", "target"),
}

# Attribute values that should be truncated for readability inside the
# bracket. URLs, alt text, og:image content are the usual offenders.
_TRUNCATE_ATTRS: frozenset[str] = frozenset({
    "href", "src", "alt", "content", "action", "data", "poster",
    "srcset", "sizes", "value", "placeholder",
})

# Tags that ARE emitted normally only if they carry id, class, or a
# significant attribute. Otherwise they're dropped from the outline,
# but their meaningful descendants float up to the parent's depth.
# Rationale: a typical modern template wraps every visible element in
# 5-10 nondescript ``<div>``s; listing each one would inflate the
# outline to no signal. The lifted-children rule preserves the structure
# of the *meaningful* descendants without the noise floors.
_DROP_BARE_TAGS = frozenset({
    "div", "span", "p", "li", "tr", "td", "th", "dt", "dd",
    "br", "hr", "wbr", "option", "optgroup",
    "ul", "ol", "dl", "tbody", "thead", "tfoot", "colgroup", "col",
    "picture",
})

# Tags whose body is always inline text decoration — they contribute no
# structural signal even with id/class, so they're never emitted (and
# their children are skipped).
_INLINE_SKIP_TAGS = frozenset({
    "b", "i", "em", "strong", "u", "s", "small", "sub", "sup", "mark",
    "kbd", "code", "var", "samp", "cite", "q", "abbr", "dfn", "time",
    "ruby", "rt", "rp", "bdi", "bdo",
})

# Tags whose subtree is rendered only as a root element with no children
# recursion. SVG icons commonly contain 30-50 ``<path>`` / ``<rect>``
# children that aren't CSS-addressable the way HTML elements are; MathML
# is similar. The root element keeps its selector form (``svg.icon``)
# so ``show svg.icon`` still works.
_SUBTREE_COLLAPSE_TAGS = frozenset({"svg", "math"})

# ``<link rel="…">`` values that mark an external asset reference. The
# value is space-separated (``rel="preload modulepreload"`` is valid) —
# any token match promotes the link to an import.
_IMPORT_LINK_RELS = frozenset({
    "stylesheet", "preload", "prefetch", "modulepreload", "icon",
    "manifest", "shortcut",
})

# Minimum run length before consecutive bare ``<details>`` siblings
# collapse to one ``details ×N`` line. Sub-threshold runs render
# normally so a 1-2 ``<details>`` FAQ stays readable.
_DETAILS_RUN_COLLAPSE_THRESHOLD = 3


class HtmlAdapter:
    language_name = "html"
    display_name = "HTML"
    extensions = {".html", ".htm"}
    # Markup — elements are tag-based, no leading declaration keyword.
    definition_keywords = frozenset()
    comment_line_prefixes = ()
    import_line_prefixes = ()
    render_family = "html"

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls: list[Declaration] = []
        imports: list[str] = []
        import_regions: list[tuple[int, int]] = []
        noise_regions: list[tuple[int, int, str]] = []
        for child in tree.root_node.named_children:
            decls.extend(_node_to_decls(
                child, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            ))
        # If the top-level walk produced nothing AND tree-sitter flagged
        # parse errors, the document is likely wrapped in a single ERROR
        # node — typical for heavily templated HTML (Jinja ``{% if %}``
        # at the document root, Vue SFC with raw template, PHP files
        # opening with ``<?php`` before any markup). Walk into ERROR
        # wrappers once to recover any well-formed elements inside. This
        # mirrors ``yaml._recover_pairs`` and gives templated files a
        # partial outline instead of a blank one (the WARNING line still
        # surfaces the parse errors).
        if not decls and tree.root_node.has_error:
            decls.extend(_recover_elements(
                tree.root_node, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            ))
        decls = _collapse_details_runs(decls)
        noise_regions.sort()
        import_regions.sort()
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            imports=imports,
            import_regions=import_regions,
            noise_regions=noise_regions,
        )


# ---------------------------------------------------------------------------
# Node dispatch
# ---------------------------------------------------------------------------


def _node_to_decls(
    node: Node,
    src: bytes,
    *,
    imports: list[str],
    import_regions: list[tuple[int, int]],
    noise_regions: list[tuple[int, int, str]],
) -> list[Declaration]:
    """Dispatch one tree-sitter HTML node to zero, one, or many Declarations.

    Returns a list because the drop-but-lift rule can promote a bare
    container's children to the caller's depth (one in, many out), and
    non-element nodes (``comment``, ``doctype``, ``text``) produce zero
    declarations but may still register noise.
    """
    t = node.type
    if t == "comment":
        noise_regions.append((node.start_byte, node.end_byte, "comment"))
        return []
    if t in ("element", "script_element", "style_element", "self_closing_tag"):
        return _element_to_decls(
            node, src,
            imports=imports,
            import_regions=import_regions,
            noise_regions=noise_regions,
        )
    # doctype, text, entity, raw_text outside script/style, erroneous_end_tag → nothing
    return []


def _element_to_decls(
    node: Node,
    src: bytes,
    *,
    imports: list[str],
    import_regions: list[tuple[int, int]],
    noise_regions: list[tuple[int, int, str]],
) -> list[Declaration]:
    """Convert one element-like node into zero, one, or many Declarations.

    Returns:
    - ``[]`` when the element is inline-skipped (§5.2) or its bare form
      is dropped *without* meaningful descendants.
    - ``[decl]`` for the normal case.
    - ``[child_a, child_b, …]`` when drop-but-lift fires — the
      element itself is bare-and-dropped, but its children carry
      structure that floats up to the caller's depth.
    """
    start_tag = _find_open_tag(node)
    if start_tag is None:
        return []
    tag, id_, classes, all_attr_pairs = _extract_attrs(start_tag, src)
    if not tag:
        return []
    tag = tag.lower()

    if tag in _INLINE_SKIP_TAGS:
        return []

    # Significant attrs are a subset of all attributes — used both for
    # selector rendering and for the drop-bare check.
    significant_keys = _SIGNIFICANT_ATTRS_BY_TAG.get(tag, ())
    significant_pairs = [
        (k, v) for (k, v) in all_attr_pairs if k in significant_keys
    ]
    attrs_dict = dict(all_attr_pairs)

    is_import = _is_import_element(tag, attrs_dict)

    # SVG / math — emit root, no children recursion.
    if tag in _SUBTREE_COLLAPSE_TAGS:
        decl = _build_decl(
            node, src, tag, id_, classes, significant_pairs,
            is_import=False, heading_text=None, children=[],
        )
        return [decl]

    is_bare = (
        not id_
        and not classes
        and not significant_pairs
        and not is_import
    )

    if tag in _DROP_BARE_TAGS and is_bare:
        # Drop this element, lift children up.
        lifted: list[Declaration] = []
        for child in node.named_children:
            lifted.extend(_node_to_decls(
                child, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            ))
        return _collapse_details_runs(lifted)

    # script / style: body is opaque to outline; register as noise.
    if tag in ("script", "style"):
        for child in node.named_children:
            if child.type == "raw_text":
                noise_regions.append(
                    (child.start_byte, child.end_byte, "string")
                )
                break
        child_decls: list[Declaration] = []
    else:
        child_decls = []
        for child in node.named_children:
            child_decls.extend(_node_to_decls(
                child, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            ))
        child_decls = _collapse_details_runs(child_decls)

    heading_text: Optional[str] = None
    if tag in _HEADING_TAGS:
        raw = _collect_text(node, src)
        if raw:
            heading_text = _truncate(raw, _HEADING_TEXT_LIMIT)

    decl = _build_decl(
        node, src, tag, id_, classes, significant_pairs,
        is_import=is_import, heading_text=heading_text, children=child_decls,
    )

    if is_import:
        # Source-true text uses the canonical attribute set the
        # whitelist would have rendered — equals the signature minus
        # the leading ``[import] `` prefix and any heading suffix.
        imports.append(_render_selector(
            tag, id_, classes, significant_pairs, heading_text=None,
        ))
        # Tag the element's full byte range (open tag for void elements,
        # whole element for `<script src>…</script>`) as an import region
        # so grep promotes inner matches to ``[import]``.
        import_regions.append((node.start_byte, node.end_byte))

    return [decl]


# ---------------------------------------------------------------------------
# Attribute extraction
# ---------------------------------------------------------------------------


def _find_open_tag(node: Node) -> Optional[Node]:
    """Return the node carrying the tag name + attributes.

    For ``element`` and ``script_element`` / ``style_element`` that's
    a ``start_tag`` child. For ``self_closing_tag`` it's the node
    itself (a self-closing tag carries its own tag_name + attributes).
    """
    if node.type == "self_closing_tag":
        return node
    return first_named_child(node, "start_tag")


def _extract_attrs(
    start_tag: Node, src: bytes
) -> tuple[str, Optional[str], list[str], list[tuple[str, Optional[str]]]]:
    """Pull tag name, id, classes (in source order), and (attr, value)
    pairs from a start_tag / self_closing_tag node.

    Boolean attributes (no ``=value`` in source) come back with
    ``value=None`` — the renderer prints them bare (``required``,
    not ``required=""``).
    """
    tag = ""
    id_: Optional[str] = None
    raw_classes: list[str] = []
    pairs: list[tuple[str, Optional[str]]] = []
    for child in start_tag.named_children:
        if child.type == "tag_name":
            tag = text_of(child, src)
        elif child.type == "attribute":
            name, value = _attr_name_value(child, src)
            if not name:
                continue
            lower = name.lower()
            if lower == "id" and value:
                id_ = value
            elif lower == "class" and value:
                # HTML class attribute is whitespace-separated.
                raw_classes.extend(value.split())
            else:
                pairs.append((lower, value))
    # Dedup classes preserving first-seen order. ``class="btn btn primary"``
    # otherwise renders as ``tag.btn.btn.primary`` and inflates
    # ``match_names`` with the same selector twice.
    seen_cls: set[str] = set()
    classes: list[str] = []
    for c in raw_classes:
        if c not in seen_cls:
            seen_cls.add(c)
            classes.append(c)
    # Dedup duplicate same-name attributes (``<a href="x" href="y">``)
    # last-wins, matching the HTML/DOM specification — browsers ignore
    # the earlier duplicate. Without this dedup, the bracketed selector
    # repeats the attribute and ``match_names`` carries both forms.
    if len(pairs) > 1:
        deduped: dict[str, Optional[str]] = {}
        for k, v in pairs:
            deduped[k] = v
        if len(deduped) != len(pairs):
            pairs = list(deduped.items())
    return tag, id_, classes, pairs


def _attr_name_value(
    attr_node: Node, src: bytes
) -> tuple[str, Optional[str]]:
    """Extract (name, value) from an ``attribute`` node.

    Value is ``None`` for boolean attributes (no ``=value`` written).
    Quotes around ``quoted_attribute_value`` are stripped — the
    bracketed selector form (``[name=email]``) doesn't need them, and
    quote characters inside the bracket would be noise.
    """
    name = ""
    value: Optional[str] = None
    has_value = False
    for child in attr_node.named_children:
        ct = child.type
        if ct == "attribute_name":
            name = text_of(child, src)
        elif ct == "quoted_attribute_value":
            has_value = True
            inner = first_named_child(child, "attribute_value")
            value = text_of(inner, src) if inner is not None else ""
        elif ct == "attribute_value":
            has_value = True
            value = text_of(child, src)
    if not has_value:
        return name, None
    return name, value


# ---------------------------------------------------------------------------
# Selector rendering + match_names
# ---------------------------------------------------------------------------


def _render_selector(
    tag: str,
    id_: Optional[str],
    classes: list[str],
    attr_pairs: list[tuple[str, Optional[str]]],
    *,
    heading_text: Optional[str],
) -> str:
    """Canonical CSS-selector form for one element, without the
    ``[import] `` prefix.

    Order: ``tag``, then ``#id`` (if any), then ``.cls1.cls2…``
    (source order), then ``[attr=val attr2=val2]`` (source order).
    Heading-text suffix (``: Pull exactly the context you need``) is
    appended last when present — heading previews share a line with
    the selector but never enter ``match_names``.
    """
    parts = [tag]
    if id_:
        parts.append(f"#{id_}")
    for cls in classes:
        parts.append(f".{cls}")
    sig = "".join(parts)
    if attr_pairs:
        sig += "[" + " ".join(_render_attr_pair(k, v) for k, v in attr_pairs) + "]"
    if heading_text is not None:
        sig += f": {heading_text}"
    return sig


def _render_attr_pair(name: str, value: Optional[str]) -> str:
    """Format one ``attr=value`` pair for the bracketed selector. Boolean
    attrs render bare; long values get truncated.

    Values without whitespace / brackets render unquoted (the bracket
    itself disambiguates token boundaries) — ``href=/css/main.css``,
    ``type=email``. Values containing whitespace, a closing bracket, or
    a quote get wrapped in double quotes so the multi-pair bracketed
    form (``[a=1 b=2 c=3]``) stays unambiguous; inner double quotes are
    backslash-escaped. The quoted form is valid CSS attribute-selector
    syntax — ``[value="Save changes"]`` works in stylesheets too — so
    the selector tokens stay round-trippable.
    """
    if value is None:
        return name
    if name in _TRUNCATE_ATTRS and len(value) > _ATTR_VALUE_TRUNCATE_LIMIT:
        value = _truncate(value, _ATTR_VALUE_TRUNCATE_LIMIT)
    if any(ch in value for ch in ' \t]"'):
        value = '"' + value.replace('"', '\\"') + '"'
    return f"{name}={value}"


def _build_match_names(
    tag: str,
    id_: Optional[str],
    classes: list[str],
    attr_pairs: list[tuple[str, Optional[str]]],
    canonical: str,
) -> list[str]:
    """Every CSS-selector form the agent might query the element by.

    Includes the bare tag, ``tag#id`` and ``#id``, ``tag.cls`` and
    ``.cls`` for each class, ``tag[attr=val]`` and ``[attr=val]`` for
    each significant attribute, and the full canonical compound form.
    Dedup preserves insertion order so the first / cheapest form is
    surfaced when ``show`` reports the match.
    """
    out: list[str] = [tag]
    if id_:
        out.append(f"{tag}#{id_}")
        out.append(f"#{id_}")
    for cls in classes:
        out.append(f"{tag}.{cls}")
        out.append(f".{cls}")
    for name, value in attr_pairs:
        rendered = _render_attr_pair(name, value)
        out.append(f"{tag}[{rendered}]")
        out.append(f"[{rendered}]")
    if canonical not in out:
        out.append(canonical)
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


# ---------------------------------------------------------------------------
# Declaration builders
# ---------------------------------------------------------------------------


def _build_decl(
    node: Node,
    src: bytes,
    tag: str,
    id_: Optional[str],
    classes: list[str],
    attr_pairs: list[tuple[str, Optional[str]]],
    *,
    is_import: bool,
    heading_text: Optional[str],
    children: list[Declaration],
) -> Declaration:
    canonical = _render_selector(
        tag, id_, classes, attr_pairs, heading_text=None
    )
    signature = _render_selector(
        tag, id_, classes, attr_pairs, heading_text=heading_text
    )
    if is_import:
        signature = f"[import] {signature}"
    match_names = _build_match_names(tag, id_, classes, attr_pairs, canonical)
    return Declaration(
        kind=KIND_HTML_ELEMENT,
        name=tag,
        signature=signature,
        native_kind=tag,
        start_line=line_for(node, src),
        end_line=line_for(node, src, end=True),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        children=children,
        match_names=match_names,
    )


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def _is_import_element(tag: str, attrs: dict[str, Optional[str]]) -> bool:
    """``<link>`` with an asset-bearing ``rel`` token, or ``<script src=…>``."""
    if tag == "link":
        rel = attrs.get("rel")
        if not rel:
            return False
        tokens = rel.split()
        return any(t.lower() in _IMPORT_LINK_RELS for t in tokens)
    if tag == "script":
        return attrs.get("src") is not None
    return False


# ---------------------------------------------------------------------------
# Heading text collection
# ---------------------------------------------------------------------------


def _collect_text(node: Node, src: bytes) -> str:
    """Concatenate all ``text`` descendants of a heading element,
    whitespace-collapsed. Used to produce the ``: text`` preview suffix
    on ``<h1>``–``<h6>``. Returns ``""`` when the heading has no text
    children (rare — e.g. ``<h1><img alt="logo"></h1>``).
    """
    pieces: list[str] = []

    # Recursive preorder walk so text lands in SOURCE order. The
    # previous LIFO-stack version appended a node's direct text children
    # inline while deferring element children to the stack — for
    # ``<h2><a>Section</a> title</h2>`` that emitted ``" title"`` before
    # the ``<a>``'s inner ``"Section"`` (→ ``"titleSection"``).
    def visit(n: Node) -> None:
        for child in n.children:
            if child.type == "text":
                pieces.append(text_of(child, src))
            elif child.type == "entity":
                # Render entities as their raw source (``&amp;``); decoding
                # would require an HTML-entity table for a heading preview,
                # which adds dependency surface for marginal gain.
                pieces.append(text_of(child, src))
            else:
                visit(child)

    visit(node)
    # Join pieces with a space, then collapse: tree-sitter-html trims
    # the whitespace between an inline element and adjacent text out of
    # the ``text`` nodes, so butt-joining would fuse words across
    # element boundaries (``<a>Section</a> title`` → ``Sectiontitle``).
    return " ".join(" ".join(pieces).split())


def _truncate(value: str, limit: int) -> str:
    """Shorten ``value`` to ``limit`` chars with a trailing ``…``. No-op
    when already within the limit. Mirrors the YAML adapter's truncation
    so output across data-shaped adapters reads the same."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Details-run collapse
# ---------------------------------------------------------------------------


def _collapse_details_runs(siblings: list[Declaration]) -> list[Declaration]:
    """Replace runs of ``_DETAILS_RUN_COLLAPSE_THRESHOLD``+ consecutive
    bare ``<details>`` siblings with a single synthetic
    ``details ×N`` declaration. Bare = no id, no class, no significant
    attribute (signature is exactly ``"details"``).

    FAQ pages routinely have 6-20 identical ``<details>`` blocks; without
    this collapse the outline would dedicate one line per FAQ entry with
    zero discrimination. A ``<details class="advanced">`` mid-run breaks
    the run because it carries real signal.
    """
    if not siblings:
        return siblings
    out: list[Declaration] = []
    i, n = 0, len(siblings)
    while i < n:
        if not _is_bare_details(siblings[i]):
            out.append(siblings[i])
            i += 1
            continue
        j = i
        while j < n and _is_bare_details(siblings[j]):
            j += 1
        run = siblings[i:j]
        if len(run) >= _DETAILS_RUN_COLLAPSE_THRESHOLD:
            first, last = run[0], run[-1]
            out.append(Declaration(
                kind=KIND_HTML_ELEMENT,
                name="details",
                signature=f"details ×{len(run)}",
                native_kind="details",
                start_line=first.start_line,
                end_line=last.end_line,
                start_byte=first.start_byte,
                end_byte=last.end_byte,
                match_names=["details"],
            ))
        else:
            out.extend(run)
        i = j
    return out


def _is_bare_details(decl: Declaration) -> bool:
    return (
        decl.kind == KIND_HTML_ELEMENT
        and decl.name == "details"
        and decl.signature == "details"
    )


# ---------------------------------------------------------------------------
# ERROR-node recovery
# ---------------------------------------------------------------------------


_ELEMENT_NODE_TYPES = frozenset({
    "element", "script_element", "style_element", "self_closing_tag",
})


def _recover_elements(
    node: Node,
    src: bytes,
    *,
    imports: list[str],
    import_regions: list[tuple[int, int]],
    noise_regions: list[tuple[int, int, str]],
) -> list[Declaration]:
    """Descend through ERROR-wrapped subtrees collecting any well-formed
    elements the recovery point can reach.

    Called once when the main top-level walk produced no declarations
    but the tree contains errors — usually a templated document
    (``{% if %}…{% endif %}`` at the root, raw Vue/Svelte template) the
    parser wrapped in a single ERROR node.

    Returns the elements found at the first level where any exist —
    not recursively into deeper elements, because each ``_node_to_decls``
    call already recurses into its own children. Going deeper here
    would duplicate the subtree.
    """
    out: list[Declaration] = []
    for c in node.named_children:
        if c.type in _ELEMENT_NODE_TYPES or c.type == "comment":
            out.extend(_node_to_decls(
                c, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            ))
    if out:
        return out
    for c in node.named_children:
        if c.named_child_count > 0:
            inner = _recover_elements(
                c, src,
                imports=imports,
                import_regions=import_regions,
                noise_regions=noise_regions,
            )
            if inner:
                return inner
    return out


# --- Composite-adapter entry points ----------------------------------------
#
# Public, stable aliases for the section-walking helpers that the Vue SFC
# adapter (``adapters/vue.py``) reuses to render a ``<template>`` block as
# HTML. They are exported without the leading underscore to declare an
# intentional cross-adapter contract: keep these names and their
# signatures stable, or update ``vue.py`` in lockstep. (Same role the
# public helpers in ``_css_base`` already play for css/scss.)
node_to_decls = _node_to_decls
recover_elements = _recover_elements
collapse_details_runs = _collapse_details_runs
