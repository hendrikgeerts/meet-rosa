"""Topic-clustering over comm_items via subject/summary-tokens.

MVP-heuristiek (geen ML / embeddings):
1. Per comm_item: tokenize subject + summary, lowercase, strip
   stopwords/leestekens, keep tokens ≥ 4 chars.
2. Per token tellen hoeveel distinct items 'em bevatten.
3. Topics = tokens met ≥ `min_items` items in de laatste `days`.
4. Voor elke topic: lijst van item-IDs + most-recent first.

Niet perfect (mail over "Q3" en "Q3-cijfers" tellen apart), maar
goed genoeg om the user te zien wat "leeft" deze week zonder Llama.
Een latere versie kan met embeddings nuance toevoegen.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+")
_MIN_TOKEN_LEN = 4

_STOPWORDS = frozenset({
    # NL korte fillers
    "de", "het", "een", "en", "of", "voor", "naar", "met", "van",
    "ook", "maar", "deze", "die", "dat", "dit", "ben", "hij",
    "zij", "wij", "jullie", "onze", "jouw", "mijn", "geen",
    # Email noise
    "fwd", "fw", "re", "antwoord", "reply", "subject", "betreft",
    "hoi", "hallo", "beste", "groet", "groeten", "regards", "thanks",
    "thank", "please", "graag", "even", "kort", "vraag", "vraagje",
    "info", "informatie", "snel", "korte",
    # Generieke ondertekening-context
    "mvg", "verstuurd", "iphone", "samsung", "android", "outlook",
    "gmail", "mail", "email",
    # EN fillers
    "the", "a", "an", "and", "or", "to", "for", "in", "on",
    "is", "be", "are", "with", "from", "this", "that", "have", "will",
})


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {
        m.group(0).lower()
        for m in _TOKEN_RE.finditer(text)
        if len(m.group(0)) >= _MIN_TOKEN_LEN
        and m.group(0).lower() not in _STOPWORDS
        and not m.group(0).isdigit()  # 2026 etc.
    }


def collect_active_topics(
    db_path: Path, *,
    days: int = 14,
    min_items: int = 3,
    max_topics: int = 10,
) -> list[dict[str, Any]]:
    """Returns top topics in de laatste `days` met >= `min_items`
    distinct comm_items. Sorted by item-count desc."""
    cutoff = int(_time.time()) - days * 86400
    token_to_items: dict[str, set[int]] = {}
    token_latest: dict[str, int] = {}

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, subject, summary, occurred_at "
                "FROM comm_items "
                "WHERE occurred_at >= ? "
                "  AND (intent IS NULL OR intent NOT IN ('newsletter','social')) "
                "ORDER BY occurred_at DESC LIMIT 2000",
                (cutoff,),
            ).fetchall()
            for r in rows:
                item_id = int(r["id"])
                text = " ".join(filter(None, [r["subject"], r["summary"]]))
                tokens = _tokenize(text)
                ts = int(r["occurred_at"] or 0)
                for tok in tokens:
                    token_to_items.setdefault(tok, set()).add(item_id)
                    if ts > token_latest.get(tok, 0):
                        token_latest[tok] = ts
    except sqlite3.OperationalError as e:
        log.warning("topics: db error %s", e)
        return []

    topics: list[dict[str, Any]] = []
    for tok, item_ids in token_to_items.items():
        if len(item_ids) < min_items:
            continue
        topics.append({
            "topic": tok,
            "item_count": len(item_ids),
            "latest_occurred_at": token_latest[tok],
            "item_ids": sorted(item_ids, reverse=True)[:20],
        })
    topics.sort(
        key=lambda t: (t["item_count"], t["latest_occurred_at"]),
        reverse=True,
    )
    return topics[:max_topics]


def collect_topic_items(
    db_path: Path, *,
    topic: str,
    days: int = 30,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Voor één topic-string (subject/summary token): vind comm_items
    waar het token in subject of summary voorkomt."""
    cutoff = int(_time.time()) - days * 86400
    needle = f"%{topic.lower()}%"
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, source, from_addr, subject, summary, "
                "occurred_at, thread_ref "
                "FROM comm_items "
                "WHERE occurred_at >= ? "
                "  AND (LOWER(subject) LIKE ? OR LOWER(summary) LIKE ?) "
                "  AND (intent IS NULL OR intent NOT IN ('newsletter','social')) "
                "ORDER BY occurred_at DESC LIMIT ?",
                (cutoff, needle, needle, limit),
            ).fetchall()
            for r in rows:
                out.append({
                    "id": r["id"],
                    "source": r["source"],
                    "from_addr": r["from_addr"],
                    "subject": (r["subject"] or "")[:120],
                    "summary": (r["summary"] or "")[:200],
                    "occurred_at": r["occurred_at"],
                    "thread_ref": r["thread_ref"],
                })
    except sqlite3.OperationalError as e:
        log.warning("topics: items query failed: %s", e)
    return out
