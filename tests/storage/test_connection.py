import sqlite3
import threading
import time

from acie.storage.connection import open_connection


def test_open_connection_sets_wal_journal_mode_for_a_real_file(tmp_path):
    db_path = str(tmp_path / "index.sqlite")

    conn = open_connection(db_path)

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_open_connection_is_a_harmless_noop_for_in_memory_db():
    # WAL requires a real file -- SQLite silently keeps ":memory:" databases
    # on "memory" journal mode instead of erroring. Every store's test
    # suite defaults to ":memory:", so this must never raise.
    conn = open_connection(":memory:")

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"


def test_a_reader_is_not_blocked_by_a_writers_uncommitted_transaction(tmp_path):
    """Direct proof of the ACIE bug-2 fix (SALTMDB memory c90f7a6e).

    Without WAL mode, a writer's uncommitted transaction takes a lock that
    blocks every reader until the writer commits (or the reader's own
    busy-timeout expires) -- however long that commit takes, e.g. a slow
    fsync. Under WAL, a reader sees the last-committed snapshot and is
    never blocked by an in-progress writer, regardless of how long the
    writer's own commit takes.
    """
    db_path = str(tmp_path / "index.sqlite")
    writer = open_connection(db_path)
    writer.execute("CREATE TABLE t (v INTEGER)")
    writer.commit()

    writer.execute("INSERT INTO t VALUES (1)")  # uncommitted -- writer holds an open transaction

    # A short busy-timeout: if the reader were blocked by the writer's open
    # transaction, this would raise "database is locked" after ~0.5s
    # instead of succeeding immediately.
    reader = sqlite3.connect(db_path, timeout=0.5)
    row_count = reader.execute("SELECT count(*) FROM t").fetchone()[0]

    assert row_count == 0  # writer's insert isn't committed yet -- reader sees the prior snapshot
    writer.commit()


def test_open_connection_retries_the_wal_transition_when_it_races_an_open_transaction(tmp_path):
    """The very first-ever WAL transition for a file needs exclusive
    access and fails ("database is locked") if another connection holds an
    open write transaction at that exact moment -- unlike an ordinary
    read/write, this specific failure does not respect sqlite3's own
    busy_timeout retry loop, so open_connection must retry it manually.
    """
    db_path = str(tmp_path / "index.sqlite")
    sqlite3.connect(db_path).execute("CREATE TABLE t (v INTEGER)")  # plain connection, DELETE mode, never WAL yet

    blocker = sqlite3.connect(db_path, check_same_thread=False)
    blocker.execute("INSERT INTO t VALUES (1)")  # uncommitted -- holds the lock the WAL transition needs

    def commit_shortly_after_a_delay() -> None:
        time.sleep(0.005)  # longer than a single retry attempt, shorter than the full retry budget
        blocker.commit()

    threading.Thread(target=commit_shortly_after_a_delay).start()

    conn = open_connection(db_path)  # must retry past the still-open transaction rather than giving up on the first try

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_open_connection_never_raises_even_if_every_wal_transition_retry_is_exhausted(tmp_path):
    """Losing the WAL-transition race entirely must not break the caller --
    this codebase's writers commit fast, so a real permanent loss is rare,
    but a caller that gets a connection either way (WAL or not) can safely
    proceed either now or on its next reconnect.
    """
    db_path = str(tmp_path / "index.sqlite")
    sqlite3.connect(db_path).execute("CREATE TABLE t (v INTEGER)")

    blocker = sqlite3.connect(db_path)
    blocker.execute("INSERT INTO t VALUES (1)")  # held open for the whole test -- outlasts every retry attempt

    conn = open_connection(db_path)  # must not raise, even though every retry loses the race

    assert conn.execute("SELECT 1").fetchone() == (1,)
    blocker.commit()
