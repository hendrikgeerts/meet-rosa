"""Tests voor MEDIUM-7: Gmail + Calendar API calls audit-gewrapped.

We controleren niet de echte HTTP-laag (mock-vrij), maar wel dat het
`_execute`-helper in beide integrations een `external_call` entry
genereert in de gebonden AuditLogger met service='gmail' resp. 'gcal'.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from core.audit import AuditLogger
from core.external_audit import bind_audit
from integrations import gcal, gmail


def _read_audit(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines() if line]


def test_gmail_execute_logs_external_call(tmp_path: Path) -> None:
    audit = AuditLogger(audit_dir=tmp_path)
    bind_audit(audit)
    try:
        req = MagicMock()
        req.execute.return_value = {"messages": [{"id": "1"}, {"id": "2"}]}
        resp = gmail._execute(req, endpoint="users.messages.list", note="max=10")
    finally:
        bind_audit(None)  # type: ignore[arg-type]

    assert resp == {"messages": [{"id": "1"}, {"id": "2"}]}
    audit_path = next(tmp_path.glob("egress-*.jsonl"))
    entries = _read_audit(audit_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "external_call"
    assert entry["service"] == "gmail"
    assert entry["endpoint"] == "users.messages.list"
    assert entry["status"] == 200
    assert entry["note"] == "max=10"
    assert "latency_ms" in entry


def test_gcal_execute_logs_external_call(tmp_path: Path) -> None:
    audit = AuditLogger(audit_dir=tmp_path)
    bind_audit(audit)
    try:
        req = MagicMock()
        req.execute.return_value = {"items": []}
        gcal._execute(req, endpoint="events.list", note="search")
    finally:
        bind_audit(None)  # type: ignore[arg-type]

    audit_path = next(tmp_path.glob("egress-*.jsonl"))
    entries = _read_audit(audit_path)
    assert len(entries) == 1
    assert entries[0]["service"] == "gcal"
    assert entries[0]["endpoint"] == "events.list"
    assert entries[0]["status"] == 200


def test_gmail_execute_logs_even_on_exception(tmp_path: Path) -> None:
    """On HttpError / network failure the audit-trail should still
    record the attempt (status stays None to signal error)."""
    audit = AuditLogger(audit_dir=tmp_path)
    bind_audit(audit)
    try:
        req = MagicMock()
        req.execute.side_effect = RuntimeError("network down")
        try:
            gmail._execute(req, endpoint="users.messages.send")
        except RuntimeError:
            pass
    finally:
        bind_audit(None)  # type: ignore[arg-type]

    audit_path = next(tmp_path.glob("egress-*.jsonl"))
    entries = _read_audit(audit_path)
    assert len(entries) == 1
    assert entries[0]["service"] == "gmail"
    assert entries[0]["endpoint"] == "users.messages.send"
    # status NOT set → None signals failure
    assert entries[0].get("status") is None
