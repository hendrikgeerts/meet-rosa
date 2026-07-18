"""Stuur een vriendelijke reply naar leveranciers die wel een factuur-mail
sturen maar zonder PDF-attachment, met het verzoek om voortaan een PDF
mee te sturen. Identificeert kandidaten op basis van een specifieke run:

- gevonden matches met attachment_path beginnend met 'email-evidence-'
  (= door Fase 2A gerenderde evidence-PDF — origineel had geen PDF)

Anti-spam: per `vendor_name` max 1 verzoek per 90 dagen via
`pdf_request_sent` tabel.

Usage:
    # Lijst kandidaten + concept-replies (geen mail verzonden):
    PYTHONPATH=src ./venv/bin/python scripts/request_invoice_pdf.py \\
        --run-id 7 --dry-run

    # Echt versturen voor één vendor:
    PYTHONPATH=src ./venv/bin/python scripts/request_invoice_pdf.py \\
        --run-id 7 --send Datadog
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from time import time as _now

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.receipt_collector.schema import list_run_items
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials


COOLDOWN_SECONDS = 90 * 24 * 3600  # 90 days

NL_DOMAINS = (".nl", ".be")

TEMPLATE_NL = """\
Beste,

Voor onze administratie hebben wij de factuur graag als PDF-bijlage
in plaats van enkel in de mailbody. Kunt u de factuur van {date}
(€{amount:.2f}) als PDF-bestand nasturen?

Bij voorbaat dank,
[Your Name]
YourCompany
"""

TEMPLATE_EN = """\
Hi,

For our accounting we need invoices as a proper PDF attachment rather
than only in the email body. Could you resend the invoice from {date}
(EUR {amount:.2f}) as a PDF file?

Many thanks,
[Your Name]
YourCompany
"""


def is_dutch_recipient(addr: str) -> bool:
    addr_l = addr.lower()
    return any(addr_l.endswith(d) or (d + ">") in addr_l for d in NL_DOMAINS)


def parse_email_address(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"<([^<>@\s]+@[^<>@\s]+)>", raw)
    if m:
        return m.group(1).lower().strip()
    m = re.search(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", raw)
    return m.group(1).lower() if m else None


def is_evidence_match(item: dict) -> bool:
    """Match was via gerenderde email-PDF (Fase 2A) — origineel had geen PDF.
    Dat is precies onze kandidatengroep."""
    if item.get("status") != "matched":
        return False
    path = item.get("attachment_path") or ""
    return path.startswith("email-evidence-")


def recently_requested(conn: sqlite3.Connection, vendor_name: str) -> bool:
    cutoff = int(_now()) - COOLDOWN_SECONDS
    row = conn.execute(
        "SELECT sent_at FROM pdf_request_sent WHERE vendor_name=? "
        "AND sent_at > ? ORDER BY sent_at DESC LIMIT 1",
        (vendor_name, cutoff),
    ).fetchone()
    return row is not None


def build_concept(item: dict, recipient: str) -> tuple[str, str]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Amsterdam")
    d = datetime.fromtimestamp(item["transaction_date"], tz).date().isoformat()
    amt = abs(item["amount_cents"]) / 100.0
    template = TEMPLATE_NL if is_dutch_recipient(recipient) else TEMPLATE_EN
    body = template.format(date=d, amount=amt)
    subject = (f"Verzoek: factuur als PDF aub — {d}"
               if template is TEMPLATE_NL
               else f"Request: invoice as PDF attachment — {d}")
    return subject, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Lijst alle kandidaten + concept-replies, niet versturen")
    ap.add_argument("--send", metavar="VENDOR",
                    help="Verstuur voor één vendor (canonical name)")
    args = ap.parse_args()

    settings = load_settings()
    creds = get_credentials(
        settings.google_credentials_path, settings.google_token_path,
    )
    gmail = GmailClient(creds)

    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        items = list_run_items(c, args.run_id)

    candidates = [it for it in items if is_evidence_match(it)]
    if not candidates:
        print(f"Run {args.run_id}: geen evidence-matches om aan te schrijven.")
        return 0

    # Group by vendor (canonical), pick one representative item each
    by_vendor: dict[str, dict] = {}
    for it in candidates:
        vendor = (it.get("vendor_canonical") or it["vendor_raw"]).strip()
        # Prefer the largest amount as the representative (most likely to be
        # taken seriously by accounts dept.)
        cur = by_vendor.get(vendor)
        if cur is None or abs(it["amount_cents"]) > abs(cur["amount_cents"]):
            by_vendor[vendor] = it

    print(f"Run {args.run_id}: {len(candidates)} evidence-matches "
          f"verdeeld over {len(by_vendor)} vendors")

    plans: list[tuple[str, str, str, str]] = []  # (vendor, recipient, subj, body)
    with sqlite3.connect(settings.db_path) as c:
        for vendor, it in by_vendor.items():
            if recently_requested(c, vendor):
                print(f"\n[skip] {vendor}: <90 dagen geleden al verzoek gestuurd")
                continue
            # Need the message-id to fetch sender of the matched mail
            msg_id = it.get("source_message_id")
            if not msg_id:
                print(f"\n[skip] {vendor}: geen source_message_id")
                continue
            try:
                full = gmail.get_message_full(msg_id)
            except Exception as e:
                print(f"\n[skip] {vendor}: gmail fetch failed ({e})")
                continue
            headers = {h["name"].lower(): h["value"]
                       for h in (full.get("payload", {}).get("headers") or [])}
            sender = parse_email_address(headers.get("from", ""))
            if not sender:
                print(f"\n[skip] {vendor}: geen afzender uit From-header")
                continue
            subject, body = build_concept(it, sender)
            plans.append((vendor, sender, subject, body))
            print(f"\n--- {vendor}  →  {sender} ---")
            print(f"Subject: {subject}")
            print(body)

    if not plans:
        print("\nGeen kandidaten over om te sturen.")
        return 0

    if args.dry_run:
        print(f"\n[dry-run] {len(plans)} replies kunnen verzonden worden.")
        print("Run met --send <VENDOR> voor één concrete reply.")
        return 0

    if not args.send:
        print("\nEnable --dry-run of geef --send <VENDOR> op om actie te nemen.")
        return 1

    target = args.send
    plan = next((p for p in plans if p[0] == target), None)
    if plan is None:
        print(f"vendor '{target}' niet in plannen-lijst", file=sys.stderr)
        return 1

    vendor, recipient, subject, body = plan
    print(f"\nVerzenden naar {recipient} voor vendor {vendor}…")
    sent = gmail.send(to=recipient, subject=subject, body=body)
    print(f"verzonden: gmail msg-id {sent['id']}")

    with sqlite3.connect(settings.db_path, isolation_level=None) as c:
        c.execute(
            "INSERT INTO pdf_request_sent("
            "  vendor_name, to_address, subject, body_excerpt, gmail_message_id"
            ") VALUES (?, ?, ?, ?, ?)",
            (vendor, recipient, subject, body[:300], sent["id"]),
        )
    print(f"opgeslagen in pdf_request_sent (90 dagen cooldown actief).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
