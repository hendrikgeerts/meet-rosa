"""Tests voor alerts-format + tool-handlers."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from extensions.tenders.alerts import _fmt_date, format_alert
from extensions.tenders.matcher import DEFAULT_FILTER, MatchResult, match
from extensions.tenders.schema import (
    init_tenders_schema, kenmerk_already_alerted, prune_old_unmatched,
    tender_exists,
)
from extensions.tenders.tools import (
    TENDER_HANDLERS, TENDER_TOOL_SCHEMAS,
    tenders_ignore_handler, tenders_list_recent_handler,
    tenders_search_handler, tenders_status_handler,
)


# fixture identiek aan test_tenders_matcher import patroon
from tests.test_tenders_matcher import (
    LEIDEN_AV, NS_RECLAMEDRAGERS, ROC_NARROWCASTING,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "tenders.db"
    init_tenders_schema(p)
    return p


def _seed(conn: sqlite3.Connection, detail: dict, *, matched: bool = True,
           alerted: bool = False, ignored: bool = False,
           layers: list[str] = None, terms: list[str] = None) -> None:
    """Test-helper: insert een synthetische tender-row."""
    import json
    aank = detail.get("aankondigingCode") or {"code": "OPE"}
    conn.execute(
        """INSERT INTO tenders
           (publicatie_id, kenmerk, aanbesteding_naam, opdrachtgever_naam,
            opdracht_beschrijving, publicatie_datum, sluitings_datum,
            type_publicatie, aankondiging_code, procedure, type_opdracht,
            cpv_codes, nuts_codes, trefwoord1, trefwoord2, link,
            matched, matched_layers, matched_terms, fetched_at,
            alerted_at, ignored_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            detail["publicatieId"], detail["kenmerk"],
            detail["aanbestedingNaam"], detail["opdrachtgeverNaam"],
            detail["opdrachtBeschrijving"], "2026-06-01T10:00:00",
            "2026-12-31T17:00:00", "Aankondiging",
            aank.get("code", "OPE") if isinstance(aank, dict) else "OPE",
            "Openbaar", "D",
            json.dumps(detail.get("cpvCodes") or []),
            "[]", detail.get("trefwoord1", ""), detail.get("trefwoord2", ""),
            f"https://www.tenderned.nl/aankondigingen/overzicht/{detail['publicatieId']}",
            1 if matched else 0,
            json.dumps(layers or []), json.dumps(terms or []),
            int(time.time()),
            int(time.time()) if alerted else None,
            int(time.time()) if ignored else None,
        ),
    )


# --- alert format -------------------------------------------------------

def test_format_alert_contains_essentials() -> None:
    result = match(ROC_NARROWCASTING, DEFAULT_FILTER)
    detail = dict(ROC_NARROWCASTING)
    detail["sluitingsDatum"] = "2026-05-11T10:00:00"
    text = format_alert(detail, result)
    # Title + opdrachtgever
    assert "Narrowcastingoplossing (SaaS)" in text
    assert "ROC van Amsterdam" in text
    # Date in NL format
    assert "11 mei 2026 10:00" in text
    # Link
    assert "tenderned.nl/aankondigingen/overzicht/419614" in text
    # Trigger summary
    assert "Trigger" in text


def test_format_alert_handles_missing_optional_fields() -> None:
    detail = {
        "publicatieId": 1,
        "aanbestedingNaam": "Mystery tender",
        "opdrachtgeverNaam": "",
        "sluitingsDatum": None,
        "cpvCodes": [], "trefwoord1": "narrowcasting", "trefwoord2": "",
        "opdrachtBeschrijving": "",
    }
    result = match(detail, DEFAULT_FILTER)
    text = format_alert(detail, result)
    assert "Mystery tender" in text
    assert "Sluiting: -" in text


def test_fmt_date_variants() -> None:
    assert _fmt_date("2026-05-11T10:00:00") == "11 mei 2026 10:00"
    assert _fmt_date("2026-05-11T10:00:00.123") == "11 mei 2026 10:00"
    assert _fmt_date("2026-12-31") == "31 dec 2026"
    assert _fmt_date(None) == "-"
    assert _fmt_date("") == "-"
    assert _fmt_date("notadate") == "notadate"


# --- schema dedupe ------------------------------------------------------

def test_tender_exists_after_insert(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING)
        assert tender_exists(conn, ROC_NARROWCASTING["publicatieId"]) is True
        assert tender_exists(conn, 999999) is False


def test_kenmerk_already_alerted(db: Path) -> None:
    """Eerste publicatie van kenmerk X gealerted → tweede pub met
    zelfde kenmerk moet kenmerk_already_alerted=True geven."""
    pub1 = dict(ROC_NARROWCASTING)
    pub1["publicatieId"] = 100001
    pub1["kenmerk"] = 555000
    pub2 = dict(ROC_NARROWCASTING)
    pub2["publicatieId"] = 100002
    pub2["kenmerk"] = 555000  # zelfde keten
    pub2["aankondigingCode"] = {"code": "REC"}
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, pub1, alerted=True)
        _seed(conn, pub2, matched=True, alerted=False)
        assert kenmerk_already_alerted(conn, 555000) is True
        assert kenmerk_already_alerted(conn, 999) is False


def test_prune_old_unmatched_keeps_matched(db: Path) -> None:
    """Cleanup mag matched rows NIET weggooien — zelfs als ze oud zijn."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True)
        _seed(conn, NS_RECLAMEDRAGERS, matched=False)
        # backdate beide naar 200 dagen geleden
        conn.execute(
            "UPDATE tenders SET fetched_at = strftime('%s','now') - 200 * 86400"
        )
        removed = prune_old_unmatched(conn, days=90)
    assert removed == 1  # alleen de unmatched
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT publicatie_id FROM tenders").fetchall()
    assert (ROC_NARROWCASTING["publicatieId"],) in rows
    assert (NS_RECLAMEDRAGERS["publicatieId"],) not in rows


# --- tools ---------------------------------------------------------------

def test_tools_are_registered() -> None:
    names = {s["name"] for s in TENDER_TOOL_SCHEMAS}
    assert names == {
        "tenders_list_recent", "tenders_search",
        "tenders_ignore", "tenders_status",
    }
    assert set(TENDER_HANDLERS) == names


def test_list_recent_returns_matched_only_by_default(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True,
               layers=["trefwoord"], terms=["narrowcasting"])
        _seed(conn, NS_RECLAMEDRAGERS, matched=False)
    out = tenders_list_recent_handler(db, {"days": 30})
    assert out["ok"] is True
    assert out["shown"] == 1
    assert out["tenders"][0]["publicatie_id"] == ROC_NARROWCASTING["publicatieId"]
    assert out["tenders"][0]["matched_layers"] == ["trefwoord"]


def test_list_recent_only_matched_false_returns_all(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True)
        _seed(conn, NS_RECLAMEDRAGERS, matched=False)
    out = tenders_list_recent_handler(db, {"only_matched": False})
    assert out["shown"] == 2


def test_list_recent_excludes_ignored(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True, ignored=True)
        _seed(conn, NS_RECLAMEDRAGERS, matched=True)
    out = tenders_list_recent_handler(db, {})
    ids = [t["publicatie_id"] for t in out["tenders"]]
    assert ROC_NARROWCASTING["publicatieId"] not in ids
    assert NS_RECLAMEDRAGERS["publicatieId"] in ids
    # Met include_ignored=True wel meegenomen
    out2 = tenders_list_recent_handler(db, {"include_ignored": True})
    ids2 = [t["publicatie_id"] for t in out2["tenders"]]
    assert ROC_NARROWCASTING["publicatieId"] in ids2


def test_search_handler_finds_by_org(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True)
    out = tenders_search_handler(db, {"query": "ROC"})
    assert out["ok"] is True
    assert out["shown"] == 1


def test_search_handler_query_too_short(db: Path) -> None:
    out = tenders_search_handler(db, {"query": "x"})
    assert out["ok"] is False


def test_ignore_marks_row(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True)
    pid = ROC_NARROWCASTING["publicatieId"]
    out = tenders_ignore_handler(db, {"publicatie_id": pid, "reason": "te ver weg"})
    assert out["ok"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT ignored_at, notes FROM tenders WHERE publicatie_id = ?",
            (pid,),
        ).fetchone()
    assert row[0] is not None
    assert "te ver weg" in row[1]


def test_ignore_unknown_id(db: Path) -> None:
    out = tenders_ignore_handler(db, {"publicatie_id": 9999999})
    assert out["ok"] is False
    assert "niet bekend" in out["error"]


def test_status_handler_returns_counts(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, ROC_NARROWCASTING, matched=True, alerted=True)
        _seed(conn, NS_RECLAMEDRAGERS, matched=False)
    out = tenders_status_handler(db, {})
    assert out["ok"] is True
    assert out["total_in_db"] == 2
    assert out["matched_total"] == 1
    assert out["alerted_today"] == 1
    assert "cpv_codes" in out["filter"]
    assert len(out["filter"]["cpv_codes"]) > 0
