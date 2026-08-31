import sqlite3

from acie.ir.symbol import Confidence, Provenance, Symbol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols_live (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    qualname TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('module', 'class', 'function', 'method', 'variable')),
    start_line INTEGER NOT NULL,
    start_col INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_col INTEGER NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
    provenance_provider TEXT NOT NULL,
    provenance_version TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    path TEXT,
    qualname TEXT,
    kind TEXT,
    start_line INTEGER,
    start_col INTEGER,
    end_line INTEGER,
    end_col INTEGER,
    confidence TEXT,
    provenance_provider TEXT,
    provenance_version TEXT,
    observed_at TEXT NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0
);
"""


class SymbolStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        # SQLite's LIKE is case-insensitive for ASCII by default -- Python
        # identifiers are case-sensitive, so search() (qualname substring
        # match) must be too, or "Foo" and "foo" would collide.
        self._conn.execute("PRAGMA case_sensitive_like = ON")

    def upsert(self, symbol: Symbol) -> None:
        existing = self.get(symbol.id)
        content_changed = existing is None or _content_differs(existing, symbol)

        self._conn.execute(
            """
            INSERT INTO symbols_live (
                id, path, qualname, kind, start_line, start_col, end_line, end_col,
                confidence, provenance_provider, provenance_version, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path = excluded.path,
                qualname = excluded.qualname,
                kind = excluded.kind,
                start_line = excluded.start_line,
                start_col = excluded.start_col,
                end_line = excluded.end_line,
                end_col = excluded.end_col,
                confidence = excluded.confidence,
                provenance_provider = excluded.provenance_provider,
                provenance_version = excluded.provenance_version,
                observed_at = excluded.observed_at
            """,
            (
                symbol.id,
                symbol.path,
                symbol.qualname,
                symbol.kind,
                symbol.start_line,
                symbol.start_col,
                symbol.end_line,
                symbol.end_col,
                symbol.confidence.value,
                symbol.provenance.provider,
                symbol.provenance.version,
                symbol.provenance.observed_at,
            ),
        )
        if content_changed:
            self._conn.execute(
                """
                INSERT INTO symbols_history (
                    id, path, qualname, kind, start_line, start_col, end_line, end_col,
                    confidence, provenance_provider, provenance_version, observed_at, tombstone
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    symbol.id,
                    symbol.path,
                    symbol.qualname,
                    symbol.kind,
                    symbol.start_line,
                    symbol.start_col,
                    symbol.end_line,
                    symbol.end_col,
                    symbol.confidence.value,
                    symbol.provenance.provider,
                    symbol.provenance.version,
                    symbol.provenance.observed_at,
                ),
            )
        self._conn.commit()

    def history(self, symbol_id: str) -> list[Symbol]:
        rows = self._conn.execute(
            """
            SELECT id, path, qualname, kind, start_line, start_col, end_line, end_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM symbols_history WHERE id = ? AND tombstone = 0 ORDER BY history_id ASC
            """,
            (symbol_id,),
        ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def delete(self, symbol_id: str, observed_at: str) -> None:
        self._conn.execute("DELETE FROM symbols_live WHERE id = ?", (symbol_id,))
        self._conn.execute(
            """
            INSERT INTO symbols_history (
                id, path, qualname, kind, start_line, start_col, end_line, end_col,
                confidence, provenance_provider, provenance_version, observed_at, tombstone
            ) VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, 1)
            """,
            (symbol_id, observed_at),
        )
        self._conn.commit()

    def list_by_path(self, path: str) -> list[Symbol]:
        rows = self._conn.execute(
            """
            SELECT id, path, qualname, kind, start_line, start_col, end_line, end_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM symbols_live WHERE path = ?
            """,
            (path,),
        ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def search(
        self, *, qualname_substring: str, kind: str | None = None, path_glob: str | None = None
    ) -> list[Symbol]:
        """Live symbols whose qualname contains qualname_substring, ordered
        by id ascending (find_symbol relies on this ordering for its
        keyset-cursor pagination). kind/path_glob narrow further when given;
        path_glob uses SQLite's native GLOB (Unix shell-style * and ?).
        """
        clauses = ["qualname LIKE ? ESCAPE '\\'"]
        params: list = [f"%{_escape_like(qualname_substring)}%"]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if path_glob is not None:
            clauses.append("path GLOB ?")
            params.append(path_glob)

        rows = self._conn.execute(
            f"""
            SELECT id, path, qualname, kind, start_line, start_col, end_line, end_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM symbols_live WHERE {" AND ".join(clauses)} ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def at_start(self, *, path: str, line: int, col: int) -> Symbol | None:
        """The live symbol whose own defining start position is exactly
        (path, line, col), or None. Used by get_definition's position
        resolution to recognize "cursor is already sitting on a def line".
        """
        row = self._conn.execute(
            """
            SELECT id, path, qualname, kind, start_line, start_col, end_line, end_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM symbols_live WHERE path = ? AND start_line = ? AND start_col = ?
            """,
            (path, line, col),
        ).fetchone()
        if row is None:
            return None
        return _row_to_symbol(row)

    def is_tombstoned(self, symbol_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT tombstone FROM symbols_history WHERE id = ?
            ORDER BY history_id DESC LIMIT 1
            """,
            (symbol_id,),
        ).fetchone()
        return row is not None and bool(row[0])

    def get(self, symbol_id: str) -> Symbol | None:
        row = self._conn.execute(
            """
            SELECT id, path, qualname, kind, start_line, start_col, end_line, end_col,
                   confidence, provenance_provider, provenance_version, observed_at
            FROM symbols_live WHERE id = ?
            """,
            (symbol_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_symbol(row)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _content_differs(a: Symbol, b: Symbol) -> bool:
    """Compares everything except provenance.observed_at.

    A reindex re-confirming the same fact must still update the live row's
    observed_at (upsert always updates live), but must not grow history --
    per ARCHITECTURE.md, history rows are appended only when something
    actually changes, not on every reparse.
    """
    return (
        a.path != b.path
        or a.qualname != b.qualname
        or a.kind != b.kind
        or a.start_line != b.start_line
        or a.start_col != b.start_col
        or a.end_line != b.end_line
        or a.end_col != b.end_col
        or a.confidence != b.confidence
        or a.provenance.provider != b.provenance.provider
        or a.provenance.version != b.provenance.version
    )


def _row_to_symbol(row: tuple) -> Symbol:
    (
        id_,
        path,
        qualname,
        kind,
        start_line,
        start_col,
        end_line,
        end_col,
        confidence,
        provenance_provider,
        provenance_version,
        observed_at,
    ) = row
    return Symbol(
        id=id_,
        path=path,
        qualname=qualname,
        kind=kind,
        start_line=start_line,
        start_col=start_col,
        end_line=end_line,
        end_col=end_col,
        confidence=Confidence(confidence),
        provenance=Provenance(
            provider=provenance_provider, version=provenance_version, observed_at=observed_at
        ),
    )
