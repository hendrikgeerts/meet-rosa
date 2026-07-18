"""Tests voor integrations.tts: dispatcher + say + elevenlabs paden."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from integrations.tts import (
    _elevenlabs_rate_status, _reset_elevenlabs_rate_for_tests,
    is_available, synthesize,
)


@pytest.fixture(autouse=True)
def _reset_rate_cap():
    """Each test starts with a fresh char-counter so order-independence."""
    _reset_elevenlabs_rate_for_tests()
    yield
    _reset_elevenlabs_rate_for_tests()


# --- meta -----------------------------------------------------------------

def test_is_available_say_returns_bool() -> None:
    assert isinstance(is_available("say"), bool)


def test_is_available_elevenlabs_always_true() -> None:
    assert is_available("elevenlabs") is True


def test_synthesize_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        synthesize("")
    with pytest.raises(ValueError):
        synthesize("   ")


# --- engine: say ----------------------------------------------------------

@pytest.mark.skipif(not is_available("say"), reason="say/afconvert missing")
def test_synthesize_say_produces_m4a(tmp_path: Path) -> None:
    out = synthesize("Test korte zin.", engine="say", out_dir=tmp_path)
    assert out.exists()
    assert out.suffix == ".m4a"
    assert out.stat().st_size > 1000


@pytest.mark.skipif(not is_available("say"), reason="say/afconvert missing")
def test_synthesize_say_cleans_aiff(tmp_path: Path) -> None:
    out = synthesize("Cleanup check.", engine="say", out_dir=tmp_path)
    aiff = out.with_suffix(".aiff")
    assert not aiff.exists()


# --- engine: elevenlabs ---------------------------------------------------

@pytest.mark.skipif(not is_available("say"), reason="say-fallback target missing")
def test_elevenlabs_without_key_falls_back_to_say(tmp_path: Path) -> None:
    """Geen API key opgegeven → silent fallback naar macOS say zodat
    voice-reply altijd lukt, geen exception naar caller."""
    out = synthesize("hallo", engine="elevenlabs", out_dir=tmp_path,
                     elevenlabs_api_key=None)
    assert out.suffix == ".m4a"   # = say-output


@pytest.mark.skipif(not is_available("say"), reason="say-fallback target missing")
def test_elevenlabs_http_failure_falls_back_to_say(tmp_path: Path) -> None:
    """API call gooit fout → fallback naar say, geen crash."""
    with patch("integrations.tts._synth_elevenlabs",
               side_effect=RuntimeError("API down")):
        out = synthesize("test", engine="elevenlabs", out_dir=tmp_path,
                         elevenlabs_api_key="fake-key")
    assert out.suffix == ".m4a"


def test_elevenlabs_success_returns_mp3(tmp_path: Path) -> None:
    """Succesvolle ElevenLabs-call → mp3-pad terug, geen fallback."""
    fake_mp3 = b"\xff\xfb" + b"\x00" * 1000  # >200 bytes is genoeg voor de check
    fake_path = tmp_path / "rosa_el_fake.mp3"

    def fake_synth(text, *, api_key, voice_id, model_id, out_dir):
        fake_path.write_bytes(fake_mp3)
        return fake_path

    with patch("integrations.tts._synth_elevenlabs", side_effect=fake_synth):
        out = synthesize("hallo", engine="elevenlabs", out_dir=tmp_path,
                         elevenlabs_api_key="fake-key")
    assert out == fake_path
    assert out.suffix == ".mp3"


# --- MED-6: daily char-cap with silent fallback -----------------------

@pytest.mark.skipif(not is_available("say"), reason="say-fallback target missing")
def test_elevenlabs_cap_falls_back_to_say_when_budget_exceeded(tmp_path: Path) -> None:
    """First call within budget hits ElevenLabs; next call that would
    exceed cap silently falls back to say without an exception."""
    fake_mp3 = b"\xff\xfb" + b"\x00" * 1000

    def fake_synth(text, *, api_key, voice_id, model_id, out_dir):
        path = out_dir / "rosa_el_fake.mp3"
        path.write_bytes(fake_mp3)
        return path

    with patch("integrations.tts._synth_elevenlabs", side_effect=fake_synth):
        out1 = synthesize(
            "x" * 80, engine="elevenlabs", out_dir=tmp_path,
            elevenlabs_api_key="key", elevenlabs_daily_char_cap=100,
        )
        out2 = synthesize(
            "x" * 30, engine="elevenlabs", out_dir=tmp_path,
            elevenlabs_api_key="key", elevenlabs_daily_char_cap=100,
        )
    # First call: ElevenLabs (mp3). Second: would push 80+30=110 over 100 → say.
    assert out1.suffix == ".mp3"
    assert out2.suffix == ".m4a"


def test_elevenlabs_cap_zero_disables_cap(tmp_path: Path) -> None:
    """cap=0 means unlimited — useful in case Hendrik switches to paid tier."""
    fake_mp3 = b"\xff\xfb" + b"\x00" * 1000

    def fake_synth(text, *, api_key, voice_id, model_id, out_dir):
        path = out_dir / "rosa_el_fake.mp3"
        path.write_bytes(fake_mp3)
        return path

    with patch("integrations.tts._synth_elevenlabs", side_effect=fake_synth):
        # 1000 chars far exceeds any sensible daily cap, but cap=0 disables
        out = synthesize(
            "y" * 1000, engine="elevenlabs", out_dir=tmp_path,
            elevenlabs_api_key="key", elevenlabs_daily_char_cap=0,
        )
    assert out.suffix == ".mp3"


def test_elevenlabs_cap_counter_resets_per_day(tmp_path: Path) -> None:
    """The counter is keyed by ISO date — testing the reset path via
    direct state mutation."""
    from integrations.tts import _RATE_STATE
    fake_mp3 = b"\xff\xfb" + b"\x00" * 1000

    def fake_synth(text, *, api_key, voice_id, model_id, out_dir):
        path = out_dir / "rosa_el_fake.mp3"
        path.write_bytes(fake_mp3)
        return path

    with patch("integrations.tts._synth_elevenlabs", side_effect=fake_synth):
        # Pretend yesterday burned almost all budget — same-day call below
        # should still be capped.
        _RATE_STATE["day"] = "2026-01-01"  # not today
        _RATE_STATE["chars"] = 9999
        out = synthesize(
            "z" * 50, engine="elevenlabs", out_dir=tmp_path,
            elevenlabs_api_key="key", elevenlabs_daily_char_cap=100,
        )
    assert out.suffix == ".mp3"  # yesterday's counter discarded
    day, used = _elevenlabs_rate_status()
    assert used == 50  # only today's call counted


def test_elevenlabs_payload_shape() -> None:
    """De HTTP-call moet text + model + voice_settings sturen."""
    captured: dict = {}

    class FakeResp:
        def read(self) -> bytes:
            return b"\xff\xfb" + b"\x00" * 1000
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        synthesize("hallo wereld", engine="elevenlabs",
                   elevenlabs_api_key="my-key",
                   elevenlabs_voice_id="vid123",
                   elevenlabs_model_id="model_x")

    assert "vid123" in captured["url"]
    # Header-keys worden door urllib title-cased
    assert captured["headers"].get("Xi-api-key") == "my-key"
    import json
    payload = json.loads(captured["body"])
    assert payload["text"] == "hallo wereld"
    assert payload["model_id"] == "model_x"
    assert "voice_settings" in payload
