"""First-boot detection + welkomstbericht.

Als Rosa voor het eerst start na een verse wizard-installatie:
stuur de user een iMessage die uitlegt dat ze aan staat, hoe ze
verder kan (help/test), en wat er verder verwacht kan worden.

Detectie: een marker-file `first_boot_done` in ROSA_HOME. Ontbreekt
= eerste boot. We schrijven 'em direct na versturen zodat een crash
tussen versturen en schrijven hooguit een dubbele welkomstbericht
geeft (acceptabel).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


_MARKER_NAME = "first_boot_done"


def _marker_path() -> Path:
    from core.config import get_rosa_home
    return get_rosa_home() / _MARKER_NAME


def is_first_boot() -> bool:
    return not _marker_path().exists()


def mark_first_boot_done() -> None:
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("done\n", encoding="utf-8")


def welcome_message(user_name: str) -> str:
    """De letterlijke tekst van Rosa's eerste iMessage aan een nieuwe user."""
    first = (user_name or "there").split()[0] or "there"
    return (
        f"Hi {first} — Rosa here. I'm up and running.\n\n"
        "A few quick things you can ask me:\n"
        "  • 'help' — see what I can do\n"
        "  • 'status' — check I'm still alive\n"
        "  • 'test' — send me a short test scenario\n\n"
        "Morning briefing lands in your inbox tomorrow at your "
        "configured time. If anything feels off, run `rosa doctor` in a "
        "terminal — it'll tell you what's wrong."
    )


def send_welcome_if_first_boot(
    *,
    user_name: str,
    handle: str,
    sender: Callable[[str, str], None],
) -> bool:
    """Return True als we een welkomstbericht hebben gestuurd, False anders."""
    if not is_first_boot():
        return False
    try:
        sender(handle, welcome_message(user_name))
        mark_first_boot_done()
        log.info("welcome-message sent to %s (first boot)", handle)
        return True
    except Exception:
        log.exception("failed to send welcome message")
        return False
