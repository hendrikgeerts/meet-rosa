"""Cross-kanaal overzicht-aggregator voor "wat heb ik allemaal open".

Eén pull over alle bronnen die the user anders los moet bevragen
(open_loops, comm_unanswered, reminders, Todoist). Output is een
gestructureerde dict zodat Claude de iMessage-render kan doen op één
ronde i.p.v. 4-5 sequentiële tool-calls.

Sources die meedoen:
- open_loops (mail/Slack/Plaud-inbound + delegated outgoing)
- comm-intel "unanswered" (laatste bericht in een thread is van
  iemand anders → wachten op the user)
- reminders.list_pending (lokale reminders nog niet gevuurd)
- Todoist (today + overdue) — als client beschikbaar is
"""
from __future__ import annotations

import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def collect_whats_open(
    db_path: Path, *,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    per_section_limit: int = 5,
    days_back_loops: int | None = None,
    days_back_unanswered: int = 14,
    user_handle: str | None = None,
) -> dict[str, Any]:
    """Returnt geaggregeerd dashboard van openstaande items.

    Faalt-safe per source — een lege/kapotte source geeft 0/[] en
    laat de rest van het overzicht door.
    """
    # --- open_loops (inbound: mail/Slack/Plaud) ---
    loops_inbound: list[dict[str, Any]] = []
    loops_waiting: list[dict[str, Any]] = []
    loops_meeting: list[dict[str, Any]] = []
    try:
        # M2 review-fix: status='open' is reeds een filter; days_back zou
        # oude maar nog-open loops verbergen — juist waar 'stale' op slaat.
        # None = geen leeftijdscap.
        from extensions.open_loops.schema import list_open
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            for kind, bucket in (
                ("incoming_question", loops_inbound),
                ("incoming_task", loops_inbound),
                ("outgoing_request", loops_waiting),
                ("meeting_action_self", loops_meeting),
            ):
                for row in list_open(conn, kind=kind, days_back=days_back_loops, limit=20):
                    bucket.append(_loop_summary(row))
    except Exception:
        log.exception("whats_open: open_loops fetch failed")

    # --- comm_unanswered (laatste-bericht-is-inkomend) ---
    unanswered_mail: list[dict[str, Any]] = []
    unanswered_slack: list[dict[str, Any]] = []
    try:
        from extensions.comm_intel.tools import comm_unanswered
        all_un = comm_unanswered(db_path, {
            "source": "all", "days": days_back_unanswered, "limit": 40,
        })
        for item in all_un:
            src = (item.get("source") or "").lower()
            if src in ("gmail", "imap"):
                unanswered_mail.append(_unanswered_summary(item))
            elif src == "slack":
                unanswered_slack.append(_unanswered_summary(item))
    except Exception:
        log.exception("whats_open: comm_unanswered fetch failed")

    # --- reminders.list_pending ---
    # H2 review-fix: scope op user_handle zodat een toekomstige multi-
    # handle setup geen andermans reminders in the user's "wat heb ik
    # open" laat lekken.
    reminders_pending: list[dict[str, Any]] = []
    try:
        from extensions.reminders import list_pending
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            conn.row_factory = sqlite3.Row
            for r in list_pending(conn, handle=user_handle):
                if r.get("status") != "pending":
                    continue
                reminders_pending.append({
                    "id": r["id"],
                    "remind_at": r["remind_at"],
                    "body": (r.get("body") or "")[:200],
                })
        # oldest-first (= eerstvolgende)
        reminders_pending.sort(key=lambda r: r["remind_at"])
    except Exception:
        log.exception("whats_open: reminders fetch failed")

    # --- Todoist (today + overdue) ---
    todoist_today: list[dict[str, Any]] = []
    todoist_overdue: list[dict[str, Any]] = []
    todoist_available = False
    if todoist_client is not None:
        try:
            from extensions.todoist_sync.briefing import build_todoist_pulse
            pulse = build_todoist_pulse(
                todoist_client, project_id=todoist_project_id,
            )
            todoist_today = pulse.get("today") or []
            todoist_overdue = pulse.get("overdue") or []
            todoist_available = bool(pulse.get("available"))
        except Exception:
            log.exception("whats_open: todoist pulse failed")

    # Review-queue: nieuwe open_loops die wachten op the user's
    # approve/reject (sinds 28/6). Aparte bucket — niet meegerekend
    # in grand_total want zijn aandacht in een ander spoor.
    review_queue_count = 0
    try:
        from extensions.todoist_sync.schema import queue_pending_count
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            review_queue_count = queue_pending_count(conn)
    except Exception:
        log.exception("whats_open: review-queue count failed")

    totals = {
        "loops_inbound": len(loops_inbound),
        "loops_waiting": len(loops_waiting),
        "loops_meeting": len(loops_meeting),
        "unanswered_mail": len(unanswered_mail),
        "unanswered_slack": len(unanswered_slack),
        "reminders_pending": len(reminders_pending),
        "todoist_today": len(todoist_today),
        "todoist_overdue": len(todoist_overdue),
    }
    totals["grand_total"] = sum(totals.values())
    totals["todoist_review_queue"] = review_queue_count

    return {
        "as_of": int(_time.time()),
        "totals": totals,
        "loops_inbound": loops_inbound[:per_section_limit],
        "loops_waiting": loops_waiting[:per_section_limit],
        "loops_meeting": loops_meeting[:per_section_limit],
        "unanswered_mail": unanswered_mail[:per_section_limit],
        "unanswered_slack": unanswered_slack[:per_section_limit],
        "reminders_pending": reminders_pending[:per_section_limit],
        "todoist": {
            "available": todoist_available,
            "today": todoist_today[:per_section_limit],
            "overdue": todoist_overdue[:per_section_limit],
        },
    }


def _loop_summary(row: dict[str, Any]) -> dict[str, Any]:
    now = int(_time.time())
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "who": row.get("who"),
        "title": (row.get("title") or "")[:200],
        "source": row.get("source"),
        "age_days": (now - int(row.get("created_at") or now)) // 86400,
    }


def _unanswered_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "source": row.get("source"),
        "from": row.get("sender") or row.get("from"),
        "subject": (row.get("subject") or row.get("title") or "")[:200],
        "occurred_at": row.get("occurred_at"),
    }
