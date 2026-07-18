"""Embedding-helpers voor memory cards.

Deelt het Ollama-call-pattern uit comm_intel.embeddings, maar slaat op
in `memory_embeddings` (eigen vec0-tabel) zodat memory-cards en
comm-items losse similarity-spaces hebben.
"""
from __future__ import annotations

import logging
import sqlite3
import struct

from extensions.comm_intel.embeddings import EMBED_DIM, embed

log = logging.getLogger(__name__)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{EMBED_DIM}f", *vec)


def upsert_embedding(
    conn: sqlite3.Connection, memory_id: int, vec: list[float],
) -> None:
    """Insert-or-replace embedding voor een memories rij.

    vec0 ondersteunt geen ON CONFLICT — delete + insert."""
    blob = _pack(vec)
    conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (memory_id,))
    conn.execute(
        "INSERT INTO memory_embeddings(rowid, emb) VALUES (?, ?)",
        (memory_id, blob),
    )


def semantic_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    k: int = 5,
) -> list[tuple[int, float]]:
    """Top-K rowids by similarity tot query. Returns [(memory_id, distance), …].
    Lege list bij embedding-fail of geen embeddings opgeslagen.

    Voor callers die zelf willen weten of de embed-stap faalde (vs echt
    geen match): gebruik `search_by_vec` met een al-gegenereerde qvec.
    """
    qvec = embed(query, kind="query")
    if qvec is None:
        return []
    return search_by_vec(conn, qvec=qvec, k=k)


def search_by_vec(
    conn: sqlite3.Connection,
    *,
    qvec: list[float],
    k: int = 5,
) -> list[tuple[int, float]]:
    """Top-K rowids tegen een al-berekende query-vector."""
    blob = _pack(qvec)
    try:
        rows = conn.execute(
            """SELECT rowid, distance
               FROM memory_embeddings
               WHERE emb MATCH ?
               ORDER BY distance
               LIMIT ?""",
            (blob, int(k)),
        ).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("memory search_by_vec failed: %s", e)
        return []
    return [(int(r[0]), float(r[1])) for r in rows]
