"""Gmail-source: pakt nieuwe inbox + sent messages via Gmail API.

Gmail biedt geen UID-style high-water-mark, dus we gebruiken `after:` query
op `internalDate`. Voor incremental: pak alles ná `since_unix - 60s` (kleine
overlap; insert_item dedupliceert op (source, account, external_id)).

Direction: `in` voor INBOX, `out` voor SENT. We doen één query per richting
en mergen de resultaten.
"""
from __future__ import annotations

import base64
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from extensions.comm_intel.schema import CommItem
from integrations.gmail import GmailClient

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


class GmailSource:
    source = "gmail"
    account = "gmail"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def archive(self, external_id: str) -> bool:
        """Remove INBOX label so the message disappears from the user's
        inbox-view (still in All Mail for audit). Used for PA-LOC mails
        post-ingest: the coords are persisted, the mail itself has no
        further interactive value. Returns False on API failure (non-
        fatal — ingest continues)."""
        try:
            self._client.archive(external_id)
            return True
        except Exception:
            log.exception("gmail.archive failed for %s", external_id)
            return False

    def fetch_new(
        self,
        *,
        last_external_id: str | None,
        since_unix: int,
        limit: int,
    ) -> Iterable[CommItem]:
        since_dt = datetime.fromtimestamp(max(since_unix - 60, 0), TZ)
        query_after = since_dt.strftime("after:%Y/%m/%d")

        # Inbox (incoming)
        for direction, query in (
            ("in",  f"in:inbox -in:chats {query_after}"),
            ("out", f"in:sent -in:chats {query_after}"),
        ):
            try:
                summaries = self._client.list_recent(max_results=limit, query=query)
            except Exception:
                log.exception("gmail list_recent failed for query=%s", query)
                continue

            for s in summaries:
                msg_id = s.get("id")
                if not msg_id:
                    continue
                full = self._fetch_full(msg_id)
                if not full:
                    continue
                full["direction"] = direction
                yield _to_comm_item(full)

    def _fetch_full(self, msg_id: str) -> dict[str, Any] | None:
        """One full-format users.messages.get call — we need the body."""
        try:
            m = self._client._service.users().messages().get(  # type: ignore[attr-defined]
                userId="me", id=msg_id, format="full",
            ).execute()
        except Exception:
            log.exception("gmail get %s failed", msg_id)
            return None
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        body = _extract_text(m.get("payload", {}))
        return {
            "id": m["id"],
            "thread_id": m.get("threadId"),
            "internal_date_ms": int(m.get("internalDate", 0) or 0),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "snippet": m.get("snippet", ""),
            "body": body,
            "label_ids": m.get("labelIds", []),
        }


def _extract_text(payload: dict[str, Any]) -> str:
    """MIME-walk: prefer text/plain; fall back to text/html (stripped)."""
    if not payload:
        return ""

    def _decode(data: str | None) -> str:
        if not data:
            return ""
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")

    queue = [payload]
    html_fallback = ""
    while queue:
        part = queue.pop(0)
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        if mime == "text/plain" and body.get("data"):
            return _decode(body["data"]).strip()
        if mime == "text/html" and body.get("data") and not html_fallback:
            html_fallback = _decode(body["data"]).strip()
        for sub in part.get("parts", []) or []:
            queue.append(sub)
    return _strip_html(html_fallback)


def _strip_html(html: str) -> str:
    """Crude tag-strip — voor de samenvatter is grove tekst goed genoeg.
    Geen bs4 dependency voor MVP."""
    if not html:
        return ""
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _to_comm_item(d: dict[str, Any]) -> CommItem:
    occurred = int(d["internal_date_ms"] / 1000) if d["internal_date_ms"] else 0
    to_list = [a.strip() for a in (d.get("to") or "").split(",") if a.strip()]
    cc_list = [a.strip() for a in (d.get("cc") or "").split(",") if a.strip()]
    return CommItem(
        source="gmail",
        account="gmail",
        external_id=d["id"],
        folder=("INBOX" if d["direction"] == "in" else "SENT"),
        direction=d["direction"],
        from_addr=d.get("from") or "",
        to_addrs=to_list,
        cc_addrs=cc_list,
        subject=d.get("subject") or "",
        occurred_at=occurred,
        body_full=(d.get("body") or d.get("snippet") or "").strip(),
        thread_ref=d.get("thread_id"),
        raw_meta={"label_ids": d.get("label_ids", [])},
    )
