import subprocess

from acie.daemon.dispatch import DISPATCH_TABLE, dispatch_request
from acie.daemon.protocol import build_request
from acie.indexer import index_file
from acie.repo_id import resolve_index_db_path
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore

_OBSERVED_AT = "2026-09-01T00:00:00Z"

_ALL_ALWAYS_READY = lambda repo_path: True  # noqa: E731
_NEVER_READY = lambda repo_path: False  # noqa: E731


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _index_one_file(repo_path, base_dir, path="pkg/mod.py", source_text="def foo():\n    pass\n"):
    db_path = resolve_index_db_path(str(repo_path), base_dir=str(base_dir))
    symbol_store = SymbolStore(db_path)
    relation_store = RelationStore(db_path)
    index_meta_store = IndexMetaStore(db_path)
    index_file(
        path=path, source_text=source_text, observed_at=_OBSERVED_AT,
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
    )
    return db_path


def test_dispatch_table_has_exactly_the_8_locked_tool_names():
    assert set(DISPATCH_TABLE) == {
        "find_symbol", "get_definition", "find_references", "list_imports",
        "structural_search", "graph", "impact_analysis", "explain",
    }
    assert all(callable(fn) for fn in DISPATCH_TABLE.values())


def test_dispatch_request_returns_unknown_method_for_an_unregistered_method(tmp_path):
    request = build_request("not_a_real_tool", str(tmp_path), {})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY)

    assert response["id"] == request["id"]
    assert response["ok"] is False
    assert response["error"]["code"] == "UNKNOWN_METHOD"


def test_dispatch_request_returns_malformed_request_when_method_missing(tmp_path):
    request = build_request("find_symbol", str(tmp_path), {})
    del request["method"]

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY)

    assert response["ok"] is False
    assert response["error"]["code"] == "MALFORMED_REQUEST"


def test_dispatch_request_returns_malformed_request_when_repo_path_missing():
    request = build_request("find_symbol", "/irrelevant", {})
    del request["repo_path"]

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY)

    assert response["ok"] is False
    assert response["error"]["code"] == "MALFORMED_REQUEST"


def test_dispatch_request_returns_malformed_request_when_params_is_not_a_dict(tmp_path):
    request = build_request("find_symbol", str(tmp_path), {})
    request["params"] = "not-a-dict"

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY)

    assert response["ok"] is False
    assert response["error"]["code"] == "MALFORMED_REQUEST"


def test_dispatch_request_short_circuits_index_not_ready_before_touching_disk(tmp_path):
    # repo_path is not even a real git repo -- proves the readiness check
    # runs before any repo resolution/store construction is attempted.
    request = build_request("find_symbol", str(tmp_path / "does-not-exist"), {"name": "foo"})

    response = dispatch_request(request, repo_ready=_NEVER_READY)

    assert response["ok"] is False
    assert response["error"]["code"] == "INDEX_NOT_READY"
    assert response["error"]["index_generation"] == 0


def test_dispatch_request_returns_malformed_request_when_repo_path_is_not_a_git_repo(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    request = build_request("find_symbol", str(not_a_repo), {"name": "foo"})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY)

    assert response["ok"] is False
    assert response["error"]["code"] == "MALFORMED_REQUEST"


def test_dispatch_request_calls_find_symbol_end_to_end_against_real_stores(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("find_symbol", str(repo), {"name": "foo"})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["id"] == request["id"]
    assert response["ok"] is True
    assert response["result"]["total_count"] == 1
    assert response["result"]["results"][0]["qualname"] == "foo"


def test_dispatch_request_calls_list_imports_which_needs_no_symbol_store(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(
        repo, base_dir, path="pkg/mod.py",
        source_text="import os\n\n\ndef foo():\n    pass\n",
    )

    request = build_request("list_imports", str(repo), {"file": "pkg/mod.py"})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is True
    assert response["result"]["total_count"] == 1


def test_dispatch_request_maps_acie_tool_error_to_its_own_code(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("impact_analysis", str(repo), {"root": "does:not:exist"})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "SYMBOL_NOT_FOUND"


def test_dispatch_request_maps_any_other_exception_to_internal_error(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    # get_definition requires exactly one of symbol_id/position -- giving
    # neither raises a plain ValueError from inside the tool, not an
    # AcieToolError subclass.
    request = build_request("get_definition", str(repo), {})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INTERNAL_ERROR"


def test_dispatch_request_fills_structural_search_files_from_real_disk(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)  # gives the repo an index.sqlite for index_generation
    pkg_dir = repo / "pkg"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "mod.py").write_text("def foo():\n    pass\n")
    (pkg_dir / "other.py").write_text("def bar():\n    pass\n")

    request = build_request(
        "structural_search", str(repo),
        {"pattern": "(function_definition name: (identifier) @func.name)"},
    )

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is True
    names = {r["captures"]["func.name"][0]["text"] for r in response["result"]["results"]}
    assert names == {"foo", "bar"}


def test_dispatch_request_honors_structural_search_path_glob_by_only_reading_matching_files(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)
    pkg_dir = repo / "pkg"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "mod.py").write_text("def foo():\n    pass\n")
    (pkg_dir / "other.py").write_text("def bar():\n    pass\n")

    request = build_request(
        "structural_search", str(repo),
        {"pattern": "(function_definition name: (identifier) @func.name)", "path_glob": "pkg/other.py"},
    )

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is True
    assert response["result"]["total_count"] == 1
    assert response["result"]["results"][0]["captures"]["func.name"][0]["text"] == "bar"
