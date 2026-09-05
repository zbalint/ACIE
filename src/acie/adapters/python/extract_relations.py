from importlib.metadata import version

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from acie.adapters.python.extract_symbols import extract_symbols
from acie.module_paths import path_to_dotted
from acie.ir.relation import DeferredImportCall, DeferredImportInherit, DeferredImportOverride, Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.pytest_conventions import is_test_file_path, is_test_qualname

_LANGUAGE = Language(tspython.language())
_PROVENANCE_VERSION = version("tree-sitter-python")

# Bumped only if the fixture-DI heuristic's own matching logic changes --
# not tied to any external package version, since this heuristic isn't
# pytest's own behavior, just ACIE's static approximation of it.
_FIXTURE_HEURISTIC_VERSION = "1"


def extract_relations(path: str, source_text: str, observed_at: str) -> list[Relation]:
    """Same-file relations only -- see extract_relations_with_deferred_edges
    for the sibling entry point that also surfaces cross-file-candidate
    calls/inherits this pure, single-file function cannot itself resolve.
    """
    relations, _deferred_calls, _deferred_inherits, _deferred_overrides = _extract(
        path=path, source_text=source_text, observed_at=observed_at
    )
    return relations


def extract_relations_with_deferred_edges(
    path: str, source_text: str, observed_at: str
) -> tuple[list[Relation], list[DeferredImportCall], list[DeferredImportInherit], list[DeferredImportOverride]]:
    """Like extract_relations, but also returns bare-identifier calls,
    `class Foo(Base):` base identifiers, and method overrides of such a base
    whose name resolves to no same-file symbol yet *is* imported in this
    file -- candidates for cross-file resolution against the repo-wide
    symbol index, which only indexer.py (not this pure, single-file
    function) can do. See DeferredImportCall / DeferredImportInherit /
    DeferredImportOverride.
    """
    return _extract(path=path, source_text=source_text, observed_at=observed_at)


def _extract(
    path: str, source_text: str, observed_at: str
) -> tuple[list[Relation], list[DeferredImportCall], list[DeferredImportInherit], list[DeferredImportOverride]]:
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

    inherits_relations, deferred_inherits = _inherits_relations(
        tree.root_node,
        symbols,
        top_level_by_name=top_level_by_name,
        import_alias_map=import_alias_map,
        path=path,
        provenance=provenance,
    )

    overrides_relations, deferred_overrides = _overrides_relations(
        tree.root_node,
        top_level_by_name=top_level_by_name,
        methods_by_class=methods_by_class,
        import_alias_map=import_alias_map,
        path=path,
        provenance=provenance,
    )

    fixture_provenance = Provenance(
        provider="pytest-fixture-heuristic", version=_FIXTURE_HEURISTIC_VERSION, observed_at=observed_at
    )
    by_position = _symbol_by_position(symbols)
    module_fixtures, class_fixtures = _fixture_definitions(
        tree.root_node, by_position=by_position, import_alias_map=import_alias_map
    )
    fixture_relations = _fixture_di_relations(
        tree.root_node,
        by_position=by_position,
        module_fixtures=module_fixtures,
        class_fixtures=class_fixtures,
        path=path,
        fixture_provenance=fixture_provenance,
    )

    relations: list[Relation] = []
    relations.extend(_defines_relations(symbols, path=path, provenance=provenance))
    relations.extend(import_relations)
    relations.extend(inherits_relations)
    relations.extend(overrides_relations)
    relations.extend(call_relations)
    relations.extend(fixture_relations)
    return relations, deferred_calls, deferred_inherits, deferred_overrides


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
    so it's in scope. An attribute-access call on anything else stays
    explicitly deferred (a DeferredImportCall with `attribute` set, see F1)
    only when the base identifier is itself a `from`-imported name
    (import_alias_map) -- e.g. `scan.run_scan()` where `from acie import
    scan`. Any other base -- a local variable, a plain `import x` module, a
    nested attribute chain (`pkg.sub.func()`) -- still requires real
    type/reference resolution this pure, single-file pass can't do, and is
    silently dropped, not deferred; `ClassName.method()` through an
    imported class is the same shape but a distinct, still-unbuilt
    resolution (F2). A bare name passed as a call argument or returned is
    likewise deferred, since only assignment RHS is checked.

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
                if object_node is not None and object_node.type == "identifier" and attribute_node is not None:
                    object_name = object_node.text.decode("utf-8")
                    if object_name == "self" and current_class is not None:
                        candidates = methods_by_class.get(current_class.qualname, {}).get(
                            attribute_node.text.decode("utf-8"), []
                        )
                        resolve(attribute_node, source=current_source, candidates=candidates, predicate="calls")
                    elif object_name in import_alias_map:
                        deferred.append(
                            DeferredImportCall(
                                source=current_source.id,
                                module_path=import_alias_map[object_name],
                                name=object_name,
                                attribute=attribute_node.text.decode("utf-8"),
                                site_file=path,
                                site_line=attribute_node.start_point.row + 1,
                                site_col=attribute_node.start_point.column,
                                provenance=provenance,
                            )
                        )
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
    import_alias_map: dict[str, str],
    path: str,
    provenance: Provenance,
) -> tuple[list[Relation], list[DeferredImportInherit]]:
    """Top-level `class Foo(Base): ...` only (this slice's narrow first cut,
    matching extract_symbols's own top-level-only scope) -- decorated
    classes, keyword base-class args like `metaclass=M` (correctly skipped,
    not an identifier), and multi-level nesting are out of scope here.

    A base identifier that matches neither a same-file class nor
    import_alias_map is a genuinely undefined/unresolvable name and produces
    no edge at all -- unchanged from before this slice. One that resolves to
    no same-file class but *is* a `from`-imported name produces a
    DeferredImportInherit instead (slice A2), mirroring how
    _call_and_reference_relations defers a bare call to an imported name:
    indexer.py resolves it against the repo-wide symbol index, which this
    pure, single-file function has no access to.
    """
    by_position = _symbol_by_position(symbols)
    class_candidates_by_name = {
        name: [s for s in candidates if s.kind == "class"]
        for name, candidates in top_level_by_name.items()
    }

    relations: list[Relation] = []
    deferred: list[DeferredImportInherit] = []
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
            base_name = base.text.decode("utf-8")
            candidates = class_candidates_by_name.get(base_name, [])
            if not candidates:
                if base_name in import_alias_map:
                    deferred.append(
                        DeferredImportInherit(
                            source=source_symbol.id,
                            module_path=import_alias_map[base_name],
                            name=base_name,
                            site_file=path,
                            site_line=base.start_point.row + 1,
                            site_col=base.start_point.column,
                            provenance=provenance,
                        )
                    )
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
    return relations, deferred


def _overrides_relations(
    root,
    *,
    top_level_by_name: dict[str, list[Symbol]],
    methods_by_class: dict[str, dict[str, list[Symbol]]],
    import_alias_map: dict[str, str],
    path: str,
    provenance: Provenance,
) -> tuple[list[Relation], list[DeferredImportOverride]]:
    """Top-level `class Foo(Base): def bar(...)` where an immediate base
    already defines a method of the same name. `overrides` points at the
    immediate overridden base method, not the transitive root -- multi-level
    chains fall out for free via BFS hopping through the edges
    (impact_analysis, a later slice).

    A same-file base (same scope as _inherits_relations resolves)
    resolves immediately, exactly as before slice A3. An immediate base
    that resolves to no same-file class but *is* `from`-imported produces
    a DeferredImportOverride instead (mirroring DeferredImportInherit's
    same split for the `inherits` predicate): this pure, single-file
    function cannot know whether that cross-file base defines a matching
    method -- only indexer.py's repo-wide symbol index can. One deferred
    candidate is emitted per (own method, import-deferred base) pair,
    since either might be the one the base actually overrides.

    Ambiguity is computed per (subclass, method_name) pair over the *union*
    of every immediate SAME-FILE base's matching method candidates -- not
    per base independently -- because Python's MRO would pick exactly one
    candidate deterministically, but tree-sitter alone cannot compute MRO
    linearization across multiple bases; more than one candidate anywhere
    in that union means the override target is genuinely ambiguous without
    semantic resolution (later upgraded to INFERRED by pyright enrichment).
    A cross-file base's candidates are NOT folded into this same union --
    indexer.py's _resolve_deferred_overrides computes each deferred item's
    own confidence independently once resolved. shortcut: a method
    overriding both a same-file base and a cross-file base therefore gets
    two independently-confident edges instead of one true joint-MRO-
    ambiguous edge; true joint confidence would need cross-file candidates
    available before this single-file function returns, which isn't this
    slice's scope -- revisit if joint same-file+cross-file MRO ambiguity
    ever needs modeling.

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
    deferred: list[DeferredImportOverride] = []
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
        deferred_bases: list[tuple[str, str]] = []  # (base_name, module_path)
        for base in superclasses.named_children:
            if base.type != "identifier":
                continue
            base_name = base.text.decode("utf-8")
            same_file_candidates = class_candidates_by_name.get(base_name, [])
            if same_file_candidates:
                for candidate in same_file_candidates:
                    base_qualnames.add(candidate.qualname)
            elif base_name in import_alias_map:
                deferred_bases.append((base_name, import_alias_map[base_name]))

        for method_name, subclass_methods in own_methods.items():
            base_method_candidates = [
                method
                for base_qualname in sorted(base_qualnames)
                for method in methods_by_class.get(base_qualname, {}).get(method_name, [])
            ]
            if base_method_candidates:
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
            for base_name, module_path in deferred_bases:
                for subclass_method in subclass_methods:
                    deferred.append(
                        DeferredImportOverride(
                            source=subclass_method.id,
                            module_path=module_path,
                            base_name=base_name,
                            method_name=method_name,
                            site_file=path,
                            site_line=subclass_method.start_line,
                            site_col=subclass_method.start_col,
                            provenance=provenance,
                        )
                    )
    return relations, deferred


def _unwrap_decorated(node):
    """Mirrors extract_symbols._unwrap_decorated (kept local rather than
    imported across the module boundary, same as this file's other small
    tree-walking helpers like _symbol_by_position/_within_span): a
    decorated def is wrapped in a `decorated_definition` node whose
    `definition` field holds the actual function_definition/
    class_definition -- unwrap it so both fixture-definition and fixture-DI
    detection below can treat a decorated function/class the same as an
    undecorated one wherever decorators themselves aren't what's being
    inspected.
    """
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition")
    return node


def _fixture_definitions(
    root, *, by_position: dict[tuple[int, int], Symbol], import_alias_map: dict[str, str]
) -> tuple[dict[str, list[Symbol]], dict[str, dict[str, list[Symbol]]]]:
    """Returns (module_fixtures, class_fixtures):

    - `module_fixtures`: public fixture name -> candidate Symbols, for
      every module-level (top-level) `@pytest.fixture`-decorated function
      -- visible file-wide to every test/fixture regardless of enclosing
      class, matching pytest's own resolution.
    - `class_fixtures`: class qualname -> {public fixture name -> candidate
      Symbols}, for every class-level (one level into a class body)
      `@pytest.fixture`-decorated method -- visible ONLY within that same
      class. A sibling class's same-named fixture is never a candidate,
      and a class-level fixture correctly SHADOWS a same-named module-level
      one for its own class's members (both directions verified against a
      real `pytest` run, 2026-09-03 code review, finding P1: a flat
      by-name-only dict previously let `TestB.test_x(db)` wrongly resolve
      against `TestA.db`, an impossible edge -- pytest class-scoped
      fixtures are invisible outside their own class).

    Recognizes `@pytest.fixture` (bare, `@pytest.fixture()`, or
    `@pytest.fixture(scope=...)`), or `@fixture` when `fixture` is `from
    pytest import`ed. Scoped to the same two tiers extract_symbols itself
    recognizes (module-level defs, one level into a class body) -- no
    Symbol exists to attach a Relation to at any deeper nesting regardless.
    An aliased fixture import (`from pytest import fixture as fx`) is not
    recognized -- same pre-existing limitation as extract_relations' own
    aliased-import handling elsewhere in this module.

    Public name resolution honors `@pytest.fixture(name="...")` (a real
    pytest feature: `_pytest.fixtures.FixtureFunctionMarker.__call__` sets
    `name = self.name or function.__name__` -- a `name=` override REPLACES
    the function's own name as the fixture's sole public identifier, it
    does not additionally register the original name too; verified
    against the installed pytest's own source, 2026-09-03 code review,
    finding P1). A parameter matching the underlying function's own name
    after such a rename produces no edge, matching real pytest.
    """
    module_fixtures: dict[str, list[Symbol]] = {}
    class_fixtures: dict[str, dict[str, list[Symbol]]] = {}

    def consider(node, *, class_qualname: str | None) -> None:
        if node.type != "decorated_definition":
            return
        definition = node.child_by_field_name("definition")
        if definition is None or definition.type != "function_definition":
            return
        fixture_decorators = [
            c for c in node.named_children if c.type == "decorator" and _is_pytest_fixture_decorator(c, import_alias_map)
        ]
        if not fixture_decorators:
            return
        sym = by_position.get((definition.start_point.row + 1, definition.start_point.column))
        if sym is None:
            return
        own_name = sym.qualname.rsplit(".", 1)[-1]
        public_name = _fixture_public_name(fixture_decorators[0], default_name=own_name)
        if class_qualname is None:
            module_fixtures.setdefault(public_name, []).append(sym)
        else:
            class_fixtures.setdefault(class_qualname, {}).setdefault(public_name, []).append(sym)

    for child in root.named_children:
        consider(child, class_qualname=None)
        unwrapped = _unwrap_decorated(child)
        if unwrapped.type == "class_definition":
            body = unwrapped.child_by_field_name("body")
            name_node = unwrapped.child_by_field_name("name")
            if body is not None and name_node is not None:
                class_qualname = name_node.text.decode("utf-8")
                for member in body.named_children:
                    consider(member, class_qualname=class_qualname)
    return module_fixtures, class_fixtures


def _is_pytest_fixture_decorator(decorator_node, import_alias_map: dict[str, str]) -> bool:
    if not decorator_node.named_children:
        return False
    head = decorator_node.named_children[0]
    if head.type == "call":
        head = head.child_by_field_name("function")
    if head is None:
        return False
    if head.type == "attribute":
        object_node = head.child_by_field_name("object")
        attribute_node = head.child_by_field_name("attribute")
        return (
            object_node is not None
            and object_node.type == "identifier"
            and object_node.text == b"pytest"
            and attribute_node is not None
            and attribute_node.text == b"fixture"
        )
    if head.type == "identifier":
        return head.text.decode("utf-8") == "fixture" and import_alias_map.get("fixture") == "pytest"
    return False


def _fixture_public_name(decorator_node, *, default_name: str) -> str:
    """The fixture's real registered name: default_name (the decorated
    function's own bare name) unless overridden by a literal-string
    `@pytest.fixture(name="...")` keyword argument -- see _fixture_
    definitions' docstring for the pytest-source citation. A non-literal
    `name=` value (a variable, an f-string) can't be resolved statically
    and falls back to default_name, same as any other unresolvable case
    in this heuristic.
    """
    if not decorator_node.named_children:
        return default_name
    head = decorator_node.named_children[0]
    if head.type != "call":
        return default_name
    arguments = head.child_by_field_name("arguments")
    if arguments is None:
        return default_name
    for arg in arguments.named_children:
        if arg.type != "keyword_argument":
            continue
        key_node = arg.child_by_field_name("name")
        if key_node is None or key_node.text != b"name":
            continue
        value_node = arg.child_by_field_name("value")
        literal = _string_literal_value(value_node) if value_node is not None else None
        if literal is not None:
            return literal
    return default_name


def _string_literal_value(node) -> str | None:
    """The literal text of a plain `string` node (no f-string
    interpolation support needed here -- a `name=f"..."` argument simply
    falls back to the decorated function's own name via
    _fixture_public_name, same as any other statically-unresolvable case).
    """
    if node.type != "string":
        return None
    parts = [c.text.decode("utf-8") for c in node.named_children if c.type == "string_content"]
    return "".join(parts)


def _fixture_di_relations(
    root,
    *,
    by_position: dict[tuple[int, int], Symbol],
    module_fixtures: dict[str, list[Symbol]],
    class_fixtures: dict[str, dict[str, list[Symbol]]],
    path: str,
    fixture_provenance: Provenance,
) -> list[Relation]:
    """Synthesizes an AMBIGUOUS-confidence `calls` edge from a test function
    (or another fixture) to each same-file `@pytest.fixture` its parameter
    list names -- pytest's own implicit by-name dependency injection is
    never a real function call in the source, so tree-sitter's ordinary
    call-site extraction (_call_and_reference_relations) can never see it.
    Always AMBIGUOUS, even for a single same-file match: this is a naming-
    convention heuristic, not a resolved reference -- pytest's real
    fixture resolution also considers a conftest.py hierarchy this static
    heuristic cannot model (see this module's cross-file scope note below).

    Candidate resolution respects pytest's real class-vs-module fixture
    visibility (verified against a real pytest run, 2026-09-03 code review
    finding P1): for a method, its OWN class's class_fixtures are checked
    FIRST and, if the name matches there, used exclusively -- a class-level
    fixture shadows a same-named module-level one for its own class's
    members, matching real pytest precedence. Only when the name has no
    match in the method's own class does resolution fall back to
    module_fixtures. A module-level DI-target (no enclosing class) only
    ever consults module_fixtures. This means a fixture defined in one
    class is NEVER a candidate for a different class's test -- pytest
    class-scoped fixtures are invisible outside their own class.

    A DI-target function is exactly one of the two kinds pytest itself
    would inject fixtures into: a test function/method (is_test_file_path
    + is_test_qualname, the identical convention affected_tests/B1 already
    established -- shared via acie.pytest_conventions, a real second
    caller) or a fixture function/method itself (fixtures can depend on
    other fixtures). An ordinary helper is not a DI site even if one of its
    parameter names happens to collide with a fixture's -- pytest would
    never call it with injection semantics.

    Cross-file scope (not this slice): a fixture defined in a conftest.py
    ancestor directory, or any file this test file doesn't otherwise
    reference, is invisible to this pass -- extract_relations is single-
    file-scoped and fixtures are never imported (pytest auto-discovers
    them), so there is no DeferredImportCall-style import_alias_map entry
    to defer against, unlike the calls/inherits/overrides predicates. A
    real cross-file version would need a persistent repo-wide fixture
    registry (a Symbol-schema or storage change, not merely an indexer.py
    resolution pass) -- deliberately left as a flagged follow-up, same
    status as B1's own naming-convention-vs-actually-collected gap
    (memory 9a12ed13), not built now.
    """
    fixture_symbol_ids = {sym.id for candidates in module_fixtures.values() for sym in candidates}
    for by_name in class_fixtures.values():
        fixture_symbol_ids |= {sym.id for candidates in by_name.values() for sym in candidates}
    is_test_file = is_test_file_path(path)
    relations: list[Relation] = []

    def candidates_for(name: str, *, own_class: str | None) -> list[Symbol]:
        if own_class is not None:
            shadowed = class_fixtures.get(own_class, {}).get(name)
            if shadowed:
                return shadowed
        return module_fixtures.get(name, [])

    def consider(node, *, own_class: str | None) -> None:
        node = _unwrap_decorated(node)
        if node.type != "function_definition":
            return
        sym = by_position.get((node.start_point.row + 1, node.start_point.column))
        if sym is None:
            return
        is_test = is_test_file and is_test_qualname(sym.qualname)
        if not (is_test or sym.id in fixture_symbol_ids):
            return
        for name_node in _param_name_nodes(node):
            name = name_node.text.decode("utf-8")
            if name in ("self", "cls"):
                continue
            for fixture_symbol in candidates_for(name, own_class=own_class):
                if fixture_symbol.id == sym.id:
                    continue  # a fixture can't depend on itself via a same-named parameter
                relations.append(
                    Relation(
                        source=sym.id,
                        target=fixture_symbol.id,
                        predicate="calls",
                        site_file=path,
                        site_line=name_node.start_point.row + 1,
                        site_col=name_node.start_point.column,
                        confidence=Confidence.AMBIGUOUS,
                        provenance=fixture_provenance,
                    )
                )

    for child in root.named_children:
        consider(child, own_class=None)
        unwrapped = _unwrap_decorated(child)
        if unwrapped.type == "class_definition":
            body = unwrapped.child_by_field_name("body")
            name_node = unwrapped.child_by_field_name("name")
            if body is not None and name_node is not None:
                own_class = name_node.text.decode("utf-8")
                for member in body.named_children:
                    consider(member, own_class=own_class)
    return relations


def _param_name_nodes(function_node) -> list:
    """The identifier node for each MANDATORY (no-default) parameter of a
    function_definition -- plain (`x`) and typed-without-default (`x: T`)
    only. `*args`/`**kwargs` (list_splat_pattern/dictionary_splat_pattern)
    are excluded, since neither is ever a fixture-name binding. A defaulted
    parameter (`x=1`, `x: T = 1` -- default_parameter/typed_default_
    parameter) is ALSO deliberately excluded: pytest's own fixture-request
    resolution (`_pytest.compat.getfuncargnames`, verified against the
    installed pytest's real source, 2026-09-03 code review finding from
    an independent reviewer) only ever treats a parameter as a fixture
    request when it has no default value (`p.default is Parameter.empty`)
    -- a defaulted parameter is a plain optional argument, never injected.
    Confirmed empirically too: a real pytest run collecting `def test_x(db
    ="x"): ...` never resolves `db` against a same-named fixture. Verified
    against the live tree-sitter-python grammar (2026-09-02): typed_
    parameter has no `name` field (its identifier is taken positionally),
    unlike default_parameter/typed_default_parameter which do -- moot now
    that both are excluded outright, kept here as the reason this function
    only bothers with the two node shapes it still handles.
    """
    params_node = function_node.child_by_field_name("parameters")
    if params_node is None:
        return []
    names = []
    for p in params_node.named_children:
        if p.type == "identifier":
            names.append(p)
        elif p.type == "typed_parameter":
            if p.named_children and p.named_children[0].type == "identifier":
                names.append(p.named_children[0])
    return names


def _import_relations(
    root, *, module: Symbol, path: str, provenance: Provenance
) -> tuple[list[Relation], dict[str, str]]:
    """Collect import statements anywhere in the syntax tree.

    `import_alias_map` remains a flat file-level map because downstream
    deferred relation resolution has no scope model.
    """
    relations: list[Relation] = []
    alias_map: dict[str, str] = {}

    def walk(node) -> None:
        if node.type == "import_statement":
            _handle_import_statement(
                node, module=module, path=path, provenance=provenance, relations=relations
            )
        elif node.type == "import_from_statement":
            _handle_import_from_statement(
                node,
                module=module,
                path=path,
                provenance=provenance,
                relations=relations,
                alias_map=alias_map,
            )
        for child in node.named_children:
            walk(child)

    walk(root)
    return relations, alias_map


def _imports_relation(
    module: Symbol, target: str, site_node, path: str, provenance: Provenance
) -> Relation:
    return Relation(
        source=module.id,
        target=target,
        predicate="imports",
        site_file=path,
        site_line=site_node.start_point.row + 1,
        site_col=site_node.start_point.column,
        confidence=Confidence.EXTRACTED,
        provenance=provenance,
    )


def _handle_import_statement(
    child, *, module: Symbol, path: str, provenance: Provenance, relations: list[Relation]
) -> None:
    for name_node in child.children_by_field_name("name"):
        if name_node.type == "dotted_name":
            target = name_node.text.decode("utf-8")
        elif name_node.type == "aliased_import":
            imported_node = name_node.child_by_field_name("name")
            if imported_node is None or imported_node.type != "dotted_name":
                continue
            target = imported_node.text.decode("utf-8")
        else:
            continue
        relations.append(_imports_relation(module, target, child, path, provenance))


def _handle_import_from_statement(
    child,
    *,
    module: Symbol,
    path: str,
    provenance: Provenance,
    relations: list[Relation],
    alias_map: dict[str, str],
) -> None:
    module_node = child.child_by_field_name("module_name")
    if module_node is None:
        return
    base = _module_base(module_node, path)
    if base is None:
        return
    for name_node in child.children_by_field_name("name"):
        if name_node.type == "dotted_name":
            imported_name = name_node.text.decode("utf-8")
            bound_name = imported_name
        elif name_node.type == "aliased_import":
            imported_node = name_node.child_by_field_name("name")
            alias_node = name_node.child_by_field_name("alias")
            if imported_node is None or imported_node.type != "dotted_name" or alias_node is None:
                continue
            imported_name = imported_node.text.decode("utf-8")
            bound_name = alias_node.text.decode("utf-8")
        else:
            continue
        target = f"{base}.{imported_name}" if base else imported_name
        relations.append(_imports_relation(module, target, child, path, provenance))
        alias_map[bound_name] = base


def _module_base(module_node, path: str) -> str | None:
    if module_node.type == "dotted_name":
        return module_node.text.decode("utf-8")
    if module_node.type != "relative_import":
        return None

    prefix = next(
        (child for child in module_node.named_children if child.type == "import_prefix"),
        None,
    )
    if prefix is None:
        return None
    level = sum(1 for child in prefix.children if child.type == ".")
    own_base = _relative_import_base(path, level)
    if own_base is None:
        return None
    submodule_node = next(
        (child for child in module_node.named_children if child.type == "dotted_name"),
        None,
    )
    if submodule_node is None:
        return own_base
    submodule = submodule_node.text.decode("utf-8")
    return f"{own_base}.{submodule}" if own_base else submodule


def _relative_import_base(path: str, level: int) -> str | None:
    """Resolve a relative import's leading dots against its file path.

    A package's own `__init__.py` treats level 1 (a single dot) as the
    package itself, so all of its own-name segments -- down to "" for a
    bare root-level `__init__.py` with no wrapping directory -- are
    climbable. Any other module treats level 1 as its *enclosing* package,
    one segment short of its own name, so climbing exhausts one segment
    sooner (an already-tested boundary: `pkg/mod.py` + `from ..` must stay
    unresolvable).
    """
    is_init = path.endswith("/__init__.py") or path == "__init__.py"
    dotted = path_to_dotted(path)
    if is_init:
        # A bare root `__init__.py` doesn't match path_to_dotted's
        # "/__init__.py" suffix strip (no leading "/"), so it falls through
        # to the generic ".py" strip and returns the literal "__init__" --
        # normalize that back to "no package name" rather than let it leak
        # into a resolved dotted target.
        own_package_parts = [] if dotted == "__init__" else dotted.split(".")
    else:
        own_package_parts = dotted.split(".")[:-1]
    pops = level - 1
    climbable = len(own_package_parts) if is_init else len(own_package_parts) - 1
    if pops > climbable:
        return None
    remaining = own_package_parts[: len(own_package_parts) - pops] if pops else own_package_parts
    return ".".join(remaining)


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
