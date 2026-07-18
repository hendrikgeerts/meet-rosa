"""Todoist-pulse voor briefings + midday.

Returnt een compacte snapshot van open Todoist-taken zodat de
briefing-prompt the user kan herinneren aan vandaag's items + overdue
spullen. Wordt aangeroepen door core.briefings.collect_briefing_context
en core.midday.collect_midday_context wanneer een TodoistClient is
geconfigureerd.

Faalt nooit hard — bij netwerk-/auth-fouten wordt een lege snapshot
geretourneerd zodat de hele briefing niet platgaat door één API-glitch.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from integrations.todoist import Task, TodoistClient

log = logging.getLogger(__name__)


def _due_iso(t: Task, *, tz: Any = None) -> str | None:
    """Pak YYYY-MM-DD in target-TZ — werkt voor due_date en due_datetime.
    Review 27/6 M3: due_datetime van Todoist staat in UTC-Z; zonder
    conversie geeft 23:30 NL per ongeluk een verkeerde dag."""
    if t.due_date:
        return t.due_date
    if not t.due_datetime:
        return None
    if tz is None:
        return t.due_datetime[:10]
    try:
        s = t.due_datetime.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return t.due_datetime[:10]
        return dt.astimezone(tz).date().isoformat()
    except (ValueError, TypeError):
        return t.due_datetime[:10]


def _task_summary(t: Task) -> dict[str, Any]:
    # Review 27/6 L3: hergebruikt de tools-formatter zodat tool_results
    # en briefing-context dezelfde shape garanderen.
    from extensions.todoist_sync.tools import _task_to_dict
    return _task_to_dict(t)


def build_todoist_pulse(
    client: TodoistClient | None,
    *,
    project_id: str | None = None,
    today: date | None = None,
    today_limit: int = 5,
    overdue_limit: int = 5,
    tz: Any = None,
) -> dict[str, Any]:
    """Snapshot van open Todoist-taken voor briefing/midday.

    Returns:
        {
            "today": [{id, content, due_date, due_datetime, labels}, ...],
            "today_count": N,            # totaal vandaag (vóór limit)
            "overdue": [...],            # oudste eerst
            "overdue_count": N,
            "available": bool,
        }

    Lege/disabled state → alles 0/[] + available=False zodat de
    briefing-prompt het netjes kan skippen.
    """
    empty: dict[str, Any] = {
        "today": [], "today_count": 0,
        "overdue": [], "overdue_count": 0,
        "available": False,
    }
    if client is None:
        return empty

    try:
        tasks = client.list_tasks(project_id=project_id)
    except Exception:
        log.exception("todoist pulse: list_tasks failed")
        return empty

    # M2/M3: TZ-aware default. Caller geeft 'today' van now_local(); zonder
    # caller-arg vallen we terug op current_tz() — niet machine-naive.
    if tz is None:
        from core.timezone import current_tz
        tz = current_tz()
    if today is None:
        today = datetime.now(tz).date()
    today_iso = today.isoformat()

    today_tasks: list[Task] = []
    overdue_tasks: list[Task] = []
    for t in tasks:
        d = _due_iso(t, tz=tz)
        if d is None:
            continue
        if d == today_iso:
            today_tasks.append(t)
        elif d < today_iso:
            overdue_tasks.append(t)

    today_tasks.sort(key=lambda t: (t.due_datetime or "9999", t.content.lower()))
    overdue_tasks.sort(key=lambda t: (_due_iso(t, tz=tz) or "0000", t.content.lower()))

    return {
        "today": [_task_summary(t) for t in today_tasks[:today_limit]],
        "today_count": len(today_tasks),
        "overdue": [_task_summary(t) for t in overdue_tasks[:overdue_limit]],
        "overdue_count": len(overdue_tasks),
        "available": True,
    }


def build_todoist_midday_pulse(
    client: TodoistClient | None,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
    remaining_limit: int = 5,
    tz: Any = None,
) -> dict[str, Any]:
    """Midday-variant: focus op 'wat staat er nog open voor vandaag'.

    Geen overdue-lijst (die zit al in de ochtend-briefing); wel een
    `remaining_today` lijst van taken die nog niet completed zijn én
    een due_date == vandaag (of due_datetime in de toekomst van vandaag).
    """
    empty: dict[str, Any] = {
        "remaining_today": [], "remaining_count": 0, "available": False,
    }
    if client is None:
        return empty

    try:
        tasks = client.list_tasks(project_id=project_id)
    except Exception:
        log.exception("todoist midday pulse: list_tasks failed")
        return empty

    if tz is None:
        from core.timezone import current_tz
        tz = current_tz()
    if now is None:
        now = datetime.now(tz)
    today_iso = now.date().isoformat()

    remaining: list[Task] = []
    for t in tasks:
        d = _due_iso(t, tz=tz)
        if d != today_iso:
            continue
        # Als een datetime aanwezig is en al voorbij, vermoedelijk te-laat —
        # toch tonen want nog open, maar onder header "open vandaag".
        remaining.append(t)

    remaining.sort(key=lambda t: (t.due_datetime or "9999", t.content.lower()))

    return {
        "remaining_today": [_task_summary(t) for t in remaining[:remaining_limit]],
        "remaining_count": len(remaining),
        "available": True,
    }
