"""Claude-tools voor faillissementen-on-demand queries en
watchlist-beheer.

- insolvencies_list_recent  — recente matched-publicaties
- insolvencies_search       — LIKE-search over naam/plaats/activiteit
- insolvencies_ignore       — markeer als 'niet relevant'
- insolvencies_status       — health-check
- insolvency_watchlist_add  — KvK aan watchlist toevoegen
- insolvency_watchlist_remove — KvK verwijderen
- insolvency_watchlist_list — toon watchlist
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .matcher import DEFAULT_FILTER
from .schema import (
    add_to_ignored_kvks, add_to_watchlist, list_watchlist,
    normalize_kvk, remove_from_watchlist,
)

log = logging.getLogger(__name__)

MAX_LIST_LIMIT = 50
MAX_SEARCH_LIMIT = 30


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for json_field in ("matched_layers", "matched_terms"):
        v = d.get(json_field)
        if isinstance(v, str) and v:
            try:
                d[json_field] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


# ---- query tools -------------------------------------------------------

def insolvencies_list_recent_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
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
        "SELECT * FROM insolvencies WHERE " + " AND ".join(where) +
        " ORDER BY pub_at_unix DESC, fetched_at DESC LIMIT ?"
    )
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM insolvencies WHERE matched = 1 "
            "AND fetched_at >= strftime('%s','now') - ? * 86400",
            (days,),
        ).fetchone()[0]

    return {
        "ok": True,
        "days": days,
        "matched_total": int(total),
        "shown": len(rows),
        "insolvencies": [_row_to_dict(r) for r in rows],
    }


def insolvencies_search_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    q = (args.get("query") or "").strip()
    if len(q) < 2:
        return {"ok": False, "error": "query te kort (min 2 chars)"}
    only_matched = bool(args.get("only_matched", False))
    days = max(1, min(int(args.get("days") or 365), 3650))  # M1: window-cap
    limit = max(1, min(int(args.get("limit") or 10), MAX_SEARCH_LIMIT))

    pattern = f"%{q}%"
    where = [
        "(naam LIKE ? OR plaats LIKE ? OR hoofd_activiteit LIKE ? OR kvk LIKE ?)",
        "fetched_at >= strftime('%s','now') - ? * 86400",  # M1
    ]
    params: list[Any] = [pattern, pattern, pattern, pattern, days]
    if only_matched:
        where.append("matched = 1")

    sql = (
        "SELECT * FROM insolvencies WHERE " + " AND ".join(where) +
        " ORDER BY pub_at_unix DESC LIMIT ?"
    )
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    return {
        "ok": True,
        "query": q,
        "days": days,
        "shown": len(rows),
        "insolvencies": [_row_to_dict(r) for r in rows],
    }


def insolvencies_ignore_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """H2: Markeer publicatie als 'niet relevant'. Als het item een
    KvK heeft, wordt die KvK óók aan `ignored_kvks` toegevoegd zodat
    toekomstige publicaties van hetzelfde bedrijf (rectificaties,
    verslagen, gunning) ook geskipt worden voor alerts."""
    link = (args.get("link") or "").strip()
    if not link:
        return {"ok": False, "error": "link is required"}
    reason = (args.get("reason") or "").strip()[:300]
    suppress_future = bool(args.get("suppress_future_for_kvk", True))

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT naam, kvk FROM insolvencies WHERE link = ?", (link,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "link niet bekend"}
        conn.execute(
            "UPDATE insolvencies SET ignored_at = strftime('%s','now'), "
            "notes = COALESCE(notes,'') || ? WHERE link = ?",
            (f"\nignored: {reason}" if reason else "\nignored", link),
        )

        # H2: KvK-niveau suppression voor toekomstige publicaties
        kvk_added = False
        kvk_value = row["kvk"]
        if suppress_future and kvk_value:
            try:
                kvk_added = add_to_ignored_kvks(
                    conn, kvk=kvk_value,
                    reason=reason or "ignored via insolvencies_ignore",
                    via_link=link,
                )
            except ValueError:
                pass  # KvK was niet valid voor normalisatie — skip

    return {
        "ok": True, "link": link, "naam": row["naam"],
        "reason": reason or None,
        "kvk": kvk_value,
        "future_alerts_suppressed_for_kvk": bool(kvk_value) and suppress_future,
        "kvk_newly_added_to_ignore_list": kvk_added,
    }


def insolvencies_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) AS n FROM insolvencies").fetchone()["n"]
        matched = conn.execute(
            "SELECT COUNT(*) AS n FROM insolvencies WHERE matched = 1"
        ).fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM insolvencies "
            "WHERE date(fetched_at,'unixepoch','localtime') = "
            "date('now','localtime')"
        ).fetchone()["n"]
        alerted_today = conn.execute(
            "SELECT COUNT(*) AS n FROM insolvencies WHERE alerted_at IS NOT NULL "
            "AND date(alerted_at,'unixepoch','localtime') = "
            "date('now','localtime')"
        ).fetchone()["n"]
        watchlist_size = conn.execute(
            "SELECT COUNT(*) AS n FROM kvk_watchlist"
        ).fetchone()["n"]
        latest = conn.execute(
            "SELECT MAX(fetched_at) AS t FROM insolvencies"
        ).fetchone()["t"]
    return {
        "ok": True,
        "total_in_db": int(total),
        "matched_total": int(matched),
        "fetched_today": int(today),
        "alerted_today": int(alerted_today),
        "watchlist_size": int(watchlist_size),
        "last_fetch_unix": int(latest) if latest else None,
        "filter": {
            "activity_keywords_count": len(DEFAULT_FILTER.activity_keywords),
            "name_keywords_count": len(DEFAULT_FILTER.name_keywords),
        },
    }


# ---- watchlist tools ---------------------------------------------------

def _validate_kvk(raw: Any) -> str | None:
    """Deprecated alias — gedeelde normalize_kvk in schema.py wordt
    elders ook gebruikt. Pure pass-through, behouden voor backward-
    compat met bestaande tests."""
    return normalize_kvk(raw)


def insolvency_watchlist_add_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    kvk = _validate_kvk(args.get("kvk"))
    if kvk is None:
        return {"ok": False, "error": "kvk moet uit cijfers bestaan (4-12 lang)"}
    naam_hint = (args.get("naam_hint") or "").strip()[:200] or None
    relation = (args.get("relation") or "").strip().lower() or None
    if relation and relation not in ("klant", "leverancier", "concurrent", "other"):
        relation = "other"
    notes = (args.get("notes") or "").strip()[:500] or None
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        added = add_to_watchlist(
            conn, kvk=kvk, naam_hint=naam_hint,
            relation=relation, added_via="imessage", notes=notes,
        )
    return {
        "ok": True, "kvk": kvk, "added": added,
        "naam_hint": naam_hint, "relation": relation,
    }


def insolvency_watchlist_remove_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    kvk = _validate_kvk(args.get("kvk"))
    if kvk is None:
        return {"ok": False, "error": "kvk moet uit cijfers bestaan"}
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        removed = remove_from_watchlist(conn, kvk)
    if not removed:
        return {"ok": False, "kvk": kvk, "error": "KvK stond niet op watchlist"}
    return {"ok": True, "kvk": kvk, "removed": True}


def insolvency_watchlist_list_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        rows = list_watchlist(conn)
    return {
        "ok": True,
        "count": len(rows),
        "watchlist": [
            {
                "kvk": r["kvk"], "naam_hint": r["naam_hint"],
                "relation": r["relation"], "added_at": r["added_at"],
                "added_via": r["added_via"], "notes": r["notes"],
            }
            for r in rows
        ],
    }


# ---- registratie -------------------------------------------------------

INSOLVENCIES_HANDLERS = {
    "insolvencies_list_recent":      insolvencies_list_recent_handler,
    "insolvencies_search":           insolvencies_search_handler,
    "insolvencies_ignore":           insolvencies_ignore_handler,
    "insolvencies_status":           insolvencies_status_handler,
    "insolvency_watchlist_add":      insolvency_watchlist_add_handler,
    "insolvency_watchlist_remove":   insolvency_watchlist_remove_handler,
    "insolvency_watchlist_list":     insolvency_watchlist_list_handler,
}

INSOLVENCIES_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "insolvencies_list_recent",
        "description": (
            "Lijst recente faillissementen/surseances die door de filter "
            "zijn gekomen (KvK-watchlist OF activiteit-keyword OF "
            "naam-keyword). Voor vragen als 'staat er een klant op de "
            "lijst die failliet ging', 'welke faillissementen zijn er deze "
            "week relevant'. Default 14 dagen, alleen matched, ignored "
            "weggelaten."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "only_matched": {"type": "boolean"},
                "include_ignored": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "insolvencies_search",
        "description": (
            "LIKE-search over bedrijfsnaam, plaats, activiteit en KvK-"
            "nummer. Voor 'is bedrijf X failliet?', 'staan er bedrijven "
            "uit Tilburg failliet', 'wie is de curator van Ruitech'. "
            "Default 365 dagen scope; voor oudere zaken expliciet days "
            "verhogen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "days": {"type": "integer", "minimum": 1, "maximum": 3650,
                          "description": "Time-window in dagen (default 365)."},
                "only_matched": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insolvencies_ignore",
        "description": (
            "Markeer publicatie als 'niet relevant — niet meer in "
            "matched-lijst tonen'. Default OOK toekomstige publicaties "
            "van dezelfde KvK suppressen (rectificaties / verslagen). "
            "Gebruik wanneer the user zegt 'kan weg' / 'niet relevant' / "
            "'die ken ik niet'. Zet suppress_future_for_kvk=false als "
            "the user alleen DEZE publicatie wil ignoren maar toekomstige "
            "van dezelfde KvK wel wil zien."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "link": {"type": "string",
                         "description": "Volledige faillissementsdossier-URL uit de alert/lijst."},
                "reason": {"type": "string", "maxLength": 300},
                "suppress_future_for_kvk": {
                    "type": "boolean",
                    "description": "Default true: ook KvK toevoegen aan ignore-lijst.",
                },
            },
            "required": ["link"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insolvencies_status",
        "description": (
            "Health-check: hoeveel publicaties zijn vandaag binnen, "
            "hoeveel matched, hoeveel alerts verstuurd, hoeveel KvK's "
            "op watchlist. Voor 'doet de faillissement-monitor het nog'."
        ),
        "input_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
    },
    {
        "name": "insolvency_watchlist_add",
        "description": (
            "Voeg een KvK-nummer toe aan the user's watchlist — bij een "
            "faillissement van dit nummer krijgt hij direct een prio-1 "
            "alert. Gebruik wanneer the user zegt 'voeg KvK X toe aan "
            "watchlist', 'watch KvK X', 'hou bedrijf Y in de gaten "
            "(KvK ...)', 'we leveren aan KvK X — wil ik weten als ze "
            "omvallen'. Vraag KvK-nummer als the user alleen een "
            "bedrijfsnaam noemt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kvk": {"type": "string", "description": "KvK-nummer (4-12 cijfers, wordt genormaliseerd)."},
                "naam_hint": {"type": "string", "description": "Vriendelijke naam voor leesbaarheid."},
                "relation": {"type": "string",
                              "enum": ["klant", "leverancier", "concurrent", "other"]},
                "notes": {"type": "string", "maxLength": 500},
            },
            "required": ["kvk"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insolvency_watchlist_remove",
        "description": (
            "Verwijder KvK van de watchlist. Gebruik wanneer the user "
            "zegt 'haal KvK X eraf', 'unwatch X', 'stop met monitoren'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"kvk": {"type": "string"}},
            "required": ["kvk"],
            "additionalProperties": False,
        },
    },
    {
        "name": "insolvency_watchlist_list",
        "description": (
            "Toon alle KvK's op de watchlist met hun naam_hint en "
            "relatie. Voor 'wie staan er op mijn watchlist', 'toon "
            "watchlist', 'welke bedrijven monitoren we'."
        ),
        "input_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
    },
]
