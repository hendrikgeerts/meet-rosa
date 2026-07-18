"""Tests voor wizard.health_checks — live-integratie ping tijdens setup."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    from wizard import server as srv, google_oauth
    srv.reset_finish_event()
    google_oauth.clear_pending()
    from wizard.server import build_app, _SESSION_TOKEN
    c = TestClient(build_app())
    c.headers["X-Wizard-Token"] = _SESSION_TOKEN
    return c, tmp_path


# --- Individual check functions -------------------------------------------


def test_check_anthropic_rejects_no_key():
    from wizard.health_checks import check_anthropic
    result = check_anthropic("")
    assert result["ok"] is False
    assert "no anthropic api key" in result["message"].lower()


def test_check_anthropic_rejects_wrong_prefix():
    from wizard.health_checks import check_anthropic
    result = check_anthropic("sk-openai-nope")
    assert result["ok"] is False


def test_check_anthropic_401_reports_rejected():
    from wizard.health_checks import check_anthropic

    with patch("wizard.health_checks._http_json", return_value=(401, {"error": {"message": "auth"}})):
        result = check_anthropic("sk-ant-fake")
        assert result["ok"] is False
        assert "401" in result["message"] or "rejected" in result["message"].lower()


def test_check_anthropic_200_ok():
    from wizard.health_checks import check_anthropic
    with patch("wizard.health_checks._http_json", return_value=(200, {"data": []})):
        result = check_anthropic("sk-ant-good")
        assert result["ok"] is True


def test_check_ollama_unreachable_gives_hint():
    from wizard.health_checks import check_ollama
    import urllib.error
    with patch("wizard.health_checks._http_json",
               side_effect=urllib.error.URLError("connection refused")):
        result = check_ollama()
        assert result["ok"] is False
        assert "not reachable" in result["message"].lower()
        assert "ollama serve" in result["details"].lower()


def test_check_ollama_no_models_gives_pull_hint():
    from wizard.health_checks import check_ollama
    with patch("wizard.health_checks._http_json",
               return_value=(200, {"models": []})):
        result = check_ollama()
        assert result["ok"] is False
        assert "no models" in result["message"].lower()
        assert "ollama pull" in result["details"].lower()


def test_check_ollama_with_models_ok():
    from wizard.health_checks import check_ollama
    with patch("wizard.health_checks._http_json",
               return_value=(200, {"models": [{"name": "llama3.1:8b"}]})):
        result = check_ollama()
        assert result["ok"] is True
        assert "1 model" in result["message"]


def test_check_google_token_missing_file(tmp_path):
    from wizard.health_checks import check_google_token
    result = check_google_token(tmp_path / "google_token.json")
    assert result["ok"] is False
    assert "no google token" in result["message"].lower()


def test_check_google_token_valid(tmp_path):
    from wizard.health_checks import check_google_token
    tok = tmp_path / "google_token.json"
    tok.write_text(json.dumps({
        "refresh_token": "1//x", "client_id": "abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-y", "token": "ya29.a",
    }))
    result = check_google_token(tok)
    assert result["ok"] is True


def test_check_google_token_missing_fields(tmp_path):
    from wizard.health_checks import check_google_token
    tok = tmp_path / "google_token.json"
    tok.write_text(json.dumps({"token": "only-access"}))
    result = check_google_token(tok)
    assert result["ok"] is False
    assert "missing fields" in result["message"].lower()


def test_check_fda_readable(tmp_path):
    """We kunnen hier alleen best-effort testen dat de code niet crasht."""
    from wizard.health_checks import check_full_disk_access
    result = check_full_disk_access()
    # Op de test-machine kan het beide zijn — belangrijkste: geen crash.
    assert "ok" in result and "message" in result


# --- run_all + endpoint integration ---------------------------------------


def test_run_all_returns_summary():
    from wizard.health_checks import run_all
    with patch("wizard.health_checks._http_json") as mock_http:
        mock_http.return_value = (200, {"models": [{"name": "llama3.1:8b"}]})
        result = run_all(anthropic_key="sk-ant-x")
    assert "summary" in result
    assert "results" in result
    assert result["summary"]["total"] == len(result["results"])


def test_health_endpoint_returns_json(client):
    c, _ = client
    r = c.get("/api/health-check")
    assert r.status_code == 200
    body = r.json()
    assert "summary" in body
    assert "results" in body


def test_health_endpoint_needs_token(client):
    c, _ = client
    c.headers["X-Wizard-Token"] = "wrong"
    r = c.get("/api/health-check")
    assert r.status_code == 403
