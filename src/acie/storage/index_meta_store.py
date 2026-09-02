import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    generation INTEGER NOT NULL,
    head_sha TEXT,
    cross_file_pass_done INTEGER NOT NULL DEFAULT 0
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

    `cross_file_pass_done` is a narrow, single-purpose migration flag, not
    a general schema/IR version (ARCHITECTURE.md's "Not Yet Specified"
    still defers that as unbuilt): it exists solely so BootstrapCoordinator
    can tell whether a repo's already-existing index.sqlite (trusted ready
    immediately, per its own docstring) has ever had the cross-file-call
    resolution catch-up pass run against it, since repo_ready()'s
    file-existence check would otherwise skip that pass forever on an
    upgraded installation (codex review finding). Defaults to 0/false so
    every pre-existing index.sqlite from before this flag existed correctly
    reports "not yet done" via the same lazy-migration idiom as head_sha.
    """

    def __init__(self, db_path: str = ":memory:", *, conn: sqlite3.Connection | None = None) -> None:
        # See SymbolStore.__init__ for why an already-open conn is accepted.
        self._conn = conn if conn is not None else sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._migrate_add_head_sha_column_if_missing()
        self._migrate_add_cross_file_pass_done_column_if_missing()
        self._conn.execute(
            "INSERT OR IGNORE INTO index_meta (id, generation, head_sha, cross_file_pass_done) VALUES (0, 0, NULL, 0)"
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

    def _migrate_add_cross_file_pass_done_column_if_missing(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(index_meta)")}
        if "cross_file_pass_done" not in columns:
            self._conn.execute(
                "ALTER TABLE index_meta ADD COLUMN cross_file_pass_done INTEGER NOT NULL DEFAULT 0"
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

    def get_last_indexed_head_sha(self) -> str | None:
        row = self._conn.execute("SELECT head_sha FROM index_meta WHERE id = 0").fetchone()
        return row[0]

    def set_last_indexed_head_sha(self, sha: str) -> None:
        self._conn.execute("UPDATE index_meta SET head_sha = ? WHERE id = 0", (sha,))
        self._conn.commit()

    def cross_file_pass_done(self) -> bool:
        row = self._conn.execute(
            "SELECT cross_file_pass_done FROM index_meta WHERE id = 0"
        ).fetchone()
        return bool(row[0])

    def mark_cross_file_pass_done(self) -> None:
        self._conn.execute("UPDATE index_meta SET cross_file_pass_done = 1 WHERE id = 0")
        self._conn.commit()
