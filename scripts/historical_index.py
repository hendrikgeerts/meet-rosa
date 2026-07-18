"""One-shot embedding-index van alle bestaande comm_items.

Loopt alle items zonder embedding en stopt ze in `comm_embeddings` zodat
Rosa via `comm_semantic_search` retrieval-augmented kan antwoorden over
multi-jaars contact-historie.

Idempotent: skipt items die al een embedding hebben. Veilig om te
herstarten of meerdere keren te draaien.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/historical_index.py
    PYTHONPATH=src ./venv/bin/python scripts/historical_index.py --limit 1000 --since-days 365
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.comm_intel.embeddings import (
    _open_with_vec, embed, has_embedding, init_embeddings_schema,
    upsert_embedding,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("index")


def _build_embed_text(subject: str | None, body: str | None) -> str:
    """Combineer subject + body voor één embedding. Subject krijgt voorrang
    omdat het vaak de essentie van de mail vat. Body wordt gecapt op
    ~4000 chars om Ollama-latency in toom te houden."""
    s = (subject or "").strip()
    b = (body or "").strip()
    if not s and not b:
        return ""
    if not s:
        return b[:4000]
    if not b:
        return s
    return f"{s}\n\n{b[:4000]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Max items om te indexeren (0 = alles)")
    ap.add_argument("--since-days", type=int, default=0,
                    help="Alleen items uit laatste N dagen (0 = alles)")
    ap.add_argument("--reembed", action="store_true",
                    help="Forceer re-embed (default: skip items met bestaande embedding)")
    args = ap.parse_args()

    settings = load_settings()
    init_embeddings_schema(settings.db_path)

    with _open_with_vec(settings.db_path) as conn:
        # Pak alle item-ids in batches
        where = []
        params = []
        if args.since_days > 0:
            cutoff = int(time.time()) - args.since_days * 86400
            where.append("occurred_at >= ?")
            params.append(cutoff)
        wsql = f" WHERE {' AND '.join(where)}" if where else ""
        lsql = f" LIMIT {args.limit}" if args.limit > 0 else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM comm_items{wsql}", params,
        ).fetchone()[0]
        log.info("comm_items in scope: %d", total)

        rows = conn.execute(
            f"SELECT id, subject, body_full FROM comm_items{wsql} "
            f"ORDER BY id ASC{lsql}", params,
        ).fetchall()

        seen = 0
        embedded = 0
        skipped_existing = 0
        skipped_empty = 0
        errors = 0
        t0 = time.time()
        for item_id, subject, body in rows:
            seen += 1
            if not args.reembed and has_embedding(conn, item_id):
                skipped_existing += 1
            else:
                txt = _build_embed_text(subject, body)
                if not txt:
                    skipped_empty += 1
                else:
                    vec = embed(txt, kind="document")
                    if vec is None:
                        errors += 1
                    else:
                        upsert_embedding(conn, item_id, vec)
                        embedded += 1
            if seen % 100 == 0:
                rate = seen / max(1, time.time() - t0)
                eta_s = (total - seen) / rate if rate > 0 else 0
                log.info("progress %d/%d (embedded=%d skip_existing=%d "
                          "skip_empty=%d err=%d) %.1f items/s eta %.0fs",
                          seen, total, embedded, skipped_existing,
                          skipped_empty, errors, rate, eta_s)

        log.info("DONE seen=%d embedded=%d skip_existing=%d skip_empty=%d err=%d "
                  "elapsed=%.0fs",
                  seen, embedded, skipped_existing, skipped_empty, errors,
                  time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
