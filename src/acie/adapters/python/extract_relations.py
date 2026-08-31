from importlib.metadata import version

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from acie.adapters.python.extract_symbols import extract_symbols
from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol

_LANGUAGE = Language(tspython.language())
_PROVENANCE_VERSION = version("tree-sitter-python")


def extract_relations(path: str, source_text: str, observed_at: str) -> list[Relation]:
    symbols = extract_symbols(path=path, source_text=source_text, observed_at=observed_at)
    provenance = Provenance(
        provider="tree-sitter", version=_PROVENANCE_VERSION, observed_at=observed_at
    )

    parser = Parser(_LANGUAGE)
    tree = parser.parse(source_text.encode("utf-8"))
    module = next(s for s in symbols if s.kind == "module")

    top_level_by_name = _index_top_level_symbols(symbols)

    relations: list[Relation] = []
    relations.extend(_defines_relations(symbols, path=path, provenance=provenance))
    relations.extend(
        _import_relations(tree.root_node, module=module, path=path, provenance=provenance)
    )
    relations.extend(
        _inherits_relations(
            tree.root_node, symbols, top_level_by_name=top_level_by_name, path=path, provenance=provenance
        )
    )
    relations.extend(
        _call_and_reference_relations(
            tree.root_node, symbols, top_level_by_name=top_level_by_name, module=module, path=path, provenance=provenance
        )
    )
    return relations


def _call_and_reference_relations(
    root,
    symbols: list[Symbol],
    *,
    top_level_by_name: dict[str, list[Symbol]],
    module: Symbol,
    path: str,
    provenance: Provenance,
) -> list[Relation]:
    """Unqualified `name(...)` calls and bare-name assignment-RHS references
    only (this slice's narrow first cut, per the confirmed "Call/ref forms"
    scope) -- `self.foo()`, `obj.attr()`, and other attribute-access forms
    are explicitly deferred, since their relevant field is an `attribute`
    node, not a plain `identifier`; a bare name passed as a call argument or
    returned is likewise deferred, since only assignment RHS is checked.

    Scope tracking is limited to the same containers extract_symbols itself
    recognizes: module, top-level function, and method (a function directly
    inside a class body). A site's `source` is the innermost of those it's
    lexically inside; nested-function bodies (not their own symbols yet,
    see cb1caf24) keep attributing to their nearest tracked container.

    calls and references share this one recursive walk (rather than two
    separate walks each re-tracking scope) since both need the identical
    current_source bookkeeping.
    """
    by_position = _symbol_by_position(symbols)
    relations: list[Relation] = []

    def resolve(name_node, *, current_source: Symbol, predicate: str) -> None:
        candidates = top_level_by_name.get(name_node.text.decode("utf-8"), [])
        if not candidates:
            return
        confidence = Confidence.EXTRACTED if len(candidates) == 1 else Confidence.AMBIGUOUS
        for candidate in candidates:
            relations.append(
                Relation(
                    source=current_source.id,
                    target=candidate.id,
                    predicate=predicate,
                    site_file=path,
                    site_line=name_node.start_point.row + 1,
                    site_col=name_node.start_point.column,
                    confidence=confidence,
                    provenance=provenance,
                )
            )

    def walk(node, current_source: Symbol) -> None:
        if node.type == "function_definition":
            here = by_position.get((node.start_point.row + 1, node.start_point.column))
            current_source = here if here is not None else current_source
        elif node.type == "call":
            function_node = node.child_by_field_name("function")
            if function_node is not None and function_node.type == "identifier":
                resolve(function_node, current_source=current_source, predicate="calls")
        elif node.type == "assignment":
            right_node = node.child_by_field_name("right")
            if right_node is not None and right_node.type == "identifier":
                resolve(right_node, current_source=current_source, predicate="references")
        for child in node.named_children:
            walk(child, current_source)

    walk(root, module)
    return relations


def _index_top_level_symbols(symbols: list[Symbol]) -> dict[str, list[Symbol]]:
    """Bare (dot-free) qualname -> candidate symbols, for resolving an
    unqualified name reference against this file's own top-level scope.
    Multiple entries at the same name (a redefinition collision) is how
    ARCHITECTURE.md's "ambiguous multi-target relations" case naturally
    falls out: one candidate means EXTRACTED, more than one means every
    candidate gets its own AMBIGUOUS edge.
    """
    index: dict[str, list[Symbol]] = {}
    for sym in symbols:
        if sym.kind in ("function", "class") and "." not in sym.qualname:
            index.setdefault(sym.qualname, []).append(sym)
    return index


def _symbol_by_position(symbols: list[Symbol]) -> dict[tuple[int, int], Symbol]:
    return {(s.start_line, s.start_col): s for s in symbols}


def _inherits_relations(
    root,
    symbols: list[Symbol],
    *,
    top_level_by_name: dict[str, list[Symbol]],
    path: str,
    provenance: Provenance,
) -> list[Relation]:
    """Top-level `class Foo(Base): ...` only (this slice's narrow first cut,
    matching extract_symbols's own top-level-only scope) -- decorated
    classes, keyword base-class args like `metaclass=M` (correctly skipped,
    not an identifier), and multi-level nesting are out of scope here.
    """
    by_position = _symbol_by_position(symbols)
    class_candidates_by_name = {
        name: [s for s in candidates if s.kind == "class"]
        for name, candidates in top_level_by_name.items()
    }

    relations = []
    for child in root.named_children:
        if child.type != "class_definition":
            continue
        source_symbol = by_position.get((child.start_point.row + 1, child.start_point.column))
        superclasses = child.child_by_field_name("superclasses")
        if source_symbol is None or superclasses is None:
            continue
        for base in superclasses.named_children:
            if base.type != "identifier":
                continue
            candidates = class_candidates_by_name.get(base.text.decode("utf-8"), [])
            if not candidates:
                continue
            confidence = Confidence.EXTRACTED if len(candidates) == 1 else Confidence.AMBIGUOUS
            for candidate in candidates:
                relations.append(
                    Relation(
                        source=source_symbol.id,
                        target=candidate.id,
                        predicate="inherits",
                        site_file=path,
                        site_line=base.start_point.row + 1,
                        site_col=base.start_point.column,
                        confidence=confidence,
                        provenance=provenance,
                    )
                )
    return relations


def _import_relations(root, *, module: Symbol, path: str, provenance: Provenance) -> list[Relation]:
    """Module-level `import <dotted_name>` and `from <dotted_name> import
    <dotted_name>` statements only (this slice's narrow first cut) --
    aliased imports (`import x as y`) and relative imports (`from . import
    x`) are explicitly deferred, see the extract_relations completion
    memory.
    """
    relations = []
    for child in root.named_children:
        if child.type == "import_statement":
            name_node = child.child_by_field_name("name")
            if name_node.type != "dotted_name":
                continue
            target = name_node.text.decode("utf-8")
        elif child.type == "import_from_statement":
            module_node = child.child_by_field_name("module_name")
            name_node = child.child_by_field_name("name")
            if module_node.type != "dotted_name" or name_node.type != "dotted_name":
                continue
            target = f"{module_node.text.decode('utf-8')}.{name_node.text.decode('utf-8')}"
        else:
            continue
        relations.append(
            Relation(
                source=module.id,
                target=target,
                predicate="imports",
                site_file=path,
                site_line=child.start_point.row + 1,
                site_col=child.start_point.column,
                confidence=Confidence.EXTRACTED,
                provenance=provenance,
            )
        )
    return relations


def _defines_relations(
    symbols: list[Symbol], *, path: str, provenance: Provenance
) -> list[Relation]:
    """A symbol's containing module/class "defines" it.

    Derived purely from the qualname structure extract_symbols already
    produced -- no dot means top-level (contained by the module, qualname
    ""); one dot means a method (contained by its class, whose qualname is
    the part before the last dot). This mirrors the containment slice 3's
    extraction traversal already establishes, without re-walking the tree.
    """
    module = next(s for s in symbols if s.kind == "module")
    by_class_qualname = {s.qualname: s for s in symbols if s.kind == "class"}

    relations = []
    for sym in symbols:
        if sym.kind == "module":
            continue
        if "." in sym.qualname:
            container = by_class_qualname[sym.qualname.rsplit(".", 1)[0]]
        else:
            container = module
        relations.append(
            Relation(
                source=container.id,
                target=sym.id,
                predicate="defines",
                site_file=path,
                site_line=sym.start_line,
                site_col=sym.start_col,
                confidence=Confidence.EXTRACTED,
                provenance=provenance,
            )
        )
    return relations
