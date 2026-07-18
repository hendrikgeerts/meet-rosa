"""SMTP-send voor IMAP-accounts (HGE, DPM).

Hergebruikt het wachtwoord uit macOS Keychain dat IMAP ook gebruikt
(zelfde keychain_key). STARTTLS op poort 587 default. Returns
de Message-ID dat de server toekent zodat de caller eventueel kan
auditeren of de mail in een thread plaatsen.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import keyring

from core.external_audit import log_external
from integrations.imap import KEYRING_SERVICE, ImapAccount

log = logging.getLogger(__name__)


def send_via_account(
    account: ImapAccount,
    *,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    in_reply_to_message_id: str | None = None,
    references: str | None = None,
) -> str:
    """Verstuur mail via SMTP. Returns Message-ID dat de server toekent
    (handig voor thread-tracking)."""
    if not account.smtp_host:
        raise RuntimeError(
            f"SMTP not configured for account '{account.name}' — "
            f"add 'smtp.host' to imap_accounts.yaml"
        )

    password = keyring.get_password(KEYRING_SERVICE, account.keychain_key)
    if not password:
        raise RuntimeError(
            f"No keychain password for IMAP account '{account.name}' "
            f"(service={KEYRING_SERVICE!r}, key={account.keychain_key!r})"
        )

    from_addr = account.from_address or account.username
    from_field = (
        f"{account.from_name} <{from_addr}>" if account.from_name else from_addr
    )

    msg = EmailMessage()
    msg["From"] = from_field
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
    if references:
        msg["References"] = references
    msg.set_content(body)

    log.info("smtp: sending via %s:%d as %s to %s",
             account.smtp_host, account.smtp_port, from_addr, to)

    body_size = len(str(msg).encode("utf-8", errors="replace"))
    import time as _time
    t0 = _time.monotonic()
    error_status: int | None = 0
    try:
        if account.smtp_use_starttls:
            with smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(account.username, password)
                s.send_message(msg)
        else:
            # SSL-direct (bv. poort 465). Minder common.
            with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30) as s:
                s.login(account.username, password)
                s.send_message(msg)
    except Exception:
        error_status = None
        raise
    finally:
        log_external(
            service=f"smtp:{account.name}",
            endpoint=f"SMTP {account.smtp_host}:{account.smtp_port}",
            status=error_status,
            bytes_out=body_size,
            latency_ms=int((_time.monotonic() - t0) * 1000),
        )

    return str(msg.get("Message-ID") or "")
