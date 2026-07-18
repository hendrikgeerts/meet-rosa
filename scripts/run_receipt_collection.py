"""One-shot CLI to trigger a receipt-collection run outside the orchestrator.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/run_receipt_collection.py \\
        --excel ~/PA-Receipts/inbox/DS_Templates\\ -\\ ontbrekende\\ facturen_tm_19042026.xlsx

Reuses the same Gmail/IMAP/Ollama setup as main.py so matches mirror what
the running agent would produce. Prints summary JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python scripts/run_receipt_collection.py` from repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.receipt_collector.schema import init_receipt_collector_schema
from extensions.receipt_collector.tools import receipt_run_start_handler
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from integrations.imap import all_enabled as imap_all_enabled
from models.ollama import OllamaClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True,
                    help="Path to the afschrijvingen-Excel")
    ap.add_argument("--margin-days", type=int, default=30)
    args = ap.parse_args()

    settings = load_settings()
    init_receipt_collector_schema(settings.db_path)

    creds = get_credentials(
        settings.google_credentials_path, settings.google_token_path,
    )
    gmail = GmailClient(creds)

    config_dir = settings.data_dir.parent / "config"
    imap_yaml = config_dir / "imap_accounts.yaml"
    imap_pairs = list(imap_all_enabled(imap_yaml)) if imap_yaml.exists() else []

    # Vendor-extract uit description is een simpele structured-output taak —
    # phi3:mini doet 'm prima en is ~10× sneller dan llama3.1:8b op Intel CPU.
    # Korte timeout zodat één hangende call niet de hele run gijzelt.
    ollama = OllamaClient(model=settings.local_model_small, timeout=30.0)

    summary = receipt_run_start_handler(
        settings.db_path,
        {"excel_path": args.excel, "margin_days": args.margin_days},
        gmail=gmail,
        imap_accounts=imap_pairs,
        output_root=Path.home() / "PA-Receipts",
        ollama=ollama,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if "error" not in summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
