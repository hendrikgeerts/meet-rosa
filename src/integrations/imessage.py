"""Read incoming iMessages from chat.db and send replies via Messages.app."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01


@dataclass(frozen=True)
class IncomingMessage:
    guid: str
    rowid: int
    handle: str
    text: str
    received_at: int  # unix seconds


def _apple_nanos_to_unix(nanos: int) -> int:
    if nanos > 1_000_000_000_000:
        return int(nanos / 1_000_000_000) + APPLE_EPOCH_OFFSET
    return int(nanos) + APPLE_EPOCH_OFFSET


def _extract_text_from_attributed_body(blob: bytes | None) -> str | None:
    """Pragmatic extractor for Ventura+ attributedBody typedstream blobs.

    The NSKeyedArchiver format isn't trivially parsed without pyobjc or a dedicated
    typedstream library, so we locate the NSString marker and pull the longest
    printable UTF-8 run that follows. Works for plain-text messages; falls back to
    None for edge cases (rich attachments, reactions, etc.).
    """
    if not blob:
        return None
    idx = blob.rfind(b"NSString")
    if idx < 0:
        return None
    tail = blob[idx + len(b"NSString"):]
    best: list[int] = []
    current: list[int] = []
    for b in tail:
        if b in (9, 10, 13) or 32 <= b < 127 or b >= 128:
            current.append(b)
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    if len(best) < 1:
        return None
    try:
        return bytes(best).decode("utf-8", errors="replace").strip() or None
    except Exception:
        return None


_SNAPSHOT_MTIME_CACHE: dict[str, tuple[float, float, float]] = {}

# Per-process random snapshot path. Allocated lazily on first use via
# tempfile.mkstemp so the filename can't be guessed by a co-resident
# process (closing the predictable-name + symlink-follow race from
# ISO_AUDIT 2026-05 HIGH-3). Lives for the whole process lifetime;
# subsequent polls reuse + overwrite via shutil.copy2 — but the path
# is unknown to attackers so no symlink can be planted at it.
_SNAPSHOT_PATHS: tuple[Path, Path, Path] | None = None


def _ensure_snapshot_paths() -> tuple[Path, Path, Path]:
    """Allocate (db, wal, shm) tmpfile paths once per process, born 0600."""
    global _SNAPSHOT_PATHS
    if _SNAPSHOT_PATHS is None:
        import os as _os
        fd, db_path_str = tempfile.mkstemp(
            prefix="pa_chat_snap_", suffix=".db",
            dir=tempfile.gettempdir(),
        )
        _os.close(fd)  # mkstemp already created the file with 0600
        db_path = Path(db_path_str)
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")
        shm_path = db_path.with_suffix(db_path.suffix + "-shm")
        _SNAPSHOT_PATHS = (db_path, wal_path, shm_path)
    return _SNAPSHOT_PATHS


def _open_chat_db_readonly(chat_db: Path) -> sqlite3.Connection:
    """Open chat.db read-only. Copy to a temp file first — Messages.app holds a
    write lock on WAL, and trying to attach to a live WAL from another process
    sometimes fails. A snapshot copy is safe and cheap (chat.db is small).

    Skip de copy als chat.db én WAL én SHM dezelfde mtime hebben als de
    laatste keer — scheelt I/O en file-descriptor druk in de 3s poll-loop.
    """
    tmp, wal_tmp, shm_tmp = _ensure_snapshot_paths()
    wal = chat_db.with_suffix(chat_db.suffix + "-wal")
    shm = chat_db.with_suffix(chat_db.suffix + "-shm")

    src_mtimes = (
        chat_db.stat().st_mtime,
        wal.stat().st_mtime if wal.exists() else 0.0,
        shm.stat().st_mtime if shm.exists() else 0.0,
    )
    cache_key = str(chat_db)
    if _SNAPSHOT_MTIME_CACHE.get(cache_key) != src_mtimes or not tmp.exists():
        # Snapshots inherit chat.db's mode (often 0644); force 0600 so the
        # cached copy in /tmp is no looser than the source. ISO 27001
        # SECURITY_REVIEW_2 LOW-1. Path is per-process random
        # (mkstemp) so a symlink-attack can't pre-plant the destination
        # — closes ISO_AUDIT 2026-05 HIGH-3.
        shutil.copy2(chat_db, tmp)
        tmp.chmod(0o600)
        if wal.exists():
            shutil.copy2(wal, wal_tmp)
            wal_tmp.chmod(0o600)
        if shm.exists():
            shutil.copy2(shm, shm_tmp)
            shm_tmp.chmod(0o600)
        _SNAPSHOT_MTIME_CACHE[cache_key] = src_mtimes

    uri = f"file:{tmp}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def fetch_new_messages(
    chat_db: Path,
    owner_handles: tuple[str, ...],
    since_rowid: int,
) -> list[IncomingMessage]:
    """Return messages from any owner handle with rowid > since_rowid, oldest first."""
    if not owner_handles:
        return []
    placeholders = ",".join("?" for _ in owner_handles)
    query = f"""
        SELECT
            m.ROWID    AS rowid,
            m.guid     AS guid,
            m.text     AS text,
            m.attributedBody AS attributed_body,
            m.date     AS date,
            m.is_from_me AS is_from_me,
            h.id       AS handle
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.ROWID > ?
          AND m.is_from_me = 0
          AND h.id IN ({placeholders})
        ORDER BY m.ROWID ASC
    """
    conn = _open_chat_db_readonly(chat_db)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, (since_rowid, *owner_handles)).fetchall()
    finally:
        conn.close()

    out: list[IncomingMessage] = []
    for r in rows:
        text = r["text"]
        if not text:
            text = _extract_text_from_attributed_body(r["attributed_body"])
        if not text:
            log.debug("skipping message rowid=%s — empty text and unreadable attributedBody", r["rowid"])
            continue
        out.append(
            IncomingMessage(
                guid=r["guid"],
                rowid=int(r["rowid"]),
                handle=r["handle"],
                text=text,
                received_at=_apple_nanos_to_unix(int(r["date"])),
            )
        )
    return out


_SEND_SCRIPT = """
on run argv
    set targetHandle to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetHandle of targetService
        send messageBody to targetBuddy
    end tell
end run
"""


def send_imessage(handle: str, body: str) -> None:
    """Send an iMessage to `handle` via Messages.app (osascript)."""
    result = subprocess.run(
        ["osascript", "-", handle, body],
        input=_SEND_SCRIPT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript send failed: {result.stderr.strip()}")


_SEND_AUDIO_SCRIPT = """
on run argv
    set targetHandle to item 1 of argv
    set audioPath to POSIX file (item 2 of argv)
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetHandle of targetService
        send audioPath to targetBuddy
    end tell
end run
"""


def send_imessage_audio(handle: str, audio_path: Path) -> None:
    """Verstuur een audio-bestand als attachment via Messages.app.

    Receiver ziet het als een afspeelbaar audio-attachment (geen waveform-
    voicememo UI; daar moet je iMessage's eigen opname-knop voor gebruiken,
    en die kunnen we niet headless aansturen). Klikbaar/afspeelbaar genoeg."""
    result = subprocess.run(
        ["osascript", "-", handle, str(audio_path)],
        input=_SEND_AUDIO_SCRIPT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript audio send failed: {result.stderr.strip()}")
