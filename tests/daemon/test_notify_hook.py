import json
import subprocess

from acie.daemon.notify_hook import handle_notify_hook
from acie.daemon.write_queue import WriteQueue
from acie.repo_id import resolve_repo_id, resolve_repo_root
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.symbol_store import SymbolStore


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    return repo


def _commit(repo, message="commit"):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def _rev_parse_head(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _run(repo_path, agent, payload, *, db_path):
    # decision 10's fix (SALTMDB f4bdfc9d, grilled 2026-09-02): the caller
    # (runtime.py's dispatch()) now resolves repo_id/repo_root once and
    # passes them in directly -- handle_notify_hook no longer registers or
    # resolves repo_root itself, so this test helper does what the real
    # caller does, using the real resolvers against the real tmp git repo.
    write_queue = WriteQueue(db_path_for=lambda repo_id: db_path)
    handle_notify_hook(
        agent=agent,
        repo_id=resolve_repo_id(repo_path),
        repo_root=resolve_repo_root(repo_path),
        payload=payload,
        write_queue=write_queue,
        db_path_for=lambda repo_id: db_path,
    )
    write_queue.close(timeout=2)


# -- agent == "git": decision 11's head_sha-based diff, not hook-supplied args --


def test_git_agent_with_no_prior_head_sha_just_records_current_head_and_reindexes_nothing(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    _commit(repo)
    db_path = str(tmp_path / "index.sqlite")

    _run(str(repo), "git", "", db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert IndexMetaStore(conn=conn).get_last_indexed_head_sha() == _rev_parse_head(repo)
    assert SymbolStore(conn=conn).list_by_path("mod.py") == []


def test_git_agent_diffs_since_last_recorded_head_and_reindexes_changed_files(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    _commit(repo, "first")
    db_path = str(tmp_path / "index.sqlite")
    _run(str(repo), "git", "", db_path=db_path)  # records the starting HEAD

    (repo / "mod.py").write_text("def bar():\n    pass\n")
    _commit(repo, "second")

    _run(str(repo), "git", "", db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "bar"]
    assert IndexMetaStore(conn=conn).get_last_indexed_head_sha() == _rev_parse_head(repo)


def test_git_agent_is_a_no_op_when_head_has_not_moved(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    _commit(repo)
    db_path = str(tmp_path / "index.sqlite")
    _run(str(repo), "git", "", db_path=db_path)
    conn = __import__("sqlite3").connect(db_path)
    generation_before = IndexMetaStore(conn=conn).current_generation()

    _run(str(repo), "git", "", db_path=db_path)  # same HEAD, called again

    assert IndexMetaStore(conn=conn).current_generation() == generation_before


def test_git_agent_ignores_a_deleted_file_by_tombstoning_it(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    _commit(repo, "first")
    db_path = str(tmp_path / "index.sqlite")
    _run(str(repo), "git", "", db_path=db_path)

    (repo / "mod.py").unlink()
    _commit(repo, "delete mod.py")

    _run(str(repo), "git", "", db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert SymbolStore(conn=conn).list_by_path("mod.py") == []


# -- agent == "claude-code": clean file_path in tool_input --


def test_claude_code_agent_reindexes_the_file_named_in_tool_input(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    db_path = str(tmp_path / "index.sqlite")
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "mod.py")},
    })

    _run(str(repo), "claude-code", payload, db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "foo"]


def test_claude_code_agent_with_malformed_payload_is_a_no_op(tmp_path):
    repo = _git_repo(tmp_path)
    db_path = str(tmp_path / "index.sqlite")

    _run(str(repo), "claude-code", "not json", db_path=db_path)  # must not raise

    conn = __import__("sqlite3").connect(db_path)
    assert IndexMetaStore(conn=conn).current_generation() == 0


def test_claude_code_agent_ignores_a_file_outside_the_repo(tmp_path):
    repo = _git_repo(tmp_path)
    db_path = str(tmp_path / "index.sqlite")
    payload = json.dumps({"tool_input": {"file_path": "/somewhere/else/mod.py"}})

    _run(str(repo), "claude-code", payload, db_path=db_path)  # must not raise

    conn = __import__("sqlite3").connect(db_path)
    assert IndexMetaStore(conn=conn).current_generation() == 0


# -- agent == "codex": diff-header format in tool_input.command --


def test_codex_agent_parses_file_paths_out_of_a_unified_diff_command(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "mod.py").write_text("def foo():\n    pass\n")
    db_path = str(tmp_path / "index.sqlite")
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def foo():\n     pass\n",
        },
    })

    _run(str(repo), "codex", payload, db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "foo"]


def test_codex_agent_rejects_a_path_traversal_diff_header(tmp_path):
    # Regression (codex review, 2026-09-02): diff-header paths were
    # accepted without containment validation -- a payload naming
    # "../outside.py" would index/tombstone a file outside the repo.
    outside = tmp_path / "outside.py"
    outside.write_text("def should_not_be_touched():\n    pass\n")
    repo = _git_repo(tmp_path)
    db_path = str(tmp_path / "index.sqlite")
    payload = json.dumps({
        "tool_input": {"command": "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n"},
    })

    _run(str(repo), "codex", payload, db_path=db_path)

    conn = __import__("sqlite3").connect(db_path)
    assert SymbolStore(conn=conn).list_by_path("../outside.py") == []
    assert IndexMetaStore(conn=conn).current_generation() == 0


def test_codex_agent_with_no_command_field_is_a_no_op(tmp_path):
    repo = _git_repo(tmp_path)
    db_path = str(tmp_path / "index.sqlite")
    payload = json.dumps({"tool_input": {}})

    _run(str(repo), "codex", payload, db_path=db_path)  # must not raise

    conn = __import__("sqlite3").connect(db_path)
    assert IndexMetaStore(conn=conn).current_generation() == 0


# -- unrecognized agent name --


def test_unrecognized_agent_name_is_a_silent_no_op(tmp_path):
    repo = _git_repo(tmp_path)
    db_path = str(tmp_path / "index.sqlite")

    _run(str(repo), "some-future-agent", "{}", db_path=db_path)  # must not raise

    conn = __import__("sqlite3").connect(db_path)
    assert IndexMetaStore(conn=conn).current_generation() == 0
