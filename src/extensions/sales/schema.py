"""Sales-pipeline schema.

Drie tabellen:
  sales_accounts      — prospects + klanten met target (adl_video/dst_connect/
                        ds_templates/multi), status, cadence, contact
  sales_touchpoints   — interactie-log (manual + auto-detected uit comm_intel)
  sales_triggers      — externe signalen (tenders, insolvencies, market_intel)
                        die aan accounts worden gekoppeld voor scoring
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

VALID_TARGETS = frozenset({"adl_video", "dst_connect", "ds_templates", "multi"})
VALID_STATUSES = frozenset({
    "koud", "nurturing", "kansrijk", "offerte", "won", "lost", "snoozed",
})
VALID_PROSPECT_TYPES = frozenset({
    "end_customer", "av_reseller", "cms_vendor", "other",
})

# Default cadences (in dagen) per target × status. Bepaalt wanneer
# next_touch_at verloopt → account verschijnt in daily top-3.
# YourProduct heeft langere cycles (partnership-traject); ADL en DS
# Templates korter (project/license-traject).
DEFAULT_CADENCES: dict[str, dict[str, int]] = {
    "adl_video":    {"koud": 30, "nurturing": 14, "kansrijk": 7,  "offerte": 5},
    "dst_connect":  {"koud": 45, "nurturing": 21, "kansrijk": 14, "offerte": 7},
    "ds_templates": {"koud": 30, "nurturing": 14, "kansrijk": 7,  "offerte": 5},
    "multi":        {"koud": 30, "nurturing": 14, "kansrijk": 7,  "offerte": 5},
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_accounts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    naam                   TEXT NOT NULL,
    naam_normalized        TEXT NOT NULL,        -- lowercase, voor dedupe + auto-detect
    kvk                    TEXT,                 -- optional
    website                TEXT,
    target                 TEXT NOT NULL
        CHECK (target IN ('adl_video','dst_connect','ds_templates','multi')),
    sub_targets            TEXT,                 -- JSON list als target='multi'
    prospect_type          TEXT,                 -- end_customer | av_reseller | cms_vendor | other
    sector                 TEXT,                 -- retail/horeca/health/public/etc.
    plaats                 TEXT,
    primary_contact_name   TEXT,
    primary_contact_email  TEXT,
    primary_contact_phone  TEXT,
    primary_contact_role   TEXT,
    status                 TEXT NOT NULL DEFAULT 'koud'
        CHECK (status IN ('koud','nurturing','kansrijk','offerte','won','lost','snoozed')),
    next_touch_at          INTEGER,              -- unix; wanneer cadence verloopt
    snoozed_until          INTEGER,              -- alleen bij status='snoozed'
    nurture_cadence_days   INTEGER,              -- per-account override van DEFAULT_CADENCES
    estimated_value_eur    INTEGER,              -- optionele dealwaarde
    notes                  TEXT,
    created_at             INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    created_via            TEXT,                 -- imessage/csv/auto_detect
    won_at                 INTEGER,
    lost_at                INTEGER,
    last_touch_at          INTEGER               -- cache van max(occurred_at) uit touchpoints
);
CREATE INDEX IF NOT EXISTS idx_sales_target_status ON sales_accounts(target, status);
CREATE INDEX IF NOT EXISTS idx_sales_next_touch    ON sales_accounts(next_touch_at);
CREATE INDEX IF NOT EXISTS idx_sales_naam_norm     ON sales_accounts(naam_normalized);
CREATE INDEX IF NOT EXISTS idx_sales_kvk           ON sales_accounts(kvk);

CREATE TABLE IF NOT EXISTS sales_touchpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES sales_accounts(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,              -- email_in/email_out/linkedin/call/meeting/plaud/imessage
    occurred_at     INTEGER NOT NULL,
    summary         TEXT,
    outcome         TEXT,                       -- positive/neutral/negative/no_response
    source_ref      TEXT,                       -- gmail msg-id / plaud meeting-id / null
    detected_auto   INTEGER NOT NULL DEFAULT 0  -- 0=manual via iMessage, 1=auto uit comm_intel
);
CREATE INDEX IF NOT EXISTS idx_sales_tp_account ON sales_touchpoints(account_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_sales_tp_source  ON sales_touchpoints(source_ref);

CREATE TABLE IF NOT EXISTS sales_triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER REFERENCES sales_accounts(id) ON DELETE SET NULL,
    naam_match      TEXT,                       -- bedrijfsnaam toen trigger fired
    source          TEXT NOT NULL,              -- tender/insolvency/market_intel/manual
    source_ref      TEXT,                       -- key in bron-tabel
    occurred_at     INTEGER NOT NULL,
    title           TEXT,
    details         TEXT,
    consumed_at     INTEGER                     -- wanneer in briefing geserveerd
);
CREATE INDEX IF NOT EXISTS idx_sales_trig_account  ON sales_triggers(account_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_sales_trig_unconsumed ON sales_triggers(consumed_at) WHERE consumed_at IS NULL;
"""


def init_sales_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def normalize_naam(s: str | None) -> str:
    """Voor dedupe + auto-detect: lowercase, strip suffixen als 'B.V.'."""
    if not s:
        return ""
    norm = s.lower().strip()
    for suffix in (" b.v.", " bv", " n.v.", " nv", " holding"):
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)].rstrip()
    return norm


def prune_inactive_cold_accounts(
    conn: sqlite3.Connection, *, days_since_last_touch: int = 730,
) -> tuple[int, list[int]]:
    """M3 retention: hard-delete `koud` accounts zonder enige touchpoint
    in laatste N dagen. Returnt (rowcount_removed, list_of_ids_removed)
    voor audit-log. ON DELETE CASCADE op sales_touchpoints zorgt voor
    cleanup van interactie-data. sales_triggers krijgen account_id=NULL
    (ON DELETE SET NULL)."""
    cutoff = f"strftime('%s','now') - {int(days_since_last_touch)} * 86400"
    rows = conn.execute(
        f"SELECT id FROM sales_accounts "
        f"WHERE status = 'koud' "
        f"  AND created_at < {cutoff} "
        f"  AND (last_touch_at IS NULL OR last_touch_at < {cutoff})"
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    if not ids:
        return 0, []
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"DELETE FROM sales_accounts WHERE id IN ({placeholders})", ids,
    )
    return int(cur.rowcount or 0), ids


def cadence_for(target: str, status: str, override: int | None = None) -> int:
    """Aantal dagen voor next_touch_at. Override (per-account) wint."""
    if override is not None and override > 0:
        return int(override)
    return DEFAULT_CADENCES.get(target, DEFAULT_CADENCES["multi"]).get(status, 14)


def compute_next_touch(
    *, target: str, status: str, last_touch_unix: int | None,
    cadence_override: int | None = None, now_unix: int,
) -> int | None:
    """Bereken `next_touch_at`. Status `won`/`lost`/`snoozed` → None
    (geen reguliere opvolging). Anders: laatste_touch + cadence, of
    nu + 1 dag als nog nooit getouched."""
    if status in ("won", "lost", "snoozed"):
        return None
    cadence_days = cadence_for(target, status, cadence_override)
    base = last_touch_unix if last_touch_unix else now_unix
    return base + cadence_days * 86400
