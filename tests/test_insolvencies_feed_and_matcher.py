"""Tests voor feed-parser + matcher.

Live-validated fixtures:
- Fresh Food (uit feed van 7/6) — basis case
- Ruitech Solutions (uit Hendriks test op faillissementsdossier.nl) —
  illustratie dat layer 2/3 alleen wél AV-bedrijven misst zonder
  watchlist (negatieve test)
- Surseance Lagelanden Zorg — alternatief status type
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from extensions.insolvencies.feed import (
    InsolvencyItem, parse_description, parse_feed,
)
from extensions.insolvencies.matcher import (
    DEFAULT_FILTER, InsolvencyFilter, match,
)
from extensions.insolvencies.schema import (
    add_to_watchlist, init_insolvencies_schema,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "insolv.db"
    init_insolvencies_schema(p)
    return p


# --- raw fixtures from live feed ----------------------------------------

FRESH_FOOD_DESC = (
    "Fresh Food World B.V. te Tynaarlo (Drenthe) is door de rechtbank "
    "in Noord-Nederland failliet verklaard. Als curator is aangesteld "
    "mr J.M. Pol. Het insolventienummer van deze zaak is F.18/26/145. "
    "De (hoofd)activiteit van Fresh Food World B.V. is groothandel en "
    "handelsbemiddeling (niet in auto's en motorfietsen). Er zijn (nog) "
    "geen verslagen beschikbaar.<br><br>Status: Faillissement | "
    "KvK nummer: 62457756 | Plaats: Tynaarlo"
)

LAGELANDEN_DESC = (
    "Aan Lagelanden Zorg B.V. te Huizen (Noord-Holland) is door de "
    "rechtbank in Midden-Nederland surseance verleend. Als bewindvoerder "
    "is aangesteld mr M.A. van der Hoeven. Het insolventienummer van "
    "deze zaak is S.16/26/1091. De (hoofd)activiteit van Lagelanden "
    "Zorg B.V. is maatschappelijke dienstverlening zonder overnachting. "
    "Er zijn (nog) geen verslagen beschikbaar.<br><br>Status: Surseance | "
    "KvK nummer: 75135396 | Plaats: Huizen"
)

# Ruitech: zoals uit Hendriks test
RUITECH_ITEM = InsolvencyItem(
    link="https://www.faillissementsdossier.nl/nl/faillissement/1967525/ruitech-solutions-b-v.aspx",
    naam="Ruitech Solutions B.V.",
    pub_date="Wed, 27 May 2026 00:00:00 GMT",
    description_raw="",
    kvk="77223764",
    plaats="Zaltbommel",
    provincie="Gelderland",
    rechtbank="Gelderland",
    curator="mr R. van Dijk",
    insolventie_nr="F.05/26/207",
    status="Faillissement",
    hoofd_activiteit="Groothandel en handelsbemiddeling (niet in auto's en motorfietsen)",
)


# --- parser -------------------------------------------------------------

def test_parse_fresh_food_extracts_all_fields() -> None:
    f = parse_description(FRESH_FOOD_DESC)
    assert f["plaats"] == "Tynaarlo"
    assert f["provincie"] == "Drenthe"
    assert f["rechtbank"] == "Noord-Nederland"
    assert f["curator"] == "mr J.M. Pol"
    assert f["insolventie_nr"] == "F.18/26/145"
    assert f["hoofd_activiteit"].startswith("groothandel en handelsbemiddeling")
    assert f["status"] == "Faillissement"
    assert f["kvk"] == "62457756"


def test_parse_surseance_status_distinguished() -> None:
    f = parse_description(LAGELANDEN_DESC)
    assert f["status"] == "Surseance"
    assert f["rechtbank"] == "Midden-Nederland"
    assert f["curator"] == "mr M.A. van der Hoeven"
    assert f["insolventie_nr"] == "S.16/26/1091"


def test_parse_handles_empty_description() -> None:
    f = parse_description("")
    assert all(v is None for v in f.values())


def test_parse_handles_partial_description() -> None:
    """Fragmentarisch — alleen KvK + status, geen plaats/curator."""
    text = "Iets onbekends. Status: Faillissement | KvK nummer: 12345678"
    f = parse_description(text)
    assert f["kvk"] == "12345678"
    assert f["status"] == "Faillissement"
    assert f["plaats"] is None
    assert f["curator"] is None


def test_parse_feed_full_rss() -> None:
    # Productie-feed gebruikt HTML-entities (&lt;br&gt;) i.p.v. raw
    # <br>; ElementTree decodet entities tijdens parsen waardoor ze als
    # tekst in description-content komen. Voor leesbaarheid wrappen we
    # hier in CDATA — semantisch equivalent voor parsing-doeleinden.
    rss = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel>'
        '<item>'
        '<title>Fresh Food World B.V.</title>'
        '<link>https://www.example.com/1</link>'
        f'<description><![CDATA[{FRESH_FOOD_DESC}]]></description>'
        '<pubDate>Wed, 03 Jun 2026 00:00:00 GMT</pubDate>'
        '</item>'
        '</channel></rss>'
    ).encode()
    items = parse_feed(rss)
    assert len(items) == 1
    assert items[0].naam == "Fresh Food World B.V."
    assert items[0].kvk == "62457756"
    assert items[0].link == "https://www.example.com/1"


def test_m3_parse_feed_handles_entity_decoded_br() -> None:
    """M3: productie-feed gebruikt &lt;br&gt; entities die door
    ElementTree gedecodeerd worden naar raw '<br>' in description-string.
    _strip_html in parser moet die clean afhandelen. Regression-test
    voor een toekomstige XML-parser-wissel die entities anders doet."""
    rss = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><item>'
        '<title>Pegasus B.V.</title>'
        '<link>http://x</link>'
        '<description>Pegasus B.V. te Tilburg (Noord-Brabant) is door de '
        'rechtbank in Zeeland-West-Brabant failliet verklaard. Als curator is '
        'aangesteld mr X. Y. Het insolventienummer van deze zaak is '
        'F.02/26/100. De (hoofd)activiteit van Pegasus B.V. is reclame. '
        'Er zijn geen verslagen.&lt;br&gt;&lt;br&gt;'
        'Status: Faillissement | KvK nummer: 11223344 | Plaats: Tilburg</description>'
        '<pubDate>Wed, 03 Jun 2026 00:00:00 GMT</pubDate>'
        '</item></channel></rss>'
    ).encode()
    items = parse_feed(rss)
    assert len(items) == 1
    it = items[0]
    # Geen rauwe <br>-tekst in geparsed velden
    assert it.hoofd_activiteit is not None
    assert "<br>" not in it.hoofd_activiteit
    assert it.hoofd_activiteit == "reclame"
    assert it.kvk == "11223344"
    assert it.status == "Faillissement"


def test_parse_feed_skips_item_without_link_or_title() -> None:
    rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        '<item><title>X</title><link></link><description></description></item>'
        '<item><title></title><link>http://x</link><description></description></item>'
        '<item><title>OK</title><link>http://y</link><description></description></item>'
        '</channel></rss>'
    ).encode()
    items = parse_feed(rss)
    assert len(items) == 1
    assert items[0].naam == "OK"


# --- matcher ------------------------------------------------------------

def test_ruitech_misses_layer_2_and_3_without_watchlist(db: Path) -> None:
    """Documenteert Hendriks test-case: ZONDER KvK op watchlist
    wordt Ruitech NIET gevangen — hoofdactiviteit 'groothandel' bevat
    geen AV-trefwoorden en de naam ook niet."""
    with sqlite3.connect(db) as conn:
        r = match(RUITECH_ITEM, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is False
    assert r.layers == ()


def test_ruitech_matches_when_kvk_on_watchlist(db: Path) -> None:
    """Met KvK op watchlist → layer 1 vangt 'm."""
    with sqlite3.connect(db, isolation_level=None) as conn:
        add_to_watchlist(conn, kvk="77223764", naam_hint="Ruitech")
    with sqlite3.connect(db) as conn:
        r = match(RUITECH_ITEM, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is True
    assert "watchlist" in r.layers
    assert "77223764" in r.terms


def test_activity_keyword_layer(db: Path) -> None:
    """Bedrijf met audiovisueel-keyword in hoofdactiviteit → layer 2."""
    item = InsolvencyItem(
        link="http://x", naam="Random BV", pub_date="",
        description_raw="",
        kvk="11111111",
        hoofd_activiteit="vervaardiging van audiovisuele apparatuur",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is True
    assert "activity" in r.layers
    assert "audiovisuele" in r.terms or "audiovisueel" in r.terms


def test_name_keyword_layer(db: Path) -> None:
    """Bedrijfsnaam bevat 'Signage' → layer 3."""
    item = InsolvencyItem(
        link="http://x", naam="Pegasus Signage Holding B.V.", pub_date="",
        description_raw="",
        hoofd_activiteit="financieele holdings",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is True
    assert "name" in r.layers
    assert "signage" in r.terms


def test_short_keyword_word_boundary_prevents_false_positive(db: Path) -> None:
    """'av' (2 chars) mag NIET matchen in 'navigatie' of 'havik'."""
    item = InsolvencyItem(
        link="http://x", naam="Navigatie Plus B.V.", pub_date="",
        description_raw="", hoofd_activiteit="",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is False


def test_h3_short_av_token_is_no_longer_a_name_keyword(db: Path) -> None:
    """H3 review-fix: 'av' en 'a.v.' zijn uit name_keywords verwijderd.
    'Easy AV Solutions' valt nu via activity-laag (audiovisueel) op te
    pikken, niet meer via puur naam-substring. Het 2-char 'av' was
    onbetrouwbaar (false positives 'Wav-bestanden', 'Rav4')."""
    item = InsolvencyItem(
        link="http://x", naam="Easy AV Solutions B.V.", pub_date="",
        description_raw="", hoofd_activiteit="",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is False  # gevolg van H3-fix


def test_h3_signage_substring_in_name_still_matched(db: Path) -> None:
    """Langere tokens (>4 chars) blijven werken als naam-substring.
    Sanity-check dat H3 niet alles uit name_keywords sloopte."""
    item = InsolvencyItem(
        link="http://x", naam="Pegasus Signage Holding B.V.", pub_date="",
        description_raw="", hoofd_activiteit="",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    assert r.matched is True
    assert "name" in r.layers
    assert "signage" in r.terms


def test_empty_filter_matches_nothing(db: Path) -> None:
    cfg = InsolvencyFilter()
    with sqlite3.connect(db) as conn:
        r = match(RUITECH_ITEM, cfg, watchlist_conn=conn)
    assert r.matched is False


def test_per_layer_terms_no_cross_leak(db: Path) -> None:
    """Audit-check: term uit layer-activity mag niet in layer-name
    verschijnen."""
    item = InsolvencyItem(
        link="http://x", naam="Audiovisueel Plus B.V.", pub_date="",
        description_raw="",
        hoofd_activiteit="iets met multimedia",
    )
    with sqlite3.connect(db) as conn:
        r = match(item, DEFAULT_FILTER, watchlist_conn=conn)
    by_layer = dict(r.per_layer_terms)
    assert "multimedia" in by_layer["activity"]
    assert "multimedia" not in by_layer.get("name", ())
    assert "audiovisueel" in by_layer["name"]
    assert "audiovisueel" not in by_layer.get("activity", ())
