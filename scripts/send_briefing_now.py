"""One-shot CLI om een verse morning-briefing te genereren en via
iMessage naar de owner te sturen. Gebruikt dezelfde flow als
core/scheduler._maybe_send_briefing.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/send_briefing_now.py
    PYTHONPATH=src ./venv/bin/python scripts/send_briefing_now.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.audit import (
    AdminActionLogger, AuditLogger, PayloadAuditLogger, bind_admin_logger,
)
from core.briefings import generate_briefing
from core.config import load_settings
from core.external_audit import bind_audit as _bind_external_audit
from core.timezone import bind as bind_tz
from extensions.sales.schema import init_sales_schema
from integrations import imessage
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from integrations.gcal import CalendarClient
from integrations.here_maps import HereMapsClient
from models.ollama import OllamaClient
from privacy.classifier import load_classifier_from_yaml
from privacy.gateway import Gateway
from privacy.redactor import load_redactor_from_yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Genereer + print, niet versturen")
    args = parser.parse_args()

    settings = load_settings()
    bind_tz(settings.db_path,
            default_timezone=settings.default_timezone)
    config_dir = settings.data_dir.parent / "config"

    # Schema-init zodat het script standalone kan draaien (zonder dat
    # de daemon al een eerder gestart is). Idempotent — CREATE IF NOT EXISTS.
    init_sales_schema(settings.db_path)

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

    local_client = OllamaClient(
        model=settings.local_model_main, keep_alive=-1,
    )
    summarize_client = OllamaClient(
        model=settings.local_model_main, keep_alive=-1,
    )

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

    here_client = None
    if settings.here_api_key:
        here_client = HereMapsClient(settings.here_api_key)

    morning_extras = config_dir / "morning_extras.yaml"
    vip_path = config_dir / "vip_contacts.yaml"
    okrs_path = config_dir / "okrs.yaml"

    print("Generating briefing...", flush=True)
    text = generate_briefing(
        gateway=gateway,
        gmail=gmail,
        calendar=calendar,
        db_path=settings.db_path,
        morning_extras_yaml=morning_extras if morning_extras.exists() else None,
        ollama=summarize_client,
        vip_path=vip_path if vip_path.exists() else None,
        okrs_path=okrs_path if okrs_path.exists() else None,
        here=here_client,
        home_lat=settings.travel_alerts_home_lat,
        home_lon=settings.travel_alerts_home_lon,
    )

    print(f"--- BRIEFING ({len(text)} chars) ---")
    print(text)
    print("--- END ---")

    # Schrijf altijd een fallback-file zodat de tekst nooit verloren gaat
    # als osascript-send vastloopt.
    fallback = Path("/tmp/last_briefing.txt")
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
