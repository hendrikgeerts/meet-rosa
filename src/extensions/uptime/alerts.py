"""Alert pipeline: decide → format → dispatch over channels.

Format is consistent over kanalen, content per kanaal:
- imessage  : multi-line text bericht
- voice     : kortere zin → TTS → audio attachment in dezelfde thread
- ntfy      : title + body + Critical priority + click=url

Tijd-formatting in Europe/Amsterdam.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.uptime.schema import CheckResult
from integrations.ntfy import send_push as ntfy_send

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


def _humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}u {m}m"


def _format_down_text(
    target: dict[str, Any], result: CheckResult,
    duration_seconds: int, re_alert: bool,
) -> str:
    """Multi-line iMessage body voor een down-alert.

    M1-fix: `since {HH:MM}` toont wanneer de target down ging
    (= now - duration), niet nu. Bij re-alerts las het anders als
    'Down for 1u 5m (since 15:38)' waar 15:38 het re-alert-moment was.
    """
    prefix = "🔁 STILL DOWN" if re_alert else "🔴 DOWN"
    now = datetime.now(TZ)
    down_since = now.timestamp() - duration_seconds
    down_since_hhmm = datetime.fromtimestamp(down_since, TZ).strftime("%H:%M")
    duration = _humanize_duration(duration_seconds)

    error_line: str
    if result.status_code is not None:
        error_line = f"HTTP {result.status_code}"
        if result.error:
            error_line += f" — {result.error}"
    elif result.error:
        error_line = result.error
    else:
        error_line = "unknown error"

    return (
        f"{prefix}: {target['name']}\n"
        f"URL: {target['url']}\n"
        f"Status: {error_line}\n"
        f"Down for {duration} (since {down_since_hhmm})\n"
        f"Latency at fail: {result.latency_ms}ms"
    )


def _format_down_voice(
    target: dict[str, Any], result: CheckResult,
    duration_seconds: int, re_alert: bool,
) -> str:
    """Compacte zin voor TTS — Rosa's stem leest dit voor."""
    prefix = "Still down" if re_alert else "Alert"
    duration = _humanize_duration(duration_seconds)
    return f"{prefix}. {target['name']} is offline. Down for {duration}."


def _format_recovery_text(
    target: dict[str, Any], duration_seconds: int,
) -> str:
    duration = _humanize_duration(duration_seconds)
    now_hhmm = datetime.now(TZ).strftime("%H:%M")
    return (
        f"✅ RECOVERED: {target['name']}\n"
        f"URL: {target['url']}\n"
        f"Back online at {now_hhmm} — total downtime {duration}."
    )


def dispatch_alert(
    *,
    target: dict[str, Any],
    result: CheckResult | None,
    duration_seconds: int,
    re_alert: bool,
    kind: str,                            # 'down' | 'recovery'
    channels: set[str],
    send_imessage: Callable[[str, str], None],
    primary_handle: str,
    tts_synthesize: Callable[..., Path] | None = None,
    tts_voice: str = "Ava (Enhanced)",
    send_imessage_audio: Callable[[str, Path], None] | None = None,
    ntfy_topic: str | None = None,
    ntfy_server: str = "https://ntfy.sh",
) -> str:
    """Stuur over alle channels in `channels`. Returns het iMessage-body
    (handig voor logging). Eén channel falen is non-fatal — de andere
    proberen we nog steeds."""
    if kind == "down":
        assert result is not None
        text = _format_down_text(target, result, duration_seconds, re_alert)
        voice_text = _format_down_voice(target, result, duration_seconds, re_alert)
        ntfy_title = f"{'STILL DOWN' if re_alert else 'DOWN'}: {target['name']}"
        ntfy_priority = 5  # Critical — break Do-Not-Disturb
        ntfy_tags = ["rotating_light"]
    elif kind == "recovery":
        text = _format_recovery_text(target, duration_seconds)
        voice_text = None
        ntfy_title = f"Recovered: {target['name']}"
        ntfy_priority = 3
        ntfy_tags = ["white_check_mark"]
    else:
        log.warning("dispatch_alert: unknown kind %r — skip", kind)
        return ""

    # iMessage
    if "imessage" in channels:
        try:
            send_imessage(primary_handle, text)
        except Exception:
            log.exception("uptime: iMessage send failed")

    # voice-bubble — alleen bij down, alleen als TTS + audio-send beschikbaar
    if (
        "voice" in channels and kind == "down"
        and tts_synthesize is not None and send_imessage_audio is not None
        and voice_text is not None
    ):
        try:
            audio_path = tts_synthesize(voice_text, engine="say", voice=tts_voice)
            send_imessage_audio(primary_handle, audio_path)
        except Exception:
            log.exception("uptime: voice-bubble send failed")

    # Ntfy.sh push — critical priority breakt Do-Not-Disturb
    if "ntfy" in channels and ntfy_topic:
        try:
            ntfy_send(
                server=ntfy_server, topic=ntfy_topic,
                title=ntfy_title, message=text,
                priority=ntfy_priority, tags=ntfy_tags,
                click_url=target["url"],
            )
        except Exception:
            log.exception("uptime: ntfy push failed")

    return text
