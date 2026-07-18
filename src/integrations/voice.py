"""Local voice-message transcription with faster-whisper.

iMessage audio messages arrive as .caf files in ~/Library/Messages/Attachments/,
linked to the message via message_attachment_join. We load the model lazily
(first use only) and cache it in memory."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioAttachment:
    filename: str
    mime_type: str


_model = None
_model_size: str | None = None
_model_lock = Lock()


def _get_model(size: str = "small"):
    """Lazy-init faster-whisper model. Re-laadt als de gevraagde size
    afwijkt van de huidige (settings-change zonder restart)."""
    global _model, _model_size
    with _model_lock:
        if _model is None or _model_size != size:
            from faster_whisper import WhisperModel
            log.info("loading faster-whisper model size=%s (first use may download)", size)
            _model = WhisperModel(size, device="cpu", compute_type="int8")
            _model_size = size
        return _model


def attachments_for_message(chat_db_snapshot: Path, message_rowid: int) -> list[AudioAttachment]:
    conn = sqlite3.connect(chat_db_snapshot)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.filename AS filename, a.mime_type AS mime_type
            FROM attachment a
            JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            WHERE maj.message_id = ?
            """,
            (message_rowid,),
        ).fetchall()
    finally:
        conn.close()
    out: list[AudioAttachment] = []
    for r in rows:
        mime = (r["mime_type"] or "").lower()
        fname = r["filename"] or ""
        if mime.startswith("audio/") or fname.lower().endswith((".caf", ".m4a", ".amr", ".wav", ".mp3")):
            out.append(AudioAttachment(filename=fname, mime_type=mime))
    return out


def transcribe_caf(
    path: Path,
    *,
    language: str | None = None,        # None = auto-detect (NL+EN beide OK)
    model_size: str = "small",
    beam_size: int = 5,                 # nauwkeuriger dan 1; ~2x trager
) -> str:
    """Transcribe a .caf (or any audio file faster-whisper supports).
    Returns the transcript text; may be empty string."""
    if not path.exists():
        raise FileNotFoundError(path)

    model = _get_model(model_size)
    segments, info = model.transcribe(
        str(path),
        language=language,
        vad_filter=True,
        beam_size=beam_size,
        # Verbetert NL/EN herkenning bij accent en omgevingsruis.
        condition_on_previous_text=False,
    )
    if language is None:
        log.info("whisper auto-detected language=%s (prob=%.2f)",
                 info.language, info.language_probability)
    chunks = [seg.text.strip() for seg in segments if seg.text.strip()]
    return " ".join(chunks).strip()


def resolve_attachment_path(filename_from_db: str) -> Path | None:
    """Messages stores filenames like '~/Library/Messages/Attachments/...' — expand ~."""
    if not filename_from_db:
        return None
    p = Path(filename_from_db).expanduser()
    if p.exists():
        return p
    return None


_ATTACH_SNAPSHOT_PATHS: tuple[Path, Path] | None = None


def _ensure_attach_paths() -> tuple[Path, Path]:
    """Lazy-allocate (db, wal) tmpfile paths once per process, born 0600.
    Random name closes ISO_AUDIT 2026-05 HIGH-3 (symlink-follow race
    via predictable /tmp filename)."""
    global _ATTACH_SNAPSHOT_PATHS
    if _ATTACH_SNAPSHOT_PATHS is None:
        import os as _os
        fd, db_path_str = tempfile.mkstemp(
            prefix="pa_chat_attach_", suffix=".db",
            dir=tempfile.gettempdir(),
        )
        _os.close(fd)
        db_path = Path(db_path_str)
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")
        _ATTACH_SNAPSHOT_PATHS = (db_path, wal_path)
    return _ATTACH_SNAPSHOT_PATHS


def snapshot_chat_db_for_attachments(chat_db: Path) -> Path:
    """Copy chat.db to tmp so we can safely read attachment tables alongside messages."""
    tmp, wal_tmp = _ensure_attach_paths()
    shutil.copy2(chat_db, tmp)
    tmp.chmod(0o600)
    wal = chat_db.with_suffix(chat_db.suffix + "-wal")
    if wal.exists():
        shutil.copy2(wal, wal_tmp)
        wal_tmp.chmod(0o600)
    return tmp
