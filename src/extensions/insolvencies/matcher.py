"""3-laagse filter voor faillissementen.

Onderzoek met the user toonde aan dat de RSS-bron geen SBI-code heeft,
alleen een NL-tekst-omschrijving. Voor specifieke branches als
AV/narrowcasting is layer 1 (KvK-watchlist) dominant — layer 2/3 zijn
best-effort vangnetten voor expliciete branche-namers.

Lagen, OR-gecombineerd:
  1. KvK-watchlist match (hoogste prioriteit; checked tegen DB)
  2. (hoofd)activiteit substring-match (NL-tekst van de feed)
  3. Bedrijfsnaam substring-match (word-boundary voor korte termen
     om false-positives in willekeurige naamtekst te voorkomen)
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .feed import InsolvencyItem
from .schema import is_kvk_ignored, is_kvk_on_watchlist


@dataclass(frozen=True)
class InsolvencyFilter:
    """Filter-config. Hard-coded defaults verderop; overridable voor tests."""
    activity_keywords: tuple[str, ...] = ()       # match in (hoofd)activiteit
    name_keywords: tuple[str, ...] = ()           # match in bedrijfsnaam
    short_token_max_len: int = 4                  # ≤ deze lengte → word-boundary verplicht


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    layers: tuple[str, ...]                       # 'watchlist' | 'activity' | 'name'
    terms: tuple[str, ...]                        # alle getriggerde termen (dedupe)
    per_layer_terms: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _contains(haystack: str, needle: str, *, short_max_len: int) -> bool:
    """Substring-match, met word-boundary voor korte termen.

    'av' (2 chars) → \bAV\b nodig — anders matcht het in 'ravage', 'havik'.
    'narrowcasting' (13 chars) → gewone substring is veilig.
    """
    h = haystack.lower()
    n = needle.lower().strip()
    if not n:
        return False
    if len(n) <= short_max_len:
        return re.search(rf"\b{re.escape(n)}\b", h) is not None
    return n in h


def match(
    item: InsolvencyItem,
    cfg: InsolvencyFilter,
    *,
    watchlist_conn: sqlite3.Connection | None = None,
) -> MatchResult:
    layers: list[str] = []
    per_layer: dict[str, list[str]] = {}

    def _add(layer: str, term: str) -> None:
        if layer not in layers:
            layers.append(layer)
        bucket = per_layer.setdefault(layer, [])
        if term not in bucket:
            bucket.append(term)

    # H2: KvK op ignore-lijst → matched=False, skip alle lagen. Item komt
    # wel in DB voor history zodat tools_search 'm nog kan vinden.
    if watchlist_conn is not None and item.kvk:
        if is_kvk_ignored(watchlist_conn, item.kvk):
            return MatchResult(matched=False, layers=(), terms=(),
                                per_layer_terms=())

    # Layer 1: KvK-watchlist
    if watchlist_conn is not None and item.kvk:
        if is_kvk_on_watchlist(watchlist_conn, item.kvk):
            _add("watchlist", str(item.kvk))

    # Layer 2: hoofdactiviteit-keyword
    activity = item.hoofd_activiteit or ""
    if activity:
        for kw in cfg.activity_keywords:
            if _contains(activity, kw, short_max_len=cfg.short_token_max_len):
                _add("activity", kw)

    # Layer 3: bedrijfsnaam-keyword
    naam = item.naam or ""
    if naam:
        for kw in cfg.name_keywords:
            if _contains(naam, kw, short_max_len=cfg.short_token_max_len):
                _add("name", kw)

    # Cross-layer dedupe voor flat .terms
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


# Default filter — eerlijk geanalyseerd in overleg met the user. Pas aan
# wanneer Layer 2/3 te ruis-gevoelig blijkt of misses opduiken.
DEFAULT_FILTER = InsolvencyFilter(
    activity_keywords=(
        # AV / narrowcasting / digital signage gerelateerd
        "audiovisueel", "audio-visueel", "audio visueel",
        "audiovisuele", "audio-visuele",
        "multimedia",
        "presentatie",
        "narrowcasting",
        "informatieschermen", "informatiezuilen",
        "digital signage", "digitale signage",
        "reclame",
        "reproductie geluidsdragers",
        "communicatie-apparatuur", "communicatieapparatuur",
        "vervaardiging van consumentenelektronica",
        "vervaardiging van elektrische apparaten",
        "groothandel computers", "groothandel in elektronica",
        "verhuur en lease van apparatuur",
        "mediadienst",
        "telecommunicatie",
    ),
    name_keywords=(
        # H3-fix: 'av' en 'a.v.' verwijderd — 2-char tokens met
        # word-boundary geven false positives bij "Wav-bestanden",
        # "Rav4", "Bravo" etc. "AV-installateur BV" wordt al gevangen
        # via cpv_desc/activity-laag ("audiovisuele") of via langere
        # tokens hieronder.
        "narrowcasting", "narrowcast",
        "signage",
        "audiovisueel", "audio-visueel",
        "media",
        "display", "displays",
        "screen", "screens",
        "presentatie",
        "beeldscherm",
    ),
)
