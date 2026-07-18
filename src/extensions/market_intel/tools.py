"""Orchestrator-tools voor market-intel: ad-hoc query vanuit iMessage.

`market_search` — vrije tekst over title+summary
`market_recent` — laatste N items per domein (CEO-overzicht-vraag)
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.query_safety import QUERY_SCHEMA, validate_query
from extensions.market_intel.schema import recent, search

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


MARKET_INTEL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "market_search",
        "description": (
            "Doorzoek de markt-intel database (digital signage + AI nieuws) "
            "op een trefwoord in title of summary. Use bij vragen als "
            "'wat speelde er rond Samsung MagicInfo?' of 'noem alles "
            "rond Claude 4.5'. Query must be ≥3 chars without wildcards "
            "(%, _, *, ')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**QUERY_SCHEMA, "description": "Trefwoord of zin"},
                "domain": {"type": "string", "enum": ["digital_signage", "ai_models"],
                           "description": "Filter op domein. Optioneel."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "market_recent",
        "description": (
            "Recente markt-intel items, gesorteerd op opportunities + score. "
            "Use bij 'wat is er deze week gebeurd in digital signage?' of "
            "'laat me de laatste AI-modellen zien'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": ["digital_signage", "ai_models"]},
                "days": {"type": "integer", "minimum": 1, "maximum": 60, "default": 7},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
            },
        },
    },
]


def market_search(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = (args.get("query") or "").strip()
    ok, err = validate_query(query)
    if not ok:
        log.info("market_search rejected: %s", err)
        return []
    query = query.translate(str.maketrans("", "", "%_"))
    if not query:
        return []
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = search(
            conn, query=query,
            domain=args.get("domain"),
            limit=int(args.get("limit", 20)),
        )
    return [_row_to_dict(r) for r in rows]


def market_recent(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = recent(
            conn,
            domain=args.get("domain"),
            days=int(args.get("days", 7)),
            limit=int(args.get("limit", 15)),
        )
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    pub = r.get("published_at") or r.get("fetched_at")
    return {
        "id": r["id"],
        "domain": r["domain"],
        "source": r["source"],
        "title": r["title"],
        "url": r["url"],
        "summary": r.get("summary"),
        "relevance": r.get("relevance_score"),
        "is_opportunity": bool(r.get("is_opportunity")),
        "opportunity_reason": r.get("opportunity_reason"),
        "published": datetime.fromtimestamp(pub, TZ).isoformat() if pub else None,
    }


MARKET_INTEL_HANDLERS = {
    "market_search": market_search,
    "market_recent": market_recent,
}
