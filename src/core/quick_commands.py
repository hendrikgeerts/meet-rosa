"""Quick-command handler — beantwoordt een paar veelgebruikte user-commands
lokaal zonder Claude-call, zodat een verse user direct feedback krijgt.

Wordt aangeroepen in `main._handle_message` vóór de gateway-call.
Return None → let orchestrator handle it. Return a string → send that
back and skip Claude.
"""
from __future__ import annotations

from typing import Any


def _help_text() -> str:
    return (
        "Here's what I can do:\n\n"
        "• Read your Gmail + Google Calendar (if connected)\n"
        "• Watch a Plaud recorder folder for new recordings\n"
        "• Cross-channel triage (mail/Slack/Plaud → open loops)\n"
        "• Morning briefing, midday heads-up, day-close\n"
        "• Reminders — say 'remind me to X tomorrow 10am'\n"
        "• Ask me anything — I'll answer via Claude, keeping any "
        "confidential-domain mail local.\n\n"
        "Diagnostics: run `rosa doctor` in a terminal.\n"
        "Backup: run `rosa backup` — writes a tar.gz to your home."
    )


def _status_text(settings: Any) -> str:
    return (
        f"✓ Rosa is up.\n"
        f"  user      {settings.user_name}\n"
        f"  model     {settings.claude_model}\n"
        f"  local     {settings.local_model_main}\n"
        f"  home      {settings.data_dir.parent}"
    )


def _test_text() -> str:
    return (
        "Quick test:\n"
        "1. Say 'remind me to test rosa in 2 minutes'\n"
        "   → You should get a reminder in ~2 min via iMessage.\n"
        "2. Say 'what's on my calendar today?'\n"
        "   → I'll list today's meetings from Google Calendar.\n"
        "3. Say 'help'\n"
        "   → prints this quick reference again.\n"
        "\n"
        "If any of these hangs > 30 seconds, run `rosa doctor` and paste "
        "the output to whoever set this up."
    )


def try_quick_command(text: str, settings: Any) -> str | None:
    """Return response-text if this is a quick-command, else None."""
    stripped = text.strip().lower()
    if stripped in ("help", "/help", "?"):
        return _help_text()
    if stripped in ("status", "/status", "are you alive?", "are you up?"):
        return _status_text(settings)
    if stripped in ("test", "/test"):
        return _test_text()
    return None
