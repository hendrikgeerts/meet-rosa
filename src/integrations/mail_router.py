"""Mail-router: kies de juiste verzendkanaal (Gmail OAuth of IMAP-SMTP)
op basis van het gewenste From-adres.

Gebruik door scheduler_assist en (later) elke caller die "stuur deze
mail terug op het oorspronkelijke account" wil.

Routing:
  - Als from_address overeenkomt met een geconfigureerd IMAP-account met
    smtp_host gezet → SMTP via dat account
  - Anders → Gmail OAuth (DST primary)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from integrations.gmail import GmailClient
from integrations.imap import ImapAccount, all_enabled, load_accounts
from integrations.smtp_send import send_via_account as smtp_send

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    backend: str               # 'gmail' of 'smtp:<account-name>'
    message_id: str | None
    thread_id: str | None      # alleen Gmail


def list_smtp_capable_accounts(imap_yaml: Path) -> list[ImapAccount]:
    """Alle IMAP-accounts die een SMTP-config hebben."""
    return [
        a for a, _pw in all_enabled(imap_yaml)
        if a.smtp_host
    ]


def _account_matches(account: ImapAccount, address: str) -> bool:
    """Adres-match: account.from_address (preferred) of account.username."""
    addr = (address or "").strip().lower()
    if not addr:
        return False
    if account.from_address and account.from_address.lower() == addr:
        return True
    if account.username.lower() == addr:
        return True
    return False


def send(
    *,
    from_address: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    in_reply_to_message_id: str | None = None,
    references: str | None = None,
    in_reply_to_thread_id: str | None = None,    # Gmail-thread
    gmail: GmailClient,
    imap_accounts: Iterable[ImapAccount],
) -> SendResult:
    """Stuur mail. Pikt SMTP via een matching IMAP-account, valt anders
    terug op Gmail OAuth.
    """
    for acc in imap_accounts:
        if _account_matches(acc, from_address):
            if not acc.smtp_host:
                log.warning(
                    "mail-router: account '%s' matcht from-address maar heeft "
                    "geen SMTP — fallback naar Gmail", acc.name,
                )
                break
            msgid = smtp_send(
                acc, to=to, subject=subject, body=body, cc=cc,
                in_reply_to_message_id=in_reply_to_message_id,
                references=references,
            )
            log.info("mail-router: sent via SMTP/%s as %s → %s",
                     acc.name, from_address, to)
            return SendResult(
                backend=f"smtp:{acc.name}",
                message_id=msgid or None,
                thread_id=None,
            )

    # Fallback: Gmail OAuth (DST primary).
    sent = gmail.send(to=to, subject=subject, body=body,
                      in_reply_to_thread=in_reply_to_thread_id, cc=cc)
    log.info("mail-router: sent via Gmail (DST) → %s", to)
    return SendResult(
        backend="gmail",
        message_id=sent.get("id"),
        thread_id=sent.get("thread_id"),
    )
