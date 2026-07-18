"""Cost + rate-limit tracker voor Claude API-calls.

Legt per-call kostenschatting vast in SQLite en enforce't een
maandelijkse budget-cap zodat een bug-loop je collega niet failliet
maakt. Prijzen zijn gebaseerd op Anthropic's public pricing table
(sept 2026) — actualiseer via ANTHROPIC_PRICE_*_env-vars.

Rate-limit is soft: bij overschrijding wordt de call NIET geblokkeerd
maar krijgt de gateway een `RateLimitExceeded` exception die de
caller kan afhandelen (bv. iMessage terugstuurt "even wachten, budget
op").
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# Anthropic public prices per 1M tokens (usd). Override via env-vars.
# We prefixen op family-basis omdat exacte model-versies (haiku-4-5-20251001)
# frequent nieuwe suffixen krijgen die dezelfde prijs behouden.
DEFAULT_PRICES = {
    "claude-opus-4":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0,  "output": 15.0},
    "claude-haiku-4":  {"input": 0.8,  "output": 4.0},
    # Fallback voor onbekende modellen — bewust Sonnet-price zodat we
    # over-reporteren i.p.v. onder (aan de veilige kant voor budget-check).
    "_default": {"input": 3.0, "output": 15.0},
}


def _price_for(model: str) -> dict[str, float]:
    """Get input/output $/1M-token prices for a model.

    Model-lookup gebruikt prefix-match op family. Env-override kan met
    ANTHROPIC_PRICE_<MODEL>_INPUT/_OUTPUT — dot/dash in model-name
    worden vervangen door underscore.
    """
    env_key = model.upper().replace('-', '_').replace('.', '_')
    env_in = os.environ.get(f"ANTHROPIC_PRICE_{env_key}_INPUT")
    env_out = os.environ.get(f"ANTHROPIC_PRICE_{env_key}_OUTPUT")
    if env_in and env_out:
        return {"input": float(env_in), "output": float(env_out)}
    # Exact match first
    if model in DEFAULT_PRICES:
        return DEFAULT_PRICES[model]
    # Prefix-match (claude-haiku-4-5-20251001 → claude-haiku-4)
    for family, price in DEFAULT_PRICES.items():
        if family != "_default" and model.startswith(family):
            return price
    log.warning(
        "unknown model %r for pricing; falling back to _default "
        "(will over-report cost). Add to DEFAULT_PRICES or set "
        "ANTHROPIC_PRICE_%s_{INPUT,OUTPUT} env vars.",
        model, env_key,
    )
    return DEFAULT_PRICES["_default"]


def usd_for(model: str, tokens_in: int, tokens_out: int) -> float:
    p = _price_for(model)
    return (tokens_in / 1_000_000) * p["input"] + (tokens_out / 1_000_000) * p["output"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    task TEXT,
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_claude_calls_ts ON claude_calls(ts DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """SQLite-verbinding met WAL + 30s busy_timeout (M-4). Gedeeld door
    scheduler-thread + main-thread → beide kunnen concurrent recorden
    zonder 'database is locked' fout."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_cost_schema(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)


def record_call(
    db_path: Path,
    *,
    task: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> float:
    """Log a Claude call. Returns computed USD-cost."""
    usd = usd_for(model, tokens_in, tokens_out)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO claude_calls (ts, task, model, tokens_in, tokens_out, usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), task, model, tokens_in, tokens_out, usd),
        )
    return usd


@dataclass
class MonthlyCost:
    calls: int
    tokens_in: int
    tokens_out: int
    usd: float


def current_month_cost(db_path: Path) -> MonthlyCost:
    """Sum spend from the start of the current calendar month in LOCAL time.

    Local-time consistency (M-2): een call om 01:00 CET op Aug 1 zou in
    UTC nog Jul 31 zijn — als we UTC-boundary gebruiken zou die call in
    juli-spend blijven ondanks dat de wal-klok augustus zegt. Zowel deze
    functie als `daily_series` gebruiken lokaal-tijd zodat cost-view
    matcht met de user's kalender."""
    from datetime import datetime
    now = datetime.now()  # naive = local time
    month_start = datetime(now.year, now.month, 1)  # local midnight
    since = int(month_start.timestamp())
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in), 0), "
            "COALESCE(SUM(tokens_out), 0), COALESCE(SUM(usd), 0.0) "
            "FROM claude_calls WHERE ts >= ?",
            (since,),
        ).fetchone()
    return MonthlyCost(
        calls=row[0], tokens_in=row[1], tokens_out=row[2], usd=row[3],
    )


class BudgetExceeded(RuntimeError):
    """Raised bij Gateway wanneer maandelijkse budget cap gehaald is."""


def check_budget(
    db_path: Path,
    monthly_budget_usd: float,
    *,
    task: str = "unknown",
) -> None:
    """Raise BudgetExceeded als deze maand's spend > budget."""
    if monthly_budget_usd <= 0:
        return  # 0 == disabled
    cost = current_month_cost(db_path)
    if cost.usd >= monthly_budget_usd:
        raise BudgetExceeded(
            f"Monthly Anthropic budget of ${monthly_budget_usd:.2f} exceeded "
            f"(${cost.usd:.2f} across {cost.calls} calls). "
            f"Task blocked: {task!r}. Edit budget in config.yaml → "
            f"privacy.monthly_anthropic_budget_usd."
        )


def daily_series(db_path: Path, days: int = 30) -> list[dict]:
    """Return per-day cost totals for the last N days (nieuwste eerst).

    Groeit per local-time date zodat de output matcht met wat je user
    op de kalender ziet (M-2)."""
    from datetime import datetime
    now = datetime.now()
    since = int(now.timestamp()) - days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            # 'localtime' modifier group't op wall-clock date, niet UTC.
            "SELECT date(ts, 'unixepoch', 'localtime') as d, COUNT(*), SUM(usd) "
            "FROM claude_calls WHERE ts >= ? "
            "GROUP BY d ORDER BY d DESC",
            (since,),
        ).fetchall()
    return [{"date": r[0], "calls": r[1], "usd": r[2] or 0.0} for r in rows]
