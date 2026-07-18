"""Reclassificeer sector-veld op sales_accounts via lokale Llama.

Bestaande naïeve substring-keyword-matcher leverde 175 van 200
familiebedrijven in 'other'. Llama (lokaal) kan de NL "Activiteit"-
string mappen op een gefixeerde lijst sectoren met veel betere
recall, zonder dat data you's Mac verlaat.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/reclassify_sales_sectors.py
    PYTHONPATH=src ./venv/bin/python scripts/reclassify_sales_sectors.py --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/reclassify_sales_sectors.py --only-other

Strategie:
- Pak activiteit uit de notes (we slaan 'm op tijdens import: 'Activiteit: X').
- Stuur naar Llama met een strikte prompt: één-woord respons uit
  vaste lijst.
- Fallback bij parse-fail: sector blijft ongewijzigd.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from models.ollama import OllamaClient


SECTORS = [
    "retail",          # supermarkt, winkelketen, warenhuis
    "horeca",          # hotel, restaurant, café
    "auto",            # autodealer, automotive, garage
    "zorg",            # ziekenhuis, kliniek, apotheek, ouderenzorg
    "food",            # voedselproducent, brouwer, zuivel, vlees
    "agri",            # land/tuinbouw, kweker, mengvoeder
    "bouw",            # bouwbedrijf, installatie, vastgoed-ontwikkeling
    "industrie",       # machinefabriek, productie, metaal
    "transport",       # logistiek, transport, scheepvaart, lucht
    "energie",         # energie, olie, gas, duurzaam
    "tech",            # IT, software, hardware, telecom
    "media",           # uitgeverij, omroep, reclame
    "leisure",         # pretpark, bioscoop, sport, recreatie
    "financieel",      # bank, verzekeraar, investeringsmaatschappij
    "vastgoed",        # winkelvastgoed, beheer, makelaardij
    "consument",       # textiel, schoenen, sieraden, persoonlijke verzorging
    "groothandel",     # B2B-distributie
    "overheid",        # publiek, semi-publiek
    "diensten",        # zakelijke dienstverlening, advies, uitzending
    "other",           # fallback
]
SECTOR_SET = set(SECTORS)

PROMPT_SYSTEM = (
    "Je bent een NL-sector-classifier. Je krijgt een korte beschrijving "
    "van de hoofdactiviteit van een NL-bedrijf en MOET één woord "
    "antwoorden uit deze vaste lijst (geen ander woord, geen punt, geen "
    "uitleg):\n\n"
    + ", ".join(SECTORS)
    + "\n\nKies de meest specifieke match. Twijfel je tussen retail en "
    "consument? Retail = fysieke winkels, consument = "
    "consumentenmerk/producent. Twijfel je tussen food en agri? "
    "Food = verwerker/producent, agri = primaire productie/kweker."
)


_ACTIVITEIT_RE = re.compile(r"Activiteit:\s*([^·\n]+)", re.I)


def extract_activiteit(notes: str | None) -> str | None:
    if not notes:
        return None
    m = _ACTIVITEIT_RE.search(notes)
    if not m:
        return None
    return m.group(1).strip()


def classify(client: OllamaClient, activiteit: str) -> str | None:
    """Eén Llama-call. Returnt sector uit SECTOR_SET, of None bij parse-fail."""
    try:
        resp = client.chat(
            system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": f"Activiteit: {activiteit}"}],
            max_tokens=10,
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip().lower()
        # Strip surrounding quotes/dots/whitespace
        text = re.sub(r"[\s\".'!?]+", "", text)
        if text in SECTOR_SET:
            return text
        # Soms zegt Llama een hele zin — pak het eerste valide token
        for token in re.split(r"\W+", text):
            if token in SECTOR_SET:
                return token
        return None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Toon classificaties, schrijf niet",
    )
    parser.add_argument(
        "--only-other", action="store_true", default=True,
        help="Default: alleen accounts met sector='other' reclassificeren",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Negeer huidige sector, reclassificeer alles",
    )
    parser.add_argument(
        "--target", default="adl_video",
        help="Welk target reclassificeren (default adl_video)",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = OllamaClient(model=settings.local_model_main, keep_alive=-1)

    where = ["target = ?"]
    params: list = [args.target]
    if not args.all:
        where.append("(sector = 'other' OR sector IS NULL)")

    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, naam, sector, notes FROM sales_accounts "
            f"WHERE {' AND '.join(where)} ORDER BY id",
            params,
        ).fetchall()

    print(f"Te classificeren: {len(rows)} accounts (target={args.target})")
    if not rows:
        return 0

    by_sector: dict[str, int] = {}
    updates: list[tuple[int, str, str, str]] = []  # (id, naam, old, new)
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        activiteit = extract_activiteit(row["notes"])
        if not activiteit:
            print(f"  [{i:>3}/{len(rows)}] SKIP {row['naam']}: geen activiteit in notes")
            continue
        new_sector = classify(client, activiteit)
        if not new_sector or new_sector == row["sector"]:
            sym = "=" if new_sector == row["sector"] else "?"
            print(f"  [{i:>3}/{len(rows)}] {sym} {row['naam']:<40} ({activiteit[:40]}) → "
                  f"{new_sector or 'unparse'}")
            continue
        updates.append((row["id"], row["naam"], row["sector"] or "-", new_sector))
        by_sector[new_sector] = by_sector.get(new_sector, 0) + 1
        print(f"  [{i:>3}/{len(rows)}] + {row['naam']:<40} ({activiteit[:40]}) → "
              f"{row['sector']} → {new_sector}")

    elapsed = time.time() - t0
    print()
    print(f"Klaar in {elapsed:.1f}s. {len(updates)} updates voorgesteld.")
    print(f"Verdeling: {sorted(by_sector.items(), key=lambda x: -x[1])}")

    if args.dry_run:
        print("\n(dry-run — geen schrijfacties)")
        return 0

    print()
    print("=== Schrijven naar DB ===")
    with sqlite3.connect(settings.db_path, isolation_level=None) as conn:
        for aid, naam, old, new in updates:
            conn.execute(
                "UPDATE sales_accounts SET sector = ? WHERE id = ?",
                (new, aid),
            )
    print(f"Geüpdate: {len(updates)} accounts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
