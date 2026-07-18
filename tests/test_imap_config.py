"""Tests voor de IMAP-config laag (yaml roundtrip + Keychain wiring).

Geen echte IMAP-server nodig: alleen de account-modeling, yaml IO en de
keyring-helpers. End-to-end IMAP-fetch wordt eindjaar via integration-test
of handmatig getest met `scripts/imap_account.py recent <name>`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from integrations.imap import (
    KEYRING_SERVICE, ImapAccount, ImapFolders,
    all_enabled, delete_password, get_password, load_accounts, save_accounts, set_password,
)


# --- yaml roundtrip --------------------------------------------------------

def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_accounts(tmp_path / "nope.yaml") == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    yml = tmp_path / "imap.yaml"
    accs = [
        ImapAccount(
            name="initiale", label="Initiale werk", host="mail.initiale.nl",
            port=993, ssl=True, username="you@example.com",
            folders=ImapFolders(inbox="INBOX", sent="Verzonden"),
            poll_interval_seconds=180,
        ),
        ImapAccount(
            name="dst", label="DST-Connect", host="imap.dst.nl",
            port=993, ssl=True, username="you@example.com",
            enabled=False,
        ),
    ]
    save_accounts(yml, accs)
    loaded = load_accounts(yml)
    assert len(loaded) == 2
    assert loaded[0] == accs[0]
    assert loaded[1] == accs[1]


def test_save_locks_file_perms(tmp_path: Path) -> None:
    """yaml bevat geen secrets, maar wel je IMAP-host + username — 0600
    om gluren door andere users te voorkomen."""
    yml = tmp_path / "imap.yaml"
    save_accounts(yml, [ImapAccount(
        name="x", label="x", host="h", port=993, ssl=True, username="u",
    )])
    assert oct(yml.stat().st_mode)[-3:] == "600"


def test_account_defaults_are_sensible() -> None:
    a = ImapAccount(name="x", label="x", host="h", port=993, ssl=True, username="u")
    assert a.folders.inbox == "INBOX"
    assert a.folders.sent == "Sent"
    assert a.enabled is True
    assert a.poll_interval_seconds == 300


# --- keyring wiring (mocked, no real Keychain) -----------------------------

def _acc(name: str = "test") -> ImapAccount:
    return ImapAccount(name=name, label=name, host="h", port=993, ssl=True, username="u")


def test_get_password_consults_keyring() -> None:
    with patch("integrations.imap.keyring") as kr:
        kr.get_password.return_value = "s3cret"
        assert get_password(_acc("init")) == "s3cret"
        kr.get_password.assert_called_once_with(KEYRING_SERVICE, "init")


def test_set_password_writes_to_keyring() -> None:
    with patch("integrations.imap.keyring") as kr:
        set_password(_acc("init"), "s3cret")
        kr.set_password.assert_called_once_with(KEYRING_SERVICE, "init", "s3cret")


def test_delete_password_swallows_not_found() -> None:
    """Idempotent delete — bv. bij `remove` zonder dat een password ooit
    is gezet (CLI mag niet crashen)."""
    import keyring.errors
    with patch("integrations.imap.keyring") as kr:
        kr.errors = keyring.errors
        kr.delete_password.side_effect = keyring.errors.PasswordDeleteError
        delete_password(_acc("init"))  # geen exception
        kr.delete_password.assert_called_once()


# --- iteration helper -----------------------------------------------------

def test_all_enabled_skips_disabled_and_passwordless(tmp_path: Path) -> None:
    yml = tmp_path / "imap.yaml"
    save_accounts(yml, [
        ImapAccount(name="ok", label="ok", host="h", port=993, ssl=True, username="u"),
        ImapAccount(name="off", label="off", host="h", port=993, ssl=True, username="u",
                    enabled=False),
        ImapAccount(name="nopw", label="nopw", host="h", port=993, ssl=True, username="u"),
    ])

    def _fake_get(account: ImapAccount) -> str | None:
        if account.name == "ok":
            return "s3cret"
        return None

    with patch("integrations.imap.get_password", side_effect=_fake_get):
        out = list(all_enabled(yml))

    assert [a.name for a, _ in out] == ["ok"]
    assert out[0][1] == "s3cret"
