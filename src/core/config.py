"""Settings loader: reads config/settings.yaml + .env.

YAML holds runtime / behavioural config (model names, paths, thresholds,
feature flags). .env holds true secrets (API keys) and per-installation
identity (phone number) — anything that varies per machine.

Field names on Settings are kept stable for callers; YAML keys are the
canonical config names.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent  # src/core/config.py → repo root


# -----------------------------------------------------------------------
# ROSA_HOME / ROSA_DEV: per-installatie configuratie-locatie
# -----------------------------------------------------------------------
#
# Nieuwe klanten: ROSA_HOME = ~/Library/Application Support/Rosa/
#   Alle per-klant config + secrets + data zit daar. Aangemaakt door de
#   setup-wizard, geen files in de repo zelf.
#
# the user (dev-modus): ROSA_DEV=1 override't ROSA_HOME naar ROOT zodat
#   zijn draaiende daemon config/settings.yaml + .env + data/ leest zoals
#   voorheen. Guard rail voor de generieke-refactor.

_DEFAULT_ROSA_HOME = Path.home() / "Library" / "Application Support" / "Rosa"


def get_rosa_home() -> Path:
    """Where per-installation config + secrets + data live.

    Priority:
      1. ROSA_HOME env-var (explicit override for tests / packaging)
      2. ROSA_DEV=1 → ROOT (the user's existing setup — config/settings.yaml)
      3. Repo heeft config/settings.yaml → ROOT (implicit dev-mode
         guard — beschermt the user als hij ROSA_DEV vergeet in cron/
         terminal). Zie code-review H4/H5.
      4. ~/Library/Application Support/Rosa/ (new-user default)
    """
    override = os.environ.get("ROSA_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get("ROSA_DEV", "").strip() in ("1", "true", "yes"):
        return ROOT
    # Impliciete dev-mode: repo bevat al een settings.yaml (legacy /
    # the user's setup). Voorkom dat we per ongeluk een tweede home in
    # ~/Library/Application Support/ aanmaken en zijn daemon in de
    # wizard-wachtstate laten hangen.
    if (ROOT / "config" / "settings.yaml").exists():
        return ROOT
    return _DEFAULT_ROSA_HOME


def is_configured() -> bool:
    """True if a config.yaml or settings.yaml exists at ROSA_HOME.
    False → main.py should launch the setup-wizard instead of the daemon."""
    home = get_rosa_home()
    return (home / "config.yaml").exists() or (home / "config" / "settings.yaml").exists()


@dataclass(frozen=True)
class Settings:
    # secrets / per-installation identity (.env)
    anthropic_api_key: str
    owner_handles: tuple[str, ...]
    here_api_key: str | None       # optional — alleen nodig voor travel_alerts
    elevenlabs_api_key: str | None # optional — alleen nodig voor TTS engine='elevenlabs'
    ntfy_topic: str | None         # optional — Ntfy.sh topic voor critical uptime-pushes
    ntfy_server: str               # default ntfy.sh, kan self-hosted instance zijn
    todoist_api_token: str | None  # optional — alleen nodig voor todoist-sync

    # runtime / models (settings.yaml: runtime.*)
    claude_model: str
    local_model_small: str
    local_model_main: str
    embedding_model: str
    whisper_model: str
    ner_model: str | None              # None = NER cascade uit
    log_level: str
    # Default IANA-zone voor schedulers/briefings. Wordt overruled door
    # app_state.active_timezone (the user kan via iMessage "rosa tz PST"
    # zetten — handig wanneer hij op reis is).
    default_timezone: str

    # paths (settings.yaml: paths.*) — absolute, ~ expanded
    data_dir: Path
    audit_dir: Path
    voice_in_dir: Path
    vectors_dir: Path
    db_path: Path                  # yaml: paths.memory_db
    google_credentials_path: Path
    google_token_path: Path
    plaud_inbox_dir: Path
    log_path: Path                 # yaml: paths.log_file
    messages_db_path: Path

    # behaviour
    poll_interval_seconds: float
    briefing_enabled: bool
    briefing_weekday_time: str     # "HH:MM" Europe/Amsterdam, ma-vr
    briefing_weekend_time: str     # "HH:MM" Europe/Amsterdam, za-zo
    dayclose_enabled: bool
    dayclose_time: str             # "HH:MM" Europe/Amsterdam, dagelijks
    midday_enabled: bool
    midday_time: str               # "HH:MM" Europe/Amsterdam, dagelijks
    market_intel_enabled: bool
    market_intel_weekday: int      # 0=ma .. 6=zo (default 6 = zondag)
    market_intel_time: str         # "HH:MM" Europe/Amsterdam
    travel_alerts_enabled: bool
    travel_alerts_check_interval_seconds: int
    travel_alerts_horizon_minutes: int
    travel_alerts_plan_minutes: int
    travel_alerts_buffer_minutes: int
    # SECURITY_REVIEW_2 MEDIUM-2: cap & prune of current_location history.
    location_retention_days: int
    location_min_interval_seconds: int
    # Travel-alerts v2: home-adres als fallback origin wanneer er geen
    # recente phone-locatie is. Pas op: deze coords gaan naar HERE Maps
    # bij elke alert — alleen invullen als je dat acceptabel vindt.
    travel_alerts_home_lat: float | None
    travel_alerts_home_lon: float | None
    meeting_prep_enabled: bool
    meeting_prep_minutes_before: int
    meeting_prep_check_interval_seconds: int
    meeting_prep_skip_internal_only: bool
    expenses_enabled: bool
    expenses_inbox_dir: Path
    expenses_check_interval_seconds: int
    tts_enabled: bool
    tts_engine: str                # 'say' | 'elevenlabs'
    tts_voice: str                 # macOS `say` voice name (Xander voor NL)
    tts_max_chars: int             # cap op TTS-input; voorkomt minutenlange replies
    tts_elevenlabs_voice_id: str
    tts_elevenlabs_model_id: str
    tts_elevenlabs_daily_char_cap: int
    todoist_enabled: bool
    todoist_project_name: str      # default 'Rosa'
    todoist_sync_interval_seconds: int
    # Sinds 28/6: open_loops automatisch in een review-queue plaatsen
    # i.p.v. direct naar Todoist pushen. Reminders blijven WEL auto-syncen.
    todoist_loops_review_queue: bool
    scheduler_calendly_url: str | None   # optioneel: Calendly link in scheduling-replies

    # privacy
    default_sensitivity_label: str
    max_external_payload_bytes: int
    audit_retention_days: int
    # MED-4: shadow-payloads (1.6 MB/dag, geredacteerde bodies) krijgen
    # een kortere window dan de egress-metadata (egress = audit_retention_days).
    payloads_retention_days: int
    # Admin-action audit-stream (set_timezone, uptime_silence, etc) —
    # langer bewaard voor jaar-audits (ISO A.12.4.3 + A.18.1.3).
    admin_retention_days: int
    # ISO MED: per-table retention policies (A.18.1.3 minimal data storage).
    comm_items_retention_days: int
    expenses_retention_days: int
    uptime_events_retention_days_up: int     # 'up'/'down' rijen (ruisig)
    uptime_events_retention_days_alert: int  # 'alert'/'recovery'/'silence' (history)
    # Audit DB-2 (28/6): iMessage-conversaties + processed-message-bodies
    # bevatten echte namen en zinnen; default 180 dagen retentie
    # afgedwongen (was: onbegrensd).
    conversation_turns_retention_days: int
    processed_messages_retention_days: int

    # Lange tool-chains (web_search, multi-tool) → progress-ack naar
    # the user na N seconden zodat hij weet dat de daemon nog leeft.
    progress_ack_threshold_seconds: float
    log_payloads: bool

    # web dashboard
    web_enabled: bool
    web_host: str
    web_port: int

    # extensions: feature-flag map; missing key = disabled
    extensions_enabled: dict[str, bool] = field(default_factory=dict)

    # user profile (config.yaml: user.*) — generic, per-installatie.
    # Genereerd door de setup-wizard. Deze velden vervangen de eerder
    # hardcoded "the user"-defaults in code (M1 refactor).
    user_name: str = "you"
    user_email: str = ""
    user_preferred_language: str = "en"    # "en" | "nl"
    user_home_city: str = ""
    user_home_country: str = "NL"
    # Bedrijfscontext voor CEO-letter en soortgelijke synthetische
    # briefings. Blijft leeg voor de meeste nieuwe gebruikers — de
    # prompt-builder faket "the business you run" bij een lege value.
    user_company: str = ""

    # Maandelijkse Anthropic-budget cap. Als deze maand's spend hoger
    # wordt, throw't de Gateway BudgetExceeded — voorkomt bug-loops
    # die je credits opeten. 0 = uit (geen cap).
    monthly_anthropic_budget_usd: float = 0.0

    # Eigen email-domeinen — uitgaande facturen vanuit deze domeinen
    # worden in comm-intel summarize gemarkeerd als `fyi` (geen TODO
    # in briefing). Default leeg.
    own_email_domains: tuple[str, ...] = ()

    # Wekelijkse CEO-letter via iMessage — vrijdag 17:00 default.
    ceo_letter_enabled: bool = True
    ceo_letter_time: str = "17:00"     # "HH:MM" Europe/Amsterdam
    ceo_letter_weekday: int = 4        # 0=ma .. 4=vr .. 6=zo

    # Weekend-prep: zondag 19:00 voorbereiding op de week. Top-3
    # prioriteiten + flag op items >7d open zonder progress.
    weekend_prep_enabled: bool = True
    weekend_prep_time: str = "19:00"
    weekend_prep_weekday: int = 6      # zondag

    # Weekly retrospective — zaterdag 09:00 reflectie op de week:
    # comm-volume, patronen, delegations, sales-snapshot, ge-closede
    # decisions/loops. Hergebruikt bestaande aggregatoren.
    weekly_retro_enabled: bool = True
    weekly_retro_time: str = "09:00"
    weekly_retro_weekday: int = 5      # zaterdag

    # TenderNed-monitor — polled JSON-feed, filtert op AV/narrowcasting/
    # digital signage en alerteert via iMessage. Default aan.
    tenders_enabled: bool = True
    tenders_poll_interval_seconds: int = 1800   # 30 min
    tenders_page_size: int = 100
    tenders_skip_expired: bool = True            # geen alert als sluitingsdatum voorbij
    tenders_skip_rectifications: bool = True     # alleen 1e publicatie per kenmerk

    # Sales daily nudges — 3 reminders/dag om dagelijks-3-bedrijven-doel
    # bij the user onder de aandacht te houden. Morgen geeft suggesties,
    # middag check-in op progressie, avond dagevaluatie.
    sales_nudge_enabled: bool = True
    sales_nudge_target_count: int = 3            # aantal contacten per dag
    sales_nudge_morning_time: str = "09:00"      # HH:MM lokaal
    sales_nudge_midday_time:  str = "14:00"
    sales_nudge_evening_time: str = "19:00"
    sales_nudge_skip_weekends: bool = True       # alleen ma-vr

    # Sales-pipeline: auto-touchpoint detectie uit comm_intel.
    # M1 review-fix: opt-out flag voor wie de mail-naar-account
    # correlatie niet wil laten loggen (AVG-keuze).
    sales_auto_touchpoint_enabled: bool = True
    # M3 review-fix: AVG-retentie. Koude accounts zonder touchpoints
    # in N dagen worden door de prune-tick verwijderd.
    sales_retention_cold_days: int = 730

    # Faillissementen-monitor — RSS van faillissementsdossier.nl,
    # filter op KvK-watchlist + activiteit-keyword + naam-keyword.
    insolvencies_enabled: bool = True
    insolvencies_poll_interval_seconds: int = 1800   # 30 min
    insolvencies_max_age_days: int = 7               # backfill-bescherming

    # Wekelijkse uptime/downtime-digest via iMessage — maandag 09:00 default.
    # Bron: uptime_events.kind='recovery' over de afgelopen 7 dagen.
    uptime_report_enabled: bool = True
    uptime_report_time: str = "09:00"      # "HH:MM" lokaal
    uptime_report_weekday: int = 0          # 0=ma .. 4=vr .. 6=zo
    uptime_report_threshold_pct: float = 99.0  # ⚠️ vlag bij <%
    # Escalation-laag: bij downtime >= N sec wordt Ntfy automatisch
    # toegevoegd aan de alert-channels (mits topic geconfigureerd).
    # 0 = uit, default 600s (10 min). Ntfy Critical breekt door
    # iPhone DND/Focus heen wanneer correct geconfigureerd.
    uptime_escalate_after_seconds: int = 600
    uptime_report_include_incidents: bool = True

    # Dagelijkse English-practice nudge — alleen wanneer cards due zijn.
    english_practice_enabled: bool = True
    english_practice_time: str = "09:00"        # weekdagen
    english_practice_weekend_time: str = "10:00"  # iets later in weekend
    english_practice_skip_weekend: bool = True   # default: alleen ma-vr

    @property
    def primary_handle(self) -> str:
        return self.owner_handles[0]

    def extension_on(self, name: str) -> bool:
        return bool(self.extensions_enabled.get(name, False))


def load_settings(*, config_path: Path | None = None) -> Settings:
    home = get_rosa_home()

    # secrets.env voor nieuwe klanten (in ROSA_HOME); .env voor the user-dev.
    load_dotenv(home / "secrets.env")
    load_dotenv(home / ".env")

    if config_path is not None:
        cfg_path = config_path
    else:
        # Prefer nieuwe naam config.yaml op ROSA_HOME; fallback op oude
        # settings.yaml voor the user's dev-setup.
        new_cfg = home / "config.yaml"
        old_cfg = home / "config" / "settings.yaml"
        cfg_path = new_cfg if new_cfg.exists() else old_cfg

    if not cfg_path.exists():
        example = ROOT / "config" / "config.example.yaml"
        raise RuntimeError(
            f"config.yaml missing at {cfg_path}. Run the setup wizard: "
            f"`rosa setup` — or copy {example} to {cfg_path} and edit."
        )
    with cfg_path.open("rb") as f:
        cfg = yaml.safe_load(f) or {}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing — put it in "
            f"{home}/secrets.env (or run the wizard)."
        )

    primary = os.environ.get("OWNER_IMESSAGE_HANDLE", "").strip()
    extra = [h.strip() for h in os.environ.get("OWNER_EXTRA_HANDLES", "").split(",") if h.strip()]
    handles = tuple(h for h in (primary, *extra) if h)
    if not handles:
        raise RuntimeError(
            "OWNER_IMESSAGE_HANDLE missing — put your phone number or "
            f"iMessage-email in {home}/secrets.env."
        )

    runtime = cfg.get("runtime", {}) or {}
    paths = cfg.get("paths", {}) or {}
    imessage = cfg.get("imessage", {}) or {}
    briefings = cfg.get("briefings", {}) or {}
    dayclose = cfg.get("dayclose", {}) or {}
    midday = cfg.get("midday", {}) or {}
    market_intel = cfg.get("market_intel", {}) or {}
    travel_alerts = cfg.get("travel_alerts", {}) or {}
    meeting_prep = cfg.get("meeting_prep", {}) or {}
    expenses = cfg.get("expenses", {}) or {}
    tts = cfg.get("tts", {}) or {}
    todoist = cfg.get("todoist", {}) or {}
    scheduler = cfg.get("scheduler_assist", {}) or {}
    privacy = cfg.get("privacy", {}) or {}
    web = cfg.get("web", {}) or {}
    extensions = cfg.get("extensions", {}) or {}
    user = cfg.get("user", {}) or {}
    # `features:` block is de nieuwe canonieke plek voor feature-flags in
    # de generic-refactor. Legacy `extensions:` block blijft geldig voor
    # backwards-compat met the user's ROSA_DEV=1 setup — de merge-flow
    # aggregeert beide.
    features = cfg.get("features", {}) or {}
    merged_flags: dict[str, bool] = {}
    for k, v in extensions.items():
        merged_flags[str(k)] = bool(v)
    for k, v in features.items():
        merged_flags[str(k)] = bool(v)

    return Settings(
        anthropic_api_key=api_key,
        owner_handles=handles,
        here_api_key=os.environ.get("HERE_API_KEY", "").strip() or None,
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip() or None,
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip() or None,
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip(),
        todoist_api_token=os.environ.get("TODOIST_API_TOKEN", "").strip() or None,
        claude_model=str(runtime.get("claude_model", "claude-sonnet-4-6")),
        default_timezone=str(runtime.get("default_timezone", "Europe/Amsterdam")),
        local_model_small=str(runtime.get("local_model_small", "phi3:mini")),
        local_model_main=str(runtime.get("local_model_main", "llama3.1:8b-instruct-q4_K_M")),
        embedding_model=str(runtime.get("embedding_model", "nomic-embed-text")),
        whisper_model=str(runtime.get("whisper_model", "base")),
        ner_model=(_str_or_none(runtime.get("ner_model"))),
        log_level=str(runtime.get("log_level", "INFO")).upper(),
        data_dir=_resolve(paths.get("data_dir", "data")),
        audit_dir=_resolve(paths.get("audit_dir", "data/audit")),
        voice_in_dir=_resolve(paths.get("voice_in_dir", "data/voice-in")),
        vectors_dir=_resolve(paths.get("vectors_dir", "data/vectors")),
        db_path=_resolve(paths.get("memory_db", "data/memory.db")),
        google_credentials_path=_resolve(paths.get("google_credentials", "google_credentials.json")),
        google_token_path=_resolve(paths.get("google_token", "data/google_token.json")),
        plaud_inbox_dir=_resolve(paths.get("plaud_inbox", "~/PlaudInbox")),
        # Default naar ROSA_HOME/logs/agent.log — dezelfde plek als de
        # LaunchAgent stdout/stderr schrijft (zie scripts/rosa.plist.template).
        # Legacy configs met `log_file: data/logs/agent.log` blijven werken.
        log_path=_resolve(paths.get("log_file", "logs/agent.log")),
        messages_db_path=_resolve(paths.get("messages_db", "~/Library/Messages/chat.db")),
        poll_interval_seconds=float(imessage.get("poll_interval_seconds", 3)),
        briefing_enabled=bool(briefings.get("enabled", True)),
        briefing_weekday_time=str(briefings.get("weekday_time", "08:00")),
        briefing_weekend_time=str(briefings.get("weekend_time", "08:30")),
        dayclose_enabled=bool(dayclose.get("enabled", True)),
        dayclose_time=str(dayclose.get("time", "20:00")),
        midday_enabled=bool(midday.get("enabled", True)),
        midday_time=str(midday.get("time", "13:00")),
        market_intel_enabled=bool(market_intel.get("enabled", True)),
        market_intel_weekday=int(market_intel.get("weekday", 6)),
        market_intel_time=str(market_intel.get("time", "11:00")),
        travel_alerts_enabled=bool(travel_alerts.get("enabled", True)),
        travel_alerts_check_interval_seconds=int(travel_alerts.get("check_interval_seconds", 180)),
        travel_alerts_horizon_minutes=int(travel_alerts.get("horizon_minutes", 120)),
        travel_alerts_plan_minutes=int(travel_alerts.get("plan_minutes", 30)),
        travel_alerts_buffer_minutes=int(travel_alerts.get("buffer_minutes", 5)),
        location_retention_days=int(travel_alerts.get("location_retention_days", 7)),
        location_min_interval_seconds=int(travel_alerts.get("location_min_interval_seconds", 3600)),
        travel_alerts_home_lat=_float_or_none(travel_alerts.get("home_lat")),
        travel_alerts_home_lon=_float_or_none(travel_alerts.get("home_lon")),
        meeting_prep_enabled=bool(meeting_prep.get("enabled", True)),
        meeting_prep_minutes_before=int(meeting_prep.get("minutes_before", 30)),
        meeting_prep_check_interval_seconds=int(meeting_prep.get("check_interval_seconds", 120)),
        meeting_prep_skip_internal_only=bool(meeting_prep.get("skip_internal_only", True)),
        expenses_enabled=bool(expenses.get("enabled", True)),
        expenses_inbox_dir=_resolve(expenses.get("inbox_dir", "~/PA-Receipts")),
        expenses_check_interval_seconds=int(expenses.get("check_interval_seconds", 60)),
        tts_enabled=bool(tts.get("enabled", True)),
        tts_engine=str(tts.get("engine", "say")).lower(),
        tts_voice=str(tts.get("voice", "Xander")),
        tts_max_chars=int(tts.get("max_chars", 600)),
        tts_elevenlabs_voice_id=str(tts.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")),
        tts_elevenlabs_model_id=str(tts.get("elevenlabs_model_id", "eleven_multilingual_v2")),
        tts_elevenlabs_daily_char_cap=int(tts.get("elevenlabs_daily_char_cap", 8000)),
        todoist_enabled=bool(todoist.get("enabled", True)),
        todoist_project_name=str(todoist.get("project_name", "Rosa")),
        todoist_sync_interval_seconds=int(todoist.get("sync_interval_seconds", 300)),
        todoist_loops_review_queue=bool(todoist.get("loops_review_queue", True)),
        scheduler_calendly_url=(_str_or_none(scheduler.get("calendly_url"))),
        own_email_domains=tuple(
            str(d).lower().lstrip("@").strip()
            for d in (cfg.get("own_email_domains") or [])
            if str(d).strip()
        ),
        # M3 review-finding: `cfg.get("tenders")` kan `False` of een
        # andere niet-dict zijn als iemand per ongeluk `tenders: false`
        # in settings.yaml zet. `.get` op een bool crasht — daarom
        # `or {}` als guard.
        tenders_enabled=bool((cfg.get("tenders") or {}).get("enabled", True)),
        tenders_poll_interval_seconds=int(
            (cfg.get("tenders") or {}).get("poll_interval_seconds", 1800)),
        tenders_page_size=int((cfg.get("tenders") or {}).get("page_size", 100)),
        tenders_skip_expired=bool(
            (cfg.get("tenders") or {}).get("skip_expired", True)),
        tenders_skip_rectifications=bool(
            (cfg.get("tenders") or {}).get("skip_rectifications", True)),
        sales_auto_touchpoint_enabled=bool(
            (cfg.get("sales") or {}).get("auto_touchpoint_enabled", True)),
        sales_retention_cold_days=int(
            (cfg.get("sales") or {}).get("retention_cold_days", 730)),
        sales_nudge_enabled=bool(
            (cfg.get("sales") or {}).get("nudge_enabled", True)),
        sales_nudge_target_count=int(
            (cfg.get("sales") or {}).get("nudge_target_count", 3)),
        sales_nudge_morning_time=str(
            (cfg.get("sales") or {}).get("nudge_morning_time", "09:00")),
        sales_nudge_midday_time=str(
            (cfg.get("sales") or {}).get("nudge_midday_time", "14:00")),
        sales_nudge_evening_time=str(
            (cfg.get("sales") or {}).get("nudge_evening_time", "19:00")),
        sales_nudge_skip_weekends=bool(
            (cfg.get("sales") or {}).get("nudge_skip_weekends", True)),
        insolvencies_enabled=bool(
            (cfg.get("insolvencies") or {}).get("enabled", True)),
        insolvencies_poll_interval_seconds=int(
            (cfg.get("insolvencies") or {}).get("poll_interval_seconds", 1800)),
        insolvencies_max_age_days=int(
            (cfg.get("insolvencies") or {}).get("max_age_days", 7)),
        ceo_letter_enabled=bool(cfg.get("ceo_letter", {}).get("enabled", True)),
        ceo_letter_time=str(cfg.get("ceo_letter", {}).get("time", "17:00")),
        ceo_letter_weekday=int(cfg.get("ceo_letter", {}).get("weekday", 4)),
        weekend_prep_enabled=bool(cfg.get("weekend_prep", {}).get("enabled", True)),
        weekend_prep_time=str(cfg.get("weekend_prep", {}).get("time", "19:00")),
        weekend_prep_weekday=int(cfg.get("weekend_prep", {}).get("weekday", 6)),
        weekly_retro_enabled=bool(cfg.get("weekly_retro", {}).get("enabled", True)),
        weekly_retro_time=str(cfg.get("weekly_retro", {}).get("time", "09:00")),
        weekly_retro_weekday=int(cfg.get("weekly_retro", {}).get("weekday", 5)),
        uptime_report_enabled=bool(
            cfg.get("uptime_report", {}).get("enabled", True)),
        uptime_report_time=str(
            cfg.get("uptime_report", {}).get("time", "09:00")),
        uptime_report_weekday=int(
            cfg.get("uptime_report", {}).get("weekday", 0)),
        uptime_report_threshold_pct=float(
            cfg.get("uptime_report", {}).get("threshold_pct", 99.0)),
        uptime_report_include_incidents=bool(
            cfg.get("uptime_report", {}).get("include_incidents", True)),
        uptime_escalate_after_seconds=int(
            (cfg.get("uptime") or {}).get("escalate_after_seconds", 600)),
        english_practice_enabled=bool(
            cfg.get("english_practice", {}).get("enabled", True)),
        english_practice_time=str(
            cfg.get("english_practice", {}).get("time", "09:00")),
        english_practice_weekend_time=str(
            cfg.get("english_practice", {}).get("weekend_time", "10:00")),
        english_practice_skip_weekend=bool(
            cfg.get("english_practice", {}).get("skip_weekend", True)),
        default_sensitivity_label=str(privacy.get("default_label", "internal")),
        max_external_payload_bytes=int(privacy.get("max_external_payload_bytes", 50_000)),
        audit_retention_days=int(privacy.get("audit_retention_days", 90)),
        payloads_retention_days=int(privacy.get("payloads_retention_days", 14)),
        admin_retention_days=int(privacy.get("admin_retention_days", 365)),
        comm_items_retention_days=int(privacy.get("comm_items_retention_days", 365)),
        expenses_retention_days=int(privacy.get("expenses_retention_days", 2555)),
        uptime_events_retention_days_up=int(privacy.get("uptime_events_retention_days_up", 90)),
        uptime_events_retention_days_alert=int(privacy.get("uptime_events_retention_days_alert", 365)),
        conversation_turns_retention_days=int(privacy.get("conversation_turns_retention_days", 180)),
        processed_messages_retention_days=int(privacy.get("processed_messages_retention_days", 180)),
        monthly_anthropic_budget_usd=float(privacy.get("monthly_anthropic_budget_usd", 0.0)),
        progress_ack_threshold_seconds=float(
            cfg.get("progress_ack_threshold_seconds", 15.0),
        ),
        # Audit L-1 (28/6): default false — CLAUDE.md §3 belooft "nooit
        # content" in logs. Payload-shadow-log is opt-in voor redactor-
        # tuning periodes; tijdelijk aanzetten via settings.yaml.
        log_payloads=bool(privacy.get("log_payloads", False)),
        web_enabled=bool(web.get("enabled", False)),
        web_host=str(web.get("host", "127.0.0.1")),
        web_port=int(web.get("port", 8080)),
        extensions_enabled=merged_flags,
        user_name=str(user.get("name") or "you").strip(),
        user_email=str(user.get("email") or "").strip(),
        user_preferred_language=str(user.get("preferred_language") or "en").strip().lower(),
        user_home_city=str(user.get("home_city") or "").strip(),
        user_home_country=str(user.get("home_country") or "NL").strip(),
        user_company=str(user.get("company") or "").strip(),
    )


def _str_or_none(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _float_or_none(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _resolve(p: str) -> Path:
    """Expand ~ and resolve relative to ROSA_HOME.

    Voor the user (ROSA_DEV=1 of impliciete detectie) is ROSA_HOME == ROOT,
    dus zijn `data/memory.db` blijft `<repo>/data/memory.db`. Voor nieuwe
    users met ROSA_HOME=~/Library/Application Support/Rosa/ landt zijn
    data daar. Beide zonder handmatig pad-mappen in config.yaml.
    """
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = get_rosa_home() / path
    return path
