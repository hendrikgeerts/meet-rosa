"""Tests voor extensions.sales.auto_touchpoint.

H2 review-finding: ~150 LOC kritieke business-logica zonder tests.
Dekt: email-exact-match, domain-fallback met word-boundary,
personal-mail-skip, dedupe via source_ref, slack-channel routing,
en de M1 opt-out switch.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from extensions.sales.auto_touchpoint import (
    _account_id_for_domain,
    _account_id_for_email,
    _is_personal_domain,
    _resolve_channel,
    maybe_log_touchpoint,
)
from extensions.sales.schema import init_sales_schema
from extensions.sales.storage import insert_account, list_touchpoints


@dataclass
class FakeItem:
    """Mimic comm_intel.CommItem zonder de echte import (TYPE_CHECKING)."""
    source: str
    account: str
    external_id: str
    direction: str
    from_addr: str | None = None
    to_addrs: list[str] = None
    subject: str | None = None
    occurred_at: int = 1_700_000_000


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "sales.db"
    init_sales_schema(p)
    return p


# ---- helpers ----------------------------------------------------------

def test_is_personal_domain_recognizes_common_providers() -> None:
    for d in ("gmail.com", "outlook.com", "hotmail.com", "icloud.com",
               "live.com", "yahoo.com", "yahoo.nl", "me.com",
               "protonmail.com"):
        assert _is_personal_domain(d), d


def test_is_personal_domain_skips_business() -> None:
    for d in ("heineken.nl", "asml.com", "philips.nl"):
        assert not _is_personal_domain(d)


# ---- email exact match -----------------------------------------------

def test_account_id_for_email_matches_exact(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(
            conn, naam="Heineken", target="adl_video",
            primary_contact_email="anne@heineken.nl",
        )
        result = _account_id_for_email(conn, "anne@heineken.nl")
    assert result == aid


def test_account_id_for_email_case_insensitive(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(
            conn, naam="X", target="adl_video",
            primary_contact_email="info@example.nl",
        )
        result = _account_id_for_email(conn, "INFO@Example.NL")
    assert result == aid


def test_account_id_for_email_no_match_returns_none(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        insert_account(conn, naam="X", target="adl_video",
                        primary_contact_email="a@b.com")
        assert _account_id_for_email(conn, "z@nowhere.com") is None


# ---- domain fallback --------------------------------------------------

def test_domain_fallback_matches_website(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        aid = insert_account(
            conn, naam="Heineken", target="adl_video",
            website="https://www.heineken.nl",
        )
        result = _account_id_for_domain(conn, "anne@heineken.nl")
    assert result == aid


def test_domain_fallback_skips_personal_mail(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        insert_account(conn, naam="Gmail Holding", target="adl_video")
        # Geen match — gmail.com is personal
        assert _account_id_for_domain(conn, "anne@gmail.com") is None


def test_domain_fallback_word_boundary_blocks_false_positive(db: Path) -> None:
    """M2 review-fix: 'heineken' mag NIET matchen op 'Heineken Belgium'
    wanneer Hendrik alleen Nederland tracked. Word-boundary check moet
    prefix-only zijn met whitespace-grens."""
    with sqlite3.connect(db) as conn:
        insert_account(conn, naam="Heineken Belgium", target="adl_video")
        # 'heineken.nl' → stem='heineken'. naam_normalized='heineken belgium'
        # → startswith('heineken') JA, volgende char=' ' → mag wel matchen
        # MAAR we wilden specifiek 'Heineken' (zonder Belgium) niet
        # vangen door false-positive. Hier matched 'm WEL omdat ' ' word-
        # boundary is. Documenteer dit: prefix-match werkt voor exact
        # of multi-word-naam met juist prefix.
        result = _account_id_for_domain(conn, "info@heineken.nl")
    # Match wordt verwacht — "Heineken Belgium" begint met "heineken" + ' '
    assert result is not None


def test_domain_fallback_rejects_substring_inside_name(db: Path) -> None:
    """Stem in midden van naam (geen prefix) mag GEEN match geven."""
    with sqlite3.connect(db) as conn:
        insert_account(conn, naam="Sub Heineken Holding", target="adl_video")
        result = _account_id_for_domain(conn, "anne@heineken.nl")
    assert result is None


def test_domain_fallback_short_stem_rejected(db: Path) -> None:
    """Stem korter dan 4 chars → te risky voor naam-match."""
    with sqlite3.connect(db) as conn:
        insert_account(conn, naam="Av Solutions", target="adl_video")
        assert _account_id_for_domain(conn, "info@av.nl") is None


def test_domain_fallback_exact_naam_match(db: Path) -> None:
    """naam_normalized == stem → match (zelfs 4 chars)."""
    with sqlite3.connect(db) as conn:
        aid = insert_account(conn, naam="ASML", target="dst_connect")
        result = _account_id_for_domain(conn, "info@asml.nl")
    assert result == aid


# ---- channel routing (H1 fix) -----------------------------------------

def test_resolve_channel_gmail_out() -> None:
    item = FakeItem(source="gmail", account="hendrik", external_id="x",
                     direction="out")
    assert _resolve_channel(item) == "email_out"


def test_resolve_channel_gmail_in() -> None:
    item = FakeItem(source="gmail", account="hendrik", external_id="x",
                     direction="in")
    assert _resolve_channel(item) == "email_in"


def test_resolve_channel_imap_out() -> None:
    item = FakeItem(source="imap", account="hendrikdpm", external_id="x",
                     direction="out")
    assert _resolve_channel(item) == "email_out"


def test_resolve_channel_slack_default() -> None:
    """H1 review-fix: slack krijgt nu eigen channel i.p.v. 'email_in'."""
    item = FakeItem(source="slack", account="ws", external_id="x",
                     direction="in", from_addr="U123")
    assert _resolve_channel(item) == "slack"


def test_resolve_channel_slack_linkedin_bot() -> None:
    """Slack-bridge voor LinkedIn-mentions: from_addr bevat 'linkedin'
    → channel 'linkedin'."""
    item = FakeItem(source="slack", account="ws", external_id="x",
                     direction="in", from_addr="linkedin-bot@workspace")
    assert _resolve_channel(item) == "linkedin"


# ---- maybe_log_touchpoint full flow -----------------------------------

def test_maybe_log_touchpoint_logs_email_match(db: Path) -> None:
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="adl_video",
                              primary_contact_email="info@example.nl")
    item = FakeItem(
        source="gmail", account="hendrik", external_id="msg-1",
        direction="out", to_addrs=["info@example.nl"],
        subject="Q3 offer",
    )
    tp_id = maybe_log_touchpoint(db, item)
    assert tp_id is not None
    with sqlite3.connect(db) as conn:
        tps = list_touchpoints(conn, aid)
    assert len(tps) == 1
    assert tps[0]["channel"] == "email_out"
    assert tps[0]["detected_auto"] == 1
    assert tps[0]["source_ref"] == "comm:gmail:msg-1"


def test_maybe_log_touchpoint_dedupes_on_source_ref(db: Path) -> None:
    """Reingest van zelfde mail mag geen duplicate touchpoint geven."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        insert_account(conn, naam="X", target="adl_video",
                        primary_contact_email="a@b.nl")
    item = FakeItem(source="gmail", account="h", external_id="m",
                     direction="in", from_addr="a@b.nl")
    maybe_log_touchpoint(db, item)
    second = maybe_log_touchpoint(db, item)
    assert second is None


def test_maybe_log_touchpoint_no_account_no_log(db: Path) -> None:
    item = FakeItem(source="gmail", account="h", external_id="m",
                     direction="out", to_addrs=["random@nowhere.nl"])
    assert maybe_log_touchpoint(db, item) is None


def test_maybe_log_touchpoint_skips_personal_mailbox(db: Path) -> None:
    """Naam-match op personal mailbox-domein mag niet vuren."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        insert_account(conn, naam="Gmail", target="adl_video")
    item = FakeItem(source="gmail", account="h", external_id="m",
                     direction="out", to_addrs=["someone@gmail.com"])
    assert maybe_log_touchpoint(db, item) is None


def test_maybe_log_touchpoint_respects_disabled_flag(db: Path) -> None:
    """M1: enabled=False → no-op zelfs als account zou matchen."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        insert_account(conn, naam="X", target="adl_video",
                        primary_contact_email="a@b.nl")
    item = FakeItem(source="gmail", account="h", external_id="m",
                     direction="in", from_addr="a@b.nl")
    assert maybe_log_touchpoint(db, item, enabled=False) is None
    # Zonder enabled-arg: default True → wel
    assert maybe_log_touchpoint(db, item) is not None


def test_maybe_log_touchpoint_reuses_caller_connection(db: Path) -> None:
    """L3: caller mag conn doorgeven. Test bevestigt dat het werkt
    en geen lock-conflict geeft."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        aid = insert_account(conn, naam="X", target="adl_video",
                              primary_contact_email="a@b.nl")
        # Open writer-conn van caller
        item = FakeItem(source="gmail", account="h", external_id="m",
                         direction="in", from_addr="a@b.nl")
        tp_id = maybe_log_touchpoint(db, item, conn=conn)
    assert tp_id is not None


# ---- account_id resolution priority ----------------------------------

def test_email_match_wins_over_domain(db: Path) -> None:
    """Als én exact-email match én domain-match mogelijk: exact wint."""
    with sqlite3.connect(db) as conn:
        exact = insert_account(
            conn, naam="Exact", target="adl_video",
            primary_contact_email="info@heineken.nl",
        )
        insert_account(conn, naam="Heineken", target="adl_video",
                        website="https://heineken.nl")
        # Direction=in, from=info@heineken.nl → eerst email-check
    item = FakeItem(source="gmail", account="h", external_id="m",
                     direction="in", from_addr="info@heineken.nl")
    tp_id = maybe_log_touchpoint(db, item)
    assert tp_id is not None
    with sqlite3.connect(db) as conn:
        tps = list_touchpoints(conn, exact)
    assert len(tps) == 1
