from concurrent.futures import Future
from pathlib import Path

import pytest

from acie.daemon import lsp_enrichment
from acie.daemon.write_queue import WriteQueue
from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore


class FakeRegistry:
    def __init__(self, process=object()):
        self.process = process
        self.repo_roots = []

    def ensure_process(self, repo_root):
        self.repo_roots.append(repo_root)
        return self.process


class FakeWriteQueue:
    def __init__(self):
        self.submissions = []

    def submit(self, repo_id, job):
        future = Future()
        self.submissions.append((repo_id, job, future))
        future.set_result(None)
        return future


class FakeClient:
    def __init__(self, definition_results, *, capabilities=None):
        self.definition_results = iter(definition_results)
        self.server_capabilities = capabilities if capabilities is not None else {"definitionProvider": True}
        self.notifications = []
        self.requests = []
        self.closed = False

    def initialize(self, root_path):
        return {"serverInfo": {"name": "basedpyright", "version": "9.9.9"}}

    def send_notification(self, method, params):
        self.notifications.append((method, params))

    def send_request(self, method, params):
        self.requests.append((method, params))
        future = Future()
        result = next(self.definition_results)
        if isinstance(result, BaseException):
            future.set_exception(result)
        else:
            future.set_result(result)
        return future

    def close(self):
        self.closed = True


def _symbol(symbol_id, path, *, start_line=1, start_col=0, end_line=20, end_col=0):
    return Symbol(
        id=symbol_id,
        path=path,
        qualname=symbol_id.split(":", 1)[1].rsplit("#", 1)[0],
        kind="function",
        start_line=start_line,
        start_col=start_col,
        end_line=end_line,
        end_col=end_col,
        confidence=Confidence.EXTRACTED,
        provenance=Provenance("tree-sitter", "1", "2026-09-04T00:00:00Z"),
    )


def _run(monkeypatch, tmp_path, client, symbol_store, relation_store, files):
    monkeypatch.setattr(lsp_enrichment, "LspClient", lambda process: client)
    queue = FakeWriteQueue()
    relations = lsp_enrichment.run_enrichment_pass(
        repo_root=str(tmp_path),
        repo_id="repo-id",
        process_registry=FakeRegistry(),
        write_queue=queue,
        walk_repo=lambda root: files,
        symbol_store=symbol_store,
        relation_store=relation_store,
        observed_at_fn=lambda: "2026-09-04T12:00:00Z",
    )
    return relations, queue


def test_enrichment_resolves_an_ambiguous_site_with_live_server_provenance(monkeypatch, tmp_path):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    target_path = "pkg/target.py"
    target = _symbol("pkg/target.py:target#function", target_path, start_line=4, start_col=0)
    symbols.upsert(target)
    relations.upsert(
        Relation(
            source="pkg/caller.py:#module",
            target="old-target",
            predicate="calls",
            site_file="pkg/caller.py",
            site_line=2,
            site_col=0,
            confidence=Confidence.AMBIGUOUS,
            provenance=Provenance("tree-sitter", "1", "old"),
        )
    )
    location = {"uri": (tmp_path / target_path).as_uri(), "range": {"start": {"line": 3, "character": 4}}}
    client = FakeClient([[location]])

    resolved, queue = _run(monkeypatch, tmp_path, client, symbols, relations, [("pkg/caller.py", "target()\n")])

    assert len(resolved) == 1
    assert resolved[0].target == target.id
    assert resolved[0].confidence == Confidence.INFERRED
    assert resolved[0].provenance == Provenance("basedpyright", "9.9.9", "2026-09-04T12:00:00Z")
    assert len(queue.submissions) == 1
    assert [method for method, _ in client.notifications] == ["textDocument/didOpen"]
    assert client.closed is True


def test_enrichment_resolves_an_unpersisted_deferred_site_and_normalizes_location_link(monkeypatch, tmp_path):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    target_path = "pkg/target.py"
    target = _symbol("pkg/target.py:target#function", target_path, start_line=1, start_col=0)
    symbols.upsert(target)
    location_link = {
        "targetUri": (tmp_path / target_path).as_uri(),
        "targetSelectionRange": {"start": {"line": 0, "character": 4}},
    }
    client = FakeClient([[location_link]])
    files = [("pkg/caller.py", "from external import target\n\ntarget()\n")]

    resolved, queue = _run(monkeypatch, tmp_path, client, symbols, relations, files)

    assert [(relation.predicate, relation.target) for relation in resolved] == [("calls", target.id)]
    assert len(queue.submissions) == 1


def test_enrichment_includes_an_unresolved_attribute_deferred_site(monkeypatch, tmp_path):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    target = _symbol("pkg/scan.py:run_scan#function", "pkg/scan.py")
    # Keep a plain-call candidate at the imported module path so the old
    # unresolved-site check cannot mistake this attribute call for resolved.
    # The attribute-aware check must look for `run_scan` in `external.scan`.
    decoy = _symbol("external.py:scan#function", "external.py")
    symbols.upsert(target)
    symbols.upsert(decoy)
    location = {"uri": (tmp_path / "pkg/scan.py").as_uri(), "range": {"start": {"line": 0, "character": 0}}}
    client = FakeClient([[location]])
    files = [("pkg/caller.py", "from external import scan\n\n\nscan.run_scan(path)\n")]

    resolved, queue = _run(monkeypatch, tmp_path, client, symbols, relations, files)

    assert [(relation.predicate, relation.target) for relation in resolved] == [("calls", target.id)]
    assert resolved[0].site_line == 4
    assert resolved[0].site_col == 5
    assert client.requests[0][1]["position"] == {"line": 3, "character": 5}
    assert len(queue.submissions) == 1


@pytest.mark.parametrize(
    "definition_result, target_exists",
    [(None, True), ([], True), ([{"uri": "file:///one.py", "range": {"start": {"line": 0, "character": 0}}}] * 2, True), ([{"uri": "file:///outside.py", "range": {"start": {"line": 0, "character": 0}}}], True), ([{"uri": "file:///inside.py", "range": {"start": {"line": 0, "character": 0}}}], False)],
)
def test_enrichment_skips_non_unique_external_or_unmapped_definitions(
    monkeypatch, tmp_path, definition_result, target_exists
):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    if target_exists:
        symbols.upsert(_symbol("pkg/target.py:target#function", "pkg/target.py"))
    if definition_result and definition_result[0].get("uri") == "file:///inside.py":
        definition_result[0]["uri"] = (tmp_path / "pkg/missing.py").as_uri()
    client = FakeClient([definition_result])
    relations.upsert(
        Relation("pkg/caller.py:#module", "old", "calls", "pkg/caller.py", 1, 0, Confidence.AMBIGUOUS, Provenance("x", "1", "old"))
    )

    resolved, queue = _run(monkeypatch, tmp_path, client, symbols, relations, [("pkg/caller.py", "target()\n")])

    assert resolved == []
    assert queue.submissions == []


@pytest.mark.parametrize("process, capabilities", [(None, {"definitionProvider": True}), (object(), {})])
def test_enrichment_is_a_no_op_without_a_process_or_definition_capability(monkeypatch, tmp_path, process, capabilities):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    client = FakeClient([], capabilities=capabilities)
    monkeypatch.setattr(lsp_enrichment, "LspClient", lambda current_process: client)
    queue = FakeWriteQueue()

    result = lsp_enrichment.run_enrichment_pass(
        repo_root=str(tmp_path), repo_id="repo-id", process_registry=FakeRegistry(process), write_queue=queue,
        walk_repo=lambda root: [("pkg/caller.py", "target()\n")], symbol_store=symbols, relation_store=relations,
    )

    assert result == []
    assert queue.submissions == []
    assert client.closed is (process is not None)


def test_connection_error_stops_the_pass_after_prior_resolutions(monkeypatch, tmp_path):
    symbols = SymbolStore(":memory:")
    relations = RelationStore(":memory:")
    target = _symbol("pkg/target.py:target#function", "pkg/target.py")
    symbols.upsert(target)
    for line in (1, 2):
        relations.upsert(
            Relation("pkg/caller.py:#module", f"old-{line}", "calls", "pkg/caller.py", line, 0, Confidence.AMBIGUOUS, Provenance("x", "1", "old"))
        )
    location = {"uri": (tmp_path / "pkg/target.py").as_uri(), "range": {"start": {"line": 0, "character": 0}}}
    client = FakeClient([[location], ConnectionError("server exited")])

    resolved, queue = _run(monkeypatch, tmp_path, client, symbols, relations, [("pkg/caller.py", "first()\nsecond()\n")])

    assert [relation.target for relation in resolved] == [target.id]
    assert len(queue.submissions) == 1
    assert len(client.requests) == 2
    assert [method for method, _ in client.notifications] == ["textDocument/didOpen"]


def test_merge_job_runs_policy_against_real_queue_connection_and_preserves_extracted_fact(tmp_path):
    db_path = str(tmp_path / "index.sqlite")
    store = RelationStore(db_path)
    extracted = Relation(
        source="pkg/caller.py:#module",
        target="pkg/target.py:target#function",
        predicate="calls",
        site_file="pkg/caller.py",
        site_line=2,
        site_col=0,
        confidence=Confidence.EXTRACTED,
        provenance=Provenance("tree-sitter", "1", "2026-09-04T00:00:00Z"),
    )
    inferred = Relation(
        source=extracted.source,
        target=extracted.target,
        predicate=extracted.predicate,
        site_file=extracted.site_file,
        site_line=extracted.site_line,
        site_col=extracted.site_col,
        confidence=Confidence.INFERRED,
        provenance=Provenance("basedpyright", "9.9.9", "2026-09-04T12:00:00Z"),
    )
    store.upsert(extracted)
    queue = WriteQueue(db_path_for=lambda repo_id: db_path)
    try:
        outcome = queue.submit("repo-id", lsp_enrichment._make_merge_job(inferred)).result(timeout=1)
    finally:
        queue.close(timeout=1)

    assert outcome.applied is False
    assert outcome.reason == "would_regress_existing_confidence"
    assert RelationStore(db_path).get(
        source=extracted.source,
        target=extracted.target,
        predicate=extracted.predicate,
        site_file=extracted.site_file,
        site_line=extracted.site_line,
        site_col=extracted.site_col,
    ) == extracted
