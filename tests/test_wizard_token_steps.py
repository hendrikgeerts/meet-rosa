"""Tests voor Slack / Todoist / IMAP token-endpoints in de wizard."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def rosa_home(tmp_path, monkeypatch):
    home = tmp_path / "rosa-home"
    home.mkdir()
    monkeypatch.setenv("ROSA_HOME", str(home))
    monkeypatch.delenv("ROSA_DEV", raising=False)
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


# --- Slack -----------------------------------------------------------------


def test_slack_accepts_xoxp_token(client, rosa_home):
    r = client.post("/api/step/slack", json={"token": "xoxp-abc-def-ghi"})
    assert r.status_code == 200
    body = (rosa_home / "secrets.env").read_text()
    assert "xoxp-abc-def-ghi" in body

    status = client.get("/api/status").json()
    assert "slack" in status["completed"]


def test_slack_accepts_xoxb_token(client, rosa_home):
    r = client.post("/api/step/slack", json={"token": "xoxb-bot-token"})
    assert r.status_code == 200


def test_slack_rejects_other_prefix(client):
    r = client.post("/api/step/slack", json={"token": "sk-openai-nope"})
    assert r.status_code == 400


# --- Todoist ---------------------------------------------------------------


def test_todoist_saves_token(client, rosa_home):
    tok = "a" * 40
    r = client.post("/api/step/todoist", json={"token": tok})
    assert r.status_code == 200
    body = (rosa_home / "secrets.env").read_text()
    assert tok in body

    status = client.get("/api/status").json()
    assert "todoist" in status["completed"]


def test_todoist_rejects_short_token(client):
    r = client.post("/api/step/todoist", json={"token": "short"})
    assert r.status_code == 400


# --- IMAP ------------------------------------------------------------------


def test_imap_single_account(client, rosa_home):
    r = client.post("/api/step/imap", json={
        "token": "personal imap.fastmail.com you@example.com secret-pw 993",
    })
    assert r.status_code == 200
    assert r.json()["accounts"] == 1

    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    accts = cfg["imap"]["accounts"]
    assert accts[0] == {
        "label": "personal", "host": "imap.fastmail.com",
        "user": "you@example.com", "port": 993,
        "password_env": "IMAP_PERSONAL_PASSWORD",
    }
    # Password went to secrets.env, not config.yaml
    body = (rosa_home / "secrets.env").read_text()
    assert "IMAP_PERSONAL_PASSWORD=secret-pw" in body
    assert "secret-pw" not in (rosa_home / "config.yaml").read_text()


def test_imap_multiple_accounts_and_comments(client, rosa_home):
    r = client.post("/api/step/imap", json={
        "token": (
            "# my imap accounts\n"
            "personal imap.fastmail.com me@x.com pw1\n"
            "work outlook.office365.com me@work.com pw2 993\n"
            "\n"
            "spare imap.gmail.com me@gmail.com pw3\n"
        ),
    })
    assert r.status_code == 200
    assert r.json()["accounts"] == 3


def test_imap_rejects_line_with_missing_fields(client):
    r = client.post("/api/step/imap", json={
        "token": "personal imap.fastmail.com me",  # missing password
    })
    assert r.status_code == 400


def test_imap_default_port_993(client, rosa_home):
    r = client.post("/api/step/imap", json={
        "token": "acct imap.example.com u@x.com pw",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    accts = load_config(rosa_home / "config.yaml")["imap"]["accounts"]
    assert accts[0]["port"] == 993


def test_imap_empty_rejected(client):
    r = client.post("/api/step/imap", json={"token": ""})
    assert r.status_code == 400
