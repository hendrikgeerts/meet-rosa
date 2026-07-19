"""Multi-channel message dispatch.

Rosa kan berichten ontvangen én sturen via meerdere kanalen: iMessage
(macOS bridge) en Slack (Socket Mode bot). Voor twee use-cases moeten
we bepalen naar welk kanaal we sturen:

1. **Reply**: Rosa antwoordt op een user-message. Antwoord gaat terug
   via HET KANAAL waar de user via schreef. Zelfs als main_channel
   = slack maar user tikte iets in iMessage, gaat het antwoord naar
   iMessage. Zorgt dat conversaties in één channel blijven.

2. **Proactive**: Rosa initieert (briefing, day-close, reminder-fire,
   welcome-message, uptime-alert). Gaat naar het `main_channel` uit
   config.yaml. Voorkomt dat je hetzelfde bericht op iMessage én
   Slack krijgt.

Alle send-sites in main.py + scheduler moeten via `send_reply()` of
`send_proactive()`. Directe `imessage.send_imessage()` calls bypassen
de main_channel routing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)


class Channel(Protocol):
    """Elke kanaal-implementatie moet `send(handle_or_channel, text)`
    kunnen. `handle_or_channel` betekent voor iMessage een phone/email
    handle, voor Slack een channel-id of user-id."""
    name: str
    def send(self, dest: str, text: str) -> None: ...


@dataclass
class ChannelRegistry:
    """Central registry van geconfigureerde output kanalen.

    Voor iMessage: dest = OWNER_IMESSAGE_HANDLE.
    Voor Slack: dest = OWNER_SLACK_USER_ID (dm) of channel-id.
    """
    channels: dict[str, Channel]
    default_dests: dict[str, str]
    main_channel: str

    def send_proactive(self, text: str) -> None:
        """Stuur naar main_channel — default iMessage. Bij fail: log
        en probeer geen andere kanalen (voorkomt dubbele-bericht bug).
        """
        ch = self.channels.get(self.main_channel)
        if ch is None:
            log.error(
                "main_channel=%s not available (missing token or "
                "disabled). Proactive message dropped: %s",
                self.main_channel, text[:80],
            )
            return
        dest = self.default_dests.get(self.main_channel, "")
        if not dest:
            log.error(
                "no default destination for main_channel=%s. Set "
                "OWNER_%s_HANDLE in secrets.env.",
                self.main_channel, self.main_channel.upper(),
            )
            return
        try:
            ch.send(dest, text)
        except Exception:
            log.exception(
                "proactive send via %s failed", self.main_channel,
            )

    def send_reply(self, origin_channel: str, dest: str, text: str) -> None:
        """Antwoord terug op HET zelfde kanaal waar de user via kwam.
        `dest` is de handle/channel-id die de user gebruikte."""
        ch = self.channels.get(origin_channel)
        if ch is None:
            log.error(
                "cannot reply on channel=%s (no client registered). "
                "Falling back to main_channel.",
                origin_channel,
            )
            self.send_proactive(text)
            return
        try:
            ch.send(dest, text)
        except Exception:
            log.exception("reply via %s failed", origin_channel)


class _IMessageAdapter:
    name = "imessage"

    def send(self, dest: str, text: str) -> None:
        from integrations import imessage
        imessage.send_imessage(dest, text)


class _SlackAdapter:
    name = "slack"

    def __init__(self, send_fn: Callable[[str, str], None]):
        self._send_fn = send_fn

    def send(self, dest: str, text: str) -> None:
        self._send_fn(dest, text)


def build_registry(
    settings,
    *,
    slack_send_fn: Callable[[str, str], None] | None = None,
    slack_default_dest: str = "",
) -> ChannelRegistry:
    """Bouw registry op basis van settings + optionele Slack-client.

    iMessage is altijd beschikbaar (macOS-only bridge). Slack alleen
    als `slack_send_fn` meegegeven — main.py wire't die in bij boot als
    slack_bidirectional feature enabled is.
    """
    channels: dict[str, Channel] = {"imessage": _IMessageAdapter()}
    default_dests: dict[str, str] = {
        "imessage": settings.primary_handle,
    }
    if slack_send_fn is not None:
        channels["slack"] = _SlackAdapter(slack_send_fn)
        default_dests["slack"] = slack_default_dest

    main = settings.main_channel
    if main not in channels:
        log.warning(
            "main_channel=%s not registered (missing config?), "
            "falling back to imessage",
            main,
        )
        main = "imessage"

    return ChannelRegistry(
        channels=channels,
        default_dests=default_dests,
        main_channel=main,
    )
