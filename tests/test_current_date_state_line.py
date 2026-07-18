"""Test voor _current_date_state_line — anker dat per chat-turn aan
Claude meegegeven wordt om hem niet naar zijn training-cutoff jaar
te laten defaulten.

Productie-bug die dit fixt: Hendrik vroeg 30 mei 2026 "wat is de
uptime van mei?" en Claude riep `uptime_report` aan met
start_date='2025-05-01' i.p.v. 2026 (training-cutoff jaar). Tool
returnde lege data, Claude rendererde "Perfecte maand! 🎉".
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import core.timezone as ctz
from main import _current_date_state_line


@pytest.fixture
def restore_now_local():
    """Save+restore module-level now_local zodat tests elkaars
    monkey-patch niet doorlekken."""
    original = ctz.now_local
    yield
    ctz.now_local = original


def test_state_line_contains_year_day_date_tz(restore_now_local) -> None:
    """De state-line moet jaar, dag, datum, en TZ-naam bevatten zodat
    Claude het correct kan parsen."""
    ctz.now_local = lambda: datetime(
        2026, 5, 30, 14, 0, tzinfo=ZoneInfo("Europe/Amsterdam"),
    )
    line = _current_date_state_line()

    assert "[TODAY]" in line
    assert "Saturday" in line       # 2026-05-30 was een zaterdag
    assert "May 2026" in line
    assert "Europe/Amsterdam" in line
    # Expliciete jaar-instructie tegen training-cutoff fallback
    assert "2026" in line
    assert "training-cutoff" in line


def test_state_line_changes_with_year(restore_now_local) -> None:
    """Volgend jaar moet de helper het jaar updaten — geen hardcoded
    waarde."""
    ctz.now_local = lambda: datetime(
        2027, 1, 15, 9, 0, tzinfo=ZoneInfo("Europe/Amsterdam"),
    )
    line = _current_date_state_line()
    assert "2027" in line
    assert "January" in line


def test_state_line_uses_active_timezone(restore_now_local) -> None:
    """Als Hendrik op reis in een andere TZ is, moet de state-line
    die TZ tonen — niet hardcoded Europe/Amsterdam."""
    ctz.now_local = lambda: datetime(
        2026, 5, 30, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    line = _current_date_state_line()
    assert "America/Los_Angeles" in line


def test_state_line_explicit_year_instruction(restore_now_local) -> None:
    """De instructie moet expliciet zijn over WAAR Claude het jaartal
    vandaan moet halen — anders blijft hij toch defaulten."""
    ctz.now_local = lambda: datetime(
        2026, 5, 30, 14, 0, tzinfo=ZoneInfo("Europe/Amsterdam"),
    )
    line = _current_date_state_line()
    # Moet expliciet 'default to current year' zeggen
    assert "default to" in line.lower() or "default" in line.lower()
    # En specifiek over years zonder jaartal
    assert "without a year" in line or "zonder jaartal" in line.lower()
