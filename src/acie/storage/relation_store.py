import sqlite3

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance

# Shared between _SCHEMA (fresh install) and the overrides-predicate
# migration below (rebuilding an existing relations_live table) so the
# column list/CHECK constraint has exactly one source of truth.
_RELATIONS_LIVE_COLUMNS = """(
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    predicate TEXT NOT NULL CHECK (predicate IN ('imports', 'calls', 'references', 'defines', 'inherits', 'overrides')),
    site_file TEXT NOT NULL,
    site_line INTEGER NOT NULL,
    site_col INTEGER NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
    provenance_provider TEXT NOT NULL,
    provenance_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source, target, predicate, site_file, site_line, site_col)
)"""

_RELATIONS_LIVE_COLUMN_NAMES = (
    "source, target, predicate, site_file, site_line, site_col, "
    "confidence, provenance_provider, provenance_version, observed_at"
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS relations_live {_RELATIONS_LIVE_COLUMNS};

CREATE TABLE IF NOT EXISTS relations_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    predicate TEXT NOT NULL,
    site_file TEXT NOT NULL,
    site_line INTEGER NOT NULL,
    site_col INTEGER NOT NULL,
    confidence TEXT,
    provenance_provider TEXT,
    provenance_version TEXT,
    observed_at TEXT NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0
);
"""


class RelationStore:
    def __init__(self, db_path: str = ":memory:", *, conn: sqlite3.Connection | None = None) -> None:
        # See SymbolStore.__init__ for why an already-open conn is accepted.
        self._conn = conn if conn is not None else sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._migrate_add_overrides_predicate_if_missing()

    def _migrate_add_overrides_predicate_if_missing(self) -> None:
        """CREATE TABLE IF NOT EXISTS (_SCHEMA above) is a no-op against a
        real pre-existing index.sqlite from before 'overrides' was added to
        the predicate CHECK constraint -- every such repo would otherwise
        reject any overrides relation with a CHECK-constraint IntegrityError
        forever (code review finding, 2026-09-02). SQLite has no ALTER TABLE
        support for modifying a CHECK constraint in place (unlike
        IndexMetaStore's simpler ADD COLUMN migrations), so this is a
        create-copy-drop-rename rebuild of relations_live.

        `with self._conn:` protects everything from the INSERT onward (the
        first DML statement, which is where Python's legacy sqlite3
        isolation mode auto-opens an implicit transaction) but NOT the
        leading CREATE TABLE itself -- as the very first statement issued
        on the connection, with no transaction open yet, it auto-commits
        immediately and independently (agy/gemini review finding,
        2026-09-02, verified empirically: a `with conn:` block's own
        exception does not undo a CREATE TABLE that was its first
        statement). So a crash between that CREATE and the final RENAME
        below leaves `relations_live__migrating` committed on disk (no data
        loss -- `relations_live` itself is only ever touched from the INSERT
        onward, which *does* roll back together) but would otherwise wedge
        every future call to this method with "table already exists". The
        leading `DROP TABLE IF EXISTS` makes retrying (e.g. on the next
        daemon startup after such a crash) self-healing rather than fatal.
        relations_history carries no predicate CHECK at all (see _SCHEMA)
        and needs no equivalent migration.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations_live'"
        ).fetchone()
        if row is None or "'overrides'" in row[0]:
            return
        self._conn.execute("DROP TABLE IF EXISTS relations_live__migrating")
        with self._conn:
            self._conn.execute(f"CREATE TABLE relations_live__migrating {_RELATIONS_LIVE_COLUMNS}")
            self._conn.execute(
                f"INSERT INTO relations_live__migrating ({_RELATIONS_LIVE_COLUMN_NAMES}) "
                f"SELECT {_RELATIONS_LIVE_COLUMN_NAMES} FROM relations_live"
            )
            self._conn.execute("DROP TABLE relations_live")
            self._conn.execute("ALTER TABLE relations_live__migrating RENAME TO relations_live")

    def upsert(self, relation: Relation) -> None:
        existing = self.get(
            source=relation.source,
            target=relation.target,
            predicate=relation.predicate,
            site_file=relation.site_file,
            site_line=relation.site_line,
            site_col=relation.site_col,
        )
        content_changed = existing is None or _content_differs(existing, relation)

        self._conn.execute(
            """
            INSERT INTO relations_live (
                source, target, predicate, site_file, site_line, site_col,
                confidence, provenance_provider, provenance_version, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, target, predicate, site_file, site_line, site_col) DO UPDATE SET
                confidence = excluded.confidence,
                provenance_provider = excluded.provenance_provider,
                provenance_version = excluded.provenance_version,
                observed_at = excluded.observed_at
            """,
            (
                relation.source,
                relation.target,
                relation.predicate,
                relation.site_file,
                relation.site_line,
                relation.site_col,
                relation.confidence.value,
                relation.provenance.provider,
                relation.provenance.version,
                relation.provenance.observed_at,
            ),
        )
        if content_changed:
            self._conn.execute(
                """
                INSERT INTO relations_history (
                    source, target, predicate, site_file, site_line, site_col,
                    confidence, provenance_provider, provenance_version, observed_at, tombstone
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    relation.source,
                    relation.target,
                    relation.predicate,
                    relation.site_file,
                    relation.site_line,
                    relation.site_col,
                    relation.confidence.value,
                    relation.provenance.provider,
                    relation.provenance.version,
                    relation.provenance.observed_at,
                ),
            )
        self._conn.commit()

    def history(
        self,
        *,
        source: str,
        target: str,
        predicate: str,
        site_file: str,
        site_line: int,
        site_col: int,
    ) -> list[Relation]:
        rows = self._conn.execute(
            """
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_history
            WHERE source = ? AND target = ? AND predicate = ?
              AND site_file = ? AND site_line = ? AND site_col = ? AND tombstone = 0
            ORDER BY history_id ASC
            """,
            (source, target, predicate, site_file, site_line, site_col),
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def get(
        self,
        *,
        source: str,
        target: str,
        predicate: str,
        site_file: str,
        site_line: int,
        site_col: int,
    ) -> Relation | None:
        row = self._conn.execute(
            """
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_live
            WHERE source = ? AND target = ? AND predicate = ?
              AND site_file = ? AND site_line = ? AND site_col = ?
            """,
            (source, target, predicate, site_file, site_line, site_col),
        ).fetchone()
        if row is None:
            return None
        return _row_to_relation(row)

    def delete(
        self,
        *,
        source: str,
        target: str,
        predicate: str,
        site_file: str,
        site_line: int,
        site_col: int,
        observed_at: str,
    ) -> None:
        self._conn.execute(
            """
            DELETE FROM relations_live
            WHERE source = ? AND target = ? AND predicate = ?
              AND site_file = ? AND site_line = ? AND site_col = ?
            """,
            (source, target, predicate, site_file, site_line, site_col),
        )
        self._conn.execute(
            """
            INSERT INTO relations_history (
                source, target, predicate, site_file, site_line, site_col,
                confidence, provenance_provider, provenance_version, observed_at, tombstone
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 1)
            """,
            (source, target, predicate, site_file, site_line, site_col, observed_at),
        )
        self._conn.commit()

    def list_by_site_file(
        self,
        site_file: str,
        *,
        predicates: set[str] | None = None,
    ) -> list[Relation]:
        """Live relations sited in the given file, optionally restricted to
        a predicate set. Used by list_imports to scope to 'imports' edges
        without a separate near-identical query method.
        """
        clauses = ["site_file = ?"]
        params: list = [site_file]
        if predicates is not None:
            placeholders = ", ".join("?" for _ in predicates)
            clauses.append(f"predicate IN ({placeholders})")
            params.extend(predicates)

        rows = self._conn.execute(
            f"""
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_live WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_by_site(
        self,
        *,
        site_file: str,
        site_line: int,
        site_col: int,
        predicates: set[str] | None = None,
    ) -> list[Relation]:
        """Live relations sited at the exact (site_file, site_line, site_col)
        point, optionally restricted to a predicate set. Used by
        get_definition's position resolution: relations store only a site
        *point* (no end position), so this is an exact match, not a
        containment query.
        """
        clauses = ["site_file = ?", "site_line = ?", "site_col = ?"]
        params: list = [site_file, site_line, site_col]
        if predicates is not None:
            placeholders = ", ".join("?" for _ in predicates)
            clauses.append(f"predicate IN ({placeholders})")
            params.extend(predicates)

        rows = self._conn.execute(
            f"""
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_live WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_by_target(
        self,
        target: str,
        *,
        predicates: set[str] | None = None,
    ) -> list[Relation]:
        """Live relations whose target is exactly the given symbol id,
        optionally restricted to a predicate set. Used by find_references
        to list every reference site pointing at a resolved symbol.
        """
        clauses = ["target = ?"]
        params: list = [target]
        if predicates is not None:
            placeholders = ", ".join("?" for _ in predicates)
            clauses.append(f"predicate IN ({placeholders})")
            params.extend(predicates)

        rows = self._conn.execute(
            f"""
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_live WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_by_source(
        self,
        source: str,
        *,
        predicates: set[str] | None = None,
    ) -> list[Relation]:
        """Live relations whose source is exactly the given symbol id,
        optionally restricted to a predicate set. Used by graph's
        downstream traversal to list every outbound edge from a symbol.
        """
        clauses = ["source = ?"]
        params: list = [source]
        if predicates is not None:
            placeholders = ", ".join("?" for _ in predicates)
            clauses.append(f"predicate IN ({placeholders})")
            params.extend(predicates)

        rows = self._conn.execute(
            f"""
            SELECT source, target, predicate, site_file, site_line, site_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM relations_live WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def is_tombstoned(
        self,
        *,
        source: str,
        target: str,
        predicate: str,
        site_file: str,
        site_line: int,
        site_col: int,
    ) -> bool:
        row = self._conn.execute(
            """
            SELECT tombstone FROM relations_history
            WHERE source = ? AND target = ? AND predicate = ?
              AND site_file = ? AND site_line = ? AND site_col = ?
            ORDER BY history_id DESC LIMIT 1
            """,
            (source, target, predicate, site_file, site_line, site_col),
        ).fetchone()
        return row is not None and bool(row[0])


def _content_differs(a: Relation, b: Relation) -> bool:
    """Compares everything except provenance.observed_at.

    The composite key (source, target, predicate, site_*) already fully
    identifies a relation, so the only "content" that can change between
    re-observations of the same key is confidence/provenance -- unlike
    Symbol, there is no separate span to compare.
    """
    return (
        a.confidence != b.confidence
        or a.provenance.provider != b.provenance.provider
        or a.provenance.version != b.provenance.version
    )


def _row_to_relation(row: tuple) -> Relation:
    (
        source,
        target,
        predicate,
        site_file,
        site_line,
        site_col,
        confidence,
        provenance_provider,
        provenance_version,
        observed_at,
    ) = row
    return Relation(
        source=source,
        target=target,
        predicate=predicate,
        site_file=site_file,
        site_line=site_line,
        site_col=site_col,
        confidence=Confidence(confidence),
        provenance=Provenance(
            provider=provenance_provider, version=provenance_version, observed_at=observed_at
        ),
    )
