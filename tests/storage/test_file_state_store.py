from acie.storage.file_state_store import FileStateStore


def test_get_returns_none_for_a_path_never_recorded():
    store = FileStateStore(":memory:")

    assert store.get("src/foo.py") is None


def test_set_then_get_round_trips_mtime_and_hash():
    store = FileStateStore(":memory:")

    store.set("src/foo.py", mtime_ns=123456789, content_hash="abc123")
    result = store.get("src/foo.py")

    assert result is not None
    assert result.mtime_ns == 123456789
    assert result.content_hash == "abc123"


def test_set_again_for_the_same_path_overwrites_rather_than_duplicating():
    store = FileStateStore(":memory:")

    store.set("src/foo.py", mtime_ns=1, content_hash="first")
    store.set("src/foo.py", mtime_ns=2, content_hash="second")
    result = store.get("src/foo.py")

    assert result.mtime_ns == 2
    assert result.content_hash == "second"


def test_delete_removes_a_recorded_path():
    store = FileStateStore(":memory:")
    store.set("src/foo.py", mtime_ns=1, content_hash="first")

    store.delete("src/foo.py")

    assert store.get("src/foo.py") is None


def test_delete_of_a_never_recorded_path_is_a_no_op():
    store = FileStateStore(":memory:")

    store.delete("src/never_seen.py")  # must not raise

    assert store.get("src/never_seen.py") is None


def test_state_persists_across_store_instances_on_the_same_db_path(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    first_instance = FileStateStore(db_path)
    first_instance.set("src/foo.py", mtime_ns=1, content_hash="first")

    reopened = FileStateStore(db_path)

    result = reopened.get("src/foo.py")
    assert result.mtime_ns == 1
    assert result.content_hash == "first"


def test_conn_kwarg_reuses_an_already_open_connection_instead_of_opening_its_own():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    writer = FileStateStore(conn=conn)
    writer.set("src/foo.py", mtime_ns=1, content_hash="first")

    reader = FileStateStore(conn=conn)

    assert reader.get("src/foo.py").content_hash == "first"


def test_distinct_paths_are_tracked_independently():
    store = FileStateStore(":memory:")

    store.set("src/foo.py", mtime_ns=1, content_hash="foo-hash")
    store.set("src/bar.py", mtime_ns=2, content_hash="bar-hash")

    assert store.get("src/foo.py").content_hash == "foo-hash"
    assert store.get("src/bar.py").content_hash == "bar-hash"
