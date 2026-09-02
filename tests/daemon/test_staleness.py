from acie.daemon.staleness import extract_staleness_target

_REPO_ROOT = "/repo"


def test_extract_staleness_target_from_list_imports_file_param():
    assert extract_staleness_target("list_imports", {"file": "pkg/mod.py"}, _REPO_ROOT) == "pkg/mod.py"


def test_extract_staleness_target_from_get_definition_position():
    params = {"position": {"file": "pkg/mod.py", "line": 1, "column": 0}}
    assert extract_staleness_target("get_definition", params, _REPO_ROOT) == "pkg/mod.py"


def test_extract_staleness_target_from_find_references_position():
    params = {"position": {"file": "pkg/mod.py", "line": 1, "column": 0}}
    assert extract_staleness_target("find_references", params, _REPO_ROOT) == "pkg/mod.py"


def test_extract_staleness_target_is_none_for_symbol_id_mode():
    params = {"symbol_id": "pkg/mod.py:foo#function"}
    assert extract_staleness_target("get_definition", params, _REPO_ROOT) is None
    assert extract_staleness_target("find_references", params, _REPO_ROOT) is None


def test_extract_staleness_target_is_none_for_unrelated_methods():
    assert extract_staleness_target("find_symbol", {"name": "foo"}, _REPO_ROOT) is None
    assert extract_staleness_target("graph", {"root": "pkg/mod.py:foo#function"}, _REPO_ROOT) is None
    assert extract_staleness_target("impact_analysis", {"root": "pkg/mod.py:foo#function"}, _REPO_ROOT) is None
    assert extract_staleness_target("structural_search", {"pattern": "(function_definition)"}, _REPO_ROOT) is None
    assert extract_staleness_target("explain", {"target": "pkg/mod.py:foo#function"}, _REPO_ROOT) is None


def test_extract_staleness_target_is_none_for_missing_or_non_string_file():
    assert extract_staleness_target("list_imports", {}, _REPO_ROOT) is None
    assert extract_staleness_target("list_imports", {"file": 123}, _REPO_ROOT) is None
    assert extract_staleness_target("get_definition", {"position": "not-a-dict"}, _REPO_ROOT) is None
    assert extract_staleness_target("get_definition", {"position": {}}, _REPO_ROOT) is None


def test_extract_staleness_target_is_none_for_empty_or_non_python_file():
    assert extract_staleness_target("list_imports", {"file": ""}, _REPO_ROOT) is None
    assert extract_staleness_target("list_imports", {"file": "README.md"}, _REPO_ROOT) is None


def test_extract_staleness_target_is_none_when_params_is_not_a_dict():
    assert extract_staleness_target("list_imports", None, _REPO_ROOT) is None


# --- P1 regression: path traversal / absolute-path escape (codex review, 2026-09-02) ---
# extract_staleness_target used to accept "../outside.py" and an absolute
# .py path verbatim, which ensure_fresh then handed straight to
# make_reindex_job -- os.path.join(repo_root, rel_path) ignores repo_root
# entirely when rel_path is absolute, and resolves outside it for a ".."
# escape, so either shape let a query make the daemon read/index an
# arbitrary .py file on disk. Fixed by routing every candidate through
# repo_id.to_repo_relative, the same containment check notify_hook.py's
# agent-hook payload parsing already applies to its own untrusted paths.


def test_extract_staleness_target_rejects_parent_directory_traversal_in_list_imports_file():
    assert extract_staleness_target("list_imports", {"file": "../outside.py"}, _REPO_ROOT) is None
    assert extract_staleness_target("list_imports", {"file": "pkg/../../outside.py"}, _REPO_ROOT) is None


def test_extract_staleness_target_rejects_an_absolute_path_outside_repo_root_in_list_imports_file():
    assert extract_staleness_target("list_imports", {"file": "/etc/cron.d/evil.py"}, _REPO_ROOT) is None


def test_extract_staleness_target_accepts_an_absolute_path_that_resolves_inside_repo_root():
    assert extract_staleness_target("list_imports", {"file": "/repo/pkg/mod.py"}, _REPO_ROOT) == "pkg/mod.py"


def test_extract_staleness_target_rejects_parent_directory_traversal_in_position_file():
    params = {"position": {"file": "../outside.py", "line": 1, "column": 0}}
    assert extract_staleness_target("get_definition", params, _REPO_ROOT) is None
    assert extract_staleness_target("find_references", params, _REPO_ROOT) is None


def test_extract_staleness_target_rejects_an_absolute_position_file_outside_repo_root():
    params = {"position": {"file": "/etc/cron.d/evil.py", "line": 1, "column": 0}}
    assert extract_staleness_target("get_definition", params, _REPO_ROOT) is None
