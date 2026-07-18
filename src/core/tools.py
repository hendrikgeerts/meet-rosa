"""Tool schemas and executor for Claude tool-use.

Each tool has an Anthropic-format JSON schema plus a Python handler that
returns a JSON-serialisable dict. Failures become `{"error": "..."}` rather
than raising — Claude reads those and corrects itself.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core.query_safety import QUERY_SCHEMA, validate_query
from extensions import reminders
from extensions.comm_intel.tools import COMM_HANDLERS, COMM_TOOL_SCHEMAS
from extensions.memory.tools import MEMORY_HANDLERS, MEMORY_TOOL_SCHEMAS
from extensions.uptime.tools import UPTIME_HANDLERS, UPTIME_TOOL_SCHEMAS
from extensions.tenders.tools import TENDER_HANDLERS, TENDER_TOOL_SCHEMAS
from extensions.insolvencies.tools import (
    INSOLVENCIES_HANDLERS, INSOLVENCIES_TOOL_SCHEMAS,
)
from extensions.sales.tools import SALES_HANDLERS, SALES_TOOL_SCHEMAS
from extensions.market_intel.tools import (
    MARKET_INTEL_HANDLERS, MARKET_INTEL_TOOL_SCHEMAS,
)
from extensions.open_loops.tools import LOOPS_HANDLERS, LOOPS_TOOL_SCHEMAS
from extensions.birthdays.tools import (
    BIRTHDAY_HANDLERS, BIRTHDAY_TOOL_SCHEMAS,
)
from extensions.decisions.tools import (
    DECISIONS_HANDLERS, DECISIONS_TOOL_SCHEMAS,
)
from extensions.expenses.tools import (
    EXPENSES_HANDLERS, EXPENSES_TOOL_SCHEMAS,
)
from extensions.okrs.tools import (
    OKR_HANDLERS, OKR_TOOL_SCHEMAS,
)
from extensions.person_brief.tools import (
    PERSON_BRIEF_HANDLERS, PERSON_BRIEF_TOOL_SCHEMAS,
)
from extensions.patterns.tools import (
    PATTERN_HANDLERS, PATTERN_TOOL_SCHEMAS,
)
from extensions.projects.tools import (
    PROJECT_HANDLERS, PROJECT_TOOL_SCHEMAS,
)
from extensions.receipt_collector.tools import (
    RECEIPT_HANDLERS, RECEIPT_TOOL_SCHEMAS,
)
from extensions.config_wishes.tools import (
    CONFIG_WISHES_HANDLERS, CONFIG_WISHES_TOOL_SCHEMAS,
)
from extensions.english_practice.tools import (
    ENGLISH_PRACTICE_HANDLERS, ENGLISH_PRACTICE_TOOL_SCHEMAS,
)
from extensions.scheduler_assist.tools import (
    SCHEDULER_HANDLERS, SCHEDULER_TOOL_SCHEMAS,
)
from extensions.todoist_sync.tools import (
    TODOIST_HANDLERS, TODOIST_TOOL_SCHEMAS,
)
from extensions.whats_open.tools import (
    WHATS_OPEN_HANDLERS, WHATS_OPEN_TOOL_SCHEMAS,
)
from extensions.user_profile.tools import (
    USER_PROFILE_HANDLERS, USER_PROFILE_TOOL_SCHEMAS,
)
from integrations.imap import ImapAccount
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


# ------------------------------- schemas -----------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_current_time",
        "description": (
            "Get the current date and time in the user's active timezone. "
            "Default is Europe/Amsterdam, but the user can switch via "
            "set_timezone when he's travelling. Always call this before "
            "interpreting relative times like 'tomorrow', 'next Friday', "
            "'in 2 hours'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_timezone",
        "description": (
            "Switch the user's active timezone — used when he's travelling. "
            "Briefings, dayclose, midday and CEO-letter will fire at the "
            "configured HH:MM in the NEW zone after this call. Call when "
            "the user says things like 'I'm in Tokyo now', 'tz PST', "
            "'switch to Asia/Tokyo', 'use my home time again'. "
            "Pass IANA-zone (Asia/Tokyo, America/Los_Angeles, etc.) or "
            "one of: 'home', 'reset', 'off' to go back to the default "
            "(Europe/Amsterdam)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA-zone (Asia/Tokyo) or 'home'/'reset'/'off'.",
                },
            },
            "required": ["timezone"],
        },
    },
    {
        "name": "get_timezone",
        "description": (
            "Return the user's active timezone, the default from settings, "
            "and the offset versus default. Use when he asks 'where am "
            "I (timezone-wise)', 'what tz are you using now', 'am I "
            "still on Amsterdam time'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    # --- Gmail ---
    {
        "name": "gmail_list_recent",
        "description": "List recent Gmail messages with subject, sender, snippet. Use for 'what's in my inbox' questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "query": {"type": "string", "description": "Optional Gmail search query (e.g. 'is:unread', 'from:boss@x.com')"},
            },
        },
    },
    {
        "name": "gmail_search",
        "description": "Search Gmail using Gmail's query syntax (supports from:, to:, subject:, after:, before:, is:unread, has:attachment, etc).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "gmail_get_thread",
        "description": "Fetch the full content of a Gmail thread (all messages, full bodies). Use the thread_id returned by gmail_list_recent or gmail_search.",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
    },
    {
        "name": "gmail_send",
        "description": "Send an email. If in_reply_to_thread is provided, it will be added to that thread.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address. Multiple comma-separated."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "cc": {"type": "string"},
                "in_reply_to_thread": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "gmail_mark_read",
        "description": "Mark a Gmail message as read (remove UNREAD label).",
        "input_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    # --- Calendar ---
    {
        "name": "calendar_list_today",
        "description": "List today's events on the primary calendar.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "calendar_list_events",
        "description": "List calendar events between two ISO 8601 datetimes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO 8601 datetime, e.g. 2026-04-22T00:00:00+02:00"},
                "time_max": {"type": "string", "description": "ISO 8601 datetime"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 250, "default": 50},
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "calendar_search_events",
        "description": (
            "Search calendar events by free-text query (matches title, "
            "description, location). Use this FIRST when the user refers to "
            "an event by name (e.g. 'mijn standup', 'dat overleg met Piet'). "
            "Returns instances with `id`, `recurring_event_id` (set if it's "
            "an instance of a recurring event — pass to update_event/delete "
            "to modify the WHOLE series), and `is_recurring`. Default window "
            "is 14 days back to 60 days forward."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query, e.g. 'standup' or 'Piet offerte'"},
                "days_back": {"type": "integer", "minimum": 0, "maximum": 365, "default": 14},
                "days_forward": {"type": "integer", "minimum": 1, "maximum": 365, "default": 60},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "calendar_find_free_slots",
        "description": "Find free slots of at least duration_minutes between earliest and latest, during weekday work hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_minutes": {"type": "integer", "minimum": 5},
                "earliest": {"type": "string", "description": "ISO 8601 datetime"},
                "latest": {"type": "string", "description": "ISO 8601 datetime"},
                "work_start_hour": {"type": "integer", "default": 9},
                "work_end_hour": {"type": "integer", "default": 18},
            },
            "required": ["duration_minutes", "earliest", "latest"],
        },
    },
    {
        "name": "calendar_create_event",
        "description": (
            "Create a calendar event on the primary (YourCompany) calendar. "
            "Start/end are ISO 8601 datetimes. Set add_meet_link=true to "
            "automatically attach a Google Meet link — use that for any "
            "remote/virtual meeting (default for client/sales calls). "
            "For recurring events: pass `recurrence` (zie property-schema)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string", "format": "email"}},
                "add_meet_link": {"type": "boolean", "default": False,
                                   "description": "Add Google Meet link automatically. Default true for events with attendees unless physical location is set."},
                "recurrence": {
                    "type": "object",
                    "description": (
                        "Recurrence-spec. Voorbeelden: "
                        "'elke maandag' = {freq:WEEKLY, by_weekday:[MO]}; "
                        "'elke werkdag' = {freq:WEEKLY, by_weekday:[MO,TU,WE,TH,FR]}; "
                        "'iedere maand op de 15e' = {freq:MONTHLY, by_month_day:15}; "
                        "'elke 2 weken' = {freq:WEEKLY, interval:2}; "
                        "'tot eind juni' voeg until:'2026-06-30' toe; "
                        "'10 keer' voeg count:10 toe. Count en until samen mag niet."
                    ),
                    "properties": {
                        "freq": {"type": "string",
                                  "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]},
                        "interval": {"type": "integer", "minimum": 1, "default": 1},
                        "by_weekday": {
                            "type": "array",
                            "items": {"type": "string",
                                       "enum": ["MO","TU","WE","TH","FR","SA","SU"]},
                        },
                        "by_month_day": {"type": "integer", "minimum": 1, "maximum": 31},
                        "until": {"type": "string",
                                   "description": "ISO datum (YYYY-MM-DD) of datetime. Laatste mogelijke instance."},
                        "count": {"type": "integer", "minimum": 1,
                                   "description": "Totaal aantal voorkomens (incl. eerste)."},
                    },
                    "required": ["freq"],
                },
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "calendar_update_event",
        "description": (
            "Update an existing calendar event's fields. Only provide "
            "fields you want to change. `recurrence` accepts the same "
            "structured object as calendar_create_event om een eenmalig "
            "event naar recurring te promoveren, of `null`/`[]` om "
            "recurrence te verwijderen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "recurrence": {
                    "description": "Zelfde format als calendar_create_event.recurrence. Pass null/[] om eenmalig te maken.",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "calendar_delete_event",
        "description": "Delete a calendar event by id.",
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    },
    # --- Reminders ---
    {
        "name": "set_reminder",
        "description": (
            "Schedule a reminder delivered via iMessage. `when` is ISO 8601 "
            "in local timezone. `body` is the message text.\n\n"
            "Duplicate-check: by default (force=false) checks pending "
            "reminders + open Todoist tasks for similar content. If a "
            "likely duplicate is found, the reminder is NOT created and "
            "the tool returns `needs_confirmation=true` with the "
            "candidates. Ask the user: 'Er staat al X — vervangen, "
            "samenvoegen, of toch allebei?' On explicit confirm, retry "
            "with force=true. Only skip the check when the user has just "
            "acknowledged the duplicate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "ISO 8601 datetime. Use get_current_time first to resolve relative times."},
                "body": {"type": "string"},
                "force": {
                    "type": "boolean", "default": False,
                    "description": "Skip duplicate-check and create regardless. Only true after the user confirmed.",
                },
            },
            "required": ["when", "body"],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "List reminders. Default: pending only. Set include_history=true "
            "to also include sent + cancelled (laatste 30 dagen) — gebruik dit "
            "wanneer the user vraagt naar info die in een eerdere reminder zat "
            "(bv. 'wat was mijn ordernummer', 'welk adres heb je doorgestuurd'). "
            "Pass `query` voor body-zoek."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_history": {"type": "boolean", "default": False},
                "history_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                "query": {
                    **QUERY_SCHEMA,
                    "description": "Optionele LIKE-filter op reminder body (≥3 chars, geen wildcards)",
                },
            },
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by id.",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
    # --- Plaud transcripts ---
    {
        "name": "search_plaud_transcripts",
        "description": (
            "Search the user's Plaud voice-recording transcripts (meetings, "
            "calls, memos). Returns title, date, and matching excerpts. "
            "Query must be ≥3 chars without wildcards (%, _, *, ')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**QUERY_SCHEMA, "description": "Keywords to match against transcript bodies."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    },
    # --- Comm-intel (mail + Slack) ---
    *COMM_TOOL_SCHEMAS,
    # --- Open-loops (cross-source todo tracker) ---
    *LOOPS_TOOL_SCHEMAS,
    # --- Market-intel (digital signage + AI news) ---
    *MARKET_INTEL_TOOL_SCHEMAS,
    # --- Scheduler-assist (concept-mailer for meeting requests) ---
    *SCHEDULER_TOOL_SCHEMAS,
    # --- Person-brief (per-contact dossier across all sources) ---
    *PERSON_BRIEF_TOOL_SCHEMAS,
    # --- Birthdays + jubilea ---
    *BIRTHDAY_TOOL_SCHEMAS,
    # --- Decision log ---
    *DECISIONS_TOOL_SCHEMAS,
    # --- Expenses ---
    *EXPENSES_TOOL_SCHEMAS,
    # --- OKRs (kwartaal-objectieven) ---
    *OKR_TOOL_SCHEMAS,
    # --- Projects (active initiatives) ---
    *PROJECT_TOOL_SCHEMAS,
    # --- Patterns (behavior trends) ---
    *PATTERN_TOOL_SCHEMAS,
    # --- Receipt-collector (kwartaal-bonnen vinden) ---
    *RECEIPT_TOOL_SCHEMAS,
    # --- Config wishes (the user's structurele preferences) ---
    *CONFIG_WISHES_TOOL_SCHEMAS,
    # --- English collocations practice ---
    *ENGLISH_PRACTICE_TOOL_SCHEMAS,
    # --- Memory cards (vrije-tekst kennis via iMessage) ---
    *MEMORY_TOOL_SCHEMAS,
    # --- Uptime on-demand rapporten ---
    *UPTIME_TOOL_SCHEMAS,
    # --- TenderNed aanbestedingen ---
    *TENDER_TOOL_SCHEMAS,
    # --- Faillissementen + KvK-watchlist ---
    *INSOLVENCIES_TOOL_SCHEMAS,
    # --- Sales pipeline ---
    *SALES_TOOL_SCHEMAS,
    # --- Todoist (lezen + completen + updaten, parallel aan sync-worker) ---
    *TODOIST_TOOL_SCHEMAS,
    # --- whats_open: cross-channel "wat heb ik open"-aggregator ---
    *WHATS_OPEN_TOOL_SCHEMAS,
    # --- user_profile: Rosa-aware "wie is the user" ---
    *USER_PROFILE_TOOL_SCHEMAS,
    # --- Anthropic server-side web_search (28/6): Claude voert de
    # query zelf uit; geen lokale handler. Sub-processor is Anthropic
    # (al bestaand) — geen nieuwe DPA-actie. max_uses begrenst kosten
    # per turn.
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
    },
]


# ------------------------------ executor -----------------------------------

@dataclass
class ToolContext:
    gmail: GmailClient
    calendar: CalendarClient
    db_path: Path
    user_handle: str  # where reminders go
    imap_accounts: list[ImapAccount] = field(default_factory=list)
    vip_path: Path | None = None      # config/vip_contacts.yaml — voor person_brief
    okrs_path: Path | None = None     # config/okrs.yaml — voor okrs_*
    gateway: Any = None               # privacy.gateway.Gateway — voor LLM-tools
    ollama: Any = None                # OllamaClient — voor lokale LLM fallback (vendor-extract)
    todoist_client: Any = None        # integrations.todoist.TodoistClient — voor todoist_* tools
    todoist_project_id: str | None = None  # ID van the user's pa-agent Todoist-project
    user_profile_path: Path | None = None  # config/user_profile.yaml
    settings: Any = None               # core.config.Settings — voor prompt_builder / user_name


class ToolExecutor:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "get_current_time": self._get_current_time,
            "set_timezone": self._set_timezone,
            "get_timezone": self._get_timezone,
            "gmail_list_recent": self._gmail_list_recent,
            "gmail_search": self._gmail_search,
            "gmail_get_thread": self._gmail_get_thread,
            "gmail_send": self._gmail_send,
            "gmail_mark_read": self._gmail_mark_read,
            "calendar_list_today": self._calendar_list_today,
            "calendar_list_events": self._calendar_list_events,
            "calendar_search_events": self._calendar_search_events,
            "calendar_find_free_slots": self._calendar_find_free_slots,
            "calendar_create_event": self._calendar_create_event,
            "calendar_update_event": self._calendar_update_event,
            "calendar_delete_event": self._calendar_delete_event,
            "set_reminder": self._set_reminder,
            "list_reminders": self._list_reminders,
            "cancel_reminder": self._cancel_reminder,
            "search_plaud_transcripts": self._search_plaud,
            # Comm-intel handlers — bound to db_path via lambda capture.
            # comm_thread_summary needs gateway extra; rest is db-only.
            **{
                name: (
                    (lambda args, h=handler: h(self._ctx.db_path, args, gateway=self._ctx.gateway))
                    if name == "comm_thread_summary"
                    else (lambda args, h=handler: h(self._ctx.db_path, args))
                )
                for name, handler in COMM_HANDLERS.items()
            },
            # Open-loops handlers — same binding pattern.
            **{
                name: (lambda args, h=handler: h(self._ctx.db_path, args))
                for name, handler in LOOPS_HANDLERS.items()
            },
            # Market-intel handlers — same binding pattern.
            **{
                name: (lambda args, h=handler: h(self._ctx.db_path, args))
                for name, handler in MARKET_INTEL_HANDLERS.items()
            },
            # Scheduler-assist handlers — pass extra deps for send_proposal.
            "proposals_list": (lambda args: SCHEDULER_HANDLERS["proposals_list"](
                self._ctx.db_path, args)),
            "send_proposal": (lambda args: SCHEDULER_HANDLERS["send_proposal"](
                self._ctx.db_path, args,
                gmail=self._ctx.gmail, calendar=self._ctx.calendar,
                imap_accounts=self._ctx.imap_accounts)),
            "cancel_proposal": (lambda args: SCHEDULER_HANDLERS["cancel_proposal"](
                self._ctx.db_path, args)),
            # Person-brief — needs calendar (for upcoming events) + vip_path + ollama for summary.
            "person_brief": (lambda args: PERSON_BRIEF_HANDLERS["person_brief"](
                self._ctx.db_path, args,
                calendar=self._ctx.calendar,
                vip_path=self._ctx.vip_path or (self._ctx.db_path.parent.parent
                                                 / "config" / "vip_contacts.yaml"),
                ollama=self._ctx.ollama)),
            # Birthdays — needs vip_path.
            "upcoming_birthdays": (lambda args: BIRTHDAY_HANDLERS["upcoming_birthdays"](
                self._ctx.db_path, args,
                vip_path=self._ctx.vip_path or (self._ctx.db_path.parent.parent
                                                 / "config" / "vip_contacts.yaml"))),
            # Decisions — db-only; log_decision needs ollama for auto-tagging.
            **{
                name: (
                    (lambda args, h=handler: h(self._ctx.db_path, args, ollama=self._ctx.ollama))
                    if name == "log_decision"
                    else (lambda args, h=handler: h(self._ctx.db_path, args))
                )
                for name, handler in DECISIONS_HANDLERS.items()
            },
            # Expenses — db-only (folder-watcher draait in scheduler).
            **{
                name: (lambda args, h=handler: h(self._ctx.db_path, args))
                for name, handler in EXPENSES_HANDLERS.items()
            },
            # OKRs — yaml-only; okrs_check needs gateway.
            "okrs_list": (lambda args: OKR_HANDLERS["okrs_list"](
                self._okrs_path(), args)),
            "okrs_check": (lambda args: OKR_HANDLERS["okrs_check"](
                self._okrs_path(), args, gateway=self._ctx.gateway,
                settings=self._ctx.settings)),
            "okrs_update_progress": (lambda args: OKR_HANDLERS["okrs_update_progress"](
                self._okrs_path(), args)),
            # Projects — db-only; project_status needs calendar voor upcoming.
            "project_list": (lambda args: PROJECT_HANDLERS["project_list"](
                self._ctx.db_path, args)),
            "project_create": (lambda args: PROJECT_HANDLERS["project_create"](
                self._ctx.db_path, args)),
            "project_update": (lambda args: PROJECT_HANDLERS["project_update"](
                self._ctx.db_path, args)),
            "project_status": (lambda args: PROJECT_HANDLERS["project_status"](
                self._ctx.db_path, args, calendar=self._ctx.calendar)),
            # Patterns — db-only.
            **{
                name: (lambda args, h=handler: h(self._ctx.db_path, args))
                for name, handler in PATTERN_HANDLERS.items()
            },
            # Receipt-collector — start needs gmail + imap accounts + ollama.
            "receipt_run_start": (lambda args: RECEIPT_HANDLERS["receipt_run_start"](
                self._ctx.db_path, args,
                gmail=self._ctx.gmail,
                imap_accounts=self._receipt_imap_pairs(),
                ollama=self._ctx.ollama,
            )),
            "receipt_run_status": (lambda args: RECEIPT_HANDLERS["receipt_run_status"](
                self._ctx.db_path, args)),
            "receipt_runs_list": (lambda args: RECEIPT_HANDLERS["receipt_runs_list"](
                self._ctx.db_path, args)),
            "vendor_strategy_remember": (lambda args: RECEIPT_HANDLERS["vendor_strategy_remember"](
                self._ctx.db_path, args)),
            "vendor_strategies_list": (lambda args: RECEIPT_HANDLERS["vendor_strategies_list"](
                self._ctx.db_path, args)),
            # Config wishes — add captures user_handle for traceerbaarheid.
            "add_config_wish": (lambda args: CONFIG_WISHES_HANDLERS["add_config_wish"](
                self._ctx.db_path, args, source_handle=self._ctx.user_handle)),
            "config_wishes_list": (lambda args: CONFIG_WISHES_HANDLERS["config_wishes_list"](
                self._ctx.db_path, args)),
            "config_wish_set_status": (lambda args: CONFIG_WISHES_HANDLERS["config_wish_set_status"](
                self._ctx.db_path, args)),
            # English practice — evaluate needs the gateway for Claude scoring.
            "english_practice_start": (lambda args: ENGLISH_PRACTICE_HANDLERS["english_practice_start"](
                self._ctx.db_path, args)),
            "english_practice_evaluate": (lambda args: ENGLISH_PRACTICE_HANDLERS["english_practice_evaluate"](
                self._ctx.db_path, args, gateway=self._ctx.gateway)),
            "english_practice_skip": (lambda args: ENGLISH_PRACTICE_HANDLERS["english_practice_skip"](
                self._ctx.db_path, args)),
            "english_practice_status": (lambda args: ENGLISH_PRACTICE_HANDLERS["english_practice_status"](
                self._ctx.db_path, args)),
            "english_practice_end": (lambda args: ENGLISH_PRACTICE_HANDLERS["english_practice_end"](
                self._ctx.db_path, args)),
            # Memory cards (vrije-tekst kennis via iMessage)
            "add_memory": (lambda args: MEMORY_HANDLERS["add_memory"](
                self._ctx.db_path, args)),
            "recall": (lambda args: MEMORY_HANDLERS["recall"](
                self._ctx.db_path, args)),
            "list_memories": (lambda args: MEMORY_HANDLERS["list_memories"](
                self._ctx.db_path, args)),
            "forget_memory": (lambda args: MEMORY_HANDLERS["forget_memory"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            # Uptime on-demand rapport
            "uptime_report": (lambda args: UPTIME_HANDLERS["uptime_report"](
                self._ctx.db_path, args)),
            # TenderNed aanbestedingen
            "tenders_list_recent": (lambda args: TENDER_HANDLERS["tenders_list_recent"](
                self._ctx.db_path, args)),
            "tenders_search":      (lambda args: TENDER_HANDLERS["tenders_search"](
                self._ctx.db_path, args)),
            "tenders_ignore":      (lambda args: TENDER_HANDLERS["tenders_ignore"](
                self._ctx.db_path, args)),
            "tenders_status":      (lambda args: TENDER_HANDLERS["tenders_status"](
                self._ctx.db_path, args)),
            # Faillissementen + watchlist
            "insolvencies_list_recent": (lambda args: INSOLVENCIES_HANDLERS["insolvencies_list_recent"](
                self._ctx.db_path, args)),
            "insolvencies_search": (lambda args: INSOLVENCIES_HANDLERS["insolvencies_search"](
                self._ctx.db_path, args)),
            "insolvencies_ignore": (lambda args: INSOLVENCIES_HANDLERS["insolvencies_ignore"](
                self._ctx.db_path, args)),
            "insolvencies_status": (lambda args: INSOLVENCIES_HANDLERS["insolvencies_status"](
                self._ctx.db_path, args)),
            "insolvency_watchlist_add": (lambda args: INSOLVENCIES_HANDLERS["insolvency_watchlist_add"](
                self._ctx.db_path, args)),
            "insolvency_watchlist_remove": (lambda args: INSOLVENCIES_HANDLERS["insolvency_watchlist_remove"](
                self._ctx.db_path, args)),
            "insolvency_watchlist_list": (lambda args: INSOLVENCIES_HANDLERS["insolvency_watchlist_list"](
                self._ctx.db_path, args)),
            # Sales pipeline
            "sales_account_add": (lambda args: SALES_HANDLERS["sales_account_add"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            "sales_account_update": (lambda args: SALES_HANDLERS["sales_account_update"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            "sales_account_set_status": (lambda args: SALES_HANDLERS["sales_account_set_status"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            "sales_account_snooze": (lambda args: SALES_HANDLERS["sales_account_snooze"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            "sales_account_list": (lambda args: SALES_HANDLERS["sales_account_list"](
                self._ctx.db_path, args)),
            "sales_account_search": (lambda args: SALES_HANDLERS["sales_account_search"](
                self._ctx.db_path, args)),
            "sales_account_forget": (lambda args: SALES_HANDLERS["sales_account_forget"](
                self._ctx.db_path, args,
                actor=self._ctx.user_handle or "imessage")),
            "sales_touchpoint_log": (lambda args: SALES_HANDLERS["sales_touchpoint_log"](
                self._ctx.db_path, args)),
            "sales_touchpoint_history": (lambda args: SALES_HANDLERS["sales_touchpoint_history"](
                self._ctx.db_path, args)),
            "sales_top3_today": (lambda args: SALES_HANDLERS["sales_top3_today"](
                self._ctx.db_path, args)),
            "sales_why": (lambda args: SALES_HANDLERS["sales_why"](
                self._ctx.db_path, args)),
            "sales_pipeline_status": (lambda args: SALES_HANDLERS["sales_pipeline_status"](
                self._ctx.db_path, args)),
            # Todoist (lees + complete + update)
            "todoist_list_open_tasks": (lambda args: TODOIST_HANDLERS["todoist_list_open_tasks"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_complete_task": (lambda args: TODOIST_HANDLERS["todoist_complete_task"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_update_task": (lambda args: TODOIST_HANDLERS["todoist_update_task"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_search": (lambda args: TODOIST_HANDLERS["todoist_search"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_create_task": (lambda args: TODOIST_HANDLERS["todoist_create_task"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_cleanup_suggest": (lambda args: TODOIST_HANDLERS["todoist_cleanup_suggest"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_cleanup_apply": (lambda args: TODOIST_HANDLERS["todoist_cleanup_apply"](
                self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_review_queue_list": (lambda args: TODOIST_HANDLERS["todoist_review_queue_list"](
                self._ctx.db_path, self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_review_queue_approve": (lambda args: TODOIST_HANDLERS["todoist_review_queue_approve"](
                self._ctx.db_path, self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            "todoist_review_queue_reject": (lambda args: TODOIST_HANDLERS["todoist_review_queue_reject"](
                self._ctx.db_path, self._ctx.todoist_client, self._ctx.todoist_project_id, args)),
            # Cross-channel "wat heb ik open"-aggregator
            "whats_open": (lambda args: WHATS_OPEN_HANDLERS["whats_open"](
                self._ctx.db_path, args,
                todoist_client=self._ctx.todoist_client,
                todoist_project_id=self._ctx.todoist_project_id,
                user_handle=self._ctx.user_handle)),
            # User-profile (wie is the user)
            "user_profile_get": (lambda args: USER_PROFILE_HANDLERS["user_profile_get"](
                self._ctx.user_profile_path, args)),
            "user_profile_update": (lambda args: USER_PROFILE_HANDLERS["user_profile_update"](
                self._ctx.user_profile_path, args)),
        }

    def _receipt_imap_pairs(self) -> list[tuple[Any, str]]:
        """Build (account, password) tuples for receipt-search. Reuses the
        Keychain-loaded imap_accounts from ToolContext + fetches passwords."""
        from integrations.imap import get_password as _imap_get_pw
        out: list[tuple[Any, str]] = []
        for acc in self._ctx.imap_accounts:
            if not acc.enabled:
                continue
            pw = _imap_get_pw(acc)
            if pw:
                out.append((acc, pw))
        return out

    def _okrs_path(self) -> Path:
        return self._ctx.okrs_path or (self._ctx.db_path.parent.parent
                                        / "config" / "okrs.yaml")

    def execute(self, name: str, args: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            result = handler(args or {})
            return json.dumps(result, default=str, ensure_ascii=False)
        except Exception as exc:
            log.exception("tool %s failed", name)
            # ISO_AUDIT 2026-05 HIGH-2: tracebacks leakten absolute file
            # paths, module names, en stack-frame variables naar Claude
            # (en daarmee naar Anthropic's logs + iMessage). Houd nu alleen
            # type-name en first-line message (max 200 chars). Volledige
            # traceback gaat naar agent.log via log.exception().
            first_line = str(exc).splitlines()[0] if str(exc) else ""
            return json.dumps({
                "error": f"{type(exc).__name__}: {first_line[:200]}",
            })

    # --- handlers ---

    def _get_current_time(self, _args: dict[str, Any]) -> dict[str, Any]:
        # Use active TZ zodat Claude in dezelfde tijdzone denkt als
        # the user (bv. tijdens een trip — "rosa tz America/Los_Angeles").
        from core.timezone import current_tz
        tz = current_tz()
        now = datetime.now(tz)
        return {
            "iso": now.isoformat(),
            "weekday": now.strftime("%A"),
            "date": now.date().isoformat(),
            "timezone": str(tz),
        }

    def _set_timezone(self, args: dict[str, Any]) -> dict[str, Any]:
        """Switch active timezone. Accepts IANA-zone OR alias
        ('home'/'reset'/'off') om naar default terug te vallen."""
        from core.timezone import set_active_timezone, current_tz, default_tz_name
        from core.audit import log_admin_action
        from zoneinfo import ZoneInfo
        raw_input = args.get("timezone")
        if isinstance(raw_input, (list, tuple, dict)):
            return {"ok": False, "error": "timezone must be a string"}
        raw = str(raw_input or "").strip()
        # Snapshot huidige TZ voor audit-trail before we mutate.
        old_tz = str(current_tz())
        try:
            set_active_timezone(db_path=self._ctx.db_path, name=raw or None)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Admin-action audit (A.12.4.3): wie zette de TZ, vanwaar, naar wat.
        new_tz = str(current_tz())
        log_admin_action(
            action="set_timezone",
            actor=self._ctx.user_handle or "imessage",
            from_value=old_tz, to_value=new_tz,
            raw_input=raw,
        )
        new_tz = current_tz()
        new_name = str(new_tz)
        default = default_tz_name()
        now_local = datetime.now(new_tz)
        now_default = now_local.astimezone(ZoneInfo(default))
        offset_seconds = (
            (now_local.utcoffset() or timedelta()).total_seconds()
            - (now_default.utcoffset() or timedelta()).total_seconds()
        )
        # M1: support fractional hours (India +5:30, Nepal +5:45, etc.)
        offset_hours = offset_seconds / 3600.0
        return {
            "ok": True,
            "active_timezone": new_name,
            "default_timezone": default,
            "offset_hours_vs_default": offset_hours,
            "now_local": now_local.strftime("%H:%M %Z"),
            "now_default": now_default.strftime("%H:%M %Z"),
            "note": (
                "Briefings, dayclose, midday en CEO-letter vuren nu om "
                "hun ingestelde HH:MM in deze zone."
            ),
        }

    def _get_timezone(self, _args: dict[str, Any]) -> dict[str, Any]:
        from core.timezone import current_tz, default_tz_name
        from zoneinfo import ZoneInfo
        active = current_tz()
        active_name = str(active)
        default = default_tz_name()
        now_active = datetime.now(active)
        now_default = now_active.astimezone(ZoneInfo(default))
        return {
            "active_timezone": active_name,
            "default_timezone": default,
            "is_override": active_name != default,
            "now_local": now_active.strftime("%H:%M %Z"),
            "now_default": now_default.strftime("%H:%M %Z"),
        }

    def _gmail_list_recent(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return self._ctx.gmail.list_recent(max_results=a.get("max_results", 10), query=a.get("query"))

    def _gmail_search(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return self._ctx.gmail.search(query=a["query"], max_results=a.get("max_results", 20))

    def _gmail_get_thread(self, a: dict[str, Any]) -> dict[str, Any]:
        return self._ctx.gmail.get_thread(a["thread_id"])

    def _gmail_send(self, a: dict[str, Any]) -> dict[str, Any]:
        return self._ctx.gmail.send(
            to=a["to"], subject=a["subject"], body=a["body"],
            cc=a.get("cc"), in_reply_to_thread=a.get("in_reply_to_thread"),
        )

    def _gmail_mark_read(self, a: dict[str, Any]) -> dict[str, Any]:
        self._ctx.gmail.mark_read(a["message_id"])
        return {"ok": True}

    def _calendar_list_today(self, _a: dict[str, Any]) -> list[dict[str, Any]]:
        return self._ctx.calendar.list_today()

    def _calendar_list_events(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return self._ctx.calendar.list_events(
            time_min=_parse_dt(a["time_min"]), time_max=_parse_dt(a["time_max"]),
            max_results=a.get("max_results", 50),
        )

    def _calendar_search_events(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("Europe/Amsterdam"))
        return self._ctx.calendar.search_events(
            query=str(a["query"]),
            time_min=now - _td(days=int(a.get("days_back", 14))),
            time_max=now + _td(days=int(a.get("days_forward", 60))),
            max_results=int(a.get("max_results", 25)),
        )

    def _calendar_find_free_slots(self, a: dict[str, Any]) -> list[dict[str, str]]:
        return self._ctx.calendar.find_free_slots(
            duration_minutes=a["duration_minutes"],
            earliest=_parse_dt(a["earliest"]),
            latest=_parse_dt(a["latest"]),
            work_start_hour=a.get("work_start_hour", 9),
            work_end_hour=a.get("work_end_hour", 18),
        )

    def _calendar_create_event(self, a: dict[str, Any]) -> dict[str, Any]:
        return self._ctx.calendar.create_event(
            title=a["title"], start=_parse_dt(a["start"]), end=_parse_dt(a["end"]),
            description=a.get("description"), location=a.get("location"),
            attendees=a.get("attendees"),
            add_meet_link=bool(a.get("add_meet_link", False)),
            recurrence=_build_recurrence(a.get("recurrence")),
        )

    def _calendar_update_event(self, a: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for k in ("title", "description", "location"):
            if k in a:
                fields[k] = a[k]
        for k in ("start", "end"):
            if k in a:
                fields[k] = _parse_dt(a[k])
        if "recurrence" in a:
            # Expliciete None / [] → recurrence verwijderen (eenmalig maken).
            # Anders → bouw RRULE.
            rec = a["recurrence"]
            fields["recurrence"] = (
                _build_recurrence(rec) if rec else []
            )
        return self._ctx.calendar.update_event(a["event_id"], **fields)

    def _calendar_delete_event(self, a: dict[str, Any]) -> dict[str, Any]:
        self._ctx.calendar.delete_event(a["event_id"])
        return {"ok": True}

    def _set_reminder(self, a: dict[str, Any]) -> dict[str, Any]:
        when = _parse_dt(a["when"])
        body = str(a["body"])
        force = bool(a.get("force", False))

        # Preventieve duplicate-check (kan geskipt via force=True nadat
        # the user expliciet 'oké, twee stuks dan' zei).
        if not force:
            from extensions.reminders_dedup import find_similar
            candidates = find_similar(
                db_path=self._ctx.db_path,
                handle=self._ctx.user_handle,
                new_body=body,
                todoist_client=self._ctx.todoist_client,
                todoist_project_id=self._ctx.todoist_project_id,
            )
            if candidates:
                return {
                    "needs_confirmation": True,
                    "reason": "possible duplicates found",
                    "candidates": candidates,
                    "note": (
                        "Ask the user which action: replace an existing "
                        "one (cancel_reminder + retry set_reminder with "
                        "force=true), skip creation, or force=true if "
                        "he wants both."
                    ),
                }

        with _conn(self._ctx.db_path) as conn:
            rid = reminders.add_reminder(conn, handle=self._ctx.user_handle, remind_at=when, body=body)
        return {"reminder_id": rid, "scheduled_for": when.isoformat()}

    def _list_reminders(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        query = (a.get("query") or "").strip()
        if query:
            ok, _err = validate_query(query)
            if not ok:
                query = ""  # treat as no-filter rather than empty result
            else:
                query = query.translate(str.maketrans("", "", "%_"))
        with _conn(self._ctx.db_path) as conn:
            return reminders.list_pending(
                conn, handle=self._ctx.user_handle,
                include_history=bool(a.get("include_history", False)),
                history_days=int(a.get("history_days", 30)),
                query=(query or None),
            )

    def _cancel_reminder(self, a: dict[str, Any]) -> dict[str, Any]:
        with _conn(self._ctx.db_path) as conn:
            ok = reminders.cancel_reminder(conn, int(a["reminder_id"]))
        return {"ok": ok}

    def _search_plaud(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        q = str(a.get("query") or "").strip()
        ok, _err = validate_query(q)
        if not ok:
            return []
        q = q.translate(str.maketrans("", "", "%_"))
        if not q:
            return []
        limit = int(a.get("limit", 5))
        with _conn(self._ctx.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT id, recorded_at, title, substr(body, 1, 400) AS excerpt "
                    "FROM plaud_transcripts "
                    "WHERE body LIKE ? OR title LIKE ? "
                    "ORDER BY recorded_at DESC LIMIT ?",
                    (f"%{q}%", f"%{q}%", limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]


# ------------------------------ helpers ------------------------------------

def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


_VALID_WEEKDAYS = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})
_VALID_FREQ = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})


def _build_recurrence(rec: Any) -> list[str] | None:
    """Convert structured recurrence-object (of raw RRULE) naar het
    list-of-RRULE-strings format dat Google Calendar verwacht.

    Accepteert drie vormen:
    - None / leeg: geen recurrence → returnt None
    - str startend met "RRULE:": passed-through (caller weet wat hij doet)
    - dict: {"freq": "WEEKLY", "interval": 2, "by_weekday": ["MO","WE"],
            "until": "YYYY-MM-DD" of "YYYY-MM-DDTHH:MM:SS", "count": N}

    Validation: onbekende freq of weekday → ValueError. count én until
    tegelijk → ValueError (RFC 5545 verbiedt).
    """
    if rec is None or rec == "":
        return None
    if isinstance(rec, str):
        # Raw RRULE — minimal validation
        s = rec.strip()
        if not s.upper().startswith("RRULE:"):
            raise ValueError(
                "raw recurrence-string moet beginnen met 'RRULE:'"
            )
        return [s]
    if isinstance(rec, list):
        # Already a list of RRULE/RDATE strings — pass through
        return [str(x) for x in rec if str(x).strip()]
    if not isinstance(rec, dict):
        raise ValueError(f"recurrence type {type(rec).__name__} niet ondersteund")

    freq = str(rec.get("freq", "")).upper().strip()
    if freq not in _VALID_FREQ:
        raise ValueError(
            f"recurrence.freq moet één van {sorted(_VALID_FREQ)} zijn, kreeg {freq!r}"
        )
    parts = [f"FREQ={freq}"]

    interval = rec.get("interval")
    if interval is not None:
        try:
            n = int(interval)
        except (TypeError, ValueError):
            raise ValueError(f"recurrence.interval moet int zijn, kreeg {interval!r}")
        if n < 1:
            raise ValueError("recurrence.interval moet ≥ 1")
        if n > 1:
            parts.append(f"INTERVAL={n}")

    by_weekday = rec.get("by_weekday")
    if by_weekday:
        if isinstance(by_weekday, str):
            by_weekday = [by_weekday]
        normalized = [str(d).upper().strip() for d in by_weekday]
        unknown = [d for d in normalized if d not in _VALID_WEEKDAYS]
        if unknown:
            raise ValueError(
                f"recurrence.by_weekday bevat onbekende dagen: {unknown}. "
                f"Geldig: {sorted(_VALID_WEEKDAYS)}"
            )
        parts.append(f"BYDAY={','.join(normalized)}")

    by_month_day = rec.get("by_month_day")
    if by_month_day is not None:
        try:
            d = int(by_month_day)
        except (TypeError, ValueError):
            raise ValueError(
                f"recurrence.by_month_day moet int 1-31 zijn, kreeg {by_month_day!r}"
            )
        if not 1 <= d <= 31:
            raise ValueError("recurrence.by_month_day moet 1-31 zijn")
        parts.append(f"BYMONTHDAY={d}")

    count = rec.get("count")
    until = rec.get("until")
    if count is not None and until is not None:
        raise ValueError(
            "geef count OF until, niet beide (RFC 5545 verbiedt het)"
        )
    if count is not None:
        try:
            c = int(count)
        except (TypeError, ValueError):
            raise ValueError(f"recurrence.count moet int zijn, kreeg {count!r}")
        if c < 1:
            raise ValueError("recurrence.count moet ≥ 1")
        parts.append(f"COUNT={c}")
    if until is not None:
        # Parse als datetime of date, format als UTC RFC 5545
        try:
            dt = _parse_dt(str(until))
        except ValueError as e:
            raise ValueError(f"recurrence.until ongeldig: {e}")
        # UTC, geen scheidings-tekens
        utc_dt = dt.astimezone(timezone.utc)
        parts.append(f"UNTIL={utc_dt.strftime('%Y%m%dT%H%M%SZ')}")

    return [f"RRULE:{';'.join(parts)}"]
