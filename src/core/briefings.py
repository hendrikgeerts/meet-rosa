"""Daily morning briefing: summarise today's calendar + important unread mail
+ pending reminders, delivered via iMessage at a configured time."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions import reminders  # noqa: F401 — schema-init side effect
from extensions.morning_extras.news import (
    fetch_news_bundle,
    load_morning_extras_config,
    parse_feeds,
)
from extensions.morning_extras.weather import fetch_weather
from extensions.okrs.loader import load_okrs, to_briefing_snapshot
from extensions.travel_alerts.schema import latest_location
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from integrations.here_maps import HereMapsClient
from models.ollama import OllamaClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import now_local

TZ = ZoneInfo("Europe/Amsterdam")


BRIEFING_PROMPT = """You are Rosa, the user's personal assistant. You write his morning briefing. He reads this as iMessage, so:
- Keep it short, bulleted, no bold formatting.
- Address him directly ("you").
- Start with one sentence about the day (e.g. "Morning — 3 meetings today, 1 client").
- 🌤 Weather (1 line): location, current/min/max temp, short word (cloudy/rain/sun), rain chance, wind. Skip if data missing.
- 📅 Calendar: list events with time + title. Mark the first of the day.
- 📧 Mail: only what really needs attention (questions for you, deadlines, escalations). Skip newsletters/noise. Name sender + 1 sentence why it matters.
- ⏰ Reminders for today: brief, one line.
- 🎂 Birthdays/jubilea: when birthdays_upcoming has items happening today, mention them at the top so the user can send a quick message. Use the emoji from the data (🎂 birthday, 🏆 jubileum, 🏢 org-anniversary). Items in next 7 days as a one-liner heads-up. Skip the section if empty.
- 🎯 Focus blocks: if focus_block_suggestions has gaps ≥2h, suggest them as 'protected' deep-work slots the user can intentionally use today (or block in calendar). Format: "10:00-12:30 (2.5h)". Max 3 listed. Skip the section if no blocks fit.
- 🎯 OKR pulse: if okrs is non-empty, list each active objective on one line: title + avg progress %. If any KR is <30% with <30 days left in the period, flag it briefly ("behind schedule"). Skip the section if okrs is empty.
- 🔴 VIP-alerts: from `vip_alerts` — strategic clients gone silent (flag='alert' only — warn-level wordt bewaard voor de dayclose). Max 3 items. Format: "[A] {name} — {days_silent}d stil". Skip de sectie als list leeg. Doel: zachte ochtend-nudge dat een belangrijke klant aandacht nodig heeft.
- 🚗 Travel: from `travel_today` (list of physical meetings with computed travel-time). Per item one line: "HH:MM {title} — leave ±{leave_by}, {travel_min}m{traffic if delay≥5}". If `from_home` is true on an item, append " (from home)". Skip the section if list empty. Max 3 items. Helps the user plan his morning before alerts trigger 30 min before leave_by.
- 🎯 Sales — 3 voor vandaag: from `sales_pulse.top_three`. Skip de sectie als top_three leeg is (= geen accounts in pipeline). Format per item, één regel + suggestie-regel eronder:
  "{N}. {naam} [{target}] — {reason}"
  "   → {suggestion}"
  Target weergeven als 'ADL' (adl_video), 'DST' (dst_connect), 'DS' (ds_templates), 'multi'. Onder de 3 items één regel pipeline-summary uit `sales_pulse.pipeline_snapshot`: "Pipeline: ADL 3 nurturing · DST 2 koud / 1 offerte · DS 4 nurturing" (toon alleen targets met items).
- ✅ Todoist: from `todoist_pulse`. Skip the section entirely if available=false (Todoist not configured) OR (today_count=0 AND overdue_count=0). If today_count>0 list top entries from `today` array, one per line: "HH:MM {content}" if due_datetime present, else just "{content}". Cap at 5 lines; if today_count > shown items, append a "(+N more)" line. If overdue_count>0 add a separator line and list overdue items the same way prefixed with "⚠ overdue: ". This is the user's Todoist project — treat it as authoritative for what he has committed to today.
- 📰 News: max 5 bullets from the supplied headlines. Per bullet: title + 1 sentence why relevant. Source in italics between brackets. Skip the section if no headlines.
- When suggesting a top priority for today, prefer items that move an OKR forward when there's a clear link.
- If a section is empty, say so briefly — no filler.
- English by default."""


def collect_briefing_context(
    *,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    morning_extras_yaml: Path | None = None,
    ollama: OllamaClient | None = None,
    vip_path: Path | None = None,
    okrs_path: Path | None = None,
    here: HereMapsClient | None = None,
    home_lat: float | None = None,
    home_lon: float | None = None,
    travel_buffer_minutes: int = 5,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
) -> dict[str, Any]:
    now = now_local()

    today_events = calendar.list_today()

    try:
        important_mail = gmail.list_unread_important(max_results=15)
    except Exception:
        log.exception("briefing: gmail fetch failed")
        important_mail = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        end_of_day = datetime.combine(now.date(), time(23, 59), tzinfo=now.tzinfo)
        rows = conn.execute(
            "SELECT id, remind_at, body FROM reminders "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL AND remind_at <= ? "
            "ORDER BY remind_at ASC",
            (int(end_of_day.timestamp()),),
        ).fetchall()
        today_reminders = [
            {"id": r["id"], "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(), "body": r["body"]}
            for r in rows
        ]

    weather_data: dict[str, Any] | None = None
    news_data: dict[str, Any] | None = None
    if morning_extras_yaml is not None:
        cfg = load_morning_extras_config(morning_extras_yaml)
        if cfg:
            loc = cfg.get("location") or {}
            if loc.get("latitude") and loc.get("longitude"):
                w = fetch_weather(
                    latitude=float(loc["latitude"]),
                    longitude=float(loc["longitude"]),
                    location=str(loc.get("name", "")),
                    timezone=str(loc.get("timezone", "Europe/Amsterdam")),
                )
                if w:
                    weather_data = w.to_dict()
            news_cfg = cfg.get("news") or {}
            if ollama is not None and news_cfg:
                bundle = fetch_news_bundle(
                    feeds=parse_feeds(cfg),
                    interests=list(news_cfg.get("interests") or []),
                    top_n=int(news_cfg.get("top_n", 5)),
                    max_age_hours=int(news_cfg.get("max_age_hours", 24)),
                    ollama=ollama,
                )
                if bundle.items:
                    news_data = bundle.to_dict()

    # Birthdays + jubilea voor vandaag + komende 7 dagen
    birthdays_data: list[dict[str, Any]] = []
    if vip_path is not None:
        from extensions.birthdays.tracker import list_upcoming
        try:
            birthdays_data = list_upcoming(vip_path, days_forward=7)
        except Exception:
            log.exception("briefing: birthdays fetch failed")

    # Focus blocks: lege uren ≥2h tijdens werkdag (9:00-18:00) — als suggestie
    # in de briefing zodat the user bewust deep-work tijd kan blokken.
    focus_blocks = _detect_focus_blocks(today_events, now=now)

    okrs_snapshot: list[dict[str, Any]] = []
    if okrs_path is not None:
        try:
            okrs_snapshot = to_briefing_snapshot(load_okrs(okrs_path))
        except Exception:
            log.exception("briefing: okrs load failed")

    # VIP-alerts: alleen de hardste (flag='alert') in de ochtend zodat
    # de briefing-toon licht blijft — warn-niveau bewaren voor dayclose.
    vip_alerts: list[dict[str, Any]] = []
    try:
        from web.vip_aggregator import build_vip_snapshot
        vip_path = db_path.parent.parent / "config" / "vip_contacts.yaml"
        snap = build_vip_snapshot(db_path, vip_path)
        if snap.get("has_config"):
            vip_alerts = [v for v in snap["vips"]
                          if v["flag"] == "alert"][:3]
    except Exception:
        log.exception("briefing: vip snapshot failed")

    # Travel-snapshot: per fysiek event van vandaag de berekende reistijd
    # zodat de briefing alle vertrek-momenten op één lijn toont (eerder
    # waarschuwen dan de 30-min-vooraf alert).
    travel_today: list[dict[str, Any]] = []
    if here is not None:
        try:
            travel_today = _compute_today_travels(
                here=here, db_path=db_path, events=today_events,
                home_lat=home_lat, home_lon=home_lon,
                buffer_minutes=travel_buffer_minutes, now=now,
            )
        except Exception:
            log.exception("briefing: travel-snapshot failed")

    # Sales-pulse: top-3 + pipeline-snapshot. Markeert tegelijk
    # gebruikte triggers als consumed.
    sales_pulse: dict[str, Any] = {}
    try:
        from extensions.sales.briefing import build_sales_pulse
        sales_pulse = build_sales_pulse(db_path)
    except Exception:
        log.exception("briefing: sales-pulse failed")

    # Todoist-pulse: top-N vandaag + overdue. Skipt als client None is.
    todoist_pulse: dict[str, Any] = {
        "today": [], "today_count": 0,
        "overdue": [], "overdue_count": 0, "available": False,
    }
    if todoist_client is not None:
        try:
            from extensions.todoist_sync.briefing import build_todoist_pulse
            todoist_pulse = build_todoist_pulse(
                todoist_client,
                project_id=todoist_project_id,
                today=now.date(),
                tz=now.tzinfo,
            )
        except Exception:
            log.exception("briefing: todoist-pulse failed")

    return {
        "now": now.isoformat(),
        "weekday": now.strftime("%A"),
        "date": now.date().isoformat(),
        "events_today": today_events,
        "unread_mail": important_mail,
        "reminders_today": today_reminders,
        "weather": weather_data,
        "news": news_data,
        "birthdays_upcoming": birthdays_data,
        "focus_block_suggestions": focus_blocks,
        "okrs": okrs_snapshot,
        "vip_alerts": vip_alerts,
        "travel_today": travel_today,
        "sales_pulse": sales_pulse,
        "todoist_pulse": todoist_pulse,
    }


def _compute_today_travels(
    *,
    here: HereMapsClient, db_path: Path,
    events: list[dict[str, Any]],
    home_lat: float | None, home_lon: float | None,
    buffer_minutes: int, now: datetime,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """Voor elk physical event vandaag: bereken reistijd vanaf laatste
    PA-LOC (of home-fallback), retourneer leave_by + travel_min + delta.
    Vermijdt online meetings en past max_items toe."""

    from extensions.travel_alerts.check import (
        _ONLINE_MEETING_PATTERNS,
        _parse_destination,
    )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        loc_row = latest_location(conn, max_age_seconds=7200)
    from_home = False
    if loc_row is None and home_lat is not None and home_lon is not None:
        loc_row = {"lat": home_lat, "lon": home_lon, "source": "home_fallback"}
        from_home = True
    elif loc_row is not None and loc_row.get("source") == "home_fallback":
        from_home = True
    if loc_row is None:
        return []

    out: list[dict[str, Any]] = []
    for ev in events:
        if len(out) >= max_items:
            break
        loc_str = str(ev.get("location") or "").strip()
        if not loc_str:
            continue
        if _ONLINE_MEETING_PATTERNS.search(loc_str):
            continue
        start = _parse_iso_event(ev.get("start"))
        if start is None or start <= now:
            continue
        dest = _parse_destination(loc_str)
        if dest is None:
            dest = here.geocode(loc_str)
        if dest is None:
            continue
        summary = here.car_travel_time(
            origin_lat=loc_row["lat"], origin_lon=loc_row["lon"],
            dest_lat=dest[0], dest_lon=dest[1],
        )
        if summary is None:
            continue
        travel_min = summary.duration_seconds // 60
        delay_min = summary.traffic_delay_seconds // 60
        leave_by = start - timedelta(seconds=summary.duration_seconds + buffer_minutes * 60)
        out.append({
            "event_id": ev.get("id"),
            "title": ev.get("title") or "(no title)",
            "start": start.isoformat(),
            "location": loc_str,
            "travel_min": travel_min,
            "delay_min": delay_min,
            "leave_by": leave_by.isoformat(),
            "from_home": from_home,
        })
    return out


def _detect_focus_blocks(
    today_events: list[dict[str, Any]], *, now: datetime,
    work_start_hour: int = 9, work_end_hour: int = 18,
    min_block_minutes: int = 120,
) -> list[dict[str, str]]:
    """Vind lege uren ≥ min_block_minutes vandaag tussen werkuren.
    Returns lijst van {start, end, duration_minutes} ISO strings."""
    work_start = datetime.combine(now.date(), time(work_start_hour, 0), tzinfo=now.tzinfo)
    work_end = datetime.combine(now.date(), time(work_end_hour, 0), tzinfo=now.tzinfo)
    if now > work_end:
        return []
    cursor = max(work_start, now)

    busy: list[tuple[datetime, datetime]] = []
    for ev in today_events:
        s = _parse_iso_event(ev.get("start"))
        e = _parse_iso_event(ev.get("end"))
        if s and e and s < work_end and e > work_start:
            busy.append((max(s, work_start), min(e, work_end)))
    busy.sort()

    blocks: list[dict[str, str]] = []
    for bstart, bend in busy:
        if bend <= cursor:
            continue
        if bstart >= work_end:
            break
        if bstart > cursor:
            gap = (bstart - cursor).total_seconds() / 60
            if gap >= min_block_minutes:
                blocks.append({
                    "start": cursor.isoformat(),
                    "end": bstart.isoformat(),
                    "duration_minutes": int(gap),
                })
        cursor = max(cursor, bend)
    if work_end > cursor:
        gap = (work_end - cursor).total_seconds() / 60
        if gap >= min_block_minutes:
            blocks.append({
                "start": cursor.isoformat(),
                "end": work_end.isoformat(),
                "duration_minutes": int(gap),
            })
    return blocks


def _parse_iso_event(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        from core.timezone import current_tz
        dt = dt.replace(tzinfo=current_tz())
    from core.timezone import current_tz
    return dt.astimezone(current_tz())


def generate_briefing(
    *,
    gateway: Gateway,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    morning_extras_yaml: Path | None = None,
    ollama: OllamaClient | None = None,
    vip_path: Path | None = None,
    okrs_path: Path | None = None,
    here: HereMapsClient | None = None,
    home_lat: float | None = None,
    home_lon: float | None = None,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    settings: Any | None = None,
) -> str:
    context = collect_briefing_context(
        gmail=gmail, calendar=calendar, db_path=db_path,
        morning_extras_yaml=morning_extras_yaml, ollama=ollama,
        vip_path=vip_path, okrs_path=okrs_path,
        here=here, home_lat=home_lat, home_lon=home_lon,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project_id,
    )
    user_payload = (
        "Context (JSON):\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de briefing."
    )
    system = BRIEFING_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    # force_label='internal' (2/7): skip classifier zodat een keyword-hit
    # in de context (bv. 'salaris' in een open loop, of andere confidential-
    # trigger) niet stilletjes de briefing naar lokaal Llama routeert.
    # Llama blokkeert de scheduler-thread minutenlang → health-monitor SIGTERM.
    # Redactor blijft actief via internal-pad.
    response = gateway.complete(
        task="morning_briefing",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=1024,
        force_label="internal",
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(briefing was leeg)"


def next_fire_time(now: datetime, weekday_hhmm: str, weekend_hhmm: str) -> datetime:
    """Return the next briefing fire-time, respecting weekday/weekend split.

    Weekday = ma..vr (datetime.weekday() < 5). Weekend = za, zo.
    Always returns a future time strictly > now (skipt vandaag als we de
    target al voorbij zijn, en kiest dan de juiste tijd voor de volgende dag —
    wat een weekend-overgang of weekend→weekday-overgang kan zijn)."""
    def _hhmm_for(day: datetime) -> str:
        return weekend_hhmm if day.weekday() >= 5 else weekday_hhmm

    def _at(day: datetime, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)

    today = _at(now, _hhmm_for(now))
    if today > now:
        return today
    next_day = now + timedelta(days=1)
    return _at(next_day, _hhmm_for(next_day))


def next_fire_time_with_catchup(
    now: datetime, weekday_hhmm: str, weekend_hhmm: str,
    *,
    last_fired: datetime | None = None,
    grace: timedelta = timedelta(minutes=120),
) -> datetime:
    """Like `next_fire_time`, maar met catch-up. Als today's slot al voorbij
    is EN we hebben vandaag nog niet gefired EN we zijn binnen `grace`,
    return `now` (fire-asap). Beschermt tegen restart-crashes vlak na de
    geplande tijd waar de oude logica de hele dag stilletjes oversloeg.

    `last_fired` mag naive of TZ-aware zijn — we vergelijken via timestamps
    om TZ-mismatches te vermijden."""
    def _hhmm_for(day: datetime) -> str:
        return weekend_hhmm if day.weekday() >= 5 else weekday_hhmm

    def _at(day: datetime, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)

    today_target = _at(now, _hhmm_for(now))
    if today_target > now:
        return today_target

    next_day = now + timedelta(days=1)
    next_slot = _at(next_day, _hhmm_for(next_day))

    if last_fired is not None and last_fired.timestamp() >= today_target.timestamp():
        return next_slot

    if (now - today_target) <= grace:
        return now  # catch-up firing

    return next_slot
