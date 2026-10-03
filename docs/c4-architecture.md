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
selectors are `path_prefix`, `path_glob`, `source_file`, `source_file_regex`,
`node_id`, `label`, `label_regex`, and metadata fields.

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

### Discussion metadata

The Component graph is the default technical starting point for architecture
discussions. Add optional, reviewed metadata to an element or a
`component_generator` so the graph explains responsibility as well as
connectivity:

```json
{
  "id": "back.authorization",
  "c4_type": "component",
  "parent": "back",
  "name": "AuthorizationService",
  "responsibility": "Decide scoped permissions",
  "owner": "Identity team",
  "trust_boundary": "Authenticated tenant and project scope",
  "data_access": ["role bindings", "memberships"],
  "public_contracts": ["CheckScopedPermission"],
  "criticality": "high"
}
```

`responsibility`, `owner`, `trust_boundary`, and `criticality` are non-empty
strings; `data_access` and `public_contracts` are lists of non-empty strings.
Generator metadata is copied to every generated component. It is deliberately
descriptive rather than inferred: a team reviews it as part of its C4 contract.

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

For an implementation-only Component view, a generator can additionally require
an extracted root relation and AST metadata, then attach a visual, non-hierarchical
layer. This keeps package paths useful as selection rules without rendering them
as grouping nodes:

```json
{
  "id_prefix": "component.service",
  "parent": "container.back",
  "root_relation": "method",
  "selector": {
    "path_prefix": "back/internal/service",
    "metadata": {"language": "go", "kind": "struct"}
  },
  "layer": "Application Service",
  "visual": {"shape": "square", "color": "#D55E00"}
}
```

`source_file_regex` is available alongside `label_regex` for conventions such
as React components in `*.tsx`. The interactive explorer renders `layer` with
both colour and shape; it does not turn a layer into a C4 parent or package node.

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
  the same vis-network renderer as Graphify's `graph.html`. It opens on the
  complete Component graph. `Highlight` emphasizes a selected component or
  boundary and its direct relationships without removing the rest of the
  system; the node inspector shows the discussion metadata above. Declared
  relations are solid green arrows; code-derived evidence is orange and
  dashed.
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

## Architecture discussion protocol

For a technical architecture question, start with the complete Component graph,
then use `up`, `down`, `impact`, and edge evidence to confirm the relevant
claims. Do not create a separate, isolated topic graph: highlighting is a
navigation aid, not a filter for architectural context. Before proposing a
change, report:

1. **Graph facts** — observed and declared relationships, with evidence.
2. **Uncertainties** — unmapped, ambiguous, or suspect edges.
3. **Decision** — the boundary or dependency rule being proposed.
4. **Full-graph impact** — components, relations, and C4 rules that change.

Start at Context only for actor/external-system questions, and at Container for
cross-runtime or deployment questions. Move to Components before making a
technical design conclusion.

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
