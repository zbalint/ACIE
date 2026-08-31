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

    def add(node, qualname: str, kind: str) -> None:
        key = (qualname, kind)
        seen_counts[key] = seen_counts.get(key, 0) + 1
        ordinal = seen_counts[key] if seen_counts[key] > 1 else None
        symbols.append(
            _build_symbol(node, path=path, qualname=qualname, kind=kind, ordinal=ordinal, provenance=provenance)
        )

    for child in root.named_children:
        child = _unwrap_decorated(child)
        if child.type == "function_definition":
            add(child, _def_name(child), "function")
        elif child.type == "class_definition":
            class_name = _def_name(child)
            add(child, class_name, "class")
            body = child.child_by_field_name("body")
            for member in body.named_children:
                member = _unwrap_decorated(member)
                if member.type == "function_definition":
                    add(member, f"{class_name}.{_def_name(member)}", "method")

    return symbols


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
    node, *, path: str, qualname: str, kind: str, provenance: Provenance, ordinal: int | None = None
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
    )
