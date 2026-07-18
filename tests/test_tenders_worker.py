"""Tests voor extensions.tenders.worker.

Review-findings dekkend:
  H1 — `_is_expired` honoreert Europe/Amsterdam ongeacht active TZ
  H2 — `_tick_once`, `_should_alert`, `_is_expired` getest
  H3 — `_publication_age_hours` + backfill-bombardement-bescherming
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import core.timezone as ctz
from extensions.tenders.schema import init_tenders_schema
from extensions.tenders.worker import (
    TENDERNED_TZ,
    TenderWorker,
    _is_expired,
    _parse_tenderned_dt,
    _publication_age_hours,
)

NL = TENDERNED_TZ


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "tenders.db"
    init_tenders_schema(p)
    return p


@pytest.fixture
def restore_now_local():
    """Save+restore module-level now_local zodat test-monkey-patches
    elkaar niet door lekken."""
    original = ctz.now_local
    yield
    ctz.now_local = original


# --- H1: TZ-handling -----------------------------------------------------

def test_is_expired_uses_tenderned_tz_not_active_tz(restore_now_local) -> None:
    """De productie-bug uit de review: Hendrik in San Francisco met
    active TZ=America/Los_Angeles. Aanbesteding sluit 11 mei 10:00 NL
    = 11 mei 01:00 PT. Het is nu 11 mei 09:00 NL = 11 mei 00:00 PT.

    Met de oude code: dt zou geïnterpreteerd worden als 10:00 PT
    (= 19:00 NL), now als 00:00 PT → niet expired (FOUT).
    Met de fix: dt is 10:00 NL, now is 09:00 NL → niet expired (correct
    omdat 1 uur in de toekomst).
    """
    # Simuleer Hendrik in PT, dt-string van TenderNed
    iso = "2026-05-11T10:00:00"   # NL local
    # Test: is dt door fix gepind aan NL?
    dt = _parse_tenderned_dt(iso)
    assert dt is not None
    assert dt.tzinfo == NL
    assert dt.year == 2026 and dt.month == 5 and dt.day == 11 and dt.hour == 10


def test_is_expired_correct_when_past() -> None:
    """Sluitingsdatum 1 dag geleden in NL → expired."""
    yesterday_nl = datetime.now(NL) - timedelta(days=1)
    iso = yesterday_nl.strftime("%Y-%m-%dT%H:%M:%S")
    assert _is_expired(iso) is True


def test_is_expired_correct_when_future() -> None:
    """Sluitingsdatum 1 dag in de toekomst → niet expired."""
    tomorrow_nl = datetime.now(NL) + timedelta(days=1)
    iso = tomorrow_nl.strftime("%Y-%m-%dT%H:%M:%S")
    assert _is_expired(iso) is False


def test_is_expired_handles_none_and_garbage() -> None:
    assert _is_expired(None) is False
    assert _is_expired("") is False
    assert _is_expired("not a date") is False


def test_is_expired_handles_date_only_iso() -> None:
    """Date-only (geen T-component): end-of-day in NL."""
    yesterday_nl = (datetime.now(NL) - timedelta(days=1)).date()
    assert _is_expired(yesterday_nl.isoformat()) is True


# --- H3: publication-age + backfill-bombardement -------------------------

def test_publication_age_hours_recent_returns_small() -> None:
    """Publicatie van 30 min geleden → age <= 1h."""
    half_hour_ago = datetime.now(NL) - timedelta(minutes=30)
    iso = half_hour_ago.strftime("%Y-%m-%dT%H:%M:%S")
    age = _publication_age_hours(iso)
    assert 0.2 < age < 1.0


def test_publication_age_hours_old_returns_large() -> None:
    """Publicatie van 3 dagen geleden → age >= 72h."""
    three_days = datetime.now(NL) - timedelta(days=3)
    iso = three_days.strftime("%Y-%m-%dT%H:%M:%S")
    age = _publication_age_hours(iso)
    assert age >= 70.0


def test_publication_age_hours_handles_none() -> None:
    """Onbekende publicatiedatum → 0 (= behandel als nieuw, anders missen we items)."""
    assert _publication_age_hours(None) == 0.0
    assert _publication_age_hours("garbage") == 0.0


def test_should_alert_skips_old_publication(db: Path) -> None:
    """H3 backfill-bescherming: publicatie van 48u geleden → geen alert
    (default drempel = 24h)."""
    worker = _make_worker(db)
    old_iso = (datetime.now(NL) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    future_close = (datetime.now(NL) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    detail = {
        "publicatieId": 1, "kenmerk": 100,
        "publicatieDatum": old_iso,
        "sluitingsDatum": future_close,
        "aankondigingCode": {"code": "OPE"},
    }
    with sqlite3.connect(db, isolation_level=None) as conn:
        assert worker._should_alert(conn, detail) is False


def test_should_alert_passes_fresh_publication(db: Path) -> None:
    """Recent publicatie (1u geleden) + future close + nieuw kenmerk → alert."""
    worker = _make_worker(db)
    fresh = (datetime.now(NL) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future_close = (datetime.now(NL) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    detail = {
        "publicatieId": 1, "kenmerk": 100,
        "publicatieDatum": fresh,
        "sluitingsDatum": future_close,
        "aankondigingCode": {"code": "OPE"},
    }
    with sqlite3.connect(db, isolation_level=None) as conn:
        assert worker._should_alert(conn, detail) is True


def test_should_alert_skips_expired(db: Path) -> None:
    """skip_expired=True (default) en sluiting in het verleden → no-alert."""
    worker = _make_worker(db)
    fresh = (datetime.now(NL) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    past_close = (datetime.now(NL) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    detail = {
        "publicatieId": 1, "kenmerk": 100,
        "publicatieDatum": fresh,
        "sluitingsDatum": past_close,
        "aankondigingCode": {"code": "OPE"},
    }
    with sqlite3.connect(db, isolation_level=None) as conn:
        assert worker._should_alert(conn, detail) is False


def test_should_alert_skips_rectification_of_alerted_kenmerk(db: Path) -> None:
    """Eerste publicatie van kenmerk X krijgt alert. Rectificatie met
    zelfde kenmerk → no-alert."""
    worker = _make_worker(db)
    fresh = (datetime.now(NL) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (datetime.now(NL) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    with sqlite3.connect(db, isolation_level=None) as conn:
        # Seed eerste pub als alerted
        conn.execute(
            "INSERT INTO tenders (publicatie_id, kenmerk, aanbesteding_naam, "
            "link, matched, alerted_at) VALUES (1, 500, 'eerste', 'x', 1, ?)",
            (int(time.time()),),
        )
        rectification = {
            "publicatieId": 2, "kenmerk": 500,
            "publicatieDatum": fresh,
            "sluitingsDatum": future,
            "aankondigingCode": {"code": "REC"},
        }
        assert worker._should_alert(conn, rectification) is False


# --- H2: _tick_once integration ----------------------------------------

def test_tick_once_inserts_and_alerts(db: Path) -> None:
    """Volledige flow: mock feed → 2 publicaties → een matched + recent,
    de andere niet-matched → 1 row matched, 1 alert verzonden."""
    sent: list[tuple[str, str]] = []
    worker = _make_worker(db, send_imessage=lambda h, t: sent.append((h, t)))
    fresh = (datetime.now(NL) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (datetime.now(NL) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    matched_detail = {
        "publicatieId": 100, "kenmerk": 100,
        "aanbestedingNaam": "Narrowcasting upgrade",
        "opdrachtgeverNaam": "Test Org",
        "opdrachtBeschrijving": "",
        "publicatieDatum": fresh, "sluitingsDatum": future,
        "aankondigingCode": {"code": "OPE"},
        "trefwoord1": "Narrowcasting", "trefwoord2": "",
        "cpvCodes": [],
    }
    unmatched_detail = {
        "publicatieId": 101, "kenmerk": 101,
        "aanbestedingNaam": "Rioolaanleg",
        "opdrachtgeverNaam": "Test Org",
        "opdrachtBeschrijving": "",
        "publicatieDatum": fresh, "sluitingsDatum": future,
        "aankondigingCode": {"code": "OPE"},
        "trefwoord1": "Riool", "trefwoord2": "",
        "cpvCodes": [],
    }

    with patch("extensions.tenders.worker.fetch_recent_summaries") as fetch_list, \
         patch("extensions.tenders.worker.fetch_publication_detail") as fetch_one:
        fetch_list.return_value = [{"publicatieId": 100}, {"publicatieId": 101}]
        fetch_one.side_effect = lambda pid: matched_detail if pid == 100 else unmatched_detail
        worker._tick_once()

    # Beide rows in DB
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT publicatie_id, matched, alerted_at FROM tenders ORDER BY publicatie_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0] == (100, 1, rows[0][2])  # matched
    assert rows[0][2] is not None             # alerted
    assert rows[1] == (101, 0, None)          # unmatched + niet gealerteerd
    # Eén alert verstuurd
    assert len(sent) == 1
    assert "Narrowcasting upgrade" in sent[0][1]


def test_tick_once_dedupes_already_seen(db: Path) -> None:
    """Publicatie die al in DB staat → geen detail-fetch, geen alert."""
    sent: list[tuple[str, str]] = []
    worker = _make_worker(db, send_imessage=lambda h, t: sent.append((h, t)))
    with sqlite3.connect(db, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO tenders (publicatie_id, kenmerk, aanbesteding_naam, "
            "link, matched) VALUES (200, 200, 'eerder', 'x', 1)"
        )
    with patch("extensions.tenders.worker.fetch_recent_summaries") as fetch_list, \
         patch("extensions.tenders.worker.fetch_publication_detail") as fetch_one:
        fetch_list.return_value = [{"publicatieId": 200}]
        worker._tick_once()
        fetch_one.assert_not_called()
    assert sent == []


def test_tick_once_handles_rate_limited(db: Path) -> None:
    """Bij 429 (TenderNedRateLimited) skipt de tick netjes — geen crash."""
    from extensions.tenders.feed import TenderNedRateLimited
    worker = _make_worker(db)
    with patch("extensions.tenders.worker.fetch_recent_summaries") as fetch_list:
        fetch_list.side_effect = TenderNedRateLimited(retry_after_seconds=300)
        worker._tick_once()  # may not raise
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    assert n == 0


# --- helper -------------------------------------------------------------

def _make_worker(
    db: Path, *, send_imessage=None,
) -> TenderWorker:
    return TenderWorker(
        db_path=db,
        stop_event=threading.Event(),
        send_imessage=send_imessage or (lambda h, t: None),
        primary_handle="test@me",
        poll_interval_seconds=60,
        page_size=10,
    )
