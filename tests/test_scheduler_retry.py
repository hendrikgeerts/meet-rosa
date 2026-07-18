"""Tests voor _handle_job_failure retry-logica + iMessage-notify."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")


def _make_scheduler():
    """Build minimal Scheduler with mocked dependencies for testing
    just _handle_job_failure / _handle_job_success."""
    from core.scheduler import Scheduler
    # We poke directly at the methods — no need to call __init__ properly.
    s = Scheduler.__new__(Scheduler)
    s._retry_count = {}
    s._send = MagicMock()
    s._settings = MagicMock()
    s._settings.primary_handle = "test@x.nl"
    return s


def test_first_failure_returns_retry_window_5min() -> None:
    s = _make_scheduler()
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    result = s._handle_job_failure(
        job_name="briefing", label="Ochtendbriefing",
        failure=RuntimeError("network down"),
        next_normal_slot=next_normal,
    )
    delta_min = (result - datetime.now(TZ)).total_seconds() / 60
    assert 4 < delta_min < 6, f"expected ~5min retry, got {delta_min:.1f}min"
    assert s._retry_count["briefing"] == 1
    # Geen notificatie bij eerste fail
    s._send.assert_not_called()


def test_second_failure_returns_15min() -> None:
    s = _make_scheduler()
    s._retry_count = {"briefing": 1}
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    result = s._handle_job_failure(
        job_name="briefing", label="Ochtendbriefing",
        failure=RuntimeError("still down"),
        next_normal_slot=next_normal,
    )
    delta_min = (result - datetime.now(TZ)).total_seconds() / 60
    assert 14 < delta_min < 16
    assert s._retry_count["briefing"] == 2


def test_third_failure_returns_30min() -> None:
    s = _make_scheduler()
    s._retry_count = {"briefing": 2}
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    result = s._handle_job_failure(
        job_name="briefing", label="Ochtendbriefing",
        failure=RuntimeError("still down"),
        next_normal_slot=next_normal,
    )
    delta_min = (result - datetime.now(TZ)).total_seconds() / 60
    assert 29 < delta_min < 31
    assert s._retry_count["briefing"] == 3
    s._send.assert_not_called()


def test_fourth_failure_sends_notify_and_returns_normal_slot() -> None:
    s = _make_scheduler()
    s._retry_count = {"briefing": 3}  # al 3 retries gehad
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    result = s._handle_job_failure(
        job_name="briefing", label="Ochtendbriefing",
        failure=ConnectionError("dns failure"),
        next_normal_slot=next_normal,
    )
    assert result == next_normal  # gewone slot, geen retry
    assert s._retry_count["briefing"] == 0  # counter reset
    # Hendrik krijgt nu een iMessage
    s._send.assert_called_once()
    call_args = s._send.call_args
    body = call_args[0][1]
    assert "Ochtendbriefing" in body
    assert "ConnectionError" in body
    assert "dns failure" in body
    assert "3 pogingen" in body


def test_handle_job_success_resets_counter() -> None:
    s = _make_scheduler()
    s._retry_count = {"briefing": 2}
    s._handle_job_success("briefing")
    assert s._retry_count["briefing"] == 0


def test_failure_notify_failure_is_swallowed() -> None:
    """Als de iMessage-send zelf faalt, mag dat de scheduler niet crashen."""
    s = _make_scheduler()
    s._retry_count = {"briefing": 3}
    s._send.side_effect = RuntimeError("imessage daemon down")
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    # Should not raise
    result = s._handle_job_failure(
        job_name="briefing", label="Ochtendbriefing",
        failure=RuntimeError("primary fail"),
        next_normal_slot=next_normal,
    )
    assert result == next_normal
    assert s._retry_count["briefing"] == 0


def test_different_jobs_have_independent_counters() -> None:
    s = _make_scheduler()
    next_normal = datetime.now(TZ) + timedelta(hours=18)
    s._handle_job_failure(
        job_name="briefing", label="Briefing",
        failure=RuntimeError("x"), next_normal_slot=next_normal,
    )
    s._handle_job_failure(
        job_name="briefing", label="Briefing",
        failure=RuntimeError("x"), next_normal_slot=next_normal,
    )
    s._handle_job_failure(
        job_name="midday", label="Midday",
        failure=RuntimeError("y"), next_normal_slot=next_normal,
    )
    assert s._retry_count["briefing"] == 2
    assert s._retry_count["midday"] == 1
