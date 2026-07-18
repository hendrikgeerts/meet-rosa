"""Wish-audit: detecteer chat-uitspraken die op een config-wish lijken
maar die nooit via `add_config_wish` zijn vastgelegd.

Achtergrond: memory `feedback_agent_honest_persistence.md` documenteert
dat Rosa soms "Genoteerd!" zegt zonder de tool aan te roepen. De
SYSTEM_PROMPT zegt nu wel "altijd opslaan", maar als safety-net scant
deze module dagelijks de chat-history en flagt wish-achtige uitspraken
zonder bijbehorende wish-row, zodat the user in de dayclose ziet welke
preferences mogelijk ondergesneeuwd zijn.

Patronen heuristisch (NL+EN). False positives zijn beter dan false
negatives — the user kan dismissen in chat. Eén message geeft max één
candidate (eerste matchende pattern stopt de scan voor die rij), zelfs
als 'kun je voortaan X en onthoud dat Y' twee distinct preferences
bevat — bewuste keuze, anders krijg je dubbele entries voor dezelfde
quote.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Heuristische patronen die structurele preferences signaleren.
# Gericht op imperatieve/wens-vormen, niet op concrete ad-hoc requests.
_WISH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bkun je (voortaan|altijd|standaard)\b", re.IGNORECASE),
    re.compile(r"\bgraag voortaan\b", re.IGNORECASE),
    re.compile(r"\bik wil dat je (voortaan|altijd|standaard)\b", re.IGNORECASE),
    re.compile(r"\bvoortaan moet je\b", re.IGNORECASE),
    re.compile(r"\bonthoud (dat|even dat|even)\b", re.IGNORECASE),
    re.compile(r"\bdoe (dat|dit) altijd\b", re.IGNORECASE),
    re.compile(r"\bplease always\b", re.IGNORECASE),
    re.compile(r"\bfrom now on\b", re.IGNORECASE),
    re.compile(r"\bremember (that|to)\b", re.IGNORECASE),
]

# Hoe vroeg vóór/na een user-message een wish-insert nog meetelt als
# "gecovered". 1 uur dekt de orchestrator-tick + eventuele extra
# clarifying turns ruim.
_COVERAGE_WINDOW_SECONDS = 3600


def find_unrecorded_wish_candidates(
    db_path: Path, *,
    since: datetime, until: datetime,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return user-messages die wish-patronen matchen maar GEEN bijbehorende
    config_wishes row hebben binnen het coverage-window.

    Output per item: {at, content_excerpt, pattern_hint}. content_excerpt
    is geclipt op 200 chars zodat dayclose-prompt niet ontploft.
    """
    since_ts = int(since.timestamp())
    until_ts = int(until.timestamp())
    out: list[dict[str, Any]] = []
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        try:
            user_rows = conn.execute(
                "SELECT content, created_at FROM conversation_turns "
                "WHERE role='user' AND created_at >= ? AND created_at < ? "
                "ORDER BY created_at ASC",
                (since_ts, until_ts),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        # Pak alle wish-inserts binnen window+buffer; in-memory matching
        # is goedkoper dan een per-message subquery.
        try:
            wish_rows = conn.execute(
                "SELECT created_at FROM config_wishes "
                "WHERE created_at >= ? AND created_at < ?",
                (since_ts - _COVERAGE_WINDOW_SECONDS,
                 until_ts + _COVERAGE_WINDOW_SECONDS),
            ).fetchall()
            wish_times: list[int] = sorted(int(r[0]) for r in wish_rows)
        except sqlite3.OperationalError:
            wish_times = []

    for r in user_rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        hit_pattern: str | None = None
        for pat in _WISH_PATTERNS:
            m = pat.search(content)
            if m:
                hit_pattern = m.group(0)
                break
        if hit_pattern is None:
            continue
        msg_ts = int(r["created_at"])
        if _covered(msg_ts, wish_times):
            continue
        out.append({
            "at": datetime.fromtimestamp(msg_ts, since.tzinfo).isoformat(),
            "content_excerpt": content[:200],
            "pattern_hint": hit_pattern,
        })
        if len(out) >= limit:
            break
    return out


def _covered(msg_ts: int, wish_times: list[int]) -> bool:
    """O(log n) binary-search-achtig: is er een wish binnen het
    coverage-window van deze message?"""
    if not wish_times:
        return False
    lo = msg_ts - 60  # tot 1 min vóór mag (clock-skew)
    hi = msg_ts + _COVERAGE_WINDOW_SECONDS
    # Klein lineair scan — wish_times is sort + meestal kort (<20/dag).
    for t in wish_times:
        if lo <= t <= hi:
            return True
        if t > hi:
            return False
    return False
