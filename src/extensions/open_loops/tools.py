"""Orchestrator-tool: `loops_open` — toon open-loops in een lijst.

Returns summary-only — body's blijven in `comm_items.body_full` of
`plaud_transcripts.body`. Tool-handler vult de details in via de juiste
tabel als Claude een specifiek item wil opvragen.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.query_safety import validate_query
from extensions.open_loops.schema import (
    close_loop, delegations_due_for_followup, extend_followup,
    list_open, snooze_loop,
)

LOOPS_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "loops_open",
        "description": (
            "List open loops — items die nog actie van the user vragen. "
            "Bron is mail/Slack ('incoming_question'/'incoming_task') of "
            "Plaud-meetings ('meeting_action_self'). Use voor "
            "vragen als 'wat staat er nog open' / 'waar moet ik op reageren' / "
            "'wat heeft Piet me gevraagd'. Returns id + title + age_days — "
            "die id heb je later nodig voor close_loop / snooze_loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [
                    "incoming_question", "incoming_task",
                    "meeting_action_self", "meeting_action_other",
                    "outgoing_request",
                ]},
                "who": {
                    "type": "string",
                    "minLength": 3,
                    "pattern": "^[^%_*']+$",
                    "description": "Filter op afzender (substring, ≥3 chars, geen wildcards)",
                },
                "days_back": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "close_loop",
        "description": (
            "Markeer een open loop als afgehandeld (status='done'). Gebruik "
            "wanneer the user via iMessage zegt dat hij een actie/vraag al "
            "heeft afgewerkt buiten Rosa om — bv. 'die mail van Piet heb "
            "ik gisteren al gebeld, mag dicht'. Pass `notes` als the user "
            "context geeft die we willen onthouden ('telefonisch geregeld', "
            "'klant ziet er vanaf')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "integer"},
                "notes": {"type": "string", "description": "Optionele context bij het sluiten"},
            },
            "required": ["loop_id"],
        },
    },
    {
        "name": "snooze_loop",
        "description": (
            "Verplaats een loop naar later — verschijnt automatisch weer "
            "als 'open' op het opgegeven tijdstip. Gebruik wanneer the user "
            "zegt 'niet nu, herinner me volgende week' of similar. "
            "Roep get_current_time eerst aan om relatieve tijden te resolven."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "integer"},
                "until": {"type": "string", "description": "ISO 8601 datetime, bv. 2026-05-03T09:00:00+02:00"},
            },
            "required": ["loop_id", "until"],
        },
    },
]


def loops_open(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    who_raw = (args.get("who") or "").strip()
    who: str | None = None
    if who_raw:
        ok, _err = validate_query(who_raw)
        if ok:
            who = who_raw.translate(str.maketrans("", "", "%_")) or None
        # If validation fails, fall back to "no filter" rather than empty
        # result — the tool is also useful without a who-filter.
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_open(
            conn,
            kind=args.get("kind"),
            who=who,
            days_back=int(args.get("days_back", 30)),
            limit=int(args.get("limit", 20)),
        )
    tz = ZoneInfo("Europe/Amsterdam")
    out = []
    for r in rows:
        # action_summary kan ontbreken in oudere rows — fallback op title.
        action = r["action_summary"] if "action_summary" in r.keys() else None
        out.append({
            "id": r["id"],
            "kind": r["kind"],
            "who": r["who"],
            "title": r["title"],
            "action_summary": action,
            "body_excerpt": r["body_excerpt"],
            "context": r["context"],
            "created": datetime.fromtimestamp(r["created_at"], tz).isoformat(),
            "due": datetime.fromtimestamp(r["due_at"], tz).isoformat() if r["due_at"] else None,
            "age_days": (int(datetime.now(tz).timestamp()) - r["created_at"]) // 86400,
        })
    return out


def close_loop_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        ok = close_loop(
            conn, int(args["loop_id"]),
            via="manual", notes=(args.get("notes") or None),
        )
    return {"ok": ok, "loop_id": int(args["loop_id"])}


def snooze_loop_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime as _dt
    until_iso = args["until"]
    try:
        until_unix = int(_dt.fromisoformat(until_iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return {"error": f"invalid `until`: {until_iso!r}"}
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        ok = snooze_loop(conn, int(args["loop_id"]), until_unix=until_unix)
    return {"ok": ok, "loop_id": int(args["loop_id"]), "until": until_iso}


def delegations_list_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """List open delegations (outgoing_request + meeting_action_other)
    waar the user op iemand WACHT. Hetzelfde dataset als
    `loops_open(kind='outgoing_request')` maar gegroepeerd voor de
    delegation-tracker UX."""
    raw_limit = args.get("limit", 30)
    try:
        limit = max(1, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        limit = 30
    out: list[dict[str, Any]] = []
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        for kind in ("outgoing_request", "meeting_action_other"):
            for r in list_open(conn, kind=kind, limit=limit):
                out.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "who": r.get("who"),
                    "title": r.get("action_summary") or r.get("title"),
                    "delegated_at": r.get("created_at"),
                    "followup_at": r.get("followup_at"),
                    "followup_pinged_at": r.get("followup_pinged_at"),
                })
    out.sort(key=lambda r: r.get("delegated_at") or 0)
    return {"count": len(out), "items": out[:limit]}


def delegation_extend_followup_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Verschuif de followup-reminder voor een delegation met N dagen
    en reset de 'al gepingd'-marker zodat Rosa over X dagen opnieuw
    kan pingen."""
    try:
        loop_id = int(args["loop_id"])
        extra_days = int(args.get("extra_days", 7))
    except (KeyError, ValueError, TypeError):
        return {"error": "loop_id (int) + optional extra_days (int) required"}
    extra_days = max(1, min(extra_days, 365))
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        ok = extend_followup(conn, loop_id=loop_id, extra_days=extra_days)
    return {"ok": ok, "loop_id": loop_id, "extended_by_days": extra_days}


LOOPS_TOOL_SCHEMAS.extend([
    {
        "name": "delegations_list",
        "description": (
            "List delegations — items where the user is WAITING on someone "
            "else (outgoing_request from mail/Slack + meeting_action_other "
            "from Plaud). Returns id + who + title + delegated_at + "
            "followup_at + followup_pinged_at. Use when the user asks "
            "'waar wacht ik nog op', 'wat heb ik allemaal uitgezet', "
            "'wie moet me nog terugbellen'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 100,
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "delegation_extend_followup",
        "description": (
            "Extend a delegation's follow-up reminder by N days. Use "
            "when the user says 'verschuif #12 met een week', 'remind me "
            "about X in 14 days'. Resets the 'already pinged' marker so "
            "Rosa pings again at the new date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "integer"},
                "extra_days": {
                    "type": "integer", "minimum": 1, "maximum": 365,
                    "default": 7,
                },
            },
            "required": ["loop_id"],
        },
    },
])


LOOPS_HANDLERS = {
    "loops_open": loops_open,
    "close_loop": close_loop_handler,
    "snooze_loop": snooze_loop_handler,
    "delegations_list": delegations_list_handler,
    "delegation_extend_followup": delegation_extend_followup_handler,
}
