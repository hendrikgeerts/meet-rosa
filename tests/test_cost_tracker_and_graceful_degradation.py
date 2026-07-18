"""Tests voor cost_tracker + graceful Ollama-fail degradation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# --- Cost tracker ---------------------------------------------------------


def test_cost_tracker_records_call(tmp_path):
    db_path = tmp_path / "memory.db"
    from core.cost_tracker import current_month_cost, init_cost_schema, record_call
    init_cost_schema(db_path)

    usd = record_call(
        db_path, task="briefing", model="claude-sonnet-4-6",
        tokens_in=1000, tokens_out=500,
    )
    assert usd > 0

    m = current_month_cost(db_path)
    assert m.calls == 1
    assert m.tokens_in == 1000
    assert m.tokens_out == 500
    assert m.usd == usd


def test_cost_tracker_usd_scales_with_tokens(tmp_path):
    from core.cost_tracker import usd_for
    small = usd_for("claude-sonnet-4-6", 100, 100)
    big = usd_for("claude-sonnet-4-6", 100_000, 100_000)
    assert big > 100 * small


def test_cost_tracker_uses_env_price_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PRICE_CLAUDE_TEST_INPUT", "1.0")
    monkeypatch.setenv("ANTHROPIC_PRICE_CLAUDE_TEST_OUTPUT", "5.0")
    from core.cost_tracker import usd_for
    # 1M input tokens @ $1/1M = $1.00
    result = usd_for("claude-test", 1_000_000, 0)
    assert abs(result - 1.0) < 0.001


def test_budget_exceeded_raises(tmp_path):
    from core.cost_tracker import (
        BudgetExceeded,
        check_budget,
        init_cost_schema,
        record_call,
    )
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)

    record_call(
        db_path, task="test", model="claude-sonnet-4-6",
        tokens_in=10_000_000, tokens_out=1_000_000,
    )
    # Should be ~$30 * 3 + $15 = ~$45. Set budget lower.
    with pytest.raises(BudgetExceeded, match="budget"):
        check_budget(db_path, monthly_budget_usd=10.0, task="test")


def test_budget_zero_is_disabled(tmp_path):
    from core.cost_tracker import (
        check_budget,
        init_cost_schema,
        record_call,
    )
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)
    record_call(
        db_path, task="t", model="claude-sonnet-4-6",
        tokens_in=100_000_000, tokens_out=100_000_000,
    )
    # Zero = disabled; should not raise
    check_budget(db_path, monthly_budget_usd=0.0)


def test_daily_series_groups_by_date(tmp_path):
    from core.cost_tracker import daily_series, init_cost_schema, record_call
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)
    record_call(
        db_path, task="a", model="claude-sonnet-4-6",
        tokens_in=100, tokens_out=100,
    )
    record_call(
        db_path, task="b", model="claude-sonnet-4-6",
        tokens_in=200, tokens_out=200,
    )
    rows = daily_series(db_path, days=1)
    assert len(rows) == 1
    assert rows[0]["calls"] == 2


# --- Gateway budget-enforce -----------------------------------------------


def test_gateway_records_cost_on_complete(tmp_path):
    """Full integration: Gateway.complete's Claude-call gets logged."""
    from core.audit import AuditLogger
    from core.cost_tracker import current_month_cost, init_cost_schema
    from privacy.gateway import Gateway

    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)

    audit = AuditLogger(tmp_path / "audit")

    # Mock the ClaudeClient
    fake_response = MagicMock()
    fake_response.usage = MagicMock(input_tokens=1000, output_tokens=500)
    fake_response.content = []
    fake_response.stop_reason = "end_turn"

    gw = Gateway(
        api_key="sk-ant-fake", model="claude-sonnet-4-6",
        audit=audit,
        cost_db_path=db_path,
        monthly_budget_usd=0.0,
    )
    gw._claude = MagicMock()
    gw._claude.reply = MagicMock(return_value=fake_response)
    gw._claude.model = "claude-sonnet-4-6"

    gw._call_claude(
        task="test", system="", messages=[], tools=None, max_tokens=100,
        label="internal", classification=None,
    )

    cost = current_month_cost(db_path)
    assert cost.calls == 1
    assert cost.tokens_in == 1000


# --- Graceful Ollama degradation ------------------------------------------


def test_graceful_degradation_reraises_without_redactor(tmp_path):
    """M17h — zonder redactor kan niet safe naar Claude fallback,
    dus re-raise the original exception."""
    from core.audit import AuditLogger
    from privacy.classifier import Classification
    from privacy.gateway import Gateway

    audit = AuditLogger(tmp_path / "audit")
    fake_ollama = MagicMock()
    fake_ollama.model = "llama3.1:8b"
    fake_ollama.chat = MagicMock(side_effect=ConnectionError("Ollama down"))

    gw = Gateway(
        api_key="sk-ant-fake", model="claude-sonnet-4-6",
        audit=audit, local_client=fake_ollama,
        # redactor=None → no safe path
    )
    classification = Classification(
        label="confidential", reason="test", matched=(),
    )
    with pytest.raises(ConnectionError):
        gw._call_local(
            task="t", system="", messages=[{"role": "user", "content": "hi"}],
            max_tokens=100, classification=classification,
        )


def test_graceful_degradation_falls_through_with_redactor(tmp_path):
    """M17h — met redactor: Ollama-failure valt terug op Claude met redact."""
    from core.audit import AuditLogger
    from privacy.classifier import Classification
    from privacy.gateway import Gateway
    from privacy.redactor import Redactor

    audit = AuditLogger(tmp_path / "audit")
    fake_ollama = MagicMock()
    fake_ollama.model = "llama3.1:8b"
    fake_ollama.chat = MagicMock(side_effect=ConnectionError("Ollama down"))

    fake_response = MagicMock()
    fake_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    fake_response.content = []
    fake_response.stop_reason = "end_turn"

    gw = Gateway(
        api_key="sk-ant-fake", model="claude-sonnet-4-6",
        audit=audit, local_client=fake_ollama, redactor=Redactor(),
    )
    gw._claude = MagicMock()
    gw._claude.reply = MagicMock(return_value=fake_response)
    gw._claude.model = "claude-sonnet-4-6"

    classification = Classification(
        label="confidential", reason="test", matched=(),
    )
    result = gw._call_local(
        task="t", system="", messages=[{"role": "user", "content": "hi"}],
        max_tokens=100, classification=classification,
    )
    assert result is not None
    assert gw._claude.reply.called
