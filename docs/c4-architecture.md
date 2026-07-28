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
graphify architecture audit --suspect
graphify architecture view --level container
graphify architecture html
graphify architecture diff --before snapshots/before/architecture.json --after snapshots/after/architecture.json --html graphify-out/architecture-diff.html
```

`init` refuses to overwrite an existing model. The generated file is a small
starter contract; teams must name the system, its ownership, and the important
boundaries before treating it as an architectural source of truth.

## Contract

The model uses the schema `graphify.architecture/v1`. Elements have stable IDs,
a C4 type, optional parent, and optional implementation selectors. Supported
selectors are `path_prefix`, `path_glob`, `source_file`, `node_id`, `label`, and
`label_regex`.

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

### Concrete implementation components

A folder is often a useful *container boundary*, but it is usually too broad
to be a C4 Component. `component_generators` create a component per selected
source symbol: a Go handler/struct, a Java or Kotlin class, or a React
component. The generated component owns either its complete source file when
it is the sole selected root, or just its proven `contains`/`method` members.
That makes a component-level diff about implementations such as `RoleHandler`,
not `internal/handlers`.

```json
{
  "component_generators": [{
    "id_prefix": "back.handler",
    "parent": "container.back",
    "name_template": "{label}",
    "selector": {
      "path_prefix": "back/internal/handlers",
      "label_regex": "^[A-Z][A-Za-z0-9]*Handler$"
    },
    "ownership": "file_if_unique"
  }]
}
```

`file_if_unique` is the default and follows the normal one-primary-type-per-file
convention. Use `structural` for files containing several selected classes: the
component then owns only the root and direct structural members. Generated roots
take precedence over a broad folder selector; two generated roots claiming the
same code are reported as an ambiguous mapping instead of silently choosing one.

## Multi-repository workspace

A workspace model is the same C4 contract with an additional `repositories`
catalogue. Each entry has a stable `id`, a path relative to the workspace root,
and optionally a non-default graph path. It is language-neutral: Graphify
extracts Java, Kotlin/Android, TypeScript/React and other supported source into
the same observed-facts format before C4 projection.

Start a portable model for an arbitrary multi-repository project:

```text
graphify architecture workspace init \
  --system "Storefront" \
  --repo web=web \
  --repo api=services/api \
  --repo android-app=mobile/android
```

This writes `architecture/graphify.workspace.c4.json` without overwriting an
existing model. The starter gives each repository a container but intentionally
does not invent a folder-shaped component. Add `component_generators` for the
real implementation roots after architectural review; this avoids a misleading
Component view in a new Java/Kotlin/React workspace.

Build one local fact graph per repository before composition. For a source-only
first pass this has no LLM dependency:

```text
graphify extract web --out web --code-only
graphify extract services/api --out services/api --code-only
graphify extract mobile/android --out mobile/android --code-only
```

```json
{
  "schema": "graphify.architecture/v1",
  "repositories": [
    {"id": "front", "path": "front"},
    {"id": "back", "path": "back", "graph": "architecture/graphify-out/graph.json"}
  ],
  "scope": {"include": ["front/src/**", "back/internal/**"]}
}
```

```text
graphify architecture workspace html \
  --model architecture/graphify.workspace.c4.json \
  --root . \
  --out architecture/graphify-out/architecture.json \
  --graph-out architecture/graphify-out/workspace-graph.json \
  --output architecture/graphify-out/architecture.html
```

The workspace command namespaces node IDs and source paths (`web::…`,
`web/src/…`) before C4 projection, and writes graph references relative to the
workspace root. Consequently the contract and its JSON/HTML outputs can move to
another checkout or CI machine. It does not infer a cross-repository
implementation edge: declare HTTP, message, CLI, or datastore boundaries as
contracts until an extractor gives direct evidence. The generated explorer keeps
Context, Container, Component, and Code navigation.

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
- `audit --suspect` lists cross-component dependencies that are based on a
  name-only or unknown resolution rather than a same-file/same-package match or
  an explicit local Go import (including an imported Go type). Treat these as
  leads for review, not as confirmed architecture violations.

Every observed relationship in `architecture.json` retains the code node IDs,
source file, source location, resolution provenance, and
`EXTRACTED`/`INFERRED` confidence. Only reviewed rules backed by `EXTRACTED`
evidence with trustworthy resolution should fail CI.

## Historical diff

Run `sync` independently for both revisions, preferably with the same C4
contract, then compare the two projections:

```text
graphify architecture diff \
  --before snapshots/f029bdd/architecture.json \
  --after snapshots/e9e3a82/architecture.json \
  --out graphify-out/architecture-diff.json \
  --html graphify-out/architecture-diff.html
```

The JSON payload uses `graphify.architecture-diff/v1` and contains a diff for
each C4 level: `context`, `container`, `component`, and `code`. Each level is
rolled up *before* comparison, so a pair of offsetting code changes does not
become a false added or removed component dependency. `added` and `removed`
node statuses describe only direct C4-element lifecycle; a stable component
whose Code changes is `modified` and retains its descendant delta. Relationships are `added`, `removed`,
`modified` when their evidence count changes, or `unchanged`.

At Code level a stable symbol is `modified` when its structural metadata or an
outgoing extracted relationship changes. This deliberately does not classify a
callee as modified merely because a new caller references it. Pure body edits
that leave the extracted structure unchanged need a future AST body fingerprint
to be detectable.

The HTML view switches levels and drills down on double-click. It hides
unchanged facts by default, but can show them for context. A historical model
is ideal; if a common current model is used for both revisions, treat the
result as a comparable code-to-boundary projection, not proof that the older
C4 contract existed at that time.
