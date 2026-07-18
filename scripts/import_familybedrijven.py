"""Import top-N familiebedrijven uit de EW-Digitaal Flourish-feed naar
sales_accounts.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/import_familybedrijven.py \\
        --target adl_video --dry-run                  # alleen tonen
    PYTHONPATH=src ./venv/bin/python scripts/import_familybedrijven.py \\
        --target adl_video --commit                   # echt schrijven
    PYTHONPATH=src ./venv/bin/python scripts/import_familybedrijven.py \\
        --target adl_video --no-filter --commit       # geen sector-filter

Filter-default (optie B uit chat met you):
- alleen sectoren met fysieke publieksstromen waar narrowcasting waarde
  toevoegt: retail/horeca/auto/zorg/tankstations
- omzet >= 100 mln EUR drempel
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.sales.schema import (
    VALID_TARGETS, init_sales_schema, normalize_naam,
)
from extensions.sales.storage import find_account_by_name, insert_account


FEED_URL = "https://public.flourish.studio/visualisation/9477370/visualisation.json"

# Sector-keywords op de 'Activiteit'-string. Lowercase substring-match.
RELEVANT_SECTORS = {
    "retail":      ["supermarkt", "winkelketen", "warenhuis", "retail",
                     "kledingwinkel", "drogisterij", "elektronicaketen",
                     "doe-het-zelf", "tuincentrum"],
    "horeca":      ["hotel", "horeca", "restaurant", "café", "cafétaria",
                     "casino", "evenement"],
    "auto":        ["autodealer", "autobedrijf", "automotive", "garagebedrijf"],
    "zorg":        ["zorg", "kliniek", "ziekenhuis", "apotheek", "tandarts",
                     "fysiotherapie"],
    "tankstation": ["tankstation", "benzinepomp", "wegrestaurant"],
    "vastgoed":    ["winkelvastgoed", "winkelcentrum"],  # locatie-eigenaar
    "leisure":     ["pretpark", "attractie", "bioscoop", "sportcomplex",
                     "vakantiepark"],
}


def _parse_int(s: str) -> int | None:
    """'26583' / '26.583' / '1,5' → int. Lege/onbekende → None."""
    if not s or not s.strip():
        return None
    cleaned = re.sub(r"[^\d]", "", s)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _sector_for(activiteit: str) -> str | None:
    a = activiteit.lower()
    for sector, keywords in RELEVANT_SECTORS.items():
        for kw in keywords:
            if kw in a:
                return sector
    return None


def fetch_rows() -> list[dict[str, Any]]:
    req = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "pa-agent/import-familybedrijven"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = payload["data"]["rows"]
    header, *body = rows
    out: list[dict[str, Any]] = []
    for r in body:
        if len(r) < 6:
            continue
        out.append({
            "naam":      str(r[0]).strip(),
            "positie":   _parse_int(str(r[1])),
            "omzet_mln": _parse_int(str(r[2])),
            "eigenaar":  str(r[3]).strip(),
            "werknemers": _parse_int(str(r[4])),
            "activiteit": str(r[5]).strip(),
        })
    return out


def filter_relevant(
    rows: list[dict[str, Any]], *,
    min_omzet_mln: int = 100,
    apply_sector_filter: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["omzet_mln"] is not None and r["omzet_mln"] < min_omzet_mln:
            continue
        sector = _sector_for(r["activiteit"])
        if apply_sector_filter and sector is None:
            continue
        r2 = dict(r)
        r2["sector"] = sector or "other"
        out.append(r2)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", required=True, choices=sorted(VALID_TARGETS - {"multi"}),
        help="Welk bedrijf: adl_video / dst_connect / ds_templates",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Default: alleen tonen, niet schrijven (use --commit om wel)",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Daadwerkelijk schrijven naar sales_accounts",
    )
    parser.add_argument(
        "--no-filter", action="store_true", default=True,
        help="Default: alle 200 importeren zonder sector-filter",
    )
    parser.add_argument(
        "--apply-filter", action="store_true",
        help="Alleen relevante sectoren importeren (retail/horeca/auto/zorg/etc.)",
    )
    parser.add_argument(
        "--min-omzet", type=int, default=0,
        help="Min. omzet (mln EUR), default 0 (geen drempel)",
    )
    parser.add_argument(
        "--status", default="koud",
        help="Initial status voor alle accounts (default: koud)",
    )
    args = parser.parse_args()

    commit = bool(args.commit)
    if commit:
        args.dry_run = False

    print(f"Fetching {FEED_URL} …")
    rows = fetch_rows()
    print(f"  → {len(rows)} bedrijven uit feed")

    # Default: no_filter=True (alle 200). Met --apply-filter aan-zetten.
    apply_filter = bool(args.apply_filter)
    filtered = filter_relevant(
        rows, min_omzet_mln=args.min_omzet,
        apply_sector_filter=apply_filter,
    )
    print(
        f"  → {len(filtered)} na filter "
        f"(sector={apply_filter}, min omzet={args.min_omzet} mln)",
    )
    print()
    print("=== Wat zou geïmporteerd worden ===")
    for r in filtered:
        omzet = f"{r['omzet_mln']:>5} mln" if r["omzet_mln"] else "    -"
        sector = r.get("sector", "-")
        print(f"  #{r['positie']:>3} | {r['naam']:<45} | {omzet} | "
              f"{sector:<12} | {r['activiteit'][:50]}")

    if not commit:
        print("\n(dry-run — geen schrijfacties. Voeg --commit toe om "
              "daadwerkelijk in sales_accounts te zetten.)")
        return 0

    settings = load_settings()
    init_sales_schema(settings.db_path)

    skipped = 0
    inserted = 0
    print()
    print(f"=== Schrijven naar sales_accounts (target={args.target}) ===")
    with sqlite3.connect(settings.db_path, isolation_level=None) as conn:
        for r in filtered:
            existing = find_account_by_name(conn, r["naam"])
            if existing:
                print(f"  SKIP (bestaat al): {r['naam']} (id={existing['id']})")
                skipped += 1
                continue
            notes_parts = [
                f"Geïmporteerd uit EW top-200 familiebedrijven 2022",
                f"Positie: {r['positie']}",
            ]
            if r["eigenaar"]:
                notes_parts.append(f"Eigenaarstype: {r['eigenaar']}")
            if r["werknemers"]:
                notes_parts.append(f"Werknemers: {r['werknemers']}")
            notes_parts.append(f"Activiteit: {r['activiteit']}")
            est_value = (
                int(r["omzet_mln"]) * 1000  # mln→EUR, very rough proxy
                if r["omzet_mln"] else None
            )
            try:
                new_id = insert_account(
                    conn,
                    naam=r["naam"],
                    target=args.target,
                    prospect_type="end_customer",
                    sector=r.get("sector"),
                    status=args.status,
                    notes=" · ".join(notes_parts),
                    estimated_value_eur=est_value,
                    created_via="csv_import",
                )
                print(f"  + {r['naam']} (id={new_id}, sector={r.get('sector')})")
                inserted += 1
            except ValueError as e:
                print(f"  ERR {r['naam']}: {e}")
                skipped += 1

    print()
    print(f"Klaar: {inserted} nieuwe accounts, {skipped} overgeslagen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
