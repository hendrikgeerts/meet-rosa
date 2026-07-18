"""Tests voor extensions.sales.nudges — morgen/middag/avond reminders."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import core.timezone as ctz
from extensions.sales.nudges import (
    build_evening_nudge, build_midday_nudge, build_morning_nudge,
    count_outbound_today,
)
from extensions.sales.schema import init_sales_schema
from extensions.sales.storage import (
    insert_account, insert_touchpoint, update_account,
)


TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "sales.db"
    init_sales_schema(p)
    return p


@pytest.fixture
def restore_now_local():
    original = ctz.now_local
    yield
    ctz.now_local = original


# ---- count_outbound_today --------------------------------------------

def test_count_outbound_today_empty(db: Path, restore_now_local) -> None:
    ctz.now_local = lambda: datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    n, contacted = count_outbound_today(db)
    assert n == 0
    assert contacted == []


def test_count_outbound_today_counts_distinct_accounts(
    db: Path, restore_now_local,
) -> None:
    """Twee touchpoints op zelfde dag op zelfde account = 1, niet 2."""
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning_unix = int(fake_now.replace(hour=10).timestamp())
    afternoon_unix = int(fake_now.replace(hour=13).timestamp())

    with sqlite3.connect(db, isolation_level=None) as conn:
        a1 = insert_account(conn, naam="A", target="adl_video")
        a2 = insert_account(conn, naam="B", target="dst_connect")
        insert_touchpoint(conn, account_id=a1, channel="email_out",
                           occurred_at_unix=morning_unix)
        insert_touchpoint(conn, account_id=a1, channel="call",
                           occurred_at_unix=afternoon_unix)
        insert_touchpoint(conn, account_id=a2, channel="linkedin",
                           occurred_at_unix=morning_unix)
    n, contacted = count_outbound_today(db)
    assert n == 2
    names = sorted(c["naam"] for c in contacted)
    assert names == ["A", "B"]


def test_count_outbound_today_skips_email_in(
    db: Path, restore_now_local,
) -> None:
    """Inbound replies tellen niet als 'ik heb iemand benaderd'."""
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())

    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="A", target="adl_video")
        insert_touchpoint(conn, account_id=aid, channel="email_in",
                           occurred_at_unix=morning)
    n, _ = count_outbound_today(db)
    assert n == 0


def test_count_outbound_today_excludes_yesterday(
    db: Path, restore_now_local,
) -> None:
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    yesterday = int((fake_now - timedelta(days=1)).timestamp())

    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="A", target="adl_video")
        insert_touchpoint(conn, account_id=aid, channel="email_out",
                           occurred_at_unix=yesterday)
    n, _ = count_outbound_today(db)
    assert n == 0


# ---- morning -----------------------------------------------------------

def test_morning_nudge_includes_target_and_suggestions(
    db: Path, restore_now_local,
) -> None:
    ctz.now_local = lambda: datetime(2026, 6, 8, 9, 0, tzinfo=TZ)
    with sqlite3.connect(db, isolation_level=None) as conn:
        insert_account(conn, naam="Heineken", target="adl_video",
                        status="kansrijk")
        insert_account(conn, naam="Jumbo", target="adl_video",
                        status="nurturing")
        insert_account(conn, naam="Mediq", target="ds_templates",
                        status="kansrijk")
        # next_touch backdate zodat ze in selectie komen
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') - 86400 WHERE status IN "
            "('kansrijk','nurturing')"
        )
    text = build_morning_nudge(db, target_count=3)
    assert "Doel vandaag: 3 bedrijven" in text
    assert "Heineken" in text
    # Targets in afkortingen
    assert "[ADL]" in text or "[DS]" in text


def test_morning_nudge_empty_pipeline(db: Path, restore_now_local) -> None:
    ctz.now_local = lambda: datetime(2026, 6, 8, 9, 0, tzinfo=TZ)
    text = build_morning_nudge(db, target_count=3)
    assert "Doel vandaag" in text
    assert "Geen kandidaten" in text


# ---- midday ------------------------------------------------------------

def test_midday_zero_contacts_shows_remaining(
    db: Path, restore_now_local,
) -> None:
    ctz.now_local = lambda: datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    with sqlite3.connect(db, isolation_level=None) as conn:
        insert_account(conn, naam="Heineken", target="adl_video",
                        status="kansrijk")
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') - 86400"
        )
    text = build_midday_nudge(db, target_count=3)
    assert "0/3" in text
    assert "Nog 3 te gaan" in text


def test_midday_partial_contacts_shows_progress(
    db: Path, restore_now_local,
) -> None:
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        a1 = insert_account(conn, naam="Heineken", target="adl_video")
        insert_touchpoint(conn, account_id=a1, channel="email_out",
                           occurred_at_unix=morning)
        # Andere kandidaat in pipeline
        a2 = insert_account(conn, naam="Jumbo", target="adl_video",
                              status="kansrijk")
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') - 86400 WHERE id = ?", (a2,),
        )
    text = build_midday_nudge(db, target_count=3)
    assert "1/3" in text
    assert "Heineken" in text       # in 'al benaderd'
    assert "Nog 2 te gaan" in text


def test_midday_goal_reached_celebrates(
    db: Path, restore_now_local,
) -> None:
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        for naam in ("A", "B", "C"):
            aid = insert_account(conn, naam=naam, target="adl_video")
            insert_touchpoint(conn, account_id=aid, channel="email_out",
                               occurred_at_unix=morning)
    text = build_midday_nudge(db, target_count=3)
    assert "✅" in text
    assert "3/3" in text


def test_midday_overdelivery_shows_bonus(
    db: Path, restore_now_local,
) -> None:
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        for naam in ("A", "B", "C", "D", "E"):
            aid = insert_account(conn, naam=naam, target="adl_video")
            insert_touchpoint(conn, account_id=aid, channel="email_out",
                               occurred_at_unix=morning)
    text = build_midday_nudge(db, target_count=3)
    assert "5/3" in text
    assert "+2 extra" in text


def test_midday_excludes_already_contacted_from_suggestions(
    db: Path, restore_now_local,
) -> None:
    """Account dat vandaag al benaderd is mag NIET als suggestie verschijnen."""
    fake_now = datetime(2026, 6, 8, 14, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        contacted = insert_account(
            conn, naam="Heineken", target="adl_video", status="kansrijk",
        )
        other = insert_account(
            conn, naam="Jumbo", target="adl_video", status="kansrijk",
        )
        insert_touchpoint(conn, account_id=contacted, channel="email_out",
                           occurred_at_unix=morning)
        conn.execute(
            "UPDATE sales_accounts SET next_touch_at = "
            "strftime('%s','now') - 86400"
        )
    text = build_midday_nudge(db, target_count=3)
    # Heineken in "al benaderd"-blok, Jumbo in suggesties.
    # Onder de "Nog X te gaan"-lijn mag Heineken niet als suggestie
    # voorkomen.
    after_remaining = text.split("Nog ", 1)[1] if "Nog " in text else ""
    assert "Heineken" not in after_remaining


# ---- evening ------------------------------------------------------------

def test_evening_goal_reached(db: Path, restore_now_local) -> None:
    fake_now = datetime(2026, 6, 8, 19, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        for naam in ("A", "B", "C"):
            aid = insert_account(conn, naam=naam, target="adl_video")
            insert_touchpoint(conn, account_id=aid, channel="email_out",
                               occurred_at_unix=morning)
    text = build_evening_nudge(db, target_count=3)
    assert "3/3" in text
    assert "Doel gehaald" in text


def test_evening_partial(db: Path, restore_now_local) -> None:
    fake_now = datetime(2026, 6, 8, 19, 0, tzinfo=TZ)
    ctz.now_local = lambda: fake_now
    morning = int(fake_now.replace(hour=10).timestamp())
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="A", target="adl_video")
        insert_touchpoint(conn, account_id=aid, channel="call",
                           occurred_at_unix=morning)
    text = build_evening_nudge(db, target_count=3)
    assert "1/3" in text
    assert "Net niet" in text
    assert "2 kort" in text


def test_evening_zero_friendly(db: Path, restore_now_local) -> None:
    ctz.now_local = lambda: datetime(2026, 6, 8, 19, 0, tzinfo=TZ)
    text = build_evening_nudge(db, target_count=3)
    assert "0/3" in text
    assert "Niemand benaderd" in text
    assert "morgen" in text.lower()
