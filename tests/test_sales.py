"""Tests voor sales-pipeline: storage + selectie-algoritme + tools."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from extensions.sales.schema import (
    DEFAULT_CADENCES, compute_next_touch, init_sales_schema, normalize_naam,
)
from extensions.sales.selection import select_top_n
from extensions.sales.storage import (
    find_account_by_name, get_account, insert_account, insert_touchpoint,
    list_accounts, search_accounts, snooze_account, unsnooze_expired,
    update_account,
)
from extensions.sales.tools import (
    SALES_HANDLERS, SALES_TOOL_SCHEMAS,
    sales_account_add_handler, sales_account_list_handler,
    sales_account_search_handler, sales_account_set_status_handler,
    sales_account_snooze_handler, sales_account_update_handler,
    sales_pipeline_status_handler, sales_top3_today_handler,
    sales_touchpoint_history_handler, sales_touchpoint_log_handler,
    sales_why_handler,
)
from extensions.sales.briefing import build_sales_pulse


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "sales.db"
    init_sales_schema(p)
    return p


# ---- normalize_naam ---------------------------------------------------

def test_normalize_naam_strips_bv() -> None:
    assert normalize_naam("Heineken B.V.") == "heineken"
    assert normalize_naam("Mediq Holding") == "mediq"
    assert normalize_naam("Achmea NV") == "achmea"


def test_normalize_naam_empty() -> None:
    assert normalize_naam("") == ""
    assert normalize_naam(None) == ""


# ---- cadence -----------------------------------------------------------

def test_cadence_defaults_differ_per_target() -> None:
    """DST Connect heeft langere cycles dan ADL/DS."""
    assert DEFAULT_CADENCES["dst_connect"]["nurturing"] > \
           DEFAULT_CADENCES["adl_video"]["nurturing"]


def test_compute_next_touch_uses_last_touch_when_present() -> None:
    now = 1700_000_000
    last_touch = now - 5 * 86400
    nxt = compute_next_touch(
        target="adl_video", status="nurturing",
        last_touch_unix=last_touch, now_unix=now,
    )
    expected = last_touch + 14 * 86400
    assert nxt == expected


def test_compute_next_touch_uses_now_when_no_last_touch() -> None:
    now = 1700_000_000
    nxt = compute_next_touch(
        target="dst_connect", status="koud",
        last_touch_unix=None, now_unix=now,
    )
    assert nxt == now + 45 * 86400


def test_compute_next_touch_none_for_terminal_status() -> None:
    for status in ("won", "lost", "snoozed"):
        nxt = compute_next_touch(
            target="adl_video", status=status,
            last_touch_unix=None, now_unix=1700_000_000,
        )
        assert nxt is None


# ---- storage CRUD ------------------------------------------------------

def test_insert_account_basic(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(
            conn, naam="Heineken B.V.", target="adl_video",
            prospect_type="end_customer", sector="horeca",
            primary_contact_email="anne@heineken.nl",
        )
        acc = get_account(conn, aid)
    assert acc is not None
    assert acc["naam"] == "Heineken B.V."
    assert acc["naam_normalized"] == "heineken"
    assert acc["target"] == "adl_video"
    assert acc["status"] == "koud"
    assert acc["next_touch_at"] is not None


def test_insert_account_multi_requires_sub_targets(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        with pytest.raises(ValueError, match="sub_targets"):
            insert_account(conn, naam="X", target="multi")


def test_insert_account_invalid_target(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        with pytest.raises(ValueError, match="target"):
            insert_account(conn, naam="X", target="xyz")


def test_find_account_by_name_dedupes_via_normalization(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(conn, naam="Heineken B.V.", target="adl_video")
        # Zelfde bedrijf zonder BV en uppercase
        acc = find_account_by_name(conn, "HEINEKEN")
    assert acc is not None
    assert acc["id"] == aid


def test_update_account_status_recomputes_next_touch(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(
            conn, naam="X", target="adl_video", status="koud",
        )
        original = get_account(conn, aid)
        # Upgrade to offerte → cadence van 5d (was 30d)
        updated = update_account(conn, aid, status="offerte")
    assert updated["status"] == "offerte"
    assert updated["next_touch_at"] < original["next_touch_at"]


def test_update_account_won_sets_won_at(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(conn, naam="X", target="adl_video")
        updated = update_account(conn, aid, status="won")
    assert updated["status"] == "won"
    assert updated["won_at"] is not None
    assert updated["next_touch_at"] is None  # terminal status


def test_snooze_account_sets_status_and_until(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(conn, naam="X", target="adl_video")
        snoozed = snooze_account(conn, aid, days=14)
    assert snoozed["status"] == "snoozed"
    assert snoozed["snoozed_until"] is not None
    assert snoozed["next_touch_at"] == snoozed["snoozed_until"]


def test_unsnooze_expired(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="adl_video")
        # Simuleer een snooze die al voorbij is
        conn.execute(
            "UPDATE sales_accounts SET status='snoozed', "
            "snoozed_until = strftime('%s','now') - 86400, "
            "next_touch_at = strftime('%s','now') - 86400 WHERE id = ?",
            (aid,),
        )
        affected = unsnooze_expired(conn)
    assert affected == 1
    with sqlite3.connect(db) as conn:
        acc = get_account(conn, aid)
    assert acc["status"] == "nurturing"
    assert acc["snoozed_until"] is None


def test_insert_touchpoint_updates_last_and_next(db: Path) -> None:
    """Touchpoint op 'nu' verlengt next_touch met de cadence (14d voor
    nurturing adl_video). Maak occurred_at expliciet groter dan
    insert-time anders is alles binnen één unix-second."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="adl_video",
                              status="nurturing")
        before = get_account(conn, aid)
        future_touch = int(time.time()) + 60  # 1 minuut later dan insert
        insert_touchpoint(
            conn, account_id=aid, channel="email_out",
            occurred_at_unix=future_touch,
            summary="follow-up sent",
        )
        after = get_account(conn, aid)
    assert after["last_touch_at"] == future_touch
    # next_touch_at = last_touch_at + 14*86400
    assert after["next_touch_at"] > before["next_touch_at"]


def test_search_accounts_matches_email(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        insert_account(
            conn, naam="X", target="adl_video",
            primary_contact_email="anne@heineken.nl",
        )
        rows = search_accounts(conn, "heineken")
    assert len(rows) == 1


# ---- selection -------------------------------------------------------

def test_top3_prioritizes_old_offertes(db: Path) -> None:
    """Open offerte met laatste touch >5d wint van cadence-overdue
    kansrijk."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        old_offerte = insert_account(
            conn, naam="Mediq", target="ds_templates", status="offerte",
        )
        # Forceer laatste touch 10d geleden
        conn.execute(
            "UPDATE sales_accounts SET last_touch_at = "
            "strftime('%s','now') - 10*86400 WHERE id = ?",
            (old_offerte,),
        )
        kansrijk = insert_account(
            conn, naam="Heineken", target="adl_video", status="kansrijk",
        )
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') - 86400 WHERE id = ?",
            (kansrijk,),
        )
    top = select_top_n(db, n=3)
    assert top[0].account["id"] == old_offerte
    assert top[0].reason_code == "urgent_offerte"


def test_top3_diversifies_when_targets_mixed(db: Path) -> None:
    """Met cadence-overdue accounts uit 3 targets respecteert
    max_per_target=2 — dus geen target krijgt 3 slots."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        for naam, target in (
            ("A1", "adl_video"), ("A2", "adl_video"), ("A3", "adl_video"),
            ("D1", "dst_connect"),
            ("S1", "ds_templates"),
        ):
            aid = insert_account(
                conn, naam=naam, target=target, status="nurturing",
            )
            conn.execute(
                "UPDATE sales_accounts SET next_touch_at = "
                "strftime('%s','now') - 86400 WHERE id = ?", (aid,),
            )
    top = select_top_n(db, n=3)
    targets = [t.account["target"] for t in top]
    # Geen target meer dan 2 keer wanneer er alternatives beschikbaar zijn
    for t in set(targets):
        assert targets.count(t) <= 2


def test_top3_relaxes_diversification_when_no_alternatives(db: Path) -> None:
    """Als alle candidates van zelfde target zijn (geen alternatives),
    vult selectie alsnog n=3 met die target — beter 3 acties dan 2."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        for naam in ("A", "B", "C", "D"):
            aid = insert_account(
                conn, naam=naam, target="adl_video", status="nurturing",
            )
            conn.execute(
                "UPDATE sales_accounts SET next_touch_at = "
                "strftime('%s','now') - 86400 WHERE id = ?", (aid,),
            )
    top = select_top_n(db, n=3)
    assert len(top) == 3
    assert all(t.account["target"] == "adl_video" for t in top)


def test_top3_includes_trigger_recent(db: Path) -> None:
    """Account met onbenutte trigger van vandaag → trigger_today reason."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(
            conn, naam="X", target="dst_connect", status="nurturing",
        )
        conn.execute(
            "INSERT INTO sales_triggers "
            "(account_id, source, source_ref, occurred_at, title) "
            "VALUES (?, 'tender', 'tnd:1', strftime('%s','now'), 'AV-tender XX')",
            (aid,),
        )
    top = select_top_n(db, n=3)
    found = [t for t in top if t.reason_code == "trigger_today"]
    assert len(found) == 1
    assert found[0].account["id"] == aid


def test_top3_skips_snoozed(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(
            conn, naam="X", target="adl_video", status="nurturing",
        )
        snooze_account(conn, aid, days=30)
    top = select_top_n(db, n=3)
    assert all(t.account["id"] != aid for t in top)


# ---- tools -----------------------------------------------------------

def test_tool_add_account_via_handler(db: Path) -> None:
    out = sales_account_add_handler(db, {
        "naam": "Heineken",
        "target": "adl_video",
        "prospect_type": "end_customer",
        "sector": "horeca",
        "contact_email": "anne@heineken.nl",
    })
    assert out["ok"] is True
    assert out["account"]["target"] == "adl_video"


def test_tool_add_account_rejects_duplicate(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    out = sales_account_add_handler(db, {"naam": "X B.V.", "target": "adl_video"})
    assert out["ok"] is False
    assert "bestaat al" in out["error"]


def test_tool_add_account_multi_requires_sub_targets(db: Path) -> None:
    out = sales_account_add_handler(db, {"naam": "Z", "target": "multi"})
    assert out["ok"] is False
    assert "sub_targets" in out["error"]


def test_tool_add_account_multi_with_sub_targets(db: Path) -> None:
    out = sales_account_add_handler(db, {
        "naam": "Q", "target": "multi",
        "sub_targets": ["adl_video", "ds_templates"],
    })
    assert out["ok"] is True
    assert sorted(out["account"]["sub_targets"]) == ["adl_video", "ds_templates"]


def test_tool_set_status(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    out = sales_account_set_status_handler(db, {"naam": "X", "status": "kansrijk"})
    assert out["ok"] is True
    assert out["account"]["status"] == "kansrijk"


def test_tool_touchpoint_log(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    out = sales_touchpoint_log_handler(db, {
        "naam": "X", "channel": "email_out", "summary": "follow-up",
    })
    assert out["ok"] is True
    assert out["account"]["last_touch_at"] is not None


def test_tool_touchpoint_invalid_channel(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    out = sales_touchpoint_log_handler(db, {
        "naam": "X", "channel": "carrier-pigeon",
    })
    assert out["ok"] is False


def test_tool_snooze(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    out = sales_account_snooze_handler(db, {"naam": "X", "days": 14})
    assert out["ok"] is True
    assert out["account"]["status"] == "snoozed"


def test_tool_list_filter_by_target(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "A", "target": "adl_video"})
    sales_account_add_handler(db, {"naam": "B", "target": "dst_connect"})
    out = sales_account_list_handler(db, {"target": "adl_video"})
    assert out["count"] == 1
    assert out["accounts"][0]["target"] == "adl_video"


def test_tool_pipeline_status_groups_per_target(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "A", "target": "adl_video"})
    sales_account_add_handler(db, {"naam": "B", "target": "dst_connect",
                                     "status": "offerte"})
    out = sales_pipeline_status_handler(db, {})
    assert out["ok"] is True
    assert out["total_accounts"] == 2
    pipe = out["pipeline_by_target"]
    assert "adl_video" in pipe
    assert "dst_connect" in pipe
    assert pipe["dst_connect"]["offerte"]["count"] == 1


def test_tool_top3_today_returns_selections(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "A", "target": "adl_video",
                                     "status": "offerte"})
    out = sales_top3_today_handler(db, {})
    assert out["ok"] is True
    assert out["count"] >= 1


def test_tool_why_explains_top_account(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "A", "target": "adl_video",
                                     "status": "offerte"})
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = conn.execute("SELECT id FROM sales_accounts").fetchone()[0]
        # Force old last_touch zodat het in urgent_offerte komt
        conn.execute(
            "UPDATE sales_accounts SET last_touch_at = "
            "strftime('%s','now') - 10*86400 WHERE id = ?", (aid,),
        )
    out = sales_why_handler(db, {"naam": "A"})
    assert out["ok"] is True
    assert out["reason_code"] == "urgent_offerte"


def test_tool_search(db: Path) -> None:
    sales_account_add_handler(db, {
        "naam": "Heineken Holding",
        "target": "adl_video",
        "contact_email": "anne@heineken.nl",
    })
    out = sales_account_search_handler(db, {"query": "anne"})
    assert out["shown"] == 1


def test_tool_history_returns_recent(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
    sales_touchpoint_log_handler(db, {"naam": "X", "channel": "email_out"})
    sales_touchpoint_log_handler(db, {"naam": "X", "channel": "linkedin"})
    out = sales_touchpoint_history_handler(db, {"naam": "X"})
    assert out["ok"] is True
    assert len(out["touchpoints"]) == 2


# ---- briefing integration --------------------------------------------

def test_build_sales_pulse_returns_shape(db: Path) -> None:
    sales_account_add_handler(db, {"naam": "A", "target": "adl_video"})
    pulse = build_sales_pulse(db)
    assert "top_three" in pulse
    assert "pipeline_snapshot" in pulse


def test_build_sales_pulse_marks_trigger_consumed(db: Path) -> None:
    """Trigger gisteren binnengekomen → consumed na build_sales_pulse."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="dst_connect",
                              status="nurturing")
        cur = conn.execute(
            "INSERT INTO sales_triggers "
            "(account_id, source, source_ref, occurred_at, title) "
            "VALUES (?, 'tender', 'tnd:99', strftime('%s','now'), 'X')",
            (aid,),
        )
        trigger_id = cur.lastrowid
    build_sales_pulse(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM sales_triggers WHERE id = ?",
            (trigger_id,),
        ).fetchone()
    assert row[0] is not None


# ---- registry --------------------------------------------------------

# ---- review-ronde fixes ----------------------------------------------

def test_h3_compute_sales_pulse_is_pure_read(db: Path) -> None:
    """H3: compute_sales_pulse mag triggers NIET consumeren — caller
    beslist of record_briefing_served wordt aangeroepen."""
    from extensions.sales.briefing import compute_sales_pulse

    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="dst_connect",
                              status="nurturing")
        cur = conn.execute(
            "INSERT INTO sales_triggers "
            "(account_id, source, source_ref, occurred_at, title) "
            "VALUES (?, 'tender', 'tnd:42', strftime('%s','now'), 'X')",
            (aid,),
        )
        trigger_id = cur.lastrowid

    pulse, ids = compute_sales_pulse(db)
    assert trigger_id in ids
    # Trigger nog NIET geconsumeerd
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM sales_triggers WHERE id = ?",
            (trigger_id,),
        ).fetchone()
    assert row[0] is None

    # record_briefing_served verbruikt 'm wel
    from extensions.sales.briefing import record_briefing_served
    record_briefing_served(db, ids)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM sales_triggers WHERE id = ?",
            (trigger_id,),
        ).fetchone()
    assert row[0] is not None


def test_h4_audit_log_on_account_add(db: Path, tmp_path: Path) -> None:
    """H4: account-CRUD acties moeten in admin-audit-stream landen."""
    from core.audit import AdminActionLogger, bind_admin_logger

    audit_dir = tmp_path / "audit"
    bind_admin_logger(AdminActionLogger(audit_dir))
    try:
        sales_account_add_handler(
            db, {"naam": "TestCo", "target": "adl_video"},
            actor="imessage",
        )
        files = list(audit_dir.glob("admin-*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "sales_account_add" in content
        assert "imessage" in content
        assert "TestCo" in content
    finally:
        import core.audit
        core.audit._admin = None


def test_h4_audit_log_on_status_change(db: Path, tmp_path: Path) -> None:
    from core.audit import AdminActionLogger, bind_admin_logger

    audit_dir = tmp_path / "audit"
    bind_admin_logger(AdminActionLogger(audit_dir))
    try:
        sales_account_add_handler(db, {"naam": "X", "target": "adl_video"})
        sales_account_set_status_handler(
            db, {"naam": "X", "status": "kansrijk"},
            actor="imessage",
        )
        content = (audit_dir / next(iter(
            f.name for f in audit_dir.glob("admin-*.jsonl")
        ))).read_text()
        assert "sales_account_update_status_or_target" in content
    finally:
        import core.audit
        core.audit._admin = None


def test_m3_prune_inactive_cold_accounts(db: Path) -> None:
    """M3: koud-account zonder touchpoints + ouder dan threshold → weg."""
    from extensions.sales.schema import prune_inactive_cold_accounts

    with sqlite3.connect(db, isolation_level=None) as conn:
        old_cold = insert_account(conn, naam="OldCold",
                                    target="adl_video", status="koud")
        # Backdate created_at + last_touch_at
        conn.execute(
            "UPDATE sales_accounts SET created_at = "
            "strftime('%s','now') - 800 * 86400, last_touch_at = NULL "
            "WHERE id = ?", (old_cold,),
        )
        recent_cold = insert_account(conn, naam="RecentCold",
                                       target="adl_video", status="koud")
        nurturing = insert_account(conn, naam="Active",
                                     target="adl_video", status="nurturing")
        # Status='nurturing' wordt nooit gepruned, ongeacht ouderdom
        conn.execute(
            "UPDATE sales_accounts SET created_at = "
            "strftime('%s','now') - 1000 * 86400 WHERE id = ?",
            (nurturing,),
        )

        removed, ids = prune_inactive_cold_accounts(
            conn, days_since_last_touch=730,
        )
    assert removed == 1
    assert old_cold in ids
    with sqlite3.connect(db) as conn:
        remaining = [r[0] for r in conn.execute(
            "SELECT id FROM sales_accounts ORDER BY id"
        ).fetchall()]
    assert old_cold not in remaining
    assert recent_cold in remaining
    assert nurturing in remaining


def test_m4_forget_account_hard_deletes(db: Path) -> None:
    """M4: forget hard-deletet account + cascade naar touchpoints."""
    sales_account_add_handler(db, {"naam": "ToForget", "target": "adl_video"})
    sales_touchpoint_log_handler(db, {"naam": "ToForget", "channel": "email_out"})
    # Niet bevestigd → weigerd
    out_no = SALES_HANDLERS["sales_account_forget"](
        db, {"naam": "ToForget"}, actor="imessage",
    )
    assert out_no["ok"] is False
    # Met confirm=true → weg
    out = SALES_HANDLERS["sales_account_forget"](
        db, {"naam": "ToForget", "confirm": True, "reason": "AVG-verzoek"},
        actor="imessage",
    )
    assert out["ok"] is True
    with sqlite3.connect(db) as conn:
        n_acc = conn.execute(
            "SELECT COUNT(*) FROM sales_accounts"
        ).fetchone()[0]
        n_tp = conn.execute(
            "SELECT COUNT(*) FROM sales_touchpoints"
        ).fetchone()[0]
    assert n_acc == 0
    assert n_tp == 0


def test_m4_forget_logs_audit_metadata_without_pii(
    db: Path, tmp_path: Path,
) -> None:
    """M4: audit-log behoudt 'id+naam+kvk+email' fingerprint zodat we
    AANTONEN dat we deletten, zonder verdere PII te kopiëren."""
    from core.audit import AdminActionLogger, bind_admin_logger

    audit_dir = tmp_path / "audit"
    bind_admin_logger(AdminActionLogger(audit_dir))
    try:
        sales_account_add_handler(db, {
            "naam": "Heineken", "target": "adl_video",
            "kvk": "12345678",
            "contact_email": "anne@heineken.nl",
        })
        SALES_HANDLERS["sales_account_forget"](
            db, {"naam": "Heineken", "confirm": True,
                  "reason": "AVG art. 17"},
            actor="imessage",
        )
        content = (audit_dir / next(iter(
            f.name for f in audit_dir.glob("admin-*.jsonl")
        ))).read_text()
        assert "sales_account_forget" in content
        assert "AVG" in content
        assert "12345678" in content  # KvK in fingerprint
    finally:
        import core.audit
        core.audit._admin = None


def test_all_tools_registered_consistently() -> None:
    schemas = {s["name"] for s in SALES_TOOL_SCHEMAS}
    handlers = set(SALES_HANDLERS.keys())
    assert schemas == handlers
    assert "sales_top3_today" in schemas
    assert "sales_account_add" in schemas
