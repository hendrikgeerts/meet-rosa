"""IMAP client + multi-account config + Keychain-backed credentials.

Each `ImapAccount` is one mailbox the agent reads. Config (host/port/folders/
username) lives in `config/imap_accounts.yaml` — managed via the
`scripts/imap_account.py` CLI. Passwords live in the macOS Keychain under
service `pa-agent-imap`, never in yaml or .env.

`ImapClient` wraps `imap-tools` for a single account and exposes:
  - test_connection()         — bind + login + return capabilities (sanity)
  - list_folders()            — IMAP folder names (helps user pick `sent`)
  - list_recent(folder, n)    — N most recent message summaries
  - fetch_full(uid, folder)   — full body (text + html) for ingestion later

The agent's mail-ingestion loop (next session) will call list_recent on a
poll, then fetch_full for messages it hasn't seen yet, summarise locally,
and store both summary + full body in memory.db.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import keyring
import yaml

log = logging.getLogger(__name__)

KEYRING_SERVICE = "pa-agent-imap"


# --- account model + yaml loader ------------------------------------------

@dataclass(frozen=True)
class ImapFolders:
    inbox: str = "INBOX"
    sent: str = "Sent"


@dataclass(frozen=True)
class ImapAccount:
    name: str
    label: str
    host: str
    port: int
    ssl: bool
    username: str
    folders: ImapFolders = field(default_factory=ImapFolders)
    enabled: bool = True
    poll_interval_seconds: int = 300
    # SMTP voor uitgaande mails op deze account. Bij None: geen send-capability.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_use_starttls: bool = True
    # Display-name + adres dat in de From-header staat. Default = username.
    from_address: str | None = None
    from_name: str | None = None

    @property
    def keychain_key(self) -> str:
        """The username we use to look up the password in macOS Keychain."""
        return self.name


def _account_from_dict(d: dict[str, Any]) -> ImapAccount:
    folders_d = d.get("folders") or {}
    smtp_d = d.get("smtp") or {}
    return ImapAccount(
        name=str(d["name"]),
        label=str(d.get("label", d["name"])),
        host=str(d["host"]),
        port=int(d.get("port", 993)),
        ssl=bool(d.get("ssl", True)),
        username=str(d["username"]),
        folders=ImapFolders(
            inbox=str(folders_d.get("inbox", "INBOX")),
            sent=str(folders_d.get("sent", "Sent")),
        ),
        enabled=bool(d.get("enabled", True)),
        poll_interval_seconds=int(d.get("poll_interval_seconds", 300)),
        smtp_host=(str(smtp_d["host"]) if smtp_d.get("host") else None),
        smtp_port=int(smtp_d.get("port", 587)),
        smtp_use_starttls=bool(smtp_d.get("use_starttls", True)),
        from_address=(str(d["from_address"]) if d.get("from_address") else None),
        from_name=(str(d["from_name"]) if d.get("from_name") else None),
    )


def _account_to_dict(a: ImapAccount) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": a.name,
        "label": a.label,
        "host": a.host,
        "port": a.port,
        "ssl": a.ssl,
        "username": a.username,
        "folders": {"inbox": a.folders.inbox, "sent": a.folders.sent},
        "enabled": a.enabled,
        "poll_interval_seconds": a.poll_interval_seconds,
    }
    if a.smtp_host:
        out["smtp"] = {
            "host": a.smtp_host,
            "port": a.smtp_port,
            "use_starttls": a.smtp_use_starttls,
        }
    if a.from_address:
        out["from_address"] = a.from_address
    if a.from_name:
        out["from_name"] = a.from_name
    return out


def load_accounts(yaml_path: Path) -> list[ImapAccount]:
    if not yaml_path.exists():
        return []
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    raw = cfg.get("accounts") or []
    return [_account_from_dict(d) for d in raw if d]


def save_accounts(yaml_path: Path, accounts: list[ImapAccount]) -> None:
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"accounts": [_account_to_dict(a) for a in accounts]}
    yaml_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    yaml_path.chmod(0o600)


# --- credential helpers ---------------------------------------------------

def get_password(account: ImapAccount) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, account.keychain_key)


def set_password(account: ImapAccount, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, account.keychain_key, password)


def delete_password(account: ImapAccount) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, account.keychain_key)
    except keyring.errors.PasswordDeleteError:
        pass


# --- client ---------------------------------------------------------------

@dataclass(frozen=True)
class MailHeader:
    uid: str
    folder: str
    from_addr: str
    to_addrs: tuple[str, ...]
    subject: str
    date_iso: str
    snippet: str           # first ~200 chars of body, body niet meegestuurd
    seen: bool


@dataclass(frozen=True)
class MailFull:
    uid: str
    folder: str
    from_addr: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    subject: str
    date_iso: str
    text: str              # body/plain
    html: str              # body/html (kan leeg)
    headers: dict[str, str]


class ImapClient:
    """Per-account client. `imap-tools` lazy-imported so unit-tests can
    monkeypatch zonder dat ze de echte lib hoeven."""

    def __init__(self, account: ImapAccount, password: str) -> None:
        self._account = account
        self._password = password

    @property
    def account(self) -> ImapAccount:
        return self._account

    def _connect(self):  # type: ignore[no-untyped-def]
        # Audit E-4 (28/6): expliciete socket-timeout zodat een IMAP-server
        # die hangt de polling-thread niet eindeloos blokkeert.
        from imap_tools import MailBox, MailBoxUnencrypted
        cls = MailBox if self._account.ssl else MailBoxUnencrypted
        mb = cls(self._account.host, port=self._account.port, timeout=30)
        mb.login(self._account.username, self._password)
        return mb

    def test_connection(self) -> dict[str, Any]:
        mb = self._connect()
        try:
            folders = [f.name for f in mb.folder.list()]
        finally:
            mb.logout()
        return {"ok": True, "folders": folders}

    def list_folders(self) -> list[str]:
        mb = self._connect()
        try:
            return [f.name for f in mb.folder.list()]
        finally:
            mb.logout()

    def list_recent(
        self, folder: str | None = None, limit: int = 20,
    ) -> list[MailHeader]:
        target = folder or self._account.folders.inbox
        mb = self._connect()
        try:
            mb.folder.set(target)
            out: list[MailHeader] = []
            # imap-tools .fetch reverse=True yields newest first; mark_seen=False
            # so the agent reading mail doesn't change unread state for the user.
            for msg in mb.fetch(limit=limit, reverse=True, mark_seen=False, bulk=True):
                out.append(MailHeader(
                    uid=str(msg.uid or ""),
                    folder=target,
                    from_addr=msg.from_ or "",
                    to_addrs=tuple(msg.to or ()),
                    subject=msg.subject or "(geen onderwerp)",
                    date_iso=msg.date.isoformat() if msg.date else "",
                    snippet=(msg.text or msg.html or "")[:200].strip(),
                    seen="\\Seen" in (msg.flags or ()),
                ))
            return out
        finally:
            mb.logout()

    def fetch_full(self, uid: str, folder: str | None = None) -> MailFull | None:
        target = folder or self._account.folders.inbox
        mb = self._connect()
        try:
            mb.folder.set(target)
            for msg in mb.fetch(criteria=f"UID {uid}", mark_seen=False, bulk=True):
                return MailFull(
                    uid=str(msg.uid or ""),
                    folder=target,
                    from_addr=msg.from_ or "",
                    to_addrs=tuple(msg.to or ()),
                    cc_addrs=tuple(msg.cc or ()),
                    subject=msg.subject or "(geen onderwerp)",
                    date_iso=msg.date.isoformat() if msg.date else "",
                    text=msg.text or "",
                    html=msg.html or "",
                    headers={k: str(v) for k, v in (msg.headers or {}).items()},
                )
            return None
        finally:
            mb.logout()


# --- iteration helper for the (future) ingestion loop ---------------------

def all_enabled(yaml_path: Path) -> Iterator[tuple[ImapAccount, str]]:
    """Yields (account, password) voor elk ingeschakeld account waarvoor een
    password in Keychain staat. Skipt accounts zonder password met warning."""
    for acc in load_accounts(yaml_path):
        if not acc.enabled:
            continue
        pw = get_password(acc)
        if not pw:
            log.warning("imap %s: no password in Keychain — run "
                        "`scripts/imap_account.py edit %s` to set one.", acc.name, acc.name)
            continue
        yield acc, pw
