"""Orchestrator-tools voor receipt-collector.

- receipt_run_start(excel_path) — kick-off een nieuwe kwartaal-run
- receipt_run_status(run_id)    — voortgang + per-item details
- receipt_runs_list             — recente runs
- vendor_strategy_remember      — leg vast 'vendor X komt via mail Y' /
                                  'vendor Z zit in portal W'
- vendor_strategies_list        — bestaand vendor-geheugen ophalen
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.receipt_collector.runner import run_receipt_collection
from extensions.receipt_collector.schema import (
    VALID_SOURCE_KIND,
    get_run,
    list_run_items,
    list_runs,
    list_vendor_strategies,
    upsert_vendor_strategy,
)
from integrations.imap import ImapAccount

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


RECEIPT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "receipt_run_start",
        "description": (
            "Start a quarterly receipt-collection run from an Excel file. "
            "Parses the afschrijvingen-list, derives a date-window from the "
            "transactions (oldest -30d to newest +30d), and searches Gmail "
            "+ all enabled IMAP accounts for matching invoices/PDF receipts. "
            "Use bij 'zoek bonnen voor dit Excel' / 'verzamel facturen voor "
            "Q2 2026'. Excel pad mag absoluut zijn of relatief vanaf "
            "~/PA-Receipts/inbox/."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "excel_path": {"type": "string"},
                "margin_days": {"type": "integer", "minimum": 0, "maximum": 90,
                                  "default": 30,
                                  "description": "Marge boven/onder oudste/jongste txn-datum"},
            },
            "required": ["excel_path"],
        },
    },
    {
        "name": "receipt_run_status",
        "description": (
            "Get full status van een receipt-run: transaction count, matched/"
            "needs-portal/unknown counts, per-item details. Use bij 'hoe staat "
            "die receipt-run' / 'wat ontbreekt nog van Q2'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "receipt_runs_list",
        "description": (
            "List recent receipt-runs (default last 10). Use bij 'welke "
            "kwartaal-runs heb ik gedaan'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "default": 10},
            },
        },
    },
    {
        "name": "vendor_strategy_remember",
        "description": (
            "Sla op hoe Rosa bonnen voor een specifieke vendor moet vinden. "
            "Use wanneer the user vertelt 'AWS bonnen komen van billing@aws.com' "
            "of 'Microsoft licenties moet je downloaden van admin.microsoft.com "
            "→ Billing → Invoices'. Volgende run gebruikt deze hint automatisch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                          "description": "Canonical vendor naam (bv. 'AWS')"},
                "source_kind": {"type": "string",
                                  "enum": list(VALID_SOURCE_KIND)},
                "aliases": {"type": "array", "items": {"type": "string"},
                              "description": "Alternative spellings die in Excel kunnen staan"},
                "email_query_hint": {"type": "string",
                                       "description": "Bv. 'from:billing@aws.amazon.com' (Gmail-syntax)"},
                "portal_url": {"type": "string"},
                "portal_notes": {"type": "string",
                                   "description": "Vrije tekst download-instructies"},
            },
            "required": ["name", "source_kind"],
        },
    },
    {
        "name": "vendor_strategies_list",
        "description": (
            "List alle bekende vendor-strategieën — wat Rosa weet over waar "
            "elke vendor zijn bonnen levert. Use bij 'welke vendors heeft Rosa "
            "geleerd' / 'check geheugen voor vendor X'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def receipt_run_start_handler(
    db_path: Path, args: dict[str, Any], *,
    gmail: Any | None = None,
    imap_accounts: list[tuple[ImapAccount, str]] | None = None,
    output_root: Path | None = None,
    ollama: Any | None = None,
) -> dict[str, Any]:
    # ISO_AUDIT 2026-05 HIGH-4: Claude-controllable excel_path. Restrict
    # to ~/PA-Receipts/** + .xlsx/.xls extension, fully resolve to defeat
    # `../` traversal, and don't echo the resolved path back in errors
    # (would confirm file existence outside the sandbox).
    raw_path = str(args["excel_path"]).strip()
    receipts_root = (output_root or Path.home() / "PA-Receipts").expanduser().resolve()
    inbox = (receipts_root / "inbox").resolve()
    if Path(raw_path).is_absolute():
        candidate = Path(raw_path).expanduser().resolve()
    else:
        candidate = (inbox / raw_path).resolve()
    try:
        candidate.relative_to(receipts_root)
    except ValueError:
        return {"error": "excel_path must be within ~/PA-Receipts/"}
    if candidate.suffix.lower() not in {".xlsx", ".xls"}:
        return {"error": "excel_path must point to a .xlsx or .xls file"}
    if not candidate.exists():
        return {"error": f"excel not found in ~/PA-Receipts/: {candidate.name}"}
    excel_path = candidate

    out_root = output_root or Path.home() / "PA-Receipts"
    out_root.mkdir(parents=True, exist_ok=True)

    return run_receipt_collection(
        excel_path=excel_path,
        db_path=db_path,
        output_root=out_root,
        gmail=gmail,
        imap_accounts=imap_accounts or [],
        margin_days=int(args.get("margin_days", 30)),
        ollama=ollama,
    )


def receipt_run_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    run_id = int(args["run_id"])
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        run = get_run(conn, run_id)
        if not run:
            return {"error": f"run not found: {run_id}"}
        items = list_run_items(conn, run_id)
    return {
        "run": _format_run(run),
        "items": [_format_item(i) for i in items],
    }


def receipt_runs_list_handler(
    db_path: Path, args: dict[str, Any],
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_runs(conn, limit=int(args.get("limit", 10)))
    return [_format_run(r) for r in rows]


def vendor_strategy_remember_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    try:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            vid = upsert_vendor_strategy(
                conn,
                name=str(args["name"]).strip(),
                source_kind=str(args["source_kind"]),
                aliases=list(args.get("aliases") or []),
                email_query_hint=(args.get("email_query_hint") or None),
                portal_url=(args.get("portal_url") or None),
                portal_notes=(args.get("portal_notes") or None),
            )
        return {"ok": True, "vendor_id": vid, "name": args["name"]}
    except ValueError as e:
        return {"error": str(e)}


def vendor_strategies_list_handler(
    db_path: Path, _args: dict[str, Any],
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        return list_vendor_strategies(conn)


def _format_run(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    if r.get("started_at"):
        out["started"] = datetime.fromtimestamp(r["started_at"], TZ).isoformat()
    if r.get("completed_at"):
        out["completed"] = datetime.fromtimestamp(r["completed_at"], TZ).isoformat()
    if r.get("date_window_start"):
        out["window_start_date"] = datetime.fromtimestamp(r["date_window_start"], TZ).date().isoformat()
    if r.get("date_window_end"):
        out["window_end_date"] = datetime.fromtimestamp(r["date_window_end"], TZ).date().isoformat()
    return out


def _format_item(i: dict[str, Any]) -> dict[str, Any]:
    out = dict(i)
    out["amount_eur"] = i["amount_cents"] / 100.0
    out["transaction_date_iso"] = datetime.fromtimestamp(i["transaction_date"], TZ).date().isoformat()
    return out


RECEIPT_HANDLERS = {
    "receipt_run_start": receipt_run_start_handler,
    "receipt_run_status": receipt_run_status_handler,
    "receipt_runs_list": receipt_runs_list_handler,
    "vendor_strategy_remember": vendor_strategy_remember_handler,
    "vendor_strategies_list": vendor_strategies_list_handler,
}
