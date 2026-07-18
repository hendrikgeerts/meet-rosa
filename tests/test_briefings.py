"""Tests voor core.briefings.next_fire_time — weekday/weekend split."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.briefings import next_fire_time, next_fire_time_with_catchup

TZ = ZoneInfo("Europe/Amsterdam")
WEEKDAY = "07:00"
WEEKEND = "08:30"


def _at(year: int, month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=TZ)


# Reference week: 2026-04-20 = ma, 21 = di, …, 24 = vr, 25 = za, 26 = zo, 27 = ma

def test_weekday_morning_before_target_returns_today() -> None:
    now = _at(2026, 4, 22, 6, 30)  # woensdag 06:30
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 22, 7, 0)


def test_weekday_after_target_returns_tomorrow_weekday_time() -> None:
    now = _at(2026, 4, 22, 9, 0)   # woensdag 09:00, voorbij 07:00
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 23, 7, 0)  # donderdag 07:00


def test_friday_after_target_jumps_to_saturday_weekend_time() -> None:
    now = _at(2026, 4, 24, 9, 0)   # vrijdag 09:00, voorbij 07:00
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 25, 8, 30)  # zaterdag 08:30


def test_saturday_before_weekend_target_returns_today() -> None:
    now = _at(2026, 4, 25, 7, 0)   # zaterdag 07:00, vóór 08:30
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 25, 8, 30)


def test_saturday_after_target_returns_sunday_weekend_time() -> None:
    now = _at(2026, 4, 25, 9, 0)   # zaterdag 09:00, voorbij 08:30
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 26, 8, 30)


def test_sunday_after_target_returns_monday_weekday_time() -> None:
    now = _at(2026, 4, 26, 10, 0)  # zondag 10:00, voorbij 08:30
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire == _at(2026, 4, 27, 7, 0)


def test_exact_target_is_treated_as_already_past() -> None:
    """Op de seconde van target → schuif door naar morgen (anders zou de
    scheduler oneindig vuren)."""
    now = _at(2026, 4, 22, 7, 0)   # exact 07:00
    fire = next_fire_time(now, WEEKDAY, WEEKEND)
    assert fire > now
    assert fire == _at(2026, 4, 23, 7, 0)


@pytest.mark.parametrize("weekday_t, weekend_t", [
    ("07:00", "08:30"),
    ("06:15", "10:45"),
    ("23:30", "23:59"),
])
def test_returned_time_always_in_future(weekday_t: str, weekend_t: str) -> None:
    """Voor een hele week 'now'-momenten: next_fire is altijd > now."""
    base = _at(2026, 4, 20, 0, 0)  # ma 00:00
    for day_offset in range(7):
        for hour in (0, 6, 12, 18, 23):
            now = base.replace(day=20 + day_offset, hour=hour)
            fire = next_fire_time(now, weekday_t, weekend_t)
            assert fire > now


# --- catch-up variant ------------------------------------------------------

def test_catchup_within_grace_returns_now() -> None:
    """Daemon herstart 7:04, target was 7:00, niet eerder gefired vandaag.
    Binnen 2u-grace → fire direct."""
    now = _at(2026, 4, 29, 7, 4)
    fire = next_fire_time_with_catchup(
        now, WEEKDAY, WEEKEND, last_fired=None,
    )
    assert fire == now


def test_catchup_outside_grace_skips_to_next_slot() -> None:
    """Te ver na de target (>2u): skip vandaag, fire morgen."""
    now = _at(2026, 4, 29, 12, 0)  # 5u na 7:00
    fire = next_fire_time_with_catchup(
        now, WEEKDAY, WEEKEND, last_fired=None,
    )
    assert fire == _at(2026, 4, 30, 7, 0)


def test_catchup_already_fired_today_returns_next_slot() -> None:
    """Briefing is vandaag al gefired (last_fired >= today_target):
    geen dubbele fire."""
    now = _at(2026, 4, 29, 7, 30)
    last = _at(2026, 4, 29, 7, 0)
    fire = next_fire_time_with_catchup(
        now, WEEKDAY, WEEKEND, last_fired=last,
    )
    assert fire == _at(2026, 4, 30, 7, 0)


def test_catchup_future_target_returns_today_target() -> None:
    """Voor 7:00: gewoon today's target, geen catch-up gedrag."""
    now = _at(2026, 4, 29, 6, 30)
    fire = next_fire_time_with_catchup(
        now, WEEKDAY, WEEKEND, last_fired=None,
    )
    assert fire == _at(2026, 4, 29, 7, 0)


def test_catchup_custom_grace() -> None:
    """Grace=10min: 7:15 met target 7:00 → buiten grace → next slot."""
    now = _at(2026, 4, 29, 7, 15)
    fire = next_fire_time_with_catchup(
        now, WEEKDAY, WEEKEND, last_fired=None,
        grace=timedelta(minutes=10),
    )
    assert fire == _at(2026, 4, 30, 7, 0)


# --- v2: travel_today snapshot in briefing context ---------------------

def test_compute_today_travels_home_fallback(tmp_path) -> None:
    """Geen PA-LOC + home-coords gegeven → travel_today bevat items
    voor fysieke meetings van vandaag met from_home=True. Online
    meetings worden geskipt."""
    from datetime import datetime as _dt
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo as _ZI

    from core.briefings import _compute_today_travels
    from extensions.travel_alerts.schema import init_travel_alerts_schema
    from integrations.here_maps import RouteSummary

    db = tmp_path / "b.db"
    init_travel_alerts_schema(db)
    now = _dt.now(_ZI("Europe/Amsterdam"))
    fake_here = MagicMock()
    fake_here.car_travel_time.return_value = RouteSummary(
        duration_seconds=900, base_duration_seconds=720,
        distance_meters=12000,
    )

    events = [
        {
            "id": "e1", "title": "Klant Rotterdam",
            "start": (now + timedelta(hours=2)).isoformat(),
            "end": (now + timedelta(hours=3)).isoformat(),
            "location": "51.92, 4.48",
        },
        {
            "id": "e2", "title": "Standup (zoom)",
            "start": (now + timedelta(hours=1)).isoformat(),
            "location": "https://zoom.us/j/123",
        },
    ]
    out = _compute_today_travels(
        here=fake_here, db_path=db, events=events,
        home_lat=51.5407, home_lon=4.9358,
        buffer_minutes=5, now=now,
    )
    assert len(out) == 1   # online meeting skipped
    assert out[0]["title"] == "Klant Rotterdam"
    assert out[0]["travel_min"] == 15
    assert out[0]["delay_min"] == 3
    assert out[0]["from_home"] is True


def test_compute_today_travels_returns_empty_without_origin(tmp_path) -> None:
    """Geen PA-LOC + geen home → leeg."""
    from datetime import datetime as _dt
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo as _ZI

    from core.briefings import _compute_today_travels
    from extensions.travel_alerts.schema import init_travel_alerts_schema

    db = tmp_path / "b.db"
    init_travel_alerts_schema(db)
    now = _dt.now(_ZI("Europe/Amsterdam"))
    events = [{"id": "e1", "title": "x", "start": now.isoformat(),
                "location": "52.0, 4.0"}]
    out = _compute_today_travels(
        here=MagicMock(), db_path=db, events=events,
        home_lat=None, home_lon=None, buffer_minutes=5, now=now,
    )
    assert out == []
