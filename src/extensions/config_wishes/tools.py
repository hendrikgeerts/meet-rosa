"""Orchestrator-tools voor config_wishes."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.config_wishes.schema import (
    VALID_STATUS,
    get_wish,
    insert_wish,
    list_wishes,
    update_wish_status,
)

TZ = ZoneInfo("Europe/Amsterdam")


CONFIG_WISHES_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "add_config_wish",
        "description": (
            "Sla een wens/preference/regel van the user op die structureel "
            "iets aan Rosa's gedrag wil veranderen. GEBRUIK DEZE TOOL "
            "altijd als the user zegt: 'kun je voortaan ...', 'ik wil dat "
            "je ...', 'onthoud dat ...', 'graag voortaan ...', etc. "
            "Zeg NIET 'Genoteerd!' zonder de tool aan te roepen — dan "
            "raakt de wens kwijt. Bij twijfel: liever opslaan dan niet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "Korte samenvatting (max ~10 woorden)"},
                "body": {"type": "string",
                         "description": "Volledige beschrijving met context"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "config_wishes_list",
        "description": (
            "List opgeslagen config-wishes. Default toont open + wip. "
            "Use bij vragen als 'wat heb ik je gevraagd te onthouden', "
            "'welke wensen staan er nog open'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": list(VALID_STATUS),
                           "description": "Filter op status (default: open+wip)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "default": 30},
            },
        },
    },
    {
        "name": "config_wish_set_status",
        "description": (
            "Mark een wish als done/dismissed/wip. Use bij 'die wens van "
            "X is afgehandeld' / 'doe wens 12 niet meer'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "wish_id": {"type": "integer"},
                "status": {"type": "string", "enum": list(VALID_STATUS)},
            },
            "required": ["wish_id", "status"],
        },
    },
]


def add_config_wish_handler(
    db_path: Path, args: dict[str, Any], *,
    source_handle: str | None = None,
) -> dict[str, Any]:
    title = str(args.get("title", "")).strip()
    if not title:
        return {"error": "title is required"}
    body = args.get("body")
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        wid = insert_wish(
            conn, title=title, body=body, source_handle=source_handle,
        )
    return {"ok": True, "id": wid, "title": title}


def config_wishes_list_handler(
    db_path: Path, args: dict[str, Any],
) -> list[dict[str, Any]]:
    status = args.get("status")
    limit = int(args.get("limit", 30))
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        if status:
            rows = list_wishes(conn, status=status, limit=limit)
        else:
            # Default: open + wip
            rows = list_wishes(conn, status="open", limit=limit)
            rows += list_wishes(conn, status="wip", limit=limit)
    return [_format_wish(r) for r in rows]


def config_wish_set_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    try:
        wish_id = int(args["wish_id"])
        status = str(args["status"])
    except (KeyError, ValueError, TypeError):
        return {"error": "wish_id (int) en status (string) zijn vereist"}
    try:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            ok = update_wish_status(conn, wish_id, status)
            if not ok:
                return {"error": f"wish {wish_id} not found"}
            wish = get_wish(conn, wish_id)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "wish": _format_wish(wish or {})}


def _format_wish(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    if r.get("created_at"):
        out["created"] = datetime.fromtimestamp(r["created_at"], TZ).isoformat()
    if r.get("resolved_at"):
        out["resolved"] = datetime.fromtimestamp(r["resolved_at"], TZ).isoformat()
    return out


CONFIG_WISHES_HANDLERS = {
    "add_config_wish": add_config_wish_handler,
    "config_wishes_list": config_wishes_list_handler,
    "config_wish_set_status": config_wish_set_status_handler,
}
