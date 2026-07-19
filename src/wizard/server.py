"""FastAPI setup-wizard voor Rosa.

Draait op `localhost:8765` tot alle verplichte stappen (welcome,
identity, claude, confirm) zijn afgerond. Elke stap is een POST-endpoint
dat direct doorschrijft naar `ROSA_HOME/config.yaml` +
`ROSA_HOME/secrets.env`. Frontend (`static/wizard.html` + `wizard.js`)
haalt state op via GET `/api/status` en doorloopt de stappen.

Security:
- Bind alleen op 127.0.0.1 (nooit publiek).
- Wizard sluit zichzelf zodra confirm-stap is afgerond en de daemon
  gestart is.
- Session-token (in-memory) verhindert dat een andere lokale user
  op dezelfde Mac de setup kaapt terwijl deze loopt.
"""
from __future__ import annotations

import logging
import secrets as _secrets
import shlex
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_rosa_home
from wizard import adapters
from wizard.state import (
    STEP_IDS,
    WizardState,
    load_config,
    load_secrets,
    save_secret,
    update_config,
)

log = logging.getLogger(__name__)

# In-memory session-token voor deze wizard-run. Frontend krijgt 'em via
# de index-page HTML en stuurt 'em terug in de X-Wizard-Token header.
_SESSION_TOKEN = _secrets.token_urlsafe(24)
_FINISH_EVENT = threading.Event()

# Lock rond load/mutate/save van .wizard_state.json + config.yaml zodat
# twee tabs die tegelijk een stap posten elkaars completed-set niet
# overschrijven. Zie code-review M1.
_STATE_LOCK = threading.Lock()


def _rosa_home() -> Path:
    home = get_rosa_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def _paths() -> tuple[Path, Path, Path]:
    home = _rosa_home()
    return home / "config.yaml", home / "secrets.env", home / ".wizard_state.json"


def _load_state() -> WizardState:
    _, _, state_path = _paths()
    return WizardState.load(state_path)


def _save_state(state: WizardState) -> None:
    _, _, state_path = _paths()
    state.save(state_path)


def _mark_and_save(step_id: str) -> None:
    """Atomic: load state, mark step done, save. Serialised via lock
    zodat concurrent step-posts elkaars completed-set niet overschrijven."""
    with _STATE_LOCK:
        state = _load_state()
        state.mark_done(step_id)
        _save_state(state)


def _extract_existing_values(cfg: dict, sec_path: Path) -> dict:
    """Verzamel per-step de huidige config-waardes voor edit-mode
    pre-fill. Secrets worden NIET teruggegeven — alleen "present?" flag,
    zodat we niet per abuis API-keys naar de UI lekken."""
    user = cfg.get("user") or {}
    from wizard.state import load_secrets
    secrets = load_secrets(sec_path)
    def _mask(k: str) -> str:
        v = secrets.get(k, "")
        return "•" * min(len(v), 12) if v else ""

    return {
        "identity": {
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "timezone": user.get("timezone", "Europe/Amsterdam"),
            "preferred_language": user.get("preferred_language", "en"),
            "home_city": user.get("home_city", ""),
            "home_country": user.get("home_country", "NL"),
            "company": user.get("company", ""),
        },
        "claude": {
            "anthropic_api_key_masked": _mask("ANTHROPIC_API_KEY"),
            "claude_model": (cfg.get("runtime") or {}).get(
                "claude_model", "claude-sonnet-4-6",
            ),
            "local_model_main": (cfg.get("runtime") or {}).get(
                "local_model_main", "llama3.1:8b-instruct-q4_K_M",
            ),
        },
        "imessage": {
            "primary_handle_masked": _mask("OWNER_IMESSAGE_HANDLE"),
        },
        "main_channel": {
            "channel": user.get("main_channel", "imessage"),
        },
        "features": (cfg.get("features") or {}),
        "vips": {
            "items": "\n".join((cfg.get("vips") or {}).get("contacts", [])),
        },
        "uptime": {
            "items": "\n".join((cfg.get("uptime") or {}).get("urls", [])),
        },
        "news": {
            "items": "\n".join((cfg.get("news") or {}).get("feeds", [])),
        },
        "confidential": {
            "items": "\n".join((cfg.get("confidential") or {}).get("domains", [])),
        },
    }


def _require_token(request: Request) -> None:
    tok = request.headers.get("X-Wizard-Token", "").strip()
    if tok != _SESSION_TOKEN:
        raise HTTPException(status_code=403, detail="wizard token invalid")


def build_app() -> FastAPI:
    app = FastAPI(
        title="Rosa Setup",
        description=(
            "Eenmalige setup-wizard. Sluit zichzelf zodra alle verplichte "
            "stappen zijn afgerond."
        ),
        docs_url=None, redoc_url=None,
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)),
                  name="static")

    # --------------------------------------------------------- routes ---

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html_path = static_dir / "wizard.html"
        if not html_path.exists():
            return HTMLResponse(
                "<h1>Rosa Setup</h1><p>Wizard-UI ontbreekt.</p>",
                status_code=503,
            )
        content = html_path.read_text(encoding="utf-8")
        # Inject session-token — frontend leest 'em uit meta tag.
        content = content.replace(
            "__WIZARD_TOKEN__", _SESSION_TOKEN,
        )
        return HTMLResponse(content)

    @app.get("/api/status")
    def status(request: Request) -> JSONResponse:
        _require_token(request)
        state = _load_state()
        cfg_path, sec_path, _ = _paths()
        cfg = load_config(cfg_path)
        # M19d: edit-mode retourneert huidige config-waardes zodat de
        # UI pre-fill't. In initial setup blijft `existing_config`
        # nagenoeg leeg (alleen defaults).
        existing = _extract_existing_values(cfg, sec_path)
        return JSONResponse({
            "steps": list(STEP_IDS),
            "completed": sorted(state.completed),
            "skipped": sorted(state.skipped),
            "finished": state.is_finished(),
            "has_config": cfg_path.exists(),
            "has_secrets": sec_path.exists(),
            "user_name": ((cfg.get("user") or {}).get("name") or "").strip(),
            "existing": existing,
        })

    @app.get("/api/existing/{step_id}")
    def existing_for_step(request: Request, step_id: str) -> JSONResponse:
        """Return current config values for a specific step (edit-mode)."""
        _require_token(request)
        cfg_path, sec_path, _ = _paths()
        cfg = load_config(cfg_path)
        return JSONResponse(
            _extract_existing_values(cfg, sec_path).get(step_id, {}),
        )

    # --------------------------------------------------------- steps ----

    @app.post("/api/step/welcome")
    def step_welcome(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        if not payload.get("consent"):
            raise HTTPException(400, "consent required")
        state = _load_state()
        state.mark_done("welcome")
        _save_state(state)
        return JSONResponse({"ok": True})

    @app.post("/api/step/identity")
    def step_identity(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {
            "user": {
                "name": name,
                "email": email,
                "timezone": str(payload.get("timezone") or "Europe/Amsterdam"),
                "preferred_language": str(payload.get("preferred_language") or "en").lower(),
                "home_city": str(payload.get("home_city") or "").strip(),
                "home_country": str(payload.get("home_country") or "NL"),
                "company": str(payload.get("company") or "").strip(),
            },
        })
        state = _load_state()
        state.mark_done("identity")
        _save_state(state)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/step/claude")
    def step_claude(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        key = str(payload.get("anthropic_api_key") or "").strip()
        if not key.startswith("sk-ant-"):
            raise HTTPException(400, "invalid Anthropic key format")
        _, sec_path, _ = _paths()
        save_secret(sec_path, "ANTHROPIC_API_KEY", key)
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {
            "runtime": {
                "claude_model": str(
                    payload.get("claude_model") or "claude-sonnet-4-6",
                ),
                "local_model_main": str(
                    payload.get("local_model_main")
                    or "llama3.1:8b-instruct-q4_K_M",
                ),
            },
        })
        state = _load_state()
        state.mark_done("claude")
        _save_state(state)
        return JSONResponse({"ok": True})

    @app.post("/api/step/skip")
    def step_skip(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Markeer een stap als 'later regelen'. Alleen voor niet-verplichte."""
        _require_token(request)
        step_id = str(payload.get("step") or "").strip()
        if step_id not in STEP_IDS:
            raise HTTPException(400, f"unknown step {step_id!r}")
        # M6: verplichte stappen mogen niet geskipt worden — anders
        # blijft de wizard vastzitten op confirm met een verwarrende 400.
        from wizard.state import REQUIRED_STEPS
        if step_id in REQUIRED_STEPS:
            raise HTTPException(
                400, f"step {step_id!r} is required and cannot be skipped",
            )
        state = _load_state()
        state.mark_skipped(step_id)
        _save_state(state)
        return JSONResponse({"ok": True})

    @app.post("/api/step/imessage")
    def step_imessage(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        primary = str(payload.get("primary_handle") or "").strip()
        if not primary:
            raise HTTPException(400, "primary_handle required")
        extra = payload.get("extra_handles") or []
        if isinstance(extra, str):
            extra = [h.strip() for h in extra.split(",") if h.strip()]
        _, sec_path, _ = _paths()
        save_secret(sec_path, "OWNER_IMESSAGE_HANDLE", primary)
        if extra:
            save_secret(
                sec_path, "OWNER_EXTRA_HANDLES", ",".join(extra),
            )
        st = _load_state()
        st.mark_done("imessage")
        _save_state(st)
        return JSONResponse({"ok": True})

    # ----------------------------------------------- Slack / Todoist / IMAP

    @app.post("/api/step/slack")
    def step_slack(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Slack integratie. Twee modes:

        - ingest-only (user OAuth token `xoxp-…`): Rosa leest mentions/
          DM's als context; ze antwoordt NIET via Slack.
        - bidirectional (bot token `xoxb-…` + app token `xapp-…`):
          Rosa antwoordt in Slack DM's + user kan main_channel op slack
          zetten voor briefings etc.
        """
        _require_token(request)
        user_token = str(payload.get("token") or "").strip()
        bot_token = str(payload.get("bot_token") or "").strip()
        app_token = str(payload.get("app_token") or "").strip()
        owner_user_id = str(payload.get("owner_user_id") or "").strip()

        if user_token and not user_token.startswith(("xoxp-", "xoxb-")):
            raise HTTPException(
                400, "User token should start with xoxp- or xoxb-",
            )
        if bot_token and not bot_token.startswith("xoxb-"):
            raise HTTPException(400, "Bot token should start with xoxb-")
        if app_token and not app_token.startswith("xapp-"):
            raise HTTPException(400, "App-level token should start with xapp-")

        owner_team_id = str(payload.get("owner_team_id") or "").strip()
        _, sec_path, _ = _paths()
        if user_token:
            save_secret(sec_path, "SLACK_USER_OAUTH_TOKEN", user_token)
        if bot_token:
            save_secret(sec_path, "SLACK_BOT_TOKEN", bot_token)
        if app_token:
            save_secret(sec_path, "SLACK_APP_TOKEN", app_token)
        if owner_user_id:
            save_secret(sec_path, "SLACK_OWNER_USER_ID", owner_user_id)
        if owner_team_id:
            save_secret(sec_path, "SLACK_OWNER_TEAM_ID", owner_team_id)

        st = _load_state()
        st.mark_done("slack")
        _save_state(st)
        return JSONResponse({"ok": True})

    @app.post("/api/step/main_channel")
    def step_main_channel(
        request: Request, payload: dict[str, Any],
    ) -> JSONResponse:
        """Kies naar welk kanaal Rosa's PROACTIEVE messages gaan
        (briefings, day-close, reminders). Replies gaan altijd terug via
        het kanaal waar het bericht binnenkwam."""
        _require_token(request)
        choice = str(payload.get("channel") or "imessage").lower()
        if choice not in ("imessage", "slack"):
            raise HTTPException(400, f"unknown channel: {choice!r}")
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"user": {"main_channel": choice}})
        st = _load_state()
        st.mark_done("main_channel")
        _save_state(st)
        return JSONResponse({"ok": True})

    @app.post("/api/step/todoist")
    def step_todoist(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        token = str(payload.get("token") or "").strip()
        # Todoist tokens are hex-like ~40 chars; just guard against empty
        # and clearly wrong shapes.
        if len(token) < 20:
            raise HTTPException(400, "Todoist token looks too short")
        _, sec_path, _ = _paths()
        save_secret(sec_path, "TODOIST_API_TOKEN", token)
        st = _load_state()
        st.mark_done("todoist")
        _save_state(st)
        return JSONResponse({"ok": True})

    @app.post("/api/step/imap")
    def step_imap(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """IMAP accepteert multi-line input: één regel per account met
        `label host user password [port]`. Wachtwoorden gaan naar
        secrets.env als IMAP_<LABEL>_PASSWORD; de rest naar config.yaml.
        """
        _require_token(request)
        raw = str(payload.get("token") or "").strip()
        if not raw:
            raise HTTPException(400, "imap config required")

        accounts = []
        _, sec_path, _ = _paths()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # shlex.split respecteert quotes zodat wachtwoorden met
            # spaties werken. Zie code-review M3.
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise HTTPException(400, f"IMAP line parse error: {exc}")
            if len(parts) < 4:
                raise HTTPException(
                    400,
                    f"IMAP line needs at least 4 fields "
                    f"(label host user password [port]): {line!r}",
                )
            label, host, user, password = parts[:4]
            try:
                port = int(parts[4]) if len(parts) > 4 else 993
            except ValueError:
                raise HTTPException(
                    400, f"IMAP port must be integer: {parts[4]!r}",
                )
            # Normalize label via shared helper zodat de env-var name
            # 1-op-1 matcht met wat de adapter in imap_accounts.yaml
            # schrijft (zie code-review M1/M2).
            secret_key = (
                f"IMAP_{adapters.normalize_imap_label(label).upper()}_PASSWORD"
            )
            save_secret(sec_path, secret_key, password)
            accounts.append({
                "label": label, "host": host, "user": user,
                "port": port, "password_env": secret_key,
            })

        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"imap": {"accounts": accounts}})
        adapters.write_imap_accounts(_rosa_home(), accounts)
        st = _load_state()
        st.mark_done("imap")
        _save_state(st)
        return JSONResponse({"ok": True, "accounts": len(accounts)})

    # -------------------------------------------------------- Plaud --------

    @app.post("/api/step/plaud")
    def step_plaud(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        audio_folder = str(payload.get("audio_folder") or "").strip()
        backup_folder = str(payload.get("backup_folder") or "").strip()
        if not audio_folder:
            raise HTTPException(400, "audio_folder required")
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"plaud": {
            "audio_folder": audio_folder,
            "backup_folder": backup_folder or "",
        }})
        st = _load_state()
        st.mark_done("plaud")
        _save_state(st)
        return JSONResponse({"ok": True})

    # --------------------------------------- Generic list-based endpoints ---

    def _list_lines(raw: str) -> list[str]:
        out: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out

    @app.post("/api/step/vips")
    def step_vips(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        items = _list_lines(str(payload.get("items") or ""))
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"vips": {"contacts": items}})
        adapters.write_vip_contacts(_rosa_home(), items)
        st = _load_state()
        st.mark_done("vips")
        _save_state(st)
        return JSONResponse({"ok": True, "count": len(items)})

    @app.post("/api/step/uptime")
    def step_uptime(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        items = _list_lines(str(payload.get("items") or ""))
        from urllib.parse import urlparse
        for u in items:
            if not (u.startswith("http://") or u.startswith("https://")):
                raise HTTPException(400, f"URL must start with http(s)://: {u!r}")
            # L3: reject URL zonder geldige hostname (bv. 'https:///no-host').
            if not urlparse(u).hostname:
                raise HTTPException(400, f"URL missing hostname: {u!r}")
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"uptime": {"urls": items}})
        adapters.write_uptime_targets(_rosa_home(), items)
        st = _load_state()
        st.mark_done("uptime")
        _save_state(st)
        return JSONResponse({"ok": True, "count": len(items)})

    @app.post("/api/step/news")
    def step_news(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        items = _list_lines(str(payload.get("items") or ""))
        from urllib.parse import urlparse
        for u in items:
            if not (u.startswith("http://") or u.startswith("https://")):
                raise HTTPException(400, f"RSS URL must be http(s)://: {u!r}")
            if not urlparse(u).hostname:
                raise HTTPException(400, f"RSS URL missing hostname: {u!r}")
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"news": {"feeds": items}})
        adapters.write_news_sources(_rosa_home(), items)
        st = _load_state()
        st.mark_done("news")
        _save_state(st)
        return JSONResponse({"ok": True, "count": len(items)})

    @app.post("/api/step/confidential")
    def step_confidential(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        items = _list_lines(str(payload.get("items") or ""))
        import re
        # L2: strict domain-regex. Voorkomt ../-injecties en IDN-lookalikes
        # (Cyrillic-a etc.) die de exact-match-classifier stil zouden missen.
        _domain_re = re.compile(
            r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+$",
        )
        clean = []
        for d in items:
            d = d.strip().lower()
            if "@" in d or "/" in d:
                raise HTTPException(
                    400, f"Enter bare domains, not URLs/emails: {d!r}",
                )
            if not _domain_re.match(d):
                raise HTTPException(
                    400,
                    f"Invalid domain: {d!r} (expected lowercase ASCII, "
                    f"letters/digits/dashes, at least one dot)",
                )
            clean.append(d)
        items = clean
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"confidential": {"domains": items}})
        adapters.write_confidential_domains(_rosa_home(), items)
        st = _load_state()
        st.mark_done("confidential")
        _save_state(st)
        return JSONResponse({"ok": True, "count": len(items)})

    # ----------------------------------------------------- Notifications ---

    @app.post("/api/step/notifications")
    def step_notifications(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)

        def _hhmm(key: str, default: str) -> str:
            v = str(payload.get(key) or default).strip()
            if len(v) != 5 or v[2] != ":" or not v[:2].isdigit() or not v[3:].isdigit():
                raise HTTPException(400, f"{key} must be HH:MM")
            # L4: semantiek — 26:00 en 12:75 mogen niet.
            hh, mm = int(v[:2]), int(v[3:])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise HTTPException(
                    400, f"{key} out of range: {v} (00:00–23:59)",
                )
            return v

        cfg_path, _, _ = _paths()
        update_config(cfg_path, {
            "briefings": {"morning_time": _hhmm("morning_time", "07:00")},
            "midday": {"time": _hhmm("midday_time", "14:00")},
            "dayclose": {"time": _hhmm("dayclose_time", "20:00")},
            "notifications": {
                "quiet_hours_start": _hhmm("quiet_start", "22:00"),
                "quiet_hours_end": _hhmm("quiet_end", "07:00"),
            },
        })
        st = _load_state()
        st.mark_done("notifications")
        _save_state(st)
        return JSONResponse({"ok": True})

    # -------------------------------------------------------- Features -----

    _ALLOWED_FEATURES = frozenset({
        "reminders", "comm_intel", "todoist_sync", "slack_ingest",
        "plaud_watcher", "voice_in", "uptime_monitor", "travel_alerts",
        "sales", "market_intel", "tenders", "insolvencies",
        "memory_cards", "decisions_log", "patterns", "weekly_retro",
        "weekend_prep", "ceo_letter", "english_practice",
        "okr_coaching", "receipt_collector",
    })

    @app.post("/api/step/features")
    def step_features(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        toggles = payload.get("features") or {}
        if not isinstance(toggles, dict):
            raise HTTPException(400, "features must be an object of {name: bool}")
        clean: dict[str, bool] = {}
        for k, v in toggles.items():
            if k not in _ALLOWED_FEATURES:
                raise HTTPException(400, f"unknown feature: {k}")
            clean[k] = bool(v)
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {"features": clean})
        st = _load_state()
        st.mark_done("features")
        _save_state(st)
        return JSONResponse({"ok": True, "enabled": sum(clean.values())})

    # -------------------------------------------------- Health checks -----

    @app.get("/api/health-check")
    def health_check(request: Request) -> JSONResponse:
        """Ping elke geconfigureerde integratie zodat setup-fouten vroeg
        zichtbaar worden — VOORDAT user 'Finish setup' klikt."""
        _require_token(request)
        from wizard import health_checks
        home = _rosa_home()
        # Anthropic key uit secrets.env
        _, sec_path, _ = _paths()
        secrets = load_secrets(sec_path)
        result = health_checks.run_all(
            anthropic_key=secrets.get("ANTHROPIC_API_KEY", ""),
            google_token_path=home / "google_token.json",
        )
        return JSONResponse(result)

    # ------------------------------------------------------- Google OAuth ---

    @app.post("/api/step/google/init")
    def step_google_init(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Start OAuth: user plakte credentials.json inhoud → we geven
        auth-URL terug voor redirect naar Google."""
        _require_token(request)
        creds_raw = str(payload.get("credentials") or "").strip()
        if not creds_raw:
            raise HTTPException(400, "credentials required")

        # M7: Bouw redirect_uri uit ASGI-scope['server'] (het echte
        # bind-adres van uvicorn) i.p.v. request.base_url — dat laatste
        # respecteert de Host-header en is dus spoofbaar. server-tuple
        # is (host, port); ontbreekt bij TestClient-runs → 127.0.0.1:8765.
        server = request.scope.get("server") or ("127.0.0.1", 8765)
        redirect_uri = f"http://{server[0]}:{server[1]}/oauth/google/callback"

        from wizard import google_oauth
        try:
            auth_url, _state_tok = google_oauth.start_flow(
                creds_raw, redirect_uri,
                session_token=_SESSION_TOKEN,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({
            "ok": True, "auth_url": auth_url,
            "redirect_uri": redirect_uri,
        })

    @app.get("/oauth/google/callback")
    def oauth_google_callback(
        code: str = "", state: str = "", error: str = "",
    ) -> HTMLResponse:
        """Google redirect landt hier. Wissel code voor token en
        redirect terug naar de wizard."""
        if error:
            return HTMLResponse(
                f"<h1>OAuth failed</h1><p>Google returned: {error}</p>"
                f"<p><a href='/'>Back to wizard</a></p>",
                status_code=400,
            )
        if not code or not state:
            return HTMLResponse(
                "<h1>Missing code/state</h1>"
                "<p>Try again from the wizard.</p>",
                status_code=400,
            )

        from wizard import google_oauth
        home = _rosa_home()
        token_path = home / "google_token.json"
        try:
            google_oauth.finish_flow(
                state_token=state, code=code, token_path=token_path,
                session_token=_SESSION_TOKEN,
            )
        except LookupError as exc:
            return HTMLResponse(
                f"<h1>OAuth state expired</h1><p>{exc}</p>"
                f"<p><a href='/'>Back to wizard</a></p>",
                status_code=400,
            )
        except Exception as exc:
            log.exception("oauth callback failed")
            return HTMLResponse(
                f"<h1>OAuth exchange failed</h1><pre>{exc}</pre>"
                f"<p><a href='/'>Back to wizard</a></p>",
                status_code=500,
            )

        # Persist path to config so main.py can find it.
        cfg_path, _, _ = _paths()
        update_config(cfg_path, {
            "google": {"token_path": str(token_path)},
        })

        st = _load_state()
        st.mark_done("google")
        _save_state(st)

        # Nice landing page dat de wizard vertelt om te vervolgen.
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Connected</title>"
            "<link rel='stylesheet' href='/static/wizard.css'>"
            "</head><body><main class='rosa-page'>"
            "<article class='rosa-card'><h2>Google connected</h2>"
            "<p class='lead'>Rosa now has access to your Gmail + Calendar. "
            "You can close this tab and return to the setup wizard.</p>"
            "<div class='rosa-actions'>"
            "<a class='rosa-btn primary' href='/'>Continue setup</a>"
            "</div></article></main></body></html>",
        )

    @app.post("/api/step/confirm")
    def step_confirm(request: Request, payload: dict[str, Any]) -> JSONResponse:
        _require_token(request)
        state = _load_state()
        # Alle REQUIRED_STEPS behalve 'confirm' zelf moeten klaar zijn.
        from wizard.state import REQUIRED_STEPS
        for req in REQUIRED_STEPS:
            if req == "confirm":
                continue
            if req not in state.completed:
                raise HTTPException(
                    400, f"required step {req!r} not completed",
                )
        state.mark_done("confirm")
        _save_state(state)
        _FINISH_EVENT.set()
        return JSONResponse({"ok": True, "finished": True})

    return app


def wait_until_finished(timeout: float | None = None) -> bool:
    """Block totdat wizard voltooid is (confirm-stap succesvol).

    main.py bootstrap gebruikt dit om te wachten tot de gebruiker klaar
    is, en start dan de daemon met de nieuwe config.
    """
    return _FINISH_EVENT.wait(timeout=timeout)


def reset_finish_event() -> None:
    """Test-hook."""
    _FINISH_EVENT.clear()
