"""Todoist cleanup-engine: duplicaten + stale-task detectie + voorstel-flow.

the user kan vragen "ruim m'n Todoist op". Rosa stelt voor:
  - Duplicaat-paren (hoge tekst-similariteit) → één van de twee sluiten
    of mergen tot één item.
  - Stale items (open >N dagen zonder due-date) → close of due-date geven.

We voeren NIETS automatisch uit. De `_suggest`-tool returnt voorgestelde
acties met een `proposal_id` per actie; the user bevestigt welke
proposal_ids hij wil uitvoeren via `_apply`. Confirmation-pattern
verkleint het risico dat een drift in de detector legitiem werk wist.

Similariteit is met `difflib.SequenceMatcher` ratio + token-Jaccard,
geen externe deps. Drempel default 0.82 op SequenceMatcher OF
Jaccard ≥ 0.7 — beide moeten kunnen triggeren omdat short tasks
(\"Bel verzekering\" vs \"verzekering bellen\") low op SequenceMatcher
zijn maar high op token-set.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from integrations.todoist import Task

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+")
_STOPWORDS = frozenset({
    # Korte fillers in NL/EN die similariteit niet mogen sturen.
    "de", "het", "een", "en", "of", "the", "a", "an", "to", "for", "in", "op", "met", "te", "aan", "and", "or", "is", "be", "voor",
    # Review 27/6 L5: NL/EN imperatives die elke task starten — zonder
    # filter boosten ze seq-similariteit kunstmatig ("Bel Jan" ≈ "Bel Piet"
    # via 'bel'). Content-woorden (Jan, Piet) horen te domineren.
    "bel", "bellen", "mail", "mailen", "stuur", "sturen",
    "check", "lees", "vraag", "ask", "call", "send", "read",
})


def _normalize_tokens(text: str) -> set[str]:
    tokens = {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}
    return tokens - _STOPWORDS


def _normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace voor SequenceMatcher."""
    return " ".join(_TOKEN_RE.findall((text or "").lower()))


def text_similarity(a: str, b: str) -> tuple[float, float]:
    """Return (seqmatch_ratio, jaccard) ∈ [0,1] × [0,1]."""
    na, nb = _normalize_text(a), _normalize_text(b)
    seq = SequenceMatcher(None, na, nb).ratio() if (na and nb) else 0.0
    ta, tb = _normalize_tokens(a), _normalize_tokens(b)
    if not ta or not tb:
        return seq, 0.0
    jac = len(ta & tb) / len(ta | tb)
    return seq, jac


@dataclass(frozen=True)
class DuplicateProposal:
    keep_id: str
    drop_id: str
    keep_content: str
    drop_content: str
    seq_ratio: float
    jaccard: float


@dataclass(frozen=True)
class StaleProposal:
    task_id: str
    content: str
    age_days: int
    has_due: bool


def find_duplicates(
    tasks: list[Task], *, seq_threshold: float = 0.78,
    jaccard_threshold: float = 0.6,
) -> list[DuplicateProposal]:
    """Vind paren met hoge tekst-similariteit. O(n²) — voor the user's
    paar honderd open tasks is dat prima.

    Voor elk paar: behoudt het oudste task (laagste created_at; bij gelijk
    behoudt het hoogste id). De andere wordt voorgesteld om te sluiten.
    Geen transitive grouping — als A~B~C krijg je twee paren in plaats van
    één triple, simpeler te reviewen.
    """
    out: list[DuplicateProposal] = []
    seen: set[tuple[str, str]] = set()
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a.id == b.id:
                continue
            key = tuple(sorted([a.id, b.id]))
            if key in seen:
                continue
            seq, jac = text_similarity(a.content, b.content)
            if seq >= seq_threshold or jac >= jaccard_threshold:
                keep, drop = _pick_keep_drop(a, b)
                out.append(DuplicateProposal(
                    keep_id=keep.id, drop_id=drop.id,
                    keep_content=keep.content, drop_content=drop.content,
                    seq_ratio=round(seq, 3), jaccard=round(jac, 3),
                ))
                seen.add(key)
    return out


def _pick_keep_drop(a: Task, b: Task) -> tuple[Task, Task]:
    """Keep = oudere (lager created_at) of degene mét due-date.
    Reden: een item met expliciete due-date is waarschijnlijk de bewust
    geplande versie; een duplicate zonder due-date is vaak een snelle
    re-entry.

    Review 27/6 M5: bij gelijkstand id als deterministische tiebreaker
    zodat repeated suggest-calls dezelfde proposal_ids genereren
    (cache-hit voor pending apply blijft consistent)."""
    a_has = bool(a.due_date or a.due_datetime)
    b_has = bool(b.due_date or b.due_datetime)
    if a_has and not b_has:
        return a, b
    if b_has and not a_has:
        return b, a
    key_a = (a.created_at or "9999", a.id)
    key_b = (b.created_at or "9999", b.id)
    if key_a <= key_b:
        return a, b
    return b, a


def find_stale(
    tasks: list[Task], *, today: datetime | None = None,
    days_threshold: int = 30, include_with_due: bool = False,
) -> list[StaleProposal]:
    """Vind tasks die >N dagen open staan en (default) geen due-date hebben.

    Stale = signal dat het item niet langer relevant is OF dringend
    geplanned moet worden. the user krijgt het voorstel; sluiten of
    een due-date erop is zijn keuze.
    """
    today = today or datetime.now(UTC)
    threshold = today - timedelta(days=days_threshold)
    out: list[StaleProposal] = []
    for t in tasks:
        has_due = bool(t.due_date or t.due_datetime)
        if has_due and not include_with_due:
            continue
        created = _parse_created(t.created_at)
        if created is None or created > threshold:
            continue
        age_days = (today - created).days
        out.append(StaleProposal(
            task_id=t.id, content=t.content, age_days=age_days,
            has_due=has_due,
        ))
    out.sort(key=lambda s: s.age_days, reverse=True)
    return out


def _parse_created(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


# ---- Proposal-store + apply-flow --------------------------------------

# In-memory store van actieve proposals zodat _suggest IDs uitgeeft die
# _apply later kan resolven. Sleutel = tuple(proposal_id, "dup"/"stale").
# Persistentie is bewust niet nodig: the user bevestigt binnen dezelfde
# conversatie; bij restart is een nieuwe _suggest-call beter dan oude
# IDs uitvoeren tegen een gewijzigde Todoist-staat.

# Review 27/6 L1: 1h was te kort voor "ik denk er even over na"; 24h dekt
# realistische pauzes (lunch, vergadering, slaap-en-besluiten).
_PROPOSAL_TTL_SECONDS = 24 * 3600

_proposal_store: dict[str, tuple[float, dict[str, Any]]] = {}
# Review 27/6 M1: scheduler (briefing-thread) en orchestrator (main-thread)
# kunnen suggest/apply tegelijk doen; zonder lock kan _gc_proposals
# dict-mutation crashen. Per-process lock is voldoende.
_proposal_lock = threading.Lock()


def _gc_proposals_unlocked(now: float | None = None) -> None:
    cutoff = (now or time.time()) - _PROPOSAL_TTL_SECONDS
    for pid in list(_proposal_store):
        if _proposal_store[pid][0] < cutoff:
            del _proposal_store[pid]


def _store_proposal(pid: str, payload: dict[str, Any]) -> None:
    with _proposal_lock:
        _gc_proposals_unlocked()
        _proposal_store[pid] = (time.time(), payload)


def get_proposal(pid: str) -> dict[str, Any] | None:
    with _proposal_lock:
        _gc_proposals_unlocked()
        entry = _proposal_store.get(pid)
        return entry[1] if entry else None


def reset_proposals() -> None:
    """Test-hook."""
    with _proposal_lock:
        _proposal_store.clear()


def register_duplicate_proposal(p: DuplicateProposal) -> str:
    pid = f"dup-{p.drop_id}"
    _store_proposal(pid, {
        "kind": "dup", "action": "close", "task_id": p.drop_id,
        "context": {
            "keep_id": p.keep_id, "keep_content": p.keep_content,
            "drop_content": p.drop_content,
        },
    })
    return pid


def register_stale_proposal(p: StaleProposal) -> str:
    pid = f"stale-{p.task_id}"
    _store_proposal(pid, {
        "kind": "stale", "action": "close", "task_id": p.task_id,
        "context": {"content": p.content, "age_days": p.age_days},
    })
    return pid
