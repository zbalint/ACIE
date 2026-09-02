from dataclasses import dataclass

from acie.adapters.python.extract_relations import extract_relations_with_deferred_edges
from acie.adapters.python.extract_symbols import extract_symbols, has_syntax_error
from acie.ir.relation import DeferredImportCall, DeferredImportInherit, Relation
from acie.ir.symbol import Confidence
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore


@dataclass(frozen=True)
class IndexResult:
    skipped: bool
    symbols_upserted: int
    symbols_tombstoned: int
    relations_upserted: int
    relations_tombstoned: int


def index_file(
    path: str,
    source_text: str,
    observed_at: str,
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
) -> IndexResult:
    if has_syntax_error(source_text):
        return IndexResult(
            skipped=True,
            symbols_upserted=0,
            symbols_tombstoned=0,
            relations_upserted=0,
            relations_tombstoned=0,
        )

    new_symbols = extract_symbols(path=path, source_text=source_text, observed_at=observed_at)
    new_relations, deferred_calls, deferred_inherits = extract_relations_with_deferred_edges(
        path=path, source_text=source_text, observed_at=observed_at
    )
    new_relations = new_relations + _resolve_deferred(
        deferred_calls, symbol_store, kind="function", predicate="calls"
    )
    new_relations = new_relations + _resolve_deferred(
        deferred_inherits, symbol_store, kind="class", predicate="inherits"
    )

    prior_symbol_ids = {s.id for s in symbol_store.list_by_path(path)}
    prior_relation_keys = {_relation_key(r) for r in relation_store.list_by_site_file(path)}

    for symbol in new_symbols:
        symbol_store.upsert(symbol)
    for relation in new_relations:
        relation_store.upsert(relation)

    removed_symbol_ids = prior_symbol_ids - {s.id for s in new_symbols}
    for symbol_id in removed_symbol_ids:
        symbol_store.delete(symbol_id, observed_at=observed_at)

    removed_relation_keys = prior_relation_keys - {_relation_key(r) for r in new_relations}
    for source, target, predicate, site_file, site_line, site_col in removed_relation_keys:
        relation_store.delete(
            source=source, target=target, predicate=predicate,
            site_file=site_file, site_line=site_line, site_col=site_col,
            observed_at=observed_at,
        )

    # Cross-file edges (Slice C's calls resolution) mean a relation's site
    # and target can now live in different files -- codex review finding:
    # the same-site diff above only ever catches a stale relation SITED in
    # `path`, so a symbol removed/renamed here (removed_symbol_ids) can
    # leave a `calls` edge elsewhere still EXTRACTED and pointing at a
    # tombstoned id, if that edge's own site_file was never touched. Every
    # relation elsewhere in the repo targeting a symbol just removed here is
    # invalidated too; `site_file == path` ones are skipped since the diff
    # above already removed them (avoids double-tombstoning the same key).
    stale_cross_file_relations = 0
    for symbol_id in removed_symbol_ids:
        for relation in relation_store.list_by_target(symbol_id):
            if relation.site_file == path:
                continue
            relation_store.delete(
                source=relation.source, target=relation.target, predicate=relation.predicate,
                site_file=relation.site_file, site_line=relation.site_line, site_col=relation.site_col,
                observed_at=observed_at,
            )
            stale_cross_file_relations += 1

    index_meta_store.bump_generation()

    return IndexResult(
        skipped=False,
        symbols_upserted=len(new_symbols),
        symbols_tombstoned=len(removed_symbol_ids),
        relations_upserted=len(new_relations),
        relations_tombstoned=len(removed_relation_keys) + stale_cross_file_relations,
    )


def _resolve_deferred(
    deferred_items: list[DeferredImportCall] | list[DeferredImportInherit],
    symbol_store: SymbolStore,
    *,
    kind: str,
    predicate: str,
) -> list[Relation]:
    """Resolves each DeferredImportCall/DeferredImportInherit (a bare call or
    a base-class identifier that's `from`-imported but which extract_relations
    couldn't itself resolve, single-file-scoped as it is) against the
    repo-wide symbol index. Shared by both predicates (slice A2 generalized
    this out of what was originally calls-only `_resolve_deferred_calls`,
    since the two are identical apart from which symbol `kind` to look up
    and which `predicate` to stamp the resulting Relation with) -- not
    tested directly (both call sites are private), only via index_file's own
    integration tests.

    No module-path-to-file-path mapping exists or can be assumed for an
    arbitrary target repo (no PYTHONPATH/package-root config, see the
    cross-file-resolution plan) -- so a candidate is instead found by
    reversing the *file path* into its own dotted form (slashes to dots,
    trailing .py/__init__.py stripped) and checking whether the imported
    dotted module_path is that reversed path or one of its dotted
    suffixes. This lets `from acie.daemon.discovery import x` resolve
    against a candidate at `src/acie/daemon/discovery.py` without ACIE ever
    knowing "src/" is a source root -- it's the same suffix-tolerant
    matching already used for redefinition collisions elsewhere in this
    codebase, just applied to paths instead of qualnames.

    Zero matches leaves the item unresolved (identical to today's silent
    miss on a genuinely undefined name) -- most commonly because the
    target file hasn't been indexed yet; bootstrap.py's second pass and the
    filesystem watcher's natural reindex-on-edit are what eventually close
    that gap, not this function retrying anything. One matching candidate
    is EXTRACTED; more than one (e.g. two same-named modules at different
    paths) is AMBIGUOUS per candidate, mirroring extract_relations's own
    collision handling.
    """
    relations: list[Relation] = []
    for item in deferred_items:
        candidates = [
            symbol
            for symbol in symbol_store.find_by_qualname_and_kind(qualname=item.name, kind=kind)
            if _module_path_matches(symbol.path, item.module_path)
        ]
        if not candidates:
            continue
        confidence = Confidence.EXTRACTED if len(candidates) == 1 else Confidence.AMBIGUOUS
        for candidate in candidates:
            relations.append(
                Relation(
                    source=item.source,
                    target=candidate.id,
                    predicate=predicate,
                    site_file=item.site_file,
                    site_line=item.site_line,
                    site_col=item.site_col,
                    confidence=confidence,
                    provenance=item.provenance,
                )
            )
    return relations


def _module_path_matches(candidate_file_path: str, imported_module_path: str) -> bool:
    dotted = candidate_file_path
    if dotted.endswith("/__init__.py"):
        dotted = dotted[: -len("/__init__.py")]
    elif dotted.endswith(".py"):
        dotted = dotted[: -len(".py")]
    dotted = dotted.replace("/", ".")
    return dotted == imported_module_path or dotted.endswith(f".{imported_module_path}")


def _relation_key(relation) -> tuple:
    return (
        relation.source,
        relation.target,
        relation.predicate,
        relation.site_file,
        relation.site_line,
        relation.site_col,
    )
