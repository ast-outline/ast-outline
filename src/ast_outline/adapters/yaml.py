"""YAML adapter (.yaml, .yml).

Produces an outline that mirrors the YAML key hierarchy 1:1 with the
source — every key gets its own line, scalars sit inline next to their
keys, sequences enumerate with the YAML-native ``-`` dash, line ranges
on the right margin.

Example outline of a k8s Deployment manifest::

    # k8s/api-deployment.yaml (38 lines, ~280 tokens, 3 docs — Deployment apps/v1 prod/api-server)
    apiVersion: apps/v1                                    L1
    kind: Deployment                                       L2
    metadata:                                              L3-6
        name: api-server                                   L4
        namespace: prod                                    L5
        labels:                                            L6
            app: api                                       L6
    spec:                                                  L7-38
        replicas: 3                                        L8
        template:                                          L11-38
            spec:                                          L14-38
                containers: (1 item)                       L15-38
                    - api                                  L15-38
                        name: api                          L16
                        image: registry.example.com/...    L17

Design notes
============

- One ``Declaration`` per YAML key (kind ``KIND_YAML_KEY``). Children
  are nested keys; leaf scalars store the value in ``signature``.
- ``qualified_name`` (built by ``find_symbols`` walker) follows
  JSONPath-style: ``spec.template.spec.containers[0].image``. Sequence
  items get ``[i]`` appended to the parent's name, NOT as a separate
  trail step. ``find_symbols`` parses bracket-suffixed parts so this
  natural form works in ``show`` queries.
- Multi-document files surface as a single ``KIND_YAML_DOC`` per doc,
  with the actual YAML body nested as children. Format-detect runs
  per-document so a multi-resource manifest gets per-doc annotations.
- Sequence-of-mappings: each item rendered as ``- <id-key>`` where the
  id-key is the first present of ``name``/``id``/``key``/``uses``/``run``
  (matches Kubernetes / GitHub Actions / Compose conventions). Falls
  back to a bare ``-`` when no id-key is present.
- Sequence-of-scalars is collapsed to a flow-style summary on the
  parent line: ``branches: [main, dev, staging]``.
- Long scalar values get truncated at 60 chars with ``…`` (Unicode
  ellipsis, distinct from YAML's own ``...`` end-of-stream marker).
- Anchors / aliases / Helm ``{{ }}`` template expressions: rendered
  verbatim, no expansion. tree-sitter-yaml treats Helm directives as
  plain text, so the outline structure is preserved around them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import tree_sitter_yaml as ts_yaml
from tree_sitter import Language, Node, Parser

from .base import count_parse_errors
from ..core import (
    KIND_YAML_DOC,
    KIND_YAML_KEY,
    Declaration,
    ParseResult,
)


_LANGUAGE = Language(ts_yaml.language())
_PARSER = Parser(_LANGUAGE)

# Keys whose value (a scalar) we consider as the "identifier" of a
# sequence-of-mappings item. Resolved in priority order — first hit wins.
# `name` covers k8s containers, env vars, ports, services; `uses`/`run`
# cover GitHub Actions steps; `id`/`key` are generic conventions.
# When none of these match, ``_resolve_id_key`` falls through to the
# first scalar pair's value (covers domain-specific identifiers like
# `date` / `event` / `step` / `title` / `country` etc. without us
# needing to enumerate every possible field name globally).
_ID_KEYS = ("name", "id", "key", "uses", "run")

# Long scalars get truncated to this many chars + ellipsis. Keeps the
# outline compact when YAML carries multi-line strings or long URLs as
# values, without losing the leading characters that usually identify
# what the value is.
_SCALAR_TRUNCATE = 60

# Maximum nesting depth we render. Beyond this, deeper subtrees are
# elided with a `...` placeholder. 10 levels covers k8s pod-spec
# (Deployment → spec → template → spec → containers → [i] → env →
# [j] → valueFrom → secretKeyRef → name) and OpenAPI nested schemas
# without truncation. Pathological YAML beyond this is rare enough
# that elision is the right answer.
_MAX_DEPTH = 10


class YamlAdapter:
    language_name = "yaml"
    extensions = {".yaml", ".yml"}
    # Data language — no declaration keywords.
    definition_keywords = frozenset()

    def parse(self, path: Path) -> ParseResult:
        src = path.read_bytes()
        tree = _PARSER.parse(src)
        decls = _walk_stream(tree.root_node, src)
        # Block scalars (``run: |`` shell scripts in CI workflows, Helm
        # values bodies, K8s ConfigMap data:, multi-line text in OpenAPI
        # description: |) are functionally opaque blobs embedded in the
        # YAML — agents grepping for ``npm`` across ``.github/workflows/``
        # don't want every shell line inside a ``run: |`` block as a hit.
        # Mark them as ``"string"`` so the existing noise filter handles
        # them; ``--include-noise`` re-surfaces with ``[string]`` tags.
        # Plain quoted scalars (``image: "registry.example.com/api"``)
        # are intentionally NOT marked — they ARE the data agents
        # search yaml for; filtering them would gut grep's usefulness.
        noise_regions = _collect_noise_regions(tree.root_node)
        return ParseResult(
            path=path,
            language=self.language_name,
            source=src,
            line_count=src.count(b"\n") + 1,
            declarations=decls,
            error_count=count_parse_errors(tree.root_node),
            noise_regions=noise_regions,
        )


def _collect_noise_regions(root: Node) -> list[tuple[int, int, str]]:
    """Return byte ranges of every ``block_scalar`` node — the literal
    ``|`` and folded ``>`` multi-line forms.

    Single-line and quoted scalars are deliberately left visible; only
    the block-style forms (which YAML authors reach for specifically to
    embed opaque multi-line content) get masked. Range covers the
    indicator byte (``|``/``>``) along with the body — the indicator is
    unique punctuation no one greps for, and treating the whole node as
    one range mirrors how the Python adapter masks triple-quote
    delimiters together with their docstring contents.
    """
    out: list[tuple[int, int, str]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "block_scalar":
            out.append((node.start_byte, node.end_byte, "string"))
            continue
        stack.extend(node.children)
    out.sort()
    return out


# --- Walk -----------------------------------------------------------------


def _walk_stream(stream: Node, src: bytes) -> list[Declaration]:
    """Top-level: a `stream` contains one or more `document` nodes.

    Single-document files: skip the doc wrapper entirely so the outline
    looks the same as for a code file with no extra nesting.

    Multi-document files: each doc becomes its own ``KIND_YAML_DOC``
    container so the renderer can drop a separator line between them
    and the format-detect can annotate each doc independently.

    Error-recovery fallback: when tree-sitter rejects the whole stream
    (Helm templates with ``{{ }}`` directives are the prime case —
    parser wraps everything in an ERROR node and never emits a
    ``document``), descend into the error region and pick up any
    well-formed ``block_mapping_pair`` / sequence we can find. The
    outline then covers the structural skeleton around the templated
    bits, which is the bit the agent actually needs.
    """
    docs = [c for c in stream.named_children if c.type == "document"]
    if not docs:
        return _recover_pairs(stream, src)
    if len(docs) == 1:
        return _walk_document_body(docs[0], src)
    out: list[Declaration] = []
    for i, doc in enumerate(docs, start=1):
        body = _walk_document_body(doc, src)
        hint = _format_for_doc(body)
        title = f"--- doc {i} of {len(docs)}"
        if hint:
            title += f" — {hint}"
        out.append(
            Declaration(
                kind=KIND_YAML_DOC,
                name=f"doc{i}",
                signature=title,
                start_line=doc.start_point[0] + 1,
                end_line=_inclusive_end_line(doc),
                start_byte=doc.start_byte,
                end_byte=doc.end_byte,
                doc_start_byte=doc.start_byte,
                children=body,
            )
        )
    return out


def _recover_pairs(node: Node, src: bytes) -> list[Declaration]:
    """Descend into ERROR / unusual-shape parents and collect any
    ``block_mapping_pair`` we find at the top of the recovery point.

    Used when the parse failed badly enough that no ``document`` was
    produced (Helm templates with mustache directives are the canonical
    trigger). We grab pairs at the FIRST level where pairs exist —
    not recursively into deeper pairs, because that would duplicate
    everything when ``_pair_to_decl`` recurses into its own children."""
    out: list[Declaration] = []
    found_at_this_level = False
    for c in node.named_children:
        if c.type == "block_mapping_pair":
            decl = _pair_to_decl(c, src, 0)
            if decl is not None:
                out.append(decl)
                found_at_this_level = True
        elif c.type == "block_mapping":
            out.extend(_walk_mapping(c, src, 0))
            found_at_this_level = True
        elif c.type in ("block_node", "flow_node"):
            out.extend(_walk_node(c, src, 0))
            found_at_this_level = True
    if found_at_this_level:
        return out
    # No pairs at this level — recurse into ERROR / wrapper children
    for c in node.named_children:
        if c.named_child_count > 0:
            inner = _recover_pairs(c, src)
            if inner:
                return inner
    return out


def _walk_document_body(doc: Node, src: bytes) -> list[Declaration]:
    """Walk inside one ``document`` node: skip the literal ``---`` /
    ``...`` tokens, recurse into the contained ``block_node`` /
    ``flow_node``."""
    out: list[Declaration] = []
    for c in doc.named_children:
        if c.type in ("block_node", "flow_node"):
            out.extend(_walk_node(c, src, depth=0))
    return out


def _walk_node(node: Node, src: bytes, depth: int) -> list[Declaration]:
    """Dispatch on the wrapper type. Returns the keys/items at this
    level — the caller stitches them into a parent's children list."""
    # Drill through the ``block_node`` / ``flow_node`` wrappers to
    # find the actual mapping / sequence / scalar.
    inner = _drill_to_container(node)
    if inner is None:
        return []
    t = inner.type
    if t in ("block_mapping", "flow_mapping"):
        return _walk_mapping(inner, src, depth)
    if t in ("block_sequence", "flow_sequence"):
        # Sequence at top level (rare, e.g. an Ansible play list) — render
        # each item as a synthetic `-` declaration.
        return _walk_sequence_items(inner, src, depth)
    return []


def _drill_to_container(node: Node) -> Optional[Node]:
    """``block_node`` and ``flow_node`` are wrappers — descend through
    them (and through any leading ``anchor`` siblings) to the actual
    mapping/sequence/scalar inside."""
    cur = node
    while cur is not None:
        if cur.type in ("block_node", "flow_node"):
            # Skip past anchor / tag children, find the structural inner.
            inner = None
            for c in cur.named_children:
                if c.type in (
                    "block_mapping",
                    "flow_mapping",
                    "block_sequence",
                    "flow_sequence",
                    "plain_scalar",
                    "double_quote_scalar",
                    "single_quote_scalar",
                    "block_scalar",
                    "alias",
                ):
                    inner = c
                    break
            if inner is None:
                return None
            cur = inner
            continue
        return cur
    return None


def _walk_mapping(node: Node, src: bytes, depth: int) -> list[Declaration]:
    out: list[Declaration] = []
    pair_type = "block_mapping_pair" if node.type == "block_mapping" else "flow_pair"
    for pair in node.named_children:
        if pair.type != pair_type:
            continue
        decl = _pair_to_decl(pair, src, depth)
        if decl is not None:
            out.append(decl)
    return out


def _pair_to_decl(pair: Node, src: bytes, depth: int) -> Optional[Declaration]:
    """Convert one ``key : value`` pair into a Declaration.

    Three shapes:
    - Scalar value → ``signature = "key: value"``, no children.
    - Mapping value → ``signature = "key:"``, children = nested keys.
    - Sequence value → either ``signature = "key: [a, b, c]"`` (inline,
      for sequences of scalars) or ``signature = "key: (N items)"``
      with each item as a child (for sequences of mappings).
    """
    key_node = _pair_key_node(pair)
    if key_node is None:
        return None
    key = _scalar_text(key_node, src)
    value_node = _pair_value_node(pair)

    start_line = pair.start_point[0] + 1
    end_line = _inclusive_end_line(pair)

    if value_node is None:
        # `key:` with no value (rare; YAML allows null implicit values)
        return _leaf_decl(key, "", pair, start_line, end_line)

    inner = _drill_to_container(value_node)
    if inner is None:
        return _leaf_decl(key, "", pair, start_line, end_line)

    inner_t = inner.type

    # Scalar leaf
    if inner_t in (
        "plain_scalar",
        "double_quote_scalar",
        "single_quote_scalar",
        "block_scalar",
        "alias",
    ):
        value_text = _scalar_text(inner, src)
        return _leaf_decl(key, value_text, pair, start_line, end_line)

    # Flow-style mapping/sequence value — preserve verbatim inline.
    # If the source uses ``selector: {matchLabels: {app: api}}`` we render
    # exactly that, instead of expanding into a multi-line tree. Faithful
    # to the source, avoids confusing the agent during edits, and keeps
    # the outline compact for files that lean heavily on flow syntax.
    if inner_t in ("flow_mapping", "flow_sequence"):
        flow_text = _scalar_text(inner, src)
        return _leaf_decl(key, flow_text, pair, start_line, end_line)

    # Block mapping value — recurse if depth allows
    if inner_t == "block_mapping":
        if depth + 1 > _MAX_DEPTH:
            return _leaf_decl(key, "(…)", pair, start_line, end_line)
        children = _walk_mapping(inner, src, depth + 1)
        return Declaration(
            kind=KIND_YAML_KEY,
            name=key,
            signature=f"{key}:",
            start_line=start_line,
            end_line=end_line,
            start_byte=pair.start_byte,
            end_byte=pair.end_byte,
            doc_start_byte=pair.start_byte,
            children=children,
        )

    # Block sequence value — sequence-of-scalars collapses inline,
    # sequence-of-mappings enumerates as `- <id-key>` children.
    if inner_t == "block_sequence":
        items = _sequence_items(inner)
        if _is_sequence_of_scalars(inner):
            # Collapse to inline flow-style: `key: [a, b, c]`
            scalars = [_item_inline_text(item, src) for item in items]
            preview = "[" + ", ".join(scalars) + "]"
            return _leaf_decl(key, preview, pair, start_line, end_line)

        if depth + 1 > _MAX_DEPTH:
            return _leaf_decl(key, f"({len(items)} items)", pair, start_line, end_line)
        children: list[Declaration] = []
        for idx, item in enumerate(items):
            child = _seq_item_to_decl(item, src, depth + 1, idx)
            if child is not None:
                children.append(child)
        n = len(items)
        plural = "item" if n == 1 else "items"
        return Declaration(
            kind=KIND_YAML_KEY,
            name=key,
            signature=f"{key}: ({n} {plural})",
            start_line=start_line,
            end_line=end_line,
            start_byte=pair.start_byte,
            end_byte=pair.end_byte,
            doc_start_byte=pair.start_byte,
            children=children,
        )

    return _leaf_decl(key, "", pair, start_line, end_line)


def _walk_sequence_items(seq: Node, src: bytes, depth: int) -> list[Declaration]:
    """Top-level sequence — produce one Declaration per item."""
    out: list[Declaration] = []
    for idx, item in enumerate(_sequence_items(seq)):
        decl = _seq_item_to_decl(item, src, depth, idx)
        if decl is not None:
            out.append(decl)
    return out


def _seq_item_to_decl(item: Node, src: bytes, depth: int, idx: int) -> Optional[Declaration]:
    """Render one ``block_sequence_item`` (or ``flow_node`` inside a
    ``flow_sequence``) as a Declaration.

    The item's ``name`` is the bracket-indexed path component
    (``[0]``, ``[1]``, …) so ``qualified_name`` ends up as
    ``parent[i].sub`` — the JSONPath shape an LLM would write naturally.
    The ``signature`` shows the YAML dash with the resolved id-key:
    ``- api``."""
    inner_node = _seq_item_inner(item)
    inner = _drill_to_container(inner_node) if inner_node is not None else None
    bracket_name = f"[{idx}]"
    start_line = item.start_point[0] + 1
    end_line = _inclusive_end_line(item)

    if inner is None:
        return Declaration(
            kind=KIND_YAML_KEY,
            name=bracket_name,
            signature="-",
            start_line=start_line,
            end_line=end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            doc_start_byte=item.start_byte,
        )

    if inner.type in (
        "plain_scalar",
        "double_quote_scalar",
        "single_quote_scalar",
        "block_scalar",
        "alias",
    ):
        text = _scalar_text(inner, src)
        return Declaration(
            kind=KIND_YAML_KEY,
            name=bracket_name,
            signature=f"- {_truncate(text)}",
            start_line=start_line,
            end_line=end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            doc_start_byte=item.start_byte,
        )

    # Flow-style mapping item — preserve inline like `- {cpu: 100m, ...}`.
    if inner.type == "flow_mapping":
        text = _scalar_text(inner, src)
        return Declaration(
            kind=KIND_YAML_KEY,
            name=bracket_name,
            signature=f"- {_truncate(text)}",
            start_line=start_line,
            end_line=end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            doc_start_byte=item.start_byte,
        )

    if inner.type == "block_mapping":
        # Resolve identifying-key for the dash label.
        id_value = _resolve_id_key(inner, src)
        label = f"- {id_value}" if id_value else "-"
        if depth + 1 > _MAX_DEPTH:
            return Declaration(
                kind=KIND_YAML_KEY,
                name=bracket_name,
                signature=label + " (…)",
                start_line=start_line,
                end_line=end_line,
                start_byte=item.start_byte,
                end_byte=item.end_byte,
                doc_start_byte=item.start_byte,
            )
        children = _walk_mapping(inner, src, depth + 1)
        return Declaration(
            kind=KIND_YAML_KEY,
            name=bracket_name,
            signature=label,
            start_line=start_line,
            end_line=end_line,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            doc_start_byte=item.start_byte,
            children=children,
        )

    return Declaration(
        kind=KIND_YAML_KEY,
        name=bracket_name,
        signature="-",
        start_line=start_line,
        end_line=end_line,
        start_byte=item.start_byte,
        end_byte=item.end_byte,
        doc_start_byte=item.start_byte,
    )


# --- Helpers -------------------------------------------------------------


def _leaf_decl(
    key: str,
    value: str,
    pair: Node,
    start_line: int,
    end_line: int,
) -> Declaration:
    """Scalar-valued mapping pair — store the truncated value inline in
    the signature."""
    if value:
        signature = f"{key}: {_truncate(value)}"
    else:
        signature = f"{key}:"
    return Declaration(
        kind=KIND_YAML_KEY,
        name=key,
        signature=signature,
        start_line=start_line,
        end_line=end_line,
        start_byte=pair.start_byte,
        end_byte=pair.end_byte,
        doc_start_byte=pair.start_byte,
    )


def _pair_key_node(pair: Node) -> Optional[Node]:
    """The first ``flow_node`` child of a mapping_pair is the key."""
    for c in pair.named_children:
        if c.type == "flow_node":
            return c
    return None


def _pair_value_node(pair: Node) -> Optional[Node]:
    """The value is the LAST ``block_node`` or ``flow_node`` after the
    ``:`` token. We walk children, find the colon, take the next
    structural sibling."""
    seen_colon = False
    for c in pair.children:
        if c.type == ":":
            seen_colon = True
            continue
        if seen_colon and c.type in ("block_node", "flow_node"):
            return c
    return None


def _sequence_items(seq: Node) -> list[Node]:
    """Items of a sequence node, regardless of block/flow style."""
    if seq.type == "block_sequence":
        return [c for c in seq.named_children if c.type == "block_sequence_item"]
    if seq.type == "flow_sequence":
        return [c for c in seq.named_children if c.type == "flow_node"]
    return []


def _seq_item_inner(item: Node) -> Optional[Node]:
    """``block_sequence_item`` wraps the value in a ``block_node`` /
    ``flow_node``; ``flow_sequence`` items ARE the flow_node themselves."""
    if item.type == "block_sequence_item":
        for c in item.named_children:
            if c.type in ("block_node", "flow_node"):
                return c
        return None
    return item  # flow_node itself


def _is_sequence_of_scalars(seq: Node) -> bool:
    """True when every item resolves to a scalar (plain/quoted/block).
    Mixed sequences and sequences-of-mappings return False."""
    items = _sequence_items(seq)
    if not items:
        return True  # empty → render inline as `[]`
    for item in items:
        inner_node = _seq_item_inner(item)
        inner = _drill_to_container(inner_node) if inner_node is not None else None
        if inner is None:
            return False
        if inner.type not in (
            "plain_scalar",
            "double_quote_scalar",
            "single_quote_scalar",
            "block_scalar",
            "alias",
        ):
            return False
    return True


def _resolve_id_key(mapping: Node, src: bytes) -> Optional[str]:
    """Find the value of the first id-priority key in this mapping.

    Walks the immediate pairs (not nested) — for `containers: [- name:
    api ...]` we want the `api` from the OUTER `name:` pair.

    Two-tier resolution:
    1. Priority chain (`name`/`id`/`key`/`uses`/`run`) — established
       conventions across k8s, GitHub Actions, Compose, generic data.
    2. Fallback — first scalar pair's value, in source order. Covers
       domain-specific identifier keys we can't enumerate universally
       (`date` / `event` / `step` / `title` / `dimension` / domain
       terms). Better to label the dash with `2024-01-15` than leave
       it bare — gives the agent something to scan visually.
    """
    pairs = [c for c in mapping.named_children if c.type in ("block_mapping_pair", "flow_pair")]
    by_key: dict[str, str] = {}
    for p in pairs:
        k_node = _pair_key_node(p)
        v_node = _pair_value_node(p)
        if k_node is None or v_node is None:
            continue
        v_inner = _drill_to_container(v_node)
        if v_inner is None:
            continue
        if v_inner.type in (
            "plain_scalar",
            "double_quote_scalar",
            "single_quote_scalar",
        ):
            key_text = _scalar_text(k_node, src)
            by_key[key_text] = _scalar_text(v_inner, src)
    for candidate in _ID_KEYS:
        if candidate in by_key:
            return _truncate(by_key[candidate])
    # Fallback: first scalar pair in source order. ``by_key`` preserved
    # insertion order (Python 3.7+), so the first inserted key is the
    # first scalar pair encountered in the YAML.
    if by_key:
        first_value = next(iter(by_key.values()))
        return _truncate(first_value)
    return None


def _scalar_text(node: Node, src: bytes) -> str:
    """Decoded text of a scalar node, with surrounding quotes preserved
    so the agent can tell `null` (plain) from `"null"` (string)."""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _item_inline_text(item: Node, src: bytes) -> str:
    """For inline-collapsed sequences-of-scalars: just the scalar text."""
    inner_node = _seq_item_inner(item)
    inner = _drill_to_container(inner_node) if inner_node is not None else None
    if inner is None:
        return ""
    return _scalar_text(inner, src)


def _truncate(s: str, limit: int = _SCALAR_TRUNCATE) -> str:
    """Truncate a long scalar value with a Unicode ellipsis. Single-line
    only — newlines collapse to spaces first so a multi-line block scalar
    doesn't break the outline layout."""
    flat = s.replace("\n", " ").strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _inclusive_end_line(node: Node) -> int:
    """tree-sitter end_point is exclusive — column 0 of the line AFTER
    the node's last actual line. Convert to our 1-based inclusive line."""
    end_row, end_col = node.end_point
    if end_col == 0 and end_row > node.start_point[0]:
        return end_row
    return end_row + 1


# --- Format detection ----------------------------------------------------


def _format_for_doc(decls: list[Declaration]) -> Optional[str]:
    """Return a one-line annotation describing what kind of YAML doc
    this is, or ``None`` for generic configs.

    Strict three-format scope (k8s / OpenAPI / GitHub Actions) — these
    cover the YAML files where outlines actually pay off and where the
    annotation gives the agent a useful triage signal. We do NOT detect
    Ansible / dbt / docker-compose / kustomize: those files are usually
    short enough that the annotation overhead doesn't earn its keep.
    """
    top: dict[str, str] = {}
    top_keys: set[str] = set()
    for d in decls:
        top_keys.add(d.name)
        # Capture the inline scalar value (if any) from the signature
        if d.signature and d.signature.startswith(d.name + ": "):
            top[d.name] = d.signature[len(d.name) + 2 :].strip()

    # Kubernetes — `apiVersion:` + `kind:` is the classic combo.
    if "apiVersion" in top_keys and "kind" in top_keys:
        kind = top.get("kind", "?")
        api_version = top.get("apiVersion", "?")
        ns_name = _k8s_namespace_name(decls)
        if ns_name:
            return f"{kind} {api_version} {ns_name}"
        return f"{kind} {api_version}"

    # OpenAPI — `openapi:` + `paths:` (must have both to disambiguate from
    # other YAMLs that happen to mention "openapi" somewhere).
    if "openapi" in top_keys and "paths" in top_keys:
        version = top.get("openapi", "?")
        n_paths = _count_children_named(decls, "paths")
        n_schemas = _count_components_schemas(decls)
        out = f"OpenAPI {version}, {n_paths} paths"
        if n_schemas is not None:
            out += f", {n_schemas} schemas"
        return out

    # GitHub Actions — `jobs:` + `on:` together, optionally with `name:`.
    # `on:` alone is too weak (could be many things).
    if "jobs" in top_keys and "on" in top_keys:
        n_jobs = _count_children_named(decls, "jobs")
        return f"GitHub Actions, {n_jobs} jobs"

    return None


def _k8s_namespace_name(decls: list[Declaration]) -> Optional[str]:
    """For k8s manifests, surface ``namespace/name`` from `metadata`."""
    metadata = next((d for d in decls if d.name == "metadata"), None)
    if metadata is None:
        return None
    name = _child_scalar(metadata, "name")
    namespace = _child_scalar(metadata, "namespace")
    if name and namespace:
        return f"{namespace}/{name}"
    if name:
        return name
    return None


def _child_scalar(decl: Declaration, key: str) -> Optional[str]:
    for c in decl.children:
        if c.name == key:
            sig = c.signature
            if sig.startswith(key + ": "):
                return sig[len(key) + 2 :].strip()
    return None


def _count_children_named(decls: list[Declaration], parent_name: str) -> int:
    """Count direct children of the named top-level key."""
    parent = next((d for d in decls if d.name == parent_name), None)
    if parent is None:
        return 0
    return len(parent.children)


def _count_components_schemas(decls: list[Declaration]) -> Optional[int]:
    """OpenAPI: ``components.schemas`` is the canonical schema container.
    Returns None if the file has no ``components`` block."""
    components = next((d for d in decls if d.name == "components"), None)
    if components is None:
        return None
    schemas = next((c for c in components.children if c.name == "schemas"), None)
    if schemas is None:
        return 0
    return len(schemas.children)
