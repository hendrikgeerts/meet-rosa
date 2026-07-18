"""Detection-helpers — vertaal CommItem (en PlaudMeeting elders) naar
OpenLoop-rijen. Bidirectional:

  inkomende vraag/task   → incoming_question/incoming_task loop opent
  uitgaande vraag/task   → outgoing_request loop opent (delegate-tracker)
  uitgaande reply        → matching incoming_*-loops in zelfde thread sluit
  inkomende reply        → matching outgoing_request-loops in zelfde thread sluit

`sync_for_comm_item` is de enige public entry — wordt door comm-intel ingest
voor élk item aangeroepen en doet beide kanten van het werk.

V2 — actionable-gate cascade om the user's klacht over false positives én
false negatives op te lossen:

  1. **Closing pattern?** (bedankings/bevestigingen) → NO LOOP, ongeacht intent
  2. **Newsletter-achtig?** → NO LOOP
  3. **intent in {question, task}**: kandidaat — als ollama beschikbaar,
     extra Llama yes/no gate als sanity-check
  4. **intent != question/task maar action-keywords aanwezig**: kandidaat —
     Llama yes/no gate vereist (vangt impliciete vragen die de summarizer
     mist, bv. 'kun je me het rapport sturen' zonder vraagteken)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from extensions.comm_intel.schema import CommItem
from extensions.open_loops.deadline import extract_deadline
from extensions.open_loops.schema import (
    OpenLoop, close_loops_by_context, insert_loop, set_action_summary,
)

log = logging.getLogger(__name__)

_TRACKABLE_INTENTS = {"question", "task"}

# Closing / bedankings / bevestigingen — geen actie meer verwacht.
_CLOSING_RE = re.compile(
    r"(?i)\b("
    r"bedankt|dank je|dank u|thx|thanks|akkoord|ok|oke?\b|prima|"
    r"helemaal goed|duidelijk|got it|received|ontvangen|"
    r"klaar voor mij|done from my side|sluit dit af"
    r")\b"
)

# Action-keywords die wijzen op een verzoek of opdracht — gebruikt om
# impliciete vragen te vangen die intent != question/task hebben.
_ACTION_RE = re.compile(
    r"(?i)\b("
    # NL — vraagvormen + verzoeken
    r"kun je|kunt u|kun jij|zou je|zou jij|graag|wil je|wil jij|"
    r"laat (?:me|ons|mij) (?:weten|zien)|"
    r"stuur (?:me|mij|ons)|stuur (?:de|het|een)|"
    r"check (?:even|graag|kort)|even checken|even kijken|even bekijken|"
    r"actie:|actiepunt|todo|to do|to-do|"
    r"reactie nodig|antwoord nodig|reageer (?:graag|svp)|"
    r"voor (?:morgen|vrijdag|maandag|dinsdag|woensdag|donderdag|zaterdag|zondag|deze week|volgende week|eind van de week|eind week)|"
    # EN
    r"can you|could you|would you|please (?:send|share|review|check|update)|"
    r"let me know|give me|send me|share with me|"
    r"action item|todo|to-do|to do|"
    r"by (?:tomorrow|monday|tuesday|wednesday|thursday|friday|next week|end of week|eod|cob)|"
    r"any update|status update|need (?:your|the) input"
    r")\b"
)

# Newsletter / promo / system-mail signals (uitgebreid van scheduler-fix).
_NOISE_RE = re.compile(
    r"(?i)\b("
    r"unsubscribe|view in browser|newsletter|"
    r"webinar registration|register (?:now|today|here)|"
    r"save the date|free demo|early bird|"
    r"do not reply|no-?reply|automated message|"
    r"deze e-mail is automatisch"
    r")\b"
)

_LLAMA_SUMMARY_PROMPT = """Vat in ÉÉN korte zin (max 15 woorden, NL of EN matchend met
de mail-taal) samen wat hier concreet wordt gevraagd of welke actie
nodig is. Zet het in actieve vorm — wie moet wat doen.

Voorbeelden:
- "Roel vraagt pricing voor lite-pakket te ontvangen voor vrijdag"
- "Klant wil status-update op proposal #42"
- "Send the contract draft for review"
- "Schedule a 30-min call about Q3 forecast"

Geen extra uitleg, geen prefix, geen aanhalingstekens — alleen de zin.

Direction: {direction}
Subject: {subject}
Body excerpt:
{body}

Eén zin:"""


_LLAMA_PROMPT = """An email or chat message arrived. Determine if it represents an OPEN LOOP — i.e. someone needs to take action OR is waiting on someone else.

Direction = "{direction}" (in = inbound, someone wrote to the user; out = the user wrote to someone).

YES (open loop) examples:
- inbound: "Could you send me the contract?" / "Wanneer kun je dit reviewen?" / "Need your input on X"
- outbound: "Stuur jij mij even het rapport voor vrijdag?" / "Can you check this proposal?"
- inbound: "Are you free for a call about pricing?" (action: respond / schedule)

NO (no loop) examples:
- "Confirming our meeting tomorrow at 10am" (already scheduled)
- "Thanks for the update!" (closing)
- "Newsletter: meet our team!" (no action requested)
- "Notes from yesterday's meeting"
- "Reminder: your appointment is at 3pm" (informational)
- "Zoals afgesproken stuur ik je de offerte" (statement, not request)
- "FYI - this came in over the weekend" (informational)

Direction context: {direction}
Subject: {subject}
Body excerpt:
{body}

Answer with exactly one word: YES or NO."""


def sync_for_comm_item(
    conn: sqlite3.Connection, item: CommItem, *,
    intent: str | None,
    ollama: Any | None = None,
) -> tuple[int | None, int]:
    """Open een nieuwe loop wanneer applicable, en sluit matching loops in
    dezelfde thread. Returns (new_loop_id_or_None, closed_count).

    `ollama` (optioneel): als meegegeven, extra yes/no gate voor borderline
    cases. Zonder ollama is het gedrag identiek aan v1 (intent-only)."""
    new_id = _track(conn, item, intent=intent, ollama=ollama)
    closed = _close_replies(conn, item)
    return new_id, closed


def _is_actionable(
    item: CommItem, intent: str | None, *, ollama: Any | None,
) -> bool:
    """Beslis of dit comm-item een open-loop verdient. Cascade:
      1. Closing/newsletter pattern → False
      2. intent in question/task → True (optioneel via Llama gegated)
      3. action-keywords + Llama bevestigt → True
      4. anders → False
    """
    haystack = " ".join((
        str(item.subject or ""),
        str(item.body_full or "")[:1500],
    ))
    if _NOISE_RE.search(haystack) or _CLOSING_RE.search(haystack):
        return False

    primary = intent in _TRACKABLE_INTENTS
    secondary = bool(_ACTION_RE.search(haystack))
    if not (primary or secondary):
        return False

    if ollama is None:
        # Backwards-compatible pad — alleen v1-gedrag (intent-only).
        return primary

    return _llama_confirm(ollama, item)


def _llama_confirm(ollama: Any, item: CommItem) -> bool:
    direction = "in" if item.direction == "in" else "out"
    subject = (item.subject or "")[:200]
    body = (item.body_full or "")[:1500]
    prompt = _LLAMA_PROMPT.format(
        direction=direction, subject=subject, body=body,
    )
    try:
        response = ollama.chat(
            system="You answer with exactly one word: YES or NO.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
        )
    except Exception:
        log.exception("open-loops: ollama yes/no call failed")
        return False
    text = ""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip().upper()
    if text.startswith("YES"):
        return True
    if not text.startswith("NO"):
        log.info("open-loops: ollama unclear answer %r — treating as NO",
                  text[:50])
    return False


def _track(
    conn: sqlite3.Connection, item: CommItem, *,
    intent: str | None,
    ollama: Any | None = None,
) -> int | None:
    """Insert a new open_loop based on the item's direction + intent +
    actionable-gate. Bij succesvolle insert: vraag Llama een 1-zin
    action_summary zodat dayclose/dashboard meteen toont WAT er gevraagd
    wordt ipv alleen de subject."""
    if not _is_actionable(item, intent, ollama=ollama):
        return None

    title = (item.subject or "").strip()
    if not title:
        first_line = (item.body_full or "").strip().splitlines()[0:1]
        title = first_line[0][:80] if first_line else "(zonder titel)"
    body_excerpt = (item.body_full or "")[:280].strip().replace("\n", " ")

    if item.direction == "in":
        # Default kind = incoming_question; mark als incoming_task wanneer
        # intent expliciet 'task' was. Voor de keyword-only-pad (intent != q/t
        # maar Llama bevestigt) val terug op incoming_question.
        kind = "incoming_task" if intent == "task" else "incoming_question"
        who = item.from_addr or None
    else:  # 'out' — the user vraagt iets aan iemand → delegate-tracker
        kind = "outgoing_request"
        who = (item.to_addrs[0] if item.to_addrs else None) or None

    # Deadline-extract uit subject + body (regex-only, geen Llama-call).
    deadline_haystack = f"{title}\n{item.body_full or ''}"[:2000]
    due_at = extract_deadline(deadline_haystack)

    loop_id = insert_loop(conn, OpenLoop(
        source="comm",
        source_ref=f"{item.source}:{item.account}:{item.external_id}",
        kind=kind, who=who,
        title=title[:200], body_excerpt=body_excerpt,
        context=item.thread_ref,
        due_at=due_at,
    ))
    if loop_id is not None and ollama is not None:
        # Best-effort: vraag Llama een actie-zin. Bij fout: fallback
        # naar lege/None summary; dashboard valt dan terug op title.
        summary = _llama_action_summary(ollama, item)
        if summary:
            try:
                set_action_summary(conn, loop_id, summary)
            except Exception:
                log.exception("open-loops: set_action_summary failed for loop %d",
                                loop_id)
    return loop_id


def _llama_action_summary(ollama: Any, item: CommItem) -> str | None:
    """Vraag Llama een 1-zin samenvatting van de concrete actie/vraag.
    Returns None bij fout — caller doet dan niks (action_summary blijft NULL)."""
    direction = "in" if item.direction == "in" else "out"
    prompt = _LLAMA_SUMMARY_PROMPT.format(
        direction=direction,
        subject=(item.subject or "")[:200],
        body=(item.body_full or "")[:1500],
    )
    try:
        response = ollama.chat(
            system="Je extracteert concrete acties uit zakelijke mail. Antwoord met EXACT één zin, geen extra tekst.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
        )
    except Exception:
        log.exception("open-loops: action_summary llama-call failed")
        return None
    text = ""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip().strip('"').strip("'").strip()
    # Pak alleen eerste regel — Llama wil soms uitleggen.
    text = text.split("\n", 1)[0].strip()
    if not text or len(text) < 5:
        return None
    return text[:200]


def _close_replies(conn: sqlite3.Connection, item: CommItem) -> int:
    """Close loops in this thread that this message resolves."""
    if not item.thread_ref:
        return 0
    if item.direction == "out":
        # the user antwoordt → sluit incoming-loops
        return close_loops_by_context(
            conn, context=item.thread_ref,
            kinds=("incoming_question", "incoming_task"),
        )
    # Iemand anders antwoordt → sluit outgoing_request-loops
    return close_loops_by_context(
        conn, context=item.thread_ref,
        kinds=("outgoing_request",),
    )


# --- backward-compat aliases (eerder API; ingest gebruikt sync_for_comm_item) ---

def track_for_comm_item(
    conn: sqlite3.Connection, item: CommItem, *,
    intent: str | None,
    ollama: Any | None = None,
) -> int | None:
    if item.direction != "in":
        return None
    return _track(conn, item, intent=intent, ollama=ollama)


def close_for_outgoing_comm_item(conn: sqlite3.Connection, item: CommItem) -> int:
    if item.direction != "out" or not item.thread_ref:
        return 0
    return close_loops_by_context(conn, context=item.thread_ref)
