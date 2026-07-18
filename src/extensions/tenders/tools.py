"""Claude-tools voor on-demand vragen over TenderNed-aanbestedingen.

- `tenders_list_recent` — "welke matched aanbestedingen zijn er deze week?"
- `tenders_search`      — "zoek aanbestedingen over X"
- `tenders_ignore`      — "die ene was niet relevant, niet meer tonen"
- `tenders_status`      — quick health-check / config-view
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .matcher import DEFAULT_FILTER

log = logging.getLogger(__name__)

MAX_LIST_LIMIT = 50
MAX_SEARCH_LIMIT = 30


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for json_field in ("cpv_codes", "nuts_codes", "matched_layers", "matched_terms"):
        v = d.get(json_field)
        if isinstance(v, str) and v:
            try:
                d[json_field] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


def tenders_list_recent_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Lijst recente publicaties, default alleen matched. Voor 'wat ligt
    er op de plank deze week'."""
    days = max(1, min(int(args.get("days") or 14), 365))
    only_matched = bool(args.get("only_matched", True))
    include_ignored = bool(args.get("include_ignored", False))
    limit = max(1, min(int(args.get("limit") or 20), MAX_LIST_LIMIT))

    where = ["fetched_at >= strftime('%s','now') - ? * 86400"]
    params: list[Any] = [days]
    if only_matched:
        where.append("matched = 1")
    if not include_ignored:
        where.append("ignored_at IS NULL")

    sql = (
        "SELECT * FROM tenders WHERE " + " AND ".join(where) +
        " ORDER BY publicatie_datum DESC, fetched_at DESC LIMIT ?"
    )
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        # Count totals for context
        total = conn.execute(
            "SELECT COUNT(*) FROM tenders WHERE matched=1 "
            "AND fetched_at >= strftime('%s','now') - ? * 86400",
            (days,),
        ).fetchone()[0]

    return {
        "ok": True,
        "days": days,
        "matched_total": int(total),
        "shown": len(rows),
        "tenders": [_row_to_dict(r) for r in rows],
    }


def tenders_search_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """LIKE-search over title + organization + description. Voor
    the user-vragen als 'wat was die ene narrowcasting-aanbesteding van
    ROC ook al weer'."""
    q = (args.get("query") or "").strip()
    if len(q) < 2:
        return {"ok": False, "error": "query te kort (min 2 chars)"}
    only_matched = bool(args.get("only_matched", False))
    limit = max(1, min(int(args.get("limit") or 10), MAX_SEARCH_LIMIT))

    where = [
        "(aanbesteding_naam LIKE ? "
        "OR opdrachtgever_naam LIKE ? "
        "OR opdracht_beschrijving LIKE ?)",
    ]
    pattern = f"%{q}%"
    params: list[Any] = [pattern, pattern, pattern]
    if only_matched:
        where.append("matched = 1")

    sql = (
        "SELECT * FROM tenders WHERE " + " AND ".join(where) +
        " ORDER BY publicatie_datum DESC LIMIT ?"
    )
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    return {
        "ok": True,
        "query": q,
        "shown": len(rows),
        "tenders": [_row_to_dict(r) for r in rows],
    }


def tenders_ignore_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Markeer publicatie als 'niet relevant — niet meer in lijst tonen'.
    Toekomstige rectificaties van dezelfde kenmerk-keten worden óók
    geskipt voor alerts."""
    try:
        pub_id = int(args.get("publicatie_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "publicatie_id (int) is required"}
    reason = (args.get("reason") or "").strip()[:300]

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT publicatie_id, aanbesteding_naam, kenmerk "
            "FROM tenders WHERE publicatie_id = ?",
            (pub_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"publicatie {pub_id} niet bekend"}
        conn.execute(
            "UPDATE tenders SET ignored_at = strftime('%s','now'), "
            "notes = COALESCE(notes,'') || ? "
            "WHERE publicatie_id = ?",
            (f"\nignored: {reason}" if reason else "\nignored", pub_id),
        )

    return {
        "ok": True,
        "publicatie_id": pub_id,
        "title": row["aanbesteding_naam"],
        "kenmerk": row["kenmerk"],
        "reason": reason or None,
    }


def tenders_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Snel overzicht: hoeveel tenders binnen, hoeveel matched, hoeveel
    alerted vandaag. Voor health-check."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) AS n FROM tenders").fetchone()["n"]
        matched = conn.execute(
            "SELECT COUNT(*) AS n FROM tenders WHERE matched = 1"
        ).fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM tenders "
            "WHERE date(fetched_at,'unixepoch','localtime') = date('now','localtime')"
        ).fetchone()["n"]
        alerted_today = conn.execute(
            "SELECT COUNT(*) AS n FROM tenders WHERE alerted_at IS NOT NULL "
            "AND date(alerted_at,'unixepoch','localtime') = date('now','localtime')"
        ).fetchone()["n"]
        latest_fetch = conn.execute(
            "SELECT MAX(fetched_at) AS t FROM tenders"
        ).fetchone()["t"]

    return {
        "ok": True,
        "total_in_db": int(total),
        "matched_total": int(matched),
        "fetched_today": int(today),
        "alerted_today": int(alerted_today),
        "last_fetch_unix": int(latest_fetch) if latest_fetch else None,
        "filter": {
            "cpv_codes": list(DEFAULT_FILTER.cpv_codes),
            "cpv_description_keywords": list(DEFAULT_FILTER.cpv_description_keywords),
            "keyword_count": len(DEFAULT_FILTER.keywords),
        },
    }


# --- registratie --------------------------------------------------------

TENDER_HANDLERS = {
    "tenders_list_recent": tenders_list_recent_handler,
    "tenders_search":      tenders_search_handler,
    "tenders_ignore":      tenders_ignore_handler,
    "tenders_status":      tenders_status_handler,
}

TENDER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "tenders_list_recent",
        "description": (
            "Lijst recente TenderNed-aanbestedingen die door de AV/narrowcasting/"
            "digital-signage filter zijn gekomen. Gebruik wanneer the user vraagt "
            "'welke aanbestedingen zijn er?', 'wat ligt er op de plank deze "
            "week', 'nieuwe tenders'. Default: alleen matched, laatste 14 dagen, "
            "20 items. Tenders die door the user als 'niet relevant' gemarkeerd "
            "zijn worden standaard weggelaten."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365,
                         "description": "Aantal dagen terug (default 14)."},
                "only_matched": {"type": "boolean",
                                  "description": "Default true. Zet op false om ALLE binnenkomende publicaties te zien."},
                "include_ignored": {"type": "boolean",
                                     "description": "Default false. Op true om door the user weggeklikte items ook terug te zien."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT,
                          "description": f"Max aantal (default 20, max {MAX_LIST_LIMIT})."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "tenders_search",
        "description": (
            "LIKE-zoek over aanbesteding-naam, opdrachtgever en omschrijving. "
            "Voor vragen als 'die ROC-narrowcasting aanbesteding van laatst' of "
            "'aanbestedingen van NS'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2,
                          "description": "Vrije zoekterm — wordt LIKE-matched."},
                "only_matched": {"type": "boolean", "description": "Default false (zoek in alles)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tenders_ignore",
        "description": (
            "Markeer publicatie als 'niet relevant' — niet meer tonen in "
            "lijst, en latere rectificaties van dezelfde aanbesteding "
            "worden ook gesuppressd voor alerts. Use case: the user kijkt "
            "naar een binnengekomen alert en zegt 'nee, niets voor mij' "
            "of 'die was niet relevant'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "publicatie_id": {"type": "integer", "minimum": 1,
                                   "description": "TenderNed publicatie-ID (uit een eerdere lijst of alert)."},
                "reason": {"type": "string", "maxLength": 300,
                           "description": "Optionele toelichting voor de notitie."},
            },
            "required": ["publicatie_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tenders_status",
        "description": (
            "Health-check van de tender-monitor: hoeveel publicaties zijn "
            "vandaag binnengekomen, hoeveel matched, hoeveel alerts "
            "verstuurd, en welk filter er actief is. Voor vragen als "
            "'doet de tender-monitor het nog' of 'waarop wordt er gefilterd'."
        ),
        "input_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
    },
]
