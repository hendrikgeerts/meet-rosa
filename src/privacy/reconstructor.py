"""Reconstructor — replace placeholders in the LLM's response with originals.

Pure local string substitution; no model, no network. Inverse of Redactor.

Handles two syntax variants Claude is observed to produce:
  - exact:           [PERSON_001]
  - parens-bare:     (PERSON_001)   ← happens when Claude paraphrases the
                                      placeholder ("een bevestiging van
                                      (PERSON_001) ontvangen")

Bare-without-any-delimiter `PERSON_001` is **not** auto-handled — too easy
to mismatch in normal narrative. Same for `([PERSON_001])` — outer parens
should stay (Claude may have wanted them for "(Heineken)" stylistic
reasons; we replace the inner `[PERSON_001]` to `Michelle` and the parens
remain naturally as `(Michelle)`).

Strategy: prompt Claude to keep brackets (gateway adds a hint), and only
handle the most common stripped-bracket variant here.

Placeholders are sorted longest-first so [PERSON_10] doesn't get partially
matched by a [PERSON_1] replacement.

Hallucinated-placeholder defense (toegevoegd 26/5 review):
  Production-payloads laten zien dat Claude ondanks de PLACEHOLDER-hint
  in ~16% van briefings placeholders verzint die NIET in de mapping
  zitten (typisch `[PERSON_001]`, `[ORG_001]`). Die bleven met de
  oude reconstruct() letterlijk staan in iMessages naar the user
  ("Meet: [URL_001]"). De nieuwe `reconstruct(..., strip_leftover=True)`
  detecteert die overgebleven `[CAT_NNN]` patterns en vervangt ze
  door een categorie-specifieke, niet-foutief-bewerende fallback
  ("someone", "an organization", "a link", …). Caller krijgt via de
  `ReconstructResult` ook een count zodat dit gemonitord kan worden
  (gateway-audit logt het als `hallucinated_placeholders`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Leftover-detection: matches [CAT_NNN] for the categories we ever produce.
_CATEGORIES = "PERSON|ORG|EMAIL|PHONE|IBAN|BSN|CC|URL|AMOUNT|PROJECT|DATE|ADDRESS"
_LEFTOVER_RE = re.compile(rf"\[({_CATEGORIES})_\d+\]")
# Claude paraphraseert soms `[PERSON_001]` naar `(PERSON_001)` — de
# bekende-mapping-fase handelt dat al af voor entities die in mapping
# zitten, maar voor gehallucineerde parens-variants is een tweede pas
# nodig (anders ontsnapt "Meet: (URL_001)" alsnog).
_LEFTOVER_PARENS_RE = re.compile(rf"\(({_CATEGORIES})_\d+\)")
# Claude stript soms beide brackets weg, vooral als de placeholder vlak
# na een ander `[X]` token staat (zoals "[A] [PERSON_021]" in
# VIP-aandacht-format) — Claude denkt 'twee brackets is teveel' en
# levert "[A] PERSON_021". Word-boundary regex om geen substring-matches
# in code-achtige tokens (`my_PERSON_021_field`) te raken.
_LEFTOVER_BARE_RE = re.compile(rf"\b({_CATEGORIES})_\d+\b")

# Per-category neutrale fallbacks — eerlijk over wat onbekend is zonder
# valse zekerheid te geven ("Marc" als we het echt niet weten).
_FALLBACKS: dict[str, str] = {
    "PERSON":  "someone",
    "ORG":     "an organization",
    "EMAIL":   "an email address",
    "PHONE":   "a phone number",
    "IBAN":    "a bank account",
    "BSN":     "a BSN",
    "CC":      "a card number",
    "URL":     "a link",
    "AMOUNT":  "an amount",
    "PROJECT": "a project",
    "DATE":    "a date",
    "ADDRESS": "a location",
}


@dataclass(frozen=True)
class ReconstructResult:
    """Output van reconstruct() — tekst plus debugging-info.

    `leftovers` lijst de categorieën van placeholders die Claude verzon
    (niet in mapping). Caller (gateway) logt dit naar de audit-stream
    zodat we hallucination-rates over de tijd kunnen volgen."""
    text: str
    leftovers: list[str]


def reconstruct(
    text: str,
    mapping: dict[str, str],
    *,
    strip_leftover: bool = True,
) -> str:
    """Replace each placeholder in `mapping` (and (PLACEHOLDER) variant)
    with its original value.

    With `strip_leftover=True` (default), any `[CAT_NNN]` that remains
    after mapping-replacement — i.e. Claude invented it — is replaced
    with a generic fallback ("someone", "a link", …). This prevents
    raw placeholders from leaking to the user.

    Use `reconstruct_with_info(...)` if you need the leftover-count for
    audit logging.
    """
    return reconstruct_with_info(text, mapping, strip_leftover=strip_leftover).text


def reconstruct_with_info(
    text: str,
    mapping: dict[str, str],
    *,
    strip_leftover: bool = True,
) -> ReconstructResult:
    """Same as reconstruct() but returns a `ReconstructResult` so callers
    can log how many placeholders Claude hallucinated."""
    if mapping:
        for placeholder in sorted(mapping, key=len, reverse=True):
            original = mapping[placeholder]
            # Exact form first; then the bare-parens variant Claude sometimes
            # emits when paraphrasing; then bare-without-delimiters with
            # word-boundary om "code-like" substring-matches te voorkomen.
            text = text.replace(placeholder, original)
            bare_parens = f"({placeholder.strip('[]')})"
            text = text.replace(bare_parens, original)
            bare = placeholder.strip("[]")
            text = re.sub(
                rf"\b{re.escape(bare)}\b",
                lambda _m, o=original: o,
                text,
            )

    if not strip_leftover:
        return ReconstructResult(text=text, leftovers=[])

    # Hallucinated-placeholder fallback. We scan for [CAT_NNN] AND
    # (CAT_NNN) — Claude soms paraphraseert naar parens-bare —
    # patterns that survived (i.e. were never in mapping). Replace
    # each with a category-appropriate generic. Log + count for
    # monitoring.
    leftovers: list[str] = []
    def _fallback(match: re.Match[str]) -> str:
        category = match.group(1)
        leftovers.append(category)
        return _FALLBACKS.get(category, "something")
    text = _LEFTOVER_RE.sub(_fallback, text)
    text = _LEFTOVER_PARENS_RE.sub(_fallback, text)
    text = _LEFTOVER_BARE_RE.sub(_fallback, text)

    if leftovers:
        # INFO niveau voor routine-hallucinaties (1-3 per call), WARNING
        # alleen als er veel zijn — dat is signal dat Claude écht
        # off-track is en de hint mogelijk verstevigd moet worden.
        level = logging.WARNING if len(leftovers) > 3 else logging.INFO
        log.log(
            level,
            "reconstruct: %d hallucinated placeholder(s) replaced with fallback "
            "(categories: %s)",
            len(leftovers), sorted(set(leftovers)),
        )

    return ReconstructResult(text=text, leftovers=leftovers)
