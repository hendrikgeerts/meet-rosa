"""Tests voor memory-cards extensie.

Embedding-call wordt gemockt — we hoeven Ollama niet draaiend te
hebben tijdens unit-tests. Vec-tabel wordt wel echt geïnit zodat we
zien dat insert + delete via vec0 niet exploderen.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# Memory-schema needs `Connection.enable_load_extension` for sqlite-vec.
# Python built without --enable-loadable-sqlite-extensions (default
# python.org installer, GitHub-Actions macos-latest) lacks it. `install.sh`
# uses Homebrew's python@3.12 where it IS on, so end-users aren't affected.
_conn = sqlite3.connect(":memory:")
if not hasattr(_conn, "enable_load_extension"):
    _conn.close()
    pytest.skip(
        "sqlite3 built without --enable-loadable-sqlite-extensions",
        allow_module_level=True,
    )
_conn.close()

from extensions.memory.schema import (
    count_memories,
    delete_memory,
    get_memory,
    init_memory_schema,
    insert_memory,
    list_memories,
    open_with_vec,
)
from extensions.memory.tools import (
    add_memory_handler,
    forget_memory_handler,
    list_memories_handler,
    recall_handler,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "memory.db"
    init_memory_schema(p)
    return p


# --- schema --------------------------------------------------------------

def test_init_creates_tables(db: Path) -> None:
    with open_with_vec(db) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual') "
            "AND name IN ('memories','memory_embeddings')"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "memories" in names
    # vec0 virtual tables registreren als 'table' OR underlying shadow tables
    with sqlite3.connect(db) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'memory_embeddings%'"
        ).fetchall()
    assert any("memory_embeddings" in r[0] for r in rows)


def test_insert_and_get(db: Path) -> None:
    with open_with_vec(db) as c:
        mid = insert_memory(
            c, text="onze SLA is 99.5%",
            tags=["contract", "sla"],
            source="chat",
        )
        mem = get_memory(c, mid)
    assert mem is not None
    assert mem["text"] == "onze SLA is 99.5%"
    assert mem["tags"] == ["contract", "sla"]
    assert mem["source"] == "chat"
    assert mem["confidence"] == 1.0
    assert mem["linked_entities"] == []


def test_list_orders_recent_first(db: Path) -> None:
    with open_with_vec(db) as c:
        a = insert_memory(c, text="oude", tags=["t1"])
        b = insert_memory(c, text="middel", tags=["t2"])
        d = insert_memory(c, text="nieuw", tags=["t1", "t3"])
        rows = list_memories(c, limit=10)
    assert [r["id"] for r in rows] == [d, b, a]


def test_list_tag_filter(db: Path) -> None:
    with open_with_vec(db) as c:
        insert_memory(c, text="a", tags=["alpha"])
        b = insert_memory(c, text="b", tags=["beta"])
        ab = insert_memory(c, text="ab", tags=["alpha", "beta"])
        rows = list_memories(c, tags=["beta"], limit=10)
    ids = {r["id"] for r in rows}
    assert ids == {b, ab}


def test_delete_removes(db: Path) -> None:
    with open_with_vec(db) as c:
        mid = insert_memory(c, text="ephemeral")
        assert get_memory(c, mid) is not None
        assert delete_memory(c, mid) is True
        assert get_memory(c, mid) is None


def test_count(db: Path) -> None:
    with open_with_vec(db) as c:
        assert count_memories(c) == 0
        insert_memory(c, text="a")
        insert_memory(c, text="b")
        assert count_memories(c) == 2


# --- tools ---------------------------------------------------------------

# 768-dim deterministic stub-vector. Hangt af van de tekst zodat
# semantically-similar text en semantically-unrelated text different scores krijgen.
def _stub_embed(text: str, *, kind: str = "document"):
    # eenvoudige hashing → 768 floats. Niet echt semantisch maar
    # deterministic en consistent kind van afstand.
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # repeat to 768 floats
    vals = [(b - 128) / 128.0 for b in h]
    while len(vals) < 768:
        vals.extend(vals[: 768 - len(vals)])
    return vals[:768]


def test_remember_stores_with_embedding(db: Path) -> None:
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        out = add_memory_handler(db, {
            "text": "Anne werkt bij Heineken als procurement",
            "tags": ["people", "heineken"],
        })
    assert out["ok"] is True
    assert out["embedded"] is True
    assert out["memory_id"] > 0


def test_remember_rejects_empty(db: Path) -> None:
    out = add_memory_handler(db, {"text": "   "})
    assert out["ok"] is False
    assert "required" in out["error"]


def test_remember_rejects_too_long(db: Path) -> None:
    out = add_memory_handler(db, {"text": "x" * 9999})
    assert out["ok"] is False
    assert "exceeds" in out["error"]


def test_remember_stores_even_when_embed_fails(db: Path) -> None:
    """Bij Ollama-down moet de memory toch opgeslagen worden (text-only)."""
    with patch("extensions.memory.tools.embed", return_value=None):
        out = add_memory_handler(db, {"text": "fallback text"})
    assert out["ok"] is True
    assert out["embedded"] is False
    assert out["memory_id"] > 0
    # Verify het in de DB staat
    with open_with_vec(db) as c:
        assert count_memories(c) == 1


def test_recall_returns_results(db: Path) -> None:
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed), \
         patch("extensions.memory.embeddings.embed", side_effect=_stub_embed):
        add_memory_handler(db, {"text": "Anne werkt bij Heineken als procurement"})
        add_memory_handler(db, {"text": "Marc Janssen is CFO bij ASR"})
        add_memory_handler(db, {"text": "onze SLA-clausule is 99.5%"})

        out = recall_handler(db, {"query": "Heineken procurement", "k": 2})
    assert out["ok"] is True
    assert len(out["results"]) <= 2
    # zonder echte embedding-semantiek garanderen we niet welke top is, maar
    # iets moet terugkomen
    assert out["count"] >= 1


def test_recall_query_too_short(db: Path) -> None:
    out = recall_handler(db, {"query": "x"})
    assert out["ok"] is False


def test_recall_empty_db(db: Path) -> None:
    with patch("extensions.memory.embeddings.embed", side_effect=_stub_embed):
        out = recall_handler(db, {"query": "anything"})
    assert out["ok"] is True
    assert out["results"] == []
    assert out["count"] == 0


def test_list_memories_handler(db: Path) -> None:
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        add_memory_handler(db, {"text": "feit 1", "tags": ["a"]})
        add_memory_handler(db, {"text": "feit 2", "tags": ["b"]})
    out = list_memories_handler(db, {"limit": 10})
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["shown"] == 2
    out_b = list_memories_handler(db, {"tags": ["b"]})
    assert out_b["shown"] == 1
    assert out_b["memories"][0]["text"] == "feit 2"


def test_forget_memory_handler(db: Path) -> None:
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        ins = add_memory_handler(db, {"text": "tijdelijk"})
        mid = ins["memory_id"]
        out = forget_memory_handler(db, {"memory_id": mid})
    assert out["ok"] is True
    assert out["memory_id"] == mid
    # En weg
    with open_with_vec(db) as c:
        assert get_memory(c, mid) is None


def test_forget_memory_missing(db: Path) -> None:
    out = forget_memory_handler(db, {"memory_id": 9999})
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_forget_memory_invalid_id(db: Path) -> None:
    out = forget_memory_handler(db, {})
    assert out["ok"] is False
    assert "required" in out["error"]


def test_max_caps(db: Path) -> None:
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed), \
         patch("extensions.memory.embeddings.embed", side_effect=_stub_embed):
        for i in range(15):
            add_memory_handler(db, {"text": f"memory {i}"})
        # k cap = 10
        out = recall_handler(db, {"query": "memory", "k": 999})
    assert len(out["results"]) <= 10


# --- review-ronde: H1/H2/M1/M3/L5 ---------------------------------------

def test_tool_name_is_add_memory_not_remember(db: Path) -> None:
    """H1 — tool moet geregistreerd staan als `add_memory` om collisie met
    `vendor_strategy_remember` te voorkomen."""
    from extensions.memory.tools import MEMORY_HANDLERS, MEMORY_TOOL_SCHEMAS
    names = {s["name"] for s in MEMORY_TOOL_SCHEMAS}
    assert "add_memory" in names
    assert "remember" not in names
    assert "add_memory" in MEMORY_HANDLERS
    assert "remember" not in MEMORY_HANDLERS


def test_recall_signals_degraded_when_embed_fails(db: Path) -> None:
    """M1 — recall moet onderscheidbaar zijn van 'echt niets gevonden' wanneer
    embedding-service down is."""
    with patch("extensions.memory.tools.embed", return_value=None):
        out = recall_handler(db, {"query": "anything semantic"})
    assert out["ok"] is True
    assert out["degraded"] is True
    assert "error" in out
    assert out["results"] == []


def test_recall_normal_path_has_no_degraded_flag(db: Path) -> None:
    """M1 — normale recall (zelfs leeg) zou NIET degraded:true moeten geven."""
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed), \
         patch("extensions.memory.embeddings.embed", side_effect=_stub_embed):
        out = recall_handler(db, {"query": "iets specifiek"})
    assert out["ok"] is True
    assert "degraded" not in out


def test_source_invalid_falls_back_to_chat(db: Path) -> None:
    """M3 — direct callers (tests, scripts) die een onverwachte source
    meegeven krijgen 'chat' terug, niet de rauwe waarde."""
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        out = add_memory_handler(db, {"text": "x", "source": "malicious"})
    assert out["ok"] is True
    with open_with_vec(db) as c:
        mem = get_memory(c, out["memory_id"])
    assert mem["source"] == "chat"


def test_source_valid_values_pass_through(db: Path) -> None:
    """M3 — geldige enum-values blijven intact."""
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        for src in ("chat", "auto", "briefing"):
            out = add_memory_handler(db, {"text": f"t-{src}", "source": src})
            with open_with_vec(db) as c:
                mem = get_memory(c, out["memory_id"])
            assert mem["source"] == src


def test_forget_memory_writes_admin_audit(db: Path, tmp_path: Path) -> None:
    """H2 — forget_memory moet naar admin-audit-stream schrijven."""
    from core.audit import AdminActionLogger, bind_admin_logger

    audit_dir = tmp_path / "audit"
    logger = AdminActionLogger(audit_dir)
    bind_admin_logger(logger)
    try:
        with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
            ins = add_memory_handler(db, {
                "text": "gevoelige info over project Q3",
                "tags": ["projects"],
            })
            mid = ins["memory_id"]
            forget_memory_handler(db, {"memory_id": mid, "reason": "klopt niet"},
                                   actor="imessage")

        # Check audit-file
        audit_files = list(audit_dir.glob("admin-*.jsonl"))
        assert len(audit_files) == 1
        lines = audit_files[0].read_text().strip().splitlines()
        assert any('"action": "forget_memory"' in line for line in lines)
        entry = next(json.loads(line) for line in lines
                     if '"forget_memory"' in line)
        assert entry["action"] == "forget_memory"
        assert entry["actor"] == "imessage"
        assert entry["reason"] == "klopt niet"
        assert entry["from"]["memory_id"] == mid
        assert "gevoelige info" in entry["from"]["preview"]
    finally:
        # Restore singleton state
        import core.audit
        core.audit._admin = None


def test_forget_memory_no_audit_when_unbound(db: Path) -> None:
    """H2 — als de logger niet gebound is (tests/scripts), moet
    forget_memory gewoon werken zonder error."""
    import core.audit
    assert core.audit._admin is None  # baseline
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        ins = add_memory_handler(db, {"text": "t"})
        out = forget_memory_handler(db, {"memory_id": ins["memory_id"]})
    assert out["ok"] is True


def test_linked_entities_persist(db: Path) -> None:
    """L5 — `links` (forward-compat met entity-graph) moet correct
    opgeslagen worden en uit get_memory komen."""
    with patch("extensions.memory.tools.embed", side_effect=_stub_embed):
        out = add_memory_handler(db, {
            "text": "Anne werkt bij Heineken",
            "links": ["person:anne", "company:heineken"],
        })
    with open_with_vec(db) as c:
        mem = get_memory(c, out["memory_id"])
    assert mem["linked_entities"] == ["person:anne", "company:heineken"]
    # En in de tool-response
    assert out["linked_entities"] == ["person:anne", "company:heineken"]
