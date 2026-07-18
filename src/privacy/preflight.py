"""Pre-flight regex scan: laatste sanity check op een redacted payload
voordat hij de machine verlaat.

Spec: PRIVACY_LAYER §5.1. Als IBAN/email/URL nog vóórkomt in een tekst
die naar de externe LLM zou gaan, dan is de redactor faalbaar — abort de
call en log het. De regex set is een **strikte deelverzameling** van die
in `redactor.py` (hier laten we phone/bedrag uit omdat die te veel false
positives geven op de redacted output).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS = {
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "url": re.compile(r"https?://[^\s<>\"')]+"),
}


@dataclass(frozen=True)
class PreflightHit:
    category: str
    sample: str


class PreflightFailure(Exception):
    def __init__(self, hit: PreflightHit) -> None:
        super().__init__(f"pre-flight: {hit.category} survived redaction: {hit.sample!r}")
        self.hit = hit


def scan(text: str) -> None:
    """Raises `PreflightFailure` on the first surviving PII pattern."""
    for cat, rx in _PATTERNS.items():
        m = rx.search(text)
        if m:
            raise PreflightFailure(PreflightHit(category=cat, sample=m.group(0)[:60]))
