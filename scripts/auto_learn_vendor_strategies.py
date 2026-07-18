"""Voor elke matched item in een receipt-run: sniff de afzender van de
gevonden mail en upsert een vendor_strategy zodat een volgende run direct
de juiste mail aanwijst.

Skipt amount-only review-matches (afzender niet betrouwbaar gekoppeld
aan vendor) en strategies die al een email_query_hint hebben.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/auto_learn_vendor_strategies.py \\
        --run-id 7 --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/auto_learn_vendor_strategies.py \\
        --run-id 7
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.receipt_collector.matcher import _clean_vendor_for_search
from extensions.receipt_collector.schema import (
    find_vendor_strategy, list_run_items, upsert_vendor_strategy,
)
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from integrations.imap import all_enabled as imap_all_enabled


_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_BARE_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")

# Eigen-domeinen: forwards van jezelf of collega's (zelfde bedrijf)
# zijn geen vendor-bron. De factuur is dan wel via de mailbox gevonden,
# maar de afzender is niet bruikbaar als email_query_hint.
# Lazy-loaded — module-level load_settings() zou pytest-collection
# breken op systemen zonder config.yaml (zie code-review M4).
INTERNAL_DOMAINS: set[str] = set()


def _load_internal_domains() -> None:
    """Roep dit aan in main() van je script — niet bij module-import."""
    global INTERNAL_DOMAINS
    try:
        from core.config import load_settings
        settings = load_settings()
        INTERNAL_DOMAINS = set(settings.own_email_domains or ())
    except Exception as exc:
        log = logging.getLogger(__name__)
        log.warning("cannot load own_email_domains: %s", exc)


def parse_email_address(raw: str) -> str | None:
    if not raw:
        return None
    m = _EMAIL_RE.search(raw)
    if m:
        return m.group(1).lower().strip()
    m = _BARE_EMAIL_RE.search(raw)
    return m.group(1).lower() if m else None


def fetch_sender(item: dict, *, gmail, imap_pairs: list) -> str | None:
    src = item.get("matched_via") or ""
    msg_id = item.get("source_message_id")
    if not msg_id:
        return None

    if src == "gmail":
        try:
            msg = gmail.get_message_full(msg_id)
        except Exception as e:
            print(f"  ! gmail fetch failed for {msg_id}: {e}", file=sys.stderr)
            return None
        headers = {h["name"].lower(): h["value"]
                   for h in (msg.get("payload", {}).get("headers") or [])}
        return parse_email_address(headers.get("from", ""))

    if src.startswith("imap:"):
        account_name = src.split(":", 1)[1]
        match = next(((a, pw) for a, pw in imap_pairs
                      if a.name == account_name), None)
        if match is None:
            print(f"  ! imap account not found: {account_name}", file=sys.stderr)
            return None
        account, password = match
        try:
            from imap_tools import AND, MailBox, MailBoxUnencrypted
            cls = MailBox if account.ssl else MailBoxUnencrypted
            with cls(account.host, port=account.port).login(
                account.username, password,
            ) as mb:
                msgs = list(mb.fetch(AND(uid=msg_id), mark_seen=False))
                if msgs:
                    return parse_email_address(msgs[0].from_ or "")
        except Exception as e:
            print(f"  ! imap fetch failed for {msg_id}: {e}", file=sys.stderr)
        return None
    return None


def derive_vendor_name(item: dict) -> str:
    """Canonical name voor de strategy. Prefer vendor_canonical (als al
    matched via bestaande strategy), anders cleaned vendor_raw."""
    canonical = (item.get("vendor_canonical") or "").strip()
    if canonical:
        return canonical
    cleaned = _clean_vendor_for_search(item["vendor_raw"]).strip()
    if cleaned:
        return cleaned
    return item["vendor_raw"][:40]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    _load_internal_domains()  # lazy: script-run tijd, niet module-load

    settings = load_settings()
    config_dir = settings.data_dir.parent / "config"
    imap_yaml = config_dir / "imap_accounts.yaml"
    imap_pairs = list(imap_all_enabled(imap_yaml)) if imap_yaml.exists() else []

    creds = get_credentials(
        settings.google_credentials_path, settings.google_token_path,
    )
    gmail = GmailClient(creds)

    with sqlite3.connect(settings.db_path) as c:
        items = list_run_items(c, args.run_id)

    matched = [i for i in items
                if i["status"] == "matched"
                and "amount-only match" not in (i.get("notes") or "")]

    print(f"Run {args.run_id}: {len(matched)} matched items eligible "
          f"(amount-only review-matches excluded)")

    suggestions = []
    for it in matched:
        sender = fetch_sender(it, gmail=gmail, imap_pairs=imap_pairs)
        if not sender:
            print(f"  - skip {it['vendor_raw'][:50]}: geen afzender")
            continue
        sender_domain = sender.split("@", 1)[-1].lower()
        if sender_domain in INTERNAL_DOMAINS:
            print(f"  - skip {it['vendor_raw'][:40]}: forward via "
                   f"intern domein ({sender})")
            continue
        vendor_name = derive_vendor_name(it)
        aliases = [it["vendor_raw"]]
        clean = _clean_vendor_for_search(it["vendor_raw"])
        if clean and clean.lower() != it["vendor_raw"].lower():
            aliases.append(clean)
        suggestions.append({
            "vendor_raw": it["vendor_raw"],
            "name": vendor_name,
            "email_query_hint": f"from:{sender}",
            "aliases": aliases,
        })
        print(f"  + {vendor_name:30}  from:{sender}")

    if args.dry_run:
        print(f"\n[dry-run] {len(suggestions)} strategies would be upserted.")
        return 0

    written = 0
    skipped = 0
    with sqlite3.connect(settings.db_path, isolation_level=None) as c:
        for s in suggestions:
            existing = find_vendor_strategy(c, vendor_text=s["vendor_raw"])
            if existing and existing.get("email_query_hint"):
                # Bestaande hint niet overschrijven — handmatige config respecteren
                skipped += 1
                continue
            upsert_vendor_strategy(
                c,
                name=s["name"],
                source_kind="email",
                aliases=s["aliases"],
                email_query_hint=s["email_query_hint"],
                portal_notes=f"auto-learned from run {args.run_id}",
            )
            written += 1
    print(f"\nupserted: {written}, skipped (already had hint): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
