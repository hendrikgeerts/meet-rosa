"""Auto-detect touchpoints uit comm_intel items.

Wordt aangeroepen door comm_intel/ingest.py na een nieuw comm_item:
- email_out / linkedin / slack-bericht naar een adres dat hoort bij
  een sales_account → touchpoint loggen
- email_in van zo'n adres → ook touchpoint (channel='email_in')

Dedupe op source_ref (comm_items.external_id) zodat re-ingest geen
duplicate touchpoints geeft.

Privacy (M1 review-fix): respecteert `enabled`-vlag uit settings.
Caller (comm_intel/ingest) hoeft niet te checken — als de feature uit
staat, returnt deze module direct None.

L2: TYPE_CHECKING-only import voor CommItem zodat we geen circular
import krijgen wanneer comm_intel ooit van sales gaat afhangen.

L3: optionele `conn`-parameter zodat de caller zijn open connection
mag hergebruiken (vermijdt double-connection lock-risk).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import (
    insert_touchpoint,
    touchpoint_exists_for_source,
)

if TYPE_CHECKING:
    from extensions.comm_intel.ingest import CommItem

log = logging.getLogger(__name__)


# Personal mailbox-domeinen mogen NOOIT op naam-stamp matchen. Een mail
# aan `iemand@gmail.com` zou anders een touchpoint loggen tegen een
# account met "Gmail" in de naam.
_PERSONAL_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
    "icloud.com", "live.com", "yahoo.com", "yahoo.nl", "me.com",
    "protonmail.com", "proton.me", "mac.com", "msn.com",
    "duck.com", "duckduckgo.com",
})


def _is_personal_domain(domain: str) -> bool:
    return domain.lower().strip() in _PERSONAL_MAIL_DOMAINS


def _account_id_for_email(
    conn: sqlite3.Connection, email: str | None,
) -> int | None:
    if not email:
        return None
    e = email.strip().lower()
    row = conn.execute(
        "SELECT id FROM sales_accounts "
        "WHERE LOWER(primary_contact_email) = ? LIMIT 1",
        (e,),
    ).fetchone()
    return int(row[0]) if row else None


def _account_id_for_domain(
    conn: sqlite3.Connection, email: str | None,
) -> int | None:
    """Fallback: match domein-deel tegen account-website of naam.
    M2 review-fix: tightened naam-match — prefix + word-boundary i.p.v.
    open `%stem%` substring zodat 'heineken' niet match maakt op
    'Heineken Belgium' wanneer the user alleen 'Heineken Nederland'
    in zijn pipeline heeft."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower().strip()
    if _is_personal_domain(domain):
        return None

    # 1) website-match (meest betrouwbaar)
    row = conn.execute(
        "SELECT id FROM sales_accounts WHERE LOWER(website) LIKE ? LIMIT 1",
        (f"%{domain}%",),
    ).fetchone()
    if row:
        return int(row[0])

    # 2) naam-normalized: domain-stam moet aan het BEGIN van naam
    # staan of na een word-boundary — anders te veel false-positives.
    stem = domain.split(".")[0]
    if len(stem) < 4:
        return None

    # SQL LIKE met token-prefix: 'stem%' OR 'stem' precies
    rows = conn.execute(
        "SELECT id, naam_normalized FROM sales_accounts "
        "WHERE naam_normalized LIKE ? OR naam_normalized = ?",
        (f"{stem}%", stem),
    ).fetchall()
    # Pythonseure word-boundary check (volgend teken na stem moet
    # whitespace zijn of einde)
    for row_id, naam_norm in rows:
        if naam_norm == stem:
            return int(row_id)
        if naam_norm.startswith(stem):
            next_char = naam_norm[len(stem)]
            if next_char in (" ", "-", "_", "."):
                return int(row_id)
    return None


def _resolve_channel(item: CommItem) -> str:
    """H1 review-fix: dead code overschrijving verwijderd. Slack-items
    krijgen nu een eigen 'slack'-channel; LinkedIn-bridges (Slack-bot
    of expliciete linkedin-mail) krijgen 'linkedin'. Gmail/IMAP per
    direction."""
    source = (item.source or "").lower()
    direction = (item.direction or "").lower()
    from_addr = (item.from_addr or "").lower()

    if source == "slack":
        if "linkedin" in from_addr:
            return "linkedin"
        return "slack"
    if direction == "out":
        return "email_out"
    return "email_in"


def maybe_log_touchpoint(
    db_path: Path,
    item: CommItem,
    *,
    enabled: bool = True,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Wordt aangeroepen per ingest comm_item. Returns touchpoint_id als
    er een touchpoint is gelogd, anders None.

    `enabled=False` → no-op (privacy opt-out, M1).
    `conn` optioneel — als caller een open write-conn doorgeeft wordt die
    hergebruikt; anders eigen connection (L3).
    """
    if not enabled:
        return None
    source_ref = f"comm:{item.source}:{item.external_id}"

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        if touchpoint_exists_for_source(conn, source_ref):
            return None

        # Match-strategie afhankelijk van direction
        account_id: int | None = None
        if (item.direction or "").lower() == "out":
            for to in item.to_addrs or []:
                account_id = _account_id_for_email(conn, to)
                if account_id is None:
                    account_id = _account_id_for_domain(conn, to)
                if account_id:
                    break
        else:
            account_id = _account_id_for_email(conn, item.from_addr)
            if account_id is None:
                account_id = _account_id_for_domain(conn, item.from_addr)

        if account_id is None:
            return None

        channel = _resolve_channel(item)
        tp_id = insert_touchpoint(
            conn, account_id=account_id,
            channel=channel,
            occurred_at_unix=item.occurred_at,
            summary=(item.subject or "")[:200],
            source_ref=source_ref,
            detected_auto=True,
        )
        log.info(
            "sales auto-touchpoint: account=%d channel=%s source=%s",
            account_id, channel, source_ref,
        )
        return tp_id
    except Exception:
        log.exception("sales auto-touchpoint failed")
        return None
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
