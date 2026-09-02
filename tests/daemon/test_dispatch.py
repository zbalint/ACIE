import subprocess

from acie.daemon.dispatch import DISPATCH_TABLE, _read_source_files, dispatch_request
from acie.daemon.protocol import build_request
from acie.indexer import index_file
from acie.repo_id import resolve_index_db_path
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.pagination import encode_cursor

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


def test_dispatch_table_has_exactly_the_9_locked_tool_names():
    assert set(DISPATCH_TABLE) == {
        "find_symbol", "get_definition", "find_references", "list_imports",
        "structural_search", "graph", "impact_analysis", "explain", "affected_tests",
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

    # get_definition's exactly-one-selector check now raises a typed
    # InvalidArgumentError (see test_dispatch_request_maps_missing_selector_
    # to_invalid_argument below), so it no longer exercises this fallback.
    # An unexpected extra param does: _call_tool forwards params straight
    # through as kwargs, so a bogus one raises a plain TypeError from
    # inside find_symbol()'s own call, not an AcieToolError subclass.
    request = build_request("find_symbol", str(repo), {"name": "foo", "bogus_param": 1})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INTERNAL_ERROR"


# Regression tests for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): these
# reproduce the exact live-observed bugs end-to-end through dispatch_request,
# confirming the fix at the level codex actually probed (a real MCP request),
# not just the underlying tool function.
def test_dispatch_request_maps_malformed_cursor_to_invalid_cursor(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("find_symbol", str(repo), {"name": "foo", "cursor": "not-valid-base64!!!"})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_CURSOR"


def test_dispatch_request_maps_zero_limit_to_invalid_limit(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("find_symbol", str(repo), {"name": "foo", "limit": 0})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_LIMIT"


def test_dispatch_request_maps_missing_selector_to_invalid_argument(tmp_path):
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("get_definition", str(repo), {})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_dispatch_request_maps_a_semantically_wrong_cursor_last_id_to_invalid_cursor(tmp_path):
    # Code-review regression (2026-09-02): the exact repro from the review --
    # a syntactically valid [1, 0] cursor (int last_id where find_symbol
    # expects a string symbol id) used to crash with a bare
    # "'>' not supported between instances of 'str' and 'int'" TypeError,
    # which this dispatcher's generic exception handler then mapped to
    # INTERNAL_ERROR instead of INVALID_CURSOR.
    repo = _git_repo(tmp_path)
    base_dir = tmp_path / "acie-home"
    _index_one_file(repo, base_dir)

    request = build_request("find_symbol", str(repo), {"name": "foo", "cursor": encode_cursor(1, 0)})

    response = dispatch_request(request, repo_ready=_ALL_ALWAYS_READY, base_dir=str(base_dir))

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_CURSOR"


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


def test_read_source_files_with_no_is_ignored_reads_everything_as_before(tmp_path):
    repo_root = tmp_path
    (repo_root / "foo.py").write_text("x = 1\n")

    files = _read_source_files(str(repo_root), path_glob=None)

    assert files == {"foo.py": "x = 1\n"}


def test_read_source_files_skips_files_the_predicate_says_are_ignored(tmp_path):
    repo_root = tmp_path
    (repo_root / "foo.py").write_text("x = 1\n")
    (repo_root / "generated.py").write_text("y = 2\n")

    files = _read_source_files(
        str(repo_root), path_glob=None, is_ignored=lambda rel_path: rel_path == "generated.py"
    )

    assert files == {"foo.py": "x = 1\n"}


def test_read_source_files_prunes_ignored_directories_instead_of_just_filtering_their_files(tmp_path):
    repo_root = tmp_path
    (repo_root / "foo.py").write_text("x = 1\n")
    build_dir = repo_root / "build"
    build_dir.mkdir()
    (build_dir / "output.py").write_text("z = 3\n")
    visited_dirs: list[str] = []

    def is_ignored(rel_path: str) -> bool:
        visited_dirs.append(rel_path)
        return rel_path == "build"

    files = _read_source_files(str(repo_root), path_glob=None, is_ignored=is_ignored)

    assert files == {"foo.py": "x = 1\n"}
    # build/output.py's rel_path is never even checked -- the whole
    # directory was pruned before descending into it.
    assert "build/output.py" not in visited_dirs
