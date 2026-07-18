"""Tests voor de daily English-practice reminder + scheduler-hook."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from extensions.english_practice.reminder import generate_english_reminder
from extensions.english_practice.schema import (
    init_english_practice_schema, insert_card, set_active_card,
)

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_english_practice_schema(p)
    return p


# --- reminder text generator --------------------------------------------

def test_reminder_returns_none_when_schema_missing(tmp_path: Path) -> None:
    assert generate_english_reminder(tmp_path / "nonexistent.db") is None


def test_reminder_returns_none_when_no_cards_due(db_path: Path) -> None:
    assert generate_english_reminder(db_path) is None


def test_reminder_singular_when_one_card_due(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        insert_card(conn, collocation="fierce competition")
    text = generate_english_reminder(db_path)
    assert text is not None
    assert "1 English collocation" in text
    assert "practice" in text.lower()


def test_reminder_plural_when_many_cards_due(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        for col in ["fierce competition", "market share", "narrow margins"]:
            insert_card(conn, collocation=col)
    text = generate_english_reminder(db_path)
    assert text is not None
    assert "3 English collocations" in text


def test_reminder_mentions_active_card_when_session_open(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        rid = insert_card(conn, collocation="fierce competition")
        assert rid is not None
        # Make the card non-due so we know the reminder isn't fired by due-count
        conn.execute(
            "UPDATE english_cards SET next_due_at=strftime('%s','now','+10 days') "
            "WHERE id=?", (rid,),
        )
        set_active_card(conn, rid)
    text = generate_english_reminder(db_path)
    assert text is not None
    assert "fierce competition" in text
    assert "open English card" in text


# --- scheduler hook -----------------------------------------------------

def _make_scheduler():
    """Build minimal Scheduler with mocked deps for testing
    just _maybe_send_english_reminder."""
    from core.scheduler import Scheduler
    s = Scheduler.__new__(Scheduler)
    s._retry_count = {}
    s._send = MagicMock()
    s._settings = MagicMock()
    s._settings.primary_handle = "test@x.nl"
    s._settings.english_practice_enabled = True
    s._settings.english_practice_time = "09:00"
    s._settings.english_practice_weekend_time = "10:00"
    s._settings.english_practice_skip_weekend = True
    s._settings.db_path = Path("/nonexistent.db")
    # Set next slot to a value far in the past so the "is it time?" guard
    # always lets the test's `now` pass through. Per-test code can still
    # override this to test the "not yet" branch.
    s._next_english_practice = datetime(2020, 1, 1, tzinfo=TZ)
    s._record_fired = MagicMock()
    return s


def test_scheduler_hook_skips_when_disabled() -> None:
    s = _make_scheduler()
    s._settings.english_practice_enabled = False
    s._maybe_send_english_reminder(datetime.now(TZ))
    s._send.assert_not_called()


def test_scheduler_hook_skips_when_next_in_future() -> None:
    s = _make_scheduler()
    s._next_english_practice = datetime.now(TZ) + timedelta(hours=1)
    s._maybe_send_english_reminder(datetime.now(TZ))
    s._send.assert_not_called()


def test_scheduler_hook_skips_when_reminder_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_scheduler()
    # Force "today" to be a weekday so skip-weekend doesn't intercept.
    monday_9am = datetime(2026, 5, 18, 9, 0, tzinfo=TZ)  # 2026-05-18 = Mon
    monkeypatch.setattr(
        "core.scheduler.generate_english_reminder",
        lambda _p: None,
    )
    s._maybe_send_english_reminder(monday_9am)
    s._send.assert_not_called()
    # Still bumped forward
    assert s._next_english_practice > monday_9am


def test_scheduler_hook_sends_when_due_text_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_scheduler()
    monday_9am = datetime(2026, 5, 18, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "core.scheduler.generate_english_reminder",
        lambda _p: "5 English collocations are due. Reply 'practice' to start.",
    )
    s._maybe_send_english_reminder(monday_9am)
    s._send.assert_called_once()
    assert "practice" in s._send.call_args[0][1].lower()
    s._record_fired.assert_called_once()


def test_scheduler_hook_skips_weekend_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_scheduler()
    saturday_9am = datetime(2026, 5, 16, 9, 0, tzinfo=TZ)  # 2026-05-16 = Sat
    called = {"n": 0}

    def _spy(_p):
        called["n"] += 1
        return "5 due"
    monkeypatch.setattr("core.scheduler.generate_english_reminder", _spy)

    s._maybe_send_english_reminder(saturday_9am)
    s._send.assert_not_called()
    # Should not even generate text on weekend with skip_weekend=true
    assert called["n"] == 0
    # next should be a future weekday
    assert s._next_english_practice.weekday() < 5
