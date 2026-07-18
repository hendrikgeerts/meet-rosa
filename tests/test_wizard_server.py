"""Smoke tests voor de setup-wizard.

Doel: garanderen dat de wizard-flow van start tot finish werkt zonder
dat een human in de browser hoeft te klikken. Verifieert:

  - GET /api/status draait tegen een lege ROSA_HOME
  - POST welcome/identity/claude schrijven naar config.yaml + secrets.env
  - Skip-endpoint markeert non-required steps
  - Confirm faalt als een required step mist en slaagt als alles er is
  - secrets.env krijgt 0600 perms (privacy-critical)
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def rosa_home(tmp_path, monkeypatch):
    home = tmp_path / "rosa-home"
    home.mkdir()
    monkeypatch.setenv("ROSA_HOME", str(home))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    # Reset wizard module-state tussen tests (in-memory FINISH_EVENT).
    from wizard import server as srv
    srv.reset_finish_event()
    yield home


@pytest.fixture
def client(rosa_home):
    from wizard.server import _SESSION_TOKEN, build_app
    app = build_app()
    c = TestClient(app)
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c


def test_status_on_empty_home(client, rosa_home):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "welcome" in body["steps"]
    assert body["completed"] == []
    assert body["finished"] is False


def test_wrong_token_rejected(rosa_home):
    from wizard.server import build_app
    app = build_app()
    c = TestClient(app)
    c.headers["X-Wizard-Token"] = "wrong"
    r = c.get("/api/status")
    assert r.status_code == 403


def test_welcome_requires_consent(client):
    r = client.post("/api/step/welcome", json={"consent": False})
    assert r.status_code == 400
    r = client.post("/api/step/welcome", json={"consent": True})
    assert r.status_code == 200


def test_identity_writes_config(client, rosa_home):
    r = client.post("/api/step/identity", json={
        "name": "Alex Bakker",
        "email": "alex@example.com",
        "timezone": "Europe/Amsterdam",
        "preferred_language": "nl",
        "home_city": "Amsterdam",
        "home_country": "NL",
    })
    assert r.status_code == 200
    cfg = (rosa_home / "config.yaml").read_text()
    assert "Alex Bakker" in cfg
    assert "alex@example.com" in cfg
    assert "Amsterdam" in cfg


def test_identity_rejects_empty_name(client):
    r = client.post("/api/step/identity", json={"name": "", "email": ""})
    assert r.status_code == 400


def test_claude_writes_secret_0600(client, rosa_home):
    r = client.post("/api/step/claude", json={
        "anthropic_api_key": "sk-ant-test-abc123",
    })
    assert r.status_code == 200
    sec = rosa_home / "secrets.env"
    assert sec.exists()
    assert "sk-ant-test-abc123" in sec.read_text()
    mode = os.stat(sec).st_mode & 0o777
    assert mode == 0o600, f"expected 0600 for secrets.env, got {oct(mode)}"


def test_claude_rejects_bad_key_format(client):
    r = client.post("/api/step/claude", json={
        "anthropic_api_key": "sk-openai-nope",
    })
    assert r.status_code == 400


def test_imessage_marks_step_completed(client, rosa_home):
    """Regressie voor bug uit live E2E-test: iMessage-endpoint schreef
    secrets maar markeerde de stap niet als done, waardoor de user
    stuck bleef in de wizard-loop."""
    r = client.post("/api/step/imessage", json={
        "primary_handle": "+31612345678",
        "extra_handles": "you@icloud.com,+31687654321",
    })
    assert r.status_code == 200
    status = client.get("/api/status").json()
    assert "imessage" in status["completed"]
    body = (rosa_home / "secrets.env").read_text()
    assert "OWNER_IMESSAGE_HANDLE" in body
    assert "+31612345678" in body
    assert "you@icloud.com" in body


def test_imessage_rejects_empty_handle(client):
    r = client.post("/api/step/imessage", json={"primary_handle": ""})
    assert r.status_code == 400


def test_imessage_accepts_string_extra(client, rosa_home):
    """Extra handles kunnen als comma-separated string komen (browser)
    of als list (API-caller)."""
    r = client.post("/api/step/imessage", json={
        "primary_handle": "+31612345678",
        "extra_handles": "a@x.com, b@x.com",  # notice space after comma
    })
    assert r.status_code == 200
    body = (rosa_home / "secrets.env").read_text()
    assert "a@x.com,b@x.com" in body  # gestript


def test_confirm_blocks_when_required_missing(client):
    r = client.post("/api/step/confirm", json={})
    assert r.status_code == 400


def test_full_happy_path(client, rosa_home):
    # welcome
    assert client.post("/api/step/welcome",
                       json={"consent": True}).status_code == 200
    # identity
    assert client.post("/api/step/identity", json={
        "name": "Alex", "email": "alex@example.com",
    }).status_code == 200
    # claude
    assert client.post("/api/step/claude", json={
        "anthropic_api_key": "sk-ant-happy-path",
    }).status_code == 200
    # confirm
    r = client.post("/api/step/confirm", json={})
    assert r.status_code == 200
    assert r.json()["finished"] is True

    status = client.get("/api/status").json()
    assert status["finished"] is True
    assert "welcome" in status["completed"]
    assert "identity" in status["completed"]
    assert "claude" in status["completed"]
    assert "confirm" in status["completed"]

    # Files landed where they should.
    assert (rosa_home / "config.yaml").exists()
    assert (rosa_home / "secrets.env").exists()
    assert (rosa_home / ".wizard_state.json").exists()


def test_skip_marks_non_required(client, rosa_home):
    r = client.post("/api/step/skip", json={"step": "slack"})
    assert r.status_code == 200
    status = client.get("/api/status").json()
    assert "slack" in status["skipped"]


def test_skip_unknown_step_400(client):
    r = client.post("/api/step/skip", json={"step": "hackers-utopia"})
    assert r.status_code == 400
