"""Response-time analytics: per `from_addr` meet hoe snel the user
historisch reageert, en flag threads die langer dan zijn gemiddelde
open staan.

Algoritme:
1. Per `thread_ref` met ≥1 inkomende en ≥1 uitgaande bericht:
   - Pak eerste 'in' bericht.
   - Pak eerste 'out' bericht NA die 'in'.
   - delta_seconds = out_ts - in_ts (positief, niet null).
2. Aggregatie per `from_addr` van het inkomend bericht:
   - count, mean_seconds, p50_seconds.
3. Overdue-detectie: thread waar laatste bericht 'in' is en
   ouder dan p50_seconds * factor (default 1.5).

Geen aparte tabel — alles on-the-fly query op comm_items. Voor
the user's volume (~50-200 mails/dag) is dat ruim genoeg snel.
"""
from __future__ import annotations

import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Minimum reageer-tijden waaronder we 'em niet meetellen (auto-bounces).
_MIN_RESPONSE_SECONDS = 30


def collect_per_sender_stats(
    db_path: Path, *,
    days: int = 90,
    min_threads: int = 2,
) -> list[dict[str, Any]]:
    """Per from_addr (alleen incoming): gemiddelde + median antwoord-tijd
    over de laatste `days` dagen.

    Vereist `min_threads` ≥ N threads voor statistische significantie.
    """
    cutoff = int(_time.time()) - days * 86400
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Voor elke thread: vind eerste 'in' en eerste 'out' DAARNA
            rows = conn.execute(
                """
                WITH first_in AS (
                  SELECT thread_ref, MIN(occurred_at) AS in_ts, from_addr
                  FROM comm_items
                  WHERE direction='in' AND thread_ref IS NOT NULL
                    AND occurred_at >= ?
                  GROUP BY thread_ref
                ),
                first_out AS (
                  SELECT ci.thread_ref, MIN(ci.occurred_at) AS out_ts
                  FROM comm_items ci
                  JOIN first_in fi ON ci.thread_ref = fi.thread_ref
                  WHERE ci.direction='out' AND ci.occurred_at > fi.in_ts
                  GROUP BY ci.thread_ref
                )
                SELECT fi.from_addr, fo.out_ts - fi.in_ts AS delta_s
                FROM first_in fi
                JOIN first_out fo ON fi.thread_ref = fo.thread_ref
                WHERE fo.out_ts - fi.in_ts >= ?
                """,
                (cutoff, _MIN_RESPONSE_SECONDS),
            ).fetchall()
            # Aggregatie per from_addr
            buckets: dict[str, list[int]] = {}
            for r in rows:
                addr = (r["from_addr"] or "").strip()
                if not addr:
                    continue
                buckets.setdefault(addr, []).append(int(r["delta_s"]))
            for addr, deltas in buckets.items():
                if len(deltas) < min_threads:
                    continue
                deltas.sort()
                mid = deltas[len(deltas) // 2]
                mean = int(sum(deltas) / len(deltas))
                out.append({
                    "from_addr": addr,
                    "thread_count": len(deltas),
                    "mean_seconds": mean,
                    "median_seconds": mid,
                    "mean_hours": round(mean / 3600.0, 1),
                    "median_hours": round(mid / 3600.0, 1),
                })
            out.sort(key=lambda r: r["thread_count"], reverse=True)
    except sqlite3.OperationalError as e:
        log.warning("response_time: db error %s", e)
    return out


def find_overdue_threads(
    db_path: Path, *,
    factor: float = 1.5,
    min_age_hours: float = 24.0,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Vind threads waarvan het laatste bericht INKOMEND is, EN ouder
    dan `max(median_response_time * factor, min_age_hours)` voor die
    from_addr. Dat zijn de threads waar the user traag-zelfs-voor-zijn-
    eigen-baseline op zit.

    Voor afzenders zonder genoeg geschiedenis (< min_threads) valt
    'em terug op de globale min_age_hours.
    """
    stats_list = collect_per_sender_stats(db_path)
    median_per_addr = {
        s["from_addr"]: s["median_seconds"] for s in stats_list
    }
    now_ts = int(_time.time())
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Threads waar laatste bericht 'in' is — same trick als
            # comm_unanswered maar met from_addr + occurred_at terug.
            rows = conn.execute(
                """
                WITH latest AS (
                  SELECT thread_ref, MAX(occurred_at) AS latest_at
                  FROM comm_items
                  WHERE thread_ref IS NOT NULL
                  GROUP BY thread_ref
                )
                SELECT ci.thread_ref, ci.from_addr, ci.subject,
                       ci.occurred_at, ci.source
                FROM comm_items ci
                JOIN latest l
                  ON ci.thread_ref = l.thread_ref
                 AND ci.occurred_at = l.latest_at
                WHERE ci.direction='in'
                  AND (ci.intent IS NULL OR ci.intent NOT IN ('newsletter','social'))
                ORDER BY ci.occurred_at ASC
                LIMIT 200
                """,
            ).fetchall()
            for r in rows:
                addr = (r["from_addr"] or "").strip()
                age_seconds = now_ts - int(r["occurred_at"])
                age_hours = age_seconds / 3600.0
                median_s = median_per_addr.get(addr)
                if median_s is not None:
                    threshold_seconds = max(
                        median_s * factor, min_age_hours * 3600,
                    )
                else:
                    threshold_seconds = min_age_hours * 3600
                if age_seconds < threshold_seconds:
                    continue
                out.append({
                    "thread_ref": r["thread_ref"],
                    "from_addr": addr,
                    "subject": (r["subject"] or "")[:120],
                    "age_hours": round(age_hours, 1),
                    "median_hours": round(median_s / 3600.0, 1) if median_s else None,
                    "source": r["source"],
                })
            out.sort(key=lambda x: x["age_hours"], reverse=True)
    except sqlite3.OperationalError as e:
        log.warning("response_time: overdue query failed: %s", e)
    return out[:limit]
