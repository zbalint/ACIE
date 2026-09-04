"""Shared connection-opening seam for every store's raw `sqlite3.connect`.

See ACIE bug-2 investigation, SALTMDB memory `c90f7a6e` (2026-09-04, live
kernel-traced on the running daemon): the default rollback-journal mode
gives a writer's commit an exclusive lock that blocks every reader for the
commit's whole duration, however long that turns out to be (in the live
incident, a WSL2-side ext4 journal-commit stall). `write_queue.py`'s
single-writer-thread-per-repo serialization was never the problem and is
unchanged by this -- the gap was purely reader-vs-writer isolation at the
SQLite layer.
"""

import sqlite3
import time

# journal_mode=WAL is a persistent, file-level setting: once any connection
# has switched a database file to WAL, every later connection (including a
# concurrent one) opens in WAL mode for free with no further contention --
# verified empirically, not assumed (see the "Do NOT" note below). Only the
# very first-ever transition for a given file can lose a race: if another
# connection holds an open (uncommitted) write transaction at that exact
# moment, SQLite raises "database is locked" for the PRAGMA -- and, unlike
# an ordinary read/write, this specific failure mode does NOT respect
# sqlite3's own busy_timeout retry loop (SQLITE_LOCKED, not SQLITE_BUSY),
# so a manual retry is required. This codebase's writers commit after every
# row (SymbolStore.upsert/RelationStore.upsert etc.), so any given
# transaction is open for microseconds -- a handful of short retries all
# but always lands after the very first one.
_WAL_TRANSITION_RETRIES = 20
_WAL_TRANSITION_RETRY_DELAY_SECONDS = 0.001


def open_connection(db_path: str) -> sqlite3.Connection:
    """Opens one sqlite3 connection with this codebase's standard pragmas.

    WAL mode lets a reader see a consistent snapshot without ever waiting
    on an in-progress writer's commit, no matter how slow that commit's own
    fsync is -- writers still fully serialize against each other exactly as
    before. `synchronous = NORMAL` is WAL's own documented pairing (safe
    against an app crash or OS crash, fsyncs only at checkpoints rather
    than every commit). A harmless no-op for ":memory:" -- SQLite silently
    keeps an in-memory database on "memory" journal mode instead of
    erroring, and every store's test suite defaults to ":memory:".

    Do NOT remove the retry loop as unnecessary "just try once" -- live-
    reproduced without it (ACIE bug-2 fix, TDD): `tests/daemon/
    test_bootstrap.py`'s already-indexed-repo tests open many concurrent
    connections (the migration catch-up pass's own writer plus the test's
    own polling `RelationStore(db_path)` constructions) against a freshly
    created, never-yet-WAL file, and losing this race even once left that
    one connection permanently off WAL for its whole lifetime -- most
    consequentially for `write_queue.py`'s single long-lived writer
    connection per repo, which is exactly the connection this whole fix is
    for.
    """
    conn = sqlite3.connect(db_path)
    # Reading the current mode never needs exclusive access and never
    # contends with anything -- only ever attempt (and retry) the SET once
    # a real transition is actually needed, which after a file's very
    # first connection is essentially never (WAL is sticky per file), so
    # every later connection -- notably the read path's frequent
    # short-lived per-call connections -- pays no retry-loop cost at all.
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        for attempt in range(_WAL_TRANSITION_RETRIES):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                if attempt == _WAL_TRANSITION_RETRIES - 1:
                    break
                time.sleep(_WAL_TRANSITION_RETRY_DELAY_SECONDS)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
