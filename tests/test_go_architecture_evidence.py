from graphify.extract import extract


def test_go_complementary_build_variants_keep_conditions(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/app\n")
    (tmp_path / "main.go").write_text("package app\nfunc Run() bool { return enabled() }\n")
    (tmp_path / "normal.go").write_text(
        "//go:build !feature\n\npackage app\nfunc enabled() bool {return false}\n"
    )
    (tmp_path / "feature.go").write_text(
        "//go:build feature\n\npackage app\nfunc enabled() bool {return true}\n"
    )
    g = extract(list(tmp_path.glob("*.go")), root=tmp_path, cache_root=tmp_path, parallel=False)
    es = [e for e in g["edges"] if e.get("resolution") == "go_build_variant"]
    assert len(es) == 2
    assert {e["build_constraint"] for e in es} == {"feature", "!feature"}
    assert all(e["confidence"] == "EXTRACTED" for e in es)


def test_coincident_go_variants_remain_unresolved(tmp_path):
    (tmp_path / "main.go").write_text("package app\nfunc Run() bool {return enabled()}\n")
    for name, tag in [("one", "a"), ("two", "a")]:
        (tmp_path / (name + ".go")).write_text(
            "//go:build " + tag + "\n\npackage app\nfunc enabled() bool {return true}\n"
        )
    g = extract(list(tmp_path.glob("*.go")), root=tmp_path, cache_root=tmp_path, parallel=False)
    assert not any(e.get("resolution") == "go_build_variant" for e in g["edges"])


def test_complex_build_constraints_partition():
    from graphify.go_build import is_build_partition

    assert is_build_partition(["a || b", "!a && !b"])
    assert is_build_partition(["integration && feature", "!integration || !feature"])
    assert is_build_partition(["a && b", "!a", "a && !b"])
    assert not is_build_partition(["a", "b"])
    assert not is_build_partition(["a", "a && b"])
    assert not is_build_partition(["a;bad", "!a;bad"])


def test_imported_composite_and_cross_file_receiver(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/app\n")
    (tmp_path / "ports").mkdir()
    (tmp_path / "ports/model.go").write_text("package ports\ntype Reader struct{}\n")
    (tmp_path / "ports/method.go").write_text("package ports\nfunc (r Reader) Read() {}\n")
    (tmp_path / "main.go").write_text(
        'package app\nimport "example.test/app/ports"\nfunc Run() { _ = ports.Reader{} }\n'
    )
    g = extract(list(tmp_path.rglob("*.go")), root=tmp_path, cache_root=tmp_path, parallel=False)
    ns = {n["id"]: n for n in g["nodes"]}
    constructors = [
        e
        for e in g["edges"]
        if e.get("relation") == "constructs" and e.get("context") == "composite_literal"
    ]
    assert any(ns[e["target"]]["source_file"] == "ports/model.go" for e in constructors)
    receivers = [e for e in g["edges"] if e.get("resolution") == "go_receiver_type"]
    assert any(
        ns[e["source"]]["source_file"] == "ports/method.go"
        and ns[e["target"]]["source_file"] == "ports/model.go"
        for e in receivers
    )


def test_package_values_and_shadowing(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/app\n")
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta/vars.go").write_text('package meta\nvar Revision = "dev"\n')
    (tmp_path / "flags.go").write_text("package app\nconst Enabled = true\n")
    (tmp_path / "main.go").write_text(
        'package app\nimport "example.test/app/meta"\nfunc Run() string {if Enabled {return meta.Revision};return ""}\nfunc Shadow(Enabled bool) bool {return Enabled}\n'
    )
    g = extract(list(tmp_path.rglob("*.go")), root=tmp_path, cache_root=tmp_path, parallel=False)
    ns = {n["id"]: n for n in g["nodes"]}
    es = [e for e in g["edges"] if e.get("resolution") == "go_package_value"]
    assert {ns[e["target"]]["label"] for e in es} == {"Revision", "Enabled"}
    assert not any(ns[e["source"]]["label"] == "Shadow()" for e in es)


def test_imported_build_variants(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/app\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "main.go").write_text(
        'package app\nimport "example.test/app/lib"\nfunc Run() bool {return lib.Enabled()}\n'
    )
    for name, tag in [("one", "a || b"), ("two", "!a && !b")]:
        (tmp_path / "lib" / (name + ".go")).write_text(
            "//go:build " + tag + "\n\npackage lib\nfunc Enabled() bool {return true}\n"
        )
    g = extract(list(tmp_path.rglob("*.go")), root=tmp_path, cache_root=tmp_path, parallel=False)
    assert len([e for e in g["edges"] if e.get("resolution") == "go_build_variant"]) == 2


def test_nonexhaustive_feature_variants_require_unique_build_witness():
    from graphify.go_build import build_variant_witnesses

    witnesses = build_variant_witnesses(["a", "b", "c", "!a && !b && !c"])
    assert witnesses is not None
    assert witnesses[0] == {"a": True, "b": False, "c": False}
    assert build_variant_witnesses(["a", "a"]) is None
    assert build_variant_witnesses(["a", "!a"], "a") is None


def test_c4_preserves_build_evidence():
    from graphify.architecture import build_projection, suspect_dependencies

    model = {
        "schema": "graphify.architecture/v1",
        "elements": [
            {"id": "s", "c4_type": "software_system", "name": "System"},
            {"id": "c", "c4_type": "container", "parent": "s", "name": "App"},
            {
                "id": "a",
                "c4_type": "component",
                "parent": "c",
                "name": "Caller",
                "implementation": [{"node_id": "one"}],
            },
            {
                "id": "b",
                "c4_type": "component",
                "parent": "c",
                "name": "Feature",
                "implementation": [{"node_id": "two"}],
            },
        ],
    }
    graph = {
        "nodes": [
            {"id": "one", "source_file": "one.go", "file_type": "code"},
            {"id": "two", "source_file": "two.go", "file_type": "code"},
        ],
        "edges": [
            {
                "source": "one",
                "target": "two",
                "relation": "calls",
                "resolution": "go_build_variant",
                "build_constraint": "feature",
                "build_witness": {"feature": True},
            }
        ],
    }
    projection = build_projection(model, graph)
    proof = projection["observed_relations"][0]["evidence"][0]
    assert proof["build_constraint"] == "feature"
    assert proof["build_witness"] == {"feature": True}
    assert suspect_dependencies(projection, graph) == []
