"""Schema voor memory cards: vrije-tekst herinneringen die the user
declaratief aan Rosa kan leren via iMessage.

Twee tabellen:
- memories          regulier — tekst + metadata
- memory_embeddings vec0 virtual table — rowid = memories.id

Embedding-laag deelt de Ollama-helper uit comm_intel.embeddings (zelfde
model nomic-embed-text, zelfde 768-dim). Dat houdt embedding-model-keuze
op één plek.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from extensions.comm_intel.embeddings import EMBED_DIM


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memories (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  text            TEXT NOT NULL,
  tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
  source          TEXT NOT NULL DEFAULT 'chat', -- 'chat' | 'auto' | 'briefing' | …
  linked_entities TEXT NOT NULL DEFAULT '[]',   -- JSON array (forward-compat met entity-graph)
  confidence      REAL NOT NULL DEFAULT 1.0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_source  ON memories(source);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings
USING vec0(emb float[{EMBED_DIM}]);
"""


def init_memory_schema(db_path: Path) -> None:
    """Maakt memories-tabel + vec0 virtual table aan (idempotent)."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def open_with_vec(db_path: Path) -> sqlite3.Connection:
    """Open een connectie met sqlite_vec extensie geladen. Caller closes."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


# --- low-level CRUD ------------------------------------------------------

def insert_memory(
    conn: sqlite3.Connection,
    *,
    text: str,
    tags: list[str] | None = None,
    source: str = "chat",
    linked_entities: list[str] | None = None,
    confidence: float = 1.0,
) -> int:
    """Voeg memory toe, return memory_id. Geen embedding hier — dat doet
    de tool-laag zodat insert + embed atomisch in één try/except kan."""
    cur = conn.execute(
        """INSERT INTO memories (text, tags, source, linked_entities, confidence)
           VALUES (?, ?, ?, ?, ?)""",
        (text.strip(),
         json.dumps(tags or []),
         source,
         json.dumps(linked_entities or []),
         confidence),
    )
    return int(cur.lastrowid or 0)


def get_memory(conn: sqlite3.Connection, memory_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["linked_entities"] = json.loads(d.get("linked_entities") or "[]")
    return d


def list_memories(
    conn: sqlite3.Connection,
    *,
    tags: list[str] | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Lijst memories — meest recente eerst. `tags` is een OR-filter
    (match elke meegegeven tag); `since` is een ISO-timestamp."""
    sql = "SELECT * FROM memories"
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if tags:
        # JSON1 search: any of the tags moet voorkomen in de json-array
        tag_clauses = []
        for t in tags:
            tag_clauses.append(
                "EXISTS (SELECT 1 FROM json_each(memories.tags) WHERE json_each.value = ?)"
            )
            params.append(t)
        where.append("(" + " OR ".join(tag_clauses) + ")")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["linked_entities"] = json.loads(d.get("linked_entities") or "[]")
        out.append(d)
    return out


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """Hard-delete memory + embedding. Return True als rij bestond."""
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    deleted = (cur.rowcount or 0) > 0
    # embedding cleanup — vec0 staat geen ON DELETE CASCADE toe
    try:
        conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (memory_id,))
    except sqlite3.OperationalError:
        pass  # vec0 niet geladen (test-DB zonder extensie)
    return deleted


def count_memories(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    return int(row[0]) if row else 0
