import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    content_hash: str


class FileStateStore:
    """Per-file mtime+hash staleness state for the watcher's hybrid check.

    See ARCHITECTURE.md's (now-resolved) "Not Yet Specified" staleness
    mechanic decision: mtime is checked first (cheap), content_hash only
    matters when mtime differs from what's stored here. Lives in
    index.sqlite, same as SymbolStore/RelationStore/IndexMetaStore -- see
    those for why an already-open `conn` is accepted (the daemon's
    write-queue worker thread passes its one per-repo connection to every
    store a queued job constructs).
    """

    def __init__(self, db_path: str = ":memory:", *, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn if conn is not None else sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)

    def get(self, path: str) -> FileState | None:
        row = self._conn.execute(
            "SELECT mtime_ns, content_hash FROM file_state WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        return FileState(mtime_ns=row[0], content_hash=row[1])

    def set(self, path: str, mtime_ns: int, content_hash: str) -> None:
        self._conn.execute(
            """
            INSERT INTO file_state (path, mtime_ns, content_hash) VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                content_hash = excluded.content_hash
            """,
            (path, mtime_ns, content_hash),
        )
        self._conn.commit()

    def delete(self, path: str) -> None:
        self._conn.execute("DELETE FROM file_state WHERE path = ?", (path,))
        self._conn.commit()
