"""Orchestrator-tool: recent_expenses."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.expenses.schema import CATEGORIES, list_recent

TZ = ZoneInfo("Europe/Amsterdam")


EXPENSES_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "recent_expenses",
        "description": (
            "List recent expense receipts. Use bij vragen als 'wat heb ik "
            "deze maand uitgegeven', 'hoeveel software-bonnen staan er', "
            "'mijn reiskosten van laatste 30 dagen'. Returns vendor + bedrag "
            "+ datum + categorie per item, plus totaal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                "category": {"type": "string", "enum": list(CATEGORIES) + [""]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
]


def recent_expenses_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    cat = (args.get("category") or "").strip() or None
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_recent(
            conn, days=int(args.get("days", 30)),
            category=cat, limit=int(args.get("limit", 50)),
        )
    items = [_format(r) for r in rows]
    total_cents = sum(int(r.get("amount_cents") or 0) for r in rows)
    return {
        "items": items,
        "count": len(items),
        "total_amount": round(total_cents / 100, 2),
        "currency": items[0]["currency"] if items else "EUR",
    }


def _format(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "vendor": r["vendor"],
        "date": (datetime.fromtimestamp(r["receipt_date"], TZ).date().isoformat()
                 if r["receipt_date"] else None),
        "amount": round((r["amount_cents"] or 0) / 100, 2),
        "vat": round((r["vat_cents"] or 0) / 100, 2),
        "currency": r["currency"],
        "category": r["category"],
        "description": r["description"],
        "source_path": r["source_path"],
    }


EXPENSES_HANDLERS = {"recent_expenses": recent_expenses_handler}
