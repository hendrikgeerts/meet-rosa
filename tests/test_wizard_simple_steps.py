"""Tests voor de zeven simpelere wizard-stappen (M10).

Endpoints: /api/step/{plaud,vips,uptime,news,confidential,notifications,features}
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


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
    from wizard.server import build_app, _SESSION_TOKEN
    app = build_app()
    c = TestClient(app)
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c


# --- Plaud -----------------------------------------------------------------


def test_plaud_saves_folders(client, rosa_home):
    r = client.post("/api/step/plaud", json={
        "audio_folder": "/Users/x/Plaud",
        "backup_folder": "/Users/x/Plaud-done",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["plaud"]["audio_folder"] == "/Users/x/Plaud"
    assert cfg["plaud"]["backup_folder"] == "/Users/x/Plaud-done"


def test_plaud_requires_audio_folder(client):
    r = client.post("/api/step/plaud", json={"audio_folder": ""})
    assert r.status_code == 400


def test_plaud_backup_optional(client, rosa_home):
    r = client.post("/api/step/plaud", json={"audio_folder": "/x"})
    assert r.status_code == 200


# --- VIPs (list-step) ------------------------------------------------------


def test_vips_saves_list_ignoring_comments(client, rosa_home):
    r = client.post("/api/step/vips", json={
        "items": "# my vips\nJane\njane@x.com\n\n# footer\nJim\n",
    })
    assert r.status_code == 200
    assert r.json()["count"] == 3
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["vips"]["contacts"] == ["Jane", "jane@x.com", "Jim"]


# --- Uptime ----------------------------------------------------------------


def test_uptime_rejects_non_http(client):
    r = client.post("/api/step/uptime", json={"items": "ftp://x.com"})
    assert r.status_code == 400


def test_uptime_saves_https_urls(client, rosa_home):
    r = client.post("/api/step/uptime", json={
        "items": "https://a.com\nhttps://b.com/health",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["uptime"]["urls"] == ["https://a.com", "https://b.com/health"]


# --- News feeds ------------------------------------------------------------


def test_news_rejects_non_http(client):
    r = client.post("/api/step/news", json={"items": "not-a-url"})
    assert r.status_code == 400


def test_news_saves_feeds(client, rosa_home):
    r = client.post("/api/step/news", json={
        "items": "https://feed.com/rss\nhttps://other.com/atom",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["news"]["feeds"] == ["https://feed.com/rss", "https://other.com/atom"]


# --- Confidential domains --------------------------------------------------


def test_confidential_rejects_email_or_url(client):
    r = client.post("/api/step/confidential", json={
        "items": "legal.com\nyou@legal.com",  # email not allowed
    })
    assert r.status_code == 400


def test_confidential_rejects_url_slashes(client):
    r = client.post("/api/step/confidential", json={
        "items": "legal.com/path",
    })
    assert r.status_code == 400


def test_confidential_saves_bare_domains(client, rosa_home):
    r = client.post("/api/step/confidential", json={
        "items": "legal-firm.com\ntherapist.nl\naccountant.com",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert len(cfg["confidential"]["domains"]) == 3


# --- Notifications ---------------------------------------------------------


def test_notifications_saves_times(client, rosa_home):
    r = client.post("/api/step/notifications", json={
        "morning_time": "07:30",
        "midday_time": "13:00",
        "dayclose_time": "19:00",
        "quiet_start": "23:00",
        "quiet_end": "06:00",
    })
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["briefings"]["morning_time"] == "07:30"
    assert cfg["midday"]["time"] == "13:00"
    assert cfg["dayclose"]["time"] == "19:00"
    assert cfg["notifications"]["quiet_hours_start"] == "23:00"


def test_notifications_uses_defaults_for_missing(client, rosa_home):
    r = client.post("/api/step/notifications", json={})
    assert r.status_code == 200
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["briefings"]["morning_time"] == "07:00"


def test_notifications_rejects_bad_time_format(client):
    r = client.post("/api/step/notifications", json={
        "morning_time": "7am",
    })
    assert r.status_code == 400


# --- Features --------------------------------------------------------------


def test_features_saves_toggles(client, rosa_home):
    r = client.post("/api/step/features", json={
        "features": {
            "reminders": True,
            "comm_intel": True,
            "sales": False,
            "market_intel": True,
        },
    })
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] == 3
    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["features"]["reminders"] is True
    assert cfg["features"]["sales"] is False


def test_features_rejects_unknown(client):
    r = client.post("/api/step/features", json={
        "features": {"world_domination": True},
    })
    assert r.status_code == 400


def test_features_rejects_non_dict(client):
    r = client.post("/api/step/features", json={"features": "on"})
    assert r.status_code == 400


# --- Marked-done propagation -----------------------------------------------


def test_all_seven_steps_mark_completed(client, rosa_home):
    """Doorloop alle zeven en check /api/status."""
    client.post("/api/step/plaud", json={"audio_folder": "/x"})
    client.post("/api/step/vips", json={"items": "Jane"})
    client.post("/api/step/uptime", json={"items": ""})
    client.post("/api/step/news", json={"items": ""})
    client.post("/api/step/confidential", json={"items": ""})
    client.post("/api/step/notifications", json={})
    client.post("/api/step/features", json={"features": {}})
    status = client.get("/api/status").json()
    for step in ("plaud", "vips", "uptime", "news",
                 "confidential", "notifications", "features"):
        assert step in status["completed"], f"{step} not completed"
