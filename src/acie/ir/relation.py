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
    """A bare `name(...)` call whose name resolves to no same-file symbol
    but *is* imported (`from module_path import name`) in this file --
    extract_relations (single-file, pure) can go no further than naming the
    calling symbol and the imported name/module it came from. Cross-file
    resolution against the repo-wide symbol index happens in indexer.py,
    which turns a successfully-resolved one into a normal `calls` Relation;
    one that stays unresolved (target file not yet indexed, or genuinely
    external) simply produces no edge, same as an undefined name today.
    """

    source: str
    module_path: str
    name: str
    site_file: str
    site_line: int
    site_col: int
    provenance: Provenance


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
