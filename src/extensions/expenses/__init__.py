"""Expense / receipt tracker.

Drop-folder pattern (zelfde als Plaud): the user gooit factuur/bon-PDF
in `~/PA-Receipts/` (configureerbaar). Scheduler-tick scant elke 60s
op nieuwe PDFs:
  1. SHA-256 dedup (zelfde file 2× negeert)
  2. PDF text extraction via pypdf
  3. Claude classify: vendor, datum, bedrag (excl/incl BTW), categorie
  4. Insert in `expenses` tabel
  5. Originele PDF blijft staan voor de boekhouder

Tool `recent_expenses(days, category?)` voor query. Maandelijkse CSV-
export via `scripts/expenses_export.py`.
"""
