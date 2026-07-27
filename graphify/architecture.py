"""C4 architecture projection and conformance checks for Graphify.

The generic Graphify graph is deliberately a graph of observed facts: symbols,
files and their relationships.  This module adds a separate, versioned C4
projection.  Keeping it separate means architecture declarations never pollute
generic queries, community detection, or the on-disk ``graph.json`` contract.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from fnmatch import fnmatch
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from graphify.paths import write_json_atomic, write_text_atomic


SCHEMA = "graphify.architecture/v1"
DIFF_SCHEMA = "graphify.architecture-diff/v1"
C4_TYPES = {
    "person",
    "software_system",
    "external_system",
    "container",
    "component",
    "code",
    "datastore",
}
LEVELS = ("context", "container", "component", "code")
LEVEL_TYPES = {
    "context": {"person", "software_system", "external_system"},
    "container": {"container", "datastore"},
    "component": {"component"},
    "code": {"code"},
}
TRUSTED_RESOLUTIONS = frozenset({"same_file", "same_go_package", "go_import", "go_import_type"})


def default_model() -> dict[str, Any]:
    """Return an intentionally small, valid starting point for a C4 contract."""
    return {
        "schema": SCHEMA,
        "scope": {"include": ["**"], "exclude": ["graphify-out/**"]},
        "elements": [
            {
                "id": "system",
                "c4_type": "software_system",
                "name": "System",
                "source": "declared",
            }
        ],
        "relations": [],
        "rules": [],
    }


def load_model(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"architecture model not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"architecture model is not valid JSON: {path}: {exc}") from exc
    errors = validate_model(data)
    if errors:
        raise ValueError("architecture model has errors:\n" + "\n".join(f"  - {e}" for e in errors))
    return data


def validate_model(model: object) -> list[str]:
    """Validate the declarative C4 model without depending on a graph build."""
    if not isinstance(model, dict):
        return ["model must be a JSON object"]
    errors: list[str] = []
    if model.get("schema") != SCHEMA:
        errors.append(f"schema must be '{SCHEMA}'")

    elements = model.get("elements")
    if not isinstance(elements, list) or not elements:
        errors.append("elements must be a non-empty list")
        elements = []
    ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            errors.append(f"element {index} must be an object")
            continue
        element_id = element.get("id")
        if not isinstance(element_id, str) or not element_id:
            errors.append(f"element {index} has invalid id")
            continue
        if element_id in ids:
            errors.append(f"duplicate element id '{element_id}'")
        ids.add(element_id)
        by_id[element_id] = element
        if element.get("c4_type") not in C4_TYPES:
            errors.append(f"element '{element_id}' has invalid c4_type '{element.get('c4_type')}'")
        if not isinstance(element.get("name"), str) or not element["name"].strip():
            errors.append(f"element '{element_id}' needs a non-empty name")
        implementation = element.get("implementation", [])
        if not isinstance(implementation, list):
            errors.append(f"element '{element_id}' implementation must be a list")
        else:
            for selector_index, selector in enumerate(implementation):
                if not isinstance(selector, dict):
                    errors.append(f"element '{element_id}' implementation {selector_index} must be an object")
                elif not any(key in selector for key in ("path_prefix", "source_file", "node_id", "label")):
                    errors.append(
                        f"element '{element_id}' implementation {selector_index} needs "
                        "path_prefix, source_file, node_id, or label"
                    )

    for element_id, element in by_id.items():
        parent = element.get("parent")
        if parent is not None and parent not in ids:
            errors.append(f"element '{element_id}' parent '{parent}' does not exist")
        if parent == element_id:
            errors.append(f"element '{element_id}' cannot be its own parent")

    for element_id in by_id:
        current: str | None = element_id
        chain: set[str] = set()
        while current is not None and current in by_id:
            if current in chain:
                errors.append(f"parent cycle involving '{element_id}'")
                break
            chain.add(current)
            parent = by_id[current].get("parent")
            current = parent if isinstance(parent, str) else None

    relations = model.get("relations", [])
    if not isinstance(relations, list):
        errors.append("relations must be a list")
        relations = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            errors.append(f"relation {index} must be an object")
            continue
        for field in ("source", "target", "kind"):
            if not isinstance(relation.get(field), str) or not relation[field]:
                errors.append(f"relation {index} needs a non-empty {field}")
        for endpoint in ("source", "target"):
            if isinstance(relation.get(endpoint), str) and relation[endpoint] not in ids:
                errors.append(f"relation {index} {endpoint} '{relation[endpoint]}' does not exist")

    rules = model.get("rules", [])
    if not isinstance(rules, list):
        errors.append("rules must be a list")
        rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or not isinstance(rule.get("deny"), dict):
            errors.append(f"rule {index} must contain a deny object")
            continue
        deny = rule["deny"]
        if not isinstance(deny.get("source"), str) or deny["source"] not in ids:
            errors.append(f"rule {index} deny.source must reference an element")
        target = deny.get("target")
        target_type = deny.get("target_type")
        if target is None and target_type is None:
            errors.append(f"rule {index} needs deny.target or deny.target_type")
        if target is not None and (not isinstance(target, str) or target not in ids):
            errors.append(f"rule {index} deny.target must reference an element")
        if target_type is not None and target_type not in C4_TYPES:
            errors.append(f"rule {index} deny.target_type is invalid")
    return errors


def _links(raw: dict[str, Any]) -> list[dict[str, Any]]:
    links = raw.get("links") if "links" in raw else raw.get("edges", [])
    return [link for link in links if isinstance(link, dict)] if isinstance(links, list) else []


def _normalise_path(value: object) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def _in_scope(path: str, scope: dict[str, Any]) -> bool:
    include = scope.get("include", ["**"])
    exclude = scope.get("exclude", [])
    if not isinstance(include, list):
        include = ["**"]
    if not isinstance(exclude, list):
        exclude = []
    matched = any(fnmatch(path, str(pattern)) for pattern in include)
    return matched and not any(fnmatch(path, str(pattern)) for pattern in exclude)


def _matches_selector(node_id: str, node: dict[str, Any], selector: dict[str, Any]) -> bool:
    source_file = _normalise_path(node.get("source_file"))
    if "node_id" in selector and node_id != selector["node_id"]:
        return False
    if "source_file" in selector and source_file != _normalise_path(selector["source_file"]):
        return False
    if "path_prefix" in selector:
        prefix = _normalise_path(selector["path_prefix"]).rstrip("/")
        if not (source_file == prefix or source_file.startswith(prefix + "/")):
            return False
    if "label" in selector and str(node.get("label", "")) != str(selector["label"]):
        return False
    return True


def _ancestor(element_id: str, elements: dict[str, dict[str, Any]], types: set[str]) -> str | None:
    current: str | None = element_id
    visited: set[str] = set()
    while current is not None and current not in visited:
        visited.add(current)
        element = elements.get(current)
        if element is None:
            return None
        if element.get("c4_type") in types:
            return current
        parent = element.get("parent")
        current = parent if isinstance(parent, str) else None
    return None


def _is_descendant(candidate: str, parent: str, elements: dict[str, dict[str, Any]]) -> bool:
    current: str | None = candidate
    visited: set[str] = set()
    while current is not None and current not in visited:
        if current == parent:
            return True
        visited.add(current)
        element = elements.get(current, {})
        next_parent = element.get("parent")
        current = next_parent if isinstance(next_parent, str) else None
    return False


def _code_element_id(node_id: str) -> str:
    """Keep generated code IDs distinct from declared C4 element IDs."""
    return f"code:{node_id}"


def build_projection(model: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """Map observed graph nodes and relationships onto a declared C4 model."""
    elements_list = model["elements"]
    elements = {element["id"]: element for element in elements_list}
    scoped_nodes = {
        str(node["id"]): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
        and isinstance(node.get("id"), str)
        and _in_scope(_normalise_path(node.get("source_file")), model.get("scope", {}))
    }
    mappings: dict[str, list[str]] = defaultdict(list)
    for element in elements_list:
        for selector in element.get("implementation", []):
            if not isinstance(selector, dict):
                continue
            for node_id, node in scoped_nodes.items():
                if _matches_selector(node_id, node, selector) and element["id"] not in mappings[node_id]:
                    mappings[node_id].append(element["id"])

    ambiguous = [node_id for node_id, mapped in mappings.items() if len(mapped) > 1]
    mapped_nodes = {node_id for node_id, mapped in mappings.items() if len(mapped) == 1}
    production_nodes = {
        node_id
        for node_id, node in scoped_nodes.items()
        if node.get("file_type") == "code"
    }
    unmapped = sorted(production_nodes - mapped_nodes)

    code_node_ids = {
        node_id: _code_element_id(node_id)
        for node_id in sorted(production_nodes & mapped_nodes)
    }
    code_elements = [
        {
            "id": code_node_ids[node_id],
            "c4_type": "code",
            "parent": mappings[node_id][0],
            "name": str(node.get("label") or node_id),
            "source": "observed",
            "source_file": _normalise_path(node.get("source_file")),
            "graph_node_id": node_id,
        }
        for node_id, node in sorted(scoped_nodes.items())
        if node_id in code_node_ids
    ]

    observed: dict[tuple[str, str, str], dict[str, Any]] = {}
    observed_code: dict[tuple[str, str, str], dict[str, Any]] = {}
    for link in _links(graph):
        source = str(link.get("_src") or link.get("source") or "")
        target = str(link.get("_tgt") or link.get("target") or "")
        source_mapping = mappings.get(source, [])
        target_mapping = mappings.get(target, [])
        if len(source_mapping) != 1 or len(target_mapping) != 1:
            continue
        source_component, target_component = source_mapping[0], target_mapping[0]
        relation = str(link.get("relation") or "uses")
        evidence = {
            "source_node": source,
            "target_node": target,
            "source_file": link.get("source_file", ""),
            "source_location": link.get("source_location", ""),
            "confidence": link.get("confidence", "EXTRACTED"),
            "resolution": link.get("resolution", "unknown"),
        }
        if source in code_node_ids and target in code_node_ids:
            code_key = (code_node_ids[source], code_node_ids[target], relation)
            code_item = observed_code.setdefault(
                code_key,
                {
                    "source": code_node_ids[source],
                    "target": code_node_ids[target],
                    "kind": relation,
                    "evidence": [],
                    "confidence": Counter(),
                },
            )
            code_item["evidence"].append(evidence)
            code_item["confidence"][str(evidence["confidence"])] += 1

        if source_component == target_component:
            continue
        key = (source_component, target_component, relation)
        item = observed.setdefault(
            key,
            {
                "source": source_component,
                "target": target_component,
                "kind": relation,
                "evidence": [],
                "confidence": Counter(),
            },
        )
        item["evidence"].append(evidence)
        item["confidence"][str(evidence["confidence"])] += 1

    observed_relations = []
    for item in observed.values():
        item["evidence"].sort(key=lambda evidence: (
            str(evidence["source_file"]), str(evidence["source_location"]), str(evidence["source_node"])
        ))
        item["confidence"] = dict(sorted(item["confidence"].items()))
        observed_relations.append(item)
    observed_relations.sort(key=lambda relation: (relation["source"], relation["target"], relation["kind"]))

    observed_code_relations = []
    for item in observed_code.values():
        item["evidence"].sort(key=lambda evidence: (
            str(evidence["source_file"]), str(evidence["source_location"]), str(evidence["source_node"])
        ))
        item["confidence"] = dict(sorted(item["confidence"].items()))
        observed_code_relations.append(item)
    observed_code_relations.sort(key=lambda relation: (relation["source"], relation["target"], relation["kind"]))

    return {
        "schema": SCHEMA,
        "elements": elements_list + code_elements,
        "declared_relations": model.get("relations", []),
        "rules": model.get("rules", []),
        "mappings": {node_id: mapped for node_id, mapped in sorted(mappings.items())},
        "code_node_ids": code_node_ids,
        "unmapped_code_nodes": unmapped,
        "ambiguous_code_nodes": sorted(ambiguous),
        "observed_relations": observed_relations,
        "observed_code_relations": observed_code_relations,
    }


def _declared_covers(
    observed: dict[str, Any],
    declared: dict[str, Any],
    elements: dict[str, dict[str, Any]],
) -> bool:
    source = str(declared.get("source", ""))
    target = str(declared.get("target", ""))
    return _is_descendant(str(observed["source"]), source, elements) and _is_descendant(
        str(observed["target"]), target, elements
    )


def conformance(projection: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic architecture findings; only hard facts are errors."""
    elements = {element["id"]: element for element in projection["elements"]}
    findings: list[dict[str, Any]] = []
    for node_id in projection["ambiguous_code_nodes"]:
        findings.append({"severity": "error", "kind": "ambiguous_mapping", "node": node_id})
    for node_id in projection["unmapped_code_nodes"]:
        findings.append({"severity": "warning", "kind": "unmapped_code", "node": node_id})

    declared_relations = projection.get("declared_relations", [])
    for observed in projection["observed_relations"]:
        for rule in projection.get("rules", []):
            deny = rule.get("deny", {}) if isinstance(rule, dict) else {}
            source_matches = _is_descendant(str(observed["source"]), str(deny.get("source", "")), elements)
            target_matches = (
                "target" in deny
                and _is_descendant(str(observed["target"]), str(deny["target"]), elements)
            ) or (
                "target_type" in deny
                and elements.get(str(observed["target"]), {}).get("c4_type") == deny["target_type"]
            )
            if source_matches and target_matches:
                findings.append(
                    {
                        "severity": "error",
                        "kind": "forbidden_dependency",
                        "rule": rule.get("id", "unnamed-rule"),
                        "source": observed["source"],
                        "target": observed["target"],
                        "evidence": observed["evidence"],
                    }
                )
        if not any(_declared_covers(observed, declared, elements) for declared in declared_relations):
            findings.append(
                {
                    "severity": "warning",
                    "kind": "undeclared_dependency",
                    "source": observed["source"],
                    "target": observed["target"],
                    "relation": observed["kind"],
                    "evidence": observed["evidence"],
                }
            )
    return findings


def suspect_dependencies(projection: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cross-component facts that lack a namespace/import proof.

    The result is deliberately an audit queue, not an architecture violation:
    older graphs and language extractors may not yet emit resolution provenance.
    """
    mappings = projection.get("mappings", {})
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for link in _links(graph):
        source_node = str(link.get("_src") or link.get("source") or "")
        target_node = str(link.get("_tgt") or link.get("target") or "")
        source_mapping = mappings.get(source_node, [])
        target_mapping = mappings.get(target_node, [])
        if len(source_mapping) != 1 or len(target_mapping) != 1:
            continue
        source, target = source_mapping[0], target_mapping[0]
        if source == target:
            continue
        confidence = str(link.get("confidence", "EXTRACTED"))
        resolution = str(link.get("resolution", "unknown"))
        if confidence == "EXTRACTED" and resolution in TRUSTED_RESOLUTIONS:
            continue
        finding = grouped.setdefault(
            (source, target),
            {
                "severity": "warning",
                "kind": "suspect_dependency",
                "source": source,
                "target": target,
                "evidence_count": 0,
                "reasons": set(),
                "samples": [],
            },
        )
        finding["evidence_count"] += 1
        finding["reasons"].add(
            (str(link.get("relation") or "uses"), confidence, resolution)
        )
        if len(finding["samples"]) < 3:
            finding["samples"].append(
                {
                    "source_node": source_node,
                    "target_node": target_node,
                    "source_file": link.get("source_file", ""),
                    "source_location": link.get("source_location", ""),
                }
            )
    findings = []
    for finding in grouped.values():
        finding["reasons"] = [
            {"relation": relation, "confidence": confidence, "resolution": resolution}
            for relation, confidence, resolution in sorted(finding["reasons"])
        ]
        findings.append(finding)
    return sorted(
        findings,
        key=lambda item: (item["source"], item["target"]),
    )


def _element_index(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {element["id"]: element for element in projection["elements"]}


def view(projection: dict[str, Any], level: str, focus: str | None = None) -> dict[str, Any]:
    if level not in LEVEL_TYPES:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    elements = _element_index(projection)
    selected = []
    for element_id, element in elements.items():
        if element.get("c4_type") not in LEVEL_TYPES[level]:
            continue
        if focus is not None and not _is_descendant(element_id, focus, elements):
            continue
        selected.append(element)
    selected_ids = {element["id"] for element in selected}

    rolled: dict[tuple[str, str, str], dict[str, Any]] = {}
    relations = projection.get("observed_code_relations", []) if level == "code" else projection["observed_relations"]
    for relation in relations:
        source = _ancestor(relation["source"], elements, LEVEL_TYPES[level])
        target = _ancestor(relation["target"], elements, LEVEL_TYPES[level])
        if source is None or target is None or source == target:
            continue
        if source not in selected_ids or target not in selected_ids:
            continue
        key = (source, target, relation["kind"])
        item = rolled.setdefault(
            key,
            {"source": source, "target": target, "kind": relation["kind"], "evidence_count": 0},
        )
        item["evidence_count"] += len(relation["evidence"])
    return {
        "level": level,
        "focus": focus,
        "elements": sorted(selected, key=lambda item: item["id"]),
        "observed_relations": sorted(rolled.values(), key=lambda item: (item["source"], item["target"], item["kind"])),
    }


def load_projection(path: Path) -> dict[str, Any]:
    """Load a previously synced C4 projection for historical comparison."""
    try:
        projection = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"architecture projection not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"architecture projection is not valid JSON: {path}: {exc}") from exc
    if not isinstance(projection, dict) or projection.get("schema") != SCHEMA:
        raise ValueError(f"architecture projection has unsupported schema: {path}")
    if not isinstance(projection.get("elements"), list):
        raise ValueError(f"architecture projection has no elements array: {path}")
    return projection


def _element_fingerprint(element: dict[str, Any]) -> str:
    """Stable structural identity used to detect a changed, non-replaced element."""
    fields = ("c4_type", "parent", "name", "description", "source_file", "graph_node_id")
    return json.dumps({field: element.get(field) for field in fields}, sort_keys=True, ensure_ascii=False)


def _rolled_diff_relations(projection: dict[str, Any], level: str) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Roll one snapshot to a C4 level before comparing it with another snapshot."""
    elements = _element_index(projection)
    source_relations = projection.get("observed_code_relations", []) if level == "code" else projection.get("observed_relations", [])
    all_relations = [(relation, "observed") for relation in source_relations]
    if level != "code":
        all_relations.extend((relation, "declared") for relation in projection.get("declared_relations", []))
    rolled: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for relation, origin in all_relations:
        source = _ancestor(str(relation.get("source", "")), elements, LEVEL_TYPES[level])
        target = _ancestor(str(relation.get("target", "")), elements, LEVEL_TYPES[level])
        if source is None or target is None or source == target:
            continue
        key = (source, target, str(relation.get("kind", "uses")), origin)
        item = rolled.setdefault(
            key,
            {"source": source, "target": target, "kind": key[2], "origin": origin, "evidence_count": 0},
        )
        item["evidence_count"] += len(relation.get("evidence", [])) if origin == "observed" else 1
    return rolled


def _code_change_statuses(before: dict[str, Any], after: dict[str, Any]) -> dict[str, str]:
    before_code = {key: value for key, value in _element_index(before).items() if value.get("c4_type") == "code"}
    after_code = {key: value for key, value in _element_index(after).items() if value.get("c4_type") == "code"}
    statuses: dict[str, str] = {}
    for element_id in sorted(set(before_code) | set(after_code)):
        if element_id not in before_code:
            statuses[element_id] = "added"
        elif element_id not in after_code:
            statuses[element_id] = "removed"
        elif _element_fingerprint(before_code[element_id]) != _element_fingerprint(after_code[element_id]):
            statuses[element_id] = "modified"
        else:
            statuses[element_id] = "unchanged"
    return statuses


def architecture_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare two C4 projections at every C4 level without mixing roll-up stages."""
    before_elements, after_elements = _element_index(before), _element_index(after)
    code_status = _code_change_statuses(before, after)
    descendants: dict[str, Counter[str]] = defaultdict(Counter)
    for projection, status_source in ((before, "removed"), (after, "added")):
        elements = _element_index(projection)
        for code_id, status in code_status.items():
            if status != status_source or code_id not in elements:
                continue
            current: str | None = code_id
            while current is not None:
                descendants[current][status] += 1
                parent = elements.get(current, {}).get("parent")
                current = parent if isinstance(parent, str) else None
    for code_id, status in code_status.items():
        if status != "modified":
            continue
        current: str | None = code_id
        while current is not None:
            descendants[current][status] += 1
            parent = after_elements.get(current, {}).get("parent")
            current = parent if isinstance(parent, str) else None

    levels: dict[str, dict[str, Any]] = {}
    for level in LEVELS:
        selected_before = {key: value for key, value in before_elements.items() if value.get("c4_type") in LEVEL_TYPES[level]}
        selected_after = {key: value for key, value in after_elements.items() if value.get("c4_type") in LEVEL_TYPES[level]}
        elements: list[dict[str, Any]] = []
        for element_id in sorted(set(selected_before) | set(selected_after)):
            old, new = selected_before.get(element_id), selected_after.get(element_id)
            if old is None:
                direct_status = "added"
            elif new is None:
                direct_status = "removed"
            elif _element_fingerprint(old) != _element_fingerprint(new):
                direct_status = "modified"
            else:
                direct_status = "unchanged"
            descendant = {state: descendants[element_id][state] for state in ("added", "removed", "modified")}
            effective_status = direct_status
            if effective_status == "unchanged" and any(descendant.values()):
                # A stable C4 element with changing implementation is modified,
                # never added or removed.  Direct status alone conveys lifecycle.
                effective_status = "modified"
            element = new or old or {}
            elements.append({
                "id": element_id,
                "name": element.get("name", element_id),
                "c4_type": element.get("c4_type", "unknown"),
                "parent": element.get("parent"),
                "status": effective_status,
                "direct_status": direct_status,
                "descendant_delta": descendant,
                "source_file": element.get("source_file", ""),
            })

        before_relations, after_relations = _rolled_diff_relations(before, level), _rolled_diff_relations(after, level)
        relation_rows: list[dict[str, Any]] = []
        for key in sorted(set(before_relations) | set(after_relations)):
            old, new = before_relations.get(key), after_relations.get(key)
            if old is None:
                status = "added"
            elif new is None:
                status = "removed"
            elif old["evidence_count"] != new["evidence_count"]:
                status = "modified"
            else:
                status = "unchanged"
            relation = new or old or {}
            relation_rows.append({
                **relation,
                "status": status,
                "before_evidence_count": old["evidence_count"] if old else 0,
                "after_evidence_count": new["evidence_count"] if new else 0,
            })
        levels[level] = {"level": level, "elements": elements, "relations": relation_rows}

    return {
        "schema": DIFF_SCHEMA,
        "before": {"elements": len(before_elements), "unmapped_code_nodes": len(before.get("unmapped_code_nodes", []))},
        "after": {"elements": len(after_elements), "unmapped_code_nodes": len(after.get("unmapped_code_nodes", []))},
        "levels": levels,
    }


def resolve_architecture_node(projection: dict[str, Any], value: str) -> str | None:
    elements = _element_index(projection)
    if value in elements:
        return value
    code_node_id = projection.get("code_node_ids", {}).get(value)
    if isinstance(code_node_id, str) and code_node_id in elements:
        return code_node_id
    mapping = projection.get("mappings", {}).get(value, [])
    if len(mapping) == 1:
        return mapping[0]
    matches = [element_id for element_id, element in elements.items() if element.get("name") == value]
    return matches[0] if len(matches) == 1 else None


def up(projection: dict[str, Any], value: str) -> list[dict[str, Any]]:
    elements = _element_index(projection)
    element_id = resolve_architecture_node(projection, value)
    if element_id is None:
        raise ValueError(f"no unique architecture node for '{value}'")
    result: list[dict[str, Any]] = []
    current: str | None = element_id
    while current is not None:
        element = elements[current]
        result.append(element)
        parent = element.get("parent")
        current = parent if isinstance(parent, str) else None
    return result


def down(projection: dict[str, Any], value: str, target: str) -> dict[str, Any]:
    if target not in LEVEL_TYPES:
        raise ValueError(f"target must be one of {', '.join(LEVELS)}")
    elements = _element_index(projection)
    element_id = resolve_architecture_node(projection, value)
    if element_id is None:
        raise ValueError(f"no unique architecture node for '{value}'")
    if target == "code":
        node_ids = [
            node_id
            for node_id, mapped in projection.get("mappings", {}).items()
            if len(mapped) == 1 and _is_descendant(mapped[0], element_id, elements)
        ]
        return {"target": target, "elements": sorted(node_ids)}
    descendants = [
        element
        for candidate, element in elements.items()
        if candidate != element_id
        and element.get("c4_type") in LEVEL_TYPES[target]
        and _is_descendant(candidate, element_id, elements)
    ]
    return {"target": target, "elements": sorted(descendants, key=lambda item: item["id"])}


def impact(projection: dict[str, Any], graph: dict[str, Any], value: str, depth: int = 2) -> dict[str, Any]:
    """Reverse-walk factual edges and roll impacted code back to C4 components."""
    mappings = projection.get("mappings", {})
    node_ids = {str(node.get("id")) for node in graph.get("nodes", []) if isinstance(node, dict)}
    seed = value if value in node_ids else None
    if seed is None:
        source_matches = [
            str(node.get("id"))
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and _normalise_path(node.get("source_file")) == _normalise_path(value)
        ]
        seed = source_matches[0] if len(source_matches) == 1 else None
    if seed is None:
        raise ValueError(f"no unique graph node for '{value}'")
    incoming: dict[str, list[str]] = defaultdict(list)
    for link in _links(graph):
        source = str(link.get("_src") or link.get("source") or "")
        target = str(link.get("_tgt") or link.get("target") or "")
        incoming[target].append(source)
    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    seen = {seed}
    impacted: set[str] = set()
    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue
        for caller in incoming.get(current, []):
            if caller in seen:
                continue
            seen.add(caller)
            queue.append((caller, current_depth + 1))
            mapped = mappings.get(caller, [])
            if len(mapped) == 1:
                impacted.add(mapped[0])
    return {"seed": seed, "depth": depth, "components": sorted(impacted)}


def load_graph(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"graph not found: {path}; run 'graphify extract' first") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"graph is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise ValueError(f"graph has no nodes array: {path}")
    return data


def sync(model_path: Path, graph_path: Path, output_path: Path) -> dict[str, Any]:
    model = load_model(model_path)
    graph = load_graph(graph_path)
    projection = build_projection(model, graph)
    projection["findings"] = conformance(projection)
    write_json_atomic(output_path, projection, indent=2, ensure_ascii=False)
    return projection


def init_model(path: Path) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite existing architecture model: {path}")
    write_text_atomic(path, json.dumps(default_model(), indent=2, ensure_ascii=False) + "\n")


def format_view(data: dict[str, Any]) -> str:
    lines = [f"Architecture view: {data['level']}"]
    if data.get("focus"):
        lines.append(f"Focus: {data['focus']}")
    lines.append("Elements:")
    for element in data["elements"]:
        lines.append(f"- {element['id']} [{element['c4_type']}] {element['name']}")
    lines.append("Observed relationships:")
    for relation in data["observed_relations"]:
        lines.append(
            f"- {relation['source']} --{relation['kind']}--> {relation['target']} "
            f"({relation['evidence_count']} evidence)"
        )
    return "\n".join(lines)


def format_findings(findings: Iterable[dict[str, Any]]) -> str:
    findings = list(findings)
    if not findings:
        return "Architecture conformance: OK"
    lines = ["Architecture conformance:"]
    for finding in findings:
        severity = str(finding.get("severity", "warning")).upper()
        kind = finding.get("kind", "finding")
        subject = finding.get("rule") or finding.get("node") or (
            f"{finding.get('source', '?')} -> {finding.get('target', '?')}"
        )
        lines.append(f"- {severity} {kind}: {subject}")
    return "\n".join(lines)


def _common_paths(args: list[str]) -> tuple[Path, Path, Path, list[str]]:
    """Read shared architecture command flags and return unconsumed arguments."""
    model_path = Path("architecture/graphify.c4.json")
    graph_path = Path("graphify-out/graph.json")
    output_path = Path("graphify-out/architecture.json")
    rest: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--model", "--graph", "--out"):
            if index + 1 >= len(args):
                raise ValueError(f"{arg} needs a path")
            value = Path(args[index + 1])
            if arg == "--model":
                model_path = value
            elif arg == "--graph":
                graph_path = value
            else:
                output_path = value
            index += 2
        elif arg.startswith("--model="):
            model_path = Path(arg.split("=", 1)[1]); index += 1
        elif arg.startswith("--graph="):
            graph_path = Path(arg.split("=", 1)[1]); index += 1
        elif arg.startswith("--out="):
            output_path = Path(arg.split("=", 1)[1]); index += 1
        else:
            rest.append(arg); index += 1
    return model_path, graph_path, output_path, rest


def architecture_usage() -> str:
    return """Usage: graphify architecture <command> [options]

Commands:
  init       create architecture/graphify.c4.json without overwriting it
  sync       project graph.json onto the declared C4 model
  validate   report model/conformance violations (exit 2 on errors)
  audit      list cross-component edges without namespace/import proof
  view       show one C4 level: --level context|container|component|code
  html       write an interactive architecture.html browser view
  diff       compare two synced projections at Context, Container, Component and Code
  up <node>  raise a code node or C4 id to its C4 ancestors
  down <id>  descend via --to component|container|code
  impact <node-or-file>  reverse-walk dependencies and return C4 components

Common options: --model PATH --graph PATH --out PATH

Diff options: --before PROJECTION --after PROJECTION --out PATH [--html PATH]
""".rstrip()


def dispatch_cli(args: list[str]) -> int:
    """Dispatch ``graphify architecture`` while keeping command parsing testable."""
    if not args or args[0] in ("-h", "--help"):
        print(architecture_usage())
        return 0
    command = args[0]
    try:
        if command == "diff":
            before_path: Path | None = None
            after_path: Path | None = None
            output_path = Path("graphify-out/architecture-diff.json")
            html_output: Path | None = None
            index = 1
            while index < len(args):
                option = args[index]
                if option in ("--before", "--after", "--out", "--html"):
                    if index + 1 >= len(args):
                        raise ValueError(f"{option} needs a path")
                    value = Path(args[index + 1])
                    if option == "--before":
                        before_path = value
                    elif option == "--after":
                        after_path = value
                    elif option == "--out":
                        output_path = value
                    else:
                        html_output = value
                    index += 2
                elif option.startswith("--before="):
                    before_path = Path(option.split("=", 1)[1]); index += 1
                elif option.startswith("--after="):
                    after_path = Path(option.split("=", 1)[1]); index += 1
                elif option.startswith("--out="):
                    output_path = Path(option.split("=", 1)[1]); index += 1
                elif option.startswith("--html="):
                    html_output = Path(option.split("=", 1)[1]); index += 1
                else:
                    raise ValueError(f"unknown diff option '{option}'")
            if before_path is None or after_path is None:
                raise ValueError("diff needs --before PROJECTION and --after PROJECTION")
            result = architecture_diff(load_projection(before_path), load_projection(after_path))
            write_json_atomic(output_path, result, indent=2, ensure_ascii=False)
            if html_output is not None:
                from graphify.architecture_diff_html import write_architecture_diff_html

                write_architecture_diff_html(result, html_output)
            print(f"Wrote architecture diff: {output_path}")
            if html_output is not None:
                print(f"Wrote interactive architecture diff: {html_output}")
            return 0

        model_path, graph_path, output_path, rest = _common_paths(args[1:])
        if command == "init":
            if rest:
                raise ValueError("init accepts only --model")
            init_model(model_path)
            print(f"Created architecture model: {model_path}")
            return 0

        if command == "sync":
            if rest:
                raise ValueError("sync accepts only common path options")
            projection = sync(model_path, graph_path, output_path)
            print(
                f"Architecture projection: {len(projection['elements'])} elements, "
                f"{len(projection['observed_relations'])} observed relationships, "
                f"{len(projection['findings'])} findings\n"
                f"Wrote {output_path}"
            )
            return 0

        model = load_model(model_path)
        graph = load_graph(graph_path)
        projection = build_projection(model, graph)
        projection["findings"] = conformance(projection)

        if command == "validate":
            if rest:
                raise ValueError("validate accepts only common path options")
            print(format_findings(projection["findings"]))
            return 2 if any(item["severity"] == "error" for item in projection["findings"]) else 0

        if command == "audit":
            if rest and rest != ["--suspect"]:
                raise ValueError("audit accepts only --suspect")
            findings = suspect_dependencies(projection, graph)
            if not findings:
                print("Architecture audit: no suspect dependencies")
                return 0
            print("Architecture audit: suspect dependencies")
            for finding in findings:
                reasons = ", ".join(
                    f"{item['relation']}; {item['confidence']}; {item['resolution']}"
                    for item in finding["reasons"]
                )
                samples = ", ".join(
                    f"{item['source_file']}:{item['source_location']}"
                    for item in finding["samples"]
                )
                print(
                    f"- {finding['source']} -> {finding['target']} "
                    f"[{finding['evidence_count']} evidence; {reasons}] {samples}"
                )
            return 0

        if command == "html":
            output = Path("graphify-out/architecture.html")
            if not rest:
                pass
            elif len(rest) == 2 and rest[0] == "--output":
                output = Path(rest[1])
            elif len(rest) == 1 and rest[0].startswith("--output="):
                output = Path(rest[0].split("=", 1)[1])
            else:
                raise ValueError("html accepts only --output PATH")
            from graphify.architecture_html import write_architecture_html

            write_architecture_html(projection, output)
            print(f"Wrote interactive architecture view: {output}")
            return 0

        if command == "view":
            level = "container"
            focus: str | None = None
            index = 0
            while index < len(rest):
                if rest[index] == "--level" and index + 1 < len(rest):
                    level = rest[index + 1]; index += 2
                elif rest[index].startswith("--level="):
                    level = rest[index].split("=", 1)[1]; index += 1
                elif rest[index] == "--focus" and index + 1 < len(rest):
                    focus = rest[index + 1]; index += 2
                elif rest[index].startswith("--focus="):
                    focus = rest[index].split("=", 1)[1]; index += 1
                else:
                    raise ValueError(f"unknown view option '{rest[index]}'")
            print(format_view(view(projection, level, focus)))
            return 0

        if command == "up":
            if len(rest) != 1:
                raise ValueError("up needs exactly one code node or C4 id")
            for element in up(projection, rest[0]):
                print(f"{element['id']} [{element['c4_type']}] {element['name']}")
            return 0

        if command == "down":
            if not rest:
                raise ValueError("down needs a C4 id")
            value = rest[0]
            target = "component"
            options = rest[1:]
            if len(options) == 2 and options[0] == "--to":
                target = options[1]
            elif len(options) == 1 and options[0].startswith("--to="):
                target = options[0].split("=", 1)[1]
            elif options:
                raise ValueError("down accepts only --to LEVEL")
            result = down(projection, value, target)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if command == "impact":
            if not rest:
                raise ValueError("impact needs a graph node id or source-file path")
            value = rest[0]
            depth = 2
            options = rest[1:]
            if len(options) == 2 and options[0] == "--depth":
                depth = int(options[1])
            elif len(options) == 1 and options[0].startswith("--depth="):
                depth = int(options[0].split("=", 1)[1])
            elif options:
                raise ValueError("impact accepts only --depth N")
            print(json.dumps(impact(projection, graph, value, depth), indent=2, ensure_ascii=False))
            return 0

        raise ValueError(f"unknown architecture command '{command}'")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
