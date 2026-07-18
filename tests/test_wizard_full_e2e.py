"""End-to-end flow test — doorloop alle 15 wizard-stappen in de goede
volgorde via de TestClient en verifieer eindstate.

Guard rail: als iemand een stap toevoegt maar `mark_done` vergeet
(zoals eerder met imessage), faalt deze test.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from wizard import google_oauth
    from wizard import server as srv
    srv.reset_finish_event()
    google_oauth.clear_pending()
    from wizard.server import _SESSION_TOKEN, build_app
    c = TestClient(build_app())
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c, tmp_path


def test_full_wizard_flow_all_15_steps(client):
    """Belangrijkste guard: elke stap markeert zichzelf als completed
    en de wizard finished op confirm."""
    c, home = client

    steps = [
        ("welcome",  {"consent": True}),
        ("identity", {"name": "Alex Bakker", "email": "alex@example.com",
                      "timezone": "Europe/Berlin", "preferred_language": "en",
                      "home_city": "Berlin", "home_country": "DE",
                      "company": "Acme Ventures"}),
        ("claude",   {"anthropic_api_key": "sk-ant-e2e-test",
                      "claude_model": "claude-sonnet-4-6"}),
        ("imessage", {"primary_handle": "+31612345678",
                      "extra_handles": "a@x.com,b@x.com"}),
        ("imap",     {"token": "personal imap.fastmail.com me@x.com pw 993"}),
        ("slack",    {"token": "xoxp-real-token"}),
        ("todoist",  {"token": "a" * 40}),
        ("plaud",    {"audio_folder": "/Users/x/Plaud"}),
        ("vips",     {"items": "Jane\njane@x.com"}),
        ("uptime",   {"items": "https://acme.com"}),
        ("news",     {"items": "https://feed.com/rss"}),
        ("confidential", {"items": "legal.com"}),
        ("notifications", {"morning_time": "07:00", "midday_time": "14:00",
                           "dayclose_time": "20:00",
                           "quiet_start": "22:00", "quiet_end": "07:00"}),
        ("features", {"features": {"reminders": True, "comm_intel": True}}),
    ]
    for name, body in steps:
        r = c.post(f"/api/step/{name}", json=body)
        assert r.status_code == 200, f"{name}: {r.status_code} {r.text}"

    status = c.get("/api/status").json()
    for name, _ in steps:
        assert name in status["completed"], f"{name} not marked completed"

    # confirm should work now — all required (welcome/identity/claude) done.
    r = c.post("/api/step/confirm", json={})
    assert r.status_code == 200
    assert r.json()["finished"] is True

    # Post-conditions: file layout correct
    assert (home / "config.yaml").exists()
    assert (home / "secrets.env").exists()
    assert (home / ".wizard_state.json").exists()
    for f in ["secrets.env", ".wizard_state.json"]:
        assert (os.stat(home / f).st_mode & 0o777) == 0o600, f"{f} not 0600"

    # Config-body integrity check
    from wizard.state import load_config
    cfg = load_config(home / "config.yaml")
    assert cfg["user"]["name"] == "Alex Bakker"
    assert cfg["user"]["company"] == "Acme Ventures"
    assert cfg["runtime"]["claude_model"] == "claude-sonnet-4-6"
    assert cfg["plaud"]["audio_folder"] == "/Users/x/Plaud"
    assert cfg["vips"]["contacts"] == ["Jane", "jane@x.com"]
    assert cfg["uptime"]["urls"] == ["https://acme.com"]
    assert cfg["features"]["reminders"] is True

    # Secrets integrity
    sec = (home / "secrets.env").read_text()
    for expected in [
        "ANTHROPIC_API_KEY=sk-ant-e2e-test",
        "OWNER_IMESSAGE_HANDLE=+31612345678",
        "SLACK_USER_OAUTH_TOKEN=xoxp-real-token",
        "TODOIST_API_TOKEN=" + ("a" * 40),
        "IMAP_PERSONAL_PASSWORD=pw",
    ]:
        assert expected in sec, f"missing in secrets.env: {expected}"


def test_confirm_with_only_required_completed(client):
    """Non-required stappen mogen open blijven — confirm werkt zolang
    welcome/identity/claude done zijn."""
    c, _ = client
    c.post("/api/step/welcome", json={"consent": True})
    c.post("/api/step/identity", json={"name": "Alex"})
    c.post("/api/step/claude", json={"anthropic_api_key": "sk-ant-x"})
    r = c.post("/api/step/confirm", json={})
    assert r.status_code == 200
    assert r.json()["finished"] is True


def test_confirm_blocks_missing_required(client):
    c, _ = client
    c.post("/api/step/welcome", json={"consent": True})
    # skip identity + claude
    r = c.post("/api/step/confirm", json={})
    assert r.status_code == 400
