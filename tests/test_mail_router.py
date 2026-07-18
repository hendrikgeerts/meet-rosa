"""Tests voor mail_router routing logic — geen netwerkcalls, alle backends gemockt."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from integrations.imap import ImapAccount, ImapFolders
from integrations.mail_router import _account_matches, send


def _account(
    name: str = "hge", username: str = "hendrik@hge.nl",
    from_address: str | None = None, smtp_host: str | None = "smtp.hge.nl",
) -> ImapAccount:
    return ImapAccount(
        name=name, label=name.title(), host=f"imap.{name}.nl", port=993,
        ssl=True, username=username, folders=ImapFolders(),
        smtp_host=smtp_host, from_address=from_address,
    )


def test_account_matches_via_from_address() -> None:
    acc = _account(username="user@hge.nl", from_address="hendrik@hge.nl")
    assert _account_matches(acc, "hendrik@hge.nl")
    # Beide adressen routeren naar dit account — handig als de IMAP-login
    # afwijkt van het displayed From-adres.
    assert _account_matches(acc, "user@hge.nl")


def test_account_matches_via_username_when_no_from_address() -> None:
    acc = _account(username="hendrik@hge.nl", from_address=None)
    assert _account_matches(acc, "hendrik@hge.nl")
    assert _account_matches(acc, "HENDRIK@HGE.NL")   # case-insensitive
    assert not _account_matches(acc, "iemand@anders.nl")


def test_account_matches_empty_address_is_false() -> None:
    acc = _account()
    assert not _account_matches(acc, "")
    assert not _account_matches(acc, "   ")


def test_send_routes_to_smtp_when_account_matches() -> None:
    acc = _account(name="hge", from_address="hendrik@hge.nl",
                   smtp_host="smtp.hge.nl")
    gmail = MagicMock()
    with patch("integrations.mail_router.smtp_send",
               return_value="<msgid@server>") as smtp:
        result = send(
            from_address="hendrik@hge.nl",
            to="klant@x.nl", subject="re: afspraak", body="hoi",
            gmail=gmail, imap_accounts=[acc],
        )
    assert result.backend == "smtp:hge"
    assert result.message_id == "<msgid@server>"
    smtp.assert_called_once()
    gmail.send.assert_not_called()


def test_send_falls_back_to_gmail_when_no_match() -> None:
    acc = _account(name="hge", from_address="hendrik@hge.nl")
    gmail = MagicMock()
    gmail.send.return_value = {"id": "g123", "thread_id": "t456"}
    with patch("integrations.mail_router.smtp_send") as smtp:
        result = send(
            from_address="you@example.com",
            to="klant@x.nl", subject="re: x", body="y",
            gmail=gmail, imap_accounts=[acc],
        )
    assert result.backend == "gmail"
    assert result.thread_id == "t456"
    smtp.assert_not_called()
    gmail.send.assert_called_once()


def test_send_falls_back_to_gmail_when_account_lacks_smtp() -> None:
    """Account matcht het adres maar heeft geen smtp_host → log warning,
    Gmail-fallback (anders zou de mail mislukken)."""
    acc = _account(name="legacy", username="user@example.com", smtp_host=None)
    gmail = MagicMock()
    gmail.send.return_value = {"id": "g999", "thread_id": "tX"}
    with patch("integrations.mail_router.smtp_send") as smtp:
        result = send(
            from_address="user@example.com",
            to="x@y.nl", subject="x", body="y",
            gmail=gmail, imap_accounts=[acc],
        )
    assert result.backend == "gmail"
    smtp.assert_not_called()
