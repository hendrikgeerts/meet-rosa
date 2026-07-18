"""Detecteer scheduling-intent op een binnenkomend comm-item.

V2: regex (strikter dan v1) + optionele Llama yes/no-gate. Triggert op:
  - direction='in' (inkomende mail/slack)
  - intent niet in fyi/newsletter/social
  - body/subject bevat een scheduling-keyword
  - geen anti-pattern (newsletter / webinar / agenda-referentie)
  - (optioneel) Llama bevestigt: "is dit iemand die met MIJ een afspraak
    wil maken?" — vangt resterende false positives waar bv. een
    onduidelijke mail het woord 'meeting' bevat.

Bewust conservatief: liever een gemiste afspraak (the user moet zelf
reageren) dan een ongepaste auto-proposal voor een mail die toevallig
een keyword bevat.

V2 dropt deze v1-keywords als notoir te breed:
  - `agenda(?:punt|tje)?` (vaak in notulen, project-agenda, klant-agenda)
  - `kalender` (vaak in nieuwsbrieven 'kalender 2026')
  - `plannen` als losse vorm (vaak 'we hebben plannen voor Q3')
  - `schedule` zonder context (vaak 'publishing schedule', 'schedule
    of payments') — `reschedule` blijft wél (specifiek scheduling)
  - `availability` los (vaak in stock-mails)
  - `catch up` (vaak in informele closing 'catch up later')
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_SCHEDULING_PATTERNS = re.compile(
    r"(?i)\b("
    # NL — direct scheduling-werkwoorden
    r"afspreken|afspraak|inplannen|beschikbaar(?:heid)?|"
    r"wanneer (?:kun(?:nen)? we|past het|heb je tijd|ben je vrij)|"
    r"kun(?:nen)? we (?:bellen|spreken|videocallen|meeten?)|"
    r"call (?:plannen|inplannen)|videocall|teams[- ]?(?:call|meeting)|"
    r"meeten?|even bellen|terug bellen|kort overleg|"
    r"welke (?:dag|tijd|datum) (?:past|werkt|schikt|kun(?:nen)? we)|"
    # EN — werkwoorden + verzoeken
    r"reschedule|let'?s (?:meet|chat|connect|hop on|jump on)|"
    r"any time(?: that works)?|"
    r"when (?:are you|would you be|can we|works for you|do you have)|"
    r"what (?:day|time|date) (?:works|would work|suits|are you)|"
    r"does (?:tomorrow|monday|tuesday|wednesday|thursday|friday|next week|"
    r"this week|that day|that time) work|"
    r"would (?:tomorrow|monday|tuesday|wednesday|thursday|friday|next week|"
    r"this week) work|"
    r"find a time|book a (?:slot|meeting|call)|hop on (?:a )?call|"
    r"set up (?:a )?(?:meeting|call|chat)|"
    r"are you (?:free|available)|let me know (?:when|what time|what day)"
    r")\b"
)

# Anti-patronen: items die een scheduling-keyword bevatten maar overduidelijk
# géén afspraak-verzoek zijn.
_NEGATIVES = re.compile(
    r"(?i)\b("
    # Reeds-gemaakte afspraken / referenties
    r"hadden afgesproken|zoals afgesproken|conform afspraak|"
    r"agenda punt \d|notulen|verslag van|notulen van|"
    # Newsletter / promo / marketing-blast
    r"unsubscribe|view in browser|newsletter|webinar|"
    r"join us at|register (?:now|today|here|for)|save the date|"
    r"upcoming events|registration is open|"
    r"don'?t miss|early bird|register before|book your seat|"
    r"reserve your spot|free webinar|live demo|"
    # Bestaande-afspraak confirmation/reminder
    r"confirming (?:our|the) (?:meeting|call|appointment)|"
    r"reminder: (?:your|the|a) (?:meeting|appointment|call)|"
    r"this is a (?:reminder|confirmation)|here'?s a reminder|"
    # Agenda referenties die over content gaan, niet scheduling
    r"agenda van|agenda voor (?:de|het|onze|de komende)"
    r")\b"
)

# Llama yes/no prompt — bewust strict, vraagt ECHT scheduling-intent
_LLAMA_PROMPT = """You receive an email. Determine if the SENDER is asking the RECIPIENT to schedule a NEW meeting, call, or appointment.

Examples that are YES:
- "Are you free Thursday for a call?"
- "Let's schedule something soon"
- "Could we hop on a call next week?"
- "When works best for you to meet?"
- "Wanneer kunnen we afspreken?"
- "Heb je tijd voor een korte call deze week?"

Examples that are NO:
- "Newsletter: meet our team!"
- "Save the date: webinar on Tuesday"
- "Confirming our meeting tomorrow at 10am" (already scheduled)
- "Reminder: your appointment is at 3pm"
- "Just an update on the project"
- "Notes from yesterday's meeting"
- "Agenda voor onze quarterly review" (referring to existing meeting)
- "Webinar registration: book your seat now"
- "Zoals afgesproken stuur ik je..."

Email subject: {subject}

Email body (excerpt):
{body}

Answer with exactly one word: YES or NO."""


def is_scheduling_request(
    item: dict[str, Any], *,
    ollama: Any | None = None,
) -> bool:
    """item: dict met o.a. direction, intent, subject, body_full.

    Returns True only if BOTH gates pass:
      1. Strikt regex hit (en geen anti-pattern)
      2. (Als ollama meegegeven) Llama bevestigt scheduling-intent

    Zonder ollama is alleen gate 1 actief — backwards-compatible voor
    callers/tests die geen Ollama-client hebben."""
    if item.get("direction") != "in":
        return False
    intent = item.get("intent")
    if intent in ("fyi", "newsletter", "social"):
        return False

    subject = str(item.get("subject") or "")
    body = str(item.get("body_full") or "")[:2000]
    haystack = f"{subject} {body}"

    if _NEGATIVES.search(haystack):
        return False
    if not _SCHEDULING_PATTERNS.search(haystack):
        return False

    if ollama is None:
        return True
    return _llama_confirm(ollama, subject=subject, body=body)


def _llama_confirm(ollama: Any, *, subject: str, body: str) -> bool:
    """Tweede gate: Llama yes/no. Bij twijfel/fout → False (conservatief).

    Op fout (Ollama down, parse-fail, onverwacht antwoord) returneren we
    False zodat we niet ten onrechte een proposal sturen — the user kan
    altijd nog handmatig reageren."""
    prompt = _LLAMA_PROMPT.format(subject=subject[:200], body=body[:1500])
    try:
        response = ollama.chat(
            system="You answer with exactly one word: YES or NO.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
        )
    except Exception:
        log.exception("scheduler_assist: ollama yes/no call failed")
        return False
    text = ""
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip().upper()
    if text.startswith("YES"):
        return True
    if not text.startswith("NO"):
        log.info("scheduler_assist: ollama unclear answer %r — treating as NO",
                  text[:50])
    return False
