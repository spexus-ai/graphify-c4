from __future__ import annotations

import json

import graphify.__main__ as mainmod
from graphify.architecture import (
    architecture_diff,
    build_projection,
    compose_workspace_graph,
    conformance,
    down,
    impact,
    load_model,
    suspect_dependencies,
    sync_workspace,
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


def test_architecture_diff_rolls_code_changes_to_every_c4_level():
    before = build_projection(MODEL, GRAPH)
    after_graph = {
        "nodes": [
            *GRAPH["nodes"],
            {"id": "new", "label": "newFlow()", "file_type": "code", "source_file": "src/ui/new.ts"},
        ],
        "links": [
            *GRAPH["links"],
            {"source": "new", "target": "db", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "ui", "target": "db", "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    after = build_projection(MODEL, after_graph)

    diff = architecture_diff(before, after)

    assert diff["schema"] == "graphify.architecture-diff/v1"
    assert next(item for item in diff["levels"]["code"]["elements"] if item["id"] == "code:new")["status"] == "added"
    assert next(item for item in diff["levels"]["code"]["elements"] if item["id"] == "code:ui")["status"] == "modified"
    component = next(item for item in diff["levels"]["component"]["elements"] if item["id"] == "front.ui")
    container = next(item for item in diff["levels"]["container"]["elements"] if item["id"] == "front")
    context = next(item for item in diff["levels"]["context"]["elements"] if item["id"] == "system")
    assert component["descendant_delta"]["added"] == 1
    assert component["descendant_delta"]["modified"] == 1
    assert component["direct_status"] == "unchanged"
    assert component["status"] == "modified"
    assert container["descendant_delta"]["added"] == 1
    assert context["descendant_delta"]["added"] == 1
    relation = next(item for item in diff["levels"]["container"]["relations"] if item["source"] == "front")
    assert relation["status"] == "modified"


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
    assert "packagePath" in html
    assert "cross-package bridges" in html
    assert "go_import_type" in html
    assert "maxFocusedCodeNodes = 750" in html
    assert "growConnected" in html


def test_architecture_diff_cli_writes_all_levels_and_interactive_view(monkeypatch, tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    out_path = tmp_path / "diff.json"
    html_path = tmp_path / "diff.html"
    before_path.write_text(json.dumps(build_projection(MODEL, GRAPH)), encoding="utf-8")
    after_path.write_text(json.dumps(build_projection(MODEL, {
        "nodes": [*GRAPH["nodes"], {"id": "new", "label": "newFlow()", "file_type": "code", "source_file": "src/ui/new.ts"}],
        "links": [*GRAPH["links"], {"source": "new", "target": "db", "relation": "calls", "confidence": "EXTRACTED"}],
    })), encoding="utf-8")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "architecture", "diff", "--before", str(before_path), "--after", str(after_path),
        "--out", str(out_path), "--html", str(html_path),
    ])

    mainmod.main()

    assert "Wrote architecture diff" in capsys.readouterr().out
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(saved["levels"]) == {"context", "container", "component", "code"}
    html = html_path.read_text(encoding="utf-8")
    assert "C4 Architecture Diff" in html
    assert "double-click a node to drill down" in html
    assert "#0072B2" in html
    assert "triangleDown" in html


def test_workspace_composition_namespaces_facts_and_keeps_contracts_declared(tmp_path):
    model = {
        "schema": "graphify.architecture/v1",
        "repositories": [
            {"id": "web", "path": "web"},
            {"id": "api", "path": "api"},
        ],
        "scope": {"include": ["web/src/**", "api/src/**"]},
        "elements": [
            {"id": "system", "c4_type": "software_system", "name": "System"},
            {"id": "web.container", "c4_type": "container", "parent": "system", "name": "Web"},
            {"id": "api.container", "c4_type": "container", "parent": "system", "name": "API"},
            {"id": "web.ui", "c4_type": "component", "parent": "web.container", "name": "UI",
             "implementation": [{"path_prefix": "web/src"}]},
            {"id": "api.handlers", "c4_type": "component", "parent": "api.container", "name": "Handlers",
             "implementation": [{"path_prefix": "api/src"}]},
        ],
        "relations": [{"source": "web.container", "target": "api.container", "kind": "http"}],
        "rules": [],
    }
    raw_graph = {
        "nodes": [{"id": "main", "label": "main", "file_type": "code", "source_file": "src/main.ts"}],
        "links": [{"source": "main", "target": "main", "relation": "contains", "source_file": "src/main.ts"}],
    }
    for repository in ("web", "api"):
        output = tmp_path / repository / "graphify-out"
        output.mkdir(parents=True)
        (output / "graph.json").write_text(json.dumps(raw_graph), encoding="utf-8")
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")

    composed = compose_workspace_graph(load_model(model_path), tmp_path)
    projection_path = tmp_path / "architecture.json"
    graph_path = tmp_path / "workspace-graph.json"
    projection = sync_workspace(model_path, tmp_path, projection_path, graph_path)

    assert {item["id"] for item in composed["nodes"]} == {"web::main", "api::main"}
    assert {item["source_file"] for item in composed["nodes"]} == {"web/src/main.ts", "api/src/main.ts"}
    assert {item["id"] for item in projection["elements"] if item["c4_type"] == "code"} == {
        "code:web::main", "code:api::main",
    }
    assert projection["workspace_repositories"] == [
        {"id": "web", "path": "web", "graph": str(tmp_path / "web/graphify-out/graph.json"), "nodes": 1, "links": 1},
        {"id": "api", "path": "api", "graph": str(tmp_path / "api/graphify-out/graph.json"), "nodes": 1, "links": 1},
    ]
    assert projection["declared_relations"] == model["relations"]
    assert projection_path.exists() and graph_path.exists()


def test_workspace_html_cli_writes_all_level_explorer(monkeypatch, tmp_path, capsys):
    model = {
        "schema": "graphify.architecture/v1",
        "repositories": [{"id": "web", "path": "web"}],
        "scope": {"include": ["web/src/**"]},
        "elements": [
            {"id": "system", "c4_type": "software_system", "name": "System"},
            {"id": "web.container", "c4_type": "container", "parent": "system", "name": "Web"},
            {"id": "web.ui", "c4_type": "component", "parent": "web.container", "name": "UI",
             "implementation": [{"path_prefix": "web/src"}]},
        ],
        "relations": [], "rules": [],
    }
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    graph_dir = tmp_path / "web/graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text(json.dumps({
        "nodes": [{"id": "ui", "label": "render", "file_type": "code", "source_file": "src/render.ts"}],
        "links": [],
    }), encoding="utf-8")
    projection_path = tmp_path / "architecture.json"
    workspace_graph_path = tmp_path / "workspace-graph.json"
    html_path = tmp_path / "architecture.html"
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "architecture", "workspace", "html", "--model", str(model_path), "--root", str(tmp_path),
        "--out", str(projection_path), "--graph-out", str(workspace_graph_path), "--output", str(html_path),
    ])

    mainmod.main()

    assert "Workspace architecture projection" in capsys.readouterr().out
    assert html_path.exists()
    html = html_path.read_text(encoding="utf-8")
    assert "Graphify C4 Architecture" in html
    assert "web::ui" in html
