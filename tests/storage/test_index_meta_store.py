from acie.storage.index_meta_store import IndexMetaStore


def test_current_generation_starts_at_zero_for_a_fresh_store():
    store = IndexMetaStore(":memory:")

    assert store.current_generation() == 0


def test_bump_generation_increments_and_returns_the_new_value():
    store = IndexMetaStore(":memory:")

    result = store.bump_generation()

    assert result == 1
    assert store.current_generation() == 1


def test_bump_generation_increments_monotonically_across_repeated_calls():
    store = IndexMetaStore(":memory:")

    store.bump_generation()
    store.bump_generation()
    third = store.bump_generation()

    assert third == 3
    assert store.current_generation() == 3


def test_generation_persists_across_store_instances_on_the_same_db_path(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    first_instance = IndexMetaStore(db_path)
    first_instance.bump_generation()
    first_instance.bump_generation()

    reopened = IndexMetaStore(db_path)

    assert reopened.current_generation() == 2
