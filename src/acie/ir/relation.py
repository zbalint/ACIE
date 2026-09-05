from dataclasses import dataclass

from acie.ir.symbol import Confidence, Provenance


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    predicate: str
    site_file: str
    site_line: int
    site_col: int
    confidence: Confidence
    provenance: Provenance


@dataclass(frozen=True)
class DeferredImportCall:
    """A bare `name(...)` call, OR a `name.attribute(...)` call where
    `name` is imported (`from module_path import name`) in this file --
    extract_relations (single-file, pure) can go no further than naming the
    calling symbol and the imported name/module it came from (plus, for the
    attribute form, the attribute name itself). Cross-file resolution against
    the repo-wide symbol index happens in indexer.py: `attribute is None`
    resolves `name` as a symbol imported directly from `module_path`;
    `attribute is not None` resolves `name` as a SUBMODULE of `module_path`
    first, then `attribute` as a top-level symbol within that submodule's own
    file (mirroring DeferredImportOverride's base-class-then-method
    two-step). One that stays unresolved either way (target file not yet
    indexed, or genuinely external, or `name` doesn't actually denote a
    submodule) simply produces no edge, same as an undefined name today.
    """

    source: str
    module_path: str
    name: str
    site_file: str
    site_line: int
    site_col: int
    provenance: Provenance
    attribute: str | None = None


@dataclass(frozen=True)
class DeferredImportInherit:
    """A `class Foo(Base): ...` base identifier that resolves to no same-file
    class but *is* imported (`from module_path import name`) in this file --
    mirrors DeferredImportCall's split for the `inherits` predicate: this
    slice's extract_relations (single-file, pure) can go no further than
    naming the subclass and the imported base name/module it came from.
    Cross-file resolution against the repo-wide symbol index happens in
    indexer.py, turning a successfully-resolved one into a normal `inherits`
    Relation; one that stays unresolved (base file not yet indexed, or
    genuinely external) simply produces no edge, same as DeferredImportCall.
    """

    source: str
    module_path: str
    name: str
    site_file: str
    site_line: int
    site_col: int
    provenance: Provenance


@dataclass(frozen=True)
class DeferredImportOverride:
    """A `class Foo(Base): def bar(...)` where `Base` is not a same-file
    class but *is* imported (`from module_path import name`) -- mirrors
    DeferredImportCall/DeferredImportInherit's split for the `overrides`
    predicate (slice A3). Unlike those two, resolving this needs a
    two-step lookup, not one: extract_relations (single-file, pure) knows
    the overriding method and the imported base's bare name/module, but
    cannot itself know whether that base actually defines a same-named
    method -- only indexer.py's repo-wide symbol index can answer that, by
    first resolving base_name/module_path to a class symbol and then
    looking up method_name within it. A base that resolves to no repo
    symbol, or that resolves but defines no matching method, produces no
    edge at all, same as DeferredImportCall/DeferredImportInherit.
    """

    source: str
    module_path: str
    base_name: str
    method_name: str
    site_file: str
    site_line: int
    site_col: int
    provenance: Provenance
