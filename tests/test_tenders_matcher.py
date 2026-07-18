"""Tests voor extensions.tenders.matcher.

Hendrik gaf 3 productie-TenderNed-ID's als regression-baseline:
- 407531  Gemeente Leiden "IT Hardware en AV-Middelen"
- 229136  NS Stations "Digitale Reclamedragers"
- 419614  ROC Amsterdam "Narrowcastingoplossing (SaaS)"

We bouwen synthetische detail-payloads die de relevante velden uit de
echte JSON-API-responses spiegelen, en valideren dat alle drie matched
worden door minstens één van de 4 lagen — én dat een irrelevante
publicatie correct genegeerd wordt.
"""
from __future__ import annotations

from extensions.tenders.matcher import (
    DEFAULT_FILTER,
    TenderFilter,
    match,
)

# --- regression fixtures from Hendrik's 3 examples ----------------------

LEIDEN_AV = {
    "publicatieId": 407531,
    "kenmerk": 576629,
    "aanbestedingNaam": "A03.78.2025 IT Hardware en AV-Middelen",
    "opdrachtgeverNaam": "Gemeente Leiden",
    "opdrachtBeschrijving": "Gemeente Leiden inkoop IT Hardware en AV-middelen.",
    "trefwoord1": '"IT Hardware"',
    "trefwoord2": '"AV-Middelen"',
    "cpvCodes": [
        {"code": "30200000-1", "omschrijving": "Computeruitrusting en -benodigdheden", "isHoofdOpdracht": False},
        {"code": "30230000-0", "omschrijving": "Computerapparatuur", "isHoofdOpdracht": True},
        {"code": "32320000-2", "omschrijving": "Televisie- en audiovisuele uitrusting", "isHoofdOpdracht": False},
    ],
}

NS_RECLAMEDRAGERS = {
    "publicatieId": 229136,
    "kenmerk": 333333,
    "aanbestedingNaam": "Huur, Beheer en Onderhoud van Digitale Reclamedragers",
    "opdrachtgeverNaam": "NS Stations B.V.",
    "opdrachtBeschrijving": (
        "NS Stations huurt reclamedragers voor digital signage op stations."
    ),
    "trefwoord1": "Reclamedragers",
    "trefwoord2": "Verhuur",
    "cpvCodes": [
        {"code": "50300000-8",
         "omschrijving": "Reparatie, onderhoud en aanverwante diensten in verband met pc's, kantooruitrusting, telecommunicatie- en audiovisuele uitrusting",
         "isHoofdOpdracht": False},
        {"code": "72000000-5",
         "omschrijving": "IT-diensten: adviezen, softwareontwikkeling, internet en ondersteuning",
         "isHoofdOpdracht": False},
        {"code": "79341200-8",
         "omschrijving": "Diensten voor reclamebeheer",
         "isHoofdOpdracht": True},
    ],
}

ROC_NARROWCASTING = {
    "publicatieId": 419614,
    "kenmerk": 576629,
    "aanbestedingNaam": "Narrowcastingoplossing (SaaS)",
    "opdrachtgeverNaam": "ROC van Amsterdam - Flevoland",
    "opdrachtBeschrijving": (
        "Het leveren en beheren van een integrale narrowcastingoplossing "
        "op basis van een SaaS-platform."
    ),
    "trefwoord1": '"Narrowcasting"',
    "trefwoord2": '"Contentmanagement"',
    "cpvCodes": [
        {"code": "32322000-6", "omschrijving": "Multimedia-uitrusting", "isHoofdOpdracht": False},
        {"code": "72212500-4", "omschrijving": "Diensten voor ontwikkeling van communicatie- en multimediasoftware", "isHoofdOpdracht": True},
    ],
}

# Een aanbesteding die GEEN match mag triggeren
IRRELEVANT_GEMALEN = {
    "publicatieId": 999999,
    "kenmerk": 999999,
    "aanbestedingNaam": "Groot onderhoud gemalen 2026-2028",
    "opdrachtgeverNaam": "Gemeente Lelystad",
    "opdrachtBeschrijving": "Groot onderhoud aan bestaande rioolgemalen.",
    "trefwoord1": "Riolering",
    "trefwoord2": "Onderhoud",
    "cpvCodes": [
        {"code": "45232423-3", "omschrijving": "Bouw van rioolwaterzuiveringen", "isHoofdOpdracht": True},
    ],
}


# --- regression on Hendrik's 3 baselines --------------------------------

def test_leiden_av_matches_multiple_layers() -> None:
    r = match(LEIDEN_AV, DEFAULT_FILTER)
    assert r.matched is True
    # Trefwoord "AV-Middelen", CPV 32320000, omschrijving "audiovisuele",
    # en keyword "av-middelen" in titel — alle 4 lagen actief.
    assert "trefwoord" in r.layers
    assert "cpv_code" in r.layers
    assert "cpv_desc" in r.layers
    assert "keyword" in r.layers
    assert "32320000" in r.terms


def test_ns_reclamedragers_matches_multi_layer() -> None:
    """NS-reclamedragers werd door mijn initial-design gemist; na
    keyword-uitbreiding ('reclamedragers') én CPV-omschrijving-zoekstap
    matchen meerdere lagen. Trefwoord 'Reclamedragers' matched ook
    omdat 'reclamedragers' nu in de keyword-list staat."""
    r = match(NS_RECLAMEDRAGERS, DEFAULT_FILTER)
    assert r.matched is True
    # CPV 50300000 staat in de lijst
    assert "cpv_code" in r.layers
    assert "50300000" in r.terms
    # CPV-omschrijving heeft "audiovisuele"
    assert "cpv_desc" in r.layers
    assert "audiovisuele" in r.terms
    # Keyword "digital signage" in beschrijving + "reclamedragers" in titel
    assert "keyword" in r.layers


def test_roc_narrowcasting_matches_via_trefwoord() -> None:
    r = match(ROC_NARROWCASTING, DEFAULT_FILTER)
    assert r.matched is True
    assert "trefwoord" in r.layers
    assert "narrowcasting" in r.terms
    # CPV 32322000 én 72212500 zitten in lijst → cpv_code-laag
    assert "cpv_code" in r.layers


def test_irrelevant_gemalen_does_not_match() -> None:
    r = match(IRRELEVANT_GEMALEN, DEFAULT_FILTER)
    assert r.matched is False
    assert r.layers == ()
    assert r.terms == ()


# --- layer-isolation tests ----------------------------------------------

def test_trefwoord_match_strips_quotes() -> None:
    """TenderNed levert trefwoord soms gequoted ('"Narrowcasting"').
    Strip-helper moet die quotes weghalen voor de substring-check."""
    cfg = TenderFilter(keywords=("narrowcasting",))
    item = {"trefwoord1": '"Narrowcasting"', "trefwoord2": "",
            "cpvCodes": [], "aanbestedingNaam": "", "opdrachtBeschrijving": ""}
    r = match(item, cfg)
    assert r.matched is True
    assert r.layers == ("trefwoord",)


def test_cpv_code_match_ignores_checksum() -> None:
    """'32322000-6' moet matchen tegen filter-code '32322000'."""
    cfg = TenderFilter(cpv_codes=("32322000",))
    item = {
        "trefwoord1": "", "trefwoord2": "",
        "aanbestedingNaam": "", "opdrachtBeschrijving": "",
        "cpvCodes": [{"code": "32322000-6", "omschrijving": "x"}],
    }
    r = match(item, cfg)
    assert r.matched is True
    assert r.layers == ("cpv_code",)
    assert r.terms == ("32322000",)


def test_cpv_description_keyword_case_insensitive() -> None:
    cfg = TenderFilter(cpv_description_keywords=("Audiovisuele",))
    item = {
        "trefwoord1": "", "trefwoord2": "",
        "aanbestedingNaam": "", "opdrachtBeschrijving": "",
        "cpvCodes": [{"code": "99999999-9", "omschrijving": "Iets met AUDIOVISUELE uitrusting"}],
    }
    r = match(item, cfg)
    assert r.matched is True
    assert "cpv_desc" in r.layers


def test_title_keyword_case_insensitive() -> None:
    cfg = TenderFilter(keywords=("narrowcasting",))
    item = {
        "trefwoord1": "", "trefwoord2": "",
        "aanbestedingNaam": "NARROWCASTING in hoofdletters",
        "opdrachtBeschrijving": "",
        "cpvCodes": [],
    }
    r = match(item, cfg)
    assert r.matched is True
    assert r.layers == ("keyword",)


def test_description_keyword_matches_when_title_does_not() -> None:
    cfg = TenderFilter(keywords=("digital signage",))
    item = {
        "trefwoord1": "", "trefwoord2": "",
        "aanbestedingNaam": "Algemene IT-inkoop",
        "opdrachtBeschrijving": "We zoeken een leverancier voor digital signage in alle filialen.",
        "cpvCodes": [],
    }
    r = match(item, cfg)
    assert r.matched is True
    assert r.layers == ("keyword",)


# --- edge cases ---------------------------------------------------------

def test_empty_item_does_not_match() -> None:
    r = match({}, DEFAULT_FILTER)
    assert r.matched is False
    assert r.layers == ()


def test_missing_cpv_codes_handled_gracefully() -> None:
    item = {"trefwoord1": "narrowcasting", "trefwoord2": "",
             "aanbestedingNaam": "", "opdrachtBeschrijving": ""}
    # geen cpvCodes-veld
    r = match(item, DEFAULT_FILTER)
    assert r.matched is True
    assert "trefwoord" in r.layers


def test_empty_filter_matches_nothing() -> None:
    empty = TenderFilter()
    r = match(ROC_NARROWCASTING, empty)
    assert r.matched is False
    assert r.layers == ()


def test_multiple_terms_deduplicated() -> None:
    """Als 'narrowcasting' in trefwoord en keyword voorkomt, mag het
    maar één keer in terms staan."""
    item = {
        "trefwoord1": '"Narrowcasting"', "trefwoord2": "",
        "aanbestedingNaam": "Narrowcasting setup",
        "opdrachtBeschrijving": "",
        "cpvCodes": [],
    }
    r = match(item, DEFAULT_FILTER)
    assert r.terms.count("narrowcasting") == 1


# --- M2 review-finding: per-layer terms zijn accuraat -------------------

def test_per_layer_terms_match_actually_triggered_layer() -> None:
    """Crux van review-finding M2: de termen-per-laag moeten echt uit
    DIE laag komen, niet uit een cross-layer dedupe-lijst."""
    r = match(ROC_NARROWCASTING, DEFAULT_FILTER)
    by_layer = dict(r.per_layer_terms)

    # trefwoord-laag triggerde op trefwoord1='Narrowcasting' + trefwoord2='Contentmanagement'
    assert "narrowcasting" in by_layer["trefwoord"]
    # cpv_code-laag triggerde op codes uit het filter
    assert "32322000" in by_layer["cpv_code"]
    # cpv_desc-laag op "multimedia" (de omschrijving van CPV 32322000)
    assert "multimedia" in by_layer["cpv_desc"]


def test_per_layer_terms_no_cross_layer_leak() -> None:
    """Een term mag NIET in een laag verschijnen waar die niet
    triggerde. Vóór de fix kreeg 'cpv_code' soms een 'cpv_desc'-term."""
    r = match(LEIDEN_AV, DEFAULT_FILTER)
    by_layer = dict(r.per_layer_terms)
    # 32320000 hoort bij cpv_code, niet bij cpv_desc
    assert "32320000" in by_layer["cpv_code"]
    assert "32320000" not in by_layer.get("cpv_desc", ())
    # "audiovisuele" hoort bij cpv_desc, niet bij cpv_code
    assert "audiovisuele" in by_layer["cpv_desc"]
    assert "audiovisuele" not in by_layer.get("cpv_code", ())
