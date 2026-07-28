"""Go extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations


from pathlib import Path
from graphify.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text


_GO_PREDECLARED_TYPES = frozenset({
    "bool", "byte", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr", "any", "comparable",
})


def _go_type_id(package_scope: str, name: str) -> str:
    """Return an ID that preserves Go's exported/unexported type distinction.

    Graphify IDs are intentionally case-folded for cross-language matching, but
    Go treats ``RoleService`` and ``roleService`` as distinct symbols.  Only the
    latter needs a stable suffix to avoid collapsing that valid pair while
    retaining existing IDs for the overwhelmingly common exported declaration.
    """
    if name and not name[0].isupper():
        return _make_id(package_scope, name, "unexported")
    return _make_id(package_scope, name)

def _go_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Go type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text and text not in _GO_PREDECLARED_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_type":
        # Keep the package qualifier.  Reducing ``http.Client`` to ``Client``
        # lets the corpus-level stub rewire bind it to any project-local Client
        # declaration (for example a Sentry client), which fabricates a dependency.
        text = _read_text(node, source)
        if text and text not in _GO_PREDECLARED_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        type_field = node.child_by_field_name("type")
        if type_field is not None:
            sub: list[tuple[str, str]] = []
            _go_collect_type_refs(type_field, source, generic, sub)
            out.extend(sub)
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _go_collect_type_refs(arg, source, True, out)
        return
    if t in ("pointer_type", "slice_type", "array_type", "map_type",
             "channel_type", "parenthesized_type"):
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)

def extract_go(path: Path) -> dict:
    """Extract functions, methods, type declarations, and imports from a .go file."""
    try:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-go not installed"}

    try:
        language = Language(tsgo.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    # Use directory name as package scope so methods on the same type across
    # multiple files in a package share one canonical type node.
    pkg_scope = path.parent.name or stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []
    go_imported_pkgs: dict[str, str] = {}  # local package name/alias -> import path
    raw_type_refs: list[dict] = []
    # A dependency-injection setup often constructs an implementation through a
    # factory, stores it in a local, and then registers it with another factory
    # product.  Keep this small structural trace for the corpus pass, where both
    # factory return types can be resolved across files in the Go package.
    raw_registrations: list[dict] = []
    # A method name is not unique in Go: two receiver types may both expose
    # Validate().  Calls are resolved after declarations have been collected,
    # keyed by the receiver's static type rather than the bare method name.
    method_nids_by_receiver: dict[str, dict[str, str]] = {}

    def add_node(
        nid: str, label: str, line: int, metadata: dict[str, object] | None = None,
    ) -> None:
        if nid in seen_ids:
            # A type reference may have created a sourceless placeholder before
            # the declaration is visited. Promote it to the real declaration.
            for item in nodes:
                if item["id"] == nid:
                    if not item.get("source_file"):
                        item["source_file"] = str_path
                        item["source_location"] = f"L{line}"
                    if metadata:
                        item["metadata"] = metadata
                    return
            return
        seen_ids.add(nid)
        item = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        }
        if metadata:
            item["metadata"] = metadata
        nodes.append(item)

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None, resolution: str = "same_file") -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
            "resolution": resolution,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    # Tree-sitter visits declarations in source order, while methods can appear
    # before the type declaration that owns them. Pre-scan type kinds so a
    # receiver node receives its semantic kind even in that ordering.
    declared_type_kinds: dict[str, str] = {}
    error_receiver_types: set[str] = set()

    def collect_type_kinds(node) -> None:
        if node.type == "type_declaration":
            for child in node.children:
                if child.type != "type_spec":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                type_kind = next(
                    (candidate.type for candidate in child.children if candidate.type in {"struct_type", "interface_type"}),
                    None,
                )
                if type_kind is not None:
                    declared_type_kinds[_read_text(name_node, source)] = type_kind.removesuffix("_type")
        if node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            receiver = node.child_by_field_name("receiver")
            if name_node is not None and receiver is not None and _read_text(name_node, source) == "Error":
                parameter = next(
                    (child for child in receiver.children if child.type == "parameter_declaration"),
                    None,
                )
                type_node = parameter.child_by_field_name("type") if parameter is not None else None
                if type_node is not None:
                    receiver_type = _read_text(type_node, source).lstrip("*").strip()
                    if receiver_type:
                        error_receiver_types.add(receiver_type)
        for child in node.children:
            collect_type_kinds(child)

    collect_type_kinds(root)

    def type_metadata(name: str) -> dict[str, object] | None:
        type_kind = declared_type_kinds.get(name)
        if type_kind is None:
            return None
        if path.name.endswith("_test.go"):
            architecture_role = "test_support"
        elif name in error_receiver_types or name.lower().endswith("error"):
            architecture_role = "error"
        elif name.endswith(("Request", "Response", "Input", "Output", "Params", "Options", "Payload")):
            architecture_role = "contract"
        elif name.endswith(("Result", "Outcome", "State", "Details", "Info")):
            architecture_role = "value"
        else:
            architecture_role = "runtime_actor"
        return {
            "language": "go",
            "kind": type_kind,
            "visibility": "exported" if name and name[0].isupper() else "unexported",
            "architecture_role": architecture_role,
        }

    def ensure_named_node(name: str, line: int) -> str:
        nid = _go_type_id(pkg_scope, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't declared in this file, so this is a cross-file reference
            # (e.g. a type defined in another file of the package). Emit a SOURCELESS
            # stub — like the inheritance-base path in the other extractors — so the
            # corpus-level rewire can collapse it onto the real definition. A sourced
            # stub here makes _disambiguate_colliding_node_ids bake the referencing
            # file's path (with extension) into the id and blocks the rewire, which is
            # the phantom-duplicate-node bug (#1402).
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
                # A package-qualified type (http.Client) is external unless a
                # language-aware resolver proves otherwise.  The generic
                # sourceless-stub rewire is intentionally name-based and must
                # never turn it into a project-local Client declaration.
                "_qualified_ref": "." in name,
            })
        return nid

    def emit_type_ref(
        source_nid: str,
        ref_name: str,
        role: str,
        relation: str,
        line: int,
        context: str | None = None,
    ) -> None:
        """Emit a local type edge or defer a package-qualified one for proof."""
        qualifier, separator, type_name = ref_name.partition(".")
        if separator and qualifier in go_imported_pkgs and type_name:
            raw_type_refs.append({
                "source_nid": source_nid,
                "type_name": type_name,
                "relation": relation,
                "context": context,
                "language": "go",
                "package_qualified": True,
                "qualifier": qualifier,
                "import_path": go_imported_pkgs[qualifier],
                "source_file": str_path,
                "source_location": f"L{line}",
            })
            return
        target_nid = ensure_named_node(ref_name, line)
        if target_nid != source_nid:
            add_edge(source_nid, target_nid, relation, line, context=context)

    def emit_go_method_refs(func_node, func_nid: str, line: int) -> None:
        params = func_node.child_by_field_name("parameters")
        if params is not None:
            for p in params.children:
                if p.type != "parameter_declaration":
                    continue
                type_node = p.child_by_field_name("type")
                refs: list[tuple[str, str]] = []
                _go_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    emit_type_ref(func_nid, ref_name, role, "references", line, ctx)
        result = func_node.child_by_field_name("result")
        if result is not None:
            if result.type == "parameter_list":
                for p in result.children:
                    if p.type != "parameter_declaration":
                        continue
                    type_node = p.child_by_field_name("type")
                    if type_node is None:
                        for c in p.children:
                            if c.is_named:
                                type_node = c
                                break
                    refs = []
                    _go_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        emit_type_ref(func_nid, ref_name, role, "references", line, ctx)
            else:
                refs = []
                _go_collect_type_refs(result, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "return_type"
                    emit_type_ref(func_nid, ref_name, role, "references", line, ctx)

    def emit_factory_construction_edge(func_node, func_nid: str, func_name: str) -> None:
        """Link a conventional ``New…`` factory to its direct concrete return.

        A factory may expose an interface as its declared return type, while its
        ``return &Concrete{…}`` expression is exact evidence of the runtime
        implementation.  This is deliberately limited to direct composite
        literal returns; forwarding/wrapping factories remain unresolved.
        """
        if not func_name.startswith("New"):
            return

        def visit(candidate) -> None:
            if candidate.type == "return_statement":
                expressions = list(candidate.children)
                while expressions:
                    expression = expressions.pop()
                    if expression.type == "unary_expression":
                        operand = expression.child_by_field_name("operand")
                        if operand is not None and operand.type == "composite_literal":
                            type_node = operand.child_by_field_name("type")
                            type_name = _read_text(type_node, source).strip() if type_node else ""
                            if type_name and "." not in type_name:
                                target_nid = ensure_named_node(type_name, candidate.start_point[0] + 1)
                                if target_nid != func_nid:
                                    add_edge(
                                        func_nid, target_nid, "constructs",
                                        candidate.start_point[0] + 1,
                                        context="factory_return",
                                    )
                    else:
                        expressions.extend(expression.children)
                return
            for child in candidate.children:
                visit(child)

        body = func_node.child_by_field_name("body")
        if body is not None:
            visit(body)

    def walk(node) -> None:
        t = node.type

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
                emit_go_method_refs(node, func_nid, line)
                emit_factory_construction_edge(node, func_nid, func_name)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            receiver_type: str | None = None
            if receiver:
                for param in receiver.children:
                    if param.type == "parameter_declaration":
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            receiver_type = _read_text(type_node, source).lstrip("*").strip()
                        break
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            method_name = _read_text(name_node, source)
            line = node.start_point[0] + 1

            if receiver_type:
                parent_nid = _go_type_id(pkg_scope, receiver_type)
                add_node(parent_nid, receiver_type, line, type_metadata(receiver_type))
                method_nid = _make_id(parent_nid, method_name)
                add_node(method_nid, f".{method_name}()", line)
                add_edge(parent_nid, method_nid, "method", line)
                method_nids_by_receiver.setdefault(receiver_type, {})[method_name] = method_nid
            else:
                method_nid = _make_id(stem, method_name)
                add_node(method_nid, f"{method_name}()", line)
                add_edge(file_nid, method_nid, "contains", line)

            emit_go_method_refs(node, method_nid, line)
            body = node.child_by_field_name("body")
            if body:
                function_bodies.append((method_nid, body))
            return

        if t == "type_declaration":
            for child in node.children:
                if child.type != "type_spec":
                    continue
                name_node = child.child_by_field_name("name")
                if not name_node:
                    continue
                type_name = _read_text(name_node, source)
                line = child.start_point[0] + 1
                type_nid = _go_type_id(pkg_scope, type_name)
                add_node(type_nid, type_name, line, type_metadata(type_name))
                add_edge(file_nid, type_nid, "contains", line)
                # Type body: struct fields (with embeds) or interface embedding.
                type_body = None
                for tc in child.children:
                    if tc.type in ("struct_type", "interface_type"):
                        type_body = tc
                        break
                if type_body is None:
                    continue
                if type_body.type == "struct_type":
                    for fdl in type_body.children:
                        if fdl.type != "field_declaration_list":
                            continue
                        for field in fdl.children:
                            if field.type != "field_declaration":
                                continue
                            has_name = any(
                                fc.type == "field_identifier" for fc in field.children
                            )
                            type_node = field.child_by_field_name("type")
                            if type_node is None:
                                for fc in field.children:
                                    if fc.is_named and fc.type != "field_identifier":
                                        type_node = fc
                                        break
                            refs: list[tuple[str, str]] = []
                            _go_collect_type_refs(type_node, source, False, refs)
                            for ref_name, role in refs:
                                if not has_name and role == "type":
                                    emit_type_ref(
                                        type_nid, ref_name, role, "embeds",
                                        field.start_point[0] + 1,
                                    )
                                else:
                                    ctx = "generic_arg" if role == "generic_arg" else "field"
                                    emit_type_ref(
                                        type_nid, ref_name, role, "references",
                                        field.start_point[0] + 1, ctx,
                                    )
                elif type_body.type == "interface_type":
                    for elem in type_body.children:
                        if elem.type != "type_elem":
                            continue
                        refs = []
                        for sub in elem.children:
                            if sub.is_named:
                                _go_collect_type_refs(sub, source, False, refs)
                        for ref_name, role in refs:
                            if role == "type":
                                emit_type_ref(type_nid, ref_name, role, "embeds", elem.start_point[0] + 1)
                            else:
                                emit_type_ref(
                                    type_nid, ref_name, role, "references",
                                    elem.start_point[0] + 1, "generic_arg",
                                )
            return

        if t == "import_declaration":
            for child in node.children:
                if child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            if path_node:
                                raw = _read_text(path_node, source).strip('"')
                                # Prefix with go_pkg_ so stdlib names (e.g. "context")
                                # don't collide with local files of the same basename.
                                tgt_nid = _make_id("go", "pkg", raw)
                                add_edge(file_nid, tgt_nid, "imports_from", spec.start_point[0] + 1, context="import")
                                # Track local name (alias or last path segment)
                                alias = spec.child_by_field_name("name")
                                local_name = _read_text(alias, source) if alias else raw.split("/")[-1]
                                if local_name and local_name != "_" and local_name != ".":
                                    go_imported_pkgs[local_name] = raw
                elif child.type == "import_spec":
                    path_node = child.child_by_field_name("path")
                    if path_node:
                        raw = _read_text(path_node, source).strip('"')
                        tgt_nid = _make_id("go", "pkg", raw)
                        add_edge(file_nid, tgt_nid, "imports_from", child.start_point[0] + 1, context="import")
                        alias = child.child_by_field_name("name")
                        local_name = _read_text(alias, source) if alias else raw.split("/")[-1]
                        if local_name and local_name != "_" and local_name != ".":
                            go_imported_pkgs[local_name] = raw
            return

        for child in node.children:
            walk(child)

    walk(root)

    label_to_nid: dict[str, str] = {}
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def local_variable_types(body_node) -> dict[str, str]:
        """Collect explicit ``var name Type`` declarations in a function body.

        This intentionally avoids general Go type inference.  Explicit local
        declarations are unambiguous evidence that the enclosing function
        consumes a DTO/type and provide the static receiver type necessary to
        resolve calls such as ``request.Validate()``.
        """
        result: dict[str, str] = {}

        def visit(node) -> None:
            if node.type == "var_spec":
                names = [child for child in node.children if child.type == "identifier"]
                type_node = node.child_by_field_name("type")
                if type_node is None:
                    type_node = next(
                        (child for child in node.children if child.is_named and child.type != "identifier"),
                        None,
                    )
                refs: list[tuple[str, str]] = []
                _go_collect_type_refs(type_node, source, False, refs)
                if refs:
                    for name_node in names:
                        result[_read_text(name_node, source)] = refs[0][0]
            for child in node.children:
                visit(child)

        visit(body_node)
        return result

    def emit_local_variable_type_refs(caller_nid: str, body_node) -> dict[str, str]:
        local_types = local_variable_types(body_node)
        for ref_name in set(local_types.values()):
            emit_type_ref(
                caller_nid,
                ref_name,
                "type",
                "references",
                body_node.start_point[0] + 1,
                "local_variable_type",
            )
        return local_types

    def emit_factory_registration_trace(caller_nid: str, body_node) -> None:
        """Record proven Go factory-to-registry wiring for the corpus resolver.

        Only a direct ``name := NewFactory(...)`` binding followed by
        ``registry.Register…(name)`` qualifies.  This avoids guessing from
        interface conformance or a matching type name.
        """
        local_factories: dict[str, tuple[str, int]] = {}

        def direct_factory_name(expression) -> str | None:
            if expression is None or expression.type != "call_expression":
                return None
            function = expression.child_by_field_name("function")
            if function is None or function.type != "identifier":
                return None
            name = _read_text(function, source)
            return name if name.startswith("New") else None

        def expression_list_items(node) -> list:
            return [child for child in node.children if child.is_named] if node is not None else []

        def visit(node) -> None:
            if node.type in ("function_declaration", "method_declaration"):
                return
            if node.type == "short_var_declaration":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                names = expression_list_items(left)
                values = expression_list_items(right)
                if len(names) == 1 and len(values) == 1 and names[0].type == "identifier":
                    factory = direct_factory_name(values[0])
                    if factory:
                        local_factories[_read_text(names[0], source)] = (
                            factory, node.start_point[0] + 1,
                        )
            elif node.type == "call_expression":
                function = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                if function is not None and function.type == "selector_expression":
                    field = function.child_by_field_name("field")
                    receiver = function.child_by_field_name("operand")
                    argument_values = expression_list_items(arguments)
                    method = _read_text(field, source) if field is not None else ""
                    receiver_name = _read_text(receiver, source) if receiver is not None else ""
                    provider_name = (
                        _read_text(argument_values[0], source)
                        if len(argument_values) == 1 and argument_values[0].type == "identifier"
                        else ""
                    )
                    registry_factory = local_factories.get(receiver_name)
                    provider_factory = local_factories.get(provider_name)
                    if (
                        method.startswith("Register")
                        and registry_factory is not None
                        and provider_factory is not None
                        and registry_factory[1] < node.start_point[0] + 1
                        and provider_factory[1] < node.start_point[0] + 1
                    ):
                        raw_registrations.append({
                            "caller_nid": caller_nid,
                            "registry_factory": registry_factory[0],
                            "registered_factory": provider_factory[0],
                            "method": method,
                            "language": "go",
                            "source_file": str_path,
                            "source_location": f"L{node.start_point[0] + 1}",
                        })
            for child in node.children:
                visit(child)

        visit(body_node)

    def walk_calls(node, caller_nid: str, local_types: dict[str, str]) -> None:
        if node.type in ("function_declaration", "method_declaration"):
            return
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            callee_name: str | None = None
            is_member_call: bool = False
            receiver_name = ""
            if func_node:
                if func_node.type == "identifier":
                    callee_name = _read_text(func_node, source)
                elif func_node.type == "selector_expression":
                    field = func_node.child_by_field_name("field")
                    operand = func_node.child_by_field_name("operand")
                    receiver_name = _read_text(operand, source) if operand else ""
                    # Package-qualified call (e.g. fmt.Println) → allow cross-file resolution.
                    # Receiver method call (e.g. s.logger.Log) → skip, no import evidence.
                    is_member_call = receiver_name not in go_imported_pkgs
                    if field:
                        callee_name = _read_text(field, source)
            if callee_name and callee_name not in _LANGUAGE_BUILTIN_GLOBALS:
                # A qualified selector is governed by its import, not by a
                # same-named declaration somewhere else in the repository.
                # Leave it for the global resolver, which can prove a local
                # import path or deliberately leave stdlib/external calls
                # unresolved.
                if receiver_name in go_imported_pkgs:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": False,
                        "language": "go",
                        "package_qualified": True,
                        "qualifier": receiver_name,
                        "import_path": go_imported_pkgs[receiver_name],
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
                    for child in node.children:
                        walk_calls(child, caller_nid, local_types)
                    return
                tgt_nid = None
                if is_member_call:
                    # Do not fall back to a global same-name method: that
                    # fabricates edges when several types implement it.  A
                    # local ``var req createProjectRequest`` gives us proof.
                    receiver_type = local_types.get(receiver_name)
                    if receiver_type:
                        tgt_nid = method_nids_by_receiver.get(receiver_type, {}).get(callee_name)
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "call",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                            "resolution": "same_file",
                        })
                elif callee_name:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "language": "go",
                        "package_qualified": receiver_name in go_imported_pkgs,
                        "qualifier": receiver_name if receiver_name in go_imported_pkgs else "",
                        "import_path": go_imported_pkgs.get(receiver_name, ""),
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
        for child in node.children:
            walk_calls(child, caller_nid, local_types)

    for caller_nid, body_node in function_bodies:
        local_types = emit_local_variable_type_refs(caller_nid, body_node)
        emit_factory_registration_trace(caller_nid, body_node)
        walk_calls(body_node, caller_nid, local_types)

    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {
        "nodes": nodes,
        "edges": clean_edges,
        "raw_calls": raw_calls,
        "raw_type_refs": raw_type_refs,
        "raw_registrations": raw_registrations,
    }
