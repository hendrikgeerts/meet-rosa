"""RSS-fetcher + description-parser voor faillissementsdossier.nl.

De feed is RSS 2.0; per <item> bevat <description> een vaste-format
NL-tekst zoals:

    "Fresh Food World B.V. te Tynaarlo (Drenthe) is door de rechtbank
    in Noord-Nederland failliet verklaard. Als curator is aangesteld
    mr J.M. Pol. Het insolventienummer van deze zaak is F.18/26/145.
    De (hoofd)activiteit van Fresh Food World B.V. is groothandel en
    handelsbemiddeling (niet in auto's en motorfietsen). Er zijn
    (nog) geen verslagen beschikbaar.<br><br>Status: Faillissement |
    KvK nummer: 62457756 | Plaats: Tynaarlo"

Best-effort regex-parsing; failed fields = None i.p.v. crash. Onbekende
varianten verschijnen in de DB met de raw description voor latere
diagnose, zonder de poller te breken.
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from core.external_audit import timed_call

from .schema import normalize_kvk

log = logging.getLogger(__name__)


FEED_URL = "https://www.faillissementsdossier.nl/nl/rss/nieuwe-faillissementen.aspx"
import os as _os

_OPERATOR_CONTACT = _os.environ.get("UPTIME_OPERATOR_CONTACT", "").strip()
_USER_AGENT = (
    f"rosa-insolvencies/1.0 (+mailto:{_OPERATOR_CONTACT})"
    if _OPERATOR_CONTACT
    else "rosa-insolvencies/1.0"
)
_DEFAULT_TIMEOUT = 20.0


class FaillissementsFeedError(RuntimeError):
    """Netwerk/parsing-fout bij het ophalen of decoderen van de feed."""


@dataclass(frozen=True)
class InsolvencyItem:
    """Eén RSS-item, geparsed naar gestructureerde velden."""
    link: str
    naam: str
    pub_date: str           # RFC2822 raw uit RSS (audit-trail)
    description_raw: str
    pub_at_unix: int = 0    # H4: unix-seconds voor sorteren; 0 als parse faalt

    # Best-effort geëxtraheerd; None = niet gevonden in de NL-tekst.
    kvk: str | None = None  # genormaliseerd via normalize_kvk (M2)
    plaats: str | None = None
    provincie: str | None = None
    rechtbank: str | None = None
    curator: str | None = None
    insolventie_nr: str | None = None
    status: str | None = None
    hoofd_activiteit: str | None = None


def _pubdate_to_unix(rfc2822: str | None) -> int:
    """RFC2822 (RSS pubDate) → unix-seconds. 0 bij parse-fail of empty."""
    if not rfc2822:
        return 0
    try:
        dt = parsedate_to_datetime(rfc2822)
    except (TypeError, ValueError):
        return 0
    if dt is None:
        return 0
    return int(dt.timestamp())


# ---- HTTP --------------------------------------------------------------

def fetch_feed(*, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Returnt de raw RSS-bytes. Audit-wrapped voor egress-tracking."""
    req = urllib.request.Request(
        FEED_URL,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml"},
    )
    with timed_call(service="faillissementsdossier",
                     endpoint="rss.nieuwe-faillissementen") as ctx:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                ctx.set(status=resp.status)
                return data
        except urllib.error.HTTPError as e:
            raise FaillissementsFeedError(f"HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise FaillissementsFeedError(f"URL error: {e}") from e
        except TimeoutError as e:
            raise FaillissementsFeedError("timeout") from e


# ---- parsing -----------------------------------------------------------

_RE_PLAATS_PROV  = re.compile(r"\bte\s+([^()]+?)\s*\(([^)]+)\)")
# Rechtbank: tot eerstvolgende werkwoord/onderbreking. Voorbeelden:
#   "rechtbank in Noord-Nederland failliet verklaard"      → "Noord-Nederland"
#   "rechtbank in Midden-Nederland surseance verleend"     → "Midden-Nederland"
#   "rechtbank in Den Haag"                                 → "Den Haag"
_RE_RECHTBANK    = re.compile(
    r"rechtbank\s+in\s+([\w-]+(?:\s+[A-Z][\w-]*)*?)"
    r"(?=\s+(?:failliet|surseance|de\s+schuldsanering|toepassing|verklaard|verleend|uitgesproken))",
    re.I,
)
# Curator: tot 'Het insolventienummer' of einde zin. 'mr J.M. Pol.' bevat
# zelf punten, dus '.' kan niet de stop zijn — gebruik lookahead.
_RE_CURATOR      = re.compile(
    r"(?:curator|bewindvoerder)\s+is\s+aangesteld\s+(.+?)"
    r"(?=\.\s+(?:Het\s+insolventienummer|De|Op|Er|<br|$))",
    re.I,
)
_RE_INSOLVENTIE  = re.compile(
    r"insolventienummer\s+van\s+deze\s+zaak\s+is\s+([\w./-]+?)\.\s",
    re.I,
)
_RE_HOOFDACT     = re.compile(
    r"\(hoofd\)activiteit\s+van\s+.+?\s+is\s+(.+?)\.\s+(?:Er zijn|De|Op|<br|$)",
    re.I | re.S,
)
_RE_STATUS       = re.compile(r"Status:\s*([\w-]+)", re.I)
_RE_KVK          = re.compile(r"KvK\s+nummer:\s*(\d+)", re.I)
_RE_PLAATS_TAIL  = re.compile(r"Plaats:\s*([^|<\n]+)", re.I)


def _strip_html(text: str) -> str:
    """Heel licht — feed gebruikt alleen <br>. Volledige HTML-parser
    zou overkill zijn en is een extra dependency."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)  # any other tags safety-net
    # XML-decode entities die Python's parser al heeft gedaan, plus
    # whitespace normaliseren
    return re.sub(r"\s+", " ", text).strip()


def parse_description(description: str) -> dict[str, str | None]:
    """Trek velden uit de NL-tekst. Velden die niet matchen = None."""
    text = _strip_html(description)
    fields: dict[str, str | None] = {
        "plaats": None, "provincie": None, "rechtbank": None,
        "curator": None, "insolventie_nr": None,
        "hoofd_activiteit": None, "status": None, "kvk": None,
    }

    m = _RE_PLAATS_PROV.search(text)
    if m:
        fields["plaats"] = m.group(1).strip()
        fields["provincie"] = m.group(2).strip()

    m = _RE_RECHTBANK.search(text)
    if m:
        fields["rechtbank"] = m.group(1).strip()

    m = _RE_CURATOR.search(text)
    if m:
        fields["curator"] = m.group(1).strip()

    m = _RE_INSOLVENTIE.search(text)
    if m:
        fields["insolventie_nr"] = m.group(1).strip()

    m = _RE_HOOFDACT.search(text)
    if m:
        fields["hoofd_activiteit"] = m.group(1).strip()

    m = _RE_STATUS.search(text)
    if m:
        fields["status"] = m.group(1).strip()

    m = _RE_KVK.search(text)
    if m:
        fields["kvk"] = m.group(1).strip()

    # Plaats uit footer wint als de te-clausule miste
    if fields["plaats"] is None:
        m = _RE_PLAATS_TAIL.search(text)
        if m:
            fields["plaats"] = m.group(1).strip()

    return fields


def parse_feed(xml_bytes: bytes) -> list[InsolvencyItem]:
    """RSS 2.0 → lijst InsolvencyItem. Items met ontbrekende link/title
    worden geskipt met een log-warning."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FaillissementsFeedError(f"RSS parse failed: {e}") from e

    items: list[InsolvencyItem] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            log.warning("insolv-feed: item without title/link skipped")
            continue
        description = (item.findtext("description") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        fields = parse_description(description)
        items.append(InsolvencyItem(
            link=link,
            naam=title,
            pub_date=pub_date,
            pub_at_unix=_pubdate_to_unix(pub_date),
            description_raw=description,
            kvk=normalize_kvk(fields["kvk"]),  # M2: opslag genormaliseerd
            plaats=fields["plaats"],
            provincie=fields["provincie"],
            rechtbank=fields["rechtbank"],
            curator=fields["curator"],
            insolventie_nr=fields["insolventie_nr"],
            status=fields["status"],
            hoofd_activiteit=fields["hoofd_activiteit"],
        ))
    return items


def fetch_and_parse(*, timeout: float = _DEFAULT_TIMEOUT) -> list[InsolvencyItem]:
    """Combineert fetch + parse."""
    raw = fetch_feed(timeout=timeout)
    return parse_feed(raw)
