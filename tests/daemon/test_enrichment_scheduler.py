import threading
import time

from acie.daemon import enrichment_scheduler as scheduler_module
from acie.daemon.enrichment_scheduler import EnrichmentScheduler


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _make_scheduler(runner, *, quiet_seconds=0.01, max_wait_seconds=1.0, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(scheduler_module, "_run_triggered_enrichment", runner)
    return EnrichmentScheduler(
        object(),
        object(),
        lambda repo_id: f"{repo_id}.sqlite",
        walk_repo=lambda repo_root: [],
        quiet_seconds=quiet_seconds,
        max_wait_seconds=max_wait_seconds,
    )


def test_bootstrap_migration_and_reconciliation_triggers_coalesce_without_concurrent_passes(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    calls = []

    def runner(*args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(args[3])
            call_number = len(calls)
            if call_number == 1:
                first_started.set()
        if call_number == 1:
            release_first.wait(timeout=2.0)
        with lock:
            active -= 1
            if len(calls) == 2 and active == 0:
                finished.set()

    scheduler = _make_scheduler(runner, monkeypatch=monkeypatch)
    scheduler.trigger_now("repo-a", "/repo-a")  # bootstrap completion
    assert first_started.wait(timeout=2.0)
    scheduler.trigger_now("repo-a", "/repo-a")  # migration completion
    scheduler.on_watcher_edit("repo-a", "/repo-a")  # reconciliation/live source overlap
    time.sleep(0.05)  # let the live-edit quiet timer become the one pending rerun
    release_first.set()

    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a", "repo-a"]
    assert max_active == 1


def test_repeated_triggers_while_running_schedule_exactly_one_follow_up(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    finished = threading.Event()
    calls = []

    def runner(*args):
        calls.append(args[3])
        if len(calls) == 1:
            first_started.set()
            release_first.wait(timeout=2.0)
        else:
            finished.set()

    scheduler = _make_scheduler(runner, monkeypatch=monkeypatch)
    scheduler.trigger_now("repo-a", "/repo-a")
    assert first_started.wait(timeout=2.0)
    for _ in range(20):
        scheduler.trigger_now("repo-a", "/repo-a")

    release_first.set()
    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a", "repo-a"]


def test_completion_coalesces_two_immediate_and_one_reconciliation_trigger(monkeypatch):
    calls = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    finished = threading.Event()

    def runner(*args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(args[3])
            call_number = len(calls)
            if call_number == 1:
                first_started.set()
            if call_number == 2:
                second_started.set()
        if call_number == 1:
            release_first.wait(timeout=2.0)
        with lock:
            active -= 1
            if call_number == 2:
                finished.set()

    scheduler = _make_scheduler(runner, monkeypatch=monkeypatch)
    scheduler.trigger_now("repo-a", "/repo-a")  # bootstrap completion
    assert first_started.wait(timeout=2.0)
    scheduler.trigger_now("repo-a", "/repo-a")  # migration completion

    def reconciliation_trigger(repo_id, repo_root):
        scheduler.trigger_now(repo_id, repo_root)

    reconciliation_trigger("repo-a", "/repo-a")
    release_first.set()

    assert second_started.wait(timeout=2.0)
    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a", "repo-a"]
    assert max_active == 1


class _FakeTimer:
    instances = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.cancelled = False
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self, *, ignore_cancel=False):
        if not self.cancelled or ignore_cancel:
            self.function(*self.args)


def test_watcher_edit_resets_quiet_timer_and_fires_after_the_latest_one(monkeypatch):
    calls = []
    finished = threading.Event()

    def runner(*args):
        calls.append(args[3])
        finished.set()

    _FakeTimer.instances = []
    monkeypatch.setattr(scheduler_module.threading, "Timer", _FakeTimer)
    scheduler = _make_scheduler(runner, monkeypatch=monkeypatch)
    scheduler.on_watcher_edit("repo-a", "/repo-a")
    scheduler.on_watcher_edit("repo-a", "/repo-a")

    max_timer, first, latest = _FakeTimer.instances
    assert max_timer.cancelled is False
    assert first.cancelled is True
    assert latest.cancelled is False
    assert calls == []

    first.fire(ignore_cancel=True)
    time.sleep(0.02)
    assert calls == []
    assert latest.cancelled is False

    latest.fire()
    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a"]


def test_watcher_edit_fires_at_max_wait_and_cancels_the_quiet_timer(monkeypatch):
    now = [100.0]
    calls = []
    finished = threading.Event()

    def runner(*args):
        calls.append(args[3])
        finished.set()

    class _Clock:
        @classmethod
        def monotonic(cls):
            return now[0]

    _FakeTimer.instances = []
    monkeypatch.setattr(scheduler_module, "time", _Clock)
    monkeypatch.setattr(scheduler_module.threading, "Timer", _FakeTimer)
    scheduler = _make_scheduler(runner, quiet_seconds=30.0, max_wait_seconds=5.0, monkeypatch=monkeypatch)
    scheduler.on_watcher_edit("repo-a", "/repo-a")
    max_timer, quiet_timer = _FakeTimer.instances

    now[0] = 106.0
    scheduler.on_watcher_edit("repo-a", "/repo-a")

    assert max_timer.cancelled is True
    assert quiet_timer.cancelled is True
    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a"]


def test_watcher_edit_fires_at_max_wait_without_another_edit(monkeypatch):
    class _Clock:
        now = 100.0

        @classmethod
        def monotonic(cls):
            return cls.now

    calls = []
    finished = threading.Event()

    def runner(*args):
        calls.append(args[3])
        finished.set()

    _FakeTimer.instances = []
    monkeypatch.setattr(scheduler_module, "time", _Clock)
    monkeypatch.setattr(scheduler_module.threading, "Timer", _FakeTimer)
    scheduler = _make_scheduler(runner, quiet_seconds=30.0, max_wait_seconds=5.0, monkeypatch=monkeypatch)
    scheduler.on_watcher_edit("repo-a", "/repo-a")
    max_timer = next(timer for timer in _FakeTimer.instances if timer.interval == 5.0)

    _Clock.now = 105.0
    max_timer.fire()

    assert finished.wait(timeout=2.0)
    assert calls == ["repo-a"]
