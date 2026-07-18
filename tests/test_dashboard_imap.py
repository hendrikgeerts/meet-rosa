"""Smoke-tests voor de IMAP-account CRUD pages.

Mock keyring zodat de tests geen echte macOS Keychain raken.
Mock ImapClient.test_connection zodat we geen echte IMAP-server contacten.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def imap_yaml(tmp_path: Path) -> Path:
    return tmp_path / "imap_accounts.yaml"


@pytest.fixture
def fake_keyring():
    store: dict[tuple[str, str], str] = {}

    def _set(service: str, key: str, pw: str) -> None:
        store[(service, key)] = pw

    def _get(service: str, key: str) -> str | None:
        return store.get((service, key))

    def _del(service: str, key: str) -> None:
        store.pop((service, key), None)

    with patch("integrations.imap.keyring.set_password", side_effect=_set), \
         patch("integrations.imap.keyring.get_password", side_effect=_get), \
         patch("integrations.imap.keyring.delete_password", side_effect=_del):
        yield store


@pytest.fixture
def fake_imap_test():
    """Default: test_connection succeeds."""
    with patch("web.app._imap_test", return_value=(True, "")) as m:
        yield m


@pytest.fixture
def client(imap_yaml: Path, tmp_path: Path, fake_keyring, fake_imap_test):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    return TestClient(create_app(audit, imap_yaml=imap_yaml), base_url="http://127.0.0.1:8080")


def test_index_empty_state(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/imap-accounts")
    assert r.status_code == 200
    assert "No IMAP accounts" in r.text


def test_create_account_via_form(client, imap_yaml: Path, fake_keyring) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/imap-accounts/new", data={
        "name": "newacct",
        "label": "New Account",
        "host": "mail.example.nl",
        "port": "993",
        "ssl": "1",
        "username": "user@example.nl",
        "password": "secret123",
        "folder_inbox": "INBOX",
        "folder_sent": "Sent",
        "enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/imap-accounts?message=created" in r.headers["location"]

    # Yaml schreven en password in fake-keyring
    assert imap_yaml.exists()
    assert "newacct" in imap_yaml.read_text()
    assert ("pa-agent-imap", "newacct") in fake_keyring
    assert fake_keyring[("pa-agent-imap", "newacct")] == "secret123"

    # Wachtwoord komt NIET in yaml terecht
    assert "secret123" not in imap_yaml.read_text()


def test_create_invalid_name_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/imap-accounts/new", data={
        "name": "../etc/passwd",
        "host": "x", "username": "x", "password": "x",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "invalid+name" in r.headers["location"]


def test_create_duplicate_name(client) -> None:  # type: ignore[no-untyped-def]
    base = {"host": "x", "username": "x", "password": "x", "port": "993", "ssl": "1",
            "folder_inbox": "INBOX", "folder_sent": "Sent", "enabled": "1",
            "smtp_port": "587"}
    client.post("/imap-accounts/new", data={"name": "dup", **base})
    r = client.post("/imap-accounts/new", data={"name": "dup", **base},
                     follow_redirects=False)
    assert r.status_code == 303
    assert "already+exists" in r.headers["location"]


def test_create_test_failure_keeps_yaml_but_flags_error(
    client, imap_yaml: Path,
) -> None:  # type: ignore[no-untyped-def]
    with patch("web.app._imap_test", return_value=(False, "auth failed")):
        r = client.post("/imap-accounts/new", data={
            "name": "broken", "host": "x", "username": "x", "password": "x",
            "port": "993", "ssl": "1", "folder_inbox": "INBOX",
            "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "test+failed" in r.headers["location"]
    # Account is wel geschreven (jij wilt fix-en, niet opnieuw alles intypen)
    assert "broken" in imap_yaml.read_text()


def test_edit_keeps_password_when_blank(client, fake_keyring) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "edt", "host": "x", "username": "old", "password": "orig",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    assert fake_keyring[("pa-agent-imap", "edt")] == "orig"

    # Edit zonder password — moet "orig" behouden
    r = client.post("/imap-accounts/edt/edit", data={
        "host": "y", "username": "new", "password": "",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert fake_keyring[("pa-agent-imap", "edt")] == "orig"


def test_edit_updates_password_when_provided(client, fake_keyring) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "edt2", "host": "x", "username": "u", "password": "old",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    client.post("/imap-accounts/edt2/edit", data={
        "host": "x", "username": "u", "password": "new-pw",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    assert fake_keyring[("pa-agent-imap", "edt2")] == "new-pw"


def test_test_route_succeeds(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "tst", "host": "x", "username": "u", "password": "p",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    r = client.post("/imap-accounts/tst/test", follow_redirects=False)
    assert r.status_code == 303
    assert "test+OK" in r.headers["location"]


def test_test_route_no_password_in_keychain(
    client, fake_keyring,
) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "nopw", "host": "x", "username": "u", "password": "p",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    # Verwijder uit keyring zonder via dashboard delete te gaan
    fake_keyring.pop(("pa-agent-imap", "nopw"), None)
    r = client.post("/imap-accounts/nopw/test", follow_redirects=False)
    assert r.status_code == 303
    assert "no+password" in r.headers["location"]


def test_delete_removes_yaml_and_keychain(
    client, imap_yaml: Path, fake_keyring,
) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "del", "host": "x", "username": "u", "password": "p",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    assert ("pa-agent-imap", "del") in fake_keyring

    r = client.post("/imap-accounts/del/delete", follow_redirects=False)
    assert r.status_code == 303
    assert ("pa-agent-imap", "del") not in fake_keyring
    assert "del" not in imap_yaml.read_text()


def test_index_shows_account(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/imap-accounts/new", data={
        "name": "shown", "label": "Shown Label", "host": "imap.test.nl",
        "username": "u@test.nl", "password": "p",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    })
    r = client.get("/imap-accounts")
    assert "shown" in r.text
    assert "Shown Label" in r.text
    assert "imap.test.nl" in r.text
    assert "in Keychain" in r.text


def test_edit_unknown_redirects(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/imap-accounts/nope/edit", data={
        "host": "x", "username": "u",
        "port": "993", "ssl": "1", "folder_inbox": "INBOX",
        "folder_sent": "Sent", "enabled": "1", "smtp_port": "587",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/imap-accounts"
