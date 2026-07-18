"""Gmail operations exposed to the agent. Thin facade over google-api-python-client."""
from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.external_audit import audit_googleapi_execute

log = logging.getLogger(__name__)


def _execute(req: Any, *, endpoint: str, note: str | None = None) -> Any:
    """Thin wrapper that pins service='gmail' for audit logging.
    Implementation lives in core.external_audit so gmail/gcal share one
    helper (SECURITY_REVIEW_2 MEDIUM-7 + M2 follow-up review)."""
    return audit_googleapi_execute(
        req, service="gmail", endpoint=endpoint, note=note,
    )


@dataclass(frozen=True)
class MessageSummary:
    id: str
    thread_id: str
    subject: str
    sender: str
    snippet: str
    date: str
    unread: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.sender,
            "snippet": self.snippet,
            "date": self.date,
            "unread": self.unread,
        }


class GmailClient:
    def __init__(self, creds: Credentials) -> None:
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def list_recent(self, max_results: int = 10, query: str | None = None) -> list[dict[str, Any]]:
        resp = _execute(
            self._service.users().messages().list(
                userId="me",
                maxResults=max_results,
                q=query or "",
            ),
            endpoint="users.messages.list",
            note=f"max={max_results}",
        )
        ids = [m["id"] for m in resp.get("messages", [])]
        return [self._summarize(mid).to_dict() for mid in ids]

    def search(self, query: str, max_results: int = 20) -> list[dict[str, Any]]:
        return self.list_recent(max_results=max_results, query=query)

    def list_unread_important(self, max_results: int = 15) -> list[dict[str, Any]]:
        return self.list_recent(
            max_results=max_results,
            query="is:unread (is:important OR in:inbox -category:promotions -category:social)",
        )

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        t = _execute(
            self._service.users().threads().get(userId="me", id=thread_id, format="full"),
            endpoint="users.threads.get",
        )
        out_messages: list[dict[str, Any]] = []
        for m in t.get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            out_messages.append({
                "id": m["id"],
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "snippet": m.get("snippet", ""),
                "body": _extract_body(m.get("payload", {})),
            })
        return {"thread_id": thread_id, "messages": out_messages}

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to_thread: str | None = None,
        cc: str | None = None,
        attachments: list[Path] | None = None,
    ) -> dict[str, Any]:
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg.set_content(body)

        for att_path in (attachments or []):
            data = att_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(att_path))
            if mime is None:
                mime = "application/octet-stream"
            maintype, _, subtype = mime.partition("/")
            msg.add_attachment(
                data, maintype=maintype, subtype=subtype or "octet-stream",
                filename=att_path.name,
            )

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if in_reply_to_thread:
            payload["threadId"] = in_reply_to_thread

        sent = _execute(
            self._service.users().messages().send(userId="me", body=payload),
            endpoint="users.messages.send",
            note=f"attachments={len(attachments or [])}",
        )
        return {"id": sent["id"], "thread_id": sent.get("threadId")}

    def mark_read(self, message_id: str) -> None:
        _execute(
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ),
            endpoint="users.messages.modify",
            note="mark_read",
        )

    def archive(self, message_id: str) -> None:
        """Remove INBOX label so the message disappears from inbox-view
        but stays in All Mail for audit. Used by PA-LOC auto-archive
        post-ingest."""
        _execute(
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            ),
            endpoint="users.messages.modify",
            note="archive",
        )

    def get_message_full(self, message_id: str) -> dict[str, Any]:
        """Volledige raw Gmail-message inclusief payload+parts. Bedoeld voor
        attachment-discovery — `get_thread` strips parts en geeft alleen
        body-text terug."""
        return _execute(
            self._service.users().messages().get(
                userId="me", id=message_id, format="full",
            ),
            endpoint="users.messages.get",
            note="format=full",
        )

    def get_attachment(self, *, message_id: str, attachment_id: str) -> bytes:
        """Download een bijlage als raw bytes. Gmail levert base64url-encoded
        data; we decoden hier zodat callers `bytes` krijgen."""
        att = _execute(
            self._service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id,
            ),
            endpoint="users.messages.attachments.get",
        )
        data = att.get("data") or ""
        # Gmail uses URL-safe base64 without padding
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)

    def _summarize(self, message_id: str) -> MessageSummary:
        m = _execute(
            self._service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ),
            endpoint="users.messages.get",
            note="format=metadata",
        )
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        labels = m.get("labelIds", [])
        return MessageSummary(
            id=m["id"],
            thread_id=m.get("threadId", ""),
            subject=headers.get("subject", "(no subject)"),
            sender=headers.get("from", ""),
            snippet=m.get("snippet", ""),
            date=headers.get("date", ""),
            unread="UNREAD" in labels,
        )


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk the MIME tree and return the first text/plain part, falling back to text/html."""
    if not payload:
        return ""

    def decode(data: str | None) -> str:
        if not data:
            return ""
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")

    parts_to_check: list[dict[str, Any]] = [payload]
    html_fallback = ""
    while parts_to_check:
        part = parts_to_check.pop(0)
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return decode(body["data"]).strip()
        if mime == "text/html" and body.get("data") and not html_fallback:
            html_fallback = decode(body["data"]).strip()
        for sub in part.get("parts", []) or []:
            parts_to_check.append(sub)
    return html_fallback
