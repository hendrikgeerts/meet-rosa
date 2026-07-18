import sqlite3
from contextlib import contextmanager
from pathlib import Path

from core.perms import secure_dir, secure_file

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    guid TEXT PRIMARY KEY,
    rowid INTEGER NOT NULL,
    handle TEXT NOT NULL,
    text TEXT,
    received_at INTEGER NOT NULL,
    processed_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_processed_rowid ON processed_messages(rowid);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_turns_handle_created ON conversation_turns(handle, created_at);

CREATE TABLE IF NOT EXISTS outgoing_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    body TEXT NOT NULL,
    queued_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    sent_at INTEGER,
    error TEXT
);
"""


def init_db(path: Path) -> None:
    secure_dir(path.parent)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    # Sluit memory.db + WAL/SHM af voor andere user-processen.
    secure_file(path)
    secure_file(path.with_suffix(path.suffix + "-wal"))
    secure_file(path.with_suffix(path.suffix + "-shm"))


@contextmanager
def connect(path: Path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Audit DB-1 (28/6): FK-constraints aan voor data-integriteit.
    # SQLite default is OFF; zonder dit zijn FOREIGN KEY-clauses in
    # andere schemas decoratief. A.18.1.3.
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def max_processed_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS mx FROM processed_messages").fetchone()
    return int(row["mx"])


def mark_processed(conn: sqlite3.Connection, *, guid: str, rowid: int, handle: str, text: str, received_at: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (guid, rowid, handle, text, received_at) VALUES (?, ?, ?, ?, ?)",
        (guid, rowid, handle, text, received_at),
    )


def append_turn(conn: sqlite3.Connection, *, handle: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO conversation_turns (handle, role, content) VALUES (?, ?, ?)",
        (handle, role, content),
    )


def recent_turns(conn: sqlite3.Connection, handle: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM conversation_turns WHERE handle = ? ORDER BY id DESC LIMIT ?",
        (handle, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def prune_conversation_history(
    conn: sqlite3.Connection, *,
    turns_days: int, processed_days: int,
) -> tuple[int, int]:
    """Audit DB-2 (28/6) — verwijder oude conversation_turns +
    processed_messages volgens GDPR-opslagbeperking (art 5(1)(e)).
    Beide tabellen bevatten echte iMessage-bodies + replies met namen;
    onbegrensde groei is een ISO/GDPR-vinding.

    Returns (turns_removed, processed_removed)."""
    now_row = conn.execute("SELECT strftime('%s','now')").fetchone()
    now_ts = int(now_row[0])

    turns_cutoff = now_ts - turns_days * 86400
    processed_cutoff = now_ts - processed_days * 86400

    turns_removed = 0
    if turns_days > 0:
        cur = conn.execute(
            "DELETE FROM conversation_turns WHERE created_at < ?",
            (turns_cutoff,),
        )
        turns_removed = cur.rowcount or 0

    processed_removed = 0
    if processed_days > 0:
        cur = conn.execute(
            "DELETE FROM processed_messages WHERE received_at < ?",
            (processed_cutoff,),
        )
        processed_removed = cur.rowcount or 0

    return turns_removed, processed_removed
