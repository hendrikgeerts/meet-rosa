"""Curated source-lijsten per intel-domein.

Per source: naam + RSS-URL + (optioneel) host-filter voor catch-all feeds.
Sommige feeds zijn breed (bv. The Verge) — een `keywords_filter` zorgt
dat alleen relevante items doorkomen vóór scoring (scheelt llama-cycles).

Houd deze lijst conservatief — minder hoog-signaal feeds > veel ruis.
the user kan via de roadmap-tool nieuwe sources voorstellen.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSource:
    name: str
    url: str
    domain: str                              # 'digital_signage' | 'ai_models'
    keywords_filter: tuple[str, ...] = ()    # leeg = alle items door


# --- Digital Signage / Narrowcasting --------------------------------------
DIGITAL_SIGNAGE_SOURCES: tuple[MarketSource, ...] = (
    MarketSource(
        name="Invidis",
        url="https://invidis.com/feed/",
        domain="digital_signage",
    ),
    MarketSource(
        name="Sixteen:Nine",
        url="https://www.sixteen-nine.net/feed/",
        domain="digital_signage",
    ),
    MarketSource(
        name="DailyDOOH",
        url="https://www.dailydooh.com/feed",
        domain="digital_signage",
    ),
    MarketSource(
        name="Digital Signage Today",
        url="https://www.digitalsignagetoday.com/rss/",
        domain="digital_signage",
    ),
    MarketSource(
        name="AVIXA",
        url="https://www.avixa.org/feed",
        domain="digital_signage",
    ),
    MarketSource(
        name="r/DigitalSignage",
        url="https://www.reddit.com/r/DigitalSignage/.rss",
        domain="digital_signage",
    ),
)


# --- AI / nieuwe modellen -------------------------------------------------
# Brede feeds krijgen keyword-filter zodat alleen AI-relevante items
# door de scoring-laag gaan.
_AI_KEYWORDS = (
    "ai", "llm", "model", "claude", "gpt", "gemini", "llama",
    "mistral", "anthropic", "openai", "deepmind", "agent", "rag",
    "reasoning", "machine learning", "transformer", "embedding",
)

AI_SOURCES: tuple[MarketSource, ...] = (
    MarketSource(
        name="Anthropic News",
        url="https://www.anthropic.com/news/rss.xml",
        domain="ai_models",
    ),
    MarketSource(
        name="OpenAI Blog",
        url="https://openai.com/news/rss.xml",
        domain="ai_models",
    ),
    MarketSource(
        name="Simon Willison",
        url="https://simonwillison.net/atom/everything/",
        domain="ai_models",
    ),
    MarketSource(
        name="Hacker News (front)",
        url="https://hnrss.org/frontpage?points=150",
        domain="ai_models",
        keywords_filter=_AI_KEYWORDS,
    ),
    MarketSource(
        name="The Verge",
        url="https://www.theverge.com/rss/index.xml",
        domain="ai_models",
        keywords_filter=_AI_KEYWORDS,
    ),
    MarketSource(
        name="TechCrunch AI",
        url="https://techcrunch.com/category/artificial-intelligence/feed/",
        domain="ai_models",
    ),
    MarketSource(
        name="Hugging Face Papers",
        url="https://huggingface.co/papers.atom",
        domain="ai_models",
    ),
)


# --- Press / mentions monitoring ------------------------------------------
# Google News RSS query-API. NL+EN gecombineerd via gl=NL/en. Per
# zoekterm één feed; resultaten landen in domain='press_mentions' zodat
# de digest ze in een aparte sectie kan tonen.
def _google_news_feed(query: str, *, hl: str = "nl", gl: str = "NL") -> str:
    import urllib.parse as _up
    q = _up.quote_plus(f'"{query}"')
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}"


PRESS_MENTION_SOURCES: tuple[MarketSource, ...] = (
    MarketSource(
        name='Google News — "YourCompany"',
        url=_google_news_feed("YourCompany"),
        domain="press_mentions",
    ),
    MarketSource(
        name='Google News — "YourHolding"',
        url=_google_news_feed("YourHolding"),
        domain="press_mentions",
    ),
    MarketSource(
        name='Google News — "the user Geerts"',
        url=_google_news_feed("the user Geerts"),
        domain="press_mentions",
    ),
)


ALL_SOURCES: tuple[MarketSource, ...] = (
    DIGITAL_SIGNAGE_SOURCES + AI_SOURCES + PRESS_MENTION_SOURCES
)


# the user's company-context die in de scoring-prompt mee gaat. Bewust
# kort gehouden — Llama hoeft niet z'n hele bio te kennen, alleen genoeg
# om opportunity-flags te kunnen leggen.
COMPANY_CONTEXT = {
    "digital_signage": (
        "YourCompany: Nederlands SaaS-bedrijf dat content-templates "
        "levert voor digital-signage / narrowcasting platformen "
        "(SignageOS, BrightSign, Samsung MagicInfo). the user is CEO. "
        "Marktkansen = nieuwe DS-platforms, partnership-mogelijkheden, "
        "klant-pain rond template-creatie, regelgeving (RGPD/EAA), "
        "concurrentbewegingen, en nieuwe verticals (retail, hospitality, "
        "transit, corporate)."
    ),
    "ai_models": (
        "YourHolding: AI-gericht venture-vehikel van the user. "
        "Marktkansen = nieuwe modellen die aanzienlijk goedkoper / "
        "sneller / capabeler zijn dan vorige generatie, nieuwe AI-"
        "tooling-categorieën (agents, voice, multimodaal), opmerkelijke "
        "M&A, lanceringen die pricing-druk veroorzaken, en open-source "
        "doorbraken die zelfhost-deployment praktisch maken."
    ),
    "press_mentions": (
        "Press / online mentions van the user's bedrijven (YourCompany, "
        "YourHolding) of the user zelf. Relevantie = direct over zijn "
        "bedrijven gaan (interview, partnership, klant-case, persbericht), "
        "NIET indirecte vermeldingen of namesakes. Geen 'opportunity' "
        "in de M&A-zin — wel signaal voor brand awareness."
    ),
}
