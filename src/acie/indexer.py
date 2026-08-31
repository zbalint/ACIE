from dataclasses import dataclass

from acie.adapters.python.extract_relations import extract_relations
from acie.adapters.python.extract_symbols import extract_symbols, has_syntax_error
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
    new_relations = extract_relations(path=path, source_text=source_text, observed_at=observed_at)

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

    index_meta_store.bump_generation()

    return IndexResult(
        skipped=False,
        symbols_upserted=len(new_symbols),
        symbols_tombstoned=len(removed_symbol_ids),
        relations_upserted=len(new_relations),
        relations_tombstoned=len(removed_relation_keys),
    )


def _relation_key(relation) -> tuple:
    return (
        relation.source,
        relation.target,
        relation.predicate,
        relation.site_file,
        relation.site_line,
        relation.site_col,
    )
