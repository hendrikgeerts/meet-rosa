"""RSS-fetch voor market-intel sources.

Bewust dun gehouden — reuse feedparser pattern uit morning_extras/news.py
maar met schrijven naar market_items i.p.v. in-memory ranken. Elk item
krijgt status='new' zodat de scoring-laag later kan oppakken.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request

from core.external_audit import timed_call
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from extensions.market_intel.schema import MarketItem, insert_item
from extensions.market_intel.sources import MarketSource

log = logging.getLogger(__name__)

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def fetch_and_store(
    db_path: Path, sources: Iterable[MarketSource],
    *, per_source_cap: int = 20,
) -> int:
    """Fetch alle feeds, insert nieuwe items. Returns aantal toegevoegd."""
    added = 0
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        for src in sources:
            try:
                items = _fetch_one(src, cap=per_source_cap)
            except urllib.error.HTTPError as e:
                # Bad-URL feeds (404, 403, 410) zijn config-bugs, geen runtime-
                # incidenten. Log éénregel zonder full traceback om de log
                # leesbaar te houden. Verzamel later in STATUS welke feeds
                # vervangen moeten worden.
                log.info("market-intel: skip %s (HTTP %s)", src.name, e.code)
                continue
            except Exception as e:
                log.warning("market-intel: fetch failed for %s (%s)", src.name, type(e).__name__)
                continue
            for it in items:
                if insert_item(conn, it) is not None:
                    added += 1
    return added


def _fetch_one(src: MarketSource, *, cap: int) -> list[MarketItem]:
    import feedparser
    req = urllib.request.Request(src.url, headers={"User-Agent": "pa-agent/0.1"})
    # service-tag onderscheidt Google News van overige RSS voor latere queries.
    service = "google_news" if "news.google.com" in src.url else "rss"
    with timed_call(service=service, endpoint=f"GET {src.name}") as audit_ctx:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        audit_ctx.set(status=resp.status, bytes_in=len(raw))
    parsed = feedparser.parse(raw)
    out: list[MarketItem] = []
    for entry in parsed.entries[:cap]:
        title = _clean((getattr(entry, "title", "") or ""))
        url = (getattr(entry, "link", "") or "").strip()
        if not title or not url:
            continue

        # Keyword-filter voor brede feeds (Hacker News, The Verge):
        # laat alleen items door waarvan titel of summary een keyword bevat.
        if src.keywords_filter:
            haystack = (title + " " + _clean(getattr(entry, "summary", ""))).lower()
            if not any(kw in haystack for kw in src.keywords_filter):
                continue

        out.append(MarketItem(
            domain=src.domain,
            source=src.name,
            title=title[:300],
            url=url,
            author=_clean(getattr(entry, "author", "")) or None,
            published_at=_entry_unix(entry),
            snippet=_clean(getattr(entry, "summary", ""))[:500] or None,
        ))
    return out


def _clean(text: Any) -> str:
    if not text:
        return ""
    s = _HTML_TAG.sub(" ", str(text))
    return _WHITESPACE.sub(" ", s).strip()


def _entry_unix(entry: Any) -> int | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    try:
        return int(datetime(*parsed[:6], tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None
