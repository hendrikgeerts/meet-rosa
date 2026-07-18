"""Wekelijkse markt-intel digest.

Pakt top 15 gescoorde items uit de afgelopen 7 dagen, vraagt Claude om
een CEO-vriendelijke synthese, markeert die items als 'digested' zodat
ze volgende week niet terugkomen.

Items zijn publieke nieuws-bronnen: classifier-label = internal (default),
geen redactie van placeholders nodig — privacy-gateway pakt het via
normale flow. Wel: bedrijfs-context staat in systeem-prompt (intern OK).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.market_intel.schema import mark_digested, top_for_digest
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import current_tz, now_local
TZ = ZoneInfo("Europe/Amsterdam")


DIGEST_PROMPT = """You are Rosa, the user's personal assistant. You write his weekly market-intel digest. He is CEO of two companies:
- YourCompany: digital signage / narrowcasting (NL and worldwide)
- YourHolding: AI / new models / AI tooling

He gets this digest once a week (Sunday 11am). He wants to be ahead of
the curve on market moves and opportunities — not flooded with noise.
He reads this on iMessage; length may be longer than usual since it's a
weekly overview, but stay on point.

Structure:
1. One short opening line ("Market week, [date]" or variant).
2. 🔥 Trending — 1-3 topics that recurred this week (use trending_topics
   in context as hint). Per trend: 1 sentence why + which sources covered it.
3. 💡 Opportunities for you — all items where is_opportunity=true.
   Per item: title • source • opportunity_reason on 1 line + emoji tag
   [DST] or [HGE] based on domain (digital_signage→DST, ai_models→HGE).
   Skip the section if no opportunities.
4. 📊 Top items per domain:
   - 📺 Digital Signage (DST): top items from digital_signage; per item
     1-sentence reason why it matters + source in brackets.
   - 🤖 AI / new models (HGE): top items from ai_models; same shape.
   - 📰 Press / mentions: items from press_mentions where YourCompany,
     YourHolding, or the user Geerts are mentioned. Per item: title +
     source. Skip the section if no real mentions (filter out namesake
     false-positives like generic 'the user' results).
5. Close with 1-2 sentences of your personal take: what is, in your view,
   the most important thing to watch the coming week?

Writing style:
- English, professional but direct.
- Use bullet lists where they help.
- No URLs in prose — they're in the context as 'url' field already. the user
  reads title + source + your take and can ask "send me item X" for the link.
- When in doubt about an item: skip rather than write filler.
- No "good luck this week" or motivational fluff — the user reads this with coffee."""


def generate_market_digest(*, gateway: Gateway, db_path: Path,
                           days: int = 7, limit: int = 15,
                           settings: Any | None = None) -> str:
    """Genereer + verstuur de wekelijkse digest. Returns digest-tekst."""
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        items = top_for_digest(conn, days=days, limit=limit)

    if not items:
        return ("Markt-intel deze week: geen items om te digesten "
                "(feeds nog te kort actief, of alle items hebben score 0).")

    # Trending: bovenstem-keywords uit titels die meer dan 1 source noemen.
    trending = _detect_trending(items)
    payload = {
        "now": now_local().isoformat(),
        "week_iso": now_local().strftime("%Y-W%V"),
        "items": [_compact_item(it) for it in items],
        "trending_topics": trending,
        "stats": {
            "total": len(items),
            "opportunities": sum(1 for i in items if i["is_opportunity"]),
            "by_domain": dict(Counter(i["domain"] for i in items)),
        },
    }
    user_payload = (
        "Context (JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de wekelijkse markt-intel digest."
    )

    system = DIGEST_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="market_intel_weekly",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=2048,
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip() or "(markt-intel digest was leeg)"

    # Markeer als digested zodat volgende week dezelfde items niet
    # opnieuw verschijnen (tenzij een nieuw event ze hertriggert).
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        mark_digested(conn, [it["id"] for it in items])

    return text


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    """Houd alleen velden die de prompt nodig heeft — scheelt tokens."""
    return {
        "id": item["id"],
        "domain": item["domain"],
        "source": item["source"],
        "title": item["title"],
        "url": item.get("url"),
        "summary": item.get("summary"),
        "relevance": item.get("relevance_score"),
        "is_opportunity": bool(item.get("is_opportunity")),
        "opportunity_reason": item.get("opportunity_reason"),
        "published_at": item.get("published_at"),
    }


_STOPWORDS = {
    # NL/EN noise dat geen onderwerp is
    "de", "het", "een", "voor", "van", "naar", "with", "the", "a", "an",
    "to", "of", "in", "on", "is", "and", "for", "as", "at", "by", "from",
    "new", "nieuw", "nieuwe", "ai", "model",
}


def _detect_trending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-3 woorden (>3 chars, niet stopword) die in titels van 2+ items
    voorkomen. Lichte heuristiek; Claude gebruikt het als hint."""
    import re as _re
    word_to_sources: dict[str, set[str]] = {}
    for it in items:
        words = {
            w.lower() for w in _re.findall(r"[A-Za-z]{4,}", it["title"])
            if w.lower() not in _STOPWORDS
        }
        for w in words:
            word_to_sources.setdefault(w, set()).add(it["source"])

    trending = [
        {"keyword": w, "source_count": len(srcs), "sources": sorted(srcs)}
        for w, srcs in word_to_sources.items()
        if len(srcs) >= 2
    ]
    trending.sort(key=lambda t: (-t["source_count"], t["keyword"]))
    return trending[:3]
