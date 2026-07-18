"""Travel-alert tick: kijk naar komende calendar-events met locatie,
bereken travel-time vanaf laatste phone-locatie, stuur iMessage als de
'vertrek-tijd' nadert.

Drie alert-momenten per event:
- 'plan'      — `alert_minutes_before_leave` min vóór leave_by:
                "Plan vertrek over X min, reistijd Y (file +Z)"
- 'leave_now' — bij leave_by ± 1 min: "Vertrek nu"
- 'late'     — > leave_by + 5 min en agent denkt dat je nog niet weg bent:
                "Loop achter — over X min te laat"

Per (event_id, alert_kind) maximaal 1 bericht via travel_alerts_sent.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.travel_alerts.schema import (
    alert_already_sent,
    last_alert_duration,
    latest_location,
    mark_alert_sent,
)
from integrations.gcal import CalendarClient
from integrations.here_maps import HereMapsClient, RouteSummary

log = logging.getLogger(__name__)
from core.timezone import current_tz, now_local

TZ = ZoneInfo("Europe/Amsterdam")

# Lat/lon-pair in een event.location als die aanwezig is. Als de location
# een vrije-tekst-adres is, gebruiken we die direct als HERE-string (later
# geocoding-ondersteuning).
_LATLON_RE = re.compile(r"(-?\d+\.\d+)[,\s]+(-?\d+\.\d+)")

# Locatie-strings die wijzen op een online meeting — daar is geen reistijd
# nodig en we willen geen onnodige HERE-call (= privacy-respectvol).
_ONLINE_MEETING_PATTERNS = re.compile(
    r"(?i)\b("
    r"zoom\.us|teams\.microsoft|meet\.google|webex\.com|whereby\.com|"
    r"gotomeeting|bluejeans|jitsi|hangouts|skype|"
    r"online|virtueel|virtual|remote|teams|zoom|meet link|meeting link|"
    r"https?://"
    r")\b"
)


def _is_online_meeting(location_str: str) -> bool:
    """True als de locatie zo te zien online is (geen fysieke reis)."""
    return bool(location_str) and bool(_ONLINE_MEETING_PATTERNS.search(location_str))


def tick(
    *,
    db_path: Path,
    calendar: CalendarClient,
    here: HereMapsClient,
    send_imessage: Callable[[str, str], None],
    primary_handle: str,
    horizon_minutes: int = 120,
    plan_minutes: int = 30,
    buffer_minutes: int = 5,
    location_max_age_seconds: int = 7200,
    home_lat: float | None = None,
    home_lon: float | None = None,
    traffic_update_threshold_seconds: int = 600,
) -> int:
    """Run één check-cyclus. Returns aantal alerts verstuurd.

    Origin volgorde: laatste PA-LOC phone-positie binnen
    `location_max_age_seconds`, anders home-fallback (home_lat/lon) als
    geconfigureerd. Zonder beide skipt de tick (geen origin om vanaf te
    rekenen).
    """
    now = now_local()
    horizon = now + timedelta(minutes=horizon_minutes)

    try:
        events = calendar.list_events(time_min=now, time_max=horizon, max_results=20)
    except Exception:
        log.exception("travel-alerts: calendar fetch failed")
        return 0

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        loc = latest_location(conn, max_age_seconds=location_max_age_seconds)
    if loc is None and home_lat is not None and home_lon is not None:
        # Fallback: assume the user is at home (geen recente PA-LOC).
        # Markeer source zodat de alert-tekst het kan vermelden.
        loc = {"lat": home_lat, "lon": home_lon, "source": "home_fallback"}
    if loc is None:
        log.debug("travel-alerts: geen recente phone-locatie en geen home-fallback — skip tick")
        return 0

    sent = 0
    for ev in events:
        if not ev.get("location"):
            continue
        if not ev.get("id") or not ev.get("start"):
            continue
        # Online-meetings: geen reistijd nodig, dus geen HERE-call.
        # Privacy-bescherming: locatie wordt alleen extern gedeeld voor
        # daadwerkelijk fysieke afspraken.
        if _is_online_meeting(str(ev.get("location"))):
            log.debug("travel-alerts: event %s is online (skip)", ev.get("id"))
            continue
        result = _maybe_alert_event(
            event=ev, now=now, loc=loc,
            db_path=db_path, here=here,
            send_imessage=send_imessage, primary_handle=primary_handle,
            plan_minutes=plan_minutes, buffer_minutes=buffer_minutes,
            traffic_update_threshold_seconds=traffic_update_threshold_seconds,
        )
        if result:
            sent += 1
    return sent


def _maybe_alert_event(
    *,
    event: dict[str, Any], now: datetime, loc: dict[str, Any],
    db_path: Path, here: HereMapsClient,
    send_imessage: Callable[[str, str], None], primary_handle: str,
    plan_minutes: int, buffer_minutes: int,
    traffic_update_threshold_seconds: int = 600,
) -> bool:
    event_id = str(event["id"])
    start = _parse_iso(event["start"])
    if start is None:
        return False
    location_str = str(event.get("location") or "").strip()
    dest = _parse_destination(location_str)
    if dest is None:
        # Vrije-tekst adres → vraag HERE Geocoder.
        dest = here.geocode(location_str)
    if dest is None:
        log.debug("travel-alerts: kon coords niet uit '%s' halen — skip", location_str)
        return False
    dest_lat, dest_lon = dest

    summary: RouteSummary | None = here.car_travel_time(
        origin_lat=loc["lat"], origin_lon=loc["lon"],
        dest_lat=dest_lat, dest_lon=dest_lon,
    )
    if summary is None:
        log.warning("travel-alerts: HERE gaf geen route voor event %s", event_id)
        return False

    travel_min = summary.duration_seconds // 60
    delay_min = summary.traffic_delay_seconds // 60
    leave_by = start - timedelta(seconds=summary.duration_seconds + buffer_minutes * 60)
    minutes_to_leave = int((leave_by - now).total_seconds() // 60)

    title = event.get("title") or "(zonder titel)"
    is_home_origin = loc.get("source") == "home_fallback"
    origin_hint = " (from home)" if is_home_origin else ""
    maps_url = _maps_url(dest_lat, dest_lon, mode="d")

    # Multi-modal hint: voor korte afstanden (<10km) ook fiets/lopen
    # vergelijken. Alleen melden als de alternatieve mode binnen +5 min
    # ligt — anders is auto duidelijk de keuze. Skip leave_now (te laat
    # om nog van mode te wisselen) en traffic_update (focus op file).
    alt_hint = _multimodal_hint(
        here=here, summary=summary,
        origin_lat=loc["lat"], origin_lon=loc["lon"],
        dest_lat=dest_lat, dest_lon=dest_lon,
    )

    # Traffic-update: als plan al is verzonden en de duration is met
    # threshold verergerd, stuur een bijgewerkte alert (ook als we nog
    # vóór het plan-window zitten — files kunnen 2u vooraf al ontstaan).
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        prev_plan_duration = last_alert_duration(
            conn, event_id=event_id, alert_kind="plan",
        )
    if (
        prev_plan_duration is not None
        and minutes_to_leave > 1   # nog niet in leave_now territorium
        and summary.duration_seconds - prev_plan_duration >= traffic_update_threshold_seconds
    ):
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            if alert_already_sent(conn, event_id=event_id, alert_kind="traffic_update"):
                return False  # cap: max 1 update per event
            if not mark_alert_sent(
                conn, event_id=event_id, alert_kind="traffic_update",
                duration_seconds=summary.duration_seconds,
            ):
                return False
        prev_min = prev_plan_duration // 60
        delta_min = (summary.duration_seconds - prev_plan_duration) // 60
        body = (
            f"🚨 Traffic update — {title} at "
            f"{start.astimezone(current_tz()).strftime('%H:%M')}.\n"
            f"Reistijd was {prev_min} min, nu {travel_min} min "
            f"(+{delta_min} extra door files). Plan vertrek opnieuw.\n"
            f"Route: {maps_url}"
        )
        try:
            send_imessage(primary_handle, body)
            log.info("travel-alert sent (traffic_update) for event %s", event_id)
            return True
        except Exception:
            log.exception("travel-alerts: failed to send traffic_update for %s", event_id)
            return False

    # Bepaal welk alert-type past op dit moment.
    if minutes_to_leave > plan_minutes:
        return False
    if 0 <= minutes_to_leave <= 1:
        kind = "leave_now"
        body = (
            f"🚗 Leave now{origin_hint} — {title} at "
            f"{start.astimezone(current_tz()).strftime('%H:%M')}.\n"
            f"Travel time {travel_min} min"
            + (f" (incl. traffic +{delay_min} min)" if delay_min >= 5 else "")
            + f"\nLocation: {location_str}\n"
            f"Route: {maps_url}"
        )
    elif minutes_to_leave < 0:
        kind = "late"
        late = -minutes_to_leave
        body = (
            f"⏰ Running late — {title} at "
            f"{start.astimezone(current_tz()).strftime('%H:%M')}, "
            f"travel time now {travel_min} min ⇒ ~{late} min late.\n"
            f"Route: {maps_url}"
        )
    else:
        kind = "plan"
        body = (
            f"⏳ Plan to leave{origin_hint} in {minutes_to_leave} min for {title} "
            f"({start.astimezone(current_tz()).strftime('%H:%M')}).\n"
            f"Travel time {travel_min} min"
            + (f", traffic adds {delay_min} min" if delay_min >= 5 else "")
            + (f". {alt_hint}" if alt_hint else "")
            + f".\nLocation: {location_str}\n"
            f"Route: {maps_url}"
        )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        if alert_already_sent(conn, event_id=event_id, alert_kind=kind):
            return False
        # Direct insert (race-safe via UNIQUE constraint). Store de
        # duration zodat een latere tick traffic_update kan triggeren.
        if not mark_alert_sent(
            conn, event_id=event_id, alert_kind=kind,
            duration_seconds=summary.duration_seconds,
        ):
            return False

    try:
        send_imessage(primary_handle, body)
        log.info("travel-alert sent (%s) for event %s", kind, event_id)
        return True
    except Exception:
        log.exception("travel-alerts: failed to send iMessage for event %s", event_id)
        return False


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=current_tz())
    return dt.astimezone(current_tz())


def _multimodal_hint(
    *,
    here: HereMapsClient, summary: RouteSummary,
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
) -> str | None:
    """Voor afstanden <10km extra bike+pedestrian routes berekenen en
    een hint teruggeven als één van de alternatieven binnen +5 min van
    de car-route ligt. Returns None als geen zinvol alternatief, of als
    afstand te groot is (call-budget bespaard).

    Threshold-keuzes:
    - <10 km: bike is mogelijk realistisch
    - <3 km: pedestrian is mogelijk realistisch (anders 30+ min lopen)
    - bike binnen car_min + 5: meld als optie
    - walk binnen car_min + 2: meld als optie
    """
    # Skip lange afstanden: HERE-call-budget sparen + bike/walk niet realistisch.
    if summary.distance_meters > 10_000:
        return None
    car_min = summary.duration_seconds // 60

    parts: list[str] = []

    bike = here.travel_time(
        origin_lat=origin_lat, origin_lon=origin_lon,
        dest_lat=dest_lat, dest_lon=dest_lon, mode="bicycle",
    )
    if bike is not None:
        bike_min = bike.duration_seconds // 60
        if bike_min <= car_min + 5:
            parts.append(f"or by bike: {bike_min} min")

    if summary.distance_meters <= 3_000:
        walk = here.travel_time(
            origin_lat=origin_lat, origin_lon=origin_lon,
            dest_lat=dest_lat, dest_lon=dest_lon, mode="pedestrian",
        )
        if walk is not None:
            walk_min = walk.duration_seconds // 60
            if walk_min <= car_min + 2:
                parts.append(f"or walking: {walk_min} min")

    if not parts:
        return None
    return " — ".join(parts).capitalize()


def _maps_url(dest_lat: float, dest_lon: float, *, mode: str = "d") -> str:
    """Apple Maps deep-link. iMessage parseert dit als preview-card —
    één tap opent Maps met de routebeschrijving.

    `mode`: d=driving, w=walking, r=transit, c=cycling. Apple Maps
    valt terug op driving als de mode niet ondersteund is voor de
    bestemming.
    """
    return (
        f"https://maps.apple.com/?daddr={dest_lat:.5f},{dest_lon:.5f}"
        f"&dirflg={mode}"
    )


def _parse_destination(location_str: str) -> tuple[float, float] | None:
    """V1: extract embedded `lat,lon` uit de location-string. Geocoding van
    vrije-tekst-adressen komt later (HERE Geocoder API of cached lookup)."""
    if not location_str:
        return None
    m = _LATLON_RE.search(location_str)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
