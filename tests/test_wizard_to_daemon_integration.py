"""M13d — End-to-end integratie: wizard-config wordt correct opgepikt
door de daemon-init flow.

Deze test simuleert een verse install: doorloopt de wizard end-to-end,
laadt daarna Settings, initialiseert alle SQLite schemas, en verifieert
dat de bestaande extension-loaders (uptime.checker, classifier) de
wizard-YAMLs correct kunnen lezen zonder aanpassing.

Als iemand een integratie toevoegt aan de wizard maar de adapter
vergeet, faalt deze test.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")

from fastapi.testclient import TestClient


@pytest.fixture
def wizard_completed(tmp_path, monkeypatch):
    """Doorloop volledige wizard-flow en return ROSA_HOME + Settings."""
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from wizard import google_oauth
    from wizard import server as srv
    srv.reset_finish_event()
    google_oauth.clear_pending()
    from wizard.server import _SESSION_TOKEN, build_app
    c = TestClient(build_app())
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN

    steps = [
        ("welcome",       {"consent": True}),
        ("identity",      {"name": "Alex", "email": "alex@ex.com",
                           "timezone": "Europe/Berlin", "preferred_language": "en",
                           "home_city": "Berlin", "home_country": "DE",
                           "company": "Acme"}),
        ("claude",        {"anthropic_api_key": "sk-ant-e2e"}),
        ("imessage",      {"primary_handle": "+31612345678"}),
        ("imap",          {"token": "personal imap.fastmail.com me@x.com pw 993"}),
        ("slack",         {"token": "xoxp-slack"}),
        ("todoist",       {"token": "a" * 40}),
        ("plaud",         {"audio_folder": str(tmp_path / "plaud")}),
        ("vips",          {"items": "Jane\njane@x.com"}),
        ("uptime",        {"items": "https://acme.com\nhttps://api.acme.com/health"}),
        ("news",          {"items": "https://feed.com/rss"}),
        ("confidential",  {"items": "legal.com\ntherapist.nl"}),
        ("notifications", {"morning_time": "07:00", "midday_time": "14:00",
                           "dayclose_time": "20:00",
                           "quiet_start": "22:00", "quiet_end": "07:00"}),
        ("features",      {"features": {"reminders": True, "comm_intel": True}}),
    ]
    for name, body in steps:
        r = c.post(f"/api/step/{name}", json=body)
        assert r.status_code == 200, f"{name}: {r.text}"
    r = c.post("/api/step/confirm", json={})
    assert r.status_code == 200
    assert r.json()["finished"] is True

    from core.config import load_settings
    settings = load_settings()
    return tmp_path, settings


def test_daemon_paths_land_in_rosa_home(wizard_completed):
    """M13a — data_dir resolvt onder ROSA_HOME, niet REPO_ROOT."""
    home, settings = wizard_completed
    assert str(settings.data_dir).startswith(str(home)), (
        f"data_dir {settings.data_dir} should be under ROSA_HOME {home}"
    )
    assert str(settings.db_path).startswith(str(home))
    assert str(settings.audit_dir).startswith(str(home))


def test_all_sqlite_schemas_can_init(wizard_completed):
    """Alle schema-inits die main.run() doet moeten werken met
    wizard-config zonder crash."""
    _, settings = wizard_completed

    from core import db
    from core.app_state import init_app_state_schema
    from extensions import reminders
    from extensions.comm_intel.schema import init_comm_schema
    from extensions.market_intel.schema import init_market_intel_schema
    from extensions.open_loops.schema import init_open_loops_schema
    from extensions.plaud_intel.schema import init_plaud_meetings_schema
    from extensions.travel_alerts.schema import init_travel_alerts_schema
    from integrations import plaud

    db.init_db(settings.db_path)
    reminders.init_reminders_schema(settings.db_path)
    plaud.init_plaud_schema(settings.db_path)
    init_comm_schema(settings.db_path)
    init_open_loops_schema(settings.db_path)
    init_plaud_meetings_schema(settings.db_path)
    init_market_intel_schema(settings.db_path)
    init_travel_alerts_schema(settings.db_path)
    init_app_state_schema(settings.db_path)

    # SQLite file bestaat en heeft schema
    assert settings.db_path.exists()
    assert settings.db_path.stat().st_size > 0


def test_classifier_picks_up_wizard_confidential_domains(wizard_completed):
    """M13b — de classifier moet de domains uit de wizard's
    confidential-YAML herkennen als confidential."""
    home, settings = wizard_completed
    import yaml

    from privacy.classifier import Classifier

    conf_yaml = settings.data_dir.parent / "config" / "confidential_domains.yaml"
    assert conf_yaml.exists(), "wizard did not write confidential_domains.yaml"
    conf_data = yaml.safe_load(conf_yaml.read_text())
    all_domains = [
        d for group in (conf_data.get("domains") or {}).values() for d in group
    ]
    assert "legal.com" in all_domains

    classifier = Classifier(
        confidential_domains=tuple(all_domains),
        confidential_keywords=("vertrouwelijk",),
    )
    result = classifier.classify(sender="user@legal.com", text="hi")
    assert str(result.label) == "confidential"


def test_uptime_loader_reads_wizard_targets(wizard_completed):
    """M13b — bestaande uptime.checker.load_targets moet de
    wizard-YAML kunnen parsen zonder aanpassing."""
    home, settings = wizard_completed
    from extensions.uptime.checker import load_targets

    up_yaml = settings.data_dir.parent / "config" / "uptime.yaml"
    targets = load_targets(up_yaml)
    assert len(targets) == 2
    urls = {t["url"] for t in targets}
    assert "https://acme.com" in urls
    assert "https://api.acme.com/health" in urls


def test_imap_yaml_shape_daemon_compatible(wizard_completed):
    """M13b — imap_accounts.yaml moet dezelfde shape hebben als de
    voorbeeld-file in de repo."""
    home, settings = wizard_completed
    import yaml
    imap_yaml = settings.data_dir.parent / "config" / "imap_accounts.yaml"
    data = yaml.safe_load(imap_yaml.read_text())
    accts = data["accounts"]
    assert accts[0]["host"] == "imap.fastmail.com"
    # Password mag NIET in de YAML zitten; alleen env-ref.
    assert "password" not in accts[0] or accts[0].get("password") is None
    assert accts[0]["password_env"] == "IMAP_PERSONAL_PASSWORD"
    assert accts[0]["ssl"] is True


def test_google_token_would_refresh_without_credentials_file(wizard_completed):
    """M13c — Als de wizard een Google token had geschreven, moet
    Credentials.from_authorized_user_file dat token kunnen laden en
    heeft dat token alle info nodig voor refresh (client_id + secret +
    refresh_token) zonder aparte credentials.json."""
    home, _ = wizard_completed
    import json

    from google.oauth2.credentials import Credentials

    from integrations.google_auth import SCOPES

    # Simuleer een token zoals de wizard 'em na exchange schrijft
    tok = home / "google_token.json"
    tok.write_text(json.dumps({
        "token": "access", "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "1234-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-x",
        "scopes": SCOPES,
    }))
    creds = Credentials.from_authorized_user_file(str(tok), SCOPES)
    assert creds.refresh_token
    assert creds.client_id
    assert creds.client_secret
