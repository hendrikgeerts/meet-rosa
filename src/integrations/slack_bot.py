"""Slack Socket Mode bot — bidirectional chat.

Draait in een thread naast de iMessage-poll-loop in main.py. Ontvangt
DM's van de user, dispatch't via dezelfde orchestrator als iMessage,
en stuurt het antwoord terug via `slack.WebClient.chat_postMessage`.

Vereist twee tokens in secrets.env:
  SLACK_BOT_TOKEN=xoxb-…   (Bot User OAuth Token; installatie in workspace)
  SLACK_APP_TOKEN=xapp-…   (App-Level Token voor Socket Mode)

De bot reageert ALLEEN op DM's van de user's Slack user-id
(`SLACK_OWNER_USER_ID` in secrets.env), niet op mentions in channels.
Voorkomt dat Rosa random meeluistert.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class SlackBot:
    """Wrapper rond `slack_sdk.socket_mode.SocketModeClient` met
    Rosa-specifieke event handling."""

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        owner_user_id: str,
        on_message: Callable[[str, str], None],
        owner_team_id: str = "",
    ) -> None:
        """Args:
            bot_token: xoxb-… Bot User OAuth Token
            app_token: xapp-… App-Level Token voor Socket Mode
            owner_user_id: Slack user-id van de "user" die met Rosa mag
                praten. Berichten van andere users worden genegeerd.
            on_message: callback(channel_id, text) — main.py wire't deze
                aan de orchestrator, hetzelfde als iMessage.
        """
        from collections import deque
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest

        self._web = WebClient(token=bot_token)
        self._sm = SocketModeClient(app_token=app_token, web_client=self._web)
        self._owner = owner_user_id
        self._owner_team_id = owner_team_id.strip()
        self._on_message = on_message
        self._SMR = SocketModeRequest
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # H-2 idempotency: bounded queue van recent verwerkte event_ts.
        # 200 entries dekt ~5 min bij 40 msgs/min piek — genoeg tegen
        # Slack's retry-window van 60s.
        self._seen_event_ts: deque[str] = deque(maxlen=200)

    def _handle(self, client, req) -> None:  # noqa: ANN001
        """Ack the event and dispatch to callback if it's a user-DM.

        H-2 fix: ack MOET slagen anders retriggert Slack de envelope
        binnen 3s → duplicate Claude-calls + dubbele reply. Bij
        ack-fail: return-early zonder verdere processing. Slack stuurt
        opnieuw, en de _seen_event_ts dedup laat de tweede door.
        """
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id),
            )
        except Exception:
            log.exception(
                "failed to ack slack event — returning, Slack will "
                "retry (dedup via event_ts prevents duplicates)",
            )
            return

        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        if event.get("type") != "message":
            return

        # H-2: idempotency dedup op event_ts. Slack retriggert onack'te
        # events; onze cache voorkomt dat we een tweede Claude-call doen.
        event_ts = str(event.get("event_ts") or event.get("ts") or "")
        if event_ts and event_ts in self._seen_event_ts:
            log.debug("slack duplicate event skipped: %s", event_ts)
            return
        if event_ts:
            self._seen_event_ts.append(event_ts)

        # M-9: subtype whitelist. Standard user-messages hebben géén
        # subtype. `me_message` en `file_share` ook doorlaten (user
        # intent). Alle andere (bot_message, message_changed, thread_broadcast)
        # negeren.
        if event.get("bot_id"):
            return
        subtype = event.get("subtype")
        if subtype not in (None, "me_message", "file_share"):
            return

        # H-3: enforce team_id match zodat een shared-workspace-installatie
        # met dezelfde U... id niet stiekem meepraat.
        if self._owner_team_id:
            actual_team = event.get("team") or req.payload.get("team_id", "")
            if actual_team != self._owner_team_id:
                log.warning(
                    "slack event from unexpected team %s (expected %s) — "
                    "dropping to prevent cross-workspace impersonation",
                    actual_team, self._owner_team_id,
                )
                return

        if event.get("user") != self._owner:
            return
        channel = event.get("channel", "")
        if not channel.startswith("D"):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        log.info("slack DM from %s: %s", event.get("user"), text[:80])
        try:
            self._on_message(channel, text)
        except Exception:
            log.exception("slack on_message callback failed")

    def send(self, channel: str, text: str) -> None:
        """Post a message. Called by ChannelRegistry."""
        try:
            self._web.chat_postMessage(channel=channel, text=text)
        except Exception:
            log.exception("slack chat_postMessage failed")

    def start(self) -> None:
        """Connect Socket Mode + start listening in background thread."""
        self._sm.socket_mode_request_listeners.append(self._handle)

        def _run() -> None:
            try:
                self._sm.connect()
                while not self._stop.is_set():
                    # Socket Mode client heeft eigen thread voor de WS;
                    # we blijven hier alleen zodat de outer thread niet
                    # exit vóór de WS-thread.
                    self._stop.wait(timeout=1.0)
            except Exception:
                log.exception("slack bot loop crashed")

        self._thread = threading.Thread(
            target=_run, name="slack-bot", daemon=True,
        )
        self._thread.start()
        log.info("slack bot started (Socket Mode)")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sm.disconnect()
        except Exception:
            log.exception("slack disconnect failed")
