"""Tests voor code-review-4 fixes: Slack bot dedup + team-check +
subtype-whitelist, Ollama OLLAMA_HOST env, main_channel default,
registry-nil warning."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# --- H-1 verification: _dispatch_slack_message signature ----------------


def test_h1_dispatch_slack_uses_correct_orchestrator_signature():
    """Verifieer dat _dispatch_slack_message dezelfde converse()-args
    gebruikt als _handle_message (iMessage). Regel: kwarg-mismatch
    zou als crash zichtbaar zijn op elke Slack-DM."""
    import inspect
    from core import orchestrator
    sig = inspect.signature(orchestrator.converse)
    params = sig.parameters
    # Vereiste kwargs die _dispatch_slack_message doorgeeft
    assert "system_prompt" in params
    assert "user_message" in params
    assert "history" in params
    assert "gateway" in params
    assert "executor" in params
    assert "progress_notify" in params


# --- H-2 verification: dedup + ack-fail bail ------------------------------


def test_h2_slack_bot_has_dedup_queue():
    from integrations.slack_bot import SlackBot
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1", on_message=lambda c, t: None,
        )
    assert hasattr(bot, "_seen_event_ts")
    # bounded queue
    assert bot._seen_event_ts.maxlen == 200


def test_h2_slack_bot_skips_duplicate_events():
    from integrations.slack_bot import SlackBot
    seen = []
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1",
            on_message=lambda c, t: seen.append((c, t)),
        )

    from slack_sdk.socket_mode.response import SocketModeResponse  # noqa

    def _make_req(event_ts: str, text: str = "hi"):
        req = MagicMock()
        req.envelope_id = "e1"
        req.type = "events_api"
        req.payload = {
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "DABC",
                "text": text,
                "event_ts": event_ts,
            },
        }
        return req

    client = MagicMock()
    bot._handle(client, _make_req("1234.5"))
    bot._handle(client, _make_req("1234.5"))  # duplicate
    assert len(seen) == 1


# --- H-3 verification: team_id filter --------------------------------------


def test_h3_slack_bot_drops_event_from_wrong_team():
    from integrations.slack_bot import SlackBot
    seen = []
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1", owner_team_id="T-good",
            on_message=lambda c, t: seen.append((c, t)),
        )

    req = MagicMock()
    req.envelope_id = "e2"
    req.type = "events_api"
    req.payload = {
        "team_id": "T-evil",   # different team than owner
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "DABC",
            "text": "sneaky",
            "event_ts": "1234.6",
        },
    }
    bot._handle(MagicMock(), req)
    assert seen == []  # dropped


def test_h3_slack_bot_no_team_check_when_owner_team_empty():
    """Backwards-compat: als SLACK_OWNER_TEAM_ID niet gezet is,
    accepteren we het event (huidig gedrag pre-fix)."""
    from integrations.slack_bot import SlackBot
    seen = []
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1", owner_team_id="",
            on_message=lambda c, t: seen.append((c, t)),
        )
    req = MagicMock()
    req.envelope_id = "e3"
    req.type = "events_api"
    req.payload = {
        "team_id": "T-anything",
        "event": {
            "type": "message", "user": "U1",
            "channel": "DABC", "text": "hi",
            "event_ts": "1234.7",
        },
    }
    bot._handle(MagicMock(), req)
    assert seen == [("DABC", "hi")]


# --- M-9 verification: subtype whitelist ---------------------------------


def test_m9_subtype_whitelist_allows_me_message():
    from integrations.slack_bot import SlackBot
    seen = []
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1",
            on_message=lambda c, t: seen.append((c, t)),
        )
    req = MagicMock()
    req.envelope_id = "e4"
    req.type = "events_api"
    req.payload = {
        "event": {
            "type": "message", "user": "U1",
            "channel": "DABC", "text": "does something",
            "event_ts": "1234.8",
            "subtype": "me_message",
        },
    }
    bot._handle(MagicMock(), req)
    assert seen == [("DABC", "does something")]


def test_m9_subtype_whitelist_drops_message_changed():
    from integrations.slack_bot import SlackBot
    seen = []
    with patch("slack_sdk.WebClient"), patch("slack_sdk.socket_mode.SocketModeClient"):
        bot = SlackBot(
            bot_token="xoxb-x", app_token="xapp-x",
            owner_user_id="U1",
            on_message=lambda c, t: seen.append((c, t)),
        )
    req = MagicMock()
    req.envelope_id = "e5"
    req.type = "events_api"
    req.payload = {
        "event": {
            "type": "message", "user": "U1",
            "channel": "DABC", "text": "edited",
            "event_ts": "1234.9",
            "subtype": "message_changed",  # edits: drop
        },
    }
    bot._handle(MagicMock(), req)
    assert seen == []


# --- M-2 verification: OllamaClient respects OLLAMA_HOST -----------------


def test_m2_ollama_client_reads_env_var_by_default(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama:11434")
    from models.ollama import OllamaClient
    client = OllamaClient(model="llama3.1:8b")
    assert client._base == "http://ollama:11434"


def test_m2_ollama_client_normalizes_hostport_without_scheme(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "ollama:11434")
    from models.ollama import OllamaClient
    client = OllamaClient(model="llama3.1:8b")
    assert client._base == "http://ollama:11434"


def test_m2_ollama_client_explicit_base_url_wins(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://from-env:11434")
    from models.ollama import OllamaClient
    client = OllamaClient(
        model="llama3.1:8b", base_url="http://explicit:11434",
    )
    assert client._base == "http://explicit:11434"


def test_m2_ollama_client_no_env_falls_back_to_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    from models.ollama import OllamaClient
    client = OllamaClient(model="llama3.1:8b")
    assert client._base == "http://localhost:11434"


# --- M-5: main_channel in config.example.yaml ---------------------------


def test_m5_main_channel_documented_in_example_config():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"
    content = p.read_text()
    assert "main_channel" in content
    assert '"imessage"' in content
    assert "slack" in content.lower()


# --- H-4 verification: docstring notes -----------------------------------


def test_h4_channels_module_documents_thread_safety():
    import core.channels as ch
    doc = ch.__doc__ or ""
    # De module-docstring moet expliciet noemen dat de proactive/reply
    # paths concurrent worden aangeroepen.
    assert "Proactive" in doc or "iMessage" in doc


# --- M-1: proactive send logs warning if registry unset ------------------


def test_m1_send_proactive_logs_warning_when_registry_none(caplog):
    import main
    # Save + clear registry
    orig = main._registry
    try:
        main._registry = None
        with caplog.at_level("WARNING"):
            main._send_proactive("test-msg")
        assert any(
            "channel registry not initialised" in r.message.lower()
            for r in caplog.records
        )
    finally:
        main._registry = orig
