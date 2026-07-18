"""Live integratie-checks voor de wizard.

Vlak voor de user 'Finish setup' klikt: ping elke geconfigureerde
integratie zodat setup-fouten vroeg zichtbaar worden i.p.v. tijdens
de eerste briefing van de nieuwe user.

Elke check retourneert een dict met `{ok, message, details?}`. `ok=False`
blokkeert `confirm` niet — de user kan altijd doorgaan (misschien
weet hij zelf dat een service tijdelijk down is) — maar de UI moet
de failure wel prominent tonen.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any] | str]:
    """Minimal HTTPS-client zonder externe deps. Return (status, parsed_json_or_text)."""
    req = urllib.request.Request(
        url, data=body, method="POST" if body else "GET",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.getcode(), json.loads(raw)
            except json.JSONDecodeError:
                return r.getcode(), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def check_anthropic(api_key: str) -> dict[str, Any]:
    """Test Anthropic-key met de goedkoopste denkbare call: `/v1/models` (0 tokens).

    Deze endpoint bestaat sinds Anthropic API v1 en heeft geen cost.
    Returns 401 bij ongeldige key — dat maakt de check betrouwbaar.
    """
    if not api_key or not api_key.startswith("sk-ant-"):
        return {"ok": False, "message": "No Anthropic API key configured."}
    try:
        code, body = _http_json(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
    except (TimeoutError, urllib.error.URLError, ssl.SSLError) as e:
        return {"ok": False, "message": f"Cannot reach api.anthropic.com: {e}"}
    if code == 200:
        return {"ok": True, "message": "Anthropic API key accepted."}
    if code == 401:
        return {"ok": False, "message": "Anthropic key rejected (401). Check the key you pasted."}
    return {
        "ok": False,
        "message": f"Anthropic returned {code}",
        "details": str(body)[:200],
    }


def check_ollama(host: str = "http://localhost:11434") -> dict[str, Any]:
    """Ping Ollama's /api/tags — bevestigt dat de daemon draait én welke
    modellen beschikbaar zijn."""
    try:
        code, body = _http_json(f"{host}/api/tags", timeout=3.0)
    except (TimeoutError, urllib.error.URLError):
        return {
            "ok": False,
            "message": f"Ollama not reachable at {host}",
            "details": (
                "Start Ollama: `ollama serve` in a Terminal, or run "
                "`brew services start ollama`."
            ),
        }
    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "message": f"Ollama returned unexpected {code}"}
    models = [m.get("name", "?") for m in body.get("models", [])]
    if not models:
        return {
            "ok": False,
            "message": "Ollama is running but has no models pulled.",
            "details": "Run: ollama pull llama3.1:8b-instruct-q4_K_M",
        }
    return {"ok": True, "message": f"Ollama up with {len(models)} model(s).",
            "details": ", ".join(models[:5])}


def check_google_token(token_path: Path) -> dict[str, Any]:
    """Verifieert dat het opgeslagen Google-token de expected fields
    heeft. Doet géén live Google-call om quota te sparen — voldoende
    voor 'werkt de OAuth-flow?' vraag."""
    if not token_path.exists():
        return {
            "ok": False,
            "message": "No Google token found — you skipped Google OAuth.",
        }
    try:
        data = json.loads(token_path.read_text())
    except Exception as e:
        return {"ok": False, "message": f"Cannot parse google_token.json: {e}"}
    missing = [k for k in ("refresh_token", "client_id", "client_secret")
               if not data.get(k)]
    if missing:
        return {
            "ok": False,
            "message": f"Google token missing fields: {missing}",
            "details": "Re-run the Google step in the wizard.",
        }
    return {"ok": True, "message": "Google OAuth token stored and complete."}


def check_full_disk_access() -> dict[str, Any]:
    """Verifieert dat de daemon `~/Library/Messages/chat.db` kan lezen.
    Zonder Full Disk Access krijgt Rosa geen iMessages binnen."""
    chat_db = Path.home() / "Library" / "Messages" / "chat.db"
    if not chat_db.exists():
        return {
            "ok": False,
            "message": "chat.db not found — you may not have used iMessage yet.",
        }
    try:
        with chat_db.open("rb") as f:
            f.read(16)
        return {"ok": True, "message": "iMessage chat.db readable."}
    except PermissionError:
        return {
            "ok": False,
            "message": "chat.db not readable — Rosa needs Full Disk Access.",
            "details": (
                "System Settings → Privacy & Security → Full Disk Access "
                "→ toggle on the terminal or python-binary that runs Rosa."
            ),
        }


def check_imessage_send_permission() -> dict[str, Any]:
    """Best-effort test op AppleScript sending. Direct testen zou een
    echte iMessage sturen — daarom alleen system-config check."""
    try:
        import subprocess
        # `osascript -e 'return 1'` is een noop AppleScript.
        result = subprocess.run(
            ["osascript", "-e", "return 1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return {"ok": True, "message": "osascript is executable."}
        return {"ok": False, "message": f"osascript failed: {result.stderr[:100]}"}
    except Exception as e:
        return {"ok": False, "message": f"osascript check failed: {e}"}


def run_all(
    *,
    anthropic_key: str = "",
    google_token_path: Path | None = None,
    ollama_host: str = "http://localhost:11434",
) -> dict[str, Any]:
    """Voer alle checks uit — returns een dict met `{summary, results}`."""
    results = {
        "anthropic": check_anthropic(anthropic_key),
        "ollama": check_ollama(ollama_host),
        "full_disk_access": check_full_disk_access(),
        "imessage_send": check_imessage_send_permission(),
    }
    if google_token_path is not None:
        results["google"] = check_google_token(google_token_path)

    ok_count = sum(1 for r in results.values() if r["ok"])
    total = len(results)
    return {
        "summary": {
            "ok_count": ok_count,
            "total": total,
            "all_ok": ok_count == total,
        },
        "results": results,
    }
