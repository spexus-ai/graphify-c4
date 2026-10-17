"""Small bounded boolean proof for mutually exclusive Go build constraints."""

import itertools
import re


def _parse(text):
    tokens = re.findall(r"&&|\|\||!|\(|\)|[A-Za-z0-9_.]+", text)
    if "".join(tokens) != re.sub(r"\s+", "", text):
        raise ValueError("unsupported constraint")
    pos = 0

    def atom():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token == "!":
            return ("!", atom())
        if token == "(":
            value = expression()
            if tokens[pos] != ")":
                raise ValueError("missing close")
            pos += 1
            return value
        if not re.fullmatch(r"[A-Za-z0-9_.]+", token):
            raise ValueError("not a tag")
        return token

    def conjunction():
        nonlocal pos
        value = atom()
        while pos < len(tokens) and tokens[pos] == "&&":
            pos += 1
            value = ("&&", value, atom())
        return value

    def expression():
        nonlocal pos
        value = conjunction()
        while pos < len(tokens) and tokens[pos] == "||":
            pos += 1
            value = ("||", value, conjunction())
        return value

    tree = expression()
    if pos != len(tokens):
        raise ValueError("trailing constraint")
    return tree


def _value(tree, bindings):
    if isinstance(tree, str):
        return bindings[tree]
    if tree[0] == "!":
        return not _value(tree[1], bindings)
    if tree[0] == "&&":
        return _value(tree[1], bindings) and _value(tree[2], bindings)
    return _value(tree[1], bindings) or _value(tree[2], bindings)


def is_build_partition(constraints):
    """Prove exactly one definition in every tag assignment, up to 10 tags."""
    if len(constraints) < 2 or not all(constraints):
        return False
    tags = sorted(set(re.findall(r"[A-Za-z0-9_.]+", " ".join(constraints))))
    if len(tags) > 10:
        return False
    try:
        trees = [_parse(c) for c in constraints]
        return all(
            sum(_value(t, dict(zip(tags, values))) for t in trees) == 1
            for values in itertools.product((False, True), repeat=len(tags))
        )
    except (ValueError, IndexError):
        return False


def build_variant_witnesses(constraints, caller_constraint=""):
    """Prove each alternate function is uniquely selected in some valid build.

    Unlike a partition, tags may leave some assignments without a definition or
    with conflicting definitions (which Go rejects). A witness never uses such
    an assignment. This supports independent feature-tag overrides safely.
    """
    if len(constraints) < 2 or not all(constraints):
        return None
    tags = sorted(set(re.findall(r"[A-Za-z0-9_.]+", " ".join(constraints + [caller_constraint]))))
    if len(tags) > 10 or any(len(c) > 1024 for c in constraints):
        return None
    try:
        trees = [_parse(c) for c in constraints]
        caller = _parse(caller_constraint) if caller_constraint else None
        witnesses = {}
        for values in itertools.product((False, True), repeat=len(tags)):
            bindings = dict(zip(tags, values))
            if caller is not None and not _value(caller, bindings):
                continue
            selected = [i for i, t in enumerate(trees) if _value(t, bindings)]
            if len(selected) == 1:
                witnesses.setdefault(selected[0], bindings)
        return [witnesses[i] for i in range(len(trees))] if len(witnesses) == len(trees) else None
    except (ValueError, IndexError, RecursionError):
        return None
