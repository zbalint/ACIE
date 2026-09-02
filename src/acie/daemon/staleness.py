"""Tier 4 of ARCHITECTURE.md's incremental-indexing precedence: the final
safety-net fallback for whatever tiers 1-3 (watcher/git-hooks/agent-hooks)
might have missed -- correcting a stale answer at query time itself,
rather than waiting for the next reindex trigger to catch up.

Scope (confirmed with the user, AskUserQuestion, 2026-09-02): only the 3
tools whose params name exactly one file cheaply get a pre-query
freshness check -- `list_imports(file=...)` and `get_definition`/
`find_references` when called with `position={file, ...}` (not
`symbol_id`, which names no file at all). `find_symbol`/`graph`/
`impact_analysis`/`explain` don't name a single file up front (or, for
`explain`, staleness is beside the point -- it shows history on purpose).
`structural_search` already reads its `files` mapping live off disk on
every call (dispatch.py's `_read_source_files` seam), so it can never be
stale in the first place.

This module is a pure, easily-unit-tested extraction step only --
runtime.py's dispatch() closure is what actually submits the reindex job
and bounds its wait (see DAEMON.md "Incremental Indexing Wiring").
"""

from acie.repo_id import to_repo_relative

_SOURCE_EXTENSION = ".py"

# Mirrors resolve.py's mutually-exclusive symbol_id/position shape --
# these are the only 2 tools taking a `position` param at all.
_POSITION_METHODS = {"get_definition", "find_references"}


def extract_staleness_target(method: str, params: dict | None, repo_root: str) -> str | None:
    """Returns the one repo-relative .py path this request's params name,
    or None if this method/params combination names none (nothing to
    freshen, or the tool is out of tier 4's scope -- see module docstring).

    `params["file"]`/`params["position"]["file"]` are caller-supplied,
    untrusted strings -- every candidate is routed through
    repo_id.to_repo_relative before being returned, the same containment
    check notify_hook.py's agent-hook payload parsing already applies to
    its own untrusted paths (codex review, 2026-09-02, on this module: an
    unvalidated "../outside.py" or absolute path would otherwise reach
    ensure_fresh -> make_reindex_job, which joins it onto repo_root with
    no containment check of its own, letting a query read/index an
    arbitrary .py file on disk).
    """
    if not isinstance(params, dict):
        return None

    if method == "list_imports":
        candidate = params.get("file")
    elif method in _POSITION_METHODS:
        position = params.get("position")
        candidate = position.get("file") if isinstance(position, dict) else None
    else:
        return None

    if not isinstance(candidate, str) or not candidate:
        return None
    if not candidate.endswith(_SOURCE_EXTENSION):
        return None
    return to_repo_relative(candidate, repo_root)
