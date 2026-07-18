"""IMAP-source: pakt nieuwe berichten in inbox + sent voor één account.

Gebruikt UID > last_uid waar mogelijk; valt terug op date-since als er nog
geen high-water-mark is. State wordt per (account, folder) bijgehouden door
de ingest-loop (deze klasse is stateless).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from extensions.comm_intel.schema import CommItem
from integrations.imap import ImapAccount, ImapClient

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


class ImapSource:
    """Per-account, per-folder source. The ingest-loop creates one per
    folder (inbox + sent) so high-water-marks stay separate."""

    source = "imap"

    def __init__(self, account: ImapAccount, password: str, folder: str, direction: str) -> None:
        self._account = account
        self._password = password
        self._folder = folder
        self._direction = direction

    @property
    def account(self) -> str:
        return self._account.name

    @property
    def folder(self) -> str:
        return self._folder

    @property
    def direction(self) -> str:
        return self._direction

    def fetch_new(
        self,
        *,
        last_external_id: str | None,
        since_unix: int,
        limit: int,
    ) -> Iterable[CommItem]:
        client = ImapClient(self._account, self._password)
        last_uid = int(last_external_id) if last_external_id and last_external_id.isdigit() else None

        # We gebruiken de bestaande list_recent (haalt headers + snippets) en
        # dan per item een fetch_full voor de body. Niet de meest efficiente
        # IMAP-flow, maar het werkt op alle servers en de ingest-loop
        # rate-limit'd zelf.
        try:
            headers = client.list_recent(folder=self._folder, limit=limit)
        except Exception:
            log.exception("imap %s/%s list_recent failed", self._account.name, self._folder)
            return

        for h in headers:
            try:
                uid_int = int(h.uid)
            except ValueError:
                continue
            if last_uid is not None and uid_int <= last_uid:
                continue

            full = client.fetch_full(h.uid, folder=self._folder)
            if full is None:
                continue
            occurred = _parse_iso(full.date_iso)
            if occurred and occurred < since_unix:
                # Older than our window — incremental cap to avoid huge backfills.
                continue
            yield CommItem(
                source="imap",
                account=self._account.name,
                external_id=full.uid,
                folder=self._folder,
                direction=self._direction,
                from_addr=full.from_addr,
                to_addrs=list(full.to_addrs),
                cc_addrs=list(full.cc_addrs),
                subject=full.subject,
                occurred_at=occurred or 0,
                body_full=(full.text or full.html or "").strip(),
                thread_ref=full.headers.get("references") or full.headers.get("in-reply-to"),
                raw_meta={"message_id": full.headers.get("message-id", "")},
            )


def _parse_iso(iso: str) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp())
    except ValueError:
        return None
