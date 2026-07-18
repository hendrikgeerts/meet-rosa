"""Google OAuth for Gmail + Calendar. Runs a one-shot local-server consent flow
the first time, then auto-refreshes using the stored refresh token."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

log = logging.getLogger(__name__)

# Minimum-noodzakelijke scopes per agent-functie:
# - gmail.modify  : list/get + mark-as-read (messages.modify met removeLabelIds=UNREAD)
# - gmail.send    : draft-loos verzenden (messages.send) — geen Compose UI nodig
# - calendar.events: read/insert/update/delete events op primary calendar.
#                    Bewust geen `auth/calendar` (full): die laat ACL-/sharing-/
#                    calendar-list-mgmt toe wat de agent nooit doet.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]

OAUTH_LOCAL_PORT = 8765


def get_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None

    if token_path.exists():
        # Lees granted scopes uit het token-bestand zelf — `creds.scopes`
        # van Credentials is de aangevraagde set, niet de daadwerkelijk
        # door Google goedgekeurde set. Voor scope-reductie willen we
        # weten wat eerder is goedgekeurd om re-consent te forceren als
        # het bredere rechten heeft dan we nu vragen. Onder launchd
        # kunnen we GEEN browser openen voor de consent-flow, dus we
        # raisen liever met een duidelijke instructie i.p.v. blokkeren.
        try:
            granted = set(json.loads(token_path.read_text()).get("scopes") or [])
        except (OSError, ValueError):
            granted = set()
        wanted = set(SCOPES)
        if granted - wanted:
            raise RuntimeError(
                f"Google OAuth token grants wider scopes than now requested.\n"
                f"  granted: {sorted(granted)}\n"
                f"  wanted:  {sorted(wanted)}\n"
                f"Run `python scripts/setup_google_oauth.py --force` to re-consent "
                f"with the reduced scope-set, then restart the agent."
            )
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("refreshing Google OAuth token")
        creds.refresh(Request())
        _persist(creds, token_path)
        return creds

    if not credentials_path.exists():
        raise RuntimeError(
            f"Google OAuth client secrets missing at {credentials_path}. "
            "Download OAuth desktop-app credentials from Google Cloud Console and save them there."
        )

    log.info("starting Google OAuth consent flow on http://localhost:%d", OAUTH_LOCAL_PORT)
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(
        port=OAUTH_LOCAL_PORT,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )
    _persist(creds, token_path)
    return creds


def _persist(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
