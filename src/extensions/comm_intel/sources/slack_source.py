"""Slack-source: pakt nieuwe messages per channel-the-user-is-in.

Polling-strategie:
  - One source-instance per workspace.
  - Eerste run: backfill `since_unix` voor channels waar user member is.
  - Incremental: `oldest=last_ts` per channel.
  - Externalid format: `<channel_id>:<ts>` zodat dedupe over alle channels werkt.

We slaan kanaalnamen op als `folder` zodat queries op channel kunnen filteren.
Direction: 'out' als sender == auth-user-id, anders 'in'.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from extensions.comm_intel.schema import CommItem
from integrations.slack import SlackClient, SlackWorkspace

log = logging.getLogger(__name__)


class SlackSource:
    source = "slack"

    def __init__(self, workspace: SlackWorkspace, token: str) -> None:
        self._workspace = workspace
        self._token = token
        self._client = SlackClient(workspace, token)
        self._auth_user: str | None = None

    @property
    def account(self) -> str:
        return self._workspace.name

    def _own_user_id(self) -> str:
        if self._auth_user is not None:
            return self._auth_user
        try:
            info = self._client.test_connection()
            self._auth_user = info.get("user_id") or info.get("user") or ""
        except Exception:
            log.exception("slack auth.test failed for %s", self._workspace.name)
            self._auth_user = ""
        return self._auth_user

    def fetch_new(
        self,
        *,
        last_external_id: str | None,
        since_unix: int,
        limit: int,
    ) -> Iterable[CommItem]:
        # last_external_id is per-source (we use last_occurred_at per-channel
        # as the actual high-water-mark, aggregated across channels).
        own = self._own_user_id()
        try:
            channels = self._client.list_channels()
        except Exception:
            log.exception("slack list_channels failed for %s", self._workspace.name)
            return

        # Cap channels: only ones we're a member of, sorted by type so DMs
        # come first (most likely to have new traffic).
        active = [c for c in channels if c.is_member]
        order = {"im": 0, "mpim": 1, "private": 2, "public": 3}
        active.sort(key=lambda c: order.get(c.type, 9))

        per_channel_cap = max(5, limit // max(len(active), 1))
        oldest_ts = f"{since_unix:.6f}"

        own_name = self._resolve_user(own)
        for ch in active:
            try:
                msgs = self._fetch_channel(ch.id, ch.name, oldest_ts, per_channel_cap)
            except Exception:
                log.exception("slack history failed for %s/%s", self._workspace.name, ch.name)
                continue
            for m in msgs:
                # Compare op zowel raw uid (uit raw_meta) als op resolved
                # naam — zodat de direction-check niet kapot gaat door de
                # name-resolutie.
                raw_uid = (m.raw_meta or {}).get("slack_user_id", "")
                if raw_uid == own or (own_name and m.from_addr == own_name):
                    m.direction = "out"
                else:
                    m.direction = "in"
                yield m

    def _resolve_user(self, uid: str) -> str:
        """Resolve Slack user-id (U…) of bot-id (B…) naar leesbare naam.
        Hergebruikt de cache van SlackClient (laadt users_list eenmalig
        per worker-lifecycle). Bij onbekend/fail: returnt 'name (U…)' zodat
        de raw ID nog terugvindbaar is voor debug maar de UI iets
        nuttigs toont. Lege uid → 'unknown'."""
        if not uid:
            return "unknown"
        try:
            api = self._client._api()
            names = self._client._user_names(api)
        except Exception:
            return uid
        resolved = names.get(uid)
        if resolved and resolved != uid:
            return resolved
        return uid

    def _fetch_channel(self, channel_id: str, channel_name: str,
                       oldest_ts: str, limit: int) -> list[CommItem]:
        api = self._client._api()
        r = api.conversations_history(channel=channel_id, oldest=oldest_ts, limit=limit)
        out: list[CommItem] = []
        for m in r.get("messages") or []:
            ts = str(m.get("ts", ""))
            if not ts:
                continue
            try:
                occurred = int(float(ts))
            except ValueError:
                continue
            uid = m.get("user", m.get("bot_id", "")) or ""
            from_addr = self._resolve_user(uid)
            text = str(m.get("text", "") or "")
            out.append(CommItem(
                source="slack",
                account=self._workspace.name,
                external_id=f"{channel_id}:{ts}",
                folder=channel_name,
                direction="in",   # patched by caller against own user-id
                from_addr=from_addr,
                to_addrs=[channel_id],
                cc_addrs=[],
                subject="",
                occurred_at=occurred,
                body_full=text,
                thread_ref=m.get("thread_ts"),
                raw_meta={"slack_subtype": m.get("subtype"),
                           "slack_user_id": uid},
            ))
        return out
