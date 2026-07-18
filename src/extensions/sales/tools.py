"""Claude-tools voor sales-pipeline beheer via iMessage."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .schema import VALID_PROSPECT_TYPES, VALID_STATUSES, VALID_TARGETS
from .selection import select_top_n
from .storage import (
    find_account_by_name, forget_account, get_account, insert_account,
    insert_touchpoint, list_accounts, list_touchpoints, search_accounts,
    snooze_account, update_account,
)


def _log_admin(
    *, action: str, actor: str, from_value=None, to_value=None,
    reason: str | None = None, **extra,
) -> None:
    """H4 review-fix: hook in core.audit.log_admin_action. No-op als
    de logger niet gebound is (tests/scripts)."""
    try:
        from core.audit import log_admin_action
        log_admin_action(
            action=action, actor=actor,
            from_value=from_value, to_value=to_value,
            reason=reason, **extra,
        )
    except Exception:
        pass


VALID_CHANNELS = ("email_in", "email_out", "linkedin", "call",
                   "meeting", "plaud", "imessage", "slack", "other")
VALID_OUTCOMES = ("positive", "neutral", "negative", "no_response")


def _coerce_sub_targets(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    if not isinstance(raw, list):
        return None
    cleaned = [str(s).strip().lower() for s in raw if str(s).strip()]
    return cleaned or None


# ---- accounts -----------------------------------------------------------

def sales_account_add_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    naam = (args.get("naam") or "").strip()
    if not naam:
        return {"ok": False, "error": "naam is required"}
    target = (args.get("target") or "").strip().lower()
    if target not in VALID_TARGETS:
        return {"ok": False, "error": f"target moet één van {sorted(VALID_TARGETS)} zijn"}

    sub_targets = _coerce_sub_targets(args.get("sub_targets"))
    if target == "multi" and not sub_targets:
        return {"ok": False, "error": "sub_targets verplicht bij target=multi"}

    prospect_type = args.get("prospect_type")
    if prospect_type and prospect_type not in VALID_PROSPECT_TYPES:
        return {"ok": False, "error": f"prospect_type ongeldig"}

    status = args.get("status", "koud")
    if status not in VALID_STATUSES:
        return {"ok": False, "error": "status ongeldig"}

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        # Dedupe: bestaand account met zelfde naam?
        existing = find_account_by_name(conn, naam)
        if existing:
            return {
                "ok": False, "error": "account met deze naam bestaat al",
                "existing_id": existing["id"],
                "existing_target": existing["target"],
                "existing_status": existing["status"],
            }
        try:
            new_id = insert_account(
                conn,
                naam=naam, target=target, sub_targets=sub_targets,
                prospect_type=prospect_type,
                sector=args.get("sector"),
                plaats=args.get("plaats"),
                kvk=args.get("kvk"),
                website=args.get("website"),
                primary_contact_name=args.get("contact_name"),
                primary_contact_email=args.get("contact_email"),
                primary_contact_phone=args.get("contact_phone"),
                primary_contact_role=args.get("contact_role"),
                status=status,
                nurture_cadence_days=args.get("nurture_cadence_days"),
                estimated_value_eur=args.get("estimated_value_eur"),
                notes=args.get("notes"),
                created_via="imessage",
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        acc = get_account(conn, new_id)

    _log_admin(
        action="sales_account_add", actor=actor or "unknown",
        to_value={
            "id": new_id, "naam": acc["naam"], "target": acc["target"],
            "status": acc["status"],
            "kvk": acc.get("kvk"), "contact_email": acc.get("primary_contact_email"),
        },
    )
    return {"ok": True, "account": acc, "created_id": new_id}


def _resolve_account(
    conn: sqlite3.Connection, args: dict[str, Any],
) -> dict[str, Any] | None:
    """Pak account uit args via id of naam."""
    if args.get("account_id"):
        try:
            return get_account(conn, int(args["account_id"]))
        except (TypeError, ValueError):
            return None
    if args.get("naam"):
        return find_account_by_name(conn, str(args["naam"]))
    return None


def sales_account_update_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden (geef account_id of naam)"}

        before_status = acc.get("status")
        before_target = acc.get("target")

        fields: dict[str, Any] = {}
        for k in ("naam", "kvk", "website", "sector", "plaats", "notes",
                   "primary_contact_name", "primary_contact_email",
                   "primary_contact_phone", "primary_contact_role"):
            if k in args:
                fields[k] = args[k]
        if "contact_name" in args:
            fields["primary_contact_name"] = args["contact_name"]
        if "contact_email" in args:
            fields["primary_contact_email"] = args["contact_email"]
        if "contact_phone" in args:
            fields["primary_contact_phone"] = args["contact_phone"]
        if "contact_role" in args:
            fields["primary_contact_role"] = args["contact_role"]
        if "target" in args:
            fields["target"] = args["target"]
        if "sub_targets" in args:
            fields["sub_targets"] = _coerce_sub_targets(args["sub_targets"])
        if "prospect_type" in args:
            fields["prospect_type"] = args["prospect_type"]
        if "status" in args:
            fields["status"] = args["status"]
        if "nurture_cadence_days" in args:
            fields["nurture_cadence_days"] = args["nurture_cadence_days"]
        if "estimated_value_eur" in args:
            fields["estimated_value_eur"] = args["estimated_value_eur"]

        try:
            updated = update_account(conn, int(acc["id"]), **fields)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if updated:
        changes = {k: v for k, v in fields.items() if k != "sub_targets"}
        # Status/target wijzigingen apart loggen voor traceerbaarheid
        if (
            updated.get("status") != before_status
            or updated.get("target") != before_target
        ):
            _log_admin(
                action="sales_account_update_status_or_target",
                actor=actor or "unknown",
                from_value={"status": before_status, "target": before_target},
                to_value={"status": updated.get("status"),
                            "target": updated.get("target"),
                            "id": updated["id"], "naam": updated["naam"]},
            )
        elif changes:
            _log_admin(
                action="sales_account_update",
                actor=actor or "unknown",
                to_value={"id": updated["id"], "naam": updated["naam"],
                            "fields": list(changes.keys())},
            )
    return {"ok": True, "account": updated}


def sales_account_set_status_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    status = (args.get("status") or "").strip().lower()
    if status not in VALID_STATUSES:
        return {"ok": False, "error": f"status moet één van {sorted(VALID_STATUSES)}"}
    args2 = dict(args)
    args2["status"] = status
    return sales_account_update_handler(db_path, args2, actor=actor)


def sales_account_snooze_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    days = int(args.get("days") or 14)
    if days < 1 or days > 365:
        return {"ok": False, "error": "days moet tussen 1 en 365"}
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden"}
        snoozed = snooze_account(conn, int(acc["id"]), days=days)

    _log_admin(
        action="sales_account_snooze", actor=actor or "unknown",
        from_value={"id": snoozed["id"], "naam": snoozed["naam"]},
        to_value={"snoozed_until": snoozed["snoozed_until"], "days": days},
    )
    return {"ok": True, "account": snoozed}


def sales_account_list_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    target = args.get("target")
    status = args.get("status")
    limit = max(1, min(int(args.get("limit") or 50), 200))
    if target and target not in VALID_TARGETS:
        return {"ok": False, "error": "ongeldig target"}
    if status and status not in VALID_STATUSES:
        return {"ok": False, "error": "ongeldig status"}
    with sqlite3.connect(db_path) as conn:
        rows = list_accounts(conn, target=target, status=status, limit=limit)
    return {"ok": True, "count": len(rows), "accounts": rows}


def sales_account_search_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    q = (args.get("query") or "").strip()
    if len(q) < 2:
        return {"ok": False, "error": "query te kort (min 2)"}
    limit = max(1, min(int(args.get("limit") or 20), 50))
    with sqlite3.connect(db_path) as conn:
        rows = search_accounts(conn, q, limit=limit)
    return {"ok": True, "shown": len(rows), "accounts": rows}


def sales_account_forget_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    """M4 — hard-delete account + cascade touchpoints. Voor GDPR
    right-to-be-forgotten verzoeken. Audit-log behoudt een metadata-
    fingerprint (id, naam, kvk, contact-email) zodat we kunnen aantonen
    DAT we verwijderd hebben — zonder PII te kopiëren."""
    confirmed = bool(args.get("confirm", False))
    reason = (args.get("reason") or "").strip()[:300]
    if not confirmed:
        return {
            "ok": False,
            "error": "hard-delete vereist confirm=true (kan niet ongedaan worden gemaakt)",
        }
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden"}
        forgotten = forget_account(conn, int(acc["id"]))

    if forgotten:
        _log_admin(
            action="sales_account_forget", actor=actor or "unknown",
            from_value={
                "id": forgotten["id"], "naam": forgotten["naam"],
                "kvk": forgotten.get("kvk"),
                "contact_email": forgotten.get("primary_contact_email"),
                "status": forgotten.get("status"),
                "target": forgotten.get("target"),
            },
            reason=reason or "user-requested",
        )

    return {"ok": True, "deleted_id": acc["id"], "deleted_naam": acc["naam"]}


# ---- touchpoints --------------------------------------------------------

def sales_touchpoint_log_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    channel = (args.get("channel") or "").strip().lower()
    if channel not in VALID_CHANNELS:
        return {"ok": False, "error": f"channel moet één van {VALID_CHANNELS}"}
    outcome = args.get("outcome")
    if outcome and outcome not in VALID_OUTCOMES:
        return {"ok": False, "error": f"outcome moet één van {VALID_OUTCOMES}"}
    summary = (args.get("summary") or "").strip()[:500] or None

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden — geef account_id of naam"}
        tp_id = insert_touchpoint(
            conn, account_id=int(acc["id"]),
            channel=channel, summary=summary, outcome=outcome,
            source_ref=args.get("source_ref"),
        )
        refreshed = get_account(conn, int(acc["id"]))
    return {"ok": True, "touchpoint_id": tp_id, "account": refreshed}


def sales_touchpoint_history_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden"}
        rows = list_touchpoints(
            conn, int(acc["id"]),
            limit=max(1, min(int(args.get("limit") or 20), 100)),
        )
    return {"ok": True, "account_id": acc["id"], "account_naam": acc["naam"],
            "touchpoints": rows}


# ---- top3 + pipeline ----------------------------------------------------

def sales_top3_today_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    n = max(1, min(int(args.get("n") or 3), 10))
    selections = select_top_n(db_path, n=n)
    out = []
    for s in selections:
        out.append({
            "account": s.account,
            "reason_code": s.reason_code,
            "reason_text": s.reason_text,
            "suggestion": s.suggestion,
            "related_trigger_id": s.related_trigger_id,
        })
    return {"ok": True, "count": len(out), "selections": out}


def sales_why_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Voor 'waarom kies je dit account vandaag' — selecteer top-N en
    pak de uitleg voor het opgegeven account."""
    with sqlite3.connect(db_path) as conn:
        acc = _resolve_account(conn, args)
        if acc is None:
            return {"ok": False, "error": "account niet gevonden"}
    sels = select_top_n(db_path, n=10)
    for s in sels:
        if s.account["id"] == acc["id"]:
            return {
                "ok": True,
                "account_id": acc["id"], "account_naam": acc["naam"],
                "reason_code": s.reason_code,
                "reason_text": s.reason_text,
                "suggestion": s.suggestion,
            }
    return {
        "ok": True,
        "account_id": acc["id"], "account_naam": acc["naam"],
        "reason_code": "not_in_today_top",
        "reason_text": (
            "Niet in vandaag's top-3 — cadence nog niet vervallen of "
            "andere accounts hadden hogere prioriteit"
        ),
        "suggestion": "Geen actie nodig vandaag, blijft in pipeline",
    }


def sales_pipeline_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        by_target_status = conn.execute(
            "SELECT target, status, COUNT(*) AS n, "
            "       SUM(COALESCE(estimated_value_eur,0)) AS total_value "
            "FROM sales_accounts "
            "WHERE status NOT IN ('won','lost') "
            "GROUP BY target, status "
            "ORDER BY target, status"
        ).fetchall()
        won_this_quarter = conn.execute(
            "SELECT COUNT(*) AS n FROM sales_accounts "
            "WHERE status = 'won' "
            "AND won_at >= strftime('%s','now','start of month','-2 months')"
        ).fetchone()["n"]
        total_accounts = conn.execute(
            "SELECT COUNT(*) AS n FROM sales_accounts"
        ).fetchone()["n"]

    pipeline: dict[str, dict[str, dict[str, int]]] = {}
    for row in by_target_status:
        t = row["target"]
        s = row["status"]
        pipeline.setdefault(t, {})[s] = {
            "count": int(row["n"]),
            "estimated_value_eur": int(row["total_value"] or 0),
        }
    return {
        "ok": True,
        "total_accounts": int(total_accounts),
        "won_recent_quarter": int(won_this_quarter),
        "pipeline_by_target": pipeline,
    }


# ---- registratie --------------------------------------------------------

SALES_HANDLERS = {
    "sales_account_add":         sales_account_add_handler,
    "sales_account_update":      sales_account_update_handler,
    "sales_account_set_status":  sales_account_set_status_handler,
    "sales_account_snooze":      sales_account_snooze_handler,
    "sales_account_list":        sales_account_list_handler,
    "sales_account_search":      sales_account_search_handler,
    "sales_account_forget":      sales_account_forget_handler,
    "sales_touchpoint_log":      sales_touchpoint_log_handler,
    "sales_touchpoint_history":  sales_touchpoint_history_handler,
    "sales_top3_today":          sales_top3_today_handler,
    "sales_why":                 sales_why_handler,
    "sales_pipeline_status":     sales_pipeline_status_handler,
}


SALES_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "sales_account_add",
        "description": (
            "Voeg een prospect/account toe aan the user's sales-pipeline. "
            "`target` MOET één van: 'adl_video' (eindgebruikers narrowcasting NL), "
            "'dst_connect' (AV-resellers die DST-software doorverkopen), "
            "'ds_templates' (eindgebruikers + CMS-vendors voor API), "
            "of 'multi' (sub_targets dan verplicht). Triggers: 'voeg X toe als "
            "prospect voor ADL/DST/YourCompany', 'nieuwe lead Y voor ADL', "
            "'X wil narrowcasting via ons', 'partner-prospect Y voor YourProduct'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "naam": {"type": "string"},
                "target": {"type": "string",
                            "enum": ["adl_video", "dst_connect", "ds_templates", "multi"]},
                "sub_targets": {"type": "array",
                                 "items": {"type": "string"}},
                "prospect_type": {"type": "string",
                                   "enum": list(VALID_PROSPECT_TYPES)},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                "sector": {"type": "string"},
                "plaats": {"type": "string"},
                "kvk": {"type": "string"},
                "website": {"type": "string"},
                "contact_name": {"type": "string"},
                "contact_email": {"type": "string"},
                "contact_phone": {"type": "string"},
                "contact_role": {"type": "string"},
                "estimated_value_eur": {"type": "integer", "minimum": 0},
                "nurture_cadence_days": {"type": "integer", "minimum": 1},
                "notes": {"type": "string"},
            },
            "required": ["naam", "target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_update",
        "description": (
            "Update willekeurige velden van een account. Identificeer "
            "via account_id of naam. Voor status-changes liever "
            "sales_account_set_status gebruiken (next_touch wordt dan "
            "automatisch herberekend)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "target": {"type": "string", "enum": list(VALID_TARGETS)},
                "sub_targets": {"type": "array",
                                 "items": {"type": "string"}},
                "prospect_type": {"type": "string",
                                   "enum": list(VALID_PROSPECT_TYPES)},
                "sector": {"type": "string"},
                "plaats": {"type": "string"},
                "kvk": {"type": "string"},
                "website": {"type": "string"},
                "contact_name": {"type": "string"},
                "contact_email": {"type": "string"},
                "contact_phone": {"type": "string"},
                "contact_role": {"type": "string"},
                "estimated_value_eur": {"type": "integer"},
                "nurture_cadence_days": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_set_status",
        "description": (
            "Update de status van een account: koud → nurturing → "
            "kansrijk → offerte → won/lost. next_touch_at wordt "
            "herberekend op basis van nieuwe status + cadence. "
            "Triggers: 'X is nu kansrijk', 'offerte verzonden naar Y', "
            "'Z hebben we gewonnen', 'A lost, geen budget'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_snooze",
        "description": (
            "Stop opvolging voor N dagen — account verschijnt niet in "
            "daily top-3 tot snooze verloopt. Triggers: 'snooze X 2 "
            "weken', 'pauzeer Y tot juli', 'Z is op vakantie tot 1/9'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
            "required": ["days"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_list",
        "description": (
            "Lijst accounts, optioneel gefilterd op target en status. "
            "Triggers: 'toon alle prospects voor ADL', 'welke offertes "
            "staan open', 'mijn nurturing-lijst voor YourProduct'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": list(VALID_TARGETS)},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_search",
        "description": (
            "LIKE-search op naam, contact-email, contact-naam, kvk. "
            "Triggers: 'wat weten we over Heineken', 'staat Mediq al "
            "op de lijst'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_account_forget",
        "description": (
            "GDPR right-to-be-forgotten: hard-delete account + alle "
            "touchpoints. Vereist confirm=true expliciet. Audit-log "
            "behoudt metadata-fingerprint zonder PII zodat we kunnen "
            "aantonen DAT we deletten. Triggers: 'verwijder X', "
            "'forget X', 'X heeft AVG-verzoek gedaan'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "confirm": {"type": "boolean",
                             "description": "Moet true zijn — anders weiger."},
                "reason": {"type": "string", "maxLength": 300},
            },
            "required": ["confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_touchpoint_log",
        "description": (
            "Log een interactie met een account. Updates last_touch_at + "
            "next_touch_at automatisch zodat het opvolg-ritme klopt. "
            "Channel verplicht — kies de meest passende: email_out, "
            "linkedin, call, meeting, plaud. Triggers: 'ik heb Anne van "
            "X gesproken', 'mail verstuurd aan Y', 'LinkedIn-bericht naar "
            "Z', 'meeting met X gehad'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "channel": {"type": "string", "enum": list(VALID_CHANNELS)},
                "summary": {"type": "string", "maxLength": 500},
                "outcome": {"type": "string", "enum": list(VALID_OUTCOMES)},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_touchpoint_history",
        "description": (
            "Toon recente touchpoints voor een account. Triggers: "
            "'historie met X', 'wanneer hadden we voor het laatst "
            "Y gesproken'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_top3_today",
        "description": (
            "Welke 3 (default) accounts moet the user vandaag benaderen? "
            "Deterministisch algoritme: urgente offertes > nieuwe "
            "triggers > cadence-vervallen > koude account met signaal. "
            "Diversificeert over targets. Triggers: 'wie moet ik vandaag "
            "benaderen', 'top 3 sales vandaag', 'mijn 3 voor vandaag'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 10,
                       "description": "Default 3"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_why",
        "description": (
            "Leg uit waarom een account in de top-3 staat (of niet). "
            "Triggers: 'waarom X vandaag', 'waarom staat Y in de lijst'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer"},
                "naam": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sales_pipeline_status",
        "description": (
            "Overzicht van de pipeline per target: hoeveel accounts in "
            "elke status, totale geschatte waarde, recent gewonnen. "
            "Triggers: 'sales status', 'hoeveel offertes open', "
            "'pipeline overzicht', 'wat staat er voor ADL/DST/DS'."
        ),
        "input_schema": {"type": "object", "properties": {},
                          "additionalProperties": False},
    },
]
