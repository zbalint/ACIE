"""Lifecycle ownership for opportunistic per-repo pyright subprocesses.

This module implements D1 decisions 1-13: it owns lazy subprocess creation
and bounded teardown only, never LSP JSON-RPC (D2), enrichment writes (D3),
or daemon wiring (D6). Processes are keyed by canonical ``repo_root``;
``pyright-langserver --stdio`` is discovered from PATH, spawned with pipes
ready for D2, and reaped after idle time because a warmed pyright server can
consume substantial memory. A missing binary, spawn failure, or crashed child
silently degrades to no process or a fresh later spawn.

Decision 4 keeps discovery injectable, checking ``basedpyright-langserver``
(the binary the now-mandatory basedpyright dependency actually ships) first,
falling back to ``pyright-langserver`` for a pre-existing plain-pyright
install on PATH. Decision 5 deliberately sends stderr to DEVNULL because D1
drains no pipe. Decision 6 records a best-effort version probe only. Decisions 7-9 use
resettable Timer-based idle expiry and lazy crash detection. Decision 10 warns
once for a missing binary. Decision 11 uses double-checked per-root creation
locks. Decisions 12-13 use bounded terminate/wait/kill and one shared close
deadline, cancelling every idle timer before shutdown begins.
"""

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

_logger = logging.getLogger(__name__)


def _default_locate_binary() -> str | None:
    return shutil.which("basedpyright-langserver") or shutil.which("pyright-langserver")


def _default_spawn(binary_path: str, repo_root: str) -> subprocess.Popen:
    return subprocess.Popen(
        [binary_path, "--stdio"],
        cwd=repo_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


class PyrightProcess:
    """One live pyright-langserver child and its observed provenance."""

    def __init__(
        self,
        repo_root: str,
        binary_path: str,
        version: str | None,
        popen: subprocess.Popen,
    ) -> None:
        self.repo_root = repo_root
        self.binary_path = binary_path
        self.version = version
        self.popen = popen

    @property
    def is_alive(self) -> bool:
        return self.popen.poll() is None


@dataclass
class _ProcessEntry:
    process: PyrightProcess
    timer: threading.Timer
    idle_deadline: float


class PyrightProcessRegistry:
    """Lazily owns one pyright child per repo root until idle or closed."""

    def __init__(
        self,
        *,
        locate_binary: Callable[[], str | None] = _default_locate_binary,
        spawn: Callable[[str, str], subprocess.Popen] = _default_spawn,
        idle_timeout_seconds: float = 900.0,
        terminate_timeout_seconds: float = 5.0,
    ) -> None:
        self._locate_binary = locate_binary
        self._spawn = spawn
        self._idle_timeout_seconds = idle_timeout_seconds
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, _ProcessEntry] = {}
        self._creation_locks: dict[str, threading.Lock] = {}
        self._closed = threading.Event()
        self._warned_missing_binary = False

    def ensure_process(self, repo_root: str) -> PyrightProcess | None:
        """Returns a live child for repo_root, spawning one only when needed."""
        if self._closed.is_set():
            return None

        existing = self._live_process_for(repo_root)
        if existing is not None:
            self.touch(repo_root)
            return existing

        with self._lock:
            creation_lock = self._creation_locks.setdefault(repo_root, threading.Lock())
        with creation_lock:
            if self._closed.is_set():
                return None
            existing = self._live_process_for(repo_root)
            if existing is not None:
                self.touch(repo_root)
                return existing

            binary_path = self._locate_binary()
            if binary_path is None:
                self._warn_missing_binary_once()
                return None

            version = self._probe_version(binary_path)
            try:
                process = PyrightProcess(repo_root, binary_path, version, self._spawn(binary_path, repo_root))
            except Exception:
                _logger.warning("Could not start pyright-langserver for repo %r", repo_root, exc_info=True)
                return None

            with self._lock:
                if self._closed.is_set():
                    should_terminate = True
                else:
                    self._entries[repo_root] = self._new_entry(repo_root, process)
                    should_terminate = False
            if should_terminate:
                self._terminate_process(process, self._terminate_timeout_seconds)
                return None
            return process

    def touch(self, repo_root: str) -> None:
        """Refreshes the idle deadline for an already-managed live child."""
        with self._lock:
            entry = self._entries.get(repo_root)
            if self._closed.is_set() or entry is None or not entry.process.is_alive:
                return
            entry.timer.cancel()
            entry.idle_deadline = time.monotonic() + self._idle_timeout_seconds
            entry.timer = self._new_timer(repo_root)
            entry.timer.start()

    def close(self, timeout: float | None = None) -> None:
        """Cancels timers and terminates children within one shared deadline."""
        self._closed.set()
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.timer.cancel()

        deadline = None if timeout is None else time.monotonic() + timeout
        for entry in entries:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            process_timeout = self._terminate_timeout_seconds if remaining is None else min(
                self._terminate_timeout_seconds, remaining
            )
            self._terminate_process(entry.process, process_timeout)

    def _live_process_for(self, repo_root: str) -> PyrightProcess | None:
        with self._lock:
            entry = self._entries.get(repo_root)
            if entry is None:
                return None
            if entry.process.is_alive:
                return entry.process
            entry.timer.cancel()
            del self._entries[repo_root]
            return None

    def _new_entry(self, repo_root: str, process: PyrightProcess) -> _ProcessEntry:
        deadline = time.monotonic() + self._idle_timeout_seconds
        entry = _ProcessEntry(process=process, timer=self._new_timer(repo_root), idle_deadline=deadline)
        entry.timer.start()
        return entry

    def _new_timer(self, repo_root: str) -> threading.Timer:
        return threading.Timer(self._idle_timeout_seconds, self._on_idle_timeout, args=(repo_root,))

    def _on_idle_timeout(self, repo_root: str) -> None:
        with self._lock:
            entry = self._entries.get(repo_root)
            if (
                self._closed.is_set()
                or entry is None
                or time.monotonic() < entry.idle_deadline
            ):
                return
            del self._entries[repo_root]
        self._terminate_process(entry.process, self._terminate_timeout_seconds)

    def _warn_missing_binary_once(self) -> None:
        with self._lock:
            if self._warned_missing_binary:
                return
            self._warned_missing_binary = True
        _logger.warning("pyright-langserver was not found on PATH; LSP enrichment is unavailable")

    @staticmethod
    def _probe_version(binary_path: str) -> str | None:
        try:
            result = subprocess.run(
                [binary_path, "--version"], capture_output=True, text=True, timeout=5.0, check=True
            )
        except Exception:
            return None
        return result.stdout.strip()

    @staticmethod
    def _terminate_process(process: PyrightProcess, timeout: float | None) -> None:
        popen = process.popen
        if popen.poll() is not None:
            return
        try:
            popen.terminate()
            popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            popen.kill()
        except Exception:
            _logger.warning("Could not terminate pyright-langserver for repo %r", process.repo_root, exc_info=True)
