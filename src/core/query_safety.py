"""Shared query-validation tegen prompt-injection-gedreven bulk-exfiltratie.

SECURITY_REVIEW_2 HIGH-4: een geinjecteerde mail kan Claude laten zoeken
op `%`/`_` (SQL-LIKE wildcards) of `*`/`'` zodat aggregatie-tools als
person_brief / comm_search / comm_about_person de hele comm_items-tabel
als tool_result teruggeven. Deze module geeft alle aggregatie-tools
dezelfde poortwachter: minimaal 3 alfanumerieke chars + geen wildcard-
karakters.

Gebruik:
    from core.query_safety import validate_query
    ok, err = validate_query(q)
    if not ok:
        return []  # or return {"error": err, "rejected": True}
"""
from __future__ import annotations

import re

_QUERY_BLOCKED_CHARS = frozenset("%_*'")
_MIN_QUERY_LEN = 3
_ALNUM_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]")

# JSON-schema fragment voor tool-inputs zodat Claude zelf al een schema-
# violation krijgt vóór de handler überhaupt draait.
QUERY_SCHEMA = {
    "type": "string",
    "minLength": _MIN_QUERY_LEN,
    "pattern": "^[^%_*']+$",
}


def validate_query(query: str) -> tuple[bool, str | None]:
    """Return (ok, error_message). Reject empty, too-short, or queries
    containing SQL-LIKE / shell wildcards. Also reject queries without
    at least one alphanumeric char (e.g. `   `, `...`, `---`)."""
    q = (query or "").strip()
    if len(q) < _MIN_QUERY_LEN:
        return False, f"query too short (min {_MIN_QUERY_LEN} chars)"
    if any(c in _QUERY_BLOCKED_CHARS for c in q):
        return False, "query contains forbidden wildcard chars (%, _, *, ')"
    if not _ALNUM_RE.search(q):
        return False, "query must contain at least one alphanumeric char"
    return True, None
