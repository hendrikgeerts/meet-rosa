"""Tests voor code-review-3 fixes (H1, H3, H4, M2, M3, M4)."""
from __future__ import annotations

import pytest

# --- H-3: BudgetExceeded propagates (not swallowed) ------------------------


def test_h3_budget_exceeded_propagates_from_gateway(tmp_path):
    """H-3: BudgetExceeded moet doorpropaganderen naar caller, niet
    door de outer try/except in _record_cost gegeten worden."""
    from core.audit import AuditLogger
    from core.cost_tracker import (
        BudgetExceeded,
        init_cost_schema,
        record_call,
    )
    from privacy.gateway import Gateway

    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)

    # Vul de db met kosten die budget overschrijden
    record_call(
        db_path, task="prior", model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,  # $18 spend
    )

    audit = AuditLogger(tmp_path / "audit")
    gw = Gateway(
        api_key="sk-ant-fake", model="claude-sonnet-4-6",
        audit=audit,
        cost_db_path=db_path,
        monthly_budget_usd=10.0,  # cap lower than existing spend
    )

    # Pre-flight check zou moeten BudgetExceeded raise'n
    with pytest.raises(BudgetExceeded, match="budget"):
        gw._pre_flight_budget_check(task="test-call")


def test_h3_pre_flight_disabled_when_budget_zero(tmp_path):
    from core.audit import AuditLogger
    from core.cost_tracker import init_cost_schema, record_call
    from privacy.gateway import Gateway

    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)
    record_call(
        db_path, task="t", model="claude-sonnet-4-6",
        tokens_in=100_000_000, tokens_out=100_000_000,
    )
    audit = AuditLogger(tmp_path / "audit")
    gw = Gateway(
        api_key="sk-ant-fake", model="claude-sonnet-4-6",
        audit=audit, cost_db_path=db_path,
        monthly_budget_usd=0.0,  # disabled
    )
    # No raise expected
    gw._pre_flight_budget_check(task="anything")


# --- H-4: Model-family alias for pricing ----------------------------------


def test_h4_haiku_family_price_matches_versioned():
    from core.cost_tracker import _price_for
    # Exact match
    p1 = _price_for("claude-haiku-4")
    # Versioned (should family-match, not fall through to Sonnet _default)
    p2 = _price_for("claude-haiku-4-5-20251001")
    assert p1 == p2
    # And Haiku is cheaper than Sonnet default
    assert p2["input"] < 3.0


def test_h4_sonnet_family_price():
    from core.cost_tracker import _price_for
    p = _price_for("claude-sonnet-4-6")
    assert p["input"] == 3.0
    assert p["output"] == 15.0


def test_h4_unknown_model_logs_warning(caplog):
    import logging

    from core.cost_tracker import _price_for
    with caplog.at_level(logging.WARNING):
        _price_for("nonexistent-model")
    assert any("unknown model" in r.message for r in caplog.records)


def test_h4_env_override_wins_over_defaults(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PRICE_CLAUDE_HAIKU_4_INPUT", "0.5")
    monkeypatch.setenv("ANTHROPIC_PRICE_CLAUDE_HAIKU_4_OUTPUT", "2.0")
    from core.cost_tracker import _price_for
    p = _price_for("claude-haiku-4")
    assert p["input"] == 0.5
    assert p["output"] == 2.0


# --- H-1: rosa reload uses pidfile ----------------------------------------


def test_h1_reload_prefers_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)

    # Write a pidfile pointing to our own PID (which is alive)
    import os
    pidfile = tmp_path / "rosa.pid"
    pidfile.write_text(f"{os.getpid()}\n")

    from cli.reload_cmd import _find_rosa_pid
    pid = _find_rosa_pid()
    assert pid == os.getpid()


def test_h1_reload_skips_stale_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)

    pidfile = tmp_path / "rosa.pid"
    pidfile.write_text("999999999\n")  # PID that doesn't exist

    # Zonder een echte match zou pgrep-fallback moeten worden probed;
    # accepteert None als er niets draait op deze test-machine.
    from cli.reload_cmd import _find_rosa_pid
    pid = _find_rosa_pid()
    # May be None or a real PID from pgrep — the important thing is
    # we didn't return 999999999.
    assert pid != 999999999


# --- M-2: timezone consistency --------------------------------------------


def test_m2_current_month_uses_local_time(tmp_path):
    """Insert a row and verify it's counted regardless of UTC/local
    difference. Meest belangrijk: geen crashes."""
    from core.cost_tracker import current_month_cost, init_cost_schema, record_call
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)
    record_call(
        db_path, task="t", model="claude-sonnet-4-6",
        tokens_in=1000, tokens_out=500,
    )
    m = current_month_cost(db_path)
    assert m.calls == 1


# --- M-3: SYSTEM_PROMPT_TEMPLATE importable without main -------------------


def test_m3_prompt_template_lives_in_core_prompts():
    """M-3 refactor: rosa simulate mag core.prompts importeren zonder
    main.py's module-scope side-effects te triggeren."""
    from core.prompts import SYSTEM_PROMPT_TEMPLATE
    assert isinstance(SYSTEM_PROMPT_TEMPLATE, str)
    assert "${user_name}" in SYSTEM_PROMPT_TEMPLATE
    assert len(SYSTEM_PROMPT_TEMPLATE) > 1000


def test_m3_main_reexports_prompt_template():
    """Backwards-compat: main.SYSTEM_PROMPT_TEMPLATE moet blijven werken
    voor tests die op main.py's namespace patchen."""
    import main
    from core.prompts import SYSTEM_PROMPT_TEMPLATE
    assert main.SYSTEM_PROMPT_TEMPLATE is SYSTEM_PROMPT_TEMPLATE


# --- M-4: SQLite WAL + busy_timeout ---------------------------------------


def test_m4_wal_mode_enabled(tmp_path):
    """M-4: init_cost_schema moet WAL journal_mode zetten voor concurrent
    writer safety."""
    import sqlite3

    from core.cost_tracker import init_cost_schema
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_m4_concurrent_writes_dont_lose_data(tmp_path):
    """M-4: Twee threads die tegelijk record_call doen mogen geen rijen
    verliezen (met busy_timeout=30s werkt SQLite fine op WAL)."""
    import threading

    from core.cost_tracker import (
        current_month_cost,
        init_cost_schema,
        record_call,
    )
    db_path = tmp_path / "memory.db"
    init_cost_schema(db_path)

    N = 20
    def worker():
        for _ in range(N):
            record_call(
                db_path, task="race", model="claude-sonnet-4-6",
                tokens_in=100, tokens_out=100,
            )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    m = current_month_cost(db_path)
    assert m.calls == 4 * N
