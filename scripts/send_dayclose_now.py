"""One-shot CLI om NU een dayclose te genereren + versturen.

Bypasst de classifier via force_label='internal' zodat de payload
naar Claude gaat i.p.v. lokaal Llama (dat op Intel MBP soms >4min doet
en dan de scheduler laat timeouten).

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/send_dayclose_now.py
    PYTHONPATH=src ./venv/bin/python scripts/send_dayclose_now.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.audit import (
    AdminActionLogger, AuditLogger, PayloadAuditLogger, bind_admin_logger,
)
from core.config import load_settings
from core.dayclose import DAYCLOSE_PROMPT, collect_dayclose_context
from core.external_audit import bind_audit as _bind_external_audit
from core.timezone import bind as bind_tz
from integrations import imessage
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from integrations.gcal import CalendarClient
from models.ollama import OllamaClient
from privacy.classifier import load_classifier_from_yaml
from privacy.gateway import Gateway
from privacy.redactor import load_redactor_from_yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    bind_tz(settings.db_path, default_timezone=settings.default_timezone)
    config_dir = settings.data_dir.parent / "config"

    audit = AuditLogger(settings.audit_dir)
    payload_audit = (
        PayloadAuditLogger(settings.audit_dir) if settings.log_payloads
        else None
    )
    bind_admin_logger(AdminActionLogger(settings.audit_dir))
    _bind_external_audit(audit)

    classifier = load_classifier_from_yaml(
        confidential_path=config_dir / "confidential_domains.yaml",
        vip_path=config_dir / "vip_contacts.yaml",
        default_label=settings.default_sensitivity_label,
    )
    redactor = load_redactor_from_yaml(
        vip_path=config_dir / "vip_contacts.yaml",
        ner_model=settings.ner_model,
    )

    local_client = OllamaClient(model=settings.local_model_main, keep_alive=-1)

    gateway = Gateway(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        audit=audit,
        classifier=classifier,
        redactor=redactor,
        local_client=local_client,
        payload_audit=payload_audit,
    )

    creds = get_credentials(
        settings.google_credentials_path, settings.google_token_path,
    )
    gmail = GmailClient(creds)
    calendar = CalendarClient(creds)

    okrs_path = config_dir / "okrs.yaml"

    print("Collecting dayclose context...", flush=True)
    context = collect_dayclose_context(
        gmail=gmail, calendar=calendar, db_path=settings.db_path,
        okrs_path=okrs_path if okrs_path.exists() else None,
    )
    user_payload = (
        "Context (JSON):\n"
        + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de dagafsluiting."
    )

    print("Calling Claude (force_label=internal, bypasses local Llama)...")
    response = gateway.complete(
        task="dayclose",
        system=DAYCLOSE_PROMPT,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=1024,
        force_label="internal",   # skip classifier — redactor blijft actief
    )
    parts = [
        b.text for b in response.content
        if getattr(b, "type", None) == "text"
    ]
    text = "".join(parts).strip() or "(dagafsluiting was leeg)"

    print(f"--- DAYCLOSE ({len(text)} chars) ---")
    print(text)
    print("--- END ---")

    fallback = Path("/tmp/last_dayclose.txt")
    fallback.write_text(text, encoding="utf-8")
    print(f"\nFallback opgeslagen: {fallback}")

    if args.dry_run:
        print("(dry-run, niet verzonden)")
        return 0

    try:
        imessage.send_imessage(settings.primary_handle, text)
        print(f"Verzonden via iMessage naar {settings.primary_handle}")
    except Exception as e:
        print(f"\n[FOUT] iMessage send faalde: {e}")
        print(f"Tekst staat in {fallback} — kopieer handmatig.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
