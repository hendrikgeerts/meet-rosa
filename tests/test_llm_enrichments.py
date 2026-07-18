"""Tests voor de Llama-enrichment laag (pattern narrative, decision-tag,
person-summary, /api/suggest endpoints).

Mock OllamaClient zodat geen echte daemon nodig is."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.llm_helpers import llm_json_array, llm_json_object, llm_short_text


def _fake_ollama_returning(text: str) -> Any:
    fake = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    fake.chat.return_value = response
    return fake


# --- llm_helpers ---------------------------------------------------------

def test_llm_short_text_strips_and_caps() -> None:
    fake = _fake_ollama_returning("  Hello world.  ")
    assert llm_short_text(fake, system="x", user="y") == "Hello world."


def test_llm_short_text_returns_none_when_no_ollama() -> None:
    assert llm_short_text(None, system="x", user="y") is None


def test_llm_short_text_returns_none_on_exception() -> None:
    fake = MagicMock()
    fake.chat.side_effect = RuntimeError("ollama down")
    assert llm_short_text(fake, system="x", user="y") is None


def test_llm_json_array_parses() -> None:
    fake = _fake_ollama_returning('Here it is: ["a", "b", "c"]')
    assert llm_json_array(fake, system="x", user="y") == ["a", "b", "c"]


def test_llm_json_array_handles_markdown_fence() -> None:
    fake = _fake_ollama_returning('```json\n["a","b"]\n```')
    assert llm_json_array(fake, system="x", user="y") == ["a", "b"]


def test_llm_json_array_returns_none_on_invalid() -> None:
    fake = _fake_ollama_returning("just words no json")
    assert llm_json_array(fake, system="x", user="y") is None


def test_llm_json_object_parses() -> None:
    fake = _fake_ollama_returning('OK: {"category": "vendor", "project_slugs": ["x"]}')
    result = llm_json_object(fake, system="x", user="y")
    assert result == {"category": "vendor", "project_slugs": ["x"]}


# --- pattern narrative ---------------------------------------------------

def test_pattern_narrative_appended_when_ollama_present(tmp_path: Path) -> None:
    from extensions.comm_intel.schema import init_comm_schema
    from extensions.decisions.schema import init_decisions_schema
    from extensions.open_loops.schema import init_open_loops_schema
    from extensions.patterns.detector import run_weekly_detection
    from extensions.patterns.schema import (
        init_patterns_schema,
        list_patterns,
    )
    from extensions.plaud_intel.schema import init_plaud_meetings_schema
    from integrations.plaud import init_plaud_schema

    db = tmp_path / "p.db"
    init_patterns_schema(db)
    init_comm_schema(db)
    init_decisions_schema(db)
    init_open_loops_schema(db)
    init_plaud_meetings_schema(db)
    init_plaud_schema(db)

    today = date(2026, 4, 27)
    # Trigger stale_outgoing_rising
    cutoff_ts = 1000000000
    with sqlite3.connect(db) as c:
        for i in range(5):
            c.execute(
                "INSERT INTO open_loops (source, kind, who, title, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("comm", "outgoing_request", "k", f"r{i}", "open", cutoff_ts),
            )

    fake_ollama = _fake_ollama_returning("Probably waiting on key clients to respond.")
    detected = run_weekly_detection(db, today=today, ollama=fake_ollama)
    assert any(p["kind"] == "stale_outgoing_rising" for p in detected)

    with sqlite3.connect(db) as c:
        rows = list_patterns(c)
    assert any("Insight: Probably waiting" in (r["body"] or "") for r in rows)


# --- decision auto-tag ---------------------------------------------------

def test_decision_auto_tag_with_ollama(tmp_path: Path) -> None:
    from extensions.decisions.schema import init_decisions_schema
    from extensions.decisions.tools import log_decision_handler
    from extensions.projects.schema import init_projects_schema, insert_project

    db = tmp_path / "d.db"
    init_decisions_schema(db)
    init_projects_schema(db)
    with sqlite3.connect(db) as c:
        insert_project(c, slug="rosa", title="Rosa PA")

    fake = _fake_ollama_returning(
        '{"category": "vendor", "project_slugs": ["rosa"]}'
    )
    out = log_decision_handler(db, {
        "title": "Switch to Anthropic",
        "body": "Beter Nederlands dan OpenAI",
    }, ollama=fake)
    assert out["ok"]
    assert out["tags"] == {"category": "vendor", "project_slugs": ["rosa"]}

    # Ook in DB opgeslagen
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT tags FROM decisions WHERE id=?",
                          (out["decision_id"],)).fetchone()
    assert json.loads(row["tags"])["category"] == "vendor"


def test_decision_works_without_ollama(tmp_path: Path) -> None:
    """Backward-compat: log_decision moet werken als ollama None is."""
    from extensions.decisions.schema import init_decisions_schema
    from extensions.decisions.tools import log_decision_handler

    db = tmp_path / "d.db"
    init_decisions_schema(db)
    out = log_decision_handler(db, {"title": "X", "body": "y"}, ollama=None)
    assert out["ok"]
    assert out["tags"] is None


# --- /api/suggest endpoints ---------------------------------------------

@pytest.fixture
def suggest_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    fake = _fake_ollama_returning('["rosa", "pa-agent", "assistant"]')
    return TestClient(create_app(audit, ollama=fake), base_url="http://127.0.0.1:8080"), fake


def test_suggest_keywords_endpoint(suggest_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = suggest_client
    r = client.post("/api/suggest/project-keywords",
                     json={"title": "PA-agent v1",
                           "description": "Eerste release"})
    assert r.status_code == 200
    data = r.json()
    assert data["keywords"] == ["rosa", "pa-agent", "assistant"]


def test_suggest_keywords_no_ollama(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    client = TestClient(create_app(audit, ollama=None), base_url="http://127.0.0.1:8080")
    r = client.post("/api/suggest/project-keywords",
                     json={"title": "x"})
    assert r.status_code == 503


def test_suggest_keywords_requires_title(suggest_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = suggest_client
    r = client.post("/api/suggest/project-keywords",
                     json={"description": "no title"})
    assert r.status_code == 400


def test_suggest_vendor_email_hint(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    fake = _fake_ollama_returning("from:billing@datadoghq.com")
    client = TestClient(create_app(audit, ollama=fake), base_url="http://127.0.0.1:8080")

    r = client.post("/api/suggest/vendor-email-hint",
                     json={"name": "Datadog"})
    assert r.status_code == 200
    assert r.json()["hint"] == "from:billing@datadoghq.com"


def test_suggest_vendor_email_unknown(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    fake = _fake_ollama_returning("unknown")
    client = TestClient(create_app(audit, ollama=fake), base_url="http://127.0.0.1:8080")

    r = client.post("/api/suggest/vendor-email-hint",
                     json={"name": "ObscureCo"})
    assert r.json()["hint"] is None
