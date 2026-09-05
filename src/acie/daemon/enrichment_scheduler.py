"""Shared trigger scheduling for opportunistic repository enrichment."""

import threading
import time
from typing import Callable, Iterable

from acie.daemon.dispatch import walk_repo
from acie.daemon.pyright_process import PyrightProcessRegistry
from acie.daemon.write_queue import WriteQueue


def _run_triggered_enrichment(*args) -> None:
    """Call runtime's worker without introducing an import cycle."""
    from acie.daemon.runtime import _run_triggered_enrichment as run

    run(*args)


class EnrichmentScheduler:
    """Coalesce every enrichment trigger source through one repo guard.

    Bootstrap, migration, reconciliation, and watcher callbacks all consult
    the same lock-protected ``_running``/``_pending_rerun`` state. Therefore a
    three-way bootstrap double-fire plus reconciliation race can only start
    the current pass and one follow-up; no source can start a concurrent pass
    or drop the fact that another trigger arrived while the pass was running.
    """

    def __init__(
        self,
        process_registry: PyrightProcessRegistry,
        write_queue: WriteQueue,
        db_path_for: Callable[[str], str],
        *,
        walk_repo: Callable[[str], Iterable[tuple[str, str]]] = walk_repo,
        quiet_seconds: float = 30.0,
        max_wait_seconds: float = 300.0,
    ) -> None:
        self._process_registry = process_registry
        self._write_queue = write_queue
        self._db_path_for = db_path_for
        self._walk_repo = walk_repo
        self._quiet_seconds = quiet_seconds
        self._max_wait_seconds = max_wait_seconds
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._pending_rerun: set[str] = set()
        self._quiet_timers: dict[str, threading.Timer] = {}
        self._max_wait_timers: dict[str, threading.Timer] = {}
        self._max_wait_tokens: dict[str, object] = {}
        self._quiet_timer_tokens: dict[str, object] = {}
        self._first_pending_at: dict[str, float] = {}

    def trigger_now(self, repo_id: str, repo_root: str) -> None:
        """Trigger immediately for bootstrap/migration/reconciliation events."""
        with self._lock:
            self._fire_or_mark_pending_locked(repo_id, repo_root)

    def on_watcher_edit(self, repo_id: str, repo_root: str) -> None:
        """Debounce watcher edits, capped from the first pending edit."""
        with self._lock:
            now = time.monotonic()
            first_pending_at = self._first_pending_at.get(repo_id)
            if first_pending_at is None:
                first_pending_at = now
                self._first_pending_at[repo_id] = first_pending_at
                max_token = object()
                self._max_wait_tokens[repo_id] = max_token
            else:
                max_token = self._max_wait_tokens.get(repo_id)
                if max_token is None:
                    max_token = object()
                    self._max_wait_tokens[repo_id] = max_token

            elapsed = now - first_pending_at
            if elapsed >= self._max_wait_seconds:
                self._clear_pending_timing_locked(repo_id)
                self._fire_or_mark_pending_locked(repo_id, repo_root)
                return

            if repo_id not in self._max_wait_timers:
                self._start_max_wait_timer_locked(
                    repo_id,
                    repo_root,
                    max_token,
                    self._max_wait_seconds - elapsed,
                )

            timer = self._quiet_timers.pop(repo_id, None)
            if timer is not None:
                timer.cancel()
            quiet_token = object()
            self._quiet_timer_tokens[repo_id] = quiet_token
            timer = threading.Timer(
                self._quiet_seconds,
                self._on_quiet_elapsed,
                args=(repo_id, repo_root, quiet_token),
            )
            timer.daemon = True
            self._quiet_timers[repo_id] = timer
            timer.start()

    def _on_quiet_elapsed(self, repo_id: str, repo_root: str, token: object | None = None) -> None:
        with self._lock:
            if token is not None and self._quiet_timer_tokens.get(repo_id) is not token:
                return
            self._clear_pending_timing_locked(repo_id)
            self._fire_or_mark_pending_locked(repo_id, repo_root)

    def _start_max_wait_timer_locked(
        self,
        repo_id: str,
        repo_root: str,
        token: object,
        delay: float,
    ) -> None:
        timer = threading.Timer(
            max(0.0, delay),
            self._on_max_wait_elapsed,
            args=(repo_id, repo_root, token),
        )
        timer.daemon = True
        self._max_wait_timers[repo_id] = timer
        timer.start()

    def _on_max_wait_elapsed(self, repo_id: str, repo_root: str, token: object) -> None:
        with self._lock:
            if self._max_wait_tokens.get(repo_id) is not token:
                return
            self._max_wait_timers.pop(repo_id, None)
            first_pending_at = self._first_pending_at.get(repo_id)
            if first_pending_at is None:
                return
            remaining = self._max_wait_seconds - (time.monotonic() - first_pending_at)
            if remaining > 0:
                self._start_max_wait_timer_locked(repo_id, repo_root, token, remaining)
                return
            self._clear_pending_timing_locked(repo_id)
            self._fire_or_mark_pending_locked(repo_id, repo_root)

    def _clear_pending_timing_locked(self, repo_id: str) -> None:
        timer = self._quiet_timers.pop(repo_id, None)
        if timer is not None:
            timer.cancel()
        timer = self._max_wait_timers.pop(repo_id, None)
        if timer is not None:
            timer.cancel()
        self._quiet_timer_tokens.pop(repo_id, None)
        self._max_wait_tokens.pop(repo_id, None)
        self._first_pending_at.pop(repo_id, None)

    def _fire_or_mark_pending_locked(self, repo_id: str, repo_root: str) -> None:
        # Caller already holds _lock. The single running bit is the total
        # ordering point shared by all trigger sources for this repo.
        if repo_id in self._running:
            self._pending_rerun.add(repo_id)
            return
        self._clear_pending_timing_locked(repo_id)
        self._running.add(repo_id)
        threading.Thread(target=self._run, args=(repo_id, repo_root), daemon=True).start()

    def _run(self, repo_id: str, repo_root: str) -> None:
        try:
            _run_triggered_enrichment(
                self._process_registry,
                self._write_queue,
                self._db_path_for,
                repo_id,
                repo_root,
                self._walk_repo,
            )
        finally:
            with self._lock:
                self._running.discard(repo_id)
                if repo_id in self._pending_rerun:
                    self._pending_rerun.discard(repo_id)
                    self._fire_or_mark_pending_locked(repo_id, repo_root)
