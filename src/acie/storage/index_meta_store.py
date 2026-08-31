import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    generation INTEGER NOT NULL
);
"""


class IndexMetaStore:
    """Tracks a repo's single monotonic index_generation counter.

    Coarse by design (see ARCHITECTURE.md "MCP Tool Surface" -- every MCP
    response carries an index-generation stamp): one row, one counter,
    bumped by index_file on every successful (non-skipped) reindex of any
    file in the repo. There is no per-file or per-symbol generation -- a
    stale symbol/edge ID from before ANY reindex in the repo is what this
    guards against, not fine-grained staleness.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO index_meta (id, generation) VALUES (0, 0)"
        )
        self._conn.commit()

    def current_generation(self) -> int:
        row = self._conn.execute(
            "SELECT generation FROM index_meta WHERE id = 0"
        ).fetchone()
        return row[0]

    def bump_generation(self) -> int:
        self._conn.execute("UPDATE index_meta SET generation = generation + 1 WHERE id = 0")
        self._conn.commit()
        return self.current_generation()
