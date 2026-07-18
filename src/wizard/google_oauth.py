"""Google OAuth-flow voor de setup-wizard.

Anders dan `integrations/google_auth.py` (die `run_local_server` gebruikt
en een eigen HTTP-server op poort 8765 spawnt), draait deze flow BINNEN
de wizard-server zelf:

  1. User plakt zijn OAuth client-credentials (client_id + client_secret,
     of het downloaded credentials.json bestand).
  2. Wizard genereert de Google auth-URL en stuurt user naar Google.
  3. Google redirect terug naar POST /oauth/google/callback met code.
  4. Wizard exchanget code → refresh_token, persist naar
     ROSA_HOME/google_token.json (chmod 0600).
  5. Wizard markeert 'google' als done.

De user moet de callback-URL zelf toevoegen aan zijn Google-OAuth client
config — dat is de enige externe stap die we niet kunnen automatiseren.
Wizard toont die URL letterlijk in de UI zodat copy-paste triviaal is.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Cap + TTL voor _PENDING om memory-leak bij dubbele klikken /
# wizard-restart te voorkomen. Zie code-review H3.
_PENDING_MAX = 5
_PENDING_TTL_SECONDS = 600

log = logging.getLogger(__name__)

# Zelfde scopes als integrations/google_auth.py. Bewust smal gehouden.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]


@dataclass
class PendingOAuthState:
    """In-memory state tussen /init en /callback. Slecht om te
    persisteren want bevat je client-secret; leeft alleen tijdens
    de wizard-run."""
    state_token: str
    credentials_json: dict[str, Any]
    redirect_uri: str
    created_at: float = field(default_factory=time.time)
    session_token: str = ""  # koppel aan _SESSION_TOKEN — zie H2


# Module-scoped state store. Key = state-token.
_PENDING: dict[str, PendingOAuthState] = {}


def _prune_pending() -> None:
    """Verwijder verlopen entries + cap. Voorkom _PENDING-lekkage van
    client_secrets in memory bij dubbele klikken / wizard-restart."""
    now = time.time()
    expired = [k for k, v in _PENDING.items()
               if now - v.created_at > _PENDING_TTL_SECONDS]
    for k in expired:
        _PENDING.pop(k, None)
    # Cap: als we alsnog te vol zijn, drop oudste.
    if len(_PENDING) > _PENDING_MAX:
        by_age = sorted(_PENDING.items(), key=lambda kv: kv[1].created_at)
        for k, _ in by_age[:-_PENDING_MAX]:
            _PENDING.pop(k, None)


def _parse_credentials_input(raw: str) -> dict[str, Any]:
    """Accepteer twee formaten:
      1. Volledige credentials.json inhoud (met "installed" of "web" wrapper).
      2. Alleen {"client_id": "...", "client_secret": "..."}.
    Geeft altijd het "installed"-formaat terug voor Flow-consumers.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty credentials input")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    if "installed" in parsed:
        inner = parsed["installed"]
    elif "web" in parsed:
        inner = parsed["web"]
    else:
        inner = parsed

    client_id = str(inner.get("client_id") or "").strip()
    client_secret = str(inner.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("credentials missing client_id or client_secret")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError(
            "client_id doesn't look like a Google OAuth client "
            "(expected suffix .apps.googleusercontent.com)"
        )

    # Bouw een canoniek "installed"-formaat op voor Flow.from_client_config.
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": inner.get(
                "auth_uri", "https://accounts.google.com/o/oauth2/auth",
            ),
            "token_uri": inner.get(
                "token_uri", "https://oauth2.googleapis.com/token",
            ),
            "redirect_uris": inner.get(
                "redirect_uris", ["http://localhost"],
            ),
        }
    }


def start_flow(
    credentials_input: str,
    redirect_uri: str,
    *,
    session_token: str = "",
) -> tuple[str, str]:
    """Init: parse creds, genereer auth-URL, return (auth_url, state_token).

    session_token binds this pending state to the current wizard-run
    (see code-review H2) — callback moet dezelfde session_token krijgen.
    """
    from google_auth_oauthlib.flow import Flow  # local import

    creds_dict = _parse_credentials_input(credentials_input)
    flow = Flow.from_client_config(
        creds_dict, scopes=SCOPES, redirect_uri=redirect_uri,
    )
    state_token = secrets.token_urlsafe(24)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state_token,
    )
    _prune_pending()
    _PENDING[state_token] = PendingOAuthState(
        state_token=state_token,
        credentials_json=creds_dict,
        redirect_uri=redirect_uri,
        session_token=session_token,
    )
    return auth_url, state_token


def finish_flow(
    *,
    state_token: str,
    code: str,
    token_path: Path,
    session_token: str = "",
) -> None:
    """Callback: wissel code voor tokens en persist naar token_path (0600).

    Verifieert dat state_token bij deze wizard-run hoort. Als de wizard
    tussentijds herstart is (nieuwe session_token) faalt de exchange —
    dat is bewust: we willen niet dat een oude tab een OAuth-code kan
    inwisselen op een verse wizard-installatie.
    """
    from google_auth_oauthlib.flow import Flow  # local import

    _prune_pending()
    pending = _PENDING.pop(state_token, None)
    if pending is None:
        raise LookupError(
            "Unknown OAuth state — did the wizard restart? "
            "Go back and click 'Connect Google' again."
        )
    if pending.session_token and pending.session_token != session_token:
        raise LookupError(
            "OAuth state belongs to a different wizard session. "
            "Reload the wizard and try again."
        )
    flow = Flow.from_client_config(
        pending.credentials_json,
        scopes=SCOPES,
        redirect_uri=pending.redirect_uri,
        state=state_token,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    log.info("google oauth token persisted → %s", token_path)


def clear_pending() -> None:
    """Test-hook."""
    _PENDING.clear()
