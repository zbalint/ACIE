"""Production assembly of ACIE's daemon runtime dependencies.

This is the seam where the daemon's existing transport, bootstrap, write
queue, and dispatch modules become one running process. CLI and MCP-server
client concerns intentionally remain outside this module.
"""

import logging
import os
import time

from acie.daemon import ignore
from acie.daemon.bootstrap import BootstrapCoordinator
from acie.daemon.dispatch import _read_source_files, dispatch_request
from acie.daemon.notify_hook import handle_notify_hook
from acie.daemon.protocol import build_error_response, build_success_response
from acie.daemon.server import DaemonServer
from acie.daemon.staleness import extract_staleness_target
from acie.daemon.watcher import WatcherRegistry, make_reindex_job
from acie.daemon.write_queue import WriteQueue
from acie.repo_id import resolve_repo_id, resolve_repo_root

_logger = logging.getLogger(__name__)

_NOTIFY_HOOK_METHOD = "notify_hook"

# Tier 4 (DAEMON.md "Incremental Indexing Wiring"): bounded so a query
# naming a file never hangs behind a backlogged writer thread -- proceed
# with whatever the index currently has rather than delay or fail the
# caller, same "never break the caller" principle notify-hook's own
# 200ms fire-and-forget timeout already uses. Deliberately more generous
# than that budget since this one blocks a real query's answer on the
# outcome (when it finishes in time) rather than firing and forgetting.
_LAZY_STALENESS_TIMEOUT_SECONDS = 2.0

def ensure_fresh(
    write_queue: WriteQueue, repo_id: str, repo_root: str, method: object, params: object,
    *, timeout: float = _LAZY_STALENESS_TIMEOUT_SECONDS,
) -> None:
    """Tier 4: a synchronous, best-effort pre-query reindex of the one file
    this request's params name (see staleness.py's module docstring for
    exact scope). Never raises -- a query naming a stale file still gets
    an answer even if this check itself times out or the reindex job
    fails, per DAEMON.md "Incremental Indexing Wiring"'s "never break the
    caller" contract. Module-level (not a create_daemon() closure) so it's
    directly unit-testable against a real WriteQueue without needing a
    full daemon/socket harness.
    """
    if not isinstance(method, str):
        return
    rel_path = extract_staleness_target(method, params if isinstance(params, dict) else None, repo_root)
    if rel_path is None:
        return
    future = write_queue.submit(repo_id, make_reindex_job(repo_root, rel_path))
    try:
        future.result(timeout=timeout)
    except Exception:  # noqa: BLE001 -- best-effort, see docstring above (covers Future's own TimeoutError too).
        _logger.warning(
            "Tier 4 lazy staleness check for %r in repo %r did not complete within %.1fs "
            "or failed -- proceeding with whatever the index currently has.",
            rel_path, repo_id, timeout,
        )


# Overall budget for on_shutdown()'s whole drain (watchers.close() AND
# write_queue.close() together, sharing one deadline -- codex review,
# 2026-09-02: giving each call its own fresh _SHUTDOWN_DRAIN_TIMEOUT_SECONDS
# would let total shutdown time be 2x this, not this). Generous enough that
# a real bootstrap backlog or a normal Observer teardown always finishes
# well inside it under ordinary conditions, bounded so a stuck join can
# never hang shutdown() forever (SALTMDB ebff13f5). No documented shutdown
# time budget exists elsewhere (DAEMON.md's "Shutdown / Stop Semantics") to
# match, so this is a fresh, deliberately generous choice.
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 10.0


def create_daemon(
    *,
    state_dir: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    election_port: int | None = None,
) -> DaemonServer:
    """Assemble a daemon with its real persistence, bootstrap, and dispatch paths.

    ``state_dir`` defaults to ``~/.acie`` and is injectable so callers and
    tests can choose the complete on-disk state root without replacing the
    individual production dependencies.
    """
    state_dir = state_dir or os.path.expanduser("~/.acie")

    def db_path_for(repo_id: str) -> str:
        # Pure lookup, no `git` subprocess: repo_id is already the
        # canonical identity resolved once per request by _resolve_repo()
        # below (decision 10 fix, SALTMDB f4bdfc9d, grilled 2026-09-02) --
        # a repo_id hash can't be reversed back into a repo_path to
        # re-shell git, and once you already have it, index.sqlite's
        # location is just a string join (mirrors resolve_repo_state_dir's
        # own repos/<repo-id>/ layout). Still creates the parent directory
        # as a side effect, same contract resolve_repo_state_dir had --
        # WriteQueue's writer thread does a bare sqlite3.connect() with no
        # parent-dir handling of its own.
        repo_dir = os.path.join(state_dir, "repos", repo_id)
        os.makedirs(repo_dir, exist_ok=True)
        return os.path.join(repo_dir, "index.sqlite")

    def walk_repo(repo_root: str):
        is_ignored = ignore.get_ignore_matcher(repo_root).matches
        return _read_source_files(repo_root, path_glob=None, is_ignored=is_ignored).items()

    write_queue = WriteQueue(db_path_for=db_path_for)
    bootstrap = BootstrapCoordinator(
        write_queue=write_queue,
        db_path_for=db_path_for,
        walk_repo=walk_repo,
    )
    watchers = WatcherRegistry(write_queue)

    def _resolve_repo(repo_path: str) -> tuple[str, str] | None:
        # Resolved once per request and threaded through register_repo,
        # repo_ready, and the notify_hook branch below (decision 10 fix,
        # SALTMDB f4bdfc9d, grilled 2026-09-02): WriteQueue/
        # BootstrapCoordinator key their in-memory state on repo_id, the
        # canonical worktree-collapsing value, so two different spellings
        # of the same repo -- a symlink vs its realpath'd twin, or two
        # worktrees -- share one writer thread and one bootstrap-readiness
        # flag. WatcherRegistry keys on repo_root instead (see watcher.py):
        # a symlink/realpath'd spelling of one worktree still collapses to
        # one Observer, but two genuinely distinct worktrees intentionally
        # each get their own -- repo_root is the actual directory being
        # watched, not a repo-wide identity.
        repo_id = resolve_repo_id(repo_path)
        if repo_id is None:
            return None
        repo_root = resolve_repo_root(repo_path)
        if repo_root is None:
            return None
        return repo_id, repo_root

    def register_repo(repo_id: str, repo_root: str) -> None:
        bootstrap.register(repo_id, repo_root)
        # decision 5 (watcher/incremental-indexing grilling): same
        # implicit-on-first-RPC lifecycle as bootstrap.register -- no
        # separate "known repos" registry to enumerate at daemon startup,
        # so a watcher starts here too, on demand.
        watchers.register(repo_id, repo_root)

    def dispatch(request: dict) -> dict:
        repo_path = request.get("repo_path")
        resolved: tuple[str, str] | None = None
        if isinstance(repo_path, str) and repo_path:
            resolved = _resolve_repo(repo_path)
            if resolved is not None:
                register_repo(*resolved)

        if request.get("method") == _NOTIFY_HOOK_METHOD:
            return _dispatch_notify_hook(request, resolved=resolved)

        if resolved is not None:
            repo_id, repo_root = resolved
            if bootstrap.repo_ready(repo_id):
                ensure_fresh(write_queue, repo_id, repo_root, request.get("method"), request.get("params"))

        def repo_ready(_repo_path: str) -> bool:
            # dispatch_request owns the malformed-repo response and calls
            # this with the identical repo_path this closure already
            # resolved above (same request, same envelope field) -- reuse
            # that instead of a second resolve_repo_id/`git` subprocess
            # call. dispatch.py's own separate resolve_index_db_path call
            # (for opening its read-path stores) is the one remaining
            # resolution this doesn't collapse -- that's dispatch.py's own
            # separately-tested contract, out of this fix's scope (a real
            # fix there raises cache-invalidation questions -- a repo_path
            # could in principle start/stop being a git repo mid-daemon-
            # lifetime -- that deserve their own design pass, not a quick
            # cache bolted onto an unrelated bug-fix batch).
            if resolved is None:
                return True
            repo_id, _ = resolved
            return bootstrap.repo_ready(repo_id)

        return dispatch_request(request, repo_ready=repo_ready, base_dir=state_dir)

    def _dispatch_notify_hook(request: dict, *, resolved: tuple[str, str] | None) -> dict:
        # A control-plane call like server.py's shutdown/ping, not one of
        # the 8 read-only tools in dispatch.py's DISPATCH_TABLE -- it never
        # touches repo_ready/INDEX_NOT_READY, since its whole point is to
        # work on a repo that isn't indexed yet (register_repo above
        # already kicked off bootstrap if this is the first time).
        request_id = request.get("id")
        repo_path = request.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path:
            return build_error_response(request_id, "MALFORMED_REQUEST", "repo_path is required")
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        agent = params.get("agent")
        payload = params.get("payload", "")
        if not isinstance(agent, str) or not agent:
            return build_error_response(request_id, "MALFORMED_REQUEST", "params.agent is required")
        if not isinstance(payload, str):
            payload = ""
        if resolved is not None:
            repo_id, repo_root = resolved
            handle_notify_hook(
                agent=agent, repo_id=repo_id, repo_root=repo_root, payload=payload,
                write_queue=write_queue, db_path_for=db_path_for,
            )
        # resolved is None means repo_path isn't inside a git repository --
        # silent no-op, matching ARCHITECTURE.md's notify-hook contract of
        # never breaking the caller (register_repo above already declined
        # to run for the same reason).
        return build_success_response(request_id, {"status": "accepted"})

    def on_shutdown() -> None:
        # Bounded, never None -- SALTMDB ebff13f5's live incident was an
        # orphaned daemon process, permanently un-killable by SIGTERM,
        # stuck exactly here (watchers.close()/write_queue.close() used to
        # be called with no timeout at all, i.e. an unconditionally
        # unbounded wait). Both close() methods now log a warning per
        # watcher/writer that doesn't finish draining within budget, but
        # this call itself must always return -- a daemon that can never
        # fully exit is worse than one that occasionally abandons a slow
        # drain in the background. One shared deadline spans both calls
        # (not one fresh budget each) so total drain time is bounded by
        # _SHUTDOWN_DRAIN_TIMEOUT_SECONDS overall, not 2x it.
        deadline = time.monotonic() + _SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        watchers.close(timeout=max(0.0, deadline - time.monotonic()))
        write_queue.close(timeout=max(0.0, deadline - time.monotonic()))

    return DaemonServer(
        dispatch,
        host=host,
        port=port,
        election_port=election_port,
        discovery_path=os.path.join(state_dir, "daemon.json"),
        auth_token=None,
        on_shutdown=on_shutdown,
    )
