from __future__ import annotations

import json

import graphify.__main__ as mainmod
from graphify.architecture import (
    build_projection,
    conformance,
    down,
    impact,
    suspect_dependencies,
    up,
    validate_model,
    view,
)


MODEL = {
    "schema": "graphify.architecture/v1",
    "scope": {"include": ["src/**", "infra/**"], "exclude": ["**/*_test.py"]},
    "elements": [
        {"id": "system", "c4_type": "software_system", "name": "System"},
        {"id": "front", "c4_type": "container", "parent": "system", "name": "Frontend"},
        {"id": "front.ui", "c4_type": "component", "parent": "front", "name": "UI",
         "implementation": [{"path_prefix": "src/ui"}]},
        {"id": "database", "c4_type": "datastore", "parent": "system", "name": "Database",
         "implementation": [{"path_prefix": "infra/db"}]},
    ],
    "relations": [],
    "rules": [
        {"id": "front-no-db", "deny": {"source": "front", "target_type": "datastore"}},
    ],
}


GRAPH = {
    "nodes": [
        {"id": "ui", "label": "render()", "file_type": "code", "source_file": "src/ui/render.ts"},
        {"id": "db", "label": "query()", "file_type": "code", "source_file": "infra/db/client.py"},
        {"id": "unmapped", "label": "orphan()", "file_type": "code", "source_file": "src/orphan.py"},
        {"id": "test", "label": "test_render", "file_type": "code", "source_file": "src/ui/render_test.py"},
    ],
    "links": [
        {
            "source": "ui", "target": "db", "relation": "calls", "confidence": "EXTRACTED",
            "source_file": "src/ui/render.ts", "source_location": "L14",
            "resolution": "go_import",
        },
    ],
}


def test_model_contract_is_valid():
    assert validate_model(MODEL) == []


def test_model_validation_rejects_parent_cycles_and_wrong_collection_types():
    invalid = {
        **MODEL,
        "relations": {},
        "rules": {},
        "elements": [
            {"id": "a", "c4_type": "container", "name": "A", "parent": "b"},
            {"id": "b", "c4_type": "component", "name": "B", "parent": "a"},
        ],
    }

    errors = validate_model(invalid)

    assert any("parent cycle" in error for error in errors)
    assert "relations must be a list" in errors
    assert "rules must be a list" in errors


def test_projection_rolls_code_relationships_to_c4_and_retains_evidence():
    projection = build_projection(MODEL, GRAPH)

    assert projection["mappings"] == {"db": ["database"], "ui": ["front.ui"]}
    assert projection["unmapped_code_nodes"] == ["unmapped"]
    assert projection["code_node_ids"] == {"db": "code:db", "ui": "code:ui"}
    assert [element["id"] for element in projection["elements"] if element["c4_type"] == "code"] == [
        "code:db", "code:ui"
    ]
    assert projection["observed_code_relations"] == [
        {
            "source": "code:ui",
            "target": "code:db",
            "kind": "calls",
            "confidence": {"EXTRACTED": 1},
            "evidence": [{
                "source_node": "ui", "target_node": "db", "source_file": "src/ui/render.ts",
                "source_location": "L14", "confidence": "EXTRACTED",
                "resolution": "go_import",
            }],
        }
    ]
    assert projection["observed_relations"] == [
        {
            "source": "front.ui",
            "target": "database",
            "kind": "calls",
            "confidence": {"EXTRACTED": 1},
            "evidence": [{
                "source_node": "ui", "target_node": "db", "source_file": "src/ui/render.ts",
                "source_location": "L14", "confidence": "EXTRACTED",
                "resolution": "go_import",
            }],
        }
    ]


def test_conformance_reports_forbidden_undeclared_and_unmapped_code():
    findings = conformance(build_projection(MODEL, GRAPH))

    assert {finding["kind"] for finding in findings} == {
        "forbidden_dependency", "undeclared_dependency", "unmapped_code",
    }
    assert next(finding for finding in findings if finding["kind"] == "forbidden_dependency")["rule"] == "front-no-db"


def test_suspect_dependencies_require_namespace_or_import_proof():
    graph = {
        **GRAPH,
        "links": [{
            **GRAPH["links"][0],
            "confidence": "INFERRED",
            "resolution": "name_guess",
        }],
    }

    findings = suspect_dependencies(build_projection(MODEL, graph), graph)

    assert findings == [{
        "severity": "warning", "kind": "suspect_dependency", "source": "front.ui", "target": "database",
        "evidence_count": 1,
        "reasons": [{"relation": "calls", "confidence": "INFERRED", "resolution": "name_guess"}],
        "samples": [{
            "source_node": "ui", "target_node": "db", "source_file": "src/ui/render.ts", "source_location": "L14",
        }],
    }]


def test_go_import_type_evidence_is_not_suspect():
    graph = {
        **GRAPH,
        "links": [{
            **GRAPH["links"][0],
            "confidence": "EXTRACTED",
            "resolution": "go_import_type",
        }],
    }

    assert suspect_dependencies(build_projection(MODEL, graph), graph) == []


def test_projection_keeps_intra_component_code_relationships():
    graph = {
        "nodes": [
            {"id": "ui", "label": "render()", "file_type": "code", "source_file": "src/ui/render.ts"},
            {"id": "helper", "label": "helper()", "file_type": "code", "source_file": "src/ui/helper.ts"},
        ],
        "links": [{"source": "ui", "target": "helper", "relation": "calls", "confidence": "EXTRACTED"}],
    }

    projection = build_projection(MODEL, graph)

    assert projection["observed_relations"] == []
    assert projection["observed_code_relations"] == [{
        "source": "code:ui", "target": "code:helper", "kind": "calls",
        "confidence": {"EXTRACTED": 1},
        "evidence": [{
            "source_node": "ui", "target_node": "helper", "source_file": "",
            "source_location": "", "confidence": "EXTRACTED", "resolution": "unknown",
        }],
    }]


def test_hierarchical_navigation_moves_between_code_component_container_and_context():
    projection = build_projection(MODEL, GRAPH)

    assert [element["id"] for element in up(projection, "ui")] == ["code:ui", "front.ui", "front", "system"]
    assert down(projection, "front", "code") == {"target": "code", "elements": ["ui"]}
    container_view = view(projection, "container")
    assert [element["id"] for element in container_view["elements"]] == ["database", "front"]
    assert container_view["observed_relations"] == [
        {"source": "front", "target": "database", "kind": "calls", "evidence_count": 1}
    ]
    code_view = view(projection, "code")
    assert [element["id"] for element in code_view["elements"]] == ["code:db", "code:ui"]
    assert code_view["observed_relations"] == [
        {"source": "code:ui", "target": "code:db", "kind": "calls", "evidence_count": 1}
    ]


def test_impact_rolls_reverse_code_dependencies_to_components():
    projection = build_projection(MODEL, GRAPH)

    assert impact(projection, GRAPH, "db") == {"seed": "db", "depth": 2, "components": ["front.ui"]}


def test_architecture_sync_cli_writes_sidecar_without_changing_graph(monkeypatch, tmp_path, capsys):
    model_path = tmp_path / "model.json"
    graph_path = tmp_path / "graph.json"
    out_path = tmp_path / "architecture.json"
    model_path.write_text(json.dumps(MODEL), encoding="utf-8")
    graph_path.write_text(json.dumps(GRAPH), encoding="utf-8")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        [
            "graphify", "architecture", "sync", "--model", str(model_path), "--graph", str(graph_path),
            "--out", str(out_path),
        ],
    )

    mainmod.main()

    assert "Architecture projection:" in capsys.readouterr().out
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["schema"] == "graphify.architecture/v1"
    assert saved["observed_relations"][0]["source"] == "front.ui"


def test_architecture_html_cli_writes_interactive_c4_view(monkeypatch, tmp_path, capsys):
    model_path = tmp_path / "model.json"
    graph_path = tmp_path / "graph.json"
    html_path = tmp_path / "architecture.html"
    model_path.write_text(json.dumps(MODEL), encoding="utf-8")
    graph_path.write_text(json.dumps(GRAPH), encoding="utf-8")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        [
            "graphify", "architecture", "html", "--model", str(model_path), "--graph", str(graph_path),
            "--output", str(html_path),
        ],
    )

    mainmod.main()

    assert "interactive architecture view" in capsys.readouterr().out
    html = html_path.read_text(encoding="utf-8")
    assert "C4 Architecture" in html
    assert "front.ui" in html
    assert "Graphify C4 Architecture" in html
    assert "vis-network@9.1.6" in html
    assert "forceAtlas2Based" in html
    assert "Drag nodes to arrange them" in html
    assert "observed_code_relations" in html
    assert "select-all-cb" in html
    assert "node-filter" in html
