"""Tests voor recurrence-handling in calendar tools.

`_build_recurrence` converteert het structured object dat Claude
meegeeft naar het list-of-RRULE-strings format dat Google Calendar
verwacht (RFC 5545).
"""
from __future__ import annotations

import pytest

from core.tools import _build_recurrence


# --- happy path ----------------------------------------------------------

def test_none_returns_none() -> None:
    assert _build_recurrence(None) is None


def test_empty_string_returns_none() -> None:
    assert _build_recurrence("") is None


def test_weekly_simple() -> None:
    """Elke week — geen extra qualifier."""
    out = _build_recurrence({"freq": "WEEKLY"})
    assert out == ["RRULE:FREQ=WEEKLY"]


def test_weekly_by_weekday() -> None:
    """'Elke maandag standup' — meest voorkomende patroon."""
    out = _build_recurrence({"freq": "WEEKLY", "by_weekday": ["MO"]})
    assert out == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


def test_weekdays_only() -> None:
    """Elke werkdag — vijf BYDAY-waarden."""
    out = _build_recurrence({
        "freq": "WEEKLY",
        "by_weekday": ["MO", "TU", "WE", "TH", "FR"],
    })
    assert out == ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]


def test_biweekly() -> None:
    """Elke 2 weken — INTERVAL=2."""
    out = _build_recurrence({"freq": "WEEKLY", "interval": 2})
    assert out == ["RRULE:FREQ=WEEKLY;INTERVAL=2"]


def test_interval_1_omitted() -> None:
    """INTERVAL=1 is default → niet expliciet emitten."""
    out = _build_recurrence({"freq": "WEEKLY", "interval": 1})
    assert out == ["RRULE:FREQ=WEEKLY"]


def test_monthly_by_month_day() -> None:
    """Iedere 15e van de maand."""
    out = _build_recurrence({"freq": "MONTHLY", "by_month_day": 15})
    assert out == ["RRULE:FREQ=MONTHLY;BYMONTHDAY=15"]


def test_with_count() -> None:
    """10 sessies — COUNT eindigt de serie."""
    out = _build_recurrence({
        "freq": "WEEKLY", "by_weekday": ["MO"], "count": 10,
    })
    assert out == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"]


def test_with_until_date_only() -> None:
    """Tot eind juni 2026 — UNTIL in UTC RFC 5545 format."""
    out = _build_recurrence({
        "freq": "WEEKLY", "by_weekday": ["MO"],
        "until": "2026-06-30",
    })
    # 2026-06-30 in Europe/Amsterdam (CEST) → UTC = 2026-06-29T22:00:00Z
    assert out is not None
    assert out[0].startswith("RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=")
    # Must end with Z (UTC)
    assert out[0].endswith("Z")


def test_combined_freq_interval_weekday_until() -> None:
    """Complex: elke 2 weken op MO,WE tot eind juni."""
    out = _build_recurrence({
        "freq": "WEEKLY", "interval": 2,
        "by_weekday": ["MO", "WE"],
        "until": "2026-06-30",
    })
    assert out is not None
    rrule = out[0]
    assert "FREQ=WEEKLY" in rrule
    assert "INTERVAL=2" in rrule
    assert "BYDAY=MO,WE" in rrule
    assert "UNTIL=" in rrule


# --- raw RRULE pass-through ---------------------------------------------

def test_raw_rrule_string_pass_through() -> None:
    out = _build_recurrence("RRULE:FREQ=DAILY;COUNT=5")
    assert out == ["RRULE:FREQ=DAILY;COUNT=5"]


def test_raw_string_lowercase_rrule_prefix_accepted() -> None:
    """Case-insensitive prefix check."""
    out = _build_recurrence("rrule:FREQ=DAILY")
    assert out == ["rrule:FREQ=DAILY"]


def test_raw_string_without_rrule_prefix_rejected() -> None:
    with pytest.raises(ValueError, match="RRULE:"):
        _build_recurrence("FREQ=DAILY;COUNT=5")


def test_list_of_rrules_pass_through() -> None:
    """Gevorderde gebruikers kunnen meerdere RRULE/RDATE/EXDATE doorgeven."""
    out = _build_recurrence([
        "RRULE:FREQ=WEEKLY;BYDAY=MO",
        "EXDATE;TZID=Europe/Amsterdam:20260615T090000",
    ])
    assert len(out) == 2


# --- validation errors ---------------------------------------------------

def test_missing_freq_rejected() -> None:
    with pytest.raises(ValueError, match="freq"):
        _build_recurrence({"by_weekday": ["MO"]})


def test_invalid_freq_rejected() -> None:
    with pytest.raises(ValueError, match="freq"):
        _build_recurrence({"freq": "HOURLY"})


def test_invalid_weekday_rejected() -> None:
    with pytest.raises(ValueError, match="by_weekday"):
        _build_recurrence({"freq": "WEEKLY", "by_weekday": ["FRI"]})


def test_interval_below_one_rejected() -> None:
    with pytest.raises(ValueError, match="interval"):
        _build_recurrence({"freq": "WEEKLY", "interval": 0})


def test_by_month_day_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="by_month_day"):
        _build_recurrence({"freq": "MONTHLY", "by_month_day": 32})


def test_count_and_until_together_rejected() -> None:
    """RFC 5545 verbiedt count én until tegelijk."""
    with pytest.raises(ValueError, match="count.*until|niet beide"):
        _build_recurrence({
            "freq": "WEEKLY", "count": 5, "until": "2026-06-30",
        })


def test_count_below_one_rejected() -> None:
    with pytest.raises(ValueError, match="count"):
        _build_recurrence({"freq": "WEEKLY", "count": 0})


def test_unsupported_type_rejected() -> None:
    with pytest.raises(ValueError, match="niet ondersteund"):
        _build_recurrence(42)  # type: ignore[arg-type]
