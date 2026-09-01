"""Production assembly of ACIE's daemon runtime dependencies.

This is the seam where the daemon's existing transport, bootstrap, write
queue, and dispatch modules become one running process. CLI and MCP-server
client concerns intentionally remain outside this module.
"""

import os

from acie.daemon.bootstrap import BootstrapCoordinator
from acie.daemon.dispatch import _read_source_files, dispatch_request
from acie.daemon.server import DaemonServer
from acie.daemon.write_queue import WriteQueue
from acie.repo_id import resolve_index_db_path, resolve_repo_root


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
        return _read_source_files(repo_root, path_glob=None).items()

    write_queue = WriteQueue(db_path_for=db_path_for)
    bootstrap = BootstrapCoordinator(
        write_queue=write_queue,
        db_path_for=db_path_for,
        walk_repo=walk_repo,
    )

    def repo_ready(repo_path: str) -> bool:
        # dispatch_request owns the malformed-repo response. It checks
        # readiness before resolving its own DB path, so do not let an
        # invalid repo reach BootstrapCoordinator's path resolver first.
        if resolve_index_db_path(repo_path, base_dir=state_dir) is None:
            return True
        return bootstrap.repo_ready(repo_path)

    def dispatch(request: dict) -> dict:
        repo_path = request.get("repo_path")
        if isinstance(repo_path, str) and repo_path:
            if resolve_index_db_path(repo_path, base_dir=state_dir) is not None:
                bootstrap.register(repo_path)
        return dispatch_request(request, repo_ready=repo_ready, base_dir=state_dir)

    return DaemonServer(
        dispatch,
        host=host,
        port=port,
        election_port=election_port,
        discovery_path=os.path.join(state_dir, "daemon.json"),
        auth_token=None,
        on_shutdown=write_queue.close,
    )
