"""Command-line entry point for ACIE's daemon lifecycle."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

from acie.daemon.client import daemon_is_running, request_daemon
from acie.daemon.server import main as run_daemon_foreground
from acie.mcp_server import run_stdio_server

_STARTUP_ATTEMPTS = 25
_STARTUP_POLL_SECONDS = 0.05
_RESPAWN_EVERY_ATTEMPTS = 5


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
    parser.error("unsupported command")


def _daemon_status(*, as_json: bool) -> int:
    discovery_path = _discovery_path()
    running = daemon_is_running(discovery_path)
    if as_json:
        print(json.dumps({"running": running}))
    else:
        print("running" if running else "stopped")
    return 0 if running else 1


def _serve_mcp(*, log_level: str) -> int:
    if not _ensure_daemon():
        return 1
    run_stdio_server(discovery_path=_discovery_path(), log_level=log_level)
    return 0


def _daemon_start(*, foreground: bool) -> int:
    if foreground:
        return run_daemon_foreground()
    return 0 if _ensure_daemon() else 1


def _daemon_stop() -> int:
    response = request_daemon(
        _discovery_path(), method="shutdown", repo_path="", params={}
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
