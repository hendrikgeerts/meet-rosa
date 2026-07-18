"""Orchestrator-tools voor scheduling-proposals.

Drie tools die the user via iMessage kan triggeren ("send 3", "show 3",
"cancel 3" of natuurlijke taal die door Claude → tool wordt vertaald):

  proposals_list  — overzicht van openstaande proposals
  send_proposal   — verstuur de mail + maak calendar event met Meet-link
  cancel_proposal — markeer als cancelled, geen send

Tools worden via core/tools.py wired met een ToolContext-extension
(scheduler_assist.tools.SchedulerToolContext) zodat ze toegang hebben
tot alle dependencies (gateway niet nodig — concept is al geschreven).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.scheduler_assist.schema import (
    get_proposal, list_pending, mark_cancelled, mark_sent,
)
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from integrations.imap import ImapAccount
from integrations.mail_router import send as mail_send

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


SCHEDULER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "proposals_list",
        "description": (
            "List open scheduling-proposals (mail-replies that Rosa drafted "
            "and is waiting on the user to confirm before sending). Use when "
            "the user asks 'wat staat er klaar', 'welke voorstellen', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
    },
    {
        "name": "send_proposal",
        "description": (
            "Send a pending scheduling-proposal: emails the drafted reply "
            "to the original sender via the right mailbox (Gmail/IMAP) AND "
            "creates a tentative Google Calendar event for the chosen slot "
            "with a Google Meet link, inviting the sender."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "integer"},
                "chosen_slot_index": {
                    "type": "integer", "minimum": 1,
                    "description": "1-based index of the slot to actually book. If omitted, send only (no event yet — typical for first contact)."
                },
            },
            "required": ["proposal_id"],
        },
    },
    {
        "name": "cancel_proposal",
        "description": (
            "Cancel a pending scheduling-proposal — Rosa won't send anything "
            "and the user handles the reply himself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "integer"},
            },
            "required": ["proposal_id"],
        },
    },
]


def proposals_list(
    db_path: Path, args: dict[str, Any], **_kw: Any,
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_pending(conn, limit=int(args.get("limit", 10)))
    return [_row_to_dict(r) for r in rows]


def send_proposal(
    db_path: Path, args: dict[str, Any],
    *, gmail: GmailClient, calendar: CalendarClient,
    imap_accounts: list[ImapAccount],
) -> dict[str, Any]:
    pid = int(args["proposal_id"])
    chosen_idx = args.get("chosen_slot_index")
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        prop = get_proposal(conn, pid)
    if not prop or prop["status"] != "pending":
        return {"error": f"proposal {pid} not pending"}

    # 1) Stuur de mail via mail-router (correct from-account).
    try:
        result = mail_send(
            from_address=prop["reply_from_address"],
            to=prop["sender"],
            subject=prop["draft_subject"],
            body=prop["draft_body"],
            in_reply_to_thread_id=prop["thread_ref"] if prop["reply_via_source"] == "gmail" else None,
            in_reply_to_message_id=prop["thread_ref"] if prop["reply_via_source"] == "imap" else None,
            gmail=gmail,
            imap_accounts=imap_accounts,
        )
    except Exception as exc:
        log.exception("send_proposal: mail send failed for #%d", pid)
        return {"error": f"mail send failed: {exc}"}

    # 2) Optioneel: agenda-event aanmaken voor het gekozen slot.
    event_id: str | None = None
    if chosen_idx is not None:
        slots = prop.get("slots") or []
        try:
            slot = slots[int(chosen_idx) - 1]
        except (IndexError, ValueError):
            slot = None
        if slot:
            try:
                start_dt = datetime.fromisoformat(slot["start"])
                end_dt = datetime.fromisoformat(slot["end"])
                event = calendar.create_event(
                    title=f"Afspraak: {prop['sender']}",
                    start=start_dt, end=end_dt,
                    description=f"Auto-aangemaakt door Rosa (proposal #{pid}).",
                    attendees=[prop["sender"]] if "@" in prop["sender"] else None,
                    add_meet_link=bool(prop["add_meet_link"]),
                )
                event_id = event.get("id")
            except Exception:
                log.exception("send_proposal: calendar create failed for #%d", pid)

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        mark_sent(conn, pid,
                  message_id=result.message_id,
                  calendar_event_id=event_id)

    return {
        "ok": True,
        "proposal_id": pid,
        "backend": result.backend,
        "message_id": result.message_id,
        "calendar_event_id": event_id,
    }


def cancel_proposal(
    db_path: Path, args: dict[str, Any], **_kw: Any,
) -> dict[str, Any]:
    pid = int(args["proposal_id"])
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        ok = mark_cancelled(conn, pid)
    return {"ok": ok, "proposal_id": pid}


def _row_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "sender": r["sender"],
        "subject": r["subject"],
        "duration_minutes": r["duration_minutes"],
        "slots": r.get("slots") or [],
        "draft_subject": r["draft_subject"],
        "draft_body_preview": (r["draft_body"] or "")[:300],
        "reply_via": r["reply_via_source"]
            + (f"/{r['reply_via_account']}" if r.get("reply_via_account") else ""),
        "reply_from": r["reply_from_address"],
        "created": datetime.fromtimestamp(r["created_at"], TZ).isoformat(),
    }


SCHEDULER_HANDLERS: dict[str, Any] = {
    "proposals_list": proposals_list,
    "send_proposal": send_proposal,
    "cancel_proposal": cancel_proposal,
}
