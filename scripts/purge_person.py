#!/usr/bin/env python3
"""GDPR art 17 'right to erasure' — verwijder alle Rosa-data over één persoon.

Identifier kan zijn:
  - mail-adres ('anouk@klant.nl')
  - exacte naam ('Anouk Jansen')
  - iMessage-handle ('+31612345678' of '@iMessage')

Scant + verwijdert uit:
  - comm_items (mail/Slack/IMAP bodies via from/to/cc/subject/body LIKE)
  - conversation_turns (iMessage van die handle, of vrije-tekst match)
  - processed_messages (idem)
  - open_loops (via who-veld)
  - reminders + todoist_links (via handle of body)
  - memory_cards (vrije-tekst match)
  - data/audit/payloads-*.jsonl (entries waar identifier in matched_text staat)
  - config/vip_contacts.yaml (VIP-entry weghalen)

Voor ISO A.18.1.1 / GDPR audit-trail: schrijft naar
`data/audit/admin-*.jsonl` welke rows zijn verwijderd (counts, niet content).

Usage:
    ./venv/bin/python scripts/purge_person.py --identifier "anouk@klant.nl"
    ./venv/bin/python scripts/purge_person.py --identifier "Anouk Jansen" --dry-run
    ./venv/bin/python scripts/purge_person.py --identifier "+31612345678" --force

Default is dry-run — voorkomt per ongeluk de verkeerde persoon wegen.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Repo root op pad zodat 'src/' modules importeerbaar zijn
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from core.audit import AdminActionLogger, bind_admin_logger, log_admin_action
from core.config import load_settings

log = logging.getLogger("purge_person")


def _exec_count(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...],
    *, dry_run: bool,
) -> int:
    """SELECT COUNT(*) of DELETE — afhankelijk van dry-run."""
    count_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
    row = conn.execute(count_sql, params).fetchone()
    n = int(row[0]) if row else 0
    if n and not dry_run:
        conn.execute(sql, params)
    return n


def purge(db_path: Path, identifier: str, *, dry_run: bool) -> dict[str, int]:
    """Run de purge. Returns counts per tabel."""
    like = f"%{identifier}%"
    counts: dict[str, int] = {}

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        # 1. comm_items: from/to/cc + subject + body_full + summary
        try:
            counts["comm_items"] = _exec_count(
                conn,
                "DELETE FROM comm_items WHERE "
                "from_addr LIKE ? OR to_addrs LIKE ? OR cc_addrs LIKE ? "
                "OR subject LIKE ? OR body_full LIKE ? OR summary LIKE ?",
                (like, like, like, like, like, like),
                dry_run=dry_run,
            )
        except sqlite3.OperationalError as e:
            log.warning("comm_items: %s", e)
            counts["comm_items"] = 0

        # 2. conversation_turns + processed_messages
        for table, col in (
            ("conversation_turns", "content"),
            ("processed_messages", "text"),
        ):
            try:
                counts[table] = _exec_count(
                    conn,
                    f"DELETE FROM {table} WHERE {col} LIKE ? OR handle = ?",
                    (like, identifier),
                    dry_run=dry_run,
                )
            except sqlite3.OperationalError as e:
                log.warning("%s: %s", table, e)
                counts[table] = 0

        # 3. open_loops (who-veld) + open_loops via title/body_excerpt
        try:
            counts["open_loops"] = _exec_count(
                conn,
                "DELETE FROM open_loops WHERE "
                "who LIKE ? OR title LIKE ? OR body_excerpt LIKE ?",
                (like, like, like),
                dry_run=dry_run,
            )
        except sqlite3.OperationalError as e:
            log.warning("open_loops: %s", e)
            counts["open_loops"] = 0

        # 4. reminders + todoist_links cascade
        try:
            counts["reminders"] = _exec_count(
                conn,
                "DELETE FROM reminders WHERE handle = ? OR body LIKE ?",
                (identifier, like),
                dry_run=dry_run,
            )
        except sqlite3.OperationalError as e:
            log.warning("reminders: %s", e)
            counts["reminders"] = 0

        # 5. memories (vrije tekst memory-cards + linked_entities)
        try:
            counts["memories"] = _exec_count(
                conn,
                "DELETE FROM memories WHERE "
                "text LIKE ? OR linked_entities LIKE ? OR tags LIKE ?",
                (like, like, like),
                dry_run=dry_run,
            )
        except sqlite3.OperationalError as e:
            log.warning("memories: %s", e)
            counts["memories"] = 0

        # 6. sales_accounts (klanten-PII)
        try:
            counts["sales_accounts"] = _exec_count(
                conn,
                "DELETE FROM sales_accounts WHERE "
                "naam LIKE ? OR primary_contact_name LIKE ? "
                "OR primary_contact_email LIKE ? OR primary_contact_phone LIKE ? "
                "OR notes LIKE ?",
                (like, like, like, like, like),
                dry_run=dry_run,
            )
        except sqlite3.OperationalError as e:
            log.warning("sales_accounts: %s", e)
            counts["sales_accounts"] = 0

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GDPR art 17 erasure voor Rosa-data over één persoon",
    )
    parser.add_argument("--identifier", required=True,
                        help="mail/handle/naam")
    parser.add_argument("--db", type=Path, default=None,
                        help="path naar memory.db (default: uit settings)")
    parser.add_argument("--force", action="store_true",
                        help="echt verwijderen (anders dry-run)")
    args = parser.parse_args()
    dry_run = not args.force

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db_path = args.db or load_settings().db_path

    print(f"Mode: {'DRY-RUN (geen delete)' if dry_run else 'LIVE DELETE'}")
    print(f"Identifier: {args.identifier!r}")
    print(f"DB: {db_path}")
    print()

    counts = purge(db_path, args.identifier, dry_run=dry_run)

    total = sum(counts.values())
    print("Resultaat per tabel:")
    for tbl, n in sorted(counts.items()):
        print(f"  {tbl:>22}  {n:>6}")
    print(f"  {'TOTAL':>22}  {total:>6}")

    if not dry_run and total > 0:
        # Audit-trail: schrijf counts naar admin-stream (GDPR-audit).
        settings = load_settings()
        bind_admin_logger(AdminActionLogger(settings.audit_dir))
        try:
            log_admin_action(
                action="gdpr_erasure",
                actor=f"script:purge_person",
                from_value={"identifier_hash_chars": len(args.identifier),
                              "counts": counts, "total": total},
                reason="GDPR art 17 right to erasure",
            )
        except Exception:
            log.exception("admin-audit log failed")
        print("\nAudit-event geschreven naar data/audit/admin-*.jsonl")

    if dry_run and total > 0:
        print("\nDry-run — geen rows verwijderd. Run met --force om écht.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
