from pathlib import Path
from graphify.detect import detect
from graphify.extract import extract
from graphify.openapi_ingest import discover_openapi_yaml


def test_openapi_yaml_ref_closure_and_source_evidence(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.1.0\npaths:\n  /a:\n    $ref: ./routes.yaml#/a\n"
    )
    (tmp_path / "routes.yaml").write_text(
        'a:\n  get:\n    responses:\n      "200":\n        $ref: ./models.yaml#/Ok\n'
    )
    (tmp_path / "models.yaml").write_text("Ok:\n  type: object\n")
    (tmp_path / "prose.yaml").write_text("title: Example\n")
    d = detect(tmp_path)
    assert {Path(p).name for p in d["files"]["code"]} == {"api.yaml", "routes.yaml", "models.yaml"}
    assert str(tmp_path / "prose.yaml") in d["files"]["document"]
    g = extract(
        [Path(p) for p in d["files"]["code"]], root=tmp_path, cache_root=tmp_path, parallel=False
    )
    ns = {n["id"]: n for n in g["nodes"]}
    refs = [e for e in g["edges"] if e.get("resolution") == "openapi_local_ref"]
    assert len(refs) == 2
    assert {(ns[e["source"]]["source_file"], ns[e["target"]]["source_file"]) for e in refs} == {
        ("api.yaml", "routes.yaml"),
        ("routes.yaml", "models.yaml"),
    }
    assert all(
        e["source_location"].startswith("L") and e["confidence"] == "EXTRACTED" for e in refs
    )


def test_ref_cannot_promote_unscanned_ignored_sensitive_or_remote_files(tmp_path):
    (tmp_path / "api.yml").write_text(
        "openapi: 3.0.0\nx:\n  $ref: ./secret.yaml\ny:\n  $ref: https://example.com/no.yaml\nz:\n  $ref: ./ignored.yaml\n"
    )
    (tmp_path / "secret.yaml").write_text("password: secret\n")
    (tmp_path / "ignored.yaml").write_text("type: object\n")
    (tmp_path / ".graphifyignore").write_text("ignored.yaml\n")
    d = detect(tmp_path)
    assert {Path(p).name for p in d["files"]["code"]} == {"api.yml"}


def test_cycle_alias_invalid_swagger_and_arbitrary_filename(tmp_path):
    root = tmp_path / "any.yml"
    part = tmp_path / "part.yml"
    root.write_text('swagger: "2.0"\nx:\n  $ref: part.yml\n')
    part.write_text("x: &a\n  self: *a\n  $ref: any.yml\n")
    broken = tmp_path / "bad.yaml"
    broken.write_text("a: [")
    assert discover_openapi_yaml([str(root), str(part), str(broken)]) == {str(root), str(part)}


def test_detached_fragment_pointing_at_root_is_visible(tmp_path):
    root = tmp_path / "api.yaml"
    detached = tmp_path / "detached.yaml"
    root.write_text("openapi: 3.1.0\npaths: {}\n")
    detached.write_text(
        "/hidden:\n  get:\n    responses:\n      default:\n        $ref: api.yaml#/components/responses/Error\n"
    )
    assert discover_openapi_yaml([str(root), str(detached)]) == {str(root), str(detached)}
