"""Text-to-Speech voor Rosa's audio-replies.

Twee engines:
- "say"        — macOS built-in (lokaal, gratis, synthetisch)
- "elevenlabs" — neural TTS via api.elevenlabs.io (cloud, gratis tier
                 10K chars/mnd, near-natural Nederlandse stemmen)

Dispatch via `synthesize(engine=..., ...)`. Bij engine="elevenlabs"
zonder API key, of bij netwerkfout, valt de functie automatisch terug
op `say` zodat the user altijd audio krijgt — een TTS-glitch mag een
spraak-reply niet helemaal slikken.
"""
from __future__ import annotations

import json
import logging
import shutil as _shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from core.external_audit import timed_call
from core.perms import open_secure_bytes

log = logging.getLogger(__name__)

# --- macOS `say` defaults --------------------------------------------------
DEFAULT_SAY_VOICE = "Xander"
DEFAULT_SAY_RATE = 180

# --- ElevenLabs defaults ---------------------------------------------------
# 'Rachel' — algemene multi-language stem; werkt redelijk voor NL.
# Vervang via settings.tts_elevenlabs_voice_id voor een NL-native stem
# (the user kan in elevenlabs.io > Voices browsen).
DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
# v2 multilingual model heeft uitstekende Nederlandse uitspraak.
DEFAULT_ELEVENLABS_MODEL = "eleven_multilingual_v2"

# --- Daily rate-cap (SECURITY_REVIEW_2 MEDIUM-6) ------------------------
# ElevenLabs free tier = 10k char/month. Een agent-bug-in-loop kan dat in
# minuten dichten en paid-tier triggeren. Cap op een veilige dagwaarde;
# bij overschrijding silent fallback naar `say`.
import threading as _threading
from datetime import date as _date

_RATE_LOCK = _threading.Lock()
_RATE_STATE: dict[str, int | str] = {"day": "", "chars": 0}


def _elevenlabs_within_daily_cap(text: str, daily_char_cap: int) -> bool:
    """Returns True if `text` fits in today's remaining budget.
    Side-effect: increments the counter on success."""
    if daily_char_cap <= 0:
        return True  # cap disabled
    today = _date.today().isoformat()
    with _RATE_LOCK:
        if _RATE_STATE["day"] != today:
            _RATE_STATE["day"] = today
            _RATE_STATE["chars"] = 0
        used = int(_RATE_STATE["chars"])
        if used + len(text) > daily_char_cap:
            return False
        _RATE_STATE["chars"] = used + len(text)
        return True


def _elevenlabs_rate_status() -> tuple[str, int]:
    """For tests / health-monitoring: (day-ISO, chars-used-today)."""
    with _RATE_LOCK:
        return str(_RATE_STATE["day"]), int(_RATE_STATE["chars"])


def _reset_elevenlabs_rate_for_tests() -> None:
    """Test helper — reset counter so unit tests don't leak state."""
    with _RATE_LOCK:
        _RATE_STATE["day"] = ""
        _RATE_STATE["chars"] = 0


# ==========================================================================
# Public API
# ==========================================================================

def synthesize(
    text: str,
    *,
    engine: str = "say",
    out_dir: Path | None = None,
    # say-specifieke args
    voice: str = DEFAULT_SAY_VOICE,
    rate: int = DEFAULT_SAY_RATE,
    # elevenlabs-specifieke args
    elevenlabs_api_key: str | None = None,
    elevenlabs_voice_id: str = DEFAULT_ELEVENLABS_VOICE_ID,
    elevenlabs_model_id: str = DEFAULT_ELEVENLABS_MODEL,
    elevenlabs_daily_char_cap: int = 8000,
) -> Path:
    """Genereer audio voor `text`. Returns pad naar afspeelbaar audio-bestand
    (.m4a voor say, .mp3 voor elevenlabs — iMessage handelt beide af).

    `elevenlabs_daily_char_cap` (default 8000) beschermt het free-tier
    char-budget tegen agent-bug-in-loop scenarios. Bij overschrijding:
    silent fallback naar `say` (SECURITY_REVIEW_2 MEDIUM-6). Zet op 0
    om de cap uit te schakelen."""
    if not text.strip():
        raise ValueError("empty text")

    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)

    if engine == "elevenlabs":
        if not elevenlabs_api_key:
            log.warning("TTS engine=elevenlabs but no API key — fallback to say")
            return _synth_say(text, voice=voice, rate=rate, out_dir=out_dir)
        if not _elevenlabs_within_daily_cap(text, elevenlabs_daily_char_cap):
            _day, used = _elevenlabs_rate_status()
            log.warning(
                "ElevenLabs daily cap (%d chars) hit (used=%d) — silent "
                "fallback to say for the rest of today",
                elevenlabs_daily_char_cap, used,
            )
            return _synth_say(text, voice=voice, rate=rate, out_dir=out_dir)
        try:
            return _synth_elevenlabs(
                text, api_key=elevenlabs_api_key,
                voice_id=elevenlabs_voice_id,
                model_id=elevenlabs_model_id,
                out_dir=out_dir,
            )
        except Exception:
            log.exception("ElevenLabs TTS failed — fallback to macOS say")
            return _synth_say(text, voice=voice, rate=rate, out_dir=out_dir)

    return _synth_say(text, voice=voice, rate=rate, out_dir=out_dir)


def is_available(engine: str = "say") -> bool:
    """Snelle check of de gevraagde engine bruikbaar is.
    `say` checkt op macOS-tools; `elevenlabs` doet géén netwerk-call,
    geeft alleen aan of de client (always present) functioneel is."""
    if engine == "elevenlabs":
        return True
    return bool(_shutil.which("say") and _shutil.which("afconvert"))


# ==========================================================================
# Engines
# ==========================================================================

def _synth_say(
    text: str, *, voice: str, rate: int, out_dir: Path,
) -> Path:
    """macOS `say` → AIFF → AAC/M4A via afconvert."""
    aiff_path = out_dir / f"rosa_say_{abs(hash(text)) % 10**8}.aiff"
    m4a_path = aiff_path.with_suffix(".m4a")

    say_result = subprocess.run(
        ["say", "-v", voice, "-r", str(rate), "-o", str(aiff_path), text],
        capture_output=True, text=True, timeout=60,
    )
    if say_result.returncode != 0:
        raise RuntimeError(f"say failed: {say_result.stderr.strip()}")

    afc_result = subprocess.run(
        ["afconvert", str(aiff_path), str(m4a_path),
         "-d", "aac", "-f", "m4af", "-b", "64000"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        aiff_path.unlink(missing_ok=True)
    except OSError:
        pass

    if afc_result.returncode != 0:
        raise RuntimeError(f"afconvert failed: {afc_result.stderr.strip()}")
    if not m4a_path.exists() or m4a_path.stat().st_size == 0:
        raise RuntimeError("afconvert produced empty output")
    # SECURITY_REVIEW_2 LOW-2: TTS output contains Rosa's full text — lock
    # down mode so other local processes can't read it.
    m4a_path.chmod(0o600)
    return m4a_path


def _synth_elevenlabs(
    text: str, *, api_key: str, voice_id: str, model_id: str, out_dir: Path,
) -> Path:
    """ElevenLabs Text-to-Speech v1. Returns mp3-pad."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            # style/use_speaker_boost zijn modelspecifiek; defaults laten.
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "User-Agent": "pa-agent/0.1",
        },
    )
    try:
        with timed_call(
            service="elevenlabs",
            endpoint=f"POST /v1/text-to-speech (model={model_id})",
            bytes_out=len(body),
        ) as audit_ctx:
            with urllib.request.urlopen(req, timeout=30) as resp:
                audio_bytes = resp.read()
            audit_ctx.set(status=resp.status, bytes_in=len(audio_bytes))
    except urllib.error.HTTPError as e:
        # Lees response-body voor informatieve error (rate limit / invalid voice / etc.)
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        raise RuntimeError(
            f"ElevenLabs HTTP {e.code}: {err_body}"
        ) from e

    if not audio_bytes or len(audio_bytes) < 200:
        raise RuntimeError("ElevenLabs returned empty/short audio")

    out_path = out_dir / f"rosa_el_{abs(hash(text)) % 10**8}.mp3"
    # open_secure_bytes → file is 0600 from creation, no race window.
    with open_secure_bytes(out_path) as fh:
        fh.write(audio_bytes)
    log.info("ElevenLabs TTS: %d bytes → %s", len(audio_bytes), out_path.name)
    return out_path
