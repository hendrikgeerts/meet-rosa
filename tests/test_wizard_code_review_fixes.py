"""Tests voor de fixes uit de code-review (H1-H5, M1-M6, L2).

Elke test noemt de finding-code die 'm afdekt zodat we bij een
toekomstige refactor herkennen wat er beschermd wordt.
"""
from __future__ import annotations

import time
from pathlib import Path

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


# ------------------------------------------------------ H3: PENDING TTL ----


def test_h3_pending_dict_has_ttl_and_cap():
    """H3 — _PENDING dict moet TTL en cap hebben zodat client_secrets
    niet oneindig in memory blijven bij dubbele klikken."""
    from wizard import google_oauth

    google_oauth.clear_pending()

    # Fake 10 pending states
    for i in range(10):
        google_oauth._PENDING[f"state-{i}"] = google_oauth.PendingOAuthState(
            state_token=f"state-{i}",
            credentials_json={"web": {"client_id": "x"}},
            redirect_uri="http://x",
            created_at=time.time(),
        )
    assert len(google_oauth._PENDING) == 10
    google_oauth._prune_pending()
    # Cap = 5
    assert len(google_oauth._PENDING) <= google_oauth._PENDING_MAX


def test_h3_pending_expires_after_ttl():
    from wizard import google_oauth

    google_oauth.clear_pending()
    old_time = time.time() - google_oauth._PENDING_TTL_SECONDS - 60
    google_oauth._PENDING["ancient"] = google_oauth.PendingOAuthState(
        state_token="ancient",
        credentials_json={},
        redirect_uri="",
        created_at=old_time,
    )
    google_oauth._prune_pending()
    assert "ancient" not in google_oauth._PENDING


# --------------------------------------------- H2: OAuth session binding ---


def test_h2_oauth_finish_rejects_mismatched_session_token(tmp_path):
    """H2 — een callback die met een andere wizard-session-token komt
    moet geweigerd worden."""

    from wizard import google_oauth

    google_oauth.clear_pending()
    creds = (
        '{"web":{"client_id":"1234-abc.apps.googleusercontent.com",'
        '"client_secret":"GOCSPX-x"}}'
    )
    _, state = google_oauth.start_flow(
        creds, "http://x/cb", session_token="session-A",
    )
    # Simuleer callback met andere session-token → LookupError
    with pytest.raises(LookupError, match="different wizard session"):
        google_oauth.finish_flow(
            state_token=state, code="fake-code",
            token_path=tmp_path / "token.json",
            session_token="session-B",
        )


# ------------------------------------------------ H1: iMessage mark_done ---


def test_h1_imessage_marks_done(client):
    """H1 — regressie voor bug uit E2E test: iMessage endpoint moet
    de stap markeren als completed."""
    c, _ = client
    r = c.post("/api/step/imessage", json={"primary_handle": "+31612345678"})
    assert r.status_code == 200
    status = c.get("/api/status").json()
    assert "imessage" in status["completed"]


# -------------------------------------- M6: skip weigert required steps ---


def test_m6_skip_rejects_required_steps(client):
    """M6 — skip endpoint moet REQUIRED_STEPS weigeren; anders wordt
    de wizard confusingly stuck op confirm."""
    c, _ = client
    for req_step in ["welcome", "identity", "claude", "confirm"]:
        r = c.post("/api/step/skip", json={"step": req_step})
        assert r.status_code == 400, f"{req_step} should not be skippable"
        assert "cannot be skipped" in r.json()["detail"].lower()


def test_m6_skip_allows_optional_steps(client):
    c, _ = client
    for opt_step in ["google", "slack", "todoist", "plaud", "vips",
                     "uptime", "news", "notifications", "confidential",
                     "features", "imessage", "imap"]:
        r = c.post("/api/step/skip", json={"step": opt_step})
        assert r.status_code == 200, f"{opt_step} should be skippable"


# --------------------------------------- M3: IMAP shlex parser met quotes ---


def test_m3_imap_password_with_spaces(client, tmp_path):
    """M3 — quoted password met spaties moet werken via shlex parsing."""
    c, home = client
    r = c.post("/api/step/imap", json={
        "token": 'personal imap.fastmail.com me@x.com "hello world" 993',
    })
    assert r.status_code == 200
    sec = (home / "secrets.env").read_text()
    assert 'hello world' in sec  # password preserved through quotes


def test_m3_imap_bad_port_returns_400_not_500(client):
    """M3 — non-integer port moet 400 geven, niet crashen."""
    c, _ = client
    r = c.post("/api/step/imap", json={
        "token": "personal imap.example.com me pw notanumber",
    })
    assert r.status_code == 400


# ------------------------ M2: update_config deep-merge (nested dicts) ---


def test_m2_update_config_deep_merges_nested_dicts(tmp_path):
    """M2 — deep-merge, niet 1-level. Voorkomt dat wizard-re-run stille
    dataloss doet op nested keys."""
    from wizard.state import load_config, save_config, update_config

    p = tmp_path / "config.yaml"
    save_config(p, {
        "briefings": {
            "morning_time": "07:00",
            "enabled": True,
            "extras": {"weather": True, "todoist": True},
        },
    })
    update_config(p, {
        "briefings": {
            "morning_time": "08:00",
            "extras": {"weather": False},   # todoist moet blijven
        },
    })
    cfg = load_config(p)
    assert cfg["briefings"]["morning_time"] == "08:00"
    assert cfg["briefings"]["enabled"] is True  # niet overschreven
    assert cfg["briefings"]["extras"]["weather"] is False
    assert cfg["briefings"]["extras"]["todoist"] is True  # behouden


# ------------------------------- H4/H5: implicit dev-mode via settings.yaml -


def test_h5_repo_settings_yaml_triggers_dev_mode(tmp_path, monkeypatch):
    """H4/H5 — als de repo een config/settings.yaml heeft (Hendrik's
    setup), gebruik dan die als ROSA_HOME zelfs zonder ROSA_DEV=1.
    Beschermt tegen accidenteel wegvallen van de env-var."""
    fake_root = tmp_path / "repo"
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "config" / "settings.yaml").write_text("runtime: {}\n")

    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_HOME", raising=False)

    from unittest.mock import patch

    from core import config as cfg_mod
    with patch.object(cfg_mod, "ROOT", fake_root):
        home = cfg_mod.get_rosa_home()
        assert home == fake_root, (
            "get_rosa_home() should fall back to ROOT when "
            "config/settings.yaml exists (implicit dev-mode guard)"
        )


def test_h5_no_settings_yaml_uses_default_home(tmp_path, monkeypatch):
    """Nieuwe user zonder repo-settings gebruikt Library/App Support."""
    fake_root = tmp_path / "empty-repo"
    fake_root.mkdir()
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_HOME", raising=False)

    from unittest.mock import patch

    from core import config as cfg_mod
    with patch.object(cfg_mod, "ROOT", fake_root):
        home = cfg_mod.get_rosa_home()
        assert home == cfg_mod._DEFAULT_ROSA_HOME


# ----------------------------------------- L2: features list gelijkheid ---


def test_m7_redirect_uri_ignores_host_header(client):
    """M7 — Host-header mag redirect_uri niet spoofen. Bouw uit ASGI
    scope['server'] i.p.v. request.base_url."""
    c, _ = client
    r = c.post(
        "/api/step/google/init",
        json={"credentials": (
            '{"web":{"client_id":"1234-abc.apps.googleusercontent.com",'
            '"client_secret":"GOCSPX-x"}}'
        )},
        headers={"Host": "evil.example.com"},
    )
    assert r.status_code == 200
    body = r.json()
    # Ondanks spoofed Host-header moet de callback naar loopback verwijzen.
    assert "evil.example.com" not in body["redirect_uri"]
    assert "127.0.0.1" in body["redirect_uri"] or "testserver" in body["redirect_uri"]


def test_l4_hhmm_rejects_out_of_range(client):
    """L4 — 26:00 en 12:75 mogen niet doorglippen."""
    c, _ = client
    for bad in ["26:00", "24:00", "12:75", "99:99"]:
        r = c.post("/api/step/notifications", json={"morning_time": bad})
        assert r.status_code == 400, f"{bad} should be rejected"
        assert "out of range" in r.json()["detail"].lower()


def test_l4_hhmm_accepts_edge_values(client):
    c, _ = client
    r = c.post("/api/step/notifications", json={
        "morning_time": "00:00", "midday_time": "23:59",
    })
    assert r.status_code == 200


def test_l2_features_ui_covers_all_server_whitelist():
    """L2 — de _FEATURES lijst in wizard.js moet 1-op-1 matchen met
    _ALLOWED_FEATURES in server.py; anders kan de user features niet
    aanzetten via de UI die het backend wel accepteert."""

    js_path = Path(__file__).resolve().parent.parent / "src" / "wizard" / "static" / "wizard.js"
    js = js_path.read_text()

    # Extract ids uit de _FEATURES array via een simpele regex.
    import re
    js_ids = set(re.findall(r"id:\s*'([a-z_]+)'\s*,\s*label", js))

    # Get server whitelist door de server-module te importeren
    from wizard.server import build_app  # noqa
    # _ALLOWED_FEATURES is scoped in build_app; laten we hem uit de
    # source lezen zoals we voor JS deden.
    server_path = Path(__file__).resolve().parent.parent / "src" / "wizard" / "server.py"
    server_src = server_path.read_text()
    m = re.search(r"_ALLOWED_FEATURES\s*=\s*frozenset\({([^}]+)}\)",
                  server_src, re.DOTALL)
    assert m, "cannot find _ALLOWED_FEATURES"
    server_ids = set(re.findall(r'"([a-z_]+)"', m.group(1)))

    assert js_ids == server_ids, (
        f"drift between UI features and server whitelist.\n"
        f"only in JS: {js_ids - server_ids}\n"
        f"only in server: {server_ids - js_ids}"
    )
