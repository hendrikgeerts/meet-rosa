"""Tests voor expenses schema + extract helpers + tools."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from extensions.expenses.extract import _parse_json, classify, parse_date, to_cents
from extensions.expenses.schema import (
    already_seen, init_expenses_schema, insert_expense, list_recent,
    list_for_period, prune_old_expenses,
)
from extensions.expenses.tools import recent_expenses_handler

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "exp.db"
    init_expenses_schema(p)
    return p


# --- helpers --------------------------------------------------------------

def test_parse_date_iso() -> None:
    out = parse_date("2026-04-15")
    assert out is not None
    assert datetime.fromtimestamp(out, TZ).date().isoformat() == "2026-04-15"


def test_parse_date_invalid() -> None:
    assert parse_date(None) is None
    assert parse_date("nonsense") is None


def test_to_cents_rounds() -> None:
    assert to_cents(49.99) == 4999
    assert to_cents(0.10) == 10
    assert to_cents(None) is None
    assert to_cents("not-a-number") is None


def test_parse_json_with_fence() -> None:
    raw = '```json\n{"vendor": "Coolblue", "amount": 49.99}\n```'
    out = _parse_json(raw)
    assert out["vendor"] == "Coolblue"


def test_parse_json_garbage() -> None:
    out = _parse_json("nothing parseable")
    assert out["is_receipt"] is False


# --- schema CRUD ----------------------------------------------------------

def test_insert_and_dedup(db: Path) -> None:
    with sqlite3.connect(db) as c:
        rid = insert_expense(
            c, source_path="/tmp/x.pdf", content_hash="abc",
            vendor="Coolblue", receipt_date=parse_date("2026-04-15"),
            amount_cents=4999, vat_cents=867,
            currency="EUR", category="hardware",
            description="USB-C kabel", raw_text="x", confidence=0.9,
        )
    assert rid is not None
    with sqlite3.connect(db) as c:
        assert already_seen(c, source_path="/tmp/x.pdf", content_hash="abc")
        # Insert opnieuw → UNIQUE constraint → None.
        rid2 = insert_expense(
            c, source_path="/tmp/x.pdf", content_hash="abc",
            vendor="Coolblue", receipt_date=None, amount_cents=None, vat_cents=0,
            currency="EUR", category="other", description=None, raw_text=None,
            confidence=None,
        )
    assert rid2 is None


def test_list_recent_sorts_and_filters(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_expense(c, source_path="/a.pdf", content_hash="a",
                       vendor="A", receipt_date=parse_date("2026-04-10"),
                       amount_cents=1000, vat_cents=210, currency="EUR",
                       category="software", description="x", raw_text=None,
                       confidence=0.9)
        insert_expense(c, source_path="/b.pdf", content_hash="b",
                       vendor="B", receipt_date=parse_date("2026-04-20"),
                       amount_cents=2000, vat_cents=420, currency="EUR",
                       category="hardware", description="y", raw_text=None,
                       confidence=0.9)
        rows = list_recent(c, days=365, category="hardware")
    assert len(rows) == 1
    assert rows[0]["vendor"] == "B"


def test_list_for_period(db: Path) -> None:
    apr_start = int(datetime(2026, 4, 1, tzinfo=TZ).timestamp())
    may_start = int(datetime(2026, 5, 1, tzinfo=TZ).timestamp())
    with sqlite3.connect(db) as c:
        insert_expense(c, source_path="/in.pdf", content_hash="i",
                       vendor="V", receipt_date=parse_date("2026-04-15"),
                       amount_cents=500, vat_cents=0, currency="EUR",
                       category="other", description="x", raw_text=None,
                       confidence=0.5)
        insert_expense(c, source_path="/out.pdf", content_hash="o",
                       vendor="V", receipt_date=parse_date("2026-05-01"),
                       amount_cents=500, vat_cents=0, currency="EUR",
                       category="other", description="x", raw_text=None,
                       confidence=0.5)
        rows = list_for_period(c, start_unix=apr_start, end_unix=may_start)
    assert len(rows) == 1
    assert rows[0]["vendor"] == "V"


# --- recent_expenses tool -------------------------------------------------

def test_recent_expenses_handler_aggregates_total(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_expense(c, source_path="/a.pdf", content_hash="a",
                       vendor="A", receipt_date=None, amount_cents=4999, vat_cents=867,
                       currency="EUR", category="hardware",
                       description="x", raw_text=None, confidence=0.9)
        insert_expense(c, source_path="/b.pdf", content_hash="b",
                       vendor="B", receipt_date=None, amount_cents=1000, vat_cents=0,
                       currency="EUR", category="software",
                       description="y", raw_text=None, confidence=0.9)
    out = recent_expenses_handler(db, {"days": 30})
    assert out["count"] == 2
    assert out["total_amount"] == 59.99


# --- classify (mock gateway) ----------------------------------------------

def test_classify_with_mock_gateway() -> None:
    fake = MagicMock()
    fake.complete.return_value.content = [
        type("B", (), {"type": "text", "text": (
            '{"vendor": "Coolblue", "receipt_date": "2026-04-15", '
            '"amount": 49.99, "vat": 8.67, "currency": "EUR", '
            '"category": "hardware", "description": "USB-C kabel", '
            '"is_receipt": true, "confidence": 0.95}'
        )})
    ]
    out = classify("Factuur Coolblue ...", gateway=fake, source_filename="cb.pdf")
    assert out["vendor"] == "Coolblue"
    assert out["amount"] == 49.99
    assert out["category"] == "hardware"
    # Verifieer dat receipt-content NAAR LOKAAL gaat, niet Claude (HIGH-3 fix).
    assert fake.complete.call_args.kwargs["force_label"] == "confidential"


# --- DB-TTL: prune_old_expenses -----------------------------------------

def test_prune_old_expenses_respects_receipt_date(db: Path) -> None:
    import time as _t
    now = int(_t.time())
    with sqlite3.connect(db) as c:
        insert_expense(
            c, source_path="/tmp/a.pdf", content_hash="h_a", vendor="A",
            receipt_date=now - 3000 * 86400, amount_cents=100,
            vat_cents=0, currency="EUR", category="x",
            description=None, raw_text=None, confidence=0.9,
        )
        insert_expense(
            c, source_path="/tmp/b.pdf", content_hash="h_b", vendor="B",
            receipt_date=now - 100 * 86400, amount_cents=100,
            vat_cents=0, currency="EUR", category="x",
            description=None, raw_text=None, confidence=0.9,
        )
        removed = prune_old_expenses(c, days=2555)  # 7 years
        rest = c.execute("SELECT vendor FROM expenses").fetchall()
    assert removed == 1  # 3000-day-old row beyond 7 years
    assert {r[0] for r in rest} == {"B"}


def test_prune_old_expenses_skips_rows_without_receipt_date(db: Path) -> None:
    """Rows where Claude couldn't extract a date stay (manual review)."""
    with sqlite3.connect(db) as c:
        insert_expense(
            c, source_path="/tmp/x.pdf", content_hash="hx", vendor="X",
            receipt_date=None, amount_cents=100,
            vat_cents=0, currency="EUR", category="x",
            description=None, raw_text=None, confidence=0.5,
        )
        removed = prune_old_expenses(c, days=1)
        cnt = c.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    assert removed == 0
    assert cnt == 1


def test_prune_old_expenses_zero_days_disables(db: Path) -> None:
    import time as _t
    with sqlite3.connect(db) as c:
        insert_expense(
            c, source_path="/tmp/y.pdf", content_hash="hy", vendor="Y",
            receipt_date=int(_t.time()) - 9999 * 86400, amount_cents=1,
            vat_cents=0, currency="EUR", category="x",
            description=None, raw_text=None, confidence=0.5,
        )
        removed = prune_old_expenses(c, days=0)
    assert removed == 0
