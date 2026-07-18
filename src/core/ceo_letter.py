"""Wekelijkse CEO-letter — synthetisch overzicht voor the user elke
vrijdag 17:00 via iMessage.

Plot:
- Aggregator pakt alle signalen van afgelopen 7 dagen (volumes,
  decisions, loops opened/closed, stale items, agenda highlights,
  patterns, OKR-pulse).
- Claude schrijft via de privacy-gateway een reflectieve letter
  (niet enumeratief): wat ging goed/slecht, wat bleek écht vs ruis,
  één pattern dat opvalt, focus voor volgende week.
- Output gaat naar primary_handle als iMessage. Max ~1500 chars.

Verschilt van dayclose (dag-niveau, alleen vandaag) en briefing
(ochtend-vooruitblik): dit is een week-niveau retrospectief moment
op vrijdagmiddag — gericht op patroon-zien, niet op operationele
to-do's.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.okrs.loader import load_okrs, to_briefing_snapshot
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import now_local

TZ = ZoneInfo("Europe/Amsterdam")


CEO_LETTER_PROMPT = """You are Rosa, the user's personal assistant. It is Friday afternoon and you write him a short weekly CEO-letter — reflective rather than enumerative.

the user is CEO at DST Templates / YourHolding. The letter goes via iMessage so:
- Max ~1500 chars (incl. line breaks). Be tight.
- No bullets-explosion. Group thoughts into 4-5 short paragraphs.
- Direct, no preamble like "Hier is je weekoverzicht".
- Open met één zin context: "Vrijdag X mei — een week met ..." of vergelijkbaar (NL).

Structure suggestions (skip what doesn't apply):
- 🟢 Wins: 1-3 concrete dingen die deze week werkten / decisions die richting gaven.
- 🔴 Open / risks: stale loops > 14 dagen, overdue items, klanten die langer dan gemiddeld stil zijn — kort, met persoon-naam waar relevant.
- 🔄 Wat veranderde: zichtbare delta's (comm-volume ↑/↓, gewijzigde topics, patterns).
- 📅 Volgende week: deadlines die naderen, key meetings.
- 💡 Eén observatie: pak één pattern dat je niet uit de kale cijfers kunt zien — bv. "veel vragen rond X, mogelijk tijd voor proces-fix".

Sluit af met "Fijn weekend." of een rustige variant.

Talen: het is een reflectie voor the user in NL — schrijf primair NL met Engelse termen waar zakelijk natuurlijk (bv. "MRR", "open loops")."""


def collect_week_context(
    *,
    gmail: GmailClient | None,
    calendar: CalendarClient | None,
    db_path: Path,
    okrs_path: Path | None = None,
) -> dict[str, Any]:
    """Aggregator: pak alle signalen van afgelopen 7 dagen + outlook voor
    de komende 7. Pure SQL + Calendar API."""
    now = now_local()
    week_start_unix = int((now - timedelta(days=7)).timestamp())
    prev_week_start_unix = int((now - timedelta(days=14)).timestamp())
    next_week_end = now + timedelta(days=7)

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row

        # Comm-volumes
        vol_rows = conn.execute("""
            SELECT direction, COUNT(*) n FROM comm_items
             WHERE occurred_at >= ? GROUP BY direction
        """, (week_start_unix,)).fetchall()
        week_in = next((r["n"] for r in vol_rows if r["direction"] == "in"), 0)
        week_out = next((r["n"] for r in vol_rows if r["direction"] == "out"), 0)
        prev_rows = conn.execute("""
            SELECT direction, COUNT(*) n FROM comm_items
             WHERE occurred_at >= ? AND occurred_at < ?
             GROUP BY direction
        """, (prev_week_start_unix, week_start_unix)).fetchall()
        prev_in = next((r["n"] for r in prev_rows if r["direction"] == "in"), 0)
        prev_out = next((r["n"] for r in prev_rows if r["direction"] == "out"), 0)

        # Top correspondents (van+naar samen, deze week)
        top_corr = [
            dict(r) for r in conn.execute("""
                SELECT
                    CASE WHEN direction='in' THEN from_addr
                         ELSE json_extract(to_addrs, '$[0]') END as person,
                    COUNT(*) n
                  FROM comm_items
                 WHERE occurred_at >= ? AND source IN ('gmail','imap')
                 GROUP BY person
                HAVING person IS NOT NULL AND person != ''
                 ORDER BY n DESC
                 LIMIT 5
            """, (week_start_unix,)).fetchall()
        ]

        # Decisions deze week
        try:
            decisions = [
                dict(r) for r in conn.execute("""
                    SELECT title, context, decided_at FROM decisions
                     WHERE decided_at >= ? AND status='active'
                     ORDER BY decided_at DESC LIMIT 10
                """, (week_start_unix,)).fetchall()
            ]
        except sqlite3.OperationalError:
            decisions = []

        # Open loops: stale (>14d) + overdue + closed this week
        loops_open_now = conn.execute("""
            SELECT id, kind, who, COALESCE(action_summary, title) as action,
                   created_at, due_at
              FROM open_loops
             WHERE status='open'
        """).fetchall()
        now_unix = int(now.timestamp())
        stale = []
        overdue = []
        for r in loops_open_now:
            age = (now_unix - r["created_at"]) // 86400
            d = {"kind": r["kind"], "who": r["who"],
                 "action": (r["action"] or "")[:120], "age_days": age}
            if r["due_at"] and r["due_at"] < now_unix:
                overdue.append(d)
            if age >= 14:
                stale.append(d)

        closed_this_week = conn.execute("""
            SELECT COUNT(*) n FROM open_loops
             WHERE resolved_at >= ?
        """, (week_start_unix,)).fetchone()["n"]

        # Patterns recently surfaced
        try:
            patterns = [
                dict(r) for r in conn.execute("""
                    SELECT title, body, detected_at FROM patterns
                     WHERE detected_at >= ?
                     ORDER BY detected_at DESC LIMIT 5
                """, (week_start_unix,)).fetchall()
            ]
        except sqlite3.OperationalError:
            patterns = []

    # Calendar: events next 7 days
    events_next: list[dict[str, Any]] = []
    if calendar is not None:
        try:
            evs = calendar.list_events(
                time_min=now, time_max=next_week_end, max_results=10,
            )
            for e in evs:
                events_next.append({
                    "title": e.get("title") or "(zonder titel)",
                    "start": e.get("start", ""),
                })
        except Exception:
            log.exception("calendar list_events failed for ceo-letter")

    # OKRs
    okrs_snapshot: list[dict[str, Any]] = []
    if okrs_path and okrs_path.exists():
        try:
            okrs_snapshot = to_briefing_snapshot(load_okrs(okrs_path))
        except Exception:
            log.exception("okrs load failed for ceo-letter")

    return {
        "now": now.isoformat(),
        "weekday": now.strftime("%A"),
        "volumes": {
            "week_in": week_in, "week_out": week_out,
            "delta_in": week_in - prev_in,
            "delta_out": week_out - prev_out,
            "top_correspondents": top_corr,
        },
        "decisions": decisions,
        "loops": {
            "open_total": len(loops_open_now),
            "stale_over_14d": stale[:10],
            "overdue": overdue[:10],
            "closed_this_week": closed_this_week,
        },
        "patterns": patterns,
        "events_next_7d": events_next[:10],
        "okrs": okrs_snapshot,
    }


def generate_ceo_letter(
    *,
    gateway: Gateway,
    gmail: GmailClient | None,
    calendar: CalendarClient | None,
    db_path: Path,
    okrs_path: Path | None = None,
    settings: Any | None = None,
) -> str:
    """Bouw context + laat Claude een ~1500-char letter schrijven."""
    context = collect_week_context(
        gmail=gmail, calendar=calendar, db_path=db_path, okrs_path=okrs_path,
    )
    user_payload = (
        "Context (JSON):\n"
        + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de wekelijkse CEO-letter."
    )
    system = CEO_LETTER_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="ceo_letter",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=1024,
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(ceo-letter was leeg)"


def next_friday_at(now: datetime, hhmm: str = "17:00") -> datetime:
    """Eerstvolgende vrijdag op hhmm. Op vrijdag zelf na hhmm → volgende week."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    days_ahead = (4 - now.weekday()) % 7  # 4 = Friday
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    return target + timedelta(days=days_ahead)
