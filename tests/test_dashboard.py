"""Smoke-tests voor het lokale audit-dashboard.

Gebruikt FastAPI's TestClient — geen echte server, geen netwerk-binding."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def populated_audit(tmp_path: Path) -> Path:
    """Schrijf één egress + één matching payload in tmp_path."""
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    ts = "2026-04-22T19:30:00+02:00"
    egress = {
        "ts": ts, "event": "claude_call", "task": "morning_briefing",
        "label": "internal", "model": "claude-fake", "stop_reason": "end_turn",
        "input_tokens": 100, "output_tokens": 50, "tools_offered": 0,
        "redactions_applied": 2, "classifier_reason": "default",
    }
    payload = {
        "ts": ts, "task": "morning_briefing", "label": "internal",
        "model": "claude-fake", "backend": "claude",
        "classifier_reason": "default", "redactions_applied": 2,
        "stop_reason": "end_turn", "input_tokens": 100, "output_tokens": 50,
        "tools_offered": [],
        "system_redacted": "Schrijf een korte briefing",
        "messages_redacted": [{"role": "user", "content": "[PERSON_001] heeft gemaild"}],
        "response_text": "Goedemorgen Hendrik.",
    }
    (tmp_path / f"egress-{today}.jsonl").write_text(json.dumps(egress) + "\n")
    (tmp_path / f"payloads-{today}.jsonl").write_text(json.dumps(payload) + "\n")
    return tmp_path


@pytest.fixture
def client(populated_audit: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from web.app import create_app
    # base_url must satisfy the Host-allowlist middleware (CRIT-B).
    return TestClient(create_app(populated_audit), base_url="http://127.0.0.1:8080")


def test_index_lists_dates(client) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    r = client.get("/")
    assert r.status_code == 200
    assert today in r.text


# --- CRIT-B: Host-header allowlist (DNS-rebind protection) -------------

def test_dns_rebind_host_rejected(populated_audit: Path) -> None:
    """ISO_AUDIT 2026-05 CRITICAL-B: any Host header that is not on the
    127.0.0.1 / localhost allowlist must be rejected with 403, even if
    the TCP connection landed on the loopback bind."""
    from fastapi.testclient import TestClient
    from web.app import create_app
    cli = TestClient(create_app(populated_audit),
                      base_url="http://attacker.example")
    r = cli.get("/")
    assert r.status_code == 403
    assert "Host not allowed" in r.text


def test_localhost_host_accepted(populated_audit: Path) -> None:
    from fastapi.testclient import TestClient
    from web.app import create_app
    cli = TestClient(create_app(populated_audit),
                      base_url="http://localhost:8080")
    r = cli.get("/")
    assert r.status_code == 200


def test_audit_list_shows_record(client) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    r = client.get(f"/audit?date={today}")
    assert r.status_code == 200
    assert "morning_briefing" in r.text
    assert "claude-fake" in r.text


def test_audit_filter_by_task(client) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    r = client.get(f"/audit?date={today}&task=nonexistent")
    assert r.status_code == 200
    assert "morning_briefing" not in r.text


def test_detail_shows_redacted_payload(client) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    r = client.get(f"/audit/{today}/0")
    assert r.status_code == 200
    assert "[PERSON_001]" in r.text
    assert "Schrijf een korte briefing" in r.text
    assert "Goedemorgen Hendrik" in r.text


def test_detail_leak_scanner_clean(client) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    r = client.get(f"/audit/{today}/0")
    assert "Geen PII-patterns gevonden" in r.text


def test_detail_leak_scanner_catches_email(client, populated_audit: Path) -> None:  # type: ignore[no-untyped-def]
    """Vervang de payload met eentje die een echt email bevat → leak-banner."""
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    leak = {
        "ts": "2026-04-22T20:00:00+02:00", "task": "leak", "label": "internal",
        "model": "claude-fake", "backend": "claude",
        "classifier_reason": "default", "redactions_applied": 0,
        "stop_reason": "end_turn", "input_tokens": 5, "output_tokens": 3,
        "tools_offered": [],
        "system_redacted": "",
        "messages_redacted": [{"role": "user", "content": "Mail naar piet@klant.nl graag"}],
        "response_text": "ok",
    }
    f = populated_audit / f"payloads-{today}.jsonl"
    f.write_text(f.read_text() + json.dumps(leak) + "\n")
    # rebuild egress so it shows up
    eg = {"ts": "2026-04-22T20:00:00+02:00", "event": "claude_call", "task": "leak",
          "label": "internal", "model": "claude-fake"}
    eg_f = populated_audit / f"egress-{today}.jsonl"
    eg_f.write_text(eg_f.read_text() + json.dumps(eg) + "\n")
    r = client.get(f"/audit/{today}/0")  # newest first
    assert r.status_code == 200
    assert "potentiële PII-leak" in r.text or "PII-leak" in r.text
    assert "piet@klant.nl" in r.text


def test_invalid_date_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/audit?date=../../../etc/passwd")
    assert r.status_code == 400
