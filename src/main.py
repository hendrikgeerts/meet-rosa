"""Entry point: iMessage poll loop + background scheduler (reminders, briefings,
plaud inbox). Claude handles all reasoning via tool-use."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from core import db, orchestrator
from core.audit import (
    AdminActionLogger, AuditLogger, PayloadAuditLogger, bind_admin_logger,
)
from core.external_audit import bind_audit as _bind_external_audit
from core.config import Settings, load_settings
from core.health import HealthMonitor, HealthState, describe_health
from core.log_scrub import ScrubbingFileHandler, ScrubbingStreamHandler
from core.perms import secure_dir
from core.scheduler import Scheduler
from core.tools import ToolContext, ToolExecutor
from extensions import reminders
from extensions.comm_intel.ingest import IngestWorker, build_sources
from extensions.comm_intel.schema import init_comm_schema
from extensions.config_wishes.schema import init_config_wishes_schema
from extensions.english_practice.schema import init_english_practice_schema
from extensions.decisions.schema import init_decisions_schema
from extensions.expenses.schema import init_expenses_schema
from extensions.market_intel.schema import init_market_intel_schema
from extensions.market_intel.worker import MarketIntelWorker
from extensions.memory.schema import init_memory_schema
from extensions.tenders.schema import init_tenders_schema
from extensions.tenders.worker import TenderWorker
from extensions.insolvencies.schema import init_insolvencies_schema
from extensions.insolvencies.worker import InsolvenciesWorker
from extensions.sales.schema import init_sales_schema
from extensions.meeting_prep.schema import init_meeting_prep_schema
from extensions.open_loops.schema import init_open_loops_schema
from extensions.patterns.schema import init_patterns_schema
from extensions.plaud_intel.schema import init_plaud_meetings_schema
from extensions.projects.schema import init_projects_schema
from core.scheduler_state import init_scheduler_state_schema
from extensions.comm_intel.embeddings import init_embeddings_schema
from extensions.receipt_collector.schema import init_receipt_collector_schema
from extensions.scheduler_assist.schema import init_scheduler_schema
from extensions.todoist_sync.schema import init_todoist_sync_schema
from extensions.todoist_sync.worker import TodoistSyncWorker
from extensions.travel_alerts.schema import init_travel_alerts_schema
from integrations.here_maps import HereMapsClient
from integrations.todoist import TodoistClient
from integrations import imessage, plaud, tts, voice
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from integrations.google_auth import get_credentials
from models.ollama import OllamaClient
from privacy.classifier import load_classifier_from_yaml
from privacy.gateway import Gateway
from privacy.redactor import load_redactor_from_yaml

log = logging.getLogger(__name__)


# SYSTEM_PROMPT_TEMPLATE verhuisd naar core.prompts (code-review-3 M-3)
# — zodat rosa simulate + tests het kunnen importeren zonder main.py's
# module-scope side-effects.
from core.prompts import SYSTEM_PROMPT_TEMPLATE  # noqa: E402



class _Shutdown:
    flag = False
    reload_flag = False  # SIGHUP → main-loop reloadt Settings


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum, frame):
        log.info("received signal %s — shutting down", signum)
        _Shutdown.flag = True
        stop_event.set()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    # SIGHUP → hot-reload. Zet een flag zodat de main-loop bij de
    # volgende poll settings herleest. Signal handlers moeten kort
    # blijven; het echte werk gebeurt in de main-loop.
    def hup(signum, frame):
        log.info("received SIGHUP — flagging config-reload")
        _Shutdown.reload_flag = True
    signal.signal(signal.SIGHUP, hup)


def _configure_logging(settings: Settings) -> None:
    secure_dir(settings.log_path.parent)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            # File-handler scrubt PII en wordt born met 0600.
            ScrubbingFileHandler(settings.log_path),
            # Stdout MOET óók scrubben: launchd redirect stdout naar
            # data/logs/stdout.log. Een gewone StreamHandler zou daar
            # ongeredacteerde body/handles laten staan — regressie van
            # HIGH-1 uit review #1 (ISO_AUDIT 2026-05 CRITICAL-A).
            ScrubbingStreamHandler(sys.stdout),
        ],
    )


def _maybe_transcribe_voice(msg: imessage.IncomingMessage, settings: Settings) -> tuple[str, bool]:
    """If the message has an audio attachment, transcribe it.

    Returns (final_user_text, had_audio_input) — had_audio_input drives
    whether Rosa replies with an audio attachment too (channel-matching).
    """
    try:
        snap = voice.snapshot_chat_db_for_attachments(settings.messages_db_path)
        attachments = voice.attachments_for_message(snap, msg.rowid)
    except Exception:
        log.exception("attachment lookup failed")
        return msg.text, False

    if not attachments:
        return msg.text, False

    transcripts: list[str] = []
    for att in attachments:
        path = voice.resolve_attachment_path(att.filename)
        if not path:
            log.warning("attachment file missing: %s", att.filename)
            continue
        log.info("transcribing %s (mime=%s)", path.name, att.mime_type)
        try:
            text = voice.transcribe_caf(
                path,
                language=None,                       # auto-detect NL/EN
                model_size=settings.whisper_model,
            )
            if text:
                transcripts.append(text)
        except Exception:
            log.exception("transcription failed for %s", path)

    if not transcripts:
        return msg.text or "(voice message — transcription failed)", True

    joined = " ".join(transcripts)
    if msg.text:
        return f"{msg.text}\n[voice message: {joined}]", True
    return f"[voice message: {joined}]", True


def _maybe_send_voice_reply(handle: str, text: str, settings: Settings) -> None:
    """Synthesizeer Rosa's tekst-antwoord naar audio en stuur als iMessage-
    attachment. Best-effort: bij elke fout silent-skip (de tekst-reply is al
    verstuurd), zodat een TTS-glitch geen gebruikersfeedback verliest."""
    # Cap lengte zodat we niet 30s aan voorlezen genereren bij lange replies.
    truncated = text[: settings.tts_max_chars]
    if len(text) > settings.tts_max_chars:
        truncated += " ... (rest staat in tekst hierboven)"
    try:
        audio_path = tts.synthesize(
            truncated,
            engine=settings.tts_engine,
            voice=settings.tts_voice,
            elevenlabs_api_key=settings.elevenlabs_api_key,
            elevenlabs_voice_id=settings.tts_elevenlabs_voice_id,
            elevenlabs_model_id=settings.tts_elevenlabs_model_id,
            elevenlabs_daily_char_cap=settings.tts_elevenlabs_daily_char_cap,
        )
    except Exception:
        log.exception("TTS synthesis failed — skip audio reply")
        return
    try:
        imessage.send_imessage_audio(handle, audio_path)
        log.info("voice-reply sent to %s (%d chars TTS)", handle, len(truncated))
    except Exception:
        log.exception("voice-reply iMessage send failed")


def _start_dashboard_thread(settings: Settings) -> None:
    """Start uvicorn in a daemon thread so the dashboard runs alongside the
    rest of the agent without needing a second process. Bind 127.0.0.1 only."""
    import uvicorn
    from web.app import create_app
    from models.ollama import OllamaClient
    from core.config import get_rosa_home
    config_dir = get_rosa_home() / "config"
    # Aparte Ollama-client voor dashboard suggest-endpoints (los van
    # gateway local_client) zodat een lange call hier de privacy-routing
    # niet blokkeert.
    dashboard_ollama = OllamaClient(model=settings.local_model_main)
    app = create_app(
        settings.audit_dir,
        db_path=settings.db_path,
        imap_yaml=config_dir / "imap_accounts.yaml",
        ollama=dashboard_ollama,
    )
    config = uvicorn.Config(
        app, host=settings.web_host, port=settings.web_port,
        log_level="warning", access_log=False, lifespan="off",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="pa-dashboard", daemon=True)
    t.start()


def _send_catchup_notice_if_backlog(settings: Settings, conn, since_rowid: int) -> None:
    """If we boot and chat.db already has unprocessed messages from owner-handles,
    send a one-line "even bijbenen" per handle so ${user_name} knows we noticed.
    The actual replies come from the regular poll loop right after.

    This is a UX safety net for the launchd-restart case (agent crashed,
    ${user_name} kept typing): without this, ${user_name} experiences radio silence
    until each backlogged turn is fully processed by Claude."""
    try:
        backlog = imessage.fetch_new_messages(
            settings.messages_db_path, settings.owner_handles, since_rowid,
        )
    except Exception:
        log.exception("catch-up: backlog scan failed — skipping notice")
        return

    if not backlog:
        return

    log.info("startup backlog: %d unprocessed message(s) across %d handle(s)",
             len(backlog), len({m.handle for m in backlog}))

    counts: dict[str, int] = {}
    for m in backlog:
        counts[m.handle] = counts.get(m.handle, 0) + 1

    for handle, n in counts.items():
        # Only meaningful when there's > 1 backlogged message; for a single
        # message the regular reply (within ~3s) is fast enough that an
        # extra ping is just noise.
        if n < 2:
            continue
        notice = (
            f"Catching up — I was briefly offline and see {n} messages from you. "
            f"I'll work through them one by one."
        )
        try:
            imessage.send_imessage(handle, notice)
            db.append_turn(conn, handle=handle, role="assistant", content=notice)
        except Exception:
            log.exception("catch-up: failed to send notice to %s", handle)


def _current_date_state_line() -> str:
    """Geef Claude per turn de actuele datum/dag/tijdzone als anker.

    Zonder dit defaultet Claude naar zijn training-cutoff jaar voor
    losse maand-referenties ('mei', 'vorig kwartaal') — wat in mei 2026
    resulteerde in tools die mei 2025 queryden en lege resultaten
    teruggaven. Het anker geldt voor élke chat-turn, niet alleen
    briefings (die hebben hun datum al in de context-JSON).
    """
    from core.timezone import now_local
    now = now_local()
    # Engels weekday + datum, hoort bij Rosa's English-output policy.
    day = now.strftime("%A")
    date = now.strftime("%-d %B %Y")
    tz_name = str(now.tzinfo)
    # ${user_name}-marker wordt door render_system_prompt() gerenderd
    # nadat _handle_message deze state-line aan het prompt heeft geconcat'd.
    header = f"\n\n[TODAY] {day} {date} ({tz_name}). "
    return (
        header
        + "When ${user_name} mentions a date, month, quarter or period "
        + f"without a year, default to the CURRENT year from this line ({now.year}). "
        + "Never use a year from your training-cutoff if the [TODAY] line gives "
        + "you a more recent one."
    )


def _user_profile_block(profile_path) -> str:
    """Render het user_profile als SYSTEM_PROMPT-sectie. Leeg returnt
    als file niet bestaat zodat Rosa zonder profile blijft draaien."""
    try:
        from extensions.user_profile.profile import (
            load_user_profile, render_for_prompt,
        )
        profile = load_user_profile(profile_path)
        rendered = render_for_prompt(profile)
        return ("\n\n" + rendered) if rendered else ""
    except Exception:
        log.exception("user_profile render failed")
        return ""


def _english_practice_state_line(db_path) -> str:
    """Append a single-line hint to the system prompt when an English-practice
    card is currently active, so Claude knows to treat ${user_name}'s next message
    as the answer and call english_practice_evaluate."""
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT s.active_card_id, c.collocation, c.unit_title "
                "FROM english_state s "
                "LEFT JOIN english_cards c ON c.id = s.active_card_id "
                "WHERE s.singleton = 1"
            ).fetchone()
    except Exception:
        log.exception("english state lookup failed")
        return ""
    if not row or not row["active_card_id"]:
        return "\n\n[ENGLISH PRACTICE STATE] No active card."
    unit = f" (unit: {row['unit_title']})" if row["unit_title"] else ""
    # ${user_name}-marker wordt door render_system_prompt() gerenderd.
    header = f"\n\n[ENGLISH PRACTICE STATE] ACTIVE CARD #{row['active_card_id']}: "
    return (
        header
        + f"\"{row['collocation']}\"{unit}. "
        + "${user_name}'s next message is most "
        + "likely his answer-sentence — call english_practice_evaluate."
    )


def _handle_message(
    msg: imessage.IncomingMessage,
    *,
    settings: Settings,
    gateway: Gateway,
    executor: ToolExecutor,
    conn,
    health_state: HealthState,
) -> None:
    user_text, had_audio_input = _maybe_transcribe_voice(msg, settings)
    log.info("incoming from %s: %s", msg.handle, user_text[:100])

    # 'rosa?' shortcut — instant heartbeat, bypass orchestrator + Claude.
    stripped = user_text.strip().lower().rstrip("?!.,")
    if stripped in ("rosa", "rosa status"):
        try:
            imessage.send_imessage(msg.handle, describe_health(health_state))
        except Exception:
            log.exception("rosa?-shortcut send failed")
        db.mark_processed(conn, guid=msg.guid, rowid=msg.rowid,
                          handle=msg.handle, text=user_text,
                          received_at=msg.received_at)
        return

    # Quick-commands (help, status, test) — beantwoord lokaal zodat een
    # verse user direct feedback krijgt zonder Anthropic-latency.
    from core.quick_commands import try_quick_command
    quick = try_quick_command(user_text, settings)
    if quick is not None:
        try:
            imessage.send_imessage(msg.handle, quick)
        except Exception:
            log.exception("quick-command send failed")
        db.mark_processed(conn, guid=msg.guid, rowid=msg.rowid,
                          handle=msg.handle, text=user_text,
                          received_at=msg.received_at)
        return

    db.mark_processed(
        conn,
        guid=msg.guid,
        rowid=msg.rowid,
        handle=msg.handle,
        text=user_text,
        received_at=msg.received_at,
    )
    db.append_turn(conn, handle=msg.handle, role="user", content=user_text)

    history = [
        {"role": t["role"], "content": t["content"]}
        for t in db.recent_turns(conn, msg.handle, limit=20)[:-1]  # drop the just-appended user turn
    ]

    # config_dir leeft alleen in run() scope — _handle_message wordt
    # apart aangeroepen, dus we deriven 'em opnieuw vanuit ROSA_HOME
    # (i.p.v. settings.data_dir.parent — zie code-review H3).
    from core.config import get_rosa_home
    _config_dir = get_rosa_home() / "config"
    # Per-installatie render: vervang "${user_name}" door user.name uit config.
    # We renderen de COMPLETE system_prompt (inclusief state-lines en user-
    # profile-block) i.p.v. alleen SYSTEM_PROMPT_TEMPLATE, zodat state-lines
    # die "${user_name}" bevatten ook correct gesubstitueerd worden, en zodat
    # user_profile.yaml zelf markers mag bevatten die de user runtime kan
    # aanpassen. Identiek voor the user (user.name='the user'), gerenderd voor
    # klanten.
    from core.prompt_builder import render_system_prompt
    system_prompt = render_system_prompt(
        SYSTEM_PROMPT_TEMPLATE
        + _current_date_state_line()
        + _english_practice_state_line(settings.db_path)
        + _user_profile_block(_config_dir / "user_profile.yaml"),
        settings,
    )

    # Progress-ack: bij lange tool-chains (web_search + multi-call)
    # krijgt de user tussentijds een "moment bezig"-bericht zodat hij
    # weet dat de daemon nog leeft. Hooguit één ack per converse-call.
    def _ack(iter_count: int, _elapsed: float) -> None:
        if iter_count == 1:
            text = "Moment, even uitzoeken…"
        else:
            text = f"Moment, ik ben aan het zoeken (tool-call #{iter_count} bezig)…"
        try:
            imessage.send_imessage(msg.handle, text)
        except Exception:
            log.exception("progress-ack send failed")

    try:
        reply_text, _ = orchestrator.converse(
            gateway=gateway,
            executor=executor,
            system_prompt=system_prompt,
            history=history,
            user_message=user_text,
            progress_notify=_ack,
            progress_threshold_seconds=settings.progress_ack_threshold_seconds,
        )
    except Exception:
        log.exception("agent.converse failed")
        imessage.send_imessage(msg.handle, "Sorry, something went wrong on my end. Please try again in a moment.")
        return

    if not reply_text.strip():
        reply_text = "(geen antwoord gegenereerd)"

    try:
        imessage.send_imessage(msg.handle, reply_text)
    except Exception:
        log.exception("iMessage send failed")
        return

    # Channel-matching: als ${user_name} een spraakbericht stuurde, antwoorden we
    # ook met audio. Tekst gaat altijd mee (zodat je later in de chat-historie
    # nog kan teruglezen). Audio als losse attachment direct na de tekst.
    if had_audio_input and settings.tts_enabled and tts.is_available():
        _maybe_send_voice_reply(msg.handle, reply_text, settings)
        return

    db.append_turn(conn, handle=msg.handle, role="assistant", content=reply_text)
    log.info("replied to %s: %s", msg.handle, reply_text[:100])


def _bump_fd_limit(target: int = 4096) -> None:
    """Verhoog RLIMIT_NOFILE — voorkomt 'Too many open files' crash op
    macOS waar de default soft-limit (256) snel bereikt wordt door de
    poll-loop (chat.db copies elke 3s + IMAP/Whisper/Audit FDs)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        new_soft = min(target, hard if hard > 0 else target)
        if new_soft > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            log.info("bumped RLIMIT_NOFILE: %d → %d (hard=%d)", soft, new_soft, hard)
    except Exception:
        log.exception("kon RLIMIT_NOFILE niet bumpen — ga door met default")


def run() -> None:
    # Bootstrap: launch setup-wizard als deze installatie nog niet
    # geconfigureerd is. No-op in dev-mode (ROSA_DEV=1) en no-op als
    # ROSA_HOME/config.yaml al bestaat. Zie wizard/bootstrap.py.
    from wizard.bootstrap import ensure_configured
    ensure_configured()

    settings = load_settings()
    _configure_logging(settings)
    _bump_fd_limit()

    # Pidfile — `rosa reload` leest hem om de PID te vinden zonder
    # false-positives via `pgrep -f src/main.py`. Zie code-review-3 H-1.
    from core.config import get_rosa_home
    _pidfile = get_rosa_home() / "rosa.pid"
    try:
        _pidfile.write_text(f"{os.getpid()}\n")
    except OSError:
        log.warning("could not write pidfile at %s", _pidfile)
    import atexit as _atexit
    def _remove_pidfile() -> None:
        try:
            if _pidfile.exists() and _pidfile.read_text().strip() == str(os.getpid()):
                _pidfile.unlink()
        except OSError:
            pass
    _atexit.register(_remove_pidfile)

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    health_state = HealthState()

    # Init all SQLite schemas in the single pa_agent.sqlite file.
    db.init_db(settings.db_path)
    reminders.init_reminders_schema(settings.db_path)
    plaud.init_plaud_schema(settings.db_path)
    init_comm_schema(settings.db_path)
    init_open_loops_schema(settings.db_path)
    init_plaud_meetings_schema(settings.db_path)
    init_market_intel_schema(settings.db_path)
    init_travel_alerts_schema(settings.db_path)
    from core.app_state import init_app_state_schema
    init_app_state_schema(settings.db_path)
    # Bind het db_path zodat briefings/dayclose/midday/ceo_letter en tools
    # `core.timezone.now_local()` kunnen aanroepen zonder elke caller de
    # path mee te geven. Active TZ resolve uit app_state op-elke-call
    # (5s cache), zodat een "rosa tz America/Los_Angeles" iMessage direct
    # impact heeft.
    from core import timezone as _tz_mod
    _tz_mod.bind(settings.db_path, default_timezone=settings.default_timezone)
    init_todoist_sync_schema(settings.db_path)
    init_scheduler_schema(settings.db_path)
    init_meeting_prep_schema(settings.db_path)
    init_decisions_schema(settings.db_path)
    init_expenses_schema(settings.db_path)
    init_projects_schema(settings.db_path)
    init_patterns_schema(settings.db_path)
    init_receipt_collector_schema(settings.db_path)
    init_scheduler_state_schema(settings.db_path)
    init_embeddings_schema(settings.db_path)
    init_config_wishes_schema(settings.db_path)
    init_english_practice_schema(settings.db_path)
    init_memory_schema(settings.db_path)
    init_tenders_schema(settings.db_path)
    init_insolvencies_schema(settings.db_path)
    init_sales_schema(settings.db_path)

    # Google OAuth — will run consent flow once if token missing.
    creds = get_credentials(settings.google_credentials_path, settings.google_token_path)
    gmail = GmailClient(creds)
    calendar = CalendarClient(creds)
    # Pak Gmail's eigen 'sendAs' default — nodig voor scheduler_assist
    # om te weten welk From-adres bij Gmail-replies hoort.
    try:
        _profile = gmail._service.users().getProfile(userId="me").execute()  # noqa: SLF001
        gmail_address = str(_profile.get("emailAddress") or "")
    except Exception:
        log.exception("kon Gmail profile niet ophalen — gmail_address leeg")
        gmail_address = ""

    audit = AuditLogger(settings.audit_dir)
    payload_audit = PayloadAuditLogger(settings.audit_dir) if settings.log_payloads else None
    # Admin-action trail (apart bestand, langere retention). Modules
    # roepen `core.audit.log_admin_action(...)` aan zonder context.
    bind_admin_logger(AdminActionLogger(settings.audit_dir))
    # Bind audit voor non-Claude external calls (HERE, Todoist, ElevenLabs,
    # SMTP, RSS-feeds). Integrations doen log_external() zonder de logger
    # zelf te hoeven krijgen.
    _bind_external_audit(audit)

    # Privacy classifier from yaml dictionaries; missing files = empty rules,
    # default-label fallback still works.
    from core.config import get_rosa_home
    config_dir = get_rosa_home() / "config"
    classifier = load_classifier_from_yaml(
        confidential_path=config_dir / "confidential_domains.yaml",
        vip_path=config_dir / "vip_contacts.yaml",
        default_label=settings.default_sensitivity_label,
    )

    # Redactor uses the same VIP-list to consistently pseudonymise people /
    # orgs / projects across the conversation; regex catches IBAN/email/etc
    # that aren't in the dictionary; spaCy NER catches names that aren't
    # in either (only used when settings.ner_model is set).
    # user.home_city als extra safe-term zodat NER 'em nooit redact.
    # Voorheen ${user_name}-specifiek in Redactor-comment; nu per-installatie.
    _extra_safe = tuple(t for t in (settings.user_home_city,) if t)
    redactor = load_redactor_from_yaml(
        vip_path=config_dir / "vip_contacts.yaml",
        ner_model=settings.ner_model,
        extra_safe_terms=_extra_safe,
    )

    # Lokaal model voor confidential routing en preflight-fallback. Als Ollama
    # niet draait, valt de gateway terug naar Claude (met audit-warning) —
    # zichtbaar in egress-log.
    # keep_alive=-1 zodat llama3.1:8b in memory blijft — anders elke
    # call 30-60s model-load-overhead op Intel CPU (was de oorzaak van
    # summarize-timeouts en indexing-bottleneck).
    local_client = OllamaClient(model=settings.local_model_main, keep_alive=-1)

    # Aparte client voor de summarizer in de comm-intel ingest-loop.
    # Bewust hetzelfde model als local_client (llama3.1:8b): het hoofdmodel
    # geeft veel beter Nederlands dan phi3:mini (die soms 'respondie' /
    # 'overtonging' produceert). Wel langzamer, maar passief background-werk
    # mag dat zijn — newsletter-pre-filter pakt al >50% volume af.
    summarize_client = OllamaClient(model=settings.local_model_main, keep_alive=-1)

    # Cost-tracker schema in de shared SQLite db
    from core.cost_tracker import init_cost_schema
    init_cost_schema(settings.db_path)

    gateway = Gateway(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        audit=audit,
        classifier=classifier,
        redactor=redactor,
        local_client=local_client,
        payload_audit=payload_audit,
        cost_db_path=settings.db_path,
        monthly_budget_usd=settings.monthly_anthropic_budget_usd,
    )
    # Laad IMAP accounts (incl. SMTP-config) zodat scheduler_assist het
    # kan gebruiken om replies via het juiste mailbox te sturen.
    from integrations.imap import all_enabled as _imap_all
    imap_yaml_path = config_dir / "imap_accounts.yaml"
    imap_accounts_for_send = (
        [a for a, _pw in _imap_all(imap_yaml_path)]
        if imap_yaml_path.exists() else []
    )

    # Todoist-client al hier opzetten zodat zowel de orchestrator-tools
    # (todoist_list_open_tasks etc) als de sync-worker dezelfde instance
    # delen. Faalt het: tools krijgen None en geven een nette
    # "todoist not configured"-error i.p.v. crashen.
    todoist_client: TodoistClient | None = None
    todoist_project = None
    if settings.todoist_enabled and settings.todoist_api_token:
        try:
            # Review 27/6 L6: korte timeout (5s) zodat een Todoist-outage
            # de daemon-bootstrap niet 15s vertraagt. Worker en tools doen
            # later hun eigen retries op de echte data-calls.
            todoist_client = TodoistClient(
                settings.todoist_api_token, timeout=5.0,
            )
            todoist_project = todoist_client.find_or_create_project(
                settings.todoist_project_name,
            )
            # Bump terug naar de defaults voor lopende data-calls.
            todoist_client._timeout = 15.0  # type: ignore[attr-defined]
        except Exception:
            log.exception("todoist client init failed — tools/worker disabled")
            todoist_client = None
            todoist_project = None

    executor = ToolExecutor(ToolContext(
        gmail=gmail,
        calendar=calendar,
        db_path=settings.db_path,
        user_handle=settings.primary_handle,
        imap_accounts=imap_accounts_for_send,
        vip_path=config_dir / "vip_contacts.yaml",
        okrs_path=config_dir / "okrs.yaml",
        gateway=gateway,
        ollama=local_client,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project.id if todoist_project else None,
        user_profile_path=config_dir / "user_profile.yaml",
        settings=settings,
    ))

    morning_extras_yaml = config_dir / "morning_extras.yaml"
    here_client: HereMapsClient | None = None
    if settings.travel_alerts_enabled and settings.here_api_key:
        try:
            here_client = HereMapsClient(
                settings.here_api_key,
                cache_db_path=settings.db_path,
            )
            log.info("travel-alerts: HERE Maps client initialised (geocode cache: SQLite)")
        except Exception:
            log.exception("travel-alerts: HERE init failed — disabled this run")
    elif settings.travel_alerts_enabled:
        log.warning("travel-alerts enabled but HERE_API_KEY missing — feature off")

    scheduler = Scheduler(
        settings=settings,
        gateway=gateway,
        gmail=gmail,
        calendar=calendar,
        send_imessage=imessage.send_imessage,
        stop_event=stop_event,
        morning_extras_yaml=morning_extras_yaml if morning_extras_yaml.exists() else None,
        ollama=summarize_client,
        here=here_client,
        health_state=health_state,
        vip_path=config_dir / "vip_contacts.yaml",
        okrs_path=config_dir / "okrs.yaml",
        uptime_config_path=config_dir / "uptime.yaml",
        gmail_address=gmail_address,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project.id if todoist_project else None,
    )
    scheduler.start()

    health_monitor = HealthMonitor(
        state=health_state,
        stop_event=stop_event,
        send_imessage=imessage.send_imessage,
        primary_handle=settings.primary_handle,
        # Scheduler-ticks kunnen 3-5min duren tijdens briefing/midday/
        # dayclose generation (Claude API + Ollama summarize calls).
        # 8 × 60s = 8min grace zodat één lange tick niet auto-kills.
        consecutive_failures_before_kill=8,
    )
    health_monitor.start()
    log.info("health-monitor started")

    # Comm-intel: passieve ingest-loop voor Gmail + IMAP + Slack. Eigen
    # thread zodat lokale Ollama-summarize calls de scheduler niet blokkeren.
    sources = build_sources(
        gmail_client=gmail,
        imap_yaml=config_dir / "imap_accounts.yaml",
        slack_yaml=config_dir / "slack_workspaces.yaml",
    )
    # Local audit-dashboard (FastAPI) — alleen op 127.0.0.1, geen netwerk-blootstelling.
    if settings.web_enabled:
        _start_dashboard_thread(settings)
        log.info("dashboard listening on http://%s:%d", settings.web_host, settings.web_port)

    # scheduler_assist hook — twee paden:
    #   1. Counter-reply op een bekende thread → notify ${user_name} met
    #      vorige proposal-context (multi-turn flow).
    #   2. Nieuwe scheduling-vraag → propose 3 slots (single-turn flow).
    from extensions.scheduler_assist.detect import is_scheduling_request
    from extensions.scheduler_assist.propose import (
        notify_followup_for_item, propose_for_item,
    )
    from extensions.scheduler_assist.schema import find_recent_in_thread
    import sqlite3 as _sql3

    def _scheduler_hook(item: dict[str, Any]) -> None:
        # Skip outgoing — alleen reageren op INKOMENDE mails.
        if item.get("direction") != "in":
            return

        # 1. Bestaande thread? → follow-up flow ongeacht keyword-match.
        thread_ref = item.get("thread_ref")
        if thread_ref:
            with _sql3.connect(settings.db_path, isolation_level=None) as conn:
                prev = find_recent_in_thread(conn, thread_ref=thread_ref)
            if prev is not None:
                notify_followup_for_item(
                    item=item, prev_proposal=prev,
                    send_imessage=imessage.send_imessage,
                    primary_handle=settings.primary_handle,
                )
                return  # Geen tweede notificatie via single-turn flow.

        # 2. Nieuwe scheduling-vraag — regex-gate + Llama yes/no-gate
        # om false-positives op nieuwsbrieven / agenda-referenties te
        # vangen.
        if not is_scheduling_request(item, ollama=summarize_client):
            return
        propose_for_item(
            item=item,
            db_path=settings.db_path,
            calendar=calendar,
            gateway=gateway,
            imap_accounts=imap_accounts_for_send,
            gmail_default_address=gmail_address,
            send_imessage=imessage.send_imessage,
            primary_handle=settings.primary_handle,
            calendly_url=settings.scheduler_calendly_url,
            user_name=settings.user_name,
            user_signature=settings.user_name,
        )

    if sources:
        # Incremental embedding kan tijdens batch-runs uit (env-var). Geeft
        # de Ollama-queue rust voor de historical_index.py-job en zet
        # daarna 'm weer aan voor real-time RAG.
        embedding_enabled = (
            os.environ.get("PA_INCREMENTAL_EMBED", "1") != "0"
        )
        ingest_worker = IngestWorker(
            db_path=settings.db_path,
            sources=sources,
            ollama=summarize_client,
            stop_event=stop_event,
            poll_interval_seconds=300,
            backfill_days=3,
            per_poll_cap=20,
            embedding_enabled=embedding_enabled,
            own_email_domains=settings.own_email_domains,
            on_item_added=_scheduler_hook,
            location_min_interval_seconds=settings.location_min_interval_seconds,
            sales_auto_touchpoint_enabled=settings.sales_auto_touchpoint_enabled,
        )
        ingest_worker.start()
        log.info("comm-intel ingest-worker started with %d sources", len(sources))
    else:
        log.info("comm-intel: no sources configured — skipped")

    if settings.market_intel_enabled:
        market_worker = MarketIntelWorker(
            db_path=settings.db_path,
            gateway=gateway,
            stop_event=stop_event,
        )
        market_worker.start()
        log.info("market-intel worker started")

    if todoist_client is not None and todoist_project is not None:
        try:
            todoist_worker = TodoistSyncWorker(
                db_path=settings.db_path,
                client=todoist_client,
                project=todoist_project,
                stop_event=stop_event,
                sync_interval_seconds=settings.todoist_sync_interval_seconds,
                review_queue_loops=settings.todoist_loops_review_queue,
            )
            todoist_worker.start()
            log.info("todoist-sync worker started (project=%s)", todoist_project.name)
        except Exception:
            log.exception("todoist-sync worker start failed — disabled this run")
    elif settings.todoist_enabled and not settings.todoist_api_token:
        log.info("todoist enabled but TODOIST_API_TOKEN missing — skipped")

    # --- Uptime monitor ---
    uptime_config_path = config_dir / "uptime.yaml"
    if uptime_config_path.exists():
        try:
            from extensions.uptime.worker import UptimeWorker
            from integrations import tts as _tts
            uptime_worker = UptimeWorker(
                db_path=settings.db_path,
                config_path=uptime_config_path,
                stop_event=stop_event,
                send_imessage=imessage.send_imessage,
                primary_handle=settings.primary_handle,
                tts_synthesize=_tts.synthesize,
                tts_voice=settings.tts_voice,
                send_imessage_audio=imessage.send_imessage_audio,
                ntfy_topic=settings.ntfy_topic,
                ntfy_server=settings.ntfy_server,
                escalate_after_seconds=settings.uptime_escalate_after_seconds,
            )
            uptime_worker.start()
            log.info("uptime monitor started")
        except Exception:
            log.exception("uptime monitor init failed — disabled this run")
    else:
        log.info("uptime monitor: config/uptime.yaml missing — feature off")

    # --- TenderNed-monitor ---
    if settings.tenders_enabled:
        try:
            tender_worker = TenderWorker(
                db_path=settings.db_path,
                stop_event=stop_event,
                send_imessage=imessage.send_imessage,
                primary_handle=settings.primary_handle,
                poll_interval_seconds=settings.tenders_poll_interval_seconds,
                page_size=settings.tenders_page_size,
                skip_expired=settings.tenders_skip_expired,
                skip_rectifications_after_first=settings.tenders_skip_rectifications,
            )
            tender_worker.start()
            log.info("tender-monitor started")
        except Exception:
            log.exception("tender-monitor init failed — disabled this run")
    else:
        log.info("tender-monitor: disabled via settings.tenders_enabled=false")

    # --- Insolvencies-monitor ---
    if settings.insolvencies_enabled:
        try:
            insolv_worker = InsolvenciesWorker(
                db_path=settings.db_path,
                stop_event=stop_event,
                send_imessage=imessage.send_imessage,
                primary_handle=settings.primary_handle,
                poll_interval_seconds=settings.insolvencies_poll_interval_seconds,
                max_publication_age_days=settings.insolvencies_max_age_days,
            )
            insolv_worker.start()
            log.info("insolvencies-monitor started")
        except Exception:
            log.exception("insolvencies-monitor init failed — disabled this run")
    else:
        log.info("insolvencies-monitor: disabled via settings")

    log.info(
        "pa-agent running — model=%s, handles=%s, poll=%.1fs, "
        "briefing=%s@(weekday=%s, weekend=%s), midday=%s@%s, dayclose=%s@%s, "
        "plaud_inbox=%s",
        settings.claude_model,
        settings.owner_handles,
        settings.poll_interval_seconds,
        settings.briefing_enabled,
        settings.briefing_weekday_time,
        settings.briefing_weekend_time,
        settings.midday_enabled,
        settings.midday_time,
        settings.dayclose_enabled,
        settings.dayclose_time,
        settings.plaud_inbox_dir,
    )

    # First-boot welkomstbericht — alleen als de user Rosa net via wizard
    # heeft geconfigureerd. Zie core/first_boot.py voor detectie via
    # marker-file. Idempotent bij re-boots.
    from core.first_boot import send_welcome_if_first_boot
    try:
        send_welcome_if_first_boot(
            user_name=settings.user_name,
            handle=settings.primary_handle,
            sender=imessage.send_imessage,
        )
    except Exception:
        log.exception("welcome-message send failed (non-fatal)")

    with db.connect(settings.db_path) as conn:
        since_rowid = db.max_processed_rowid(conn)
        log.info("resuming from chat.db rowid > %d", since_rowid)
        _send_catchup_notice_if_backlog(settings, conn, since_rowid)

        while not _Shutdown.flag:
            # SIGHUP-driven hot-reload: user edit'te config.yaml en deed
            # `kill -HUP <pid>` (of `rosa reload`).
            #
            # BELANGRIJKE BEPERKING (code-review-3 H-2): alleen de MAIN-LOOP
            # settings-binding wordt vervangen. Long-lived componenten
            # (Gateway, scheduler, orchestrator, executor, HERE-client,
            # Todoist-client, IMAP-connecties) houden een reference naar
            # de OUDE Settings. Wat wél hot-reloadt:
            #  - poll_interval_seconds (main-loop leest per tick)
            #  - primary_handle / owner_handles (idem)
            #  - db_path/messages_db_path (idem)
            #
            # Wat NIET hot-reloadt (vergt daemon-restart):
            #  - claude_model, monthly_anthropic_budget_usd (Gateway __init__)
            #  - lokaal-model naam (OllamaClient)
            #  - feature-flags (scheduler tick-configs, extension enable/disable)
            #  - alle scheduler-times (briefings, dayclose, weekly retro, etc.)
            #  - google_credentials_path (OAuth-client is er al)
            #
            # Voor de niet-reloadable velden: log een warning zodat user
            # snapt dat een `launchctl kickstart -k` nodig is voor die changes.
            if _Shutdown.reload_flag:
                try:
                    new_settings = load_settings()
                    # Warn voor velden die niet automatisch propaganderen.
                    changed_no_op = []
                    for f in ("claude_model", "monthly_anthropic_budget_usd",
                              "local_model_main", "briefing_weekday_time"):
                        old = getattr(settings, f, None)
                        new = getattr(new_settings, f, None)
                        if old != new:
                            changed_no_op.append(f"{f}: {old} → {new}")
                    if changed_no_op:
                        log.warning(
                            "SIGHUP: these fields changed but require a "
                            "daemon-restart to take effect: %s",
                            "; ".join(changed_no_op),
                        )
                    settings = new_settings
                    log.info("config reloaded: user=%s, model=%s",
                             settings.user_name, settings.claude_model)
                except Exception:
                    log.exception("config reload failed — keeping old settings")
                _Shutdown.reload_flag = False
            try:
                new_msgs = imessage.fetch_new_messages(
                    settings.messages_db_path,
                    settings.owner_handles,
                    since_rowid,
                )
                health_state.record_imessage_poll()
            except Exception:
                log.exception("chat.db read failed — retrying")
                time.sleep(settings.poll_interval_seconds)
                continue

            for msg in new_msgs:
                _handle_message(msg, settings=settings, gateway=gateway,
                                 executor=executor, conn=conn,
                                 health_state=health_state)
                since_rowid = max(since_rowid, msg.rowid)

            stop_event.wait(timeout=settings.poll_interval_seconds)

    scheduler.join(timeout=5)
    log.info("pa-agent stopped")


if __name__ == "__main__":
    run()
