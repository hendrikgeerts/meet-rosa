"""End-to-end verse-install test + Hendrik-daemon guards.

Twee dingen die we willen garanderen:

  1. Verse install: uvicorn draait echt op een lokale port; POSTen naar
     de wizard-endpoints legt config.yaml + secrets.env op de juiste
     plek neer; `is_configured()` gaat van False naar True; de wizard
     signaleert 'finished'.

  2. Hendrik-daemon protection: met ROSA_DEV=1 (Hendrik's setup) resolvt
     ROSA_HOME naar de repo-root (niet ~/Library/Application Support/),
     de wizard triggert NIET, en existing settings.yaml wordt gevonden.

Als test 2 breekt, is Hendrik's live daemon in gevaar bij deployment
van deze branch. Zeer belangrijke regressie-guard.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url: str, body: dict, token: str) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Wizard-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _get(url: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"X-Wizard-Token": token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode(), json.loads(r.read().decode() or "{}")


def test_fresh_install_full_flow_via_http(tmp_path, monkeypatch):
    """Start uvicorn met een lege ROSA_HOME, doorloop verplichte stappen
    via HTTP, verifieer files landen + is_configured() flipt."""
    home = tmp_path / "rosa-home"
    home.mkdir()
    monkeypatch.setenv("ROSA_HOME", str(home))
    monkeypatch.delenv("ROSA_DEV", raising=False)

    from core.config import is_configured
    from wizard.server import build_app, _SESSION_TOKEN, reset_finish_event

    reset_finish_event()
    assert is_configured() is False

    import uvicorn
    port = _free_port()
    config = uvicorn.Config(
        build_app(), host="127.0.0.1", port=port, log_level="warning",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # Wait for the server socket to accept.
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            code, _ = _get(base + "/api/status", _SESSION_TOKEN)
            if code == 200:
                break
        except OSError:
            pass
        time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.fail("uvicorn never came up")

    try:
        # welcome
        code, _ = _post(base + "/api/step/welcome",
                        {"consent": True}, _SESSION_TOKEN)
        assert code == 200

        # identity
        code, _ = _post(base + "/api/step/identity", {
            "name": "Alex Bakker",
            "email": "alex@example.com",
            "timezone": "Europe/Berlin",
            "preferred_language": "en",
            "home_city": "Berlin",
            "home_country": "DE",
        }, _SESSION_TOKEN)
        assert code == 200

        # claude
        code, _ = _post(base + "/api/step/claude", {
            "anthropic_api_key": "sk-ant-e2e-abc123",
            "claude_model": "claude-sonnet-4-6",
            "local_model_main": "llama3.1:8b-instruct-q4_K_M",
        }, _SESSION_TOKEN)
        assert code == 200

        # confirm
        code, body = _post(base + "/api/step/confirm", {}, _SESSION_TOKEN)
        assert code == 200 and body["finished"] is True

        # Post-condities: files + is_configured
        assert (home / "config.yaml").exists()
        assert (home / "secrets.env").exists()
        assert is_configured() is True

        cfg_body = (home / "config.yaml").read_text()
        assert "Alex Bakker" in cfg_body
        assert "Europe/Berlin" in cfg_body

        sec_body = (home / "secrets.env").read_text()
        assert "sk-ant-e2e-abc123" in sec_body
        mode = os.stat(home / "secrets.env").st_mode & 0o777
        assert mode == 0o600
    finally:
        server.should_exit = True
        t.join(timeout=2.0)


def test_rosa_dev_mode_resolves_to_repo_root(monkeypatch):
    """Hendrik's guard rail. ROSA_DEV=1 → ROSA_HOME==ROOT."""
    monkeypatch.setenv("ROSA_DEV", "1")
    monkeypatch.delenv("ROSA_HOME", raising=False)

    from core.config import ROOT, get_rosa_home
    assert get_rosa_home() == ROOT


def test_rosa_dev_mode_finds_existing_settings(monkeypatch, tmp_path):
    """Bij ROSA_DEV=1 moet is_configured() True zijn als een repo
    config/settings.yaml heeft — precies Hendrik's huidige setup."""
    fake_root = tmp_path / "repo"
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "config" / "settings.yaml").write_text("runtime: {}\n")

    monkeypatch.setenv("ROSA_DEV", "1")
    monkeypatch.delenv("ROSA_HOME", raising=False)

    from core import config as cfg_mod
    with patch.object(cfg_mod, "ROOT", fake_root):
        assert cfg_mod.is_configured() is True


def test_rosa_dev_mode_bootstrap_never_starts_wizard(monkeypatch, tmp_path):
    """ensure_configured() moet stil no-op zijn onder ROSA_DEV=1, ook
    als er GEEN settings.yaml zou zijn. De guard komt vóór de check."""
    monkeypatch.setenv("ROSA_DEV", "1")
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))  # extra safeguard

    from wizard import bootstrap
    with patch.object(bootstrap, "_run_uvicorn") as run_uvicorn:
        bootstrap.ensure_configured()
    run_uvicorn.assert_not_called()


def test_hendrik_named_user_produces_identical_prompt():
    """Belt-and-braces regressie test. render_system_prompt met
    settings.user_name='Hendrik' output identiek aan input."""
    from unittest.mock import MagicMock

    from core.prompt_builder import render_system_prompt
    settings = MagicMock()
    settings.user_name = "Hendrik"
    settings.user_company = "DST Templates / HGE Ventures"
    # Snapshot van een realistische SYSTEM_PROMPT-fragment
    template = (
        "You are Rosa, Hendrik's proactive assistant. Reply to Hendrik "
        "in English. Use account='hendrikdpm' for that mailbox. "
        "Hendrik's home is in Gilze."
    )
    assert render_system_prompt(template, settings) == template


def test_get_rosa_home_priority_env_over_dev(monkeypatch, tmp_path):
    """Als ROSA_HOME expliciet gezet is wint die van ROSA_DEV=1."""
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.setenv("ROSA_DEV", "1")

    from core.config import get_rosa_home
    assert get_rosa_home() == tmp_path.resolve()
