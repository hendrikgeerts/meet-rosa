"""Tests voor de Slack-config laag (yaml roundtrip + Keychain wiring).

Geen echte Slack-API calls hier; SlackClient zelf wordt later via integration-
tests of handmatig getest met `scripts/slack_workspace.py test <name>`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from integrations.slack import (
    KEYRING_SERVICE, SlackWorkspace,
    all_enabled, delete_token, get_token, load_workspaces, save_workspaces, set_token,
)


# --- yaml roundtrip --------------------------------------------------------

def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_workspaces(tmp_path / "nope.yaml") == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    yml = tmp_path / "slack.yaml"
    ws = [
        SlackWorkspace(name="initiale", label="Initiale Slack",
                       workspace_url="initiale.slack.com",
                       poll_interval_seconds=180),
        SlackWorkspace(name="dst", label="DST", enabled=False),
    ]
    save_workspaces(yml, ws)
    loaded = load_workspaces(yml)
    assert loaded == ws


def test_save_locks_file_perms(tmp_path: Path) -> None:
    yml = tmp_path / "slack.yaml"
    save_workspaces(yml, [SlackWorkspace(name="x", label="x")])
    assert oct(yml.stat().st_mode)[-3:] == "600"


def test_workspace_defaults_are_sensible() -> None:
    w = SlackWorkspace(name="x", label="x")
    assert w.enabled is True
    assert w.poll_interval_seconds == 300
    assert w.workspace_url == ""


# --- keyring wiring (mocked) ---------------------------------------------

def _ws(name: str = "test") -> SlackWorkspace:
    return SlackWorkspace(name=name, label=name)


def test_get_token_consults_keyring() -> None:
    with patch("integrations.slack.keyring") as kr:
        kr.get_password.return_value = "xoxp-1-2-3"
        assert get_token(_ws("init")) == "xoxp-1-2-3"
        kr.get_password.assert_called_once_with(KEYRING_SERVICE, "init")


def test_set_token_writes_to_keyring() -> None:
    with patch("integrations.slack.keyring") as kr:
        set_token(_ws("init"), "xoxp-1-2-3")
        kr.set_password.assert_called_once_with(KEYRING_SERVICE, "init", "xoxp-1-2-3")


def test_delete_token_swallows_not_found() -> None:
    import keyring.errors
    with patch("integrations.slack.keyring") as kr:
        kr.errors = keyring.errors
        kr.delete_password.side_effect = keyring.errors.PasswordDeleteError
        delete_token(_ws("init"))   # geen exception
        kr.delete_password.assert_called_once()


# --- iteration helper -----------------------------------------------------

def test_all_enabled_skips_disabled_and_tokenless(tmp_path: Path) -> None:
    yml = tmp_path / "slack.yaml"
    save_workspaces(yml, [
        SlackWorkspace(name="ok", label="ok"),
        SlackWorkspace(name="off", label="off", enabled=False),
        SlackWorkspace(name="notok", label="notok"),
    ])

    def _fake_get(workspace: SlackWorkspace) -> str | None:
        return "xoxp-..." if workspace.name == "ok" else None

    with patch("integrations.slack.get_token", side_effect=_fake_get):
        out = list(all_enabled(yml))

    assert [w.name for w, _ in out] == ["ok"]
    assert out[0][1] == "xoxp-..."
