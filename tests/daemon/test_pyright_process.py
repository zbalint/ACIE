import logging
import subprocess
import sys
import time

import pytest

from acie.daemon.pyright_process import PyrightProcessRegistry, _default_locate_binary


_IDLE_TIMEOUT = 0.05
_WAIT_PAST_IDLE_TIMEOUT = 0.3


def _spawn_sleeping_process(binary_path: str, repo_root: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        cwd=repo_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _registry(*, spawn=_spawn_sleeping_process, idle_timeout_seconds=900.0):
    return PyrightProcessRegistry(
        locate_binary=lambda: sys.executable,
        spawn=spawn,
        idle_timeout_seconds=idle_timeout_seconds,
        terminate_timeout_seconds=0.1,
    )


def _wait_until_dead(process) -> None:
    deadline = time.monotonic() + _WAIT_PAST_IDLE_TIMEOUT
    while process.is_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.is_alive is False
    assert process.popen.poll() is not None


def test_ensure_process_lazily_spawns_a_process_for_a_new_repo_root(tmp_path):
    spawned = []

    def spawn(binary_path, repo_root):
        spawned.append((binary_path, repo_root))
        return _spawn_sleeping_process(binary_path, repo_root)

    registry = _registry(spawn=spawn)
    try:
        process = registry.ensure_process(str(tmp_path))

        assert process is not None
        assert process.is_alive is True
        assert spawned == [(sys.executable, str(tmp_path))]
    finally:
        registry.close()


def test_ensure_process_returns_the_same_process_on_a_second_call_for_the_same_repo_root(tmp_path):
    registry = _registry()
    try:
        first = registry.ensure_process(str(tmp_path))
        second = registry.ensure_process(str(tmp_path))

        assert second is first
    finally:
        registry.close()


def test_ensure_process_spawns_independent_processes_for_different_repo_roots(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    registry = _registry()
    try:
        first = registry.ensure_process(str(first_root))
        second = registry.ensure_process(str(second_root))

        assert first is not second
        assert first.popen is not second.popen
    finally:
        registry.close()


def test_ensure_process_returns_none_when_the_binary_is_not_located(tmp_path):
    registry = PyrightProcessRegistry(locate_binary=lambda: None)

    assert registry.ensure_process(str(tmp_path)) is None


def test_missing_binary_is_logged_only_once_across_repeated_ensure_process_calls(tmp_path, caplog):
    registry = PyrightProcessRegistry(locate_binary=lambda: None)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert registry.ensure_process(str(tmp_path)) is None

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_touch_extends_the_idle_deadline_past_the_original_timeout(tmp_path):
    registry = _registry(idle_timeout_seconds=_IDLE_TIMEOUT)
    try:
        process = registry.ensure_process(str(tmp_path))
        assert process is not None

        for _ in range(3):
            time.sleep(_IDLE_TIMEOUT / 2)
            registry.touch(str(tmp_path))
            assert process.is_alive is True
    finally:
        registry.close()


def test_an_untouched_process_is_torn_down_after_the_idle_timeout(tmp_path):
    registry = _registry(idle_timeout_seconds=_IDLE_TIMEOUT)
    process = registry.ensure_process(str(tmp_path))
    assert process is not None

    _wait_until_dead(process)
    registry.close()


def test_ensure_process_after_idle_teardown_respawns_a_fresh_process(tmp_path):
    spawned = []

    def spawn(binary_path, repo_root):
        process = _spawn_sleeping_process(binary_path, repo_root)
        spawned.append(process)
        return process

    registry = _registry(spawn=spawn, idle_timeout_seconds=_IDLE_TIMEOUT)
    try:
        first = registry.ensure_process(str(tmp_path))
        assert first is not None
        _wait_until_dead(first)

        second = registry.ensure_process(str(tmp_path))

        assert second is not first
        assert len(spawned) == 2
    finally:
        registry.close()


def test_a_crashed_process_is_detected_and_transparently_respawned(tmp_path):
    registry = _registry()
    try:
        first = registry.ensure_process(str(tmp_path))
        assert first is not None
        first.popen.kill()
        first.popen.wait(timeout=1)

        second = registry.ensure_process(str(tmp_path))

        assert second is not first
        assert second.is_alive is True
    finally:
        registry.close()


def test_close_terminates_every_live_process(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    registry = _registry()
    first = registry.ensure_process(str(first_root))
    second = registry.ensure_process(str(second_root))
    assert first is not None
    assert second is not None

    registry.close()

    assert first.popen.poll() is not None
    assert second.popen.poll() is not None


class _StuckPopen:
    def __init__(self):
        self.wait_timeouts = []

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if timeout:
            time.sleep(timeout)
        raise subprocess.TimeoutExpired("fake", timeout)

    def kill(self):
        pass


def test_close_is_bounded_by_one_shared_deadline_across_multiple_processes(tmp_path):
    stuck_processes = []

    def spawn(binary_path, repo_root):
        process = _StuckPopen()
        stuck_processes.append(process)
        return process

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    registry = _registry(spawn=spawn)
    registry.ensure_process(str(first_root))
    registry.ensure_process(str(second_root))

    started = time.monotonic()
    registry.close(timeout=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 0.12
    assert stuck_processes[0].wait_timeouts[0] > 0
    assert stuck_processes[1].wait_timeouts[0] == 0.0


def test_close_cancels_pending_idle_timers_so_none_fire_after_close(tmp_path):
    registry = _registry(idle_timeout_seconds=_IDLE_TIMEOUT)
    process = registry.ensure_process(str(tmp_path))
    assert process is not None

    registry.close()
    time.sleep(_WAIT_PAST_IDLE_TIMEOUT)

    assert process.popen.poll() is not None


def test_ensure_process_returns_none_after_close(tmp_path):
    spawned = []

    def spawn(binary_path, repo_root):
        spawned.append((binary_path, repo_root))
        return _spawn_sleeping_process(binary_path, repo_root)

    registry = _registry(spawn=spawn)
    registry.close()

    assert registry.ensure_process(str(tmp_path)) is None
    assert spawned == []


def test_default_locate_binary_prefers_basedpyright_langserver(monkeypatch):
    calls = []
    monkeypatch.setattr("acie.daemon.pyright_process.shutil.which", lambda name: calls.append(name) or "binary")

    assert _default_locate_binary() == "binary"
    assert calls == ["basedpyright-langserver"]


def test_default_locate_binary_falls_back_to_pyright_langserver(monkeypatch):
    calls = []

    def which(name):
        calls.append(name)
        return "binary" if name == "pyright-langserver" else None

    monkeypatch.setattr("acie.daemon.pyright_process.shutil.which", which)

    assert _default_locate_binary() == "binary"
    assert calls == ["basedpyright-langserver", "pyright-langserver"]


def test_version_probe_failure_does_not_prevent_spawn(tmp_path, monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("acie.daemon.pyright_process.subprocess.run", raise_timeout)
    registry = _registry()
    try:
        process = registry.ensure_process(str(tmp_path))

        assert process is not None
        assert process.version is None
        assert process.is_alive is True
    finally:
        registry.close()
