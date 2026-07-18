"""Tests voor tools (incl. watchlist CRUD), worker tick + alerts."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from extensions.insolvencies.alerts import format_alert
from extensions.insolvencies.feed import InsolvencyItem
from extensions.insolvencies.matcher import DEFAULT_FILTER, match
from extensions.insolvencies.schema import (
    add_to_watchlist,
    init_insolvencies_schema,
    is_kvk_on_watchlist,
)
from extensions.insolvencies.tools import (
    INSOLVENCIES_HANDLERS,
    INSOLVENCIES_TOOL_SCHEMAS,
    _validate_kvk,
    insolvencies_ignore_handler,
    insolvencies_list_recent_handler,
    insolvencies_search_handler,
    insolvencies_status_handler,
    insolvency_watchlist_add_handler,
    insolvency_watchlist_list_handler,
    insolvency_watchlist_remove_handler,
)
from extensions.insolvencies.worker import (
    InsolvenciesWorker,
    _publication_age_days,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "insolv.db"
    init_insolvencies_schema(p)
    return p


# --- KvK validation -----------------------------------------------------

def test_validate_kvk_normalizes_short_to_8_digits() -> None:
    assert _validate_kvk("123456") == "00123456"


def test_validate_kvk_strips_dots_spaces() -> None:
    assert _validate_kvk("12.345.678") == "12345678"
    assert _validate_kvk("123 456 78") == "12345678"


def test_validate_kvk_strips_non_digits_then_normalizes() -> None:
    """M2: shared normalize_kvk strip non-digits VOOR zerofill, dus
    'ABCD1234' wordt '1234' → '00001234'. Dit is bewust gedrag — feeds
    leveren KvK soms als 'KvK 12.34.56.78' of '12 34 56 78', dus
    strip-eerst is permissief tegen formatting."""
    assert _validate_kvk("ABCD1234") == "00001234"
    assert _validate_kvk("12.34.56.78") == "12345678"


def test_validate_kvk_rejects_too_many_digits() -> None:
    """Meer dan 8 cijfers (geen geldig NL KvK) → None."""
    assert _validate_kvk("123456789012") is None


def test_validate_kvk_rejects_none() -> None:
    assert _validate_kvk(None) is None
    assert _validate_kvk("") is None


# --- watchlist tools ---------------------------------------------------

def test_watchlist_add_and_list(db: Path) -> None:
    out = insolvency_watchlist_add_handler(db, {
        "kvk": "77223764",
        "naam_hint": "Ruitech",
        "relation": "klant",
    })
    assert out["ok"] is True
    assert out["added"] is True
    assert out["kvk"] == "77223764"

    listed = insolvency_watchlist_list_handler(db, {})
    assert listed["count"] == 1
    assert listed["watchlist"][0]["kvk"] == "77223764"
    assert listed["watchlist"][0]["naam_hint"] == "Ruitech"
    assert listed["watchlist"][0]["relation"] == "klant"


def test_watchlist_add_duplicate_returns_added_false(db: Path) -> None:
    insolvency_watchlist_add_handler(db, {"kvk": "12345678"})
    out = insolvency_watchlist_add_handler(db, {"kvk": "12345678"})
    assert out["ok"] is True
    assert out["added"] is False


def test_watchlist_add_invalid_kvk(db: Path) -> None:
    out = insolvency_watchlist_add_handler(db, {"kvk": "not-a-number"})
    assert out["ok"] is False


def test_watchlist_add_invalid_relation_becomes_other(db: Path) -> None:
    out = insolvency_watchlist_add_handler(db, {
        "kvk": "11111111", "relation": "vriend"
    })
    assert out["ok"] is True
    assert out["relation"] == "other"


def test_watchlist_remove(db: Path) -> None:
    insolvency_watchlist_add_handler(db, {"kvk": "12345678"})
    out = insolvency_watchlist_remove_handler(db, {"kvk": "12345678"})
    assert out["ok"] is True
    listed = insolvency_watchlist_list_handler(db, {})
    assert listed["count"] == 0


def test_watchlist_remove_nonexistent(db: Path) -> None:
    out = insolvency_watchlist_remove_handler(db, {"kvk": "00000000"})
    assert out["ok"] is False


# --- list/search/ignore/status -----------------------------------------

def _seed(conn: sqlite3.Connection, *,
           link: str = "http://x", naam: str = "Test BV",
           kvk: str | None = None, matched: bool = True,
           alerted: bool = False, ignored: bool = False,
           plaats: str | None = "Tilburg",
           hoofd_activiteit: str | None = "",
           status: str = "Faillissement") -> None:
    conn.execute(
        """INSERT INTO insolvencies
           (link, naam, kvk, plaats, hoofd_activiteit, status, raw_description,
            pub_date, pub_at_unix, matched, matched_layers, matched_terms,
            fetched_at, alerted_at, ignored_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (link, naam, kvk, plaats, hoofd_activiteit, status, "",
         "Wed, 03 Jun 2026 00:00:00 GMT", int(time.time()),
         1 if matched else 0, "[]", "[]",
         int(time.time()),
         int(time.time()) if alerted else None,
         int(time.time()) if ignored else None),
    )


def test_list_recent_default_only_matched(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", matched=True)
        _seed(conn, link="http://b", matched=False)
    out = insolvencies_list_recent_handler(db, {})
    assert out["shown"] == 1
    assert out["insolvencies"][0]["link"] == "http://a"


def test_list_recent_excludes_ignored_by_default(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", matched=True, ignored=True)
        _seed(conn, link="http://b", matched=True)
    out = insolvencies_list_recent_handler(db, {})
    links = [i["link"] for i in out["insolvencies"]]
    assert "http://a" not in links
    assert "http://b" in links


def test_search_finds_by_naam(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", naam="Ruitech Solutions B.V.")
    out = insolvencies_search_handler(db, {"query": "Ruitech"})
    assert out["shown"] == 1


def test_search_finds_by_kvk(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", naam="X", kvk="77223764")
    out = insolvencies_search_handler(db, {"query": "77223764"})
    assert out["shown"] == 1


def test_search_query_too_short(db: Path) -> None:
    out = insolvencies_search_handler(db, {"query": "x"})
    assert out["ok"] is False


def test_ignore_marks_row(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", naam="X")
    out = insolvencies_ignore_handler(db, {"link": "http://a", "reason": "geen klant"})
    assert out["ok"] is True
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT ignored_at, notes FROM insolvencies WHERE link=?",
            ("http://a",),
        ).fetchone()
    assert row[0] is not None
    assert "geen klant" in row[1]


def test_status_handler_counts(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://a", matched=True, alerted=True)
        _seed(conn, link="http://b", matched=False)
        add_to_watchlist(conn, kvk="11111111")
    out = insolvencies_status_handler(db, {})
    assert out["total_in_db"] == 2
    assert out["matched_total"] == 1
    assert out["alerted_today"] == 1
    assert out["watchlist_size"] == 1


# --- alerts format ------------------------------------------------------

def test_alert_format_has_essentials() -> None:
    item = InsolvencyItem(
        link="http://example/123",
        naam="Pegasus Signage B.V.",
        pub_date="Wed, 03 Jun 2026 00:00:00 GMT",
        description_raw="",
        kvk="12345678",
        plaats="Tilburg",
        provincie="Noord-Brabant",
        curator="mr Test",
        status="Faillissement",
        hoofd_activiteit="reclamebranche",
    )
    with sqlite3.connect(":memory:") as conn:
        init_insolvencies_schema(Path(":memory:"))
    # Build a minimal MatchResult
    from extensions.insolvencies.matcher import MatchResult
    r = MatchResult(
        matched=True, layers=("name",),
        terms=("signage",),
        per_layer_terms=(("name", ("signage",)),),
    )
    text = format_alert(item, r)
    assert "Pegasus Signage B.V." in text
    assert "Tilburg" in text
    assert "Noord-Brabant" in text
    assert "12345678" in text
    assert "mr Test" in text
    assert "Trigger" in text
    assert "naam=signage" in text
    assert "http://example/123" in text


def test_alert_format_emoji_by_status() -> None:
    from extensions.insolvencies.matcher import MatchResult
    r = MatchResult(matched=True, layers=("name",), terms=("x",),
                     per_layer_terms=(("name", ("x",)),))
    failliet = InsolvencyItem(link="x", naam="X", pub_date="",
                               description_raw="",
                               status="Faillissement")
    surse = InsolvencyItem(link="x", naam="X", pub_date="",
                            description_raw="",
                            status="Surseance")
    assert format_alert(failliet, r).startswith("🔴")
    assert format_alert(surse, r).startswith("🟠")


# --- worker ------------------------------------------------------------

def _make_worker(db: Path, *, send=None) -> InsolvenciesWorker:
    return InsolvenciesWorker(
        db_path=db, stop_event=threading.Event(),
        send_imessage=send or (lambda h, t: None),
        primary_handle="test@me",
        poll_interval_seconds=60,
    )


def test_publication_age_days_recent() -> None:
    recent = format_datetime(datetime.now(UTC) - timedelta(hours=2))
    age = _publication_age_days(recent)
    assert 0.0 < age < 0.5


def test_publication_age_days_old() -> None:
    old = format_datetime(datetime.now(UTC) - timedelta(days=10))
    age = _publication_age_days(old)
    assert age > 9.0


def test_publication_age_days_garbage_returns_zero() -> None:
    assert _publication_age_days(None) == 0.0
    assert _publication_age_days("not a date") == 0.0


def test_worker_tick_inserts_and_alerts(db: Path) -> None:
    """Volledige flow met mocked feed: 1 matched recent + 1 unmatched."""
    sent: list[tuple[str, str]] = []
    worker = _make_worker(db, send=lambda h, t: sent.append((h, t)))

    recent_pubdate = format_datetime(datetime.now(UTC))
    matched_item = InsolvencyItem(
        link="http://example/match",
        naam="Pegasus Signage Holding B.V.",
        pub_date=recent_pubdate,
        description_raw="",
        kvk="11111111",
        plaats="Eindhoven", provincie="Noord-Brabant",
        status="Faillissement", curator="mr X",
        hoofd_activiteit="financiële holdings",
    )
    unmatched_item = InsolvencyItem(
        link="http://example/miss",
        naam="Boerderij Holding B.V.",
        pub_date=recent_pubdate,
        description_raw="",
        kvk="22222222",
        plaats="Drenthe", status="Faillissement",
        hoofd_activiteit="akkerbouw",
    )
    with patch("extensions.insolvencies.worker.fetch_and_parse") as fetch:
        fetch.return_value = [matched_item, unmatched_item]
        worker._tick_once()

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT link, matched, alerted_at FROM insolvencies "
            "ORDER BY link"
        ).fetchall()
    # Both in DB
    assert len(rows) == 2
    matched_row = next(r for r in rows if r[0].endswith("/match"))
    miss_row = next(r for r in rows if r[0].endswith("/miss"))
    assert matched_row[1] == 1
    assert matched_row[2] is not None
    assert miss_row[1] == 0
    assert miss_row[2] is None
    # One iMessage sent
    assert len(sent) == 1
    assert "Pegasus Signage" in sent[0][1]


def test_worker_skips_old_publication(db: Path) -> None:
    """Backfill-bescherming: pub_date 10 dagen oud → opslag wel, alert niet."""
    sent: list = []
    worker = _make_worker(db, send=lambda h, t: sent.append((h, t)))
    old_pubdate = format_datetime(datetime.now(UTC) - timedelta(days=10))
    item = InsolvencyItem(
        link="http://example/old",
        naam="Signage Holding BV",
        pub_date=old_pubdate,
        description_raw="",
        kvk="33333333", status="Faillissement",
    )
    with patch("extensions.insolvencies.worker.fetch_and_parse") as fetch:
        fetch.return_value = [item]
        worker._tick_once()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT alerted_at FROM insolvencies WHERE link=?",
            ("http://example/old",),
        ).fetchone()
    assert row[0] is None
    assert sent == []


def test_worker_dedupes(db: Path) -> None:
    """Item dat al in DB staat → niet opnieuw verwerkt."""
    sent: list = []
    worker = _make_worker(db, send=lambda h, t: sent.append((h, t)))
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://example/seen", matched=True, alerted=True)
    recent_pubdate = format_datetime(datetime.now(UTC))
    item = InsolvencyItem(
        link="http://example/seen",
        naam="Already seen",
        pub_date=recent_pubdate,
        description_raw="",
    )
    with patch("extensions.insolvencies.worker.fetch_and_parse") as fetch:
        fetch.return_value = [item]
        worker._tick_once()
    assert sent == []


# --- tool registry -----------------------------------------------------

# --- review-fixes -------------------------------------------------------

def test_h1_alert_send_outside_db_connection(db: Path) -> None:
    """H1: tijdens send_imessage moet de DB schrijfbaar zijn voor andere
    writers. We mocken send met een korte sleep en proberen ondertussen
    in dezelfde DB te schrijven vanuit de hoofdthread."""
    import threading as _t

    started = _t.Event()
    can_write_during_send = []

    def slow_send(h, t):
        started.set()
        # Tijdens de send: probeer een schrijfopdracht in dezelfde DB
        try:
            with sqlite3.connect(db, isolation_level=None, timeout=2) as c:
                c.execute(
                    "INSERT INTO insolvencies "
                    "(link, naam, pub_at_unix) "
                    "VALUES ('probe', 'probe', 0)"
                )
            can_write_during_send.append(True)
        except sqlite3.OperationalError as e:
            can_write_during_send.append(f"locked: {e}")

    worker = _make_worker(db, send=slow_send)
    recent_pubdate = format_datetime(datetime.now(UTC))
    item = InsolvencyItem(
        link="http://example/match",
        naam="Pegasus Signage Holding B.V.",
        pub_date=recent_pubdate, description_raw="",
        kvk="44444444", status="Faillissement",
    )
    with patch("extensions.insolvencies.worker.fetch_and_parse") as fetch:
        fetch.return_value = [item]
        worker._tick_once()

    assert can_write_during_send == [True]


def test_h2_ignore_marks_kvk_for_future_suppression(db: Path) -> None:
    """H2: insolvencies_ignore voegt KvK toe aan ignored_kvks zodat een
    rectificatie van hetzelfde bedrijf later niet opnieuw alerteert."""
    from extensions.insolvencies.schema import is_kvk_ignored

    # Seed eerste publicatie
    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://example/p1", naam="X",
              kvk="55555555", matched=True, alerted=True)

    out = insolvencies_ignore_handler(db, {
        "link": "http://example/p1",
        "reason": "is geen klant",
    })
    assert out["ok"] is True
    assert out["future_alerts_suppressed_for_kvk"] is True
    assert out["kvk_newly_added_to_ignore_list"] is True

    # KvK staat nu op ignored_kvks
    with sqlite3.connect(db) as conn:
        assert is_kvk_ignored(conn, "55555555") is True


def test_h2_ignore_opt_out_keeps_future_alerts(db: Path) -> None:
    """Hendrik kan suppress_future_for_kvk=False meegeven als hij
    alleen DEZE publicatie wil markeren."""
    from extensions.insolvencies.schema import is_kvk_ignored

    with sqlite3.connect(db, isolation_level=None) as conn:
        _seed(conn, link="http://example/q1", naam="Y",
              kvk="66666666", matched=True)

    out = insolvencies_ignore_handler(db, {
        "link": "http://example/q1",
        "suppress_future_for_kvk": False,
    })
    assert out["ok"] is True
    assert out["future_alerts_suppressed_for_kvk"] is False
    with sqlite3.connect(db) as conn:
        assert is_kvk_ignored(conn, "66666666") is False


def test_h2_matcher_skips_ignored_kvk(db: Path) -> None:
    """End-to-end: KvK op ignored_kvks → matcher returnt matched=False
    zelfs als andere lagen zouden triggeren."""
    from extensions.insolvencies.schema import add_to_ignored_kvks

    with sqlite3.connect(db, isolation_level=None) as conn:
        add_to_ignored_kvks(conn, kvk="77777777", reason="test")
        add_to_watchlist(conn, kvk="77777777", naam_hint="vroeger op watchlist")

    item = InsolvencyItem(
        link="http://example/ignored", naam="Pegasus Signage",
        pub_date="", description_raw="",
        kvk="77777777",
        hoofd_activiteit="audiovisuele installatie",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is False


def test_h4_sort_uses_unix_not_lexicographic(db: Path) -> None:
    """H4: pub_at_unix opslaan zodat ORDER BY chronologisch werkt.
    Anders zou 'Fri, 30 May 2026' lexicografisch na 'Wed, 03 Jun 2026'
    komen — verkeerd."""
    from datetime import datetime as _dt
    with sqlite3.connect(db, isolation_level=None) as conn:
        # Drie items met expliciete unix-times
        for link, name, unix in [
            ("http://a", "Oudste",  int(_dt(2026, 5, 30).timestamp())),
            ("http://b", "Nieuwst", int(_dt(2026, 6, 3).timestamp())),
            ("http://c", "Tussen",  int(_dt(2026, 6, 1).timestamp())),
        ]:
            conn.execute(
                "INSERT INTO insolvencies "
                "(link, naam, pub_at_unix, matched, fetched_at) "
                "VALUES (?, ?, ?, 1, strftime('%s','now'))",
                (link, name, unix),
            )
    out = insolvencies_list_recent_handler(db, {"days": 365})
    names = [i["naam"] for i in out["insolvencies"]]
    assert names == ["Nieuwst", "Tussen", "Oudste"]


def test_m1_search_respects_days_filter(db: Path) -> None:
    """M1: search default 365d, items ouder vallen erbuiten."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        # Item van vandaag
        _seed(conn, link="http://recent", naam="Signage Holding BV")
        # Item van 500 dagen geleden — handmatig fetched_at terugzetten
        _seed(conn, link="http://old", naam="Signage Holding BV (oud)")
        conn.execute(
            "UPDATE insolvencies SET fetched_at = "
            "strftime('%s','now') - 500 * 86400 WHERE link = ?",
            ("http://old",),
        )
    out = insolvencies_search_handler(db, {"query": "Signage"})
    links = [i["link"] for i in out["insolvencies"]]
    assert "http://recent" in links
    assert "http://old" not in links
    # Met expliciet langer venster wel
    out2 = insolvencies_search_handler(db, {"query": "Signage", "days": 1000})
    links2 = [i["link"] for i in out2["insolvencies"]]
    assert "http://old" in links2


def test_m2_watchlist_finds_kvk_across_normalizations(db: Path) -> None:
    """M2: watchlist '12345678' moet hits geven voor feed-waarde
    '12345678', '00012345' moet '12345' vinden. is_kvk_on_watchlist
    normaliseert beide kanten."""

    insolvency_watchlist_add_handler(db, {"kvk": "12345"})  # 5 chars → '00012345'
    with sqlite3.connect(db) as conn:
        assert is_kvk_on_watchlist(conn, "00012345") is True
        assert is_kvk_on_watchlist(conn, "12345") is True
        assert is_kvk_on_watchlist(conn, "12.34.5") is True
        assert is_kvk_on_watchlist(conn, "99999999") is False


def test_all_tools_registered_consistently() -> None:
    schemas = {s["name"] for s in INSOLVENCIES_TOOL_SCHEMAS}
    handlers = set(INSOLVENCIES_HANDLERS)
    assert schemas == handlers
    assert "insolvency_watchlist_add" in schemas
    assert "insolvency_watchlist_remove" in schemas
    assert "insolvency_watchlist_list" in schemas
    assert "insolvencies_list_recent" in schemas
    assert "insolvencies_search" in schemas
    assert "insolvencies_ignore" in schemas
    assert "insolvencies_status" in schemas
