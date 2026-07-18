"""Midday heads-up briefing (default 13:00 Europe/Amsterdam).

Korte mid-dag check-in: wat is vanochtend gebeurd, wat staat nog open,
wat komt er deze middag/avond. Spiegelt `dayclose.py` qua opbouw —
context-verzameling lokaal, content-generatie via `gateway.complete()`
(privacy-laag actief).

Wat al is gebeurd vandaag:
  - Agenda-events met start_time vóór nu (today-passed)
  - Reminders die vandaag al zijn afgegaan (sent_at vandaag)

Wat komt er nog vandaag:
  - Agenda-events met start_time vanaf nu tot middernacht
  - Reminders die nog moeten afgaan (pending tot middernacht)

Open loops (zelfde split als dayclose):
  - inbound — the user moet nog reageren
  - waiting — wacht op antwoord van anderen

Volume-stats per source (mail/slack):
  - aantal items binnengekomen vandaag
  - aantal openstaand (open_loops met source='comm' + account-prefix)
zodat de prompt kan zeggen "10 mails binnen, 5 openstaand".
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.open_loops.schema import list_open
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import now_local

TZ = ZoneInfo("Europe/Amsterdam")


MIDDAY_PROMPT = """You are Rosa, the user's personal assistant. You write his mid-day heads-up around lunchtime. He reads this as iMessage:
- Short and bulleted, no bold formatting.
- Start with one short opening line ("Mid-day check" or "Halftime —").
- 📊 Volume so far (1 line): "X mails received, Y new Slack messages". Use comm_volume_today.
- ✅ This morning: calendar events that have passed (with time), reminders that fired. Skip the section if empty.
- ⏳ Still to reply to (from open_loops_inbound):
    - Per item: sender + short title + (channel: mail/slack/meeting).
    - Show max 5, oldest first.
    - One count line at the top: "5 mails open, 12 Slack messages open" (use comm_open_counts; group from open_loops_inbound by source).
- 📌 Rest of the day: calendar events this afternoon/evening with time, reminders still pending.
- ✅ Todoist remaining today: from `todoist_remaining`. Skip if available=false OR remaining_count=0. List items, one per line: "HH:MM {content}" if due_datetime present else "{content}". Cap at 5; add "(+N more)" if remaining_count > shown. Use this as a nudge: items still open at midday probably won't get done if the user doesn't act on them. If something here matches an item already in 'open_loops_inbound', mention it once — don't duplicate.
- 🎯 Suggested focus: 1 sentence with the logical next action based on what's open. No forced suggestion if the afternoon is quiet.
- Close briefly. No "good luck" or motivational fluff — the user reads this between meetings.
- If the day is quiet: say so in 1 sentence and stop."""


def collect_midday_context(
    *,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
) -> dict[str, Any]:
    now = now_local()
    start_of_today = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
    start_of_tomorrow = start_of_today + timedelta(days=1)

    try:
        events_passed = calendar.list_events(
            time_min=start_of_today, time_max=now, max_results=50,
        )
    except Exception:
        log.exception("midday: calendar passed-events fetch failed")
        events_passed = []

    try:
        events_remaining = calendar.list_events(
            time_min=now, time_max=start_of_tomorrow, max_results=50,
        )
    except Exception:
        log.exception("midday: calendar remaining-events fetch failed")
        events_remaining = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Reminders die vandaag al zijn afgegaan
        rows = conn.execute(
            "SELECT id, remind_at, body FROM reminders "
            "WHERE sent_at IS NOT NULL "
            "AND sent_at >= ? AND sent_at < ? "
            "ORDER BY remind_at ASC",
            (int(start_of_today.timestamp()), int(start_of_tomorrow.timestamp())),
        ).fetchall()
        reminders_fired = [
            {"id": r["id"], "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(), "body": r["body"]}
            for r in rows
        ]

        # Reminders die nog moeten afgaan vandaag
        rows = conn.execute(
            "SELECT id, remind_at, body FROM reminders "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL "
            "AND remind_at >= ? AND remind_at < ? "
            "ORDER BY remind_at ASC",
            (int(now.timestamp()), int(start_of_tomorrow.timestamp())),
        ).fetchall()
        reminders_remaining = [
            {"id": r["id"], "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(), "body": r["body"]}
            for r in rows
        ]

        # Open loops — split per richting (zelfde semantiek als dayclose)
        try:
            loops_inbound = [
                _loop_to_dict(l, now) for l in list_open(
                    conn, days_back=30, limit=20,
                ) if l["kind"] in ("incoming_question", "incoming_task",
                                    "meeting_action_self")
            ]
            loops_waiting = [
                _loop_to_dict(l, now) for l in list_open(
                    conn, kind="outgoing_request", days_back=30, limit=10,
                )
            ]
        except sqlite3.OperationalError:
            loops_inbound = []
            loops_waiting = []

        # Volume vandaag uit comm_items (mail/slack/imap), én openstaande
        # counters uit de inbound-loops (per source). Tabellen kunnen
        # ontbreken op een verse DB — vang dat netjes op.
        comm_volume_today: dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM comm_items "
                "WHERE direction='in' "
                "AND occurred_at >= ? AND occurred_at < ? "
                "GROUP BY source",
                (int(start_of_today.timestamp()), int(start_of_tomorrow.timestamp())),
            ).fetchall()
            comm_volume_today = {r["source"]: int(r["n"]) for r in rows}
        except sqlite3.OperationalError:
            pass

    # Counters per source over de inbound-loops (samen met de bron-link
    # die we in `_loop_to_dict` toevoegen — zie hieronder).
    comm_open_counts = _count_loops_by_source(loops_inbound)

    return {
        "now": now.isoformat(),
        "weekday": now.strftime("%A"),
        "date": now.date().isoformat(),
        "events_passed_today": events_passed,
        "events_remaining_today": events_remaining,
        "reminders_fired_today": reminders_fired,
        "reminders_remaining_today": reminders_remaining,
        "open_loops_inbound": loops_inbound,
        "open_loops_waiting": loops_waiting,
        "comm_volume_today": comm_volume_today,
        "comm_open_counts": comm_open_counts,
        "todoist_remaining": _build_midday_todoist(
            todoist_client, todoist_project_id, now=now,
        ),
    }


def _build_midday_todoist(
    client: Any, project_id: str | None, *, now: datetime,
) -> dict[str, Any]:
    """Wikkelpunt — geïsoleerd zodat een Todoist-glitch niet de hele
    midday-briefing kapotmaakt."""
    try:
        from extensions.todoist_sync.briefing import build_todoist_midday_pulse
        return build_todoist_midday_pulse(
            client, project_id=project_id, now=now, tz=now.tzinfo,
        )
    except Exception:
        log.exception("midday: todoist-pulse failed")
        return {"remaining_today": [], "remaining_count": 0, "available": False}


def _loop_to_dict(row: Any, now: datetime) -> dict[str, Any]:
    # `source_ref` is bij comm-loops `f"{source}:{account}:{external_id}"`,
    # bij plaud-loops bv. `meeting:7:self:slug`. Voor counter-grouping
    # pak ik het deel vóór de eerste ':'; voor plaud val ik terug op
    # de top-level `source` ('plaud').
    source = (row.get("source") or "unknown") if isinstance(row, dict) else "unknown"
    ref = (row.get("source_ref") or "") if isinstance(row, dict) else ""
    if source == "comm" and ref:
        source = ref.split(":", 1)[0]  # 'gmail' | 'imap' | 'slack'
    return {
        "kind": row["kind"],
        "who": row["who"],
        "title": row["title"],
        "source": source,
        "age_days": (int(now.timestamp()) - row["created_at"]) // 86400,
    }


def _count_loops_by_source(loops: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for l in loops:
        s = l.get("source") or "unknown"
        out[s] = out.get(s, 0) + 1
    return out


def generate_midday(
    *,
    gateway: Gateway,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    settings: Any | None = None,
) -> str:
    context = collect_midday_context(
        gmail=gmail, calendar=calendar, db_path=db_path,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project_id,
    )
    user_payload = (
        "Context (JSON):\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de mid-dag heads-up."
    )
    system = MIDDAY_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    # force_label='internal' (2/7): zie briefings.py — voorkom classifier-
    # keyword-trigger → lokaal Llama → scheduler-block → health-SIGTERM.
    response = gateway.complete(
        task="midday_briefing",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=768,
        force_label="internal",
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(midday-briefing was leeg)"
