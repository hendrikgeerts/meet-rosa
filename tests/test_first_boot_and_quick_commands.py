"""Tests voor first_boot detection + quick_commands (help/status/test)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# --- first_boot ------------------------------------------------------------


def test_first_boot_detects_new_install(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from core.first_boot import is_first_boot
    assert is_first_boot() is True


def test_first_boot_flips_after_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from core.first_boot import is_first_boot, mark_first_boot_done
    assert is_first_boot() is True
    mark_first_boot_done()
    assert is_first_boot() is False


def test_welcome_message_greets_by_first_name():
    from core.first_boot import welcome_message
    msg = welcome_message("Alex Bakker")
    assert "Hi Alex" in msg
    assert "help" in msg
    assert "status" in msg
    assert "test" in msg
    assert "rosa doctor" in msg


def test_welcome_message_falls_back_when_no_name():
    from core.first_boot import welcome_message
    msg = welcome_message("")
    assert "Hi there" in msg


def test_send_welcome_only_first_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from core.first_boot import send_welcome_if_first_boot
    sends = []
    sender = lambda handle, text: sends.append((handle, text))

    sent = send_welcome_if_first_boot(
        user_name="Alex", handle="+31600000000", sender=sender,
    )
    assert sent is True
    assert len(sends) == 1

    # Second call → no-op
    sent2 = send_welcome_if_first_boot(
        user_name="Alex", handle="+31600000000", sender=sender,
    )
    assert sent2 is False
    assert len(sends) == 1


def test_send_welcome_survives_send_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from core.first_boot import is_first_boot, send_welcome_if_first_boot

    def failing_sender(h, t):
        raise RuntimeError("iMessage bridge unreachable")

    sent = send_welcome_if_first_boot(
        user_name="Alex", handle="+31600000000", sender=failing_sender,
    )
    assert sent is False
    # marker NOT set so next boot will retry
    assert is_first_boot() is True


# --- quick_commands --------------------------------------------------------


@pytest.fixture
def settings_mock():
    s = MagicMock()
    s.user_name = "Alex Bakker"
    s.claude_model = "claude-sonnet-4-6"
    s.local_model_main = "llama3.1:8b"
    s.data_dir = MagicMock()
    s.data_dir.parent = "/tmp/rosa-test"
    return s


def test_quick_help(settings_mock):
    from core.quick_commands import try_quick_command
    reply = try_quick_command("help", settings_mock)
    assert reply is not None
    assert "can do" in reply.lower()
    assert "rosa doctor" in reply.lower()


def test_quick_help_variants(settings_mock):
    from core.quick_commands import try_quick_command
    for cmd in ("help", "/help", "?", "HELP", " Help "):
        assert try_quick_command(cmd, settings_mock) is not None


def test_quick_status_shows_user_name(settings_mock):
    from core.quick_commands import try_quick_command
    reply = try_quick_command("status", settings_mock)
    assert reply is not None
    assert "Alex Bakker" in reply
    assert "claude-sonnet-4-6" in reply


def test_quick_test_lists_scenarios(settings_mock):
    from core.quick_commands import try_quick_command
    reply = try_quick_command("test", settings_mock)
    assert reply is not None
    assert "remind me" in reply.lower()
    assert "calendar" in reply.lower()


def test_quick_returns_none_for_normal_message(settings_mock):
    from core.quick_commands import try_quick_command
    assert try_quick_command("Wat is het weer vandaag?",
                             settings_mock) is None
    assert try_quick_command("Hoi Rosa, hoe gaat het?",
                             settings_mock) is None
