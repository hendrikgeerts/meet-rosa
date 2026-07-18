"""Meeting-prep tick: scheduler-callable die elke N min checkt of er
events zijn over ~minutes_before minuten met externe deelnemers en
nog geen prep verzonden.

Externe-deelnemer bepaling: attendee email != the user's eigen
gmail-address (DST). Intern overleg (alleen the user / domain match) →
skip.

Build via gateway met person_brief per attendee + recent mail. Resultaat
wordt als iMessage-tekst naar the user gestuurd.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from extensions.meeting_prep.schema import already_sent, mark_sent
from extensions.person_brief.lookup import build_person_brief
from integrations.gcal import CalendarClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import current_tz, now_local
TZ = ZoneInfo("Europe/Amsterdam")


_PREP_PROMPT = """Je bent Rosa, the user's persoonlijke assistent. Je schrijft een meeting-prep brief voor een afspraak die over ~30 min begint. the user leest dit als iMessage. Stijl: kort, bullet-format, alleen wat hij MOET weten om voorbereid binnen te komen.

Output structuur:
- Eén openingsregel: titel + tijd + Meet-link of locatie.
- 👤 Wie zit erbij — per externe deelnemer 1-2 zinnen (wie ze zijn / hun rol bij hun bedrijf / relationship-tier indien VIP).
- 📜 Recente context — 2-4 bullets over wat er recent is besproken (mail-thread highlights, vorige meeting decisions). Skip als er geen recente interactie is.
- ⚠️ Open punten — open_loops met deze persoon (max 3, als er zijn).
- 🎯 Suggested talking points — 2-3 concrete punten die the user kan adresseren. Gebaseerd op recente context + open punten.

Schrijf in het Engels. Geen plichtplegingen, geen 'good luck', geen samenvatting van wat ik je heb gegeven — alleen wat the user nodig heeft."""


def tick(
    *,
    db_path: Path,
    calendar: CalendarClient,
    gateway: Gateway,
    vip_path: Path,
    send_imessage: Callable[[str, str], None],
    primary_handle: str,
    gmail_address: str,
    minutes_before: int = 30,
    fire_window_seconds: int = 240,         # ±2 min around the target
    horizon_minutes: int = 60,
    skip_internal_only: bool = True,
    settings: Any | None = None,
) -> int:
    """Run één check-cyclus. Returns aantal preps verzonden."""
    now = now_local()
    horizon = now + timedelta(minutes=horizon_minutes)

    try:
        events = calendar.list_events(time_min=now, time_max=horizon, max_results=20)
    except Exception:
        log.exception("meeting-prep: calendar fetch failed")
        return 0

    sent = 0
    for ev in events:
        if not ev.get("id") or not ev.get("start"):
            continue
        start_dt = _parse_iso(ev["start"])
        if start_dt is None:
            continue
        # Fire wanneer event over ~minutes_before minuten is (binnen window).
        delta_seconds = (start_dt - now).total_seconds()
        target = minutes_before * 60
        if not (target - fire_window_seconds <= delta_seconds <= target + fire_window_seconds):
            continue

        attendees = ev.get("attendees") or []
        external = _external_attendees(attendees, gmail_address)
        if skip_internal_only and not external:
            log.debug("meeting-prep: event %s is internal-only — skip", ev["id"])
            continue

        with sqlite3.connect(db_path, isolation_level=None) as conn:
            if already_sent(conn, ev["id"]):
                continue

        prep_text = _build_prep(
            event=ev, external_attendees=external,
            db_path=db_path, calendar=calendar, gateway=gateway,
            vip_path=vip_path, settings=settings,
        )

        with sqlite3.connect(db_path, isolation_level=None) as conn:
            if not mark_sent(conn, event_id=ev["id"], minutes_before=int(delta_seconds // 60)):
                continue

        try:
            send_imessage(primary_handle, prep_text)
            log.info("meeting-prep: sent voor event %s (%d ext attendees)",
                     ev["id"], len(external))
            sent += 1
        except Exception:
            log.exception("meeting-prep: iMessage send failed voor %s", ev["id"])
    return sent


def _build_prep(
    *,
    event: dict[str, Any],
    external_attendees: list[str],
    db_path: Path,
    calendar: CalendarClient,
    gateway: Gateway,
    vip_path: Path,
    settings: Any | None = None,
) -> str:
    """Bouw context voor Claude: per externe attendee een mini-brief +
    laatste-week mail-context. Stuur naar Claude voor synthese."""
    briefs: list[dict[str, Any]] = []
    for addr in external_attendees[:5]:   # cap voor tokens
        try:
            briefs.append(build_person_brief(
                query=addr, db_path=db_path, calendar=calendar,
                vip_path=vip_path, days_back=60, days_forward=14,
                interaction_limit=8, meeting_limit=3, loop_limit=5,
                event_limit=2,
            ))
        except Exception:
            log.exception("meeting-prep: person_brief failed voor %s", addr)

    payload = {
        "event": {
            "title": event.get("title"),
            "start": event.get("start"),
            "end": event.get("end"),
            "location": event.get("location") or "",
            "meet_url": event.get("meet_url"),
            "description": (event.get("description") or "")[:500],
        },
        "external_attendees": external_attendees,
        "person_briefs": briefs,
    }
    user_payload = (
        "Context (JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de meeting-prep brief."
    )
    system = _PREP_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="meeting_prep",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=900,
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(meeting-prep was leeg)"


def _external_attendees(attendees: list[Any], own_address: str) -> list[str]:
    own = (own_address or "").strip().lower()
    out: list[str] = []
    for a in attendees:
        addr = a if isinstance(a, str) else (a or {}).get("email")
        if not addr:
            continue
        if own and addr.lower() == own:
            continue
        out.append(addr)
    return out


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=current_tz())
    return dt.astimezone(current_tz())
