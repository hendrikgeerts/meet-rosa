"""Tests voor extensions.travel_alerts — schema-CRUD, parser, en de
end-to-end check-tick met fake HERE-client + fake calendar."""
from __future__ import annotations

import sqlite3
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from extensions.travel_alerts.check import _parse_destination, tick
from extensions.travel_alerts.parser import (
    SUBJECT_PREFIX,
    is_location_message,
    parse_location_body,
)
from extensions.travel_alerts.schema import (
    alert_already_sent,
    geocode_cache_get,
    geocode_cache_set,
    init_travel_alerts_schema,
    insert_location,
    last_alert_duration,
    latest_location,
    mark_alert_sent,
    prune_old_locations,
)
from integrations.here_maps import RouteSummary

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "travel.db"
    init_travel_alerts_schema(p)
    return p


# --- parser ----------------------------------------------------------------

def test_is_location_message_detects_subject_prefix() -> None:
    assert is_location_message(f"{SUBJECT_PREFIX} update")
    assert is_location_message("Re: [PA-LOC] iPhone")  # any position
    assert not is_location_message("Re: project meeting")
    assert not is_location_message(None)


def test_parse_location_body_full() -> None:
    body = """PA-LOCATION
    lat: 52.3702
    lon: 4.8952
    acc: 12
    """
    assert parse_location_body(body) == (52.3702, 4.8952, 12.0)


def test_parse_location_body_alt_keys_and_order() -> None:
    body = "longitude=4.85\nlatitude=52.37\n"
    assert parse_location_body(body) == (52.37, 4.85, None)


def test_parse_location_rejects_out_of_range() -> None:
    body = "lat: 999\nlon: 200\n"
    assert parse_location_body(body) is None


def test_parse_location_returns_none_for_garbage() -> None:
    assert parse_location_body("hello world") is None
    assert parse_location_body("") is None


# --- v2: alternative formats (iOS Mail 'Current Location' renderings) ---

def test_parse_location_apple_maps_url_ll_param() -> None:
    """iOS Mail renders 'Current Location' as a maps.apple.com link in
    the plain-text body of the email."""
    body = "Sent from my iPhone\nhttps://maps.apple.com/?ll=52.3702,4.8952"
    assert parse_location_body(body) == (52.3702, 4.8952, None)


def test_parse_location_apple_maps_url_with_q_and_ll() -> None:
    body = "Check this out: https://maps.apple.com/?q=Loc&ll=51.5407,4.9358&z=15"
    assert parse_location_body(body) == (51.5407, 4.9358, None)


def test_parse_location_degree_notation() -> None:
    """Some Shortcut variable renderings give 'X° N, Y° E' format."""
    body = "Currently at 52.3702° N, 4.8952° E somewhere"
    assert parse_location_body(body) == (52.3702, 4.8952, None)


def test_parse_location_degree_notation_southern_western() -> None:
    """S = negative lat; W = negative lon."""
    body = "33.8688° S, 70.6693° W"
    out = parse_location_body(body)
    assert out is not None
    lat, lon, _ = out
    assert lat == -33.8688
    assert lon == -70.6693


def test_parse_location_latitude_longitude_keys_anywhere_in_line() -> None:
    """iOS Mail soms: 'Latitude: 52.3702 Longitude: 4.8952' op één regel."""
    body = "Location info — Latitude: 52.3702 Longitude: 4.8952 (approx)"
    assert parse_location_body(body) == (52.3702, 4.8952, None)


def test_parse_location_geo_uri() -> None:
    body = "Open in maps: geo:52.3702,4.8952"
    assert parse_location_body(body) == (52.3702, 4.8952, None)


def test_parse_location_maps_url_preferred_over_lat_lon_keys() -> None:
    """Als beide aanwezig: maps URL is meer betrouwbaar dan tekst-parsing."""
    body = (
        "https://maps.apple.com/?ll=52.3702,4.8952\n"
        "lat: 99\n"  # absurde tekst — moet genegeerd worden
        "lon: 99\n"
    )
    assert parse_location_body(body) == (52.3702, 4.8952, None)


def test_parse_location_with_accuracy_in_alternative_format() -> None:
    """Accuracy hint mag overal in de body staan."""
    body = (
        "https://maps.apple.com/?ll=52.3702,4.8952\n"
        "acc: 25\n"
    )
    out = parse_location_body(body)
    assert out == (52.3702, 4.8952, 25.0)


# --- schema ----------------------------------------------------------------

def test_insert_and_latest_location(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.0, lon=4.0)
        _time.sleep(0.01)
        insert_location(c, lat=53.0, lon=5.0)
        loc = latest_location(c)
    assert loc is not None
    assert loc["lat"] == 53.0


def test_latest_location_respects_max_age(db: Path) -> None:
    old = int(_time.time()) - 7200  # 2u oud
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.0, lon=4.0, received_at=old)
        recent_loc = latest_location(c, max_age_seconds=600)  # 10 min cap
    assert recent_loc is None


# --- MEDIUM-2: throttle + retention -------------------------------------

def test_insert_location_throttles_within_interval(db: Path) -> None:
    """1-row-per-hour cap: second insert within the window is skipped."""
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        rid1 = insert_location(
            c, lat=52.0, lon=4.0, received_at=now,
            min_interval_seconds=3600,
        )
        rid2 = insert_location(
            c, lat=52.1, lon=4.1, received_at=now + 60,
            min_interval_seconds=3600,
        )
    assert rid1 > 0
    assert rid2 == 0  # skipped
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT COUNT(*) FROM current_location").fetchone()
    assert rows[0] == 1


def test_insert_location_allows_after_interval(db: Path) -> None:
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        insert_location(
            c, lat=52.0, lon=4.0, received_at=now - 7200,
            min_interval_seconds=3600,
        )
        rid2 = insert_location(
            c, lat=52.1, lon=4.1, received_at=now,
            min_interval_seconds=3600,
        )
    assert rid2 > 0


def test_insert_location_no_throttle_by_default(db: Path) -> None:
    """Backwards-compat: callers that don't pass min_interval still insert."""
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.0, lon=4.0)
        insert_location(c, lat=52.1, lon=4.1)
        cnt = c.execute("SELECT COUNT(*) FROM current_location").fetchone()[0]
    assert cnt == 2


def test_prune_old_locations_drops_rows_past_retention(db: Path) -> None:
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.0, lon=4.0, received_at=now - 30 * 86400)  # 30d
        insert_location(c, lat=52.1, lon=4.1, received_at=now - 10 * 86400)  # 10d
        insert_location(c, lat=52.2, lon=4.2, received_at=now - 1 * 86400)   # 1d
        removed = prune_old_locations(c, days=7)
        kept = c.execute(
            "SELECT received_at FROM current_location ORDER BY received_at"
        ).fetchall()
    assert removed == 2  # 30d + 10d
    assert len(kept) == 1  # only 1d row remains


def test_prune_old_locations_no_op_when_disabled(db: Path) -> None:
    """days=0 disables prune (defensive — caller misconfig shouldn't wipe)."""
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.0, lon=4.0)
        removed = prune_old_locations(c, days=0)
        cnt = c.execute("SELECT COUNT(*) FROM current_location").fetchone()[0]
    assert removed == 0
    assert cnt == 1


# --- v2: geocode-cache --------------------------------------------------

def test_geocode_cache_miss_then_hit(db: Path) -> None:
    with sqlite3.connect(db) as c:
        assert geocode_cache_get(c, address="Kantoor Rotterdam") is None
        geocode_cache_set(c, address="Kantoor Rotterdam", coords=(51.92, 4.48))
        out = geocode_cache_get(c, address="Kantoor Rotterdam")
    assert out == (51.92, 4.48)


def test_geocode_cache_normalises_key(db: Path) -> None:
    """Address normalisation: strip + lowercase so 'Foo ' == 'foo'."""
    with sqlite3.connect(db) as c:
        geocode_cache_set(c, address="  Brussel Centraal  ", coords=(50.85, 4.36))
        assert geocode_cache_get(c, address="brussel centraal") == (50.85, 4.36)


def test_geocode_cache_negative_entry_returns_none(db: Path) -> None:
    """Negative-cache: store None so we don't re-query immediately."""
    with sqlite3.connect(db) as c:
        geocode_cache_set(c, address="nonexistent place xyz", coords=None)
        # Re-query returns None (= cache hit, but no coords).
        assert geocode_cache_get(c, address="nonexistent place xyz") is None
        # And the row IS in the table:
        n = c.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0]
    assert n == 1


def test_geocode_cache_max_age_expires(db: Path) -> None:
    with sqlite3.connect(db) as c:
        # Insert with a fake old geocoded_at via direct SQL
        c.execute(
            "INSERT INTO geocode_cache (addr_key, lat, lon, geocoded_at) "
            "VALUES (?, ?, ?, strftime('%s','now') - 100*86400)",
            ("oude-adres", 50.0, 4.0),
        )
        # max_age=90 dagen → treat as miss
        assert geocode_cache_get(c, address="oude-adres", max_age_seconds=90 * 86400) is None
        # without max_age → still returns
        assert geocode_cache_get(c, address="oude-adres") == (50.0, 4.0)


def test_geocode_cache_upsert_overwrites(db: Path) -> None:
    with sqlite3.connect(db) as c:
        geocode_cache_set(c, address="kantoor", coords=(52.0, 4.0))
        geocode_cache_set(c, address="kantoor", coords=(52.1, 4.1))
        out = geocode_cache_get(c, address="kantoor")
    assert out == (52.1, 4.1)


def test_alert_dedup_via_unique_constraint(db: Path) -> None:
    with sqlite3.connect(db) as c:
        first = mark_alert_sent(c, event_id="ev1", alert_kind="plan")
        second = mark_alert_sent(c, event_id="ev1", alert_kind="plan")
    assert first is True
    assert second is False
    with sqlite3.connect(db) as c:
        assert alert_already_sent(c, event_id="ev1", alert_kind="plan")
        assert not alert_already_sent(c, event_id="ev1", alert_kind="leave_now")


# --- destination parsing ---------------------------------------------------

def test_parse_destination_extracts_embedded_latlon() -> None:
    assert _parse_destination("Klantadres 52.3702, 4.8952") == (52.3702, 4.8952)


def test_parse_destination_returns_none_for_bare_address() -> None:
    assert _parse_destination("Frederiksplein 42, Amsterdam") is None


# --- tick (end-to-end met fakes) ------------------------------------------

@dataclass
class _FakeHere:
    duration_seconds: int = 600
    base_seconds: int = 480
    distance_m: int = 5000
    geocode_result: tuple[float, float] | None = None
    # v2: multi-modal — fake returns proportional times per mode
    bike_duration_seconds: int | None = None       # default = car × 1.5
    pedestrian_duration_seconds: int | None = None  # default = car × 4

    def car_travel_time(self, **kwargs: Any) -> RouteSummary | None:
        return self.travel_time(mode="car", **kwargs)

    def travel_time(self, *, mode: str = "car", **kwargs: Any) -> RouteSummary | None:
        if mode == "bicycle":
            dur = self.bike_duration_seconds
            if dur is None:
                dur = int(self.duration_seconds * 1.5)
            return RouteSummary(
                duration_seconds=dur, base_duration_seconds=dur,
                distance_meters=self.distance_m, transport_mode="bicycle",
            )
        if mode == "pedestrian":
            dur = self.pedestrian_duration_seconds
            if dur is None:
                dur = int(self.duration_seconds * 4)
            return RouteSummary(
                duration_seconds=dur, base_duration_seconds=dur,
                distance_meters=self.distance_m, transport_mode="pedestrian",
            )
        return RouteSummary(
            duration_seconds=self.duration_seconds,
            base_duration_seconds=self.base_seconds,
            distance_meters=self.distance_m,
            transport_mode="car",
        )

    def geocode(self, address: str) -> tuple[float, float] | None:
        return self.geocode_result


def _make_event(start: datetime, location: str = "52.0, 4.5", title: str = "Klant") -> dict[str, Any]:
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        "location": location,
    }


def test_tick_sends_leave_now_when_at_threshold(db: Path) -> None:
    """Event start over 10 min, reistijd 10 min, buffer 5 min → leave_by
    is 5 min vóór nu = al voorbij. Met buffer pakt 'leave_now' bij minutes_to_leave
    in [0, 1]. Zet event_start zo dat dat klopt."""
    now = datetime.now(TZ)
    # leave_by = start - travel - buffer ≈ now → start = now + travel + buffer.
    # Voeg 30s toe zodat minutes_to_leave nog 0 is (niet -1) als de test-
    # uitvoer een paar seconden duurt.
    start = now + timedelta(minutes=10 + 5, seconds=30)

    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent_calls = []
    def send(handle: str, body: str) -> None:
        sent_calls.append((handle, body))

    here = _FakeHere(duration_seconds=600, base_seconds=480)  # 10 min met traffic
    n = tick(
        db_path=db, calendar=cal, here=here,
        send_imessage=send, primary_handle="+316",
        plan_minutes=30,
    )
    assert n == 1
    assert "Leave now" in sent_calls[0][1]


def test_tick_sends_plan_when_well_before_leave_by(db: Path) -> None:
    """Reistijd 10 min, buffer 5, plan_minutes 30. Event start 50 min ⇒
    leave_by over ~35 min ⇒ minutes_to_leave > plan_minutes (30) → géén
    plan-alert. Zet start dichterbij: 40 min ⇒ leave_by over 25 min →
    binnen plan window."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent_calls = []
    n = tick(
        db_path=db, calendar=cal, here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent_calls.append((h, b)),
        primary_handle="+316", plan_minutes=30,
    )
    assert n == 1
    assert "Plan to leave" in sent_calls[0][1]


def test_tick_dedups_within_same_kind(db: Path) -> None:
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent: list[Any] = []
    args = dict(
        db_path=db, calendar=cal, here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
    )
    assert tick(**args) == 1
    assert tick(**args) == 0   # second call: already sent


def test_tick_skips_online_meetings(db: Path) -> None:
    """Online-meeting locaties moeten geen HERE-call genereren — privacy-respect."""
    now = datetime.now(TZ)
    cal = MagicMock()
    cal.list_events.return_value = [
        _make_event(now + timedelta(minutes=20),
                    location="https://teams.microsoft.com/l/meetup-join/abc"),
        _make_event(now + timedelta(minutes=25),
                    location="Zoom — link in description", title="2"),
        _make_event(now + timedelta(minutes=30),
                    location="online", title="3"),
    ]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    here = MagicMock()
    n = tick(
        db_path=db, calendar=cal, here=here,
        send_imessage=lambda h, b: None, primary_handle="+316",
    )
    assert n == 0
    here.car_travel_time.assert_not_called()


def test_tick_skips_events_without_location(db: Path) -> None:
    now = datetime.now(TZ)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(now + timedelta(minutes=20), location="")]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    n = tick(
        db_path=db, calendar=cal, here=_FakeHere(),
        send_imessage=lambda h, b: None, primary_handle="+316",
    )
    assert n == 0


def test_tick_skips_when_no_recent_location(db: Path) -> None:
    """Geen location in DB → geen call naar HERE, geen alerts."""
    now = datetime.now(TZ)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(now + timedelta(minutes=20))]
    here = MagicMock()
    n = tick(
        db_path=db, calendar=cal, here=here,
        send_imessage=lambda h, b: None, primary_handle="+316",
    )
    assert n == 0
    here.car_travel_time.assert_not_called()


def test_tick_uses_geocoder_for_text_address(db: Path) -> None:
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    ev = _make_event(start, location="Frederiksplein 42, Amsterdam")
    cal = MagicMock(); cal.list_events.return_value = [ev]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    here = _FakeHere(geocode_result=(52.36, 4.91))
    n = tick(
        db_path=db, calendar=cal, here=here,
        send_imessage=lambda h, b: None, primary_handle="+316",
    )
    assert n == 1  # geocoded ⇒ route ⇒ alert


# --- v2: home-fallback origin -------------------------------------------

def test_tick_uses_home_fallback_when_no_recent_location(db: Path) -> None:
    """Geen PA-LOC + home_lat/lon geconfigureerd → alert wordt
    verstuurd met de home-origin marker."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    sent: list[str] = []
    n = tick(
        db_path=db, calendar=cal,
        here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
        home_lat=51.5407, home_lon=4.9358,
    )
    assert n == 1
    # Plan-alert vermeldt "(from home)" zodat Hendrik weet dat de
    # berekening uit huis komt, niet uit zijn werkelijke locatie.
    assert "(from home)" in sent[0]


def test_tick_prefers_real_location_over_home(db: Path) -> None:
    """Bij recente PA-LOC: gebruik die, niet home-fallback."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    sent: list[str] = []
    n = tick(
        db_path=db, calendar=cal,
        here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
        home_lat=51.5407, home_lon=4.9358,
    )
    assert n == 1
    assert "(from home)" not in sent[0]  # echte locatie gebruikt


def test_tick_includes_apple_maps_link(db: Path) -> None:
    """Plan/leave_now/late alerts moeten een maps.apple.com link bevatten
    zodat één tap in iMessage de routebeschrijving opent."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [
        _make_event(start, location="52.36, 4.91")
    ]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    sent: list[str] = []
    tick(
        db_path=db, calendar=cal,
        here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
    )
    assert any("maps.apple.com" in s for s in sent)
    assert any("daddr=52.36000,4.91000" in s for s in sent)
    assert any("dirflg=d" in s for s in sent)


def test_tick_skips_when_no_location_and_no_home(db: Path) -> None:
    """Geen PA-LOC én geen home_lat → niets verstuurd."""
    now = datetime.now(TZ)
    cal = MagicMock(); cal.list_events.return_value = [
        _make_event(now + timedelta(minutes=20))
    ]
    here = MagicMock()
    n = tick(
        db_path=db, calendar=cal, here=here,
        send_imessage=lambda h, b: None, primary_handle="+316",
        home_lat=None, home_lon=None,
    )
    assert n == 0
    here.car_travel_time.assert_not_called()


# --- v2: traffic-update re-alert ---------------------------------------

def test_mark_alert_stores_duration_seconds(db: Path) -> None:
    with sqlite3.connect(db) as c:
        mark_alert_sent(c, event_id="e1", alert_kind="plan",
                        duration_seconds=900)
        out = last_alert_duration(c, event_id="e1", alert_kind="plan")
    assert out == 900


def test_last_alert_duration_returns_none_when_not_sent(db: Path) -> None:
    with sqlite3.connect(db) as c:
        assert last_alert_duration(c, event_id="missing", alert_kind="plan") is None


def test_tick_sends_traffic_update_on_significant_change(db: Path) -> None:
    """Plan al verstuurd met duration=600s (10 min). Tweede tick met
    duration=1500s (25 min) = +15 min delta → traffic_update."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent: list[str] = []
    # Eerste tick: 10-min reistijd → plan-alert
    tick(
        db_path=db, calendar=cal,
        here=_FakeHere(duration_seconds=600, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
    )
    assert len(sent) == 1
    assert "Plan to leave" in sent[0]

    # Tweede tick: 25-min reistijd (file ineens) → traffic_update
    n2 = tick(
        db_path=db, calendar=cal,
        here=_FakeHere(duration_seconds=1500, base_seconds=480),
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316", plan_minutes=30,
        traffic_update_threshold_seconds=600,
    )
    assert n2 == 1
    assert len(sent) == 2
    assert "Traffic update" in sent[1]
    assert "+15 extra" in sent[1] or "+15" in sent[1]


def test_tick_does_not_repeat_traffic_update(db: Path) -> None:
    """Max 1 traffic_update per event (UNIQUE constraint)."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent: list[str] = []
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=600, base_seconds=480),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30)
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=1500, base_seconds=480),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30,
         traffic_update_threshold_seconds=600)
    # Derde tick: nog meer file (1800s). Mag geen tweede traffic_update.
    n3 = tick(db_path=db, calendar=cal,
              here=_FakeHere(duration_seconds=1800, base_seconds=480),
              send_imessage=lambda h, b: sent.append(b),
              primary_handle="+316", plan_minutes=30,
              traffic_update_threshold_seconds=600)
    assert n3 == 0
    assert sum("Traffic update" in s for s in sent) == 1


def test_tick_includes_bike_alternative_for_short_distance(db: Path) -> None:
    """Distance 4km, car 12 min, bike 14 min → fiets binnen +5 min → meld."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    sent: list[str] = []
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=720, base_seconds=720,
                         distance_m=4000, bike_duration_seconds=840),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30)
    assert len(sent) == 1
    assert "bike" in sent[0].lower()


def test_tick_skips_bike_when_distance_too_far(db: Path) -> None:
    """Distance 15km → geen bike-call (te ver), geen bike-hint."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    sent: list[str] = []
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=720, base_seconds=720, distance_m=15000),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30)
    assert len(sent) == 1
    assert "bike" not in sent[0].lower()


def test_tick_skips_bike_when_too_slow(db: Path) -> None:
    """Distance 4km, car 10 min, bike 25 min → bike te traag → geen hint."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)
    sent: list[str] = []
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=600, base_seconds=600,
                         distance_m=4000, bike_duration_seconds=1500),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30)
    assert "bike" not in sent[0].lower()


def test_tick_no_traffic_update_when_change_below_threshold(db: Path) -> None:
    """+5 min delta met threshold 600s (10 min) → géén update."""
    now = datetime.now(TZ)
    start = now + timedelta(minutes=40)
    cal = MagicMock(); cal.list_events.return_value = [_make_event(start)]
    with sqlite3.connect(db) as c:
        insert_location(c, lat=52.5, lon=4.5)

    sent: list[str] = []
    tick(db_path=db, calendar=cal,
         here=_FakeHere(duration_seconds=600, base_seconds=480),
         send_imessage=lambda h, b: sent.append(b),
         primary_handle="+316", plan_minutes=30)
    # +5 min = 300s, onder threshold 600s
    n2 = tick(db_path=db, calendar=cal,
              here=_FakeHere(duration_seconds=900, base_seconds=480),
              send_imessage=lambda h, b: sent.append(b),
              primary_handle="+316", plan_minutes=30,
              traffic_update_threshold_seconds=600)
    assert n2 == 0
    assert sum("Traffic update" in s for s in sent) == 0
