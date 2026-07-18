"""Tests voor de Google OAuth-flow in de wizard.

We mocken Google's Flow-classes zodat er geen echte HTTP naar Google gaat.
Doel: happy-path init/callback + edge cases (invalid credentials, expired
state) werken.
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def rosa_home(tmp_path, monkeypatch):
    home = tmp_path / "rosa-home"
    home.mkdir()
    monkeypatch.setenv("ROSA_HOME", str(home))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from wizard import server as srv, google_oauth
    srv.reset_finish_event()
    google_oauth.clear_pending()
    yield home


@pytest.fixture
def client(rosa_home):
    from wizard.server import build_app, _SESSION_TOKEN
    app = build_app()
    c = TestClient(app)
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c


_VALID_CREDS_JSON = json.dumps({
    "web": {
        "client_id": "1234567890-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-xxxxx",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
})


def test_google_init_returns_auth_url(client, rosa_home):
    r = client.post("/api/step/google/init", json={"credentials": _VALID_CREDS_JSON})
    assert r.status_code == 200
    body = r.json()
    assert body["auth_url"].startswith("https://accounts.google.com/o/oauth2/auth")
    assert "callback" in body["redirect_uri"]


def test_google_init_rejects_empty_creds(client):
    r = client.post("/api/step/google/init", json={"credentials": ""})
    assert r.status_code == 400


def test_google_init_rejects_invalid_json(client):
    r = client.post("/api/step/google/init", json={"credentials": "not json"})
    assert r.status_code == 400


def test_google_init_rejects_wrong_client_id_suffix(client):
    r = client.post("/api/step/google/init", json={
        "credentials": json.dumps({
            "installed": {"client_id": "not-a-google-id",
                          "client_secret": "x"},
        }),
    })
    assert r.status_code == 400


def test_google_callback_with_bad_state_shows_error(client, rosa_home):
    r = client.get(
        "/oauth/google/callback?code=fakecode&state=unknown-state",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "state expired" in r.text.lower()


def test_google_callback_google_error_shows_error(client):
    r = client.get(
        "/oauth/google/callback?error=access_denied",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "access_denied" in r.text


def test_google_callback_missing_code_shows_error(client):
    r = client.get("/oauth/google/callback", follow_redirects=False)
    assert r.status_code == 400


def test_wizard_token_is_refreshable_without_credentials_file(tmp_path):
    """M13c: token dat de wizard schrijft moet standalone refresh-baar
    zijn — dus zonder aparte credentials.json ernaast. Google's
    Credentials.to_json() moet client_id + client_secret + refresh_token
    allemaal in het token-bestand meebakken."""
    from google.oauth2.credentials import Credentials
    import json

    # Simuleer wat de wizard schrijft na een geslaagde exchange
    tok_path = tmp_path / "google_token.json"
    tok_path.write_text(json.dumps({
        "token": "ya29.access-token",
        "refresh_token": "1//refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "1234-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-secret",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.events",
        ],
        "expiry": "2099-01-01T00:00:00Z",
    }))
    tok_path.chmod(0o600)

    # Google's Credentials constructor moet dit kunnen laden — en de
    # resulting creds moeten client_id + refresh_token hebben zodat
    # google_auth.get_credentials() de refresh-tak kan lopen zonder
    # dat credentials.json aparte bestaat.
    from integrations.google_auth import SCOPES
    creds = Credentials.from_authorized_user_file(str(tok_path), SCOPES)
    assert creds.refresh_token == "1//refresh"
    assert creds.client_id.endswith(".apps.googleusercontent.com")
    assert creds.client_secret == "GOCSPX-secret"


def test_google_full_flow_via_mock(client, rosa_home, monkeypatch):
    """End-to-end: init geeft state, callback met die state exchanget +
    persisteert token. We mocken alleen `flow.fetch_token` zodat er geen
    HTTP naar Google gaat."""
    r = client.post("/api/step/google/init", json={"credentials": _VALID_CREDS_JSON})
    assert r.status_code == 200
    # Extract state uit auth-URL
    from urllib.parse import parse_qs, urlparse
    auth_url = r.json()["auth_url"]
    state_tok = parse_qs(urlparse(auth_url).query)["state"][0]

    # Monkey-patch Flow.fetch_token en credentials-property zodat we
    # geen echte Google-call doen.
    from google_auth_oauthlib.flow import Flow
    from unittest.mock import MagicMock, patch

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = json.dumps({
        "token": "fake-access", "refresh_token": "fake-refresh",
        "client_id": "1234567890-abc.apps.googleusercontent.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    })

    with patch.object(Flow, "fetch_token"), \
         patch.object(Flow, "credentials", new_callable=lambda: property(
             lambda self: fake_creds
         )):
        r2 = client.get(
            f"/oauth/google/callback?code=fake-code&state={state_tok}",
            follow_redirects=False,
        )
        assert r2.status_code == 200
        assert "Google connected" in r2.text

    # Verifieer: token-bestand aangemaakt met 0600 en config.yaml
    # verwijst er naar.
    tok_path = rosa_home / "google_token.json"
    assert tok_path.exists()
    assert (os.stat(tok_path).st_mode & 0o777) == 0o600
    assert "fake-refresh" in tok_path.read_text()

    from wizard.state import load_config
    cfg = load_config(rosa_home / "config.yaml")
    assert cfg["google"]["token_path"] == str(tok_path)

    # State moet 'google' als completed hebben.
    status = client.get("/api/status").json()
    assert "google" in status["completed"]
