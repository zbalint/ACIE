"""Command-line entry point for ACIE's daemon lifecycle."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

from acie.daemon.client import daemon_is_running, probe_daemon_status, request_daemon
from acie.daemon.server import main as run_daemon_foreground
from acie.mcp_server import run_stdio_server

_STARTUP_ATTEMPTS = 25
_STARTUP_POLL_SECONDS = 0.05
_RESPAWN_EVERY_ATTEMPTS = 5

# ARCHITECTURE.md "Agent Hook Integration": a hook integration must never
# break or delay the calling agent's own tool-use flow under any failure
# condition, so this is a strict client-side budget, not a generous one --
# Claude Code's PostToolUse hook blocks the agent's current turn until
# this process exits.
_NOTIFY_HOOK_TIMEOUT_SECONDS = 0.2
_NOTIFY_HOOK_AGENTS = ("git", "claude-code", "codex")


def main(argv: Sequence[str] | None = None) -> int:
    """Run an ACIE command and return its process exit status."""
    parser = argparse.ArgumentParser(prog="acie")
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve_mcp = subcommands.add_parser("serve-mcp")
    serve_mcp.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    daemon = subcommands.add_parser("daemon")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    start = daemon_commands.add_parser("start")
    start.add_argument("--foreground", action="store_true")
    daemon_commands.add_parser("stop")
    status = daemon_commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    notify_hook = subcommands.add_parser("notify-hook")
    notify_hook.add_argument("--agent", required=True, choices=_NOTIFY_HOOK_AGENTS)

    args = parser.parse_args(argv)
    if args.command == "serve-mcp":
        return _serve_mcp(log_level=args.log_level)
    if args.command == "daemon":
        if args.daemon_command == "start":
            return _daemon_start(foreground=args.foreground)
        if args.daemon_command == "stop":
            return _daemon_stop()
        if args.daemon_command == "status":
            return _daemon_status(as_json=args.json)
    if args.command == "notify-hook":
        return _notify_hook(agent=args.agent)
    parser.error("unsupported command")


def _daemon_status(*, as_json: bool) -> int:
    discovery_path = _discovery_path()
    status = probe_daemon_status(discovery_path)
    if as_json:
        print(json.dumps({"running": status == "running", "status": status}))
    else:
        print(status)
    return 0 if status == "running" else 1


def _serve_mcp(*, log_level: str) -> int:
    if not _ensure_daemon():
        return 1
    run_stdio_server(discovery_path=_discovery_path(), log_level=log_level)
    return 0


def _daemon_start(*, foreground: bool) -> int:
    if foreground:
        return run_daemon_foreground()
    return 0 if _ensure_daemon() else 1


def _notify_hook(*, agent: str) -> int:
    """The sole public integration contract for tier-2/tier-3 incremental
    indexing (ARCHITECTURE.md "Agent Hook Integration") -- fire-and-forget,
    always exits 0 regardless of outcome (daemon not installed, not
    running, unreachable, slow, or returning an error) so a git hook or an
    agent's PostToolUse hook can pipe into this unconditionally.
    """
    try:
        payload = sys.stdin.read()
    except Exception:  # noqa: BLE001 -- stdin read failures must not surface; see docstring.
        payload = ""
    request_daemon(
        _discovery_path(), method="notify_hook", repo_path=os.getcwd(),
        params={"agent": agent, "payload": payload}, timeout=_NOTIFY_HOOK_TIMEOUT_SECONDS,
    )
    return 0


def _daemon_stop() -> int:
    # Lazy import, matching server.py::main()'s own pattern -- keeps
    # runtime.py's heavier daemon-assembly import graph off every other
    # CLI command's startup path. The `shutdown` RPC only responds once
    # server.py::DaemonServer.shutdown() has fully returned, i.e. after
    # its whole drain, so the client socket timeout must exceed the
    # server's real drain budget (runtime.py's
    # _SHUTDOWN_DRAIN_TIMEOUT_SECONDS) or `stop`'s own exit code is
    # meaningless for any shutdown slower than request_daemon's 2.0s
    # library default -- see SALTMDB 4083924d-ed96-4356-8002-c3ce224daeb5.
    from acie.daemon.runtime import _SHUTDOWN_DRAIN_TIMEOUT_SECONDS

    response = request_daemon(
        _discovery_path(), method="shutdown", repo_path="", params={},
        timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS + 1.0,
    )
    return 0 if response is not None and response.get("ok") is True else 1


def _ensure_daemon() -> bool:
    discovery_path = _discovery_path()
    if daemon_is_running(discovery_path):
        return True
    for attempt in range(_STARTUP_ATTEMPTS):
        if attempt % _RESPAWN_EVERY_ATTEMPTS == 0:
            _spawn_daemon()
        time.sleep(_STARTUP_POLL_SECONDS)
        if daemon_is_running(discovery_path):
            return True
    return False


def _spawn_daemon() -> None:
    state_dir = os.path.dirname(_discovery_path())
    os.makedirs(state_dir, exist_ok=True)
    log_path = os.path.join(state_dir, "daemon.log")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "acie.daemon.server"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    # Reaps the child the moment it exits (e.g. it lost the election-port
    # race and exited immediately) so it never sits as a zombie for the
    # rest of this process's lifetime -- nothing else here ever calls
    # wait()/poll() on it.
    threading.Thread(target=proc.wait, daemon=True).start()


def _discovery_path() -> str:
    return os.path.join(os.path.expanduser("~/.acie"), "daemon.json")
