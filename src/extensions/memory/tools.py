"""Tool handlers + schemas voor memory cards.

Vier tools:
- remember        — stores text + tags + optional entity-links
- recall          — semantic search over memories
- list_memories   — chronological listing met tag-filter
- forget_memory   — hard-delete by id
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from extensions.comm_intel.embeddings import embed

from .embeddings import search_by_vec, semantic_search, upsert_embedding  # noqa: F401
from .schema import (
    count_memories,
    delete_memory,
    get_memory,
    insert_memory,
    list_memories as db_list_memories,
    open_with_vec,
)

log = logging.getLogger(__name__)

# Hard caps — voorkomen dat Claude per ongeluk the user's hele db dumpt
MAX_RECALL_K = 10
MAX_LIST_LIMIT = 50
MIN_QUERY_LEN = 2
MAX_TEXT_LEN = 4000


# --- handlers ------------------------------------------------------------

_VALID_SOURCES = {"chat", "auto", "briefing"}


def add_memory_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Sla een memory-card op met semantic embedding."""
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    if len(text) > MAX_TEXT_LEN:
        return {"ok": False, "error": f"text exceeds {MAX_TEXT_LEN} chars"}

    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(t).strip() for t in tags if str(t).strip()]

    links = args.get("links") or args.get("linked_entities") or []
    if isinstance(links, str):
        links = [links]
    links = [str(l).strip() for l in links if str(l).strip()]

    # M3 — handler-level enum-validatie zodat directe callers (tests,
    # interne scripts) ook niet aan de JSON-schema-validation kunnen
    # ontsnappen met een onverwachte `source`-waarde.
    source = (args.get("source") or "chat").strip() or "chat"
    if source not in _VALID_SOURCES:
        source = "chat"

    conn = open_with_vec(Path(db_path))
    try:
        memory_id = insert_memory(
            conn,
            text=text,
            tags=tags,
            source=source,
            linked_entities=links,
        )
        # Embed + opslaan. Bij embed-fail bewaren we de memory wel (text-only) —
        # later kunnen we hem alsnog embedden via een back-fill-tick.
        vec = embed(text, kind="document")
        if vec is not None:
            upsert_embedding(conn, memory_id, vec)
            embedded = True
        else:
            log.warning("memory %d stored without embedding (Ollama down?)", memory_id)
            embedded = False
    finally:
        conn.close()

    return {
        "ok": True,
        "memory_id": memory_id,
        "embedded": embedded,
        "tags": tags,
        "linked_entities": links,
    }


def recall_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Semantic search over memories."""
    query = (args.get("query") or "").strip()
    if len(query) < MIN_QUERY_LEN:
        return {"ok": False, "error": f"query too short (min {MIN_QUERY_LEN} chars)"}
    k = max(1, min(int(args.get("k") or 5), MAX_RECALL_K))
    tag_filter = args.get("tags") or None
    if tag_filter and isinstance(tag_filter, str):
        tag_filter = [tag_filter]

    # M1 — als de query-embedding faalt moet recall onderscheidbaar zijn
    # van "echt niets gevonden". Embed hier expliciet zodat we het
    # signaal kunnen doorgeven aan Claude.
    qvec = embed(query, kind="query")
    if qvec is None:
        return {
            "ok": True,
            "query": query,
            "results": [],
            "count": 0,
            "degraded": True,
            "error": "embedding service unavailable (Ollama down?) — "
                     "could not perform semantic search; result is NOT "
                     "authoritative",
        }

    conn = open_with_vec(Path(db_path))
    try:
        # Re-use de al gegenereerde qvec (we deden embed() bovenaan
        # zodat we de degraded-status konden detecteren).
        hits = search_by_vec(conn, qvec=qvec, k=k * 3 if tag_filter else k)
        if not hits:
            return {"ok": True, "query": query, "results": [], "count": 0}
        # Laad de actual rows
        results: list[dict[str, Any]] = []
        for memory_id, distance in hits:
            mem = get_memory(conn, memory_id)
            if mem is None:
                continue
            if tag_filter:
                if not any(t in mem["tags"] for t in tag_filter):
                    continue
            mem["distance"] = round(distance, 4)
            results.append(mem)
            if len(results) >= k:
                break
    finally:
        conn.close()

    return {"ok": True, "query": query, "results": results, "count": len(results)}


def list_memories_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Chronologische listing, meest recente eerst."""
    tags = args.get("tags") or None
    if tags and isinstance(tags, str):
        tags = [tags]
    since = args.get("since") or None
    limit = min(int(args.get("limit") or 20), MAX_LIST_LIMIT)

    conn = open_with_vec(Path(db_path))
    try:
        rows = db_list_memories(conn, tags=tags, since=since, limit=limit)
        total = count_memories(conn)
    finally:
        conn.close()
    return {"ok": True, "memories": rows, "shown": len(rows), "total": total}


def forget_memory_handler(
    db_path: Path, args: dict[str, Any], *, actor: str | None = None,
) -> dict[str, Any]:
    """Hard-delete een memory-card by id.

    `actor` (kwarg) wordt mee gelogd naar `data/audit/admin-*.jsonl`
    voor accountability + forward-compat GDPR right-to-be-forgotten.
    """
    try:
        memory_id = int(args.get("memory_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "memory_id (int) is required"}

    conn = open_with_vec(Path(db_path))
    try:
        mem = get_memory(conn, memory_id)
        if mem is None:
            return {"ok": False, "error": f"memory {memory_id} not found"}
        delete_memory(conn, memory_id)
    finally:
        conn.close()

    preview = mem["text"][:120]
    # H2 — admin-action audit (ISO 27001 A.12.4.3). No-op als de logger
    # niet gebound is (tests/scripts).
    try:
        from core.audit import log_admin_action
        log_admin_action(
            action="forget_memory",
            actor=actor or "unknown",
            from_value={"memory_id": memory_id, "preview": preview,
                        "tags": mem.get("tags", []),
                        "source": mem.get("source", "unknown"),
                        "created_at": mem.get("created_at")},
            to_value=None,
            reason=(args.get("reason") or "user-requested")[:200],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("forget_memory audit failed: %s", e)

    return {
        "ok": True,
        "memory_id": memory_id,
        "deleted_text_preview": preview,
    }


# --- registratie ---------------------------------------------------------

MEMORY_HANDLERS = {
    "add_memory": add_memory_handler,
    "recall": recall_handler,
    "list_memories": list_memories_handler,
    "forget_memory": forget_memory_handler,
}

MEMORY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "add_memory",
        "description": (
            "Sla iets op in Rosa's lange-termijn-geheugen. Gebruik wanneer the user "
            "expliciet zegt 'onthoud X', 'remember X', 'remember this:', 'leg vast', "
            "'voeg toe aan mijn geheugen'. Niet voor todos of reminders (die "
            "hebben eigen tools). Niet voor preferences ('voortaan X' → "
            "add_config_wish). NIET hetzelfde als vendor_strategy_remember — "
            "die is specifiek voor receipt-collector (waar je bonnen vindt). "
            "add_memory is voor algemene feiten over the user's wereld "
            "('onze SLA is 99.5%', 'Anne werkt bij Heineken'). Memories worden "
            "semantisch geïndexeerd en later automatisch teruggehaald. Tags "
            "zijn vrije strings ('contract','pricing','heineken'); links is een "
            "lijst entity-keys voor toekomstige entity-graph-koppeling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEXT_LEN,
                    "description": "De feitelijke inhoud van de memory.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 40},
                    "maxItems": 8,
                    "description": "Optionele tags voor categorisatie/retrieval.",
                },
                "links": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    "maxItems": 8,
                    "description": "Optionele entity-keys (forward-compat).",
                },
                "source": {
                    "type": "string",
                    "enum": ["chat", "auto", "briefing"],
                    "description": "Waar deze memory vandaan komt. Default 'chat'.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recall",
        "description": (
            "Semantic-search over Rosa's geheugen. Gebruik wanneer the user vraagt "
            "naar iets dat hij eerder verteld heeft: 'wat weet je over X', "
            "'herinner je nog dat ik zei...', 'wat hebben we afgesproken over Y'. "
            "Returnt top-k meest relevante memory-cards met distance-score "
            "(lager = beter). Bij geen match: lege results-list, niet verzinnen. "
            "Tag-filter is POST-vector — als je hard op tag wilt filteren is "
            "list_memories(tags=…) accurater. Als response `degraded: true` "
            "bevat, kon de embedding niet berekend worden — vermeld dat eerlijk "
            "aan the user ('ik kon je geheugen even niet doorzoeken')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": MIN_QUERY_LEN,
                    "maxLength": 500,
                    "description": "Vraag of trefwoord om op te zoeken.",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RECALL_K,
                    "description": f"Aantal results (default 5, max {MAX_RECALL_K}).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optioneel tag-filter (post-vector). Match elke tag.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_memories",
        "description": (
            "Lijst memory-cards chronologisch, meest recente eerst. Gebruik wanneer "
            "the user vraagt 'wat heb je allemaal over X opgeslagen', 'toon mijn "
            "memories', 'welke memories hebben tag pricing'. Niet als hij iets "
            "specifieks zoekt — gebruik dan recall."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optioneel tag-filter (OR-match).",
                },
                "since": {
                    "type": "string",
                    "description": "ISO-timestamp; alleen memories vanaf dan.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                    "description": f"Max aantal (default 20, max {MAX_LIST_LIMIT}).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "forget_memory",
        "description": (
            "Verwijder een memory-card by id. Gebruik wanneer the user zegt "
            "'vergeet wat ik laatst zei over X', 'verwijder die memory', of "
            "'klopt niet, haal hem weg'. Roep eerst recall/list_memories aan om "
            "de juiste memory_id te identificeren, behalve als the user de id "
            "expliciet noemt. Bij twijfel: confirm eerst aan the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "ID van de te verwijderen memory.",
                },
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
    },
]
