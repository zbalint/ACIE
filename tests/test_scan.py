import pytest
import subprocess
from acie.daemon.bootstrap import make_index_job
from acie.indexer import IndexResult
from acie.storage.connection import open_connection
from acie.repo_id import resolve_index_db_path
from acie.storage.relation_store import RelationStore
from acie.daemon import lsp_enrichment
from tests.daemon.test_lsp_enrichment import FakeClient, FakeRegistry
from acie.storage.index_meta_store import IndexMetaStore
from acie.daemon.write_queue import WriteQueue

from acie import scan


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_make_index_job_returns_index_result_from_indexing(tmp_path):
    db_path = tmp_path / "index.sqlite"
    conn = open_connection(str(db_path))
    try:
        result = make_index_job("pkg/mod.py", "def helper():\n    pass\n")(conn)
    finally:
        conn.close()

    assert result == IndexResult(
        skipped=False,
        symbols_upserted=2,
        symbols_tombstoned=0,
        relations_upserted=1,
        relations_tombstoned=0,
    )


def test_run_scan_rejects_a_path_outside_a_git_repository_before_setup(monkeypatch, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.setattr(scan, "WriteQueue", lambda **kwargs: pytest.fail("queue was constructed"))
    monkeypatch.setattr(scan, "PyrightProcessRegistry", lambda: pytest.fail("registry was constructed"))

    with pytest.raises(scan.ScanError, match=f"{str(not_a_repo)!r} is not inside a git repository"):
        scan.run_scan(str(not_a_repo), base_dir=str(tmp_path / "state"))


def test_run_scan_indexes_cross_file_calls_with_two_passes(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    package = repo / "pkg"
    package.mkdir()
    (package / "mod.py").write_text("from pkg.other import helper\n\n\ndef caller():\n    helper()\n")
    (package / "other.py").write_text("def helper():\n    pass\n")
    monkeypatch.setattr(scan, "run_enrichment_pass", lambda **kwargs: [])

    result = scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))

    assert result.files_scanned == 2
    assert result.files_failed == 0
    assert result.symbols_upserted > 0
    assert result.relations_upserted > 0
    db_path = resolve_index_db_path(str(repo), base_dir=str(tmp_path / "state"))
    calls = RelationStore(db_path).list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 1
    assert calls[0].target == "pkg/other.py:helper#function"


def test_run_scan_rewalks_an_existing_index_and_persists_migration_flag(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "module.py").write_text("def target():\n    pass\n")
    walk_calls = []

    def counting_walk(root):
        walk_calls.append(root)
        return [("module.py", "def target():\n    pass\n")]

    monkeypatch.setattr(scan.dispatch, "walk_repo", counting_walk)
    monkeypatch.setattr(scan, "run_enrichment_pass", lambda **kwargs: [])
    state_dir = str(tmp_path / "state")

    first = scan.run_scan(str(repo), base_dir=state_dir)
    second = scan.run_scan(str(repo), base_dir=state_dir)

    assert walk_calls == [str(repo), str(repo)]
    assert second.files_scanned == first.files_scanned == 1
    assert second.symbols_upserted == first.symbols_upserted
    assert second.relations_upserted == first.relations_upserted
    db_path = resolve_index_db_path(str(repo), base_dir=state_dir)
    assert IndexMetaStore(db_path).cross_file_pass_done() is True


def test_run_scan_counts_and_logs_a_failed_file_without_aborting_other_files(
    monkeypatch, tmp_path, caplog
):
    repo = _git_repo(tmp_path)
    (repo / "good.py").write_text("def good():\n    pass\n")
    (repo / "broken.py").write_text("def broken():\n    pass\n")
    original_make_index_job = scan.make_index_job

    def make_job(path, source_text):
        if path == "broken.py":
            def fail(conn):
                raise RuntimeError("indexing failed")

            return fail
        return original_make_index_job(path, source_text)

    monkeypatch.setattr(scan, "make_index_job", make_job)
    monkeypatch.setattr(scan, "run_enrichment_pass", lambda **kwargs: [])

    result = scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))

    assert result.files_scanned == 2
    assert result.files_failed == 1
    assert result.symbols_upserted > 0
    assert "broken.py" in caplog.text


def test_run_scan_runs_enrichment_and_reports_resolved_relations(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "vendor_a").mkdir()
    (repo / "vendor_a" / "pkg").mkdir()
    (repo / "vendor_b").mkdir()
    (repo / "vendor_b" / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("from pkg.other import helper\n\n\nhelper()\n")
    (repo / "vendor_a" / "pkg" / "other.py").write_text("def helper():\n    pass\n")
    (repo / "vendor_b" / "pkg" / "other.py").write_text("def helper():\n    pass\n")

    registry = FakeRegistry()
    registry.close = lambda timeout: None
    target = "vendor_a/pkg/other.py:helper#function"
    location = {
        "uri": (repo / "vendor_a" / "pkg" / "other.py").as_uri(),
        "range": {"start": {"line": 0, "character": 4}},
    }
    client = FakeClient([[location]])
    monkeypatch.setattr(scan, "PyrightProcessRegistry", lambda: registry)
    monkeypatch.setattr(lsp_enrichment, "LspClient", lambda process: client)

    result = scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))

    assert result.relations_enriched == 1
    assert client.closed is True
    db_path = resolve_index_db_path(str(repo), base_dir=str(tmp_path / "state"))
    calls = RelationStore(db_path).list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert {relation.target for relation in calls} == {target}


def test_run_scan_succeeds_without_pyright_enrichment(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "module.py").write_text("def target():\n    pass\n")
    registry = FakeRegistry(process=None)
    registry.close = lambda timeout: None
    monkeypatch.setattr(scan, "PyrightProcessRegistry", lambda: registry)

    result = scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))

    assert result.relations_enriched == 0
    assert result.files_failed == 0


def test_run_scan_closes_the_queue_when_registry_cleanup_raises(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    events = []
    queues = []

    class TrackingQueue(WriteQueue):
        def __init__(self, db_path_for):
            super().__init__(db_path_for)
            self.closed = False
            queues.append(self)

        def close(self, timeout=None):
            events.append("queue")
            self.closed = True
            super().close(timeout)

    class RaisingRegistry:
        def close(self, timeout=None):
            events.append("registry")
            raise RuntimeError("registry cleanup failed")

    monkeypatch.setattr(scan, "WriteQueue", TrackingQueue)
    monkeypatch.setattr(scan, "PyrightProcessRegistry", RaisingRegistry)
    monkeypatch.setattr(scan, "run_enrichment_pass", lambda **kwargs: [])

    try:
        with pytest.raises(RuntimeError, match="registry cleanup failed"):
            scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))
    finally:
        failure_events = list(events)
        if queues and not queues[0].closed:
            queues[0].close(timeout=1.0)

    assert failure_events == ["registry", "queue"]


def test_run_scan_elapsed_time_includes_cleanup(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    events = []

    class FakeClock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = FakeClock()

    class TrackingQueue(WriteQueue):
        def close(self, timeout=None):
            events.append("queue")
            clock.now += 2.0
            super().close(timeout)

    class TrackingRegistry:
        def close(self, timeout=None):
            events.append("registry")
            clock.now += 1.0

    monkeypatch.setattr(scan, "time", clock)
    monkeypatch.setattr(scan, "WriteQueue", TrackingQueue)
    monkeypatch.setattr(scan, "PyrightProcessRegistry", TrackingRegistry)
    monkeypatch.setattr(scan, "run_enrichment_pass", lambda **kwargs: [])

    result = scan.run_scan(str(repo), base_dir=str(tmp_path / "state"))

    assert events == ["registry", "queue"]
    assert result.elapsed_seconds == 3.0
