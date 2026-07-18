"""Tests voor GmailSource.archive — wordt gebruikt door de PA-LOC
auto-archive flow zodat Hendrik's inbox niet vol loopt met
[PA-LOC] mails."""
from __future__ import annotations

from unittest.mock import MagicMock

from extensions.comm_intel.sources.gmail_source import GmailSource


def test_archive_calls_gmail_client_archive() -> None:
    client = MagicMock()
    source = GmailSource(client)
    ok = source.archive("msg_abc")
    assert ok is True
    client.archive.assert_called_once_with("msg_abc")


def test_archive_returns_false_on_api_failure() -> None:
    """API-fout mag de ingest niet breken — log + return False."""
    client = MagicMock()
    client.archive.side_effect = RuntimeError("Gmail API down")
    source = GmailSource(client)
    ok = source.archive("msg_xyz")
    assert ok is False
    client.archive.assert_called_once_with("msg_xyz")


def test_gmail_client_archive_uses_modify_endpoint() -> None:
    """De wrapper op GmailClient moet via _execute (audit-wrap) lopen
    zodat elke archive-call in egress-jsonl belandt."""
    from unittest.mock import patch

    from integrations.gmail import GmailClient

    # Skip __init__ omdat we geen echte credentials hebben
    client = GmailClient.__new__(GmailClient)
    client._service = MagicMock()

    with patch("integrations.gmail._execute") as fake_exec:
        fake_exec.return_value = {}
        client.archive("msg_test")

    # _execute werd aangeroepen met endpoint="users.messages.modify"
    # en note="archive"
    assert fake_exec.call_count == 1
    kwargs = fake_exec.call_args.kwargs
    assert kwargs["endpoint"] == "users.messages.modify"
    assert kwargs["note"] == "archive"
