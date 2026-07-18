"""Mail the result of a receipt-collection run to yourself (or any address):
matched-PDF attachments + a markdown summary + a CSV of items still to
investigate (needs_portal + unknown_vendor).

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/send_receipt_run_email.py \\
        --run-id 7 \\
        --to you@example.com
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.receipt_collector.schema import (
    get_run, list_run_items, list_vendor_strategies,
)
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials

TZ = ZoneInfo("Europe/Amsterdam")


def _txn_date(item: dict) -> str:
    return datetime.fromtimestamp(item["transaction_date"], TZ).date().isoformat()


def _eur(cents: int) -> str:
    return f"€{abs(cents)/100:.2f}"


def _group_by_vendor(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for it in items:
        key = it.get("vendor_canonical") or it["vendor_raw"]
        grouped.setdefault(key, []).append(it)
    return grouped


def render_body(
    run: dict, matched: list[dict], needs_portal: list[dict],
    unknowns: list[dict], physical: list[dict], ignored: list[dict],
    strategies_by_name: dict[str, dict],
) -> str:
    period = run.get("period_label") or f"run-{run['id']}"
    total = (len(matched) + len(needs_portal) + len(unknowns)
             + len(physical) + len(ignored))
    settings = load_settings()
    first = (settings.user_name or "you").split()[0] or "you"
    lines = [
        f"Hoi {first},",
        "",
        f"Resultaat van receipt-run #{run['id']} ({period}):",
        "",
        f"  - {len(matched):>3} facturen gevonden (als bijlage)",
        f"  - {len(needs_portal):>3} via leverancierportaal op te halen — zie instructies hieronder",
        f"  - {len(physical):>3} fysieke bonnetjes (zelf scannen)",
        f"  - {len(ignored):>3} uitgesloten (test-subscription / interne overboeking)",
        f"  - {len(unknowns):>3} nog uit te zoeken",
        f"  - {total:>3} totaal",
        "",
        "Bijgevoegd:",
        f"  - {len(matched)} PDF-facturen voor je accountant",
        "  - nog-uit-te-zoeken.csv — vul per rij in hoe je 'm gevonden hebt",
        "    zodat ik Rosa slimmer kan maken voor de volgende run.",
        "",
        "Gevonden facturen:",
        "",
    ]
    for it in matched:
        review = " ⚠️ review" if "amount-only match" in (it.get("notes") or "") else ""
        lines.append(
            f"  {_txn_date(it)}  {_eur(it['amount_cents']):>10}  "
            f"{(it.get('vendor_canonical') or it['vendor_raw'])[:40]}{review}"
        )

    if needs_portal:
        lines += ["", "Op te halen via portaal (per leverancier):", ""]
        for vendor, items in sorted(_group_by_vendor(needs_portal).items()):
            total_eur = sum(abs(i["amount_cents"]) for i in items) / 100.0
            lines.append(f"  ▸ {vendor}  ({len(items)} txns, €{total_eur:.2f})")
            strat = strategies_by_name.get(vendor, {})
            url = strat.get("portal_url")
            notes = strat.get("portal_notes")
            if url:
                lines.append(f"      portal: {url}")
            if notes:
                lines.append(f"      hoe:    {notes[:150]}")
            for it in items:
                lines.append(
                    f"      - {_txn_date(it)}  {_eur(it['amount_cents']):>10}"
                )
            lines.append("")

    if physical:
        lines += ["Fysieke bonnetjes (zelf in PA-Receipts scannen):", ""]
        for vendor, items in sorted(_group_by_vendor(physical).items()):
            total_eur = sum(abs(i["amount_cents"]) for i in items) / 100.0
            lines.append(f"  ▸ {vendor}  ({len(items)} txns, €{total_eur:.2f})")
        lines.append("")

    lines += ["Groeten,", "Rosa"]
    return "\n".join(lines)


def write_unknowns_csv(items: list[dict], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "datum", "bedrag_eur", "vendor_raw",
                    "description", "hoe_gevonden_(in_te_vullen)"])
        for it in items:
            w.writerow([
                it["status"],
                _txn_date(it),
                f"{abs(it['amount_cents'])/100:.2f}",
                it["vendor_raw"],
                (it.get("description") or "").replace("\n", " ")[:300],
                "",
            ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print body + attachment list, do not send")
    args = ap.parse_args()

    settings = load_settings()
    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        run = get_run(c, args.run_id)
        if run is None:
            print(f"run {args.run_id} not found", file=sys.stderr)
            return 1
        items = list_run_items(c, args.run_id)

    matched = [i for i in items if i["status"] == "matched"]
    needs_portal = [i for i in items if i["status"] == "needs_portal"]
    unknowns = [i for i in items if i["status"] == "unknown_vendor"]
    physical = [i for i in items if i["status"] == "physical_only"]
    ignored = [i for i in items if i["status"] == "ignored"]

    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        all_strats = list_vendor_strategies(c)
    strategies_by_name = {s["name"]: s for s in all_strats}

    output_dir = Path(run["output_dir"])
    pdf_paths = [output_dir / it["attachment_path"]
                 for it in matched
                 if it.get("attachment_path")]
    pdf_paths = [p for p in pdf_paths if p.exists()]

    csv_path = output_dir / "nog-uit-te-zoeken.csv"
    write_unknowns_csv(needs_portal + unknowns, csv_path)

    body = render_body(run, matched, needs_portal, unknowns,
                        physical, ignored, strategies_by_name)
    period = run.get("period_label") or f"run-{run['id']}"
    company = settings.user_company or "Receipt-run"
    subject = (f"{company} {period} — {len(matched)} facturen, "
               f"{len(needs_portal)} portal, {len(physical)} fysiek, "
               f"{len(unknowns)} unknown")

    if args.dry_run:
        print("=== SUBJECT ===")
        print(subject)
        print()
        print("=== BODY ===")
        print(body)
        print()
        print("=== ATTACHMENTS ===")
        for p in pdf_paths + [csv_path]:
            print(f"  {p.name}  ({p.stat().st_size} bytes)")
        return 0

    creds = get_credentials(
        settings.google_credentials_path, settings.google_token_path,
    )
    gmail = GmailClient(creds)
    sent = gmail.send(
        to=args.to,
        subject=subject,
        body=body,
        attachments=pdf_paths + [csv_path],
    )
    print(f"sent: id={sent['id']} thread={sent.get('thread_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
