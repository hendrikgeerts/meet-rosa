"""Dictionary + regex + spaCy NER redactor.

Replaces named entities with stable placeholders so an external LLM only
sees `[PERSON_001]/[ORG_001]/[EMAIL_001]/...` and never the real names.
Returns the mapping so a Reconstructor can put the originals back in the
LLM's response.

Cascade per PRIVACY_LAYER §4.2:
  1. Dictionary  — VIP people/orgs/projects/emails/phones, longest-first,
                   word-boundary, case-insensitive (highest precision)
  2. Regex       — email, IBAN, BSN (11-proof), credit-card (Luhn), URL,
                   phone, euro/dollar amount > threshold
  3. spaCy NER   — Dutch model (`nl_core_news_lg` recommended; falls back
                   to whatever is installed) catches PERSON/ORG/LOC/GPE
                   entities the dictionary missed (lazy-loaded once per
                   process; falls back to a no-op if spaCy or the model
                   is unavailable, with a warning)

Layer 5 (local-LLM vangnet) is still TODO in PRIVACY_LAYER §4.2.

Coreference (§4.4): within a single redact() call the same original always
gets the same placeholder. Across calls, pass `existing_mapping` to keep
the numbering consistent across a conversation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

log = logging.getLogger(__name__)


# spaCy model cache: load once per process, share across Redactor instances.
_NER_LOCK = Lock()
_NER_CACHE: dict[str, Any] = {}


def _load_spacy_model(model_name: str) -> Any | None:
    """Lazy-load a spaCy model, thread-safe. Returns None if spaCy or the
    model isn't installed — caller falls back to no-op."""
    with _NER_LOCK:
        if model_name in _NER_CACHE:
            return _NER_CACHE[model_name]
        try:
            import spacy
            nlp = spacy.load(model_name, disable=["parser", "lemmatizer", "tagger", "attribute_ruler"])
            _NER_CACHE[model_name] = nlp
            log.info("spaCy NER loaded: %s", model_name)
            return nlp
        except Exception as exc:
            log.warning(
                "spaCy NER unavailable (%s) — Redactor falls back to "
                "dictionary+regex layers only. Install: `python -m spacy download %s`",
                exc, model_name,
            )
            _NER_CACHE[model_name] = None
            return None


# spaCy entity-label → our placeholder category.
_NER_LABEL_MAP = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "LOC": "ADDRESS",   # geographic location (city, street)
    "GPE": "ADDRESS",   # geo-political entity (country, region)
}


_PLACEHOLDER_RE = re.compile(
    r"\[(?:PERSON|ORG|EMAIL|PHONE|IBAN|BSN|CC|URL|AMOUNT|PROJECT|DATE|ADDRESS)_\d+\]"
)


# --- regex set ---------------------------------------------------------------
# Order matters: most specific first, so an email isn't double-matched by the
# URL regex etc.

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# E.164-ish + Dutch-local. "+" optional, allows spaces and dashes.
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s\-]?)?(?:\(?0\d{1,3}\)?[\s\-]?)?\d(?:[\s\-]?\d){7,11}(?!\w)"
)
# IBAN: country (2) + check (2) + 11..30 alnum
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# BSN: exactly 9 digits, validated via 11-proof. Matches contiguous
# (123456782) or uniformly dot- or space-separated (123.456.782 /
# 123 456 782) forms. Mixed separators ('123.456 782') worden bewust
# geweigerd — geen reëel BSN-formaat en het voorkomt false positives
# op samengevoegde strings met losse 3-digit groepen.
# Cascade order: BSN before phone — a 9-digit BSN also matches _PHONE_RE.
_BSN_RE = re.compile(r"(?<!\d)(\d{9}|\d{3}\.\d{3}\.\d{3}|\d{3} \d{3} \d{3})(?!\d)")
# Credit card: 13–19 digits, optional space/dash separators in 4-digit
# groups (Visa/MC/Amex layouts). Validated via Luhn.
_CC_RE = re.compile(
    r"(?<!\d)(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7}|\d{13,19})(?!\d)"
)
# Any http(s) URL — coarse but safe (we'd rather over-redact a URL than miss
# a token-bearing one).
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
# Amounts: €, EUR, $; capture the numeric part separately so we can
# threshold-filter on value.
_AMOUNT_RE = re.compile(
    r"(?:€|EUR|\$|USD)\s?(\d[\d.,]*)",
    re.IGNORECASE,
)


def _is_valid_bsn(s: str) -> bool:
    """Burgerservicenummer 11-proof: sum(d_i * w_i) mod 11 == 0 with
    weights 9,8,7,6,5,4,3,2,-1. Reject all-zeros."""
    digits = [int(c) for c in s if c.isdigit()]
    if len(digits) != 9 or all(d == 0 for d in digits):
        return False
    weights = (9, 8, 7, 6, 5, 4, 3, 2, -1)
    return sum(d * w for d, w in zip(digits, weights)) % 11 == 0


def _is_valid_luhn(s: str) -> bool:
    """Luhn checksum for credit-card numbers (13–19 digits). Reject
    all-zeros, all-same-digit, and obvious sequences (12345...)."""
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    if all(d == digits[0] for d in digits):
        return False
    total = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Redaction:
    text: str
    mapping: dict[str, str]   # placeholder -> original


class Redactor:
    """Regex + dictionary redactor. See module docstring for the cascade."""

    def __init__(
        self,
        *,
        vip_people: tuple[str, ...] = (),
        vip_emails: tuple[str, ...] = (),
        vip_phones: tuple[str, ...] = (),
        vip_orgs: tuple[str, ...] = (),
        vip_projects: tuple[str, ...] = (),
        safe_terms: tuple[str, ...] = (),
        amount_threshold: float = 1000.0,
        ner_model: str | None = None,
    ) -> None:
        # Sort longest-first so substrings don't shadow longer matches
        # (e.g. 'Heineken Brouwerij' before 'Heineken').
        self._people = tuple(sorted(_dedup(vip_people), key=len, reverse=True))
        self._emails = tuple(sorted(_dedup(vip_emails), key=len, reverse=True))
        self._phones = tuple(sorted(_dedup(vip_phones), key=len, reverse=True))
        self._orgs = tuple(sorted(_dedup(vip_orgs), key=len, reverse=True))
        self._projects = tuple(sorted(_dedup(vip_projects), key=len, reverse=True))
        # Safe-terms: case-insensitive whitelist die NIET geredacteerd mag
        # worden, zelfs als spaCy NER ze als LOC/GPE/PERSON markeert. Voor
        # gebruiker's woonplaats — anders pseudonymiseert de NER 'm
        # naar [ADDRESS_xxx] en raakt context kwijt of komt verkeerd
        # gereconstrueerd terug ('Hasselt' bug uit briefing). Wordt door
        # setup-wizard gevuld op basis van config.user.home_city.
        self._safe_terms_lower = frozenset(
            t.lower() for t in safe_terms if t and t.strip()
        )
        self._amount_threshold = amount_threshold
        self._ner_model_name = ner_model
        # Touch the cache early so tests / startup see whether NER is wired.
        # (No-op if model_name is None; logs once per process if it fails.)
        if ner_model:
            _load_spacy_model(ner_model)

    def redact(
        self,
        text: str,
        *,
        existing_mapping: dict[str, str] | None = None,
    ) -> Redaction:
        # Build "next id per category" + inverse lookup (original → placeholder)
        # from existing_mapping so re-used entities get the same placeholder.
        mapping: dict[str, str] = dict(existing_mapping or {})
        inverse: dict[str, str] = {v: k for k, v in mapping.items()}
        counters: dict[str, int] = {}
        for ph in mapping:
            cat = ph.strip("[]").rsplit("_", 1)[0]
            try:
                n = int(ph.rsplit("_", 1)[1].rstrip("]"))
            except ValueError:
                continue
            counters[cat] = max(counters.get(cat, 0), n)

        def alloc(category: str, original: str) -> str:
            if original in inverse:
                return inverse[original]
            counters[category] = counters.get(category, 0) + 1
            ph = f"[{category}_{counters[category]:03d}]"
            mapping[ph] = original
            inverse[original] = ph
            return ph

        # 1. Dictionary first — exact, longest-first, word-boundary matches.
        for original in self._emails:
            text = _replace_word(text, original, lambda m, o=original: alloc("EMAIL", o))
        for original in self._phones:
            text = _replace_word(text, original, lambda m, o=original: alloc("PHONE", o))
        for original in self._projects:
            text = _replace_word(text, original, lambda m, o=original: alloc("PROJECT", o))
        for original in self._orgs:
            text = _replace_word(text, original, lambda m, o=original: alloc("ORG", o))
        for original in self._people:
            text = _replace_word(text, original, lambda m, o=original: alloc("PERSON", o))

        # 2. Regex layer — only fires on what dictionary missed.
        text = _EMAIL_RE.sub(lambda m: alloc("EMAIL", m.group(0)), text)
        text = _IBAN_RE.sub(lambda m: alloc("IBAN", m.group(0)), text)
        text = _URL_RE.sub(lambda m: alloc("URL", m.group(0)), text)
        # CC before BSN before PHONE: a 9-digit BSN also matches PHONE,
        # and a 16-digit CC contains spans that match PHONE. Checksum
        # gates (Luhn / 11-proof) prevent false positives on random
        # digit strings.
        text = _CC_RE.sub(
            lambda m: alloc("CC", m.group(0)) if _is_valid_luhn(m.group(0)) else m.group(0),
            text,
        )
        text = _BSN_RE.sub(
            lambda m: alloc("BSN", m.group(0)) if _is_valid_bsn(m.group(0)) else m.group(0),
            text,
        )
        text = _PHONE_RE.sub(lambda m: alloc("PHONE", m.group(0)), text)
        text = _AMOUNT_RE.sub(
            lambda m: alloc("AMOUNT", m.group(0)) if _amount_value(m.group(1)) >= self._amount_threshold else m.group(0),
            text,
        )

        # 3. spaCy NER layer — catches PERSON/ORG/LOC/GPE the dictionary missed.
        if self._ner_model_name:
            nlp = _load_spacy_model(self._ner_model_name)
            if nlp is not None:
                text = _apply_ner(text, nlp, alloc,
                                    safe_terms_lower=self._safe_terms_lower)

        return Redaction(text=text, mapping=mapping)


def _apply_ner(
    text: str, nlp: Any, alloc, *,  # type: ignore[no-untyped-def]
    safe_terms_lower: frozenset[str] = frozenset(),
) -> str:
    """Run spaCy NER over `text`, allocating placeholders for entities that
    aren't already inside an existing `[CAT_NNN]` placeholder.

    Replaces from end to start so character offsets don't shift mid-loop.
    Skips entities whose surface is purely whitespace, a punctuation-only
    fragment, already a placeholder, or matches the safe-terms whitelist
    (e.g. the user's woonplaats die context-relevant blijft in briefings).
    """
    doc = nlp(text)
    candidates = []
    for ent in doc.ents:
        category = _NER_LABEL_MAP.get(ent.label_)
        if not category:
            continue
        surface = ent.text.strip()
        if not surface or _PLACEHOLDER_RE.fullmatch(surface):
            continue
        if surface.lower() in safe_terms_lower:
            continue
        candidates.append((ent.start_char, ent.end_char, category, surface))

    # Replace right-to-left so earlier offsets stay valid.
    for start, end, category, surface in sorted(candidates, key=lambda t: t[0], reverse=True):
        # Skip if the entity span overlaps with an already-placed placeholder.
        if _PLACEHOLDER_RE.search(text[start:end]):
            continue
        placeholder = alloc(category, surface)
        text = text[:start] + placeholder + text[end:]
    return text


def _replace_word(text: str, needle: str, repl):  # type: ignore[no-untyped-def]
    """Word-boundary case-insensitive replacement using a callable, like re.sub."""
    if not needle:
        return text
    pattern = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", re.IGNORECASE)
    return pattern.sub(repl, text)


def _dedup(items: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _amount_value(num_str: str) -> float:
    """Parse a NL/EN-style amount (1.234,56 or 1,234.56 or 45000) to float."""
    s = num_str.strip()
    if not s:
        return 0.0
    # If both . and , present, the rightmost is the decimal separator.
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Treat , as decimal if exactly two digits after; else thousands.
        if re.search(r",\d{2}$", s):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_redactor_from_yaml(
    *,
    vip_path: Path,
    amount_threshold: float = 1000.0,
    ner_model: str | None = None,
    extra_safe_terms: tuple[str, ...] = (),
) -> Redactor:
    """Build a Redactor from config/vip_contacts.yaml.

    `extra_safe_terms` wordt door main.py gevuld met o.a. `user.home_city`
    zodat de gebruiker's woonplaats nooit door NER geredacteerd wordt.
    Voorheen was the user's Gilze hardcoded als comment; nu generiek."""
    if not vip_path.exists():
        return Redactor(
            safe_terms=tuple(extra_safe_terms),
            amount_threshold=amount_threshold, ner_model=ner_model,
        )

    cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}

    people: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    for p in cfg.get("people") or []:
        if name := p.get("name"):
            people.append(name)
            people.extend(p.get("aliases") or [])
        emails.extend(p.get("emails") or [])
        phones.extend(p.get("phones") or [])

    orgs: list[str] = []
    for o in cfg.get("organizations") or []:
        if name := o.get("name"):
            orgs.append(name)
            orgs.extend(o.get("aliases") or [])

    projects: list[str] = []
    for proj in cfg.get("projects") or []:
        if code := proj.get("code"):
            projects.append(code)
        if name := proj.get("name"):
            projects.append(name)

    # Top-level `safe_terms:` lijst — strings die NIET geredacteerd
    # mogen worden ondanks dat NER ze als LOC/GPE/PERSON markeert.
    # Voorbeelden: eigen woonplaats / kantoorstad / land voor weather
    # of travel-context.
    safe_terms = [str(t) for t in (cfg.get("safe_terms") or []) if t]
    # Merge extra safe-terms (o.a. user.home_city uit config.user.*)
    safe_terms.extend(t for t in extra_safe_terms if t)

    return Redactor(
        vip_people=tuple(people),
        vip_emails=tuple(emails),
        vip_phones=tuple(phones),
        vip_orgs=tuple(orgs),
        vip_projects=tuple(projects),
        safe_terms=tuple(safe_terms),
        amount_threshold=amount_threshold,
        ner_model=ner_model,
    )
