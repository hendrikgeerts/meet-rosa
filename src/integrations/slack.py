"""Slack client + multi-workspace config + Keychain-backed user-tokens.

Each `SlackWorkspace` is one Slack-team the agent reads. Config (name/label/
url/enabled) lives in `config/slack_workspaces.yaml` — managed via the
`scripts/slack_workspace.py` CLI. User-tokens (xoxp-...) live in macOS
Keychain under service `pa-agent-slack`, never in yaml or .env.

`SlackClient` wraps `slack_sdk.WebClient` for a single workspace and exposes:
  - test_connection()             — auth.test (sanity)
  - list_channels()               — public + private + DMs + group DMs
  - list_recent(channel, limit)   — N most-recent messages with resolved names
  - resolve_channel(name_or_id)   — turn 'general' → 'C012ABCDE'

The mail/slack-intel ingest-loop (next session) will poll list_channels +
list_recent per workspace, classify+summarise each new message locally, and
store both summary + raw text in memory.db.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring
import yaml

from core.external_audit import timed_call

log = logging.getLogger(__name__)

KEYRING_SERVICE = "pa-agent-slack"


# --- workspace model + yaml loader ----------------------------------------

@dataclass(frozen=True)
class SlackWorkspace:
    name: str
    label: str
    workspace_url: str = ""        # display only ("initiale.slack.com")
    enabled: bool = True
    poll_interval_seconds: int = 300

    @property
    def keychain_key(self) -> str:
        return self.name


def _workspace_from_dict(d: dict[str, Any]) -> SlackWorkspace:
    return SlackWorkspace(
        name=str(d["name"]),
        label=str(d.get("label", d["name"])),
        workspace_url=str(d.get("workspace_url", "")),
        enabled=bool(d.get("enabled", True)),
        poll_interval_seconds=int(d.get("poll_interval_seconds", 300)),
    )


def _workspace_to_dict(w: SlackWorkspace) -> dict[str, Any]:
    return {
        "name": w.name,
        "label": w.label,
        "workspace_url": w.workspace_url,
        "enabled": w.enabled,
        "poll_interval_seconds": w.poll_interval_seconds,
    }


def load_workspaces(yaml_path: Path) -> list[SlackWorkspace]:
    if not yaml_path.exists():
        return []
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    raw = cfg.get("workspaces") or []
    return [_workspace_from_dict(d) for d in raw if d]


def save_workspaces(yaml_path: Path, workspaces: list[SlackWorkspace]) -> None:
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"workspaces": [_workspace_to_dict(w) for w in workspaces]}
    yaml_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    yaml_path.chmod(0o600)


# --- credential helpers ---------------------------------------------------

def get_token(workspace: SlackWorkspace) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, workspace.keychain_key)


def set_token(workspace: SlackWorkspace, token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, workspace.keychain_key, token)


def delete_token(workspace: SlackWorkspace) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, workspace.keychain_key)
    except keyring.errors.PasswordDeleteError:
        pass


# --- client ---------------------------------------------------------------

@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str           # 'general' / 'C-DM-with-piet' (DM name = user display)
    type: str           # 'public' | 'private' | 'im' | 'mpim'
    is_member: bool


@dataclass(frozen=True)
class SlackMessageHeader:
    ts: str             # Slack timestamp (unique within channel)
    channel_id: str
    channel_name: str
    user_id: str
    user_name: str
    text_snippet: str   # first ~200 chars; full text in memory.db later
    thread_ts: str | None


class SlackClient:
    """Per-workspace client. `slack_sdk` lazy-imported zodat unit-tests zonder
    het package kunnen draaien (CI-friendly)."""

    def __init__(self, workspace: SlackWorkspace, token: str) -> None:
        self._workspace = workspace
        self._token = token
        self._users_cache: dict[str, str] | None = None
        self._channels_cache: list[SlackChannel] | None = None

    @property
    def workspace(self) -> SlackWorkspace:
        return self._workspace

    def _api(self) -> Any:
        from slack_sdk import WebClient
        return WebClient(token=self._token)

    # --- auth -------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        # Audit E-1 (28/6): Slack-egress door audit-stream zodat
        # forensisch zichtbaar is hoeveel calls/dag (A.12.4.1 + A.15.1).
        with timed_call(service="slack", endpoint="auth.test") as ctx:
            r = self._api().auth_test()
            ctx.set(status=200 if r.get("ok") else 0)
        return {
            "ok": bool(r.get("ok")),
            "team": r.get("team"),
            "user": r.get("user"),
            "url": r.get("url"),
        }

    # --- channels ---------------------------------------------------------

    def list_channels(
        self,
        types: tuple[str, ...] = ("public_channel", "private_channel", "im", "mpim"),
    ) -> list[SlackChannel]:
        if self._channels_cache is not None:
            return self._channels_cache
        api = self._api()
        users = self._user_names(api)
        out: list[SlackChannel] = []
        cursor: str | None = None
        while True:
            with timed_call(service="slack",
                             endpoint="conversations.list") as ctx:
                r = api.conversations_list(
                    types=",".join(types), limit=200, cursor=cursor,
                )
                ctx.set(status=200 if r.get("ok") else 0)
            for ch in r.get("channels") or []:
                if ch.get("is_im"):
                    ctype = "im"
                    name = users.get(ch.get("user", ""), ch.get("user", "(dm)"))
                elif ch.get("is_mpim"):
                    ctype = "mpim"
                    name = ch.get("name") or "(mpim)"
                elif ch.get("is_private"):
                    ctype = "private"
                    name = ch.get("name") or ch.get("id", "")
                else:
                    ctype = "public"
                    name = ch.get("name") or ch.get("id", "")
                out.append(SlackChannel(
                    id=ch.get("id", ""),
                    name=name,
                    type=ctype,
                    is_member=bool(ch.get("is_member")),
                ))
            cursor = (r.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        self._channels_cache = out
        return out

    def resolve_channel(self, name_or_id: str) -> str:
        """Turn 'general' (channel name) or 'C012ABCDE' (id) into an id."""
        if name_or_id.startswith(("C", "D", "G")) and len(name_or_id) > 5:
            return name_or_id
        for ch in self.list_channels():
            if ch.name == name_or_id:
                return ch.id
        raise ValueError(f"channel not found: {name_or_id!r}")

    # --- messages ---------------------------------------------------------

    def list_recent(
        self,
        channel: str,
        limit: int = 20,
    ) -> list[SlackMessageHeader]:
        channel_id = self.resolve_channel(channel)
        api = self._api()
        users = self._user_names(api)
        ch_lookup = {c.id: c.name for c in self.list_channels()}
        ch_name = ch_lookup.get(channel_id, channel_id)
        with timed_call(service="slack",
                         endpoint="conversations.history") as ctx:
            r = api.conversations_history(channel=channel_id, limit=limit)
            ctx.set(status=200 if r.get("ok") else 0)
        out: list[SlackMessageHeader] = []
        for m in r.get("messages") or []:
            uid = m.get("user", m.get("bot_id", ""))
            out.append(SlackMessageHeader(
                ts=str(m.get("ts", "")),
                channel_id=channel_id,
                channel_name=ch_name,
                user_id=uid,
                user_name=users.get(uid, uid or "(unknown)"),
                text_snippet=str(m.get("text", "") or "")[:200].strip(),
                thread_ts=m.get("thread_ts"),
            ))
        return out

    # --- internal ---------------------------------------------------------

    def _user_names(self, api: Any) -> dict[str, str]:
        if self._users_cache is not None:
            return self._users_cache
        out: dict[str, str] = {}
        cursor: str | None = None
        while True:
            with timed_call(service="slack", endpoint="users.list") as ctx:
                r = api.users_list(limit=200, cursor=cursor)
                ctx.set(status=200 if r.get("ok") else 0)
            for u in r.get("members") or []:
                profile = u.get("profile") or {}
                name = (profile.get("display_name") or profile.get("real_name")
                        or u.get("real_name") or u.get("name") or u.get("id"))
                out[u.get("id", "")] = name
            cursor = (r.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        self._users_cache = out
        return out


# --- iteration helper for the (future) ingestion loop ---------------------

def all_enabled(yaml_path: Path) -> Iterator[tuple[SlackWorkspace, str]]:
    """Yields (workspace, token) voor elk ingeschakeld workspace waarvoor
    een token in Keychain staat. Skipt anders met warning."""
    for w in load_workspaces(yaml_path):
        if not w.enabled:
            continue
        token = get_token(w)
        if not token:
            log.warning(
                "slack %s: no token in Keychain — run "
                "`scripts/slack_workspace.py edit %s` to set one.", w.name, w.name,
            )
            continue
        yield w, token
