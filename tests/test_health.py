"""Tests voor core.health: HealthState updates, describe snapshot,
HealthMonitor failure-counting + kill-after-N gedrag."""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

from core.health import HealthMonitor, HealthState, describe_health


def test_state_records_imessage_poll() -> None:
    s = HealthState()
    initial = s.snapshot()["last_imessage_poll_at"]
    time.sleep(0.01)
    s.record_imessage_poll()
    assert s.snapshot()["last_imessage_poll_at"] > initial


def test_state_records_scheduler_tick() -> None:
    s = HealthState()
    initial = s.snapshot()["last_scheduler_tick_at"]
    time.sleep(0.01)
    s.record_scheduler_tick()
    assert s.snapshot()["last_scheduler_tick_at"] > initial


def test_describe_health_includes_uptime_and_status() -> None:
    s = HealthState()
    text = describe_health(s)
    assert "Rosa alive" in text
    assert "Uptime:" in text
    assert "Last iMessage poll:" in text
    assert "Last scheduler tick:" in text
    assert "FDs:" in text
    assert "RAM" in text


# --- monitor ---------------------------------------------------------------

def _make_monitor(state: HealthState, **overrides: Any) -> HealthMonitor:
    sent: list[tuple[str, str]] = []
    def send(handle: str, body: str) -> None:
        sent.append((handle, body))
    monitor = HealthMonitor(
        state=state, stop_event=threading.Event(),
        send_imessage=send, primary_handle="+316",
        check_interval_seconds=60, log_interval_seconds=900,
        imessage_poll_max_silence_seconds=overrides.get("poll_max", 30),
        scheduler_tick_max_silence_seconds=overrides.get("sched_max", 60),
        consecutive_failures_before_kill=overrides.get("kill_after", 3),
    )
    # Expose sent-list for assertions.
    monitor._sent = sent  # type: ignore[attr-defined]
    return monitor


def test_monitor_no_problems_resets_failure_counter() -> None:
    state = HealthState()
    monitor = _make_monitor(state)
    monitor._consecutive_failures = 2
    monitor._tick()
    assert monitor._consecutive_failures == 0


def test_monitor_increments_on_imessage_poll_silence() -> None:
    state = HealthState()
    state.last_imessage_poll_at = time.time() - 120  # 2 min stil
    monitor = _make_monitor(state, poll_max=30)
    monitor._tick()
    assert monitor._consecutive_failures == 1
    assert any("iMessage poll silent" in body for _, body in monitor._sent)  # type: ignore[attr-defined]


def test_monitor_kills_process_after_threshold() -> None:
    state = HealthState()
    state.last_imessage_poll_at = time.time() - 120
    monitor = _make_monitor(state, poll_max=30, kill_after=2)
    with patch("os.kill") as kill_mock:
        monitor._tick()
        monitor._tick()
    assert monitor._consecutive_failures == 2
    kill_mock.assert_called_once()  # SIGTERM raised


def test_monitor_logs_only_once_per_period() -> None:
    """Periodieke log-line firet niet bij elke tick — alleen na log_interval."""
    state = HealthState()
    monitor = _make_monitor(state)
    monitor._log_interval = 1000
    monitor._tick()
    first_log_ts = monitor._last_log_ts
    monitor._tick()
    assert monitor._last_log_ts == first_log_ts  # niet opnieuw bijgewerkt


def test_monitor_recovers_logs_message() -> None:
    state = HealthState()
    state.last_imessage_poll_at = time.time() - 120
    monitor = _make_monitor(state, poll_max=30)
    monitor._tick()  # fail
    assert monitor._consecutive_failures == 1
    state.record_imessage_poll()
    monitor._tick()  # recover
    assert monitor._consecutive_failures == 0
