"""Folder watcher voor receipt-PDFs. Scheduler-callable tick."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

from extensions.expenses.extract import (
    classify, extract_text, parse_date, to_cents,
)
from extensions.expenses.schema import (
    CATEGORIES, already_seen, insert_expense,
)
from privacy.gateway import Gateway

log = logging.getLogger(__name__)


def scan_inbox(
    inbox: Path, db_path: Path, gateway: Gateway,
    *, max_per_tick: int = 5,
) -> int:
    """Verwerk nieuwe PDF's in `inbox`. Returns aantal nieuwe expenses."""
    inbox = inbox.expanduser()
    if not inbox.exists():
        try:
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("kon inbox %s niet aanmaken", inbox)
            return 0
        return 0

    added = 0
    for path in sorted(inbox.glob("*.pdf"))[:max_per_tick * 2]:
        try:
            content = path.read_bytes()
        except OSError:
            log.warning("kon %s niet lezen", path)
            continue
        if not content:
            continue
        sha = hashlib.sha256(content).hexdigest()

        with sqlite3.connect(db_path, isolation_level=None) as conn:
            if already_seen(conn, source_path=str(path), content_hash=sha):
                continue

        text = extract_text(path)
        if not text.strip():
            log.info("expenses: skip %s (geen tekst geëxtraheerd)", path.name)
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                # Insert empty record zodat we niet elke tick opnieuw proberen.
                insert_expense(
                    conn, source_path=str(path), content_hash=sha,
                    vendor=None, receipt_date=None,
                    amount_cents=None, vat_cents=None, currency="EUR",
                    category=None,
                    description="(no text extracted from PDF)",
                    raw_text="", confidence=0.0,
                )
            continue

        try:
            data = classify(text, gateway=gateway, source_filename=path.name)
        except Exception:
            log.exception("expenses: classify failed voor %s", path.name)
            continue

        if not data.get("is_receipt", True):
            log.info("expenses: %s gemarkeerd als geen bon — skip", path.name)
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                insert_expense(
                    conn, source_path=str(path), content_hash=sha,
                    vendor=None, receipt_date=None,
                    amount_cents=None, vat_cents=None, currency="EUR",
                    category=None,
                    description="(classifier: not a receipt)",
                    raw_text=text, confidence=float(data.get("confidence") or 0),
                )
            continue

        category = data.get("category")
        if category not in CATEGORIES:
            category = "other"

        with sqlite3.connect(db_path, isolation_level=None) as conn:
            rid = insert_expense(
                conn, source_path=str(path), content_hash=sha,
                vendor=data.get("vendor"),
                receipt_date=parse_date(data.get("receipt_date")),
                amount_cents=to_cents(data.get("amount")),
                vat_cents=to_cents(data.get("vat")) or 0,
                currency=str(data.get("currency") or "EUR")[:3].upper(),
                category=category,
                description=str(data.get("description") or "")[:500],
                raw_text=text,
                confidence=float(data.get("confidence") or 0),
            )
        if rid:
            added += 1
            log.info(
                "expenses: ingest %s — vendor=%s, %s%.2f, cat=%s",
                path.name, data.get("vendor"),
                str(data.get("currency") or "EUR"),
                float(data.get("amount") or 0), category,
            )
        if added >= max_per_tick:
            break
    return added
