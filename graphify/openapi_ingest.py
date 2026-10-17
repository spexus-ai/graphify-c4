"""Local OpenAPI YAML document discovery and structural reference evidence.

Only an explicit OpenAPI/Swagger root and its scanned local $ref closure are
promoted from prose to code. No URL is fetched and ignored/sensitive files
cannot enter through a reference. Scalar payload values are never indexed.
"""

from pathlib import Path
from urllib.parse import unquote, urlsplit
from graphify.extractors.base import _make_id, _file_stem

MAX_BYTES = 2 * 1024 * 1024


def _read(path):
    import yaml

    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return None
        return yaml.compose(data, Loader=yaml.SafeLoader)
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _pairs(node):
    from yaml.nodes import MappingNode, ScalarNode

    if isinstance(node, MappingNode):
        return [(key.value, value) for key, value in node.value if isinstance(key, ScalarNode)]
    return []


def _refs(node):
    from yaml.nodes import MappingNode, SequenceNode, ScalarNode

    seen = set()
    pending = [node]
    while pending and len(seen) < 50000:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, MappingNode):
            for key, value in _pairs(current):
                if key == "$ref" and isinstance(value, ScalarNode):
                    yield value.value, value.start_mark.line + 1
                pending.append(value)
        elif isinstance(current, SequenceNode):
            pending.extend(current.value)


def _local_target(path, ref):
    try:
        parts = urlsplit(ref)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or not parts.path:
        return None
    return (Path(path).parent / unquote(parts.path)).resolve()


def discover_openapi_yaml(paths):
    """Return roots and referenced YAML among the already allowed scan files."""
    from yaml.nodes import ScalarNode

    allowed = {
        Path(path).resolve(): str(path)
        for path in paths
        if Path(path).suffix.lower() in {".yaml", ".yml"}
    }
    parsed = {path: _read(path) for path in allowed}
    roots = []
    for path, node in parsed.items():
        fields = dict(_pairs(node))
        version = fields.get("openapi")
        swagger = fields.get("swagger")
        if (isinstance(version, ScalarNode) and version.value.startswith("3.")) or (
            isinstance(swagger, ScalarNode) and swagger.value == "2.0"
        ):
            roots.append(path)
    # Include reverse reference participants as well. A detached path fragment
    # can point INTO the root (shared security/parameters) without being
    # reachable FROM it. Keeping it lets reachability audits find dormant API
    # definitions instead of silently dropping them from coverage.
    neighbors = {p: set() for p in parsed}
    for path, node in parsed.items():
        for ref, _ in _refs(node):
            target = _local_target(path, ref)
            if target in parsed and parsed[target] is not None:
                neighbors[path].add(target)
                neighbors[target].add(path)
    found = set()
    while roots:
        path = roots.pop()
        if path in found:
            continue
        found.add(path)
        roots.extend(neighbors[path] - found)
    return {allowed[path] for path in found}


def extract_openapi_yaml(path):
    """A contract document root retains local $ref locations as evidence."""
    node = _read(path)
    if node is None:
        return {"nodes": [], "edges": [], "error": "invalid or oversized YAML"}
    refs = [{"ref": ref, "line": line} for ref, line in _refs(node)]
    return {
        "nodes": [
            {
                "id": _make_id(_file_stem(path)),
                "label": path.name,
                "file_type": "code",
                "source_file": str(path),
                "source_location": "L1",
                "metadata": {"kind": "openapi_document", "language": "yaml", "local_refs": refs},
            }
        ],
        "edges": [],
    }


def resolve_openapi_references(nodes, edges, root):
    """Connect only extracted documents: missing/external refs stay unresolved."""

    def absolute(source):
        p = Path(source)
        return p.resolve() if p.is_absolute() else (root / p).resolve()

    docs = {
        absolute(n["source_file"]): n
        for n in nodes
        if n.get("metadata", {}).get("kind") == "openapi_document"
    }
    for path, source in docs.items():
        for ref in source["metadata"]["local_refs"]:
            target = docs.get(_local_target(path, ref["ref"]))
            if target is None or target["id"] == source["id"]:
                continue
            edges.append(
                {
                    "source": source["id"],
                    "target": target["id"],
                    "relation": "references",
                    "confidence": "EXTRACTED",
                    "resolution": "openapi_local_ref",
                    "source_file": source["source_file"],
                    "source_location": "L" + str(ref["line"]),
                    "context": "$ref",
                    "weight": 1.0,
                }
            )
