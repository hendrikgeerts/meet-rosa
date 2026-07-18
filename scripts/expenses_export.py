#!/usr/bin/env python3
"""Maandelijkse CSV-export van expenses voor de boekhouder.

Gebruik:
    ./venv/bin/python scripts/expenses_export.py [YYYY-MM]

Default = vorige kalendermaand. CSV verschijnt in `data/expenses/`.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from extensions.expenses.schema import list_for_period  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")


def _month_bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=TZ)
    else:
        end = datetime(year, month + 1, 1, tzinfo=TZ)
    return int(start.timestamp()), int(end.timestamp())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("month", nargs="?", default=None,
                        help="YYYY-MM (default: vorige kalendermaand)")
    args = parser.parse_args()

    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
    else:
        now = datetime.now(TZ)
        first_of_this_month = datetime.combine(now.date().replace(day=1), time(0, 0), tzinfo=TZ)
        prev = first_of_this_month - timedelta(days=1)
        year, month = prev.year, prev.month

    start_unix, end_unix = _month_bounds(year, month)
    db_path = REPO_ROOT / "data" / "memory.db"
    out_dir = REPO_ROOT / "data" / "expenses"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"expenses-{year:04d}-{month:02d}.csv"

    with sqlite3.connect(db_path) as conn:
        rows = list_for_period(conn, start_unix=start_unix, end_unix=end_unix)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([
            "datum", "vendor", "bedrag_incl", "btw", "valuta",
            "categorie", "omschrijving", "bron_pdf",
        ])
        for r in rows:
            d = datetime.fromtimestamp(r["receipt_date"] or r["processed_at"], TZ)
            w.writerow([
                d.date().isoformat(),
                r["vendor"] or "",
                f"{(r['amount_cents'] or 0) / 100:.2f}",
                f"{(r['vat_cents'] or 0) / 100:.2f}",
                r["currency"] or "EUR",
                r["category"] or "",
                r["description"] or "",
                Path(r["source_path"]).name,
            ])

    total_incl = sum((r["amount_cents"] or 0) for r in rows) / 100
    print(f"✓ {len(rows)} expenses geëxporteerd → {out_path}")
    print(f"  Totaal incl. BTW: €{total_incl:.2f}")


if __name__ == "__main__":
    main()
