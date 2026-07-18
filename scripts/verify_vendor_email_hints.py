"""Controleer of de `email_query_hint` van elke email-strategy ook
daadwerkelijk mails matcht in Gmail. you kan een verkeerd
afzender-adres in zijn 'hoe-gevonden' sheet hebben staan (bv. Datadog
"system@sent-via.netsuite.com" — een delivery-server, terwijl de
visible-from "billing@datadoghq.com" is). Dit script:

1. Pakt elke source_kind=email strategy met een `from:...` hint.
2. Telt Gmail-hits in laatste 365 dagen voor die hint.
3. Als 0 hits: zoekt op vendor-naam in body, sniff dominant afzender,
   print correctie-suggestie.
4. Met --apply: schrijft de correctie weg in vendor_strategies.

Skipt internal-domain afzenders (forwards van you zelf).

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/verify_vendor_email_hints.py --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/verify_vendor_email_hints.py --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.receipt_collector.matcher import _clean_vendor_for_search
from extensions.receipt_collector.schema import (
    list_vendor_strategies, upsert_vendor_strategy,
)
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials


_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_BARE_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")

INTERNAL_DOMAINS: set[str] = set()


def _load_internal_domains() -> None:
    """Lazy — module-import mag niet load_settings() aanroepen. Zie
    code-review M4."""
    global INTERNAL_DOMAINS
    try:
        from core.config import load_settings
        INTERNAL_DOMAINS = set(load_settings().own_email_domains or ())
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "cannot load own_email_domains: %s", exc,
        )


def parse_email(raw: str) -> str | None:
    m = _EMAIL_RE.search(raw or "")
    if m:
        return m.group(1).lower()
    m = _BARE_EMAIL_RE.search(raw or "")
    return m.group(1).lower() if m else None


def hint_from_addr(hint: str) -> str | None:
    """Extract '<addr>' uit 'from:<addr>'."""
    if not hint or not hint.lower().startswith("from:"):
        return None
    return hint[5:].strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true",
                    help="Schrijf correcties daadwerkelijk weg")
    ap.add_argument("--days", type=int, default=365,
                    help="Test-window voor Gmail-search")
    args = ap.parse_args()
    apply = args.apply  # alleen bij --apply daadwerkelijk schrijven

    _load_internal_domains()  # lazy: script-run tijd, niet module-load

    settings = load_settings()
    creds = get_credentials(settings.google_credentials_path,
                              settings.google_token_path)
    gmail = GmailClient(creds)

    after_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y/%m/%d")

    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        all_strats = list_vendor_strategies(c)

    email_strats = [s for s in all_strats
                     if s["source_kind"] == "email"
                     and s.get("email_query_hint")
                     and s["email_query_hint"].lower().startswith("from:")]
    print(f"\n{len(email_strats)} email-strategies met from:-hint te checken")

    suggestions = []
    for s in email_strats:
        name = s["name"]
        hint = s["email_query_hint"]
        addr = hint_from_addr(hint)
        if not addr:
            continue

        # Test: hoeveel mails matcht deze hint in laatste N dagen?
        try:
            hits = gmail.search(query=f"{hint} after:{after_date}",
                                  max_results=5)
        except Exception as e:
            print(f"\n  [error] {name}: search failed ({e})")
            continue

        if hits:
            print(f"  ✓ {name:30}  {hint}  ({len(hits)} hits)")
            continue

        # 0 hits — probeer vendor-naam in body
        clean = _clean_vendor_for_search(name) or name
        try:
            results = gmail.search(
                query=f'"{clean}" after:{after_date}',
                max_results=10,
            )
        except Exception:
            results = []
        senders = []
        for r in results:
            sender = parse_email(r.get("from", ""))
            if not sender:
                continue
            domain = sender.split("@", 1)[-1]
            if domain in INTERNAL_DOMAINS:
                continue
            senders.append(sender)
        if not senders:
            print(f"  ⚠ {name:30}  {hint}  (0 hits, geen alternatief gevonden)")
            continue
        most_common, count = Counter(senders).most_common(1)[0]
        if most_common == addr:
            print(f"  ⚠ {name:30}  hint already correct but 0 hits — false alarm")
            continue
        new_hint = f"from:{most_common}"
        print(f"  → {name:30}  CORRECT: {hint}  →  {new_hint}  "
               f"({count}/{len(senders)} mails)")
        suggestions.append({
            "name": name,
            "old_hint": hint,
            "new_hint": new_hint,
            "evidence_count": count,
            "strategy_row": s,
        })

    print(f"\n{len(suggestions)} correcties voorgesteld")

    if not apply:
        print("\nRun met --apply om correcties weg te schrijven.")
        return 0

    with sqlite3.connect(settings.db_path, isolation_level=None) as c:
        for sug in suggestions:
            row = sug["strategy_row"]
            upsert_vendor_strategy(
                c, name=row["name"],
                source_kind=row["source_kind"],
                aliases=row.get("aliases") or [],
                email_query_hint=sug["new_hint"],
                portal_url=row.get("portal_url"),
                portal_notes=row.get("portal_notes"),
            )
    print(f"applied: {len(suggestions)} correcties")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
