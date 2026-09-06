from importlib.metadata import version

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.ir.symbol_id import build_symbol_id

_LANGUAGE = Language(tspython.language())
_PROVENANCE_VERSION = version("tree-sitter-python")


def has_syntax_error(source_text: str) -> bool:
    """Whether tree-sitter's parse of source_text contains a syntax error.

    Tree-sitter's error recovery means a malformed parse never raises -- it
    just yields a tree with ERROR/MISSING nodes and extract_symbols degrades
    gracefully (silently extracting fewer symbols). This is the cheap,
    deterministic check the indexer (slice 7) uses to distinguish "currently
    unparseable" from "currently defines fewer symbols" before deciding
    whether to touch the live index at all.
    """
    parser = Parser(_LANGUAGE)
    tree = parser.parse(source_text.encode("utf-8"))
    return tree.root_node.has_error


def extract_symbols(path: str, source_text: str, observed_at: str) -> list[Symbol]:
    parser = Parser(_LANGUAGE)
    tree = parser.parse(source_text.encode("utf-8"))
    root = tree.root_node

    provenance = Provenance(
        provider="tree-sitter", version=_PROVENANCE_VERSION, observed_at=observed_at
    )
    module_symbol = _build_symbol(root, path=path, qualname="", kind="module", provenance=provenance)
    symbols = [module_symbol]
    seen_counts: dict[tuple[str, str], int] = {}
    import_aliases = _local_import_aliases(root)
    class_contexts = _class_contexts(root, import_aliases)

    def add(node, qualname: str, kind: str, *, is_stub: bool = False) -> None:
        key = (qualname, kind)
        seen_counts[key] = seen_counts.get(key, 0) + 1
        ordinal = seen_counts[key] if seen_counts[key] > 1 else None
        symbols.append(
            _build_symbol(
                node,
                path=path,
                qualname=qualname,
                kind=kind,
                ordinal=ordinal,
                provenance=provenance,
                is_stub=is_stub,
            )
        )

    for child in root.named_children:
        unwrapped = _unwrap_decorated(child)
        if unwrapped.type == "function_definition":
            add(unwrapped, _def_name(unwrapped), "function")
        elif unwrapped.type == "class_definition":
            class_name = _def_name(unwrapped)
            add(unwrapped, class_name, "class")
            body = unwrapped.child_by_field_name("body")
            protocol_context, abc_context = class_contexts.get(
                (unwrapped.start_point.row, unwrapped.start_point.column), (False, False)
            )
            if body is None:
                continue
            for member in body.named_children:
                definition = _unwrap_decorated(member)
                if definition.type == "function_definition":
                    add(
                        definition,
                        f"{class_name}.{_def_name(definition)}",
                        "method",
                        is_stub=_is_stub_method(
                            definition,
                            decorated_node=member,
                            protocol_context=protocol_context,
                            abc_context=abc_context,
                            import_aliases=import_aliases,
                        ),
                    )

    return symbols


def _local_import_aliases(root) -> dict[str, str]:
    """Collect file-level aliases from from-imports.

    A later module-level class or function definition shadows an alias with
    the same bound name, matching Python's sequential name binding.
    """
    aliases: dict[str, str] = {}
    definition_types = {"class_definition", "function_definition"}

    def walk(node, *, module_scope: bool) -> None:
        if node.type == "import_from_statement":
            for name_node in node.children_by_field_name("name"):
                if name_node.type != "aliased_import":
                    continue
                imported = name_node.child_by_field_name("name")
                alias = name_node.child_by_field_name("alias")
                if imported is not None and imported.type == "dotted_name" and alias is not None:
                    aliases[alias.text.decode("utf-8")] = imported.text.decode("utf-8")
        elif module_scope and node.type in definition_types:
            name = node.child_by_field_name("name")
            if name is not None:
                aliases.pop(name.text.decode("utf-8"), None)
        for child in node.named_children:
            walk(
                child,
                module_scope=module_scope and node.type not in definition_types,
            )

    walk(root, module_scope=True)
    return aliases


def _class_contexts(
    root, import_aliases: dict[str, str]
) -> dict[tuple[int, int], tuple[bool, bool]]:
    class_nodes = [
        _unwrap_decorated(child)
        for child in root.named_children
        if _unwrap_decorated(child).type == "class_definition"
    ]
    contexts = {
        (node.start_point.row, node.start_point.column): (
            "Protocol" in _base_names(node, import_aliases),
            "ABC" in _base_names(node, import_aliases) or _has_abc_meta(node, import_aliases),
        )
        for node in class_nodes
    }

    for _ in class_nodes:
        protocol_classes = {
            _def_name(node)
            for node in class_nodes
            if contexts[(node.start_point.row, node.start_point.column)][0]
        }
        abc_classes = {
            _def_name(node)
            for node in class_nodes
            if contexts[(node.start_point.row, node.start_point.column)][1]
        }
        changed = False
        for node in class_nodes:
            key = (node.start_point.row, node.start_point.column)
            protocol_context, abc_context = contexts[key]
            base_names = _base_names(node, import_aliases)
            updated = (
                protocol_context or bool(protocol_classes.intersection(base_names)),
                abc_context or bool(abc_classes.intersection(base_names)),
            )
            if updated != contexts[key]:
                contexts[key] = updated
                changed = True
        if not changed:
            break

    return contexts


def _base_names(class_node, import_aliases: dict[str, str]) -> set[str]:
    superclasses = class_node.child_by_field_name("superclasses")
    if superclasses is None:
        return set()
    names: set[str] = set()
    for base in superclasses.named_children:
        if base.type in {"keyword", "keyword_argument"}:
            continue
        name = _terminal_name(base)
        if name is not None:
            resolved_name = import_aliases.get(name)
            if resolved_name in ("Protocol", "ABC"):
                name = resolved_name
            names.add(name)
    return names


def _terminal_name(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "attribute":
        attribute = node.child_by_field_name("attribute")
        return attribute.text.decode("utf-8") if attribute is not None else None
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        return _terminal_name(value) if value is not None else None
    return None


def _has_abc_meta(class_node, import_aliases: dict[str, str]) -> bool:
    superclasses = class_node.child_by_field_name("superclasses")
    if superclasses is None:
        return False
    for argument in superclasses.named_children:
        if argument.type not in {"keyword", "keyword_argument"}:
            continue
        name = argument.child_by_field_name("name")
        value = argument.child_by_field_name("value")
        if name is None or name.text != b"metaclass" or value is None:
            continue
        value_name = _terminal_name(value)
        if import_aliases.get(value_name, value_name) == "ABCMeta":
            return True
    return False


def _is_stub_method(
    function_node,
    *,
    decorated_node,
    protocol_context: bool,
    abc_context: bool,
    import_aliases: dict[str, str],
) -> bool:
    if not _is_stub_body(function_node):
        return False
    return protocol_context or (
        abc_context and _has_abstractmethod_decorator(decorated_node, import_aliases)
    )


def _is_stub_body(function_node) -> bool:
    body = function_node.child_by_field_name("body")
    if body is None or len(body.named_children) != 1:
        return False
    statement = body.named_children[0]
    if statement.type == "pass_statement":
        return True
    if statement.type != "expression_statement" or len(statement.named_children) != 1:
        return False
    return statement.named_children[0].type in {"ellipsis", "string"}


def _has_abstractmethod_decorator(node, import_aliases: dict[str, str]) -> bool:
    if node.type != "decorated_definition":
        return False
    for decorator in node.named_children:
        if decorator.type != "decorator" or not decorator.named_children:
            continue
        expression = decorator.named_children[0]
        if expression.type == "call":
            expression = expression.child_by_field_name("function")
        if expression is not None:
            name = _terminal_name(expression)
            if import_aliases.get(name, name) == "abstractmethod":
                return True
    return False


def _unwrap_decorated(node):
    """A decorated def/class is wrapped in a `decorated_definition` node
    whose `definition` field holds the actual function_definition/
    class_definition. Unwrap it so decorated defs are extracted the same
    as undecorated ones -- the symbol's own span still excludes the
    decorator line(s).
    """
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition")
    return node


def _def_name(node) -> str:
    name_node = node.child_by_field_name("name")
    return name_node.text.decode("utf-8")


def _build_symbol(
    node,
    *,
    path: str,
    qualname: str,
    kind: str,
    provenance: Provenance,
    ordinal: int | None = None,
    is_stub: bool = False,
) -> Symbol:
    return Symbol(
        id=build_symbol_id(path=path, qualname=qualname, kind=kind, ordinal=ordinal),
        path=path,
        qualname=qualname,
        kind=kind,
        start_line=node.start_point.row + 1,
        start_col=node.start_point.column,
        end_line=node.end_point.row + 1,
        end_col=node.end_point.column,
        confidence=Confidence.EXTRACTED,
        provenance=provenance,
        is_stub=is_stub,
    )
