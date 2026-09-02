"""Production assembly of ACIE's daemon runtime dependencies.

This is the seam where the daemon's existing transport, bootstrap, write
queue, and dispatch modules become one running process. CLI and MCP-server
client concerns intentionally remain outside this module.
"""

import os

from acie.daemon import ignore
from acie.daemon.bootstrap import BootstrapCoordinator
from acie.daemon.dispatch import _read_source_files, dispatch_request
from acie.daemon.notify_hook import handle_notify_hook
from acie.daemon.protocol import build_error_response, build_success_response
from acie.daemon.server import DaemonServer
from acie.daemon.watcher import WatcherRegistry
from acie.daemon.write_queue import WriteQueue
from acie.repo_id import resolve_index_db_path, resolve_repo_root

_NOTIFY_HOOK_METHOD = "notify_hook"


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

    def db_path_for(repo_path: str) -> str:
        db_path = resolve_index_db_path(repo_path, base_dir=state_dir)
        if db_path is None:
            raise ValueError(f"repo_path {repo_path!r} is not inside a git repository")
        return db_path

    def walk_repo(repo_path: str):
        repo_root = resolve_repo_root(repo_path)
        if repo_root is None:
            return []
        is_ignored = ignore.get_ignore_matcher(repo_root).matches
        return _read_source_files(repo_root, path_glob=None, is_ignored=is_ignored).items()

    write_queue = WriteQueue(db_path_for=db_path_for)
    bootstrap = BootstrapCoordinator(
        write_queue=write_queue,
        db_path_for=db_path_for,
        walk_repo=walk_repo,
    )
    watchers = WatcherRegistry(write_queue)

    def repo_ready(repo_path: str) -> bool:
        # dispatch_request owns the malformed-repo response. It checks
        # readiness before resolving its own DB path, so do not let an
        # invalid repo reach BootstrapCoordinator's path resolver first.
        #
        # shortcut: resolve_index_db_path shells out to `git` and is
        # called again here, again in the dispatch() closure below, and
        # again inside dispatch_request/BootstrapCoordinator themselves --
        # up to 3-4 real subprocess spawns for one request resolving the
        # same repo_path. Left as-is (no measured profiling data yet to
        # justify a cache, per this project's no-speculative-perf-refactor
        # rule); a real fix would also touch dispatch.py's own separately
        # tested resolution call and raise cache-invalidation questions
        # (a repo_path could in principle start/stop being a git repo
        # mid-daemon-lifetime) that deserve their own design pass. Upgrade
        # trigger: profile real request latency under load; if
        # git-subprocess overhead is measurably significant, consolidate
        # behind one cached resolution shared with dispatch.py.
        if resolve_index_db_path(repo_path, base_dir=state_dir) is None:
            return True
        return bootstrap.repo_ready(repo_path)

    def register_repo(repo_path: str) -> None:
        # shortcut: see repo_ready's docstring-adjacent comment above --
        # same redundant resolve_index_db_path call, same deferral.
        if resolve_index_db_path(repo_path, base_dir=state_dir) is None:
            return
        bootstrap.register(repo_path)
        # decision 5 (watcher/incremental-indexing grilling): same
        # implicit-on-first-RPC lifecycle as bootstrap.register -- no
        # separate "known repos" registry to enumerate at daemon startup,
        # so a watcher starts here too, on demand.
        repo_root = resolve_repo_root(repo_path)
        if repo_root is not None:
            watchers.register(repo_path, repo_root)

    def dispatch(request: dict) -> dict:
        repo_path = request.get("repo_path")
        if isinstance(repo_path, str) and repo_path:
            register_repo(repo_path)

        if request.get("method") == _NOTIFY_HOOK_METHOD:
            return _dispatch_notify_hook(request, repo_path=repo_path)

        return dispatch_request(request, repo_ready=repo_ready, base_dir=state_dir)

    def _dispatch_notify_hook(request: dict, *, repo_path) -> dict:
        # A control-plane call like server.py's shutdown/ping, not one of
        # the 8 read-only tools in dispatch.py's DISPATCH_TABLE -- it never
        # touches repo_ready/INDEX_NOT_READY, since its whole point is to
        # work on a repo that isn't indexed yet (register_repo above
        # already kicked off bootstrap if this is the first time).
        request_id = request.get("id")
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
        handle_notify_hook(
            agent=agent, repo_path=repo_path, payload=payload,
            register=register_repo, resolve_repo_root=resolve_repo_root,
            write_queue=write_queue, db_path_for=db_path_for,
        )
        return build_success_response(request_id, {"status": "accepted"})

    def on_shutdown() -> None:
        watchers.close()
        write_queue.close()

    return DaemonServer(
        dispatch,
        host=host,
        port=port,
        election_port=election_port,
        discovery_path=os.path.join(state_dir, "daemon.json"),
        auth_token=None,
        on_shutdown=on_shutdown,
    )
