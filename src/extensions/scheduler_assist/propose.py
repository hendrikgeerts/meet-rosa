"""Bouw een concept-reply met 3 voorgestelde slots voor een binnenkomend
scheduling-verzoek.

Stappen:
1. Bepaal duur uit body-context (default 30 min, sales/klant 45-60).
2. Pak 3 vrije slots in de komende 7 werkdagen via calendar.find_free_slots.
3. Genereer reply-tekst via gateway.complete (Claude, internal label).
4. Persisteer als pending_proposal + iMessage-notify the user.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.scheduler_assist.schema import (
    PendingProposal,
    insert_proposal,
)
from integrations.gcal import CalendarClient
from integrations.imap import ImapAccount
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


_DRAFT_PROMPT = """Je bent Rosa, ${user_name}'s persoonlijke assistent. ${user_name} heeft net een scheduling-vraag ontvangen en jij stelt een korte, professionele reply voor.

Schrijf in dezelfde taal als de inkomende mail (NL/EN).
Stijl: warm-zakelijk, kort, geen plichtplegingen.
Onderteken met "${user_signature}".

Inhoud:
- Bedank kort voor de vraag.
- Stel concreet de drie slots voor (datum + dag + tijd, in Europe/Amsterdam).
- Vermeld dat als hij voor 1 van de tijden kiest, hij een uitnodiging met Google Meet-link krijgt.
- Als een Calendly-URL is meegegeven: noem hem als alternatief ("Past geen van deze tijden? Je kunt ook zelf een moment kiezen via [link]").
- Vraag of een van de slots werkt, of dat een andere week beter past.

Antwoord ALLEEN met JSON, geen extra tekst:
{"subject": "<reply subject, vaak 'Re: <originele subject>'>",
 "body": "<volledige reply-tekst incl. ondertekening>"}"""


# --- duration heuristic ---------------------------------------------------

_HOURS_HINTS = re.compile(
    r"(?i)\b("
    r"(\d{1,2})[ -]?(?:uur|hour|hours|uren|hrs?)"
    r"|(\d{1,3})[ -]?(?:min(?:uten|utes)?)"
    r"|(?:half(?:uur|f hour|-hour))"
    r"|(?:kort|brief|quick|short)"
    r"|(?:uitgebreid|lange?|deep ?dive)"
    r")\b"
)


def estimate_duration_minutes(body: str, subject: str = "") -> int:
    """Best-effort uit body/subject. Default 30."""
    haystack = f"{subject}\n{body}"[:1500]
    m = _HOURS_HINTS.search(haystack)
    if m:
        hours = m.group(2)
        mins = m.group(3)
        if hours:
            return max(15, min(int(hours) * 60, 240))
        if mins:
            return max(15, min(int(mins), 240))
        token = m.group(0).lower()
        if "half" in token:
            return 30
        if any(w in token for w in ("kort", "brief", "quick", "short")):
            return 30
        if any(w in token for w in ("uitgebreid", "lang", "deep")):
            return 60
    # Heuristiek op klantvocabulaire
    klant_signals = (
        "demo", "intake", "kennismaking", "product walkthrough", "pitch",
        "discovery", "discovery call",
    )
    if any(w in haystack.lower() for w in klant_signals):
        return 45
    return 30


# --- slot picker ---------------------------------------------------------

def pick_slots(
    calendar: CalendarClient, *,
    duration_minutes: int,
    days_horizon: int = 10,
    max_slots: int = 3,
    earliest_hour: int = 9,
    latest_hour: int = 17,
) -> list[dict[str, str]]:
    """Vraag CalendarClient om vrije slots, beperk tot max_slots gespreid
    over verschillende dagen waar mogelijk."""
    now = datetime.now(TZ)
    earliest = now + timedelta(hours=1)   # iets vooruit zodat huidige uur niet kandideert
    latest = now + timedelta(days=days_horizon)
    raw = calendar.find_free_slots(
        duration_minutes=duration_minutes,
        earliest=earliest, latest=latest,
        work_start_hour=earliest_hour, work_end_hour=latest_hour,
    )
    # Pak slot-starts op verschillende dagen; cap per dag op 1 om spreiding
    # over de werkweek te tonen ipv 3 op dezelfde dag.
    seen_dates: set[str] = set()
    out: list[dict[str, str]] = []
    for s in raw:
        try:
            start_dt = datetime.fromisoformat(s["start"])
        except (ValueError, KeyError):
            continue
        date_key = start_dt.date().isoformat()
        if date_key in seen_dates:
            continue
        seen_dates.add(date_key)
        # Trim slot tot exact duration_minutes (find_free_slots geeft hele windows)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        out.append({
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        })
        if len(out) >= max_slots:
            break
    return out


# --- thread continuation -------------------------------------------------

def notify_followup_for_item(
    *,
    item: dict[str, Any],
    prev_proposal: dict[str, Any],
    send_imessage: Callable[[str, str], None],
    primary_handle: str,
) -> None:
    """Counter-reply op een bestaande thread → the user notificeren met
    context van de vorige proposal én de nieuwe inkomende tekst, en
    laat hem kiezen wat te doen.

    Bewust geen automatisch nieuw voorstel — the user is hier degene die
    inschat: 'klant accepteert slot 2' vs 'klant wil andere week' vs
    'gewoon manueel reageren'. Reactie via iMessage triggert de juiste
    tool (send_proposal met chosen_slot, of een nieuwe propose-run).
    """
    excerpt = (item.get("body_full") or "").strip()[:300].replace("\n", " ")
    prev_status = prev_proposal.get("status", "?")
    prev_id = prev_proposal["id"]
    slot_lines = "\n".join(
        f"  {i+1}. {_fmt_slot(s)}" for i, s in enumerate(prev_proposal.get("slots") or [])
    )
    notice = (
        f"📬 Reactie op proposal #{prev_id} (status: {prev_status})\n"
        f"Van: {item.get('from_addr') or '(onbekend)'}\n"
        f"Onderwerp: {(item.get('subject') or '')[:80]}\n\n"
        f"Hun antwoord:\n\"{excerpt}\"\n\n"
        f"Originele slots:\n{slot_lines or '  (geen)'}\n\n"
        f"Wat wil je doen?\n"
        f"  • 'send {prev_id} met slot N' → bevestig + maak agenda-event\n"
        f"  • 'opnieuw N voorstellen voor {prev_id}' → ik bouw nieuw voorstel\n"
        f"  • 'cancel {prev_id}' → ik laat het, jij reageert zelf"
    )
    try:
        send_imessage(primary_handle, notice)
        log.info("scheduler_assist: follow-up notified op proposal #%d (sender=%s)",
                 prev_id, item.get("from_addr"))
    except Exception:
        log.exception("scheduler_assist: follow-up iMessage failed voor #%d", prev_id)


# --- main entry ---------------------------------------------------------

def propose_for_item(
    *,
    item: dict[str, Any],
    db_path: Path,
    calendar: CalendarClient,
    gateway: Gateway,
    imap_accounts: list[ImapAccount],
    gmail_default_address: str,
    send_imessage: Callable[[str, str], None],
    primary_handle: str,
    calendly_url: str | None = None,
    user_name: str = "you",
    user_signature: str = "",
) -> int | None:
    """Bouw + persisteer + notify. Returns proposal_id of None bij dup/fail.

    `user_name` + `user_signature` uit config.user.* — voorheen hardcoded
    'the user'."""
    duration = estimate_duration_minutes(
        body=item.get("body_full") or "", subject=item.get("subject") or "",
    )
    slots = pick_slots(calendar, duration_minutes=duration)
    if not slots:
        log.info("scheduler_assist: geen vrije slots in 10 dagen — skip proposal")
        return None

    # Bepaal welke mailbox we gebruiken voor reply.
    reply_via_source = item.get("source") or "gmail"
    reply_via_account = item.get("account") if reply_via_source != "gmail" else None
    reply_from = _resolve_from_address(
        item, imap_accounts, gmail_default=gmail_default_address,
    )

    # Genereer draft via Claude.
    draft_subject, draft_body = _generate_draft(
        gateway=gateway, item=item, slots=slots, duration=duration,
        calendly_url=calendly_url,
        user_name=user_name, user_signature=user_signature,
    )

    proposal = PendingProposal(
        comm_item_id=int(item["id"]),
        sender=str(item.get("from_addr") or "(onbekend)"),
        subject=str(item.get("subject") or ""),
        thread_ref=item.get("thread_ref"),
        reply_via_source=reply_via_source,
        reply_via_account=reply_via_account,
        reply_from_address=reply_from,
        duration_minutes=duration,
        add_meet_link=True,
        slots=slots,
        draft_subject=draft_subject,
        draft_body=draft_body,
    )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        pid = insert_proposal(conn, proposal)
    if pid is None:
        log.debug("scheduler_assist: already a proposal for comm_item %s", item["id"])
        return None

    # iMessage notify.
    slot_lines = "\n".join(
        f"  {i+1}. {_fmt_slot(s)}" for i, s in enumerate(slots)
    )
    notice = (
        f"📅 Scheduling-verzoek van {proposal.sender}\n"
        f"Onderwerp: {proposal.subject[:80]}\n"
        f"Voorstel ({duration} min, Meet-link):\n{slot_lines}\n\n"
        f"Reply 'send {pid}' om te versturen, "
        f"'show {pid}' om de draft te zien, "
        f"of 'cancel {pid}' om te negeren."
    )
    try:
        send_imessage(primary_handle, notice)
        log.info("scheduler_assist: proposal #%d voorgesteld aan the user (sender=%s)",
                 pid, proposal.sender)
    except Exception:
        log.exception("scheduler_assist: iMessage notify failed for proposal %d", pid)
    return pid


def _generate_draft(
    *, gateway: Gateway, item: dict[str, Any],
    slots: list[dict[str, str]], duration: int,
    calendly_url: str | None = None,
    user_name: str = "you",
    user_signature: str = "",
) -> tuple[str, str]:
    incoming_excerpt = (item.get("body_full") or "")[:1500]
    calendly_line = (
        f"Calendly-URL (optioneel meesturen als alternatief): {calendly_url}\n\n"
        if calendly_url else ""
    )
    user_payload = (
        "Inkomende scheduling-mail:\n"
        f"Van: {item.get('from_addr') or '(onbekend)'}\n"
        f"Onderwerp: {item.get('subject') or '(geen onderwerp)'}\n\n"
        f"Body:\n{incoming_excerpt}\n\n"
        f"Voorgestelde slots ({duration} min, Europe/Amsterdam):\n"
        + "\n".join(f"- {_fmt_slot(s)}" for s in slots)
        + "\n\n"
        + calendly_line
        + "Geef de JSON met subject + body voor de reply."
    )
    signature = (user_signature or user_name or "").strip() or "-"
    system_prompt = (
        _DRAFT_PROMPT
        .replace("${user_name}", user_name or "you")
        .replace("${user_signature}", signature)
    )
    response = gateway.complete(
        task="scheduler_draft_reply",
        system=system_prompt,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=600,
    )
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    return _parse_draft_json(
        text, fallback_subject=item.get("subject") or "",
        user_signature=signature,
    )


def _parse_draft_json(
    text: str, *, fallback_subject: str, user_signature: str = "-",
) -> tuple[str, str]:
    """Tolerant: strip markdown fences, vind eerste {…} blok."""
    s = text.strip()
    # Strip code-fence
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, count=1)
        s = re.sub(r"\s*```$", "", s, count=1)
    # Greedy: find first { ... } block
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            subj = str(data.get("subject") or fallback_subject).strip()
            body = str(data.get("body") or "").strip()
            if body:
                return subj or f"Re: {fallback_subject}", body
        except json.JSONDecodeError:
            pass
    log.warning("scheduler_assist: kon draft-JSON niet parsen: %s", text[:200])
    # Fallback draft.
    return (
        f"Re: {fallback_subject}",
        "Hoi,\n\nDank voor je bericht — ik kom snel bij je terug "
        f"met voorgestelde tijden.\n\n{user_signature}",
    )


def _fmt_slot(slot: dict[str, str]) -> str:
    try:
        s = datetime.fromisoformat(slot["start"]).astimezone(TZ)
        e = datetime.fromisoformat(slot["end"]).astimezone(TZ)
    except (ValueError, KeyError):
        return slot.get("start", "?")
    days_nl = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    return (
        f"{days_nl[s.weekday()]} {s.strftime('%d/%m')} "
        f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
    )


def _resolve_from_address(
    item: dict[str, Any],
    imap_accounts: list[ImapAccount],
    *, gmail_default: str,
) -> str:
    """Bepaal exact From-adres voor de reply, op basis van waar de mail
    is ontvangen."""
    source = item.get("source")
    account_name = item.get("account")
    if source != "gmail" and account_name:
        for a in imap_accounts:
            if a.name == account_name:
                return a.from_address or a.username
    return gmail_default
