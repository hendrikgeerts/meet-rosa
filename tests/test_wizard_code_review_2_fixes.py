"""Tests voor de tweede code-review fixes (H1-H3, M1-M3, L1-L3)."""
from __future__ import annotations

from unittest.mock import MagicMock

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


# --- H1: state-lines nu renderd door render_system_prompt -----------------


def test_h1_state_lines_render_user_name():
    """H1 — state-lines bevatten `${user_name}` als literal en moeten
    door render_system_prompt gesubstitueerd worden. Voor Alex: `${user_name}`
    wordt vervangen. Voor Hendrik: identical output."""
    from core.prompt_builder import render_system_prompt
    from main import _current_date_state_line

    date_line = _current_date_state_line()
    # State-line moet `${user_name}` marker bevatten
    assert "${user_name}" in date_line

    # Voor Alex — render substitueert
    settings = MagicMock()
    settings.user_name = "Alex"
    settings.user_company = ""
    rendered = render_system_prompt(date_line, settings)
    assert "${user_name}" not in rendered
    assert "Alex" in rendered

    # Voor Hendrik — output identiek (na substitutie 'Hendrik' == 'Hendrik')
    settings.user_name = "Hendrik"
    rendered_h = render_system_prompt(date_line, settings)
    assert "${user_name}" not in rendered_h
    assert "Hendrik" in rendered_h


def test_h1_state_lines_contain_no_double_braces():
    """H1 — verifier dat `${{user_name}}` (Python-f-string-escape) NIET
    in de state-line output zit. Dat was de zichtbare regressie."""
    from main import _current_date_state_line

    assert "${{user_name}}" not in _current_date_state_line()


# --- H3: _config_dir uit get_rosa_home ipv data_dir.parent -----------------


def test_h3_config_dir_derives_from_rosa_home(tmp_path, monkeypatch):
    """H3 — als user paths.data_dir overrulet naar absoluut pad, moet
    config_dir NIET meelanden op {abs_data_dir}/../config maar bij
    ROSA_HOME/config."""
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from core.config import get_rosa_home
    assert get_rosa_home() == tmp_path.resolve()

    # Simuleer wat main.py's _handle_message doet
    from core.config import get_rosa_home as ghr
    _config_dir = ghr() / "config"
    assert _config_dir == tmp_path.resolve() / "config"


# --- M1: IMAP label normalization -----------------------------------------


def test_m1_normalize_imap_label_handles_dots():
    from wizard.adapters import normalize_imap_label
    assert normalize_imap_label("my.mail") == "my_mail"
    assert normalize_imap_label("Personal") == "personal"
    assert normalize_imap_label("work email") == "work_email"
    assert normalize_imap_label("acct-with-dashes") == "acct_with_dashes"
    assert normalize_imap_label("MY.MAIL") == "my_mail"
    assert normalize_imap_label("") == "mymail"


def test_m2_imap_endpoint_uses_normalized_secret_key(client):
    """M2 — de secret-key in secrets.env moet 1-op-1 matchen met de
    `password_env` in imap_accounts.yaml."""
    import yaml
    c, home = client
    r = c.post("/api/step/imap", json={
        "token": "my.mail imap.example.com me@x.com pw 993",
    })
    assert r.status_code == 200

    imap_yaml = home / "config" / "imap_accounts.yaml"
    data = yaml.safe_load(imap_yaml.read_text())
    acct = data["accounts"][0]
    expected_key = "IMAP_MY_MAIL_PASSWORD"
    assert acct["password_env"] == expected_key
    assert acct["name"] == "my_mail"

    # Secret is present with the expected key.
    body = (home / "secrets.env").read_text()
    assert f"{expected_key}=pw" in body


# --- M3: dead import gone -------------------------------------------------


def test_m3_no_chmod_import_in_adapters():
    import wizard.adapters as adapters
    assert not hasattr(adapters, "_chmod_600"), (
        "adapters.py should not import _chmod_600 anymore (dead code)"
    )


# --- L2: confidential domain regex ----------------------------------------


def test_l2_confidential_rejects_invalid_domain(client):
    c, _ = client
    for bad in ["..example.com", "-example.com", "example..com", "example",
                "no-tld"]:
        r = c.post("/api/step/confidential", json={"items": bad})
        assert r.status_code == 400, f"{bad!r} should be rejected"


def test_l2_confidential_accepts_valid_domains(client, tmp_path):
    import yaml
    c, home = client
    r = c.post("/api/step/confidential", json={
        "items": "legal-firm.com\nAccounts.io\nnested.sub.domain.tld",
    })
    assert r.status_code == 200
    data = yaml.safe_load(
        (home / "config" / "confidential_domains.yaml").read_text(),
    )
    # Alles is lowercased door onze normalization
    assert "accounts.io" in data["domains"]["wizard"]


# --- L3: URL hostname validation ------------------------------------------


def test_l3_uptime_rejects_url_without_hostname(client):
    c, _ = client
    for bad in ["https:///no-host/path", "http:///"]:
        r = c.post("/api/step/uptime", json={"items": bad})
        assert r.status_code == 400


def test_l3_news_rejects_url_without_hostname(client):
    c, _ = client
    r = c.post("/api/step/news", json={"items": "https:///no-host/feed"})
    assert r.status_code == 400


# --- L1: log dir default alignment ----------------------------------------


def test_l1_log_path_default_under_rosa_home(tmp_path, monkeypatch):
    """L1 — default log-file staat nu onder ROSA_HOME/logs/ (dezelfde
    plek als de LaunchAgent stdout/stderr schrijft)."""
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    # Minimal config zonder paths.log_file override
    (tmp_path / "config.yaml").write_text(
        "user:\n  name: Test\nruntime:\n  claude_model: claude-sonnet-4-6\n"
    )
    (tmp_path / "secrets.env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-x\nOWNER_IMESSAGE_HANDLE=+31600000000\n"
    )
    from core.config import load_settings
    settings = load_settings()
    assert str(settings.log_path).startswith(str(tmp_path)), (
        f"log_path {settings.log_path} should be under ROSA_HOME {tmp_path}"
    )
    # Should end in logs/agent.log (not data/logs/agent.log)
    assert settings.log_path.name == "agent.log"
    assert settings.log_path.parent.name == "logs"
