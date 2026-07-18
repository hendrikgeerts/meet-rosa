"""Nieuws-feeds + lokale Llama-rank voor de ochtendbriefing.

Fetcht alle geconfigureerde RSS-feeds, filtert op leeftijd, vraagt het
lokale Llama-model welke top-N het meest relevant zijn voor the user's
interesses, en returnt een lijst met `{title, source, link, why}`.

Geen externe LLM-call — Claude ziet alleen de uiteindelijke top-N met
korte why-uitleg via de bestaande briefing-prompt (privacy-gateway pakt dat).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from models.ollama import OllamaClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    domain: str = ""


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    domain: str
    published_unix: int
    summary: str = ""


@dataclass(frozen=True)
class RankedNewsItem:
    title: str
    source: str
    link: str
    why: str


@dataclass
class NewsBundle:
    items: list[RankedNewsItem] = field(default_factory=list)
    fetched_count: int = 0
    feed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "headlines": [
                {"title": i.title, "source": i.source, "link": i.link, "why": i.why}
                for i in self.items
            ],
            "fetched_count": self.fetched_count,
            "feed_count": self.feed_count,
        }


def fetch_news_bundle(
    *,
    feeds: list[FeedConfig],
    interests: list[str],
    top_n: int,
    max_age_hours: int,
    ollama: OllamaClient,
) -> NewsBundle:
    """Pipeline: fetch feeds → filter age → rank via Ollama → top-N."""
    items: list[NewsItem] = []
    cutoff = time.time() - max_age_hours * 3600
    for feed in feeds:
        try:
            items.extend(_fetch_one(feed, cutoff_unix=cutoff))
        except Exception:
            log.warning("rss fetch failed for %s", feed.name, exc_info=True)

    if not items:
        return NewsBundle(feed_count=len(feeds))

    # De-dupe op normalized title (verschillende bronnen rapporteren
    # vaak hetzelfde nieuws).
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for it in items:
        key = re.sub(r"\W+", "", it.title.lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    # Sort newest-first; we beperken de lijst die naar Ollama gaat.
    unique.sort(key=lambda i: i.published_unix, reverse=True)
    candidates = unique[: max(top_n * 6, 30)]

    ranked = _rank_via_ollama(candidates, interests, top_n, ollama)
    return NewsBundle(items=ranked, fetched_count=len(unique), feed_count=len(feeds))


# --- RSS fetch ------------------------------------------------------------

def _fetch_one(feed: FeedConfig, *, cutoff_unix: float) -> list[NewsItem]:
    import feedparser
    from extensions.morning_extras._http import fetch_with_retry
    raw = fetch_with_retry(feed.url, timeout=10)
    if raw is None:
        return []
    parsed = feedparser.parse(raw)
    out: list[NewsItem] = []
    for e in parsed.entries[:30]:
        published = _entry_unix(e)
        if published and published < cutoff_unix:
            continue
        title = (getattr(e, "title", "") or "").strip()
        if not title:
            continue
        out.append(NewsItem(
            title=title,
            source=feed.name,
            link=(getattr(e, "link", "") or "").strip(),
            domain=feed.domain,
            published_unix=published or int(time.time()),
            summary=(getattr(e, "summary", "") or "").strip()[:300],
        ))
    return out


def _entry_unix(entry: Any) -> int | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    try:
        return int(datetime(*parsed[:6], tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


# --- Llama rank -----------------------------------------------------------

_RANK_SYSTEM = (
    "Je bent een nieuws-curator voor één persoon. Je ontvangt een lijst "
    "headlines van vandaag plus de interesses van de lezer. Je selecteert "
    "uit die lijst de N belangrijkste items voor deze lezer. Je antwoord "
    "is uitsluitend geldige JSON, geen extra tekst, geen code-fences."
)

_RANK_USER_TMPL = """Lezer-interesses: {interests}

Headlines (genummerd):
{listing}

Selecteer de TOP-{n} headlines die voor deze lezer vandaag het belangrijkst
zijn. Geef per item terug:
- "index": het nummer uit de lijst
- "why": ÉÉN zin Nederlands over waarom dit relevant is voor de lezer

JSON-output (lijst van objecten):
[{{"index": 1, "why": "..."}}, ...]"""


def _rank_via_ollama(
    candidates: list[NewsItem], interests: list[str], top_n: int, ollama: OllamaClient,
) -> list[RankedNewsItem]:
    if not candidates:
        return []
    listing = "\n".join(
        f"{i+1}. [{c.domain or c.source}] {c.title}" for i, c in enumerate(candidates)
    )
    prompt = _RANK_USER_TMPL.format(
        interests=", ".join(interests) or "algemeen nieuws",
        listing=listing,
        n=top_n,
    )
    try:
        response = ollama.chat(
            system=_RANK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
        )
    except Exception:
        log.exception("ollama news-rank failed; falling back to newest-N")
        return [
            RankedNewsItem(title=c.title, source=c.source, link=c.link,
                           why="(ranking onbeschikbaar — recentste headline)")
            for c in candidates[:top_n]
        ]

    text = (response.content[0].text if response.content else "") or ""
    selections = _parse_rank_output(text, top_n)
    if not selections:
        log.warning("could not parse Ollama rank-output: %s", text[:200])
        return [
            RankedNewsItem(title=c.title, source=c.source, link=c.link,
                           why="(ranking-output onleesbaar)")
            for c in candidates[:top_n]
        ]

    out: list[RankedNewsItem] = []
    for sel in selections[:top_n]:
        idx = sel.get("index")
        if not isinstance(idx, int) or idx < 1 or idx > len(candidates):
            continue
        c = candidates[idx - 1]
        out.append(RankedNewsItem(
            title=c.title, source=c.source, link=c.link,
            why=str(sel.get("why", ""))[:200],
        ))
    return out


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ARRAY = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)


def _parse_rank_output(text: str, _n: int) -> list[dict[str, Any]]:
    s = text.strip()
    f = _FENCE.search(s)
    if f:
        s = f.group(1).strip()
    candidates = []
    if s.startswith("[") and s.endswith("]"):
        candidates.append(s)
    candidates.extend(_ARRAY.findall(s))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return []


# --- yaml loader ----------------------------------------------------------

def load_morning_extras_config(yaml_path) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """Load config/morning_extras.yaml. Returns None if file missing."""
    from pathlib import Path
    import yaml
    p = Path(yaml_path)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def parse_feeds(cfg: dict[str, Any]) -> list[FeedConfig]:
    out = []
    for f in cfg.get("news_feeds") or []:
        if not f.get("url"):
            continue
        out.append(FeedConfig(
            name=str(f.get("name", "?")),
            url=str(f["url"]),
            domain=str(f.get("domain", "")),
        ))
    return out
