"""4-laagse filter voor TenderNed-publicaties.

Onderzoek met the user (3 voorbeelden) toonde dat geen enkele single-laag
filter voldoet:
- 407531 (Leiden, "AV-Middelen") → CPV-code 32320000 vangt 'm
- 419614 (ROC, "Narrowcasting") → trefwoord1 = "Narrowcasting" vangt 'm
- 229136 (NS, "Digitale Reclamedragers") → geen CPV-code uit lijst, geen
  AV-trefwoord. Wel CPV-OMSCHRIJVING "audiovisuele uitrusting" → laag 3
  vangt 'm.

Vier lagen, OR-gecombineerd (één hit = match):
  1. Trefwoord1/2 — aanbesteder zelf getagd, hoogste precisie
  2. CPV-code prefix-match — officiële categorisering
  3. CPV-omschrijving keyword-match — vangt items waar de aanbesteder
     een breder CPV koos (zoals NS Stations 50300000)
  4. Titel + opdrachtBeschrijving keyword-match — vangt jargon-mismatches
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TenderFilter:
    """Configuratie voor de matcher. Wordt geladen uit
    `config/tenders.yaml` of in-memory voor tests."""
    cpv_codes: tuple[str, ...] = ()                  # 8-cijferige prefixes ('32320000')
    cpv_description_keywords: tuple[str, ...] = ()   # case-insensitive substrings
    keywords: tuple[str, ...] = ()                   # case-insensitive substrings


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    layers: tuple[str, ...]            # 'trefwoord' / 'cpv_code' / 'cpv_desc' / 'keyword'
    terms: tuple[str, ...]             # alle getriggerde termen, gededupliceerd (audit-trail)
    per_layer_terms: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Per-layer terms — voor alerts die per laag een accurate sample willen
    # tonen. Format: (("trefwoord", ("narrowcasting",)), ("cpv_code", ("32322000",)), ...)
    # M2 review-finding: zonder dit pakte _trigger_summary willekeurige
    # termen uit de dedupe-lijst en hing ze aan willekeurige lagen.


def _strip_quotes_lower(s: str | None) -> str:
    """Trefwoord-velden komen soms gequoted terug ('"Narrowcasting"')."""
    if not s:
        return ""
    return s.strip().strip('"').strip("'").lower()


def _cpv_code_prefix(code: str) -> str:
    """CPV-codes komen als '32322000-6' (8 cijfers + checksum). We
    matchen alleen op de eerste 8 cijfers."""
    return str(code or "").split("-", 1)[0].strip()


def match(item: dict[str, Any], cfg: TenderFilter) -> MatchResult:
    """Pas alle 4 lagen toe op een TenderNed-publicatie-detail (JSON-API
    output van /papi/tenderned-rs-tns/publicaties/{id}). Returns alle
    triggered lagen + termen — niet alleen de eerste, zodat de alert
    the user kan tonen WAAROM dit binnenkwam."""
    layers: list[str] = []
    per_layer: dict[str, list[str]] = {}

    keywords_lower = tuple(k.lower() for k in cfg.keywords if k)

    def _add(layer: str, term: str) -> None:
        if layer not in layers:
            layers.append(layer)
        bucket = per_layer.setdefault(layer, [])
        if term not in bucket:
            bucket.append(term)

    # Layer 1: trefwoord1 / trefwoord2 (aanbesteder-getagd)
    trefw_haystack = " ".join([
        _strip_quotes_lower(item.get("trefwoord1")),
        _strip_quotes_lower(item.get("trefwoord2")),
    ])
    for kw, kw_l in zip(cfg.keywords, keywords_lower):
        if kw_l and kw_l in trefw_haystack:
            _add("trefwoord", kw)

    # Layer 2: CPV-code prefix-match (exact op 8 cijfers)
    wanted_codes = {_cpv_code_prefix(c) for c in cfg.cpv_codes if c}
    cpv_list = item.get("cpvCodes") or []
    for c in cpv_list:
        code = _cpv_code_prefix(c.get("code", "") if isinstance(c, dict) else "")
        if code and code in wanted_codes:
            _add("cpv_code", code)

    # Layer 3: CPV-omschrijving keyword-match
    cpv_desc_haystack = " ".join(
        str(c.get("omschrijving", "")) if isinstance(c, dict) else ""
        for c in cpv_list
    ).lower()
    for kw in cfg.cpv_description_keywords:
        if kw and kw.lower() in cpv_desc_haystack:
            _add("cpv_desc", kw)

    # Layer 4: titel + opdrachtBeschrijving keyword-match
    title_desc_haystack = " ".join([
        str(item.get("aanbestedingNaam") or ""),
        str(item.get("opdrachtBeschrijving") or ""),
    ]).lower()
    for kw, kw_l in zip(cfg.keywords, keywords_lower):
        if kw_l and kw_l in title_desc_haystack:
            _add("keyword", kw)

    # Cross-layer dedupe voor audit-vriendelijke `terms`-lijst
    all_terms: list[str] = []
    for layer in layers:
        for t in per_layer[layer]:
            if t not in all_terms:
                all_terms.append(t)

    return MatchResult(
        matched=bool(layers),
        layers=tuple(layers),
        terms=tuple(all_terms),
        per_layer_terms=tuple((layer, tuple(per_layer[layer])) for layer in layers),
    )


# Default filter — gevalideerd tegen ID 407531/419614 (match) en 229136
# (vangt via cpv_desc keyword "audiovisuele"). Pas aan via config/tenders.yaml.
DEFAULT_FILTER = TenderFilter(
    cpv_codes=(
        "32320000",  # Televisie- en audiovisuele uitrusting
        "32321000",  # Tv-projectie
        "32322000",  # Multimedia-uitrusting (jouw narrowcasting case)
        "32323000",  # Monitors
        "48515000",  # Software voor videoconferenties
        "51310000",  # Installatie van radio/tv/audio/video-apparatuur
        "72212500",  # Communicatie/multimedia software-ontwikkeling
        "50300000",  # Reparatie/onderhoud audiovisuele uitrusting
        "50340000",  # Reparatie audio/video/optisch
    ),
    cpv_description_keywords=(
        "audiovisuele",      # vangt NS Reclamedragers (CPV 50300000)
        "audio-visual",
        "audiovisueel",
        "multimedia",
    ),
    keywords=(
        "narrowcasting", "narrowcast", "narrowcastingoplossing",
        "digital signage", "digitale signage",
        "av-installatie", "av installatie",
        "av-techniek", "av techniek",
        "av-middelen", "av middelen",
        "audiovisueel", "audio-visueel", "audiovisuele",
        "beeldschermsysteem", "beeldscherm informatie",
        "presentatiesysteem",
        "led-scherm", "led scherm", "lcd-scherm", "lcd scherm",
        "reclamedragers", "reclamezuil", "reclamemast",
        "infozuil", "info-zuil", "informatie-zuil", "informatiezuil",
        "stationsscherm",
        "digitale reclame", "dynamisch reclamenetwerk",
        "outdoor display", "dooh",
        "wayfinding",
        "kiosks",
        "contentmanagement",  # vangt secundair als gecombineerd met AV-CPV
    ),
)
