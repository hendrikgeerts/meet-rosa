"""Weekend-prep: zondagavond voorbereiding op de komende week.

Hergebruikt bestaande aggregatoren (whats_open + briefings + dayclose)
maar met andere lens: niet "wat is morgen" maar "hoe zit ik er voor de
hele week voor, en wat heeft the user over het hoofd gezien".

Output via iMessage met focus op:
  - Top 3 prioriteiten maandag (uit open_loops + Todoist overdue/today)
  - Items >7d open zonder progress ("hangt al twee weken — sluiten?")
  - Eerste meeting maandag + prep status
  - Reminders voor de week
  - Dingen die maandag fout kunnen gaan (geen Q3-cijfers binnen, etc)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.timezone import now_local
from extensions.whats_open.aggregator import collect_whats_open
from integrations.gcal import CalendarClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)


WEEKEND_PREP_PROMPT = """You are Rosa, the user's personal assistant. You write his Sunday-evening week-prep — short, bulleted, no bold formatting, English.

Tone: gentle reset, not anxiety-inducing. He's about to enjoy his Sunday evening; this is "here's what you have, sleep on it." Not a to-do list ranking.

- Opening: one short line — "Week ahead — Monday starts with X" if there's a notable first meeting, or "Week ahead — calmer one, only N meetings". Use `first_monday_event` and `monday_event_count`.
- 🎯 Top 3 priorities for the week: from `top_priorities` (already ranked — pre-aggregated). One line each. Format: "{N}. {title} — {why_it_matters}".
- ⚠ Hanging items: from `stale_items` — items open >7d with no progress, mail/Slack/loops/Todoist mixed. Max 5. Format per line: "  {age}d — {title} ({source})". One-line ask at end: "Want to close any? Reply with the IDs or leave them."
- 📅 Monday's first move: from `monday_events` — first 1-2 events with time. If first event has a meeting_prep state (e.g. agenda missing) — flag it. Skip if Monday is empty.
- ⏰ Reminders this week: from `week_reminders` — top 3 by remind_at. Skip if empty.
- 🌟 Open config-wishes you set: from `open_wishes` (max 3). Short heads-up that they're still pending. Skip if empty.
- Close: "Sleep well. 🌙" or short variant.
- Skip any section that's empty — don't say "geen items". Just leave it out."""


def collect_weekend_prep_context(
    *,
    calendar: CalendarClient,
    db_path: Path,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
) -> dict[str, Any]:
    now = now_local()

    # Maandag-bounds (00:00 - 23:59)
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7  # zo we 't op maandag draaien: volgende
    monday_start = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    monday_end = monday_start + timedelta(days=1)
    week_end = monday_start + timedelta(days=7)

    # Maandag-events (eerste 5)
    monday_events: list[dict[str, Any]] = []
    try:
        evs = calendar.list_events(
            time_min=monday_start, time_max=monday_end, max_results=10,
        )
        monday_events = evs[:5]
    except Exception:
        log.exception("weekend-prep: calendar fetch failed")
    first_monday_event = monday_events[0] if monday_events else None
    monday_event_count = len(monday_events)

    # Cross-channel snapshot
    whats_open = collect_whats_open(
        db_path,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project_id,
        per_section_limit=10,
    )

    # Top 3 priorities — round-robin over sources (M1-fix), deterministisch.
    top_priorities = _build_top_priorities(whats_open)

    # Stale items — items >7d oud, excl. wat al in top_priorities staat
    # (M3-fix). M4-fix: meeting_action_self ook scannen.
    exclude_ids = {p.get("id") for p in top_priorities if p.get("id") is not None}
    stale_items = _build_stale_items(
        whats_open, threshold_days=7, exclude_ids=exclude_ids,
    )

    # Week-reminders
    week_reminders: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, remind_at, body FROM reminders "
                "WHERE sent_at IS NULL AND cancelled_at IS NULL "
                "AND remind_at >= ? AND remind_at < ? "
                "ORDER BY remind_at ASC LIMIT 5",
                (int(monday_start.timestamp()), int(week_end.timestamp())),
            ).fetchall()
            week_reminders = [
                {
                    "id": r["id"],
                    "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(),
                    "body": r["body"],
                }
                for r in rows
            ]
    except Exception:
        log.exception("weekend-prep: week-reminders fetch failed")

    # Open config-wishes
    open_wishes: list[dict[str, Any]] = []
    try:
        from extensions.config_wishes.schema import list_wishes
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            open_wishes = list_wishes(conn, status="open", limit=3)
    except Exception:
        log.exception("weekend-prep: wishes fetch failed")

    return {
        "now": now.isoformat(),
        "week_start": monday_start.isoformat(),
        "first_monday_event": first_monday_event,
        "monday_event_count": monday_event_count,
        "monday_events": monday_events,
        "top_priorities": top_priorities,
        "stale_items": stale_items,
        "week_reminders": week_reminders,
        "open_wishes": open_wishes,
        "totals_snapshot": whats_open.get("totals", {}),
    }


def _build_top_priorities(whats_open: dict[str, Any]) -> list[dict[str, Any]]:
    """Round-robin ranking — max 1 item per source per slot zodat
    3 vergeten Todoist-overdue items niet alle slots wegnemen van een
    60d oude VIP-vraag. Voorkeur: overdue > inbound oldest > todoist
    today. Max 3 items, geen Claude-call. (Review-fix M1.)"""
    out: list[dict[str, Any]] = []
    seen_sources: set[str] = set()

    overdue = whats_open.get("todoist", {}).get("overdue", []) or []
    inbound_sorted = sorted(
        whats_open.get("loops_inbound", []) or [],
        key=lambda r: r.get("age_days", 0), reverse=True,
    )
    today = whats_open.get("todoist", {}).get("today", []) or []

    def _push_from_overdue() -> bool:
        if overdue:
            t = overdue.pop(0)
            out.append({
                "source": "todoist_overdue",
                "id": t.get("id"),
                "title": t.get("content"),
                "why_it_matters": f"Todoist — overdue sinds {t.get('due_date')}",
            })
            seen_sources.add("todoist_overdue")
            return True
        return False

    def _push_from_inbound() -> bool:
        if inbound_sorted:
            loop = inbound_sorted.pop(0)
            out.append({
                "source": "inbound_loop",
                "id": loop.get("id"),
                "title": loop.get("title"),
                "why_it_matters": (
                    f"Open vraag van {loop.get('who') or '?'} "
                    f"sinds {loop.get('age_days')}d"
                ),
            })
            seen_sources.add("inbound_loop")
            return True
        return False

    def _push_from_today() -> bool:
        if today:
            t = today.pop(0)
            out.append({
                "source": "todoist_today",
                "id": t.get("id"),
                "title": t.get("content"),
                "why_it_matters": "Todoist — gepland voor maandag/vandaag",
            })
            seen_sources.add("todoist_today")
            return True
        return False

    # Slot 1: prefer overdue, fallback inbound, dan today
    for fn in (_push_from_overdue, _push_from_inbound, _push_from_today):
        if fn():
            break
    # Slot 2: kies de andere primaire source als nog niet gebruikt
    if len(out) < 3:
        order = (_push_from_inbound, _push_from_overdue, _push_from_today)
        for fn in order:
            if fn():
                break
    # Slot 3: round-robin over wat over is
    if len(out) < 3:
        for fn in (_push_from_overdue, _push_from_inbound, _push_from_today):
            if fn():
                break
    return out


def _build_stale_items(
    whats_open: dict[str, Any], *, threshold_days: int = 7,
    exclude_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Items in inbound/waiting/meeting open >threshold_days.
    Review-fix M3: exclude IDs die al in top_priorities staan zodat
    dezelfde loop niet dubbel in de week-prep verschijnt.
    Review-fix M4: meeting_action_self meetellen — Plaud-actions
    kunnen ook stilletjes ouder worden."""
    exclude_ids = exclude_ids or set()
    out: list[dict[str, Any]] = []
    for bucket_name in ("loops_inbound", "loops_waiting", "loops_meeting"):
        for loop in whats_open.get(bucket_name, []) or []:
            lid = loop.get("id")
            if lid in exclude_ids:
                continue
            age = int(loop.get("age_days") or 0)
            if age >= threshold_days:
                out.append({
                    "id": lid,
                    "age_days": age,
                    "title": loop.get("title"),
                    "source": loop.get("source"),
                    "who": loop.get("who"),
                })
    out.sort(key=lambda r: r["age_days"], reverse=True)
    return out[:5]


def generate_weekend_prep(
    *,
    gateway: Gateway,
    calendar: CalendarClient,
    db_path: Path,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    settings: Any | None = None,
) -> str:
    context = collect_weekend_prep_context(
        calendar=calendar,
        db_path=db_path,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project_id,
    )
    user_payload = (
        "Context (JSON):\n"
        + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de weekend-prep."
    )
    system = WEEKEND_PREP_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="weekend_prep",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=900,
    )
    parts = [
        b.text for b in response.content
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip() or "(weekend-prep was leeg)"
