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


def test_conn_kwarg_reuses_an_already_open_connection_instead_of_opening_its_own():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    writer = IndexMetaStore(conn=conn)
    writer.bump_generation()

    reader = IndexMetaStore(conn=conn)

    assert reader.current_generation() == 1


def test_last_indexed_head_sha_is_none_for_a_fresh_store():
    store = IndexMetaStore(":memory:")

    assert store.get_last_indexed_head_sha() is None


def test_set_then_get_last_indexed_head_sha_round_trips():
    store = IndexMetaStore(":memory:")

    store.set_last_indexed_head_sha("abc123")

    assert store.get_last_indexed_head_sha() == "abc123"


def test_set_last_indexed_head_sha_again_overwrites_the_prior_value():
    store = IndexMetaStore(":memory:")
    store.set_last_indexed_head_sha("first-sha")

    store.set_last_indexed_head_sha("second-sha")

    assert store.get_last_indexed_head_sha() == "second-sha"


def test_last_indexed_head_sha_persists_across_store_instances_on_the_same_db_path(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    first_instance = IndexMetaStore(db_path)
    first_instance.set_last_indexed_head_sha("abc123")

    reopened = IndexMetaStore(db_path)

    assert reopened.get_last_indexed_head_sha() == "abc123"


def test_setting_head_sha_does_not_disturb_the_generation_counter():
    store = IndexMetaStore(":memory:")
    store.bump_generation()

    store.set_last_indexed_head_sha("abc123")

    assert store.current_generation() == 1


def test_cross_file_pass_done_is_false_for_a_fresh_store():
    store = IndexMetaStore(":memory:")

    assert store.cross_file_pass_done() is False


def test_mark_cross_file_pass_done_flips_it_true_and_persists():
    store = IndexMetaStore(":memory:")

    store.mark_cross_file_pass_done()

    assert store.cross_file_pass_done() is True


def test_cross_file_pass_done_persists_across_store_instances_on_the_same_db_path(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    IndexMetaStore(db_path).mark_cross_file_pass_done()

    assert IndexMetaStore(db_path).cross_file_pass_done() is True


def test_opening_a_pre_existing_index_meta_table_without_cross_file_pass_done_migrates_it_in_place():
    # Same lazy-migration idiom as the head_sha regression above -- a
    # pre-existing index.sqlite from before this column existed (i.e. an
    # ACIE install predating cross-file call resolution) must not hard-fail
    # on next open, and must correctly report "not yet done" rather than
    # raising or silently defaulting to done.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            generation INTEGER NOT NULL,
            head_sha TEXT
        );
        """
    )
    conn.execute("INSERT INTO index_meta (id, generation, head_sha) VALUES (0, 5, 'abc123')")
    conn.commit()

    store = IndexMetaStore(conn=conn)  # must not raise OperationalError

    assert store.cross_file_pass_done() is False


def test_a_repo_migrated_only_under_the_old_calls_only_boolean_flag_still_reports_not_done():
    # Codex review finding (P1), slice A2: the old boolean column can only
    # mean "the calls-only catch-up ran" -- it cannot distinguish that from
    # "calls+inherits both ran" (CURRENT_CROSS_FILE_PASS_VERSION). A repo
    # already at the legacy done=1 state must still report False here so
    # BootstrapCoordinator's catch-up pass runs once more and closes the new
    # inherits gap, exactly like a repo that never ran any pass at all.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            generation INTEGER NOT NULL,
            head_sha TEXT,
            cross_file_pass_done INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO index_meta (id, generation, head_sha, cross_file_pass_done) VALUES (0, 5, 'abc123', 1)")
    conn.commit()

    store = IndexMetaStore(conn=conn)

    assert store.cross_file_pass_done() is False


def test_marking_cross_file_pass_done_after_the_legacy_upgrade_makes_it_report_done():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            generation INTEGER NOT NULL,
            head_sha TEXT,
            cross_file_pass_done INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO index_meta (id, generation, head_sha, cross_file_pass_done) VALUES (0, 5, 'abc123', 1)")
    conn.commit()
    store = IndexMetaStore(conn=conn)
    assert store.cross_file_pass_done() is False

    store.mark_cross_file_pass_done()

    assert store.cross_file_pass_done() is True
    assert store.current_generation() == 5
    assert store.get_last_indexed_head_sha() == "abc123"


def test_opening_a_pre_existing_index_meta_table_without_head_sha_migrates_it_in_place():
    # Regression (codex review, 2026-09-02): CREATE TABLE IF NOT EXISTS is a
    # no-op against a real pre-existing index.sqlite from before head_sha
    # was added -- every such repo's daemon would hard-fail on next open.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            generation INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO index_meta (id, generation) VALUES (0, 5)")
    conn.commit()

    store = IndexMetaStore(conn=conn)  # must not raise OperationalError

    assert store.current_generation() == 5
    assert store.get_last_indexed_head_sha() is None
    store.set_last_indexed_head_sha("abc123")
    assert store.get_last_indexed_head_sha() == "abc123"


def test_last_enrichment_fingerprint_is_none_for_a_fresh_store():
    store = IndexMetaStore(":memory:")

    assert store.get_last_enrichment_fingerprint() is None


def test_set_then_get_last_enrichment_fingerprint_round_trips():
    store = IndexMetaStore(":memory:")

    store.set_last_enrichment_fingerprint("fingerprint-1")

    assert store.get_last_enrichment_fingerprint() == "fingerprint-1"


def test_last_enrichment_fingerprint_persists_across_store_instances_on_the_same_db_path(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    IndexMetaStore(db_path).set_last_enrichment_fingerprint("fingerprint-1")

    reopened = IndexMetaStore(db_path)

    assert reopened.get_last_enrichment_fingerprint() == "fingerprint-1"


def test_opening_a_pre_existing_index_meta_table_without_enrichment_fingerprint_migrates_it_in_place():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE index_meta (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            generation INTEGER NOT NULL,
            head_sha TEXT,
            cross_file_pass_version INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO index_meta (id, generation, head_sha, cross_file_pass_version)
        VALUES (0, 5, 'abc123', 3);
        """
    )
    conn.commit()

    store = IndexMetaStore(conn=conn)

    assert store.get_last_enrichment_fingerprint() is None
    store.set_last_enrichment_fingerprint("fingerprint-1")
    assert store.get_last_enrichment_fingerprint() == "fingerprint-1"
