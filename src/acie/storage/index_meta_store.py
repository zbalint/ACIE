import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    generation INTEGER NOT NULL,
    head_sha TEXT
);
"""


class IndexMetaStore:
    """Tracks a repo's single monotonic index_generation counter, plus the
    git HEAD sha this repo was last fully reconciled against.

    Coarse by design (see ARCHITECTURE.md "MCP Tool Surface" -- every MCP
    response carries an index-generation stamp): one row, one counter,
    bumped by index_file on every successful (non-skipped) reindex of any
    file in the repo. There is no per-file or per-symbol generation -- a
    stale symbol/edge ID from before ANY reindex in the repo is what this
    guards against, not fine-grained staleness.

    `head_sha` backs the git-hook (tier 2) reindex path: rather than trust
    hook-supplied old/new SHAs (post-commit/post-merge don't reliably
    provide them -- see the watcher/incremental-indexing grilling decision
    11), `notify-hook --agent git` diffs the current `git rev-parse HEAD`
    against this stored value itself. NULL means "never recorded" -- a
    fresh repo with nothing to diff against yet, since bootstrap already
    indexes everything from scratch.
    """

    def __init__(self, db_path: str = ":memory:", *, conn: sqlite3.Connection | None = None) -> None:
        # See SymbolStore.__init__ for why an already-open conn is accepted.
        self._conn = conn if conn is not None else sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._migrate_add_head_sha_column_if_missing()
        self._conn.execute(
            "INSERT OR IGNORE INTO index_meta (id, generation, head_sha) VALUES (0, 0, NULL)"
        )
        self._conn.commit()

    def _migrate_add_head_sha_column_if_missing(self) -> None:
        # CREATE TABLE IF NOT EXISTS (_SCHEMA above) is a no-op against a
        # real pre-existing index.sqlite from before head_sha existed --
        # every such repo's daemon would otherwise hard-fail on next open
        # (codex review, 2026-09-02). Idempotent: PRAGMA table_info is
        # cheap and this runs on every construction, same cost profile as
        # the executescript/INSERT OR IGNORE calls around it.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(index_meta)")}
        if "head_sha" not in columns:
            self._conn.execute("ALTER TABLE index_meta ADD COLUMN head_sha TEXT")
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

    def get_last_indexed_head_sha(self) -> str | None:
        row = self._conn.execute("SELECT head_sha FROM index_meta WHERE id = 0").fetchone()
        return row[0]

    def set_last_indexed_head_sha(self, sha: str) -> None:
        self._conn.execute("UPDATE index_meta SET head_sha = ? WHERE id = 0", (sha,))
        self._conn.commit()
