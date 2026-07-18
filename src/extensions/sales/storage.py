"""CRUD-helpers voor sales_accounts + sales_touchpoints.

Houdt `last_touch_at` + `next_touch_at` op de account up-to-date bij
elke touchpoint-insert/status-change.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .schema import (
    VALID_PROSPECT_TYPES,
    VALID_STATUSES,
    VALID_TARGETS,
    compute_next_touch,
    normalize_naam,
)

# ---- accounts ---------------------------------------------------------

def insert_account(
    conn: sqlite3.Connection, *,
    naam: str,
    target: str,
    sub_targets: list[str] | None = None,
    prospect_type: str | None = None,
    sector: str | None = None,
    plaats: str | None = None,
    kvk: str | None = None,
    website: str | None = None,
    primary_contact_name: str | None = None,
    primary_contact_email: str | None = None,
    primary_contact_phone: str | None = None,
    primary_contact_role: str | None = None,
    status: str = "koud",
    nurture_cadence_days: int | None = None,
    estimated_value_eur: int | None = None,
    notes: str | None = None,
    created_via: str = "imessage",
) -> int:
    if target not in VALID_TARGETS:
        raise ValueError(f"target must be one of {sorted(VALID_TARGETS)}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    if prospect_type is not None and prospect_type not in VALID_PROSPECT_TYPES:
        raise ValueError(
            f"prospect_type must be one of {sorted(VALID_PROSPECT_TYPES)}"
        )
    if target == "multi" and not sub_targets:
        raise ValueError("sub_targets required when target='multi'")
    if sub_targets:
        bad = [t for t in sub_targets if t not in VALID_TARGETS - {"multi"}]
        if bad:
            raise ValueError(f"invalid sub_targets: {bad}")

    now = int(time.time())
    next_touch = compute_next_touch(
        target=target, status=status, last_touch_unix=None,
        cadence_override=nurture_cadence_days, now_unix=now,
    )
    cur = conn.execute(
        """INSERT INTO sales_accounts
           (naam, naam_normalized, kvk, website, target, sub_targets,
            prospect_type, sector, plaats,
            primary_contact_name, primary_contact_email,
            primary_contact_phone, primary_contact_role,
            status, next_touch_at, nurture_cadence_days,
            estimated_value_eur, notes, created_via)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            naam.strip(), normalize_naam(naam), kvk, website,
            target,
            json.dumps(sub_targets) if sub_targets else None,
            prospect_type, sector, plaats,
            primary_contact_name, primary_contact_email,
            primary_contact_phone, primary_contact_role,
            status, next_touch,
            int(nurture_cadence_days) if nurture_cadence_days else None,
            int(estimated_value_eur) if estimated_value_eur else None,
            notes, created_via,
        ),
    )
    return int(cur.lastrowid or 0)


def get_account(conn: sqlite3.Connection, account_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sales_accounts WHERE id = ?", (account_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def find_account_by_name(
    conn: sqlite3.Connection, naam: str,
) -> dict[str, Any] | None:
    norm = normalize_naam(naam)
    if not norm:
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sales_accounts WHERE naam_normalized = ? LIMIT 1",
        (norm,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_account(
    conn: sqlite3.Connection, account_id: int, **fields: Any,
) -> dict[str, Any] | None:
    """Update willekeurige velden. Bij status-change wordt next_touch_at
    automatisch herberekend."""
    current = get_account(conn, account_id)
    if current is None:
        return None

    settable = {
        "naam", "kvk", "website", "target", "sub_targets",
        "prospect_type", "sector", "plaats",
        "primary_contact_name", "primary_contact_email",
        "primary_contact_phone", "primary_contact_role",
        "status", "nurture_cadence_days", "estimated_value_eur",
        "notes", "snoozed_until",
    }
    filtered = {k: v for k, v in fields.items() if k in settable}
    if "naam" in filtered:
        filtered["naam_normalized"] = normalize_naam(filtered["naam"])
    if "status" in filtered and filtered["status"] not in VALID_STATUSES:
        raise ValueError("invalid status")
    if "target" in filtered and filtered["target"] not in VALID_TARGETS:
        raise ValueError("invalid target")
    if "sub_targets" in filtered and isinstance(filtered["sub_targets"], list):
        filtered["sub_targets"] = json.dumps(filtered["sub_targets"])

    if not filtered:
        return current

    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    params: list[Any] = list(filtered.values()) + [account_id]
    conn.execute(
        f"UPDATE sales_accounts SET {set_clause} WHERE id = ?", params,
    )

    new_status = filtered.get("status", current["status"])
    new_target = filtered.get("target", current["target"])
    new_cadence = filtered.get(
        "nurture_cadence_days", current.get("nurture_cadence_days"),
    )
    if new_status == "won":
        conn.execute(
            "UPDATE sales_accounts SET won_at = strftime('%s','now') "
            "WHERE id = ? AND won_at IS NULL", (account_id,),
        )
    elif new_status == "lost":
        conn.execute(
            "UPDATE sales_accounts SET lost_at = strftime('%s','now') "
            "WHERE id = ? AND lost_at IS NULL", (account_id,),
        )

    next_touch = compute_next_touch(
        target=new_target, status=new_status,
        last_touch_unix=current.get("last_touch_at"),
        cadence_override=new_cadence,
        now_unix=int(time.time()),
    )
    conn.execute(
        "UPDATE sales_accounts SET next_touch_at = ? WHERE id = ?",
        (next_touch, account_id),
    )
    return get_account(conn, account_id)


def snooze_account(
    conn: sqlite3.Connection, account_id: int, *, days: int,
) -> dict[str, Any] | None:
    until_unix = int(time.time()) + max(1, int(days)) * 86400
    conn.execute(
        "UPDATE sales_accounts SET status = 'snoozed', "
        "snoozed_until = ?, next_touch_at = ? WHERE id = ?",
        (until_unix, until_unix, account_id),
    )
    return get_account(conn, account_id)


def unsnooze_expired(conn: sqlite3.Connection) -> int:
    """Snooze verlopen → terug naar status='nurturing'. Returns rowcount."""
    cur = conn.execute(
        "UPDATE sales_accounts SET status = 'nurturing', snoozed_until = NULL "
        "WHERE status = 'snoozed' AND snoozed_until IS NOT NULL "
        "AND snoozed_until < strftime('%s','now')"
    )
    affected = int(cur.rowcount or 0)
    # Recompute next_touch voor alle ge-unsnoozed accounts — eenvoudig
    # door cadence_for default toe te passen vanaf nu
    if affected:
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') + 86400 * "
            "COALESCE(nurture_cadence_days, 14) "
            "WHERE status = 'nurturing' AND next_touch_at IS NULL"
        )
    return affected


def list_accounts(
    conn: sqlite3.Connection, *,
    target: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if target:
        where.append("target = ?")
        params.append(target)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM sales_accounts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY status, naam LIMIT ?"
    params.append(int(limit))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def forget_account(
    conn: sqlite3.Connection, account_id: int,
) -> dict[str, Any] | None:
    """M4 — GDPR right-to-be-forgotten. Returnt de geforgotten account
    info voor audit-log, daarna delete. Touchpoints expliciet verwijderd
    omdat sqlite default geen FK CASCADE afdwingt; triggers krijgen
    account_id NULL (handmatig, om dezelfde reden)."""
    acc = get_account(conn, account_id)
    if acc is None:
        return None
    conn.execute(
        "DELETE FROM sales_touchpoints WHERE account_id = ?", (account_id,),
    )
    conn.execute(
        "UPDATE sales_triggers SET account_id = NULL WHERE account_id = ?",
        (account_id,),
    )
    conn.execute(
        "DELETE FROM sales_accounts WHERE id = ?", (account_id,),
    )
    return acc


def search_accounts(
    conn: sqlite3.Connection, query: str, *, limit: int = 20,
) -> list[dict[str, Any]]:
    q = f"%{query.lower()}%"
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sales_accounts "
        "WHERE naam_normalized LIKE ? OR primary_contact_email LIKE ? "
        "   OR primary_contact_name LIKE ? OR kvk = ? "
        "ORDER BY status, naam LIMIT ?",
        (q, q, q, query.strip(), int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---- touchpoints -----------------------------------------------------

def insert_touchpoint(
    conn: sqlite3.Connection, *,
    account_id: int,
    channel: str,
    occurred_at_unix: int | None = None,
    summary: str | None = None,
    outcome: str | None = None,
    source_ref: str | None = None,
    detected_auto: bool = False,
) -> int:
    ts = int(occurred_at_unix) if occurred_at_unix else int(time.time())
    cur = conn.execute(
        """INSERT INTO sales_touchpoints
           (account_id, channel, occurred_at, summary, outcome, source_ref,
            detected_auto)
           VALUES (?,?,?,?,?,?,?)""",
        (account_id, channel, ts, summary, outcome, source_ref,
         1 if detected_auto else 0),
    )
    # Update last_touch_at + next_touch_at op de account
    conn.row_factory = sqlite3.Row
    acc = conn.execute(
        "SELECT target, status, nurture_cadence_days FROM sales_accounts "
        "WHERE id = ?", (account_id,),
    ).fetchone()
    if acc:
        next_touch = compute_next_touch(
            target=acc["target"], status=acc["status"],
            last_touch_unix=ts,
            cadence_override=acc["nurture_cadence_days"],
            now_unix=int(time.time()),
        )
        conn.execute(
            "UPDATE sales_accounts SET last_touch_at = ?, next_touch_at = ? "
            "WHERE id = ?",
            (ts, next_touch, account_id),
        )
    return int(cur.lastrowid or 0)


def list_touchpoints(
    conn: sqlite3.Connection, account_id: int, *, limit: int = 20,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sales_touchpoints WHERE account_id = ? "
        "ORDER BY occurred_at DESC LIMIT ?",
        (account_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def touchpoint_exists_for_source(
    conn: sqlite3.Connection, source_ref: str,
) -> bool:
    """Voor auto-detect dedupe: heeft een eerder ingest-tick deze al
    geregistreerd?"""
    row = conn.execute(
        "SELECT 1 FROM sales_touchpoints WHERE source_ref = ? LIMIT 1",
        (source_ref,),
    ).fetchone()
    return row is not None


# ---- helpers ---------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("sub_targets"):
        try:
            d["sub_targets"] = json.loads(d["sub_targets"])
        except (TypeError, json.JSONDecodeError):
            pass
    return d
