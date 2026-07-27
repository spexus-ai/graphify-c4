# C4 architecture projection

Graphify keeps its extracted graph of code facts in `graphify-out/graph.json`.
The C4 feature creates a separate `graphify-out/architecture.json` projection
from that graph and a checked-in architecture contract at
`architecture/graphify.c4.json`. It never adds declared architecture nodes to
the generic graph.

## Start

```text
graphify architecture init
graphify architecture sync
graphify architecture validate
graphify architecture view --level container
graphify architecture html
```

`init` refuses to overwrite an existing model. The generated file is a small
starter contract; teams must name the system, its ownership, and the important
boundaries before treating it as an architectural source of truth.

## Contract

The model uses the schema `graphify.architecture/v1`. Elements have stable IDs,
a C4 type, optional parent, and optional implementation selectors. Supported
selectors are `path_prefix`, `source_file`, `node_id`, and `label`.

```json
{
  "schema": "graphify.architecture/v1",
  "scope": {
    "include": ["back/**", "front/**"],
    "exclude": ["**/*_test.go", "**/node_modules/**"]
  },
  "elements": [
    {"id": "system", "c4_type": "software_system", "name": "Product"},
    {"id": "back", "c4_type": "container", "parent": "system", "name": "Backend"},
    {
      "id": "back.requirements",
      "c4_type": "component",
      "parent": "back",
      "name": "Requirements",
      "implementation": [{"path_prefix": "back/internal/service/requirement"}]
    }
  ],
  "relations": [],
  "rules": []
}
```

`relations` describe intended dependencies. `rules` can forbid observed code
dependencies, for example a frontend container reaching any datastore:

```json
{
  "id": "front-no-db",
  "deny": {"source": "front", "target_type": "datastore"}
}
```

## Navigation and evidence

- `view` rolls observed symbol relationships to `context`, `container`,
  `component`, or `code` level. During `sync`, mapped code nodes become
  generated `code:<graph-node-id>` children of their component; the contract
  itself remains compact and declares only architectural boundaries.
- `html` writes `graphify-out/architecture.html`, an interactive C4 view using
  the same vis-network renderer as Graphify's `graph.html`: force-directed
  layout, drag, zoom, search, node inspection and filter-out checkboxes for
  the currently visible nodes. Declared relations are solid green arrows;
  code-derived evidence is orange and dashed.
- `up <node>` maps a code node to its component, container, and system.
- `down <element> --to code` returns the implementing graph node IDs.
- `impact <node-or-file>` performs a reverse dependency walk and returns the
  impacted components.
- `validate` reports unmapped code, ambiguous mappings, observed dependencies
  missing from `relations`, and rule violations.

Every observed relationship in `architecture.json` retains the code node IDs,
source file, source location, and `EXTRACTED`/`INFERRED` confidence. Only
reviewed rules backed by `EXTRACTED` evidence should fail CI.
