"""One-shot bulk-ingest van mail-history naar comm_items.

Verschilt van de doorlopende ingest-loop in `extensions/comm_intel/ingest.py`:
- Pagineert volledig (geen 500-cap per call)
- Skipt Ollama-summarize → veel sneller (raw body in DB)
- Idempotent (skipt al-bestaande external_ids)
- Kan tot N jaar terug

Doel: Rosa kan analyses doen op meerjarige contact-historie via
`comm_about_person` / `comm_search`. Voor pure analytics is summary
niet nodig — body_full + headers staan in DB en de tools matchen
daar al op.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/historical_backfill.py \\
        --days 1825 --source all --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/historical_backfill.py \\
        --days 1825 --source gmail
    PYTHONPATH=src ./venv/bin/python scripts/historical_backfill.py \\
        --days 1825 --source imap --account mymail
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.comm_intel.schema import (
    CommItem, insert_item, item_exists,
)
from extensions.comm_intel.sources.gmail_source import (
    _extract_text, _to_comm_item,
)
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from integrations.imap import all_enabled as imap_all_enabled

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")
TZ = ZoneInfo("Europe/Amsterdam")


def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def backfill_gmail(
    gmail: GmailClient, since_unix: int, db_path: Path, *,
    batch_size: int = 200, dry_run: bool = False,
) -> dict:
    since_dt = datetime.fromtimestamp(since_unix, TZ)
    after_str = since_dt.strftime("%Y/%m/%d")
    stats = {"seen": 0, "new": 0, "skipped": 0, "errors": 0}

    for direction, q_base in (("in", "in:inbox -in:chats"),
                                ("out", "in:sent -in:chats")):
        query = f"{q_base} after:{after_str}"
        log.info("Gmail/%s: query=%s", direction, query)
        page_token: str | None = None
        page = 0
        while True:
            page += 1
            try:
                resp = gmail._service.users().messages().list(  # type: ignore[attr-defined]
                    userId="me", maxResults=batch_size, q=query,
                    pageToken=page_token,
                ).execute()
            except Exception:
                log.exception("Gmail list failed (direction=%s page=%d)",
                                direction, page)
                stats["errors"] += 1
                break
            ids = [m["id"] for m in resp.get("messages", []) or []]
            for msg_id in ids:
                stats["seen"] += 1
                with _conn(db_path) as c:
                    if item_exists(c, source="gmail", account="gmail",
                                    external_id=msg_id):
                        stats["skipped"] += 1
                        continue
                if dry_run:
                    stats["new"] += 1
                    continue
                try:
                    m = gmail._service.users().messages().get(  # type: ignore[attr-defined]
                        userId="me", id=msg_id, format="full",
                    ).execute()
                except Exception:
                    log.exception("Gmail get %s failed", msg_id)
                    stats["errors"] += 1
                    continue
                d = {
                    "id": m["id"],
                    "thread_id": m.get("threadId"),
                    "internal_date_ms": int(m.get("internalDate", 0) or 0),
                    "from": _header(m, "from"),
                    "to": _header(m, "to"),
                    "cc": _header(m, "cc"),
                    "subject": _header(m, "subject"),
                    "body": _extract_text(m.get("payload", {})),
                    "direction": direction,
                }
                try:
                    item = _to_comm_item(d)
                except Exception:
                    log.exception("to_comm_item failed for %s", msg_id)
                    stats["errors"] += 1
                    continue
                with _conn(db_path) as c:
                    if insert_item(c, item) is not None:
                        stats["new"] += 1
                if stats["seen"] % 100 == 0:
                    log.info("  Gmail/%s: seen=%d new=%d dup=%d err=%d",
                              direction, stats["seen"], stats["new"],
                              stats["skipped"], stats["errors"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        log.info("Gmail/%s done: seen=%d new=%d dup=%d err=%d",
                  direction, stats["seen"], stats["new"],
                  stats["skipped"], stats["errors"])
    return stats


def backfill_imap(
    db_path: Path, imap_yaml: Path, *,
    since_unix: int, dry_run: bool = False,
    only_account: str | None = None,
) -> dict:
    from imap_tools import AND, MailBox, MailBoxUnencrypted
    since_date = datetime.fromtimestamp(since_unix, TZ).date()

    stats = {"seen": 0, "new": 0, "skipped": 0, "errors": 0}
    for account, password in imap_all_enabled(imap_yaml):
        if only_account and account.name != only_account:
            continue
        cls = MailBox if account.ssl else MailBoxUnencrypted
        for folder, direction in ((account.folders.inbox, "in"),
                                    (account.folders.sent, "out")):
            log.info("IMAP %s/%s (direction=%s) since %s",
                      account.name, folder, direction, since_date)
            try:
                with cls(account.host, port=account.port).login(
                    account.username, password,
                ) as mb:
                    mb.folder.set(folder)
                    for msg in mb.fetch(AND(date_gte=since_date),
                                         mark_seen=False, bulk=True):
                        stats["seen"] += 1
                        uid = str(msg.uid or "")
                        if not uid:
                            stats["errors"] += 1
                            continue
                        with _conn(db_path) as c:
                            if item_exists(c, source="imap",
                                            account=account.name,
                                            external_id=uid):
                                stats["skipped"] += 1
                                continue
                        if dry_run:
                            stats["new"] += 1
                            continue
                        occurred = int(msg.date.timestamp()) if msg.date else 0
                        thread_ref = ""
                        try:
                            thread_ref = (msg.headers.get("references", [""])[0]
                                          or msg.headers.get("in-reply-to", [""])[0]
                                          or "")
                        except Exception:
                            pass
                        item = CommItem(
                            source="imap",
                            account=account.name,
                            external_id=uid,
                            folder=folder,
                            direction=direction,
                            from_addr=msg.from_ or "",
                            to_addrs=list(msg.to or []),
                            cc_addrs=list(msg.cc or []),
                            subject=msg.subject or "",
                            occurred_at=occurred,
                            body_full=(msg.text or msg.html or "").strip(),
                            thread_ref=thread_ref or None,
                            raw_meta={
                                "message_id": (msg.headers.get("message-id", [""])[0]
                                               if hasattr(msg, "headers") else "")
                            },
                        )
                        with _conn(db_path) as c:
                            if insert_item(c, item) is not None:
                                stats["new"] += 1
                        if stats["seen"] % 200 == 0:
                            log.info("  IMAP %s/%s: seen=%d new=%d dup=%d",
                                      account.name, folder, stats["seen"],
                                      stats["new"], stats["skipped"])
            except Exception:
                log.exception("IMAP %s/%s failed", account.name, folder)
                stats["errors"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1825,
                    help="Hoeveel dagen terug (default 1825 = 5 jaar)")
    ap.add_argument("--source", choices=["gmail", "imap", "all"],
                    default="all")
    ap.add_argument("--account", help="Alleen één IMAP-account (bv. mymail)")
    ap.add_argument("--batch-size", type=int, default=200,
                    help="Gmail messages per page (max 500)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Tel hoeveel mails er zouden komen, schrijf niets weg")
    args = ap.parse_args()

    settings = load_settings()
    config_dir = settings.data_dir.parent / "config"
    imap_yaml = config_dir / "imap_accounts.yaml"
    since_unix = int(time.time()) - args.days * 86400
    log.info("Backfill start: %d days back (since %s), source=%s, dry-run=%s",
              args.days,
              datetime.fromtimestamp(since_unix, TZ).date(),
              args.source, args.dry_run)

    overall = {"seen": 0, "new": 0, "skipped": 0, "errors": 0}

    if args.source in ("gmail", "all"):
        try:
            creds = get_credentials(settings.google_credentials_path,
                                      settings.google_token_path)
            gmail = GmailClient(creds)
            s = backfill_gmail(gmail, since_unix, settings.db_path,
                                batch_size=args.batch_size, dry_run=args.dry_run)
            for k in overall:
                overall[k] += s[k]
        except Exception:
            log.exception("Gmail backfill failed")

    if args.source in ("imap", "all"):
        if imap_yaml.exists():
            s = backfill_imap(settings.db_path, imap_yaml,
                                since_unix=since_unix, dry_run=args.dry_run,
                                only_account=args.account)
            for k in overall:
                overall[k] += s[k]
        else:
            log.warning("imap_accounts.yaml not found — skipping IMAP")

    log.info("DONE: seen=%d new=%d dup=%d err=%d",
              overall["seen"], overall["new"],
              overall["skipped"], overall["errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
