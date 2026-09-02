"""Tier 2 (git hooks) and tier 3 (agent tool-use hooks) of
ARCHITECTURE.md's incremental-indexing precedence -- both funnel through
the exact same `acie notify-hook --agent <name>` CLI entrypoint and, on
the daemon side, this one handler (watcher/incremental-indexing grilling
decision 4: one shared command/RPC, not a separate one per tier).

See the grilling session (SALTMDB decision f4bdfc9d) for the full
rationale behind:
- decision 11: `agent == "git"` never trusts hook-supplied SHAs (the four
  git hook types don't uniformly provide them) -- it tracks its own
  `last_indexed_head_sha` (index_meta_store.py) and diffs that against the
  repo's current HEAD itself.
- decision 13: every changed/deleted path this module discovers is
  submitted through the exact same per-path job watcher.py's tier 1 uses
  (make_reindex_job) -- same mtime/hash gate, same delete/rename handling,
  no separate reindex logic duplicated here.

decision 12 (register() runs unconditionally first) was superseded by
decision 10's follow-up fix (SALTMDB f4bdfc9d, grilled to a locked plan
2026-09-02): runtime.py's dispatch() already resolves repo_path to the
canonical (repo_id, repo_root) pair and calls register_repo() with it
unconditionally, before ever routing to this module -- so this module no
longer registers or resolves repo_root itself; it trusts the caller and
receives repo_id/repo_root directly, always already valid.
"""

import json
import os
import sqlite3
import subprocess
from typing import Callable

from acie.daemon import ignore
from acie.daemon.watcher import make_reindex_job
from acie.daemon.write_queue import WriteQueue
from acie.repo_id import to_repo_relative
from acie.storage.index_meta_store import IndexMetaStore

_SOURCE_EXTENSION = ".py"


def handle_notify_hook(
    *,
    agent: str,
    repo_id: str,
    repo_root: str,
    payload: str,
    write_queue: WriteQueue,
    db_path_for: Callable[[str], str],
) -> None:
    if agent == "git":
        _handle_git(repo_id=repo_id, repo_root=repo_root, write_queue=write_queue, db_path_for=db_path_for)
    elif agent == "claude-code":
        rel_path = _parse_claude_code_payload(payload, repo_root)
        _submit_if_in_scope(repo_id, repo_root, write_queue, rel_path)
    elif agent == "codex":
        for rel_path in _parse_codex_payload(payload, repo_root):
            _submit_if_in_scope(repo_id, repo_root, write_queue, rel_path)
    # An unrecognized agent name is a silent no-op, not an error --
    # ARCHITECTURE.md's whole notify-hook contract is "never break or
    # delay the caller", so a future/unknown agent name must not raise.


def _submit_if_in_scope(
    repo_id: str, repo_root: str, write_queue: WriteQueue, rel_path: str | None
) -> None:
    if rel_path is None:
        return
    if not rel_path.endswith(_SOURCE_EXTENSION):
        return
    if ignore.get_ignore_matcher(repo_root).matches(rel_path):
        return
    write_queue.submit(repo_id, make_reindex_job(repo_root, rel_path))


def _handle_git(
    *, repo_id: str, repo_root: str, write_queue: WriteQueue, db_path_for: Callable[[str], str]
) -> None:
    conn = sqlite3.connect(db_path_for(repo_id))
    try:
        last_sha = IndexMetaStore(conn=conn).get_last_indexed_head_sha()
    finally:
        conn.close()

    current_sha = _git_rev_parse_head(repo_root)
    if current_sha is None:
        return
    if last_sha is None:
        # Fresh repo, nothing recorded yet -- the caller's unconditional
        # register_repo() already covers a from-scratch index via
        # bootstrap; just record today's HEAD so the *next* git-hook call
        # has something to diff from.
        _set_head_sha(repo_id, write_queue, current_sha)
        return
    if current_sha == last_sha:
        return

    for rel_path in _git_diff_name_only(repo_root, last_sha, current_sha):
        _submit_if_in_scope(repo_id, repo_root, write_queue, rel_path)
    _set_head_sha(repo_id, write_queue, current_sha)


def _set_head_sha(repo_id: str, write_queue: WriteQueue, sha: str) -> None:
    def job(conn: sqlite3.Connection) -> None:
        IndexMetaStore(conn=conn).set_last_indexed_head_sha(sha)

    # Submitted after every per-file job above -- WriteQueue is one FIFO
    # per repo, so this always runs last for this notify-hook call.
    write_queue.submit(repo_id, job)


def _git_rev_parse_head(repo_root: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_diff_name_only(repo_root: str, old_sha: str, new_sha: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo_root, "diff", "--name-only", old_sha, new_sha],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _parse_claude_code_payload(payload: str, repo_root: str) -> str | None:
    """Claude Code's PostToolUse hook hands over a clean absolute
    tool_input.file_path -- see ARCHITECTURE.md "Agent hook survey
    findings".
    """
    data = _load_json_object(payload)
    if data is None:
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    return to_repo_relative(file_path, repo_root)


def _parse_codex_payload(payload: str, repo_root: str) -> list[str]:
    """Codex's PostToolUse hook shares Claude Code's payload shape but,
    for apply_patch/Bash tool calls, tool_input.command carries a unified-
    diff body instead of a clean file_path -- ARCHITECTURE.md's "delivers
    a diff-header format that needs parsing" (verified against
    developers.openai.com/codex/hooks, 2026-09-02: tool_input.command
    for apply_patch is a "--- a/<path>\\n+++ b/<path>"-style patch).
    """
    data = _load_json_object(payload)
    if data is None:
        return []
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    return _extract_paths_from_diff_header(command, repo_root)


def _extract_paths_from_diff_header(diff_text: str, repo_root: str) -> list[str]:
    """Untrusted input -- diff_text is agent-hook payload content, not
    daemon-generated. Every extracted path is run through the same
    containment check to_repo_relative already applies to Claude Code's
    file_path (codex review, 2026-09-02: an unvalidated "../outside.py"
    header let a crafted payload index/tombstone a file outside the repo).
    """
    paths: list[str] = []
    for line in diff_text.splitlines():
        for prefix in ("+++ b/", "--- a/"):
            if line.startswith(prefix):
                raw_path = line[len(prefix):].strip()
                if raw_path == "/dev/null":
                    continue  # a pure add ("--- a/") or pure delete ("+++ b/") side.
                rel_path = to_repo_relative(raw_path, repo_root)
                if rel_path is not None and rel_path not in paths:
                    paths.append(rel_path)
    return paths


def _load_json_object(payload: str) -> dict | None:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
