"""Pdf text extraction + Claude classification voor receipts."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.expenses.schema import CATEGORIES
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


_CLASSIFY_PROMPT = (
    "Je bent Rosa. Je krijgt de tekst van een factuur of bon. Extraheer "
    "de relevante velden. Antwoord ALLEEN met geldige JSON, geen extra tekst.\n\n"
    "Velden:\n"
    "- vendor: leverancier-naam (bv. 'Coolblue', 'Microsoft', 'Albert Heijn')\n"
    "- receipt_date: 'YYYY-MM-DD' of null als onduidelijk\n"
    "- amount: totaalbedrag inclusief BTW als float (bv. 49.99)\n"
    "- vat: BTW-bedrag als float, of 0 als geen BTW\n"
    "- currency: 'EUR'/'USD'/'GBP' (default 'EUR')\n"
    f"- category: één van {list(CATEGORIES)}\n"
    "- description: 1-zin wat is gekocht (max 200 tekens)\n"
    "- is_receipt: true/false — false als dit overduidelijk geen bon/factuur is\n"
    "- confidence: 0.0-1.0 hoe zeker je bent over de extractie"
)


def extract_text(pdf_path: Path, *, max_chars: int = 8000) -> str:
    """PDF → plain text via pypdf. Returns lege string bij parse-fout."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.error("pypdf niet geinstalleerd — `pip install pypdf`")
        return ""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        log.exception("pypdf kon %s niet openen", pdf_path.name)
        return ""
    parts: list[str] = []
    for page in reader.pages[:10]:  # cap op 10 pagina's
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            log.exception("pypdf page extract mislukt voor %s", pdf_path.name)
    text = "\n".join(parts).strip()
    return text[:max_chars]


def classify(
    text: str, *, gateway: Gateway, source_filename: str = "",
) -> dict[str, Any]:
    """Stuur tekst naar Claude voor extractie. Returns parsed dict."""
    if not text.strip():
        return {"is_receipt": False, "confidence": 0.0,
                "vendor": None, "amount": None}
    user_payload = (
        f"Bestand: {source_filename}\n\n"
        f"Tekst van de factuur/bon:\n{text}\n\n"
        "Geef de JSON."
    )
    # Force naar lokaal Llama: receipts bevatten vendor-namen, bedragen,
    # BTW-info — gevoelig genoeg dat we ze niet naar Claude egress willen.
    # Vereist dat de gateway een local_client (Ollama) wired heeft.
    response = gateway.complete(
        task="expense_classify",
        system=_CLASSIFY_PROMPT,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=400,
        force_label="confidential",
    )
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    return _parse_json(raw)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_BRACES = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if (m := _FENCE.search(s)):
        s = m.group(1).strip()
    if (m := _BRACES.search(s)):
        s = m.group(0)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        log.warning("expense classify: kon JSON niet parsen: %s", text[:200])
        return {"is_receipt": False, "confidence": 0.0}
    if not isinstance(data, dict):
        return {"is_receipt": False, "confidence": 0.0}
    return data


def parse_date(s: str | None) -> int | None:
    """ISO date → unix seconds. Returns None bij parse-fout."""
    if not s:
        return None
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=TZ)
        return int(d.timestamp())
    except (ValueError, TypeError):
        return None


def to_cents(amount: Any) -> int | None:
    """Float amount (eg 49.99) → 4999 cents."""
    if amount is None:
        return None
    try:
        return int(round(float(amount) * 100))
    except (ValueError, TypeError):
        return None
