import sqlite3

from acie.storage.connection import open_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    generation INTEGER NOT NULL,
    head_sha TEXT,
    cross_file_pass_version INTEGER NOT NULL DEFAULT 0
);
"""

# 1 = cross-file `calls` resolution only (pre-slice-A2 catch-up pass).
# 2 = + cross-file `inherits` resolution (slice A2).
# 3 = + cross-file `overrides` resolution (slice A3). Bump this whenever a
# future slice adds another predicate the catch-up pass must retroactively
# resolve for an already-migrated repo -- see cross_file_pass_done()'s
# docstring for why a plain boolean can't represent "which version of the
# pass" already ran.
CURRENT_CROSS_FILE_PASS_VERSION = 3


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

    `cross_file_pass_version`/`cross_file_pass_done()` back a narrow,
    single-purpose migration marker, not a general schema/IR version
    (ARCHITECTURE.md's "Not Yet Specified" still defers that as unbuilt):
    it exists solely so BootstrapCoordinator can tell whether a repo's
    already-existing index.sqlite (trusted ready immediately, per its own
    docstring) has had the current cross-file-resolution catch-up pass run
    against it, since repo_ready()'s file-existence check would otherwise
    skip that pass forever on an upgraded installation (codex review
    finding, slice C). Defaults to 0 so every pre-existing index.sqlite
    from before this column existed correctly reports "not yet done" via
    the same lazy-migration idiom as head_sha.

    Stored as a monotonic version int, not a boolean (codex review finding,
    slice A2): a plain "done" flag can only ever mean "the pass that
    existed when it was set has run" -- it cannot tell a repo that
    completed the ORIGINAL calls-only catch-up (pre-A2) apart from one that
    has also completed the newer calls+inherits catch-up (A2+), so a
    boolean would leave the first kind of repo stuck forever without
    inherits resolution once A2 shipped, exactly like the original
    pre-cross-file-resolution repos this mechanism was built to catch up.
    `cross_file_pass_done()` compares the stored version against
    `CURRENT_CROSS_FILE_PASS_VERSION`, so a repo below the current version
    (including a legacy boolean-flag repo carried forward as version 1 --
    see the migration method below) correctly reports "not yet done" and
    gets the catch-up pass re-run once more.
    """

    def __init__(self, db_path: str = ":memory:", *, conn: sqlite3.Connection | None = None) -> None:
        # See SymbolStore.__init__ for why an already-open conn is accepted.
        self._conn = conn if conn is not None else open_connection(db_path)
        self._conn.executescript(_SCHEMA)
        self._migrate_add_head_sha_column_if_missing()
        self._migrate_add_cross_file_pass_version_column_if_missing()
        self._conn.execute(
            "INSERT OR IGNORE INTO index_meta (id, generation, head_sha, cross_file_pass_version) VALUES (0, 0, NULL, 0)"
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

    def _migrate_add_cross_file_pass_version_column_if_missing(self) -> None:
        """Same lazy-migration idiom as head_sha. Also carries a legacy
        boolean `cross_file_pass_done` column's value forward (codex review
        finding, slice A2): a pre-A2 index.sqlite that already completed
        the calls-only catch-up has that old column set to 1 -- read once
        here and translated to version 1 (still below
        CURRENT_CROSS_FILE_PASS_VERSION), so such a repo correctly gets the
        new inherits catch-up pass re-run instead of being silently skipped
        forever. A repo with neither column (pre-dates any cross-file
        resolution) or the old column at 0 both correctly land at version 0.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(index_meta)")}
        if "cross_file_pass_version" in columns:
            return
        self._conn.execute(
            "ALTER TABLE index_meta ADD COLUMN cross_file_pass_version INTEGER NOT NULL DEFAULT 0"
        )
        if "cross_file_pass_done" in columns:
            row = self._conn.execute("SELECT cross_file_pass_done FROM index_meta WHERE id = 0").fetchone()
            if row and row[0]:
                self._conn.execute("UPDATE index_meta SET cross_file_pass_version = 1 WHERE id = 0")
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
            "SELECT cross_file_pass_version FROM index_meta WHERE id = 0"
        ).fetchone()
        return row[0] >= CURRENT_CROSS_FILE_PASS_VERSION

    def mark_cross_file_pass_done(self) -> None:
        self._conn.execute(
            "UPDATE index_meta SET cross_file_pass_version = ? WHERE id = 0",
            (CURRENT_CROSS_FILE_PASS_VERSION,),
        )
        self._conn.commit()
