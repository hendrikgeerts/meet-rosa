"""Tests voor wizard.adapters — de brug tussen wizard-payloads en de
bestaande config-YAML formaten die de daemon-code leest.

Focus: het YAML dat de adapters wegschrijven moet lees-baar zijn door
de bestaande loaders (uptime.checker.load_targets, classifier's
confidential_domains loader, etc.).
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pytest.importorskip("fastapi")

import yaml
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
    from wizard.server import build_app, _SESSION_TOKEN
    c = TestClient(build_app())
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c


# --- Direct adapter tests --------------------------------------------------


def test_vip_adapter_classifies_names_vs_emails(tmp_path):
    from wizard.adapters import write_vip_contacts
    path = write_vip_contacts(tmp_path, [
        "Jane Smith",
        "jane@big.com",
        "",  # empty skipped
        "Jim",
    ])
    data = yaml.safe_load(path.read_text())
    assert data["vips"] == [
        {"name": "Jane Smith"},
        {"email": "jane@big.com"},
        {"name": "Jim"},
    ]


def test_uptime_adapter_derives_target_name_from_host(tmp_path):
    from wizard.adapters import write_uptime_targets
    path = write_uptime_targets(tmp_path, [
        "https://acme.com/health",
        "https://api.acme.com",
    ])
    data = yaml.safe_load(path.read_text())
    assert data["targets"][0]["name"] == "acme.com"
    assert data["targets"][0]["url"] == "https://acme.com/health"
    assert data["targets"][1]["name"] == "api.acme.com"


def test_uptime_yaml_readable_by_existing_load_targets(tmp_path):
    """Belangrijkste check: het YAML dat we schrijven moet door de
    bestaande extension-code gelezen kunnen worden zonder aanpassing."""
    from wizard.adapters import write_uptime_targets
    from extensions.uptime.checker import load_targets
    path = write_uptime_targets(tmp_path, [
        "https://acme.com",
        "https://api.acme.com/health",
    ])
    targets = load_targets(path)
    assert len(targets) == 2
    assert all(t.get("url", "").startswith("https://") for t in targets)


def test_confidential_adapter_groups_under_wizard(tmp_path):
    from wizard.adapters import write_confidential_domains
    path = write_confidential_domains(tmp_path, [
        "legal-firm.com", "therapist.nl", "accountant.com",
    ])
    data = yaml.safe_load(path.read_text())
    assert data["domains"]["wizard"] == [
        "legal-firm.com", "therapist.nl", "accountant.com",
    ]


def test_imap_adapter_puts_password_in_env_ref(tmp_path):
    """IMAP wachtwoord komt uit secrets.env via password_env — nooit
    hier in de YAML zelf."""
    from wizard.adapters import write_imap_accounts
    path = write_imap_accounts(tmp_path, [{
        "label": "Personal", "host": "imap.fastmail.com",
        "user": "me@x.com", "port": 993,
        "password_env": "IMAP_PERSONAL_PASSWORD",
    }])
    data = yaml.safe_load(path.read_text())
    body = path.read_text()
    assert "password" not in body.lower() or "password_env" in body
    assert data["accounts"][0]["password_env"] == "IMAP_PERSONAL_PASSWORD"
    assert data["accounts"][0]["ssl"] is True
    assert data["accounts"][0]["folders"]["inbox"] == "INBOX"


def test_news_sources_yaml_shape(tmp_path):
    from wizard.adapters import write_news_sources
    path = write_news_sources(tmp_path, [
        "https://news.ycombinator.com/rss",
        "https://blog.company.com/feed",
    ])
    data = yaml.safe_load(path.read_text())
    assert data["sources"] == [
        {"url": "https://news.ycombinator.com/rss"},
        {"url": "https://blog.company.com/feed"},
    ]


# --- End-to-end via wizard endpoints ---------------------------------------


def test_vips_endpoint_writes_yaml_next_to_config(client, rosa_home):
    r = client.post("/api/step/vips", json={
        "items": "Jane Smith\njane@x.com",
    })
    assert r.status_code == 200
    vip_yaml = rosa_home / "config" / "vip_contacts.yaml"
    assert vip_yaml.exists()
    data = yaml.safe_load(vip_yaml.read_text())
    assert len(data["vips"]) == 2


def test_uptime_endpoint_writes_yaml_for_daemon(client, rosa_home):
    r = client.post("/api/step/uptime", json={
        "items": "https://acme.com\nhttps://api.acme.com/health",
    })
    assert r.status_code == 200
    up_yaml = rosa_home / "config" / "uptime.yaml"
    assert up_yaml.exists()
    from extensions.uptime.checker import load_targets
    targets = load_targets(up_yaml)
    assert len(targets) == 2


def test_confidential_endpoint_writes_domains_yaml(client, rosa_home):
    r = client.post("/api/step/confidential", json={
        "items": "legal.com\ntherapist.nl",
    })
    assert r.status_code == 200
    cd_yaml = rosa_home / "config" / "confidential_domains.yaml"
    assert cd_yaml.exists()
    data = yaml.safe_load(cd_yaml.read_text())
    assert "legal.com" in data["domains"]["wizard"]


def test_imap_endpoint_writes_accounts_yaml(client, rosa_home):
    r = client.post("/api/step/imap", json={
        "token": "personal imap.fastmail.com me@x.com pw 993",
    })
    assert r.status_code == 200
    imap_yaml = rosa_home / "config" / "imap_accounts.yaml"
    assert imap_yaml.exists()
    data = yaml.safe_load(imap_yaml.read_text())
    assert data["accounts"][0]["username"] == "me@x.com"
    assert data["accounts"][0]["password_env"] == "IMAP_PERSONAL_PASSWORD"
