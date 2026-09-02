from importlib.metadata import version

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from acie.adapters.python.extract_symbols import extract_symbols
from acie.ir.relation import DeferredImportCall, Relation
from acie.ir.symbol import Confidence, Provenance, Symbol

_LANGUAGE = Language(tspython.language())
_PROVENANCE_VERSION = version("tree-sitter-python")


def extract_relations(path: str, source_text: str, observed_at: str) -> list[Relation]:
    """Same-file relations only -- see extract_relations_with_deferred_calls
    for the sibling entry point that also surfaces cross-file-candidate
    calls this pure, single-file function cannot itself resolve.
    """
    relations, _deferred = _extract(path=path, source_text=source_text, observed_at=observed_at)
    return relations


def extract_relations_with_deferred_calls(
    path: str, source_text: str, observed_at: str
) -> tuple[list[Relation], list[DeferredImportCall]]:
    """Like extract_relations, but also returns bare-identifier calls whose
    name resolves to no same-file symbol yet *is* imported in this file --
    candidates for cross-file resolution against the repo-wide symbol index,
    which only indexer.py (not this pure, single-file function) can do. See
    DeferredImportCall.
    """
    return _extract(path=path, source_text=source_text, observed_at=observed_at)


def _extract(
    path: str, source_text: str, observed_at: str
) -> tuple[list[Relation], list[DeferredImportCall]]:
    symbols = extract_symbols(path=path, source_text=source_text, observed_at=observed_at)
    provenance = Provenance(
        provider="tree-sitter", version=_PROVENANCE_VERSION, observed_at=observed_at
    )

    parser = Parser(_LANGUAGE)
    tree = parser.parse(source_text.encode("utf-8"))
    module = next(s for s in symbols if s.kind == "module")

    top_level_by_name = _index_top_level_symbols(symbols)
    methods_by_class = _index_methods_by_class(symbols)
    import_relations, import_alias_map = _import_relations(
        tree.root_node, module=module, path=path, provenance=provenance
    )
    call_relations, deferred_calls = _call_and_reference_relations(
        tree.root_node,
        symbols,
        top_level_by_name=top_level_by_name,
        methods_by_class=methods_by_class,
        import_alias_map=import_alias_map,
        module=module,
        path=path,
        provenance=provenance,
    )

    relations: list[Relation] = []
    relations.extend(_defines_relations(symbols, path=path, provenance=provenance))
    relations.extend(import_relations)
    relations.extend(
        _inherits_relations(
            tree.root_node, symbols, top_level_by_name=top_level_by_name, path=path, provenance=provenance
        )
    )
    relations.extend(
        _overrides_relations(
            tree.root_node,
            top_level_by_name=top_level_by_name,
            methods_by_class=methods_by_class,
            path=path,
            provenance=provenance,
        )
    )
    relations.extend(call_relations)
    return relations, deferred_calls


def _call_and_reference_relations(
    root,
    symbols: list[Symbol],
    *,
    top_level_by_name: dict[str, list[Symbol]],
    methods_by_class: dict[str, dict[str, list[Symbol]]],
    import_alias_map: dict[str, str],
    module: Symbol,
    path: str,
    provenance: Provenance,
) -> tuple[list[Relation], list[DeferredImportCall]]:
    """Unqualified `name(...)` calls, `self.name(...)` calls, and bare-name
    assignment-RHS references only (this slice's cut). `self.foo()` is
    deterministically resolvable with tree-sitter alone -- `self` always
    names the enclosing class's own instance, no type inference needed --
    so it's in scope; `obj.attr()`/`ClassName.method()` and any other
    attribute-access form on something other than literal `self` require
    real type/reference resolution and stay explicitly deferred. A bare
    name passed as a call argument or returned is likewise deferred, since
    only assignment RHS is checked.

    A bare call whose name matches neither top_level_by_name nor
    methods_by_class, but *is* a `from`-imported name (import_alias_map),
    produces a DeferredImportCall instead of silently vanishing -- indexer.py
    resolves it against the repo-wide symbol index, which this pure,
    single-file function has no access to. Only `calls` gets this treatment,
    matching the plan's "bare `name(...)` calls" scope -- `references`
    (assignment RHS) is untouched.

    Scope tracking is limited to the same containers extract_symbols itself
    recognizes: module, top-level function, and method (a function directly
    inside a class body). A site's `source` is the innermost of those it's
    lexically inside; nested-function bodies (not their own symbols yet,
    see cb1caf24) keep attributing to their nearest tracked container.
    Class-scope tracking (current_class) is threaded alongside current_source
    purely to know which class's methods `self.` refers to; it resets to
    None once walk() descends into a sibling top-level definition.

    calls and references share this one recursive walk (rather than two
    separate walks each re-tracking scope) since both need the identical
    current_source/current_class bookkeeping.
    """
    by_position = _symbol_by_position(symbols)
    relations: list[Relation] = []
    deferred: list[DeferredImportCall] = []

    def resolve(name_node, *, source: Symbol, candidates: list[Symbol], predicate: str) -> None:
        if not candidates:
            return
        confidence = Confidence.EXTRACTED if len(candidates) == 1 else Confidence.AMBIGUOUS
        for candidate in candidates:
            relations.append(
                Relation(
                    source=source.id,
                    target=candidate.id,
                    predicate=predicate,
                    site_file=path,
                    site_line=name_node.start_point.row + 1,
                    site_col=name_node.start_point.column,
                    confidence=confidence,
                    provenance=provenance,
                )
            )

    def walk(node, current_source: Symbol, current_class: Symbol | None) -> None:
        if node.type == "class_definition":
            here = by_position.get((node.start_point.row + 1, node.start_point.column))
            current_class = here if here is not None else current_class
        elif node.type == "function_definition":
            here = by_position.get((node.start_point.row + 1, node.start_point.column))
            current_source = here if here is not None else current_source
        elif node.type == "call":
            function_node = node.child_by_field_name("function")
            if function_node is not None and function_node.type == "identifier":
                name = function_node.text.decode("utf-8")
                candidates = top_level_by_name.get(name, [])
                if candidates:
                    resolve(function_node, source=current_source, candidates=candidates, predicate="calls")
                elif name in import_alias_map:
                    deferred.append(
                        DeferredImportCall(
                            source=current_source.id,
                            module_path=import_alias_map[name],
                            name=name,
                            site_file=path,
                            site_line=function_node.start_point.row + 1,
                            site_col=function_node.start_point.column,
                            provenance=provenance,
                        )
                    )
            elif function_node is not None and function_node.type == "attribute":
                object_node = function_node.child_by_field_name("object")
                attribute_node = function_node.child_by_field_name("attribute")
                if (
                    object_node is not None
                    and object_node.type == "identifier"
                    and object_node.text == b"self"
                    and attribute_node is not None
                    and current_class is not None
                ):
                    candidates = methods_by_class.get(current_class.qualname, {}).get(
                        attribute_node.text.decode("utf-8"), []
                    )
                    resolve(attribute_node, source=current_source, candidates=candidates, predicate="calls")
        elif node.type == "assignment":
            right_node = node.child_by_field_name("right")
            if right_node is not None and right_node.type == "identifier":
                candidates = top_level_by_name.get(right_node.text.decode("utf-8"), [])
                resolve(right_node, source=current_source, candidates=candidates, predicate="references")
        for child in node.named_children:
            walk(child, current_source, current_class)

    walk(root, module, None)
    return relations, deferred


def _index_methods_by_class(symbols: list[Symbol]) -> dict[str, dict[str, list[Symbol]]]:
    """class qualname -> {method name -> candidate method symbols}, for
    resolving `self.<method>(...)` calls against the enclosing class's own
    methods. Mirrors `_index_top_level_symbols`'s multi-candidate-on-
    collision shape (a redefined method name yields >1 candidate, resolved
    as AMBIGUOUS by `resolve()`), just keyed one level deeper (by class,
    then by bare method name) since methods carry a dotted qualname that
    `_index_top_level_symbols` deliberately excludes.
    """
    index: dict[str, dict[str, list[Symbol]]] = {}
    for sym in symbols:
        if sym.kind != "method" or "." not in sym.qualname:
            continue
        class_qualname, method_name = sym.qualname.rsplit(".", 1)
        index.setdefault(class_qualname, {}).setdefault(method_name, []).append(sym)
    return index


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


def _within_span(symbol: Symbol, node) -> bool:
    """Whether symbol's start position falls within node's [start, end] span
    (row,col tuple comparison -- same (line, col) ordering _symbol_by_position
    already keys on elsewhere in this module). Used by _overrides_relations
    to scope a qualname-keyed methods_by_class lookup down to only the
    methods physically defined inside one specific class_definition node.
    """
    start = (node.start_point.row + 1, node.start_point.column)
    end = (node.end_point.row + 1, node.end_point.column)
    pos = (symbol.start_line, symbol.start_col)
    return start <= pos <= end


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


def _overrides_relations(
    root,
    *,
    top_level_by_name: dict[str, list[Symbol]],
    methods_by_class: dict[str, dict[str, list[Symbol]]],
    path: str,
    provenance: Provenance,
) -> list[Relation]:
    """Top-level `class Foo(Base): def bar(...)` where an immediate base
    (same scope as _inherits_relations resolves -- same-file only, this
    slice's narrow first cut) already defines a method of the same name.
    `overrides` points at the immediate overridden base method, not the
    transitive root -- multi-level chains fall out for free via BFS hopping
    through the edges (impact_analysis, a later slice).

    Ambiguity is computed per (subclass, method_name) pair over the *union*
    of every immediate base's matching method candidates -- not per base
    independently -- because Python's MRO would pick exactly one candidate
    deterministically, but tree-sitter alone cannot compute MRO linearization
    across multiple bases; more than one candidate anywhere in that union
    means the override target is genuinely ambiguous without semantic
    resolution (later upgraded to INFERRED by pyright enrichment).

    Base qualnames are deduplicated via a set before the methods_by_class
    lookup: a redefined base class name (e.g. two `class Base:` definitions)
    produces multiple same-qualname Symbol candidates, but methods_by_class
    is itself keyed by qualname text (not id), so it already folds their
    methods into one shared candidate list -- iterating per Symbol instead
    of per unique qualname would double-count that list.

    On the SUBCLASS side, the opposite care is needed (agy/gemini review
    finding, 2026-09-02): methods_by_class's own qualname-text keying means
    a *redefined subclass name* (e.g. an unrelated earlier `class Foo:` with
    no base at all, followed later by `class Foo(Base):`) would otherwise
    merge both occurrences' methods together, letting the first Foo's bar
    leak in as a spurious override source when processing the second,
    unrelated Foo(Base) node. `own_methods` is therefore scoped down to only
    the methods physically contained within *this* class_definition node's
    own span via `_within_span`, not the raw qualname-keyed dict -- this is
    the one place in this function where per-occurrence, not per-qualname,
    identity matters.
    """
    class_candidates_by_name = {
        name: [s for s in candidates if s.kind == "class"]
        for name, candidates in top_level_by_name.items()
    }

    relations: list[Relation] = []
    for child in root.named_children:
        if child.type != "class_definition":
            continue
        superclasses = child.child_by_field_name("superclasses")
        name_node = child.child_by_field_name("name")
        if superclasses is None or name_node is None:
            continue
        all_same_named_methods = methods_by_class.get(name_node.text.decode("utf-8"), {})
        own_methods: dict[str, list[Symbol]] = {}
        for method_name, candidates in all_same_named_methods.items():
            scoped = [m for m in candidates if _within_span(m, child)]
            if scoped:
                own_methods[method_name] = scoped
        if not own_methods:
            continue

        base_qualnames: set[str] = set()
        for base in superclasses.named_children:
            if base.type != "identifier":
                continue
            for candidate in class_candidates_by_name.get(base.text.decode("utf-8"), []):
                base_qualnames.add(candidate.qualname)

        for method_name, subclass_methods in own_methods.items():
            base_method_candidates = [
                method
                for base_qualname in sorted(base_qualnames)
                for method in methods_by_class.get(base_qualname, {}).get(method_name, [])
            ]
            if not base_method_candidates:
                continue
            confidence = (
                Confidence.AMBIGUOUS
                if len(base_method_candidates) > 1 or len(subclass_methods) > 1
                else Confidence.EXTRACTED
            )
            for subclass_method in subclass_methods:
                for base_method in base_method_candidates:
                    relations.append(
                        Relation(
                            source=subclass_method.id,
                            target=base_method.id,
                            predicate="overrides",
                            site_file=path,
                            site_line=subclass_method.start_line,
                            site_col=subclass_method.start_col,
                            confidence=confidence,
                            provenance=provenance,
                        )
                    )
    return relations


def _import_relations(
    root, *, module: Symbol, path: str, provenance: Provenance
) -> tuple[list[Relation], dict[str, str]]:
    """Module-level `import <dotted_name>` and `from <dotted_name> import
    <dotted_name>[, <dotted_name>, ...]` statements only (this slice's
    narrow first cut) -- aliased imports (`import x as y`) and relative
    imports (`from . import x`) are explicitly deferred, see the
    extract_relations completion memory.

    A `from x import a, b` statement's grammar exposes every imported name
    as its own `name`-field child (not a single field) -- `children_by_field_name`
    must be used instead of `child_by_field_name`, which silently returns
    only the first, to avoid truncating multi-name imports.

    Also returns an alias map -- {imported_name: dotted_module_path} --
    covering `from`-imports only (a plain `import module` is called as
    `module.attr()`, never as a bare identifier, so it never belongs in
    this map). Consumed by `_call_and_reference_relations` to recognize a
    bare call to an imported name as a cross-file resolution candidate
    (DeferredImportCall) rather than a plain undefined-name miss.
    """
    relations = []
    alias_map: dict[str, str] = {}
    for child in root.named_children:
        if child.type == "import_statement":
            name_node = child.child_by_field_name("name")
            if name_node.type != "dotted_name":
                continue
            targets = [name_node.text.decode("utf-8")]
        elif child.type == "import_from_statement":
            module_node = child.child_by_field_name("module_name")
            if module_node.type != "dotted_name":
                continue
            module_dotted = module_node.text.decode("utf-8")
            imported_names = [
                name_node.text.decode("utf-8")
                for name_node in child.children_by_field_name("name")
                if name_node.type == "dotted_name"
            ]
            targets = [f"{module_dotted}.{name}" for name in imported_names]
            for name in imported_names:
                alias_map[name] = module_dotted
        else:
            continue
        for target in targets:
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
    return relations, alias_map


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
