import sqlite3
import threading

import pytest
from acie.indexer import index_file
from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.resolve import resolve_symbol_or_position
from acie.tools.errors import SymbolNotFoundError

_PROVENANCE = Provenance(
    provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z"
)
_POSITION = {"file": "pkg/mod.py", "line": 2, "column": 4}
_SITE = {"file": "pkg/mod.py", "line": 5, "column": 4}


def _symbol(
    symbol_id: str,
    qualname: str,
    *,
    line: int,
    kind: str = "function",
    col: int = 0,
) -> Symbol:
    return Symbol(
        id=symbol_id,
        path="pkg/mod.py",
        qualname=qualname,
        kind=kind,
        start_line=line,
        start_col=col,
        end_line=line + 1,
        end_col=col + 8,
        confidence=Confidence.EXTRACTED,
        provenance=_PROVENANCE,
    )


def _relation(source: str, target: str, *, site_line: int = 5) -> Relation:
    return Relation(
        source=source,
        target=target,
        predicate="calls",
        site_file="pkg/mod.py",
        site_line=site_line,
        site_col=4,
        confidence=Confidence.EXTRACTED,
        provenance=_PROVENANCE,
    )


def _two_connections(tmp_path):
    db_path = tmp_path / "index.sqlite"
    writer_conn = sqlite3.connect(str(db_path))
    reader_conn = sqlite3.connect(str(db_path))
    return (
        writer_conn,
        reader_conn,
        SymbolStore(conn=writer_conn),
        RelationStore(conn=writer_conn),
        SymbolStore(conn=reader_conn),
        RelationStore(conn=reader_conn),
    )
def _index_baseline(
    writer_conn: sqlite3.Connection,
    writer_symbols: SymbolStore,
    writer_relations: RelationStore,
    source_text: str,
) -> None:
    index_file(
        path="pkg/mod.py",
        source_text=source_text,
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=writer_symbols,
        relation_store=writer_relations,
        index_meta_store=IndexMetaStore(conn=writer_conn),
    )



def test_symbol_id_churn_reproduces_committed_position_gap(tmp_path):
    (
        writer_conn,
        reader_conn,
        writer_symbols,
        writer_relations,
        reader_symbols,
        reader_relations,
    ) = _two_connections(tmp_path)
    try:
        _index_baseline(
            writer_conn,
            writer_symbols,
            writer_relations,
            "class Container:\n    def old(self):\n        pass\n",
        )
        old_symbol = writer_symbols.get("pkg/mod.py:Container.old#method")
        assert old_symbol is not None
        new_symbol = _symbol(
            "pkg/mod.py:Container.new#method", "Container.new", line=2, kind="method", col=4
        )

        assert resolve_symbol_or_position(
            reader_symbols, reader_relations, symbol_id=None, position=_POSITION
        ) == [old_symbol]

        # Phase 1 characterization: the default store commits expose the
        # exact gap that index_file must batch away.
        writer_symbols.delete(old_symbol.id, observed_at="2026-08-31T01:00:00Z")
        assert reader_symbols.at_start(path="pkg/mod.py", line=2, col=4) is None
        with pytest.raises(SymbolNotFoundError):
            resolve_symbol_or_position(
                reader_symbols, reader_relations, symbol_id=None, position=_POSITION
            )

        writer_symbols.upsert(new_symbol)
        assert resolve_symbol_or_position(
            reader_symbols, reader_relations, symbol_id=None, position=_POSITION
        ) == [new_symbol]
    finally:
        reader_conn.close()
        writer_conn.close()

def test_relation_id_churn_reproduces_committed_position_gap(tmp_path):
    (
        writer_conn,
        reader_conn,
        writer_symbols,
        writer_relations,
        reader_symbols,
        reader_relations,
    ) = _two_connections(tmp_path)
    try:
        _index_baseline(
            writer_conn,
            writer_symbols,
            writer_relations,
            "def old():\n    pass\n\ndef caller():\n    old()\n",
        )
        caller = writer_symbols.get("pkg/mod.py:caller#function")
        old_target = writer_symbols.get("pkg/mod.py:old#function")
        assert caller is not None
        assert old_target is not None
        new_target = _symbol("pkg/mod.py:new#function", "new", line=1)
        writer_symbols.upsert(new_target)
        site_relations = writer_relations.list_by_site(
            site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
        )
        assert len(site_relations) == 1
        old_relation = site_relations[0]
        new_relation = _relation(caller.id, new_target.id)

        assert resolve_symbol_or_position(
            reader_symbols, reader_relations, symbol_id=None, position=_SITE
        ) == [old_target]

        # Phase 1 characterization: relation deletion has the same
        # committed-gap behavior as symbol deletion.
        writer_relations.delete(
            source=old_relation.source,
            target=old_relation.target,
            predicate=old_relation.predicate,
            site_file=old_relation.site_file,
            site_line=old_relation.site_line,
            site_col=old_relation.site_col,
            observed_at="2026-08-31T01:00:00Z",
        )
        assert reader_relations.list_by_site(
            site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
        ) == []
        with pytest.raises(SymbolNotFoundError):
            resolve_symbol_or_position(
                reader_symbols, reader_relations, symbol_id=None, position=_SITE
            )

        writer_relations.upsert(new_relation)
        assert reader_relations.list_by_site(
            site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
        ) == [new_relation]
        assert resolve_symbol_or_position(
            reader_symbols, reader_relations, symbol_id=None, position=_SITE
        ) == [new_target]
    finally:
        reader_conn.close()
        writer_conn.close()





class _CountingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1
        return super().commit()
class _PausingRelationStore(RelationStore):
    def __init__(self, *, conn: sqlite3.Connection):
        super().__init__(conn=conn)
        self.pause_target: str | None = None
        self.pause_started = threading.Event()
        self.release_pause = threading.Event()

    def upsert(self, relation: Relation, *, commit: bool = True) -> None:
        super().upsert(relation, commit=commit)
        if self.pause_target == relation.target:
            self.pause_started.set()
            if not self.release_pause.wait(timeout=5):
                raise RuntimeError("timed out waiting to inspect reindex")


def test_index_file_reindex_is_all_or_none_to_independent_reader(tmp_path):
    db_path = tmp_path / "index.sqlite"
    writer_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    reader_conn = sqlite3.connect(str(db_path))
    try:
        writer_symbols = SymbolStore(conn=writer_conn)
        writer_relations = _PausingRelationStore(conn=writer_conn)
        index_meta_store = IndexMetaStore(conn=writer_conn)
        old_source = "def old():\n    pass\n\ndef caller():\n    old()\n"
        new_source = "def new():\n    pass\n\ndef caller():\n    new()\n"
        _index_baseline(writer_conn, writer_symbols, writer_relations, old_source)
        reader_symbols = SymbolStore(conn=reader_conn)
        reader_relations = RelationStore(conn=reader_conn)
        old_target = reader_symbols.get("pkg/mod.py:old#function")
        assert old_target is not None
        old_relations = reader_relations.list_by_site(
            site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
        )
        assert len(old_relations) == 1
        old_relation = old_relations[0]
        new_relation = _relation(
            "pkg/mod.py:caller#function", "pkg/mod.py:new#function"
        )
        writer_relations.pause_target = new_relation.target
        errors = []

        def reindex():
            try:
                index_file(
                    path="pkg/mod.py",
                    source_text=new_source,
                    observed_at="2026-08-31T01:00:00Z",
                    symbol_store=writer_symbols,
                    relation_store=writer_relations,
                    index_meta_store=index_meta_store,
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=reindex)
        thread.start()
        try:
            assert writer_relations.pause_started.wait(timeout=5)
            assert reader_relations.list_by_site(
                site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
            ) == [old_relation]
            assert resolve_symbol_or_position(
                reader_symbols, reader_relations, symbol_id=None, position=_SITE
            ) == [old_target]
        finally:
            writer_relations.release_pause.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert not errors
        new_target = reader_symbols.get("pkg/mod.py:new#function")
        assert new_target is not None
        final_relations = reader_relations.list_by_site(
            site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls"}
        )
        assert len(final_relations) == 1
        assert final_relations[0].target == new_target.id
        assert resolve_symbol_or_position(
            reader_symbols, reader_relations, symbol_id=None, position=_SITE
        ) == [new_target]
    finally:
        reader_conn.close()
        writer_conn.close()


def test_shared_file_reindex_publishes_one_commit(tmp_path):
    db_path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(str(db_path), factory=_CountingConnection)
    try:
        symbol_store = SymbolStore(conn=conn)
        relation_store = RelationStore(conn=conn)
        index_meta_store = IndexMetaStore(conn=conn)
        conn.commit_count = 0

        index_file(
            path="pkg/mod.py",
            source_text="def foo():\n    pass\n",
            observed_at="2026-08-31T00:00:00Z",
            symbol_store=symbol_store,
            relation_store=relation_store,
            index_meta_store=index_meta_store,
        )

        assert conn.commit_count == 1
    finally:
        conn.close()

class _ForcedBaseException(BaseException):
    pass

@pytest.mark.parametrize("failure_type", [RuntimeError, _ForcedBaseException])
def test_failed_shared_file_reindex_rolls_back_before_writer_reuse(
    tmp_path, monkeypatch, failure_type
):
    db_path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        symbol_store = SymbolStore(conn=conn)
        relation_store = RelationStore(conn=conn)
        index_meta_store = IndexMetaStore(conn=conn)
        index_file(
            path="pkg/mod.py",
            source_text="def old():\n    pass\n",
            observed_at="2026-08-31T00:00:00Z",
            symbol_store=symbol_store,
            relation_store=relation_store,
            index_meta_store=index_meta_store,
        )
        generation_before = index_meta_store.current_generation()
        original_upsert = relation_store.upsert

        def fail_upsert(relation, *, commit=True):
            raise failure_type("forced relation write failure")

        monkeypatch.setattr(relation_store, "upsert", fail_upsert)
        with pytest.raises(failure_type, match="forced relation write failure"):
            index_file(
                path="pkg/mod.py",
                source_text="def new():\n    pass\n",
                observed_at="2026-08-31T01:00:00Z",
                symbol_store=symbol_store,
                relation_store=relation_store,
                index_meta_store=index_meta_store,
            )

        assert symbol_store.get("pkg/mod.py:old#function") is not None
        assert symbol_store.get("pkg/mod.py:new#function") is None
        assert index_meta_store.current_generation() == generation_before

        monkeypatch.setattr(relation_store, "upsert", original_upsert)
        result = index_file(
            path="pkg/mod.py",
            source_text="def new():\n    pass\n",
            observed_at="2026-08-31T02:00:00Z",
            symbol_store=symbol_store,
            relation_store=relation_store,
            index_meta_store=index_meta_store,
        )
        assert result.skipped is False
        assert symbol_store.get("pkg/mod.py:new#function") is not None
        assert index_meta_store.current_generation() == generation_before + 1
    finally:
        conn.close()
