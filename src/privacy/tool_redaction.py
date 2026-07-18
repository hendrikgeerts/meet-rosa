"""Helpers voor redactie in de tool-use loop (Niveau B).

In de orchestrator-loop krijgen we van Claude tool_use blocks waarvan de
`.input` (dict) placeholders kan bevatten — Claude zag een geredacteerde
context, dus zijn `gmail_send(to="[EMAIL_001]")`-call moet lokaal
gereconstrueerd worden naar `to="piet@klant.nl"` voordat Gmail het ziet.

`has_unresolved_placeholders` is de safety-net: als Claude per ongeluk een
placeholder verzint die niet in de mapping zit (bv. `[EMAIL_999]` zonder
voorgaand gebruik), reconstructie laat hem letterlijk staan en we sturen
géén `to="[EMAIL_999]"` naar Gmail. Beter: skip de tool-call met een
foutmelding terug naar Claude.

Pure helpers — geen state, geen network.
"""
from __future__ import annotations

import re
from typing import Any

# Same shape as Redactor placeholders. Categories:
#  PERSON, ORG, EMAIL, PHONE, IBAN, URL, AMOUNT, PROJECT, DATE, ADDRESS
_PLACEHOLDER_RE = re.compile(
    r"\[(?:PERSON|ORG|EMAIL|PHONE|IBAN|URL|AMOUNT|PROJECT|DATE|ADDRESS)_\d+\]"
)


def reconstruct_value(value: Any, mapping: dict[str, str]) -> Any:
    """Recursively replace placeholders in a JSON-style value (str / list / dict).

    Sorts placeholders longest-first so [PERSON_10] isn't broken by [PERSON_1]'s
    replacement (mirroring `privacy.reconstructor.reconstruct`)."""
    if isinstance(value, str):
        if not mapping:
            return value
        out = value
        for placeholder in sorted(mapping, key=len, reverse=True):
            out = out.replace(placeholder, mapping[placeholder])
        return out
    if isinstance(value, list):
        return [reconstruct_value(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: reconstruct_value(v, mapping) for k, v in value.items()}
    return value


def has_unresolved_placeholders(value: Any) -> bool:
    """Walk the value tree and return True if any string contains a `[CAT_NNN]`
    pattern that survived reconstruction. That means Claude either invented a
    placeholder we never minted, or our mapping was incomplete — either way
    we must not send the value to a real-world tool."""
    if isinstance(value, str):
        return bool(_PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(has_unresolved_placeholders(v) for v in value)
    if isinstance(value, dict):
        return any(has_unresolved_placeholders(v) for v in value.values())
    return False
