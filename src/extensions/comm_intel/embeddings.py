"""Embedding-laag voor comm_items: lokaal `nomic-embed-text` via Ollama
+ `sqlite-vec` virtual table voor similarity-search.

Bedoeld voor RAG-stijl vragen over multi-jaars contact-historie:
"wat speelt er rond klant X?" → embed query → top-K relevante items
→ Claude beantwoordt op basis van die K items.

Gebruikt vec0 virtual table met rowid = comm_items.id, zodat een JOIN
tegen de oorspronkelijke tabel triviaal is.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

EMBED_DIM = 768  # nomic-embed-text v1.5 default
EMBED_MODEL = "nomic-embed-text"
EMBED_TIMEOUT = 60.0


# vec0 virtual table — rowid maps 1:1 naar comm_items.id
_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS comm_embeddings
USING vec0(emb float[{EMBED_DIM}])
"""


def init_embeddings_schema(db_path: Path) -> None:
    """Lazy schema-init. Vereist sqlite_vec.load() — dat doen we via
    `_open_with_vec`, niet hier (om schema-init in main.py simpel te houden)."""
    with _open_with_vec(db_path) as conn:
        conn.executescript(_SCHEMA)


def _open_with_vec(db_path: Path) -> sqlite3.Connection:
    """Connect + load sqlite-vec extension. Caller closes."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def embed(text: str, *, model: str = EMBED_MODEL,
            host: str = "http://localhost:11434",
            kind: str = "document") -> list[float] | None:
    """Genereer een embedding via Ollama. Returns None bij fout.

    `kind` = "document" (default) of "query" — nomic-embed-text v1.5 is
    getraind met task-prefixes en presteert significant slechter zonder.
    """
    if not text or not text.strip():
        return None
    prefix = "search_query: " if kind == "query" else "search_document: "
    payload = json.dumps({
        "model": model,
        "prompt": prefix + text[:8000],
        # Hou nomic-embed-text in memory — anders elke call 30-60s
        # model-load op CPU. -1 = forever (klein model, 137M params).
        "keep_alive": -1,
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/embeddings", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("embed() failed: %s", e)
        return None
    vec = data.get("embedding") or []
    if len(vec) != EMBED_DIM:
        log.warning("embed() unexpected dim %d (want %d)", len(vec), EMBED_DIM)
        return None
    return vec


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{EMBED_DIM}f", *vec)


def upsert_embedding(
    conn: sqlite3.Connection, item_id: int, vec: list[float],
) -> None:
    """Insert or replace embedding voor een comm_items rij.

    vec0 ondersteunt geen ON CONFLICT — dus delete + insert."""
    blob = _pack(vec)
    conn.execute("DELETE FROM comm_embeddings WHERE rowid=?", (item_id,))
    conn.execute(
        "INSERT INTO comm_embeddings(rowid, emb) VALUES (?, ?)",
        (item_id, blob),
    )


def has_embedding(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comm_embeddings WHERE rowid=? LIMIT 1",
        (item_id,),
    ).fetchone()
    return row is not None


def search(
    conn: sqlite3.Connection, query: str, *,
    k: int = 10, since_unix: int | None = None,
    source: str | None = None,
) -> list[dict]:
    """Embed query, vind top-K via vec0, JOIN tegen comm_items voor metadata.

    Filtering op datum/source gebeurt POST-vector-search omdat sqlite-vec
    in 0.1.9 nog geen pre-filter constraint accepteert; we vragen K*5
    op om voldoende kandidaten te hebben na filter."""
    qvec = embed(query, kind="query")
    if qvec is None:
        return []
    blob = _pack(qvec)

    # Over-fetch om filter-shrinkage op te vangen; 5x is empirisch voldoende
    # bij smalle filters (per vendor/persoon). Bij brede filters (geen
    # filter) gebruiken we precies k.
    fetch_k = k if (since_unix is None and source is None) else k * 5

    rows = conn.execute(
        """SELECT e.rowid, e.distance, c.source, c.account, c.direction,
                    c.from_addr, c.subject, c.occurred_at, c.summary,
                    c.intent, c.body_full, c.thread_ref
              FROM comm_embeddings e
              JOIN comm_items c ON c.id = e.rowid
              WHERE e.emb MATCH ? AND k = ?
              ORDER BY e.distance""",
        (blob, fetch_k),
    ).fetchall()

    out = []
    for r in rows:
        if since_unix is not None and (r[7] or 0) < since_unix:
            continue
        if source is not None and r[2] != source:
            continue
        body = r[10] or ""
        out.append({
            "id": r[0],
            "distance": float(r[1]),
            "source": r[2],
            "account": r[3],
            "direction": r[4],
            "from": r[5],
            "subject": r[6],
            "occurred_at": r[7],
            "summary": r[8],
            "intent": r[9],
            "body_excerpt": body[:600],
            "thread_ref": r[11],
        })
        if len(out) >= k:
            break
    return out
