"""Eerste-run bootstrap: als er nog geen config is, start de wizard.

Wordt aangeroepen door `main.run()` VOOR `load_settings()`. Als
`ROSA_HOME/config.yaml` niet bestaat en ROSA_DEV=1 niet gezet is,
starten we de FastAPI-wizard op 127.0.0.1:8765 en blokkeren totdat
de gebruiker klaar is (of Ctrl-C indrukt).

Gedrag:
  - ROSA_DEV=1        → skip (the user's live daemon mag nooit
                         onverwacht een wizard starten).
  - config.yaml aanwezig → skip.
  - anders            → uvicorn in background thread + block op
                         `wait_until_finished()`.

Design keuzes:
  - Uvicorn draait in een thread (niet subprocess) zodat we
    dezelfde Python-proces zijn — geen race op de wizard-state.
  - We openen NIET automatisch een browser: sommige gebruikers
    draaien Rosa headless via SSH.  We printen de URL groot in de
    terminal en laten de gebruiker klikken.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _dev_mode() -> bool:
    return os.environ.get("ROSA_DEV", "").strip() in ("1", "true", "yes")


def _wizard_disabled() -> bool:
    """Escape-hatch voor packaging tests / CI die geen wizard moet triggeren."""
    return os.environ.get("ROSA_WIZARD_DISABLED", "").strip() in ("1", "true", "yes")


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _print_banner(url: str, home: Path) -> None:
    line = "=" * 70
    print(file=sys.stderr)
    print(line, file=sys.stderr)
    print("  Rosa needs a one-time setup.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Open this URL in a browser:  {url}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Config will be written to:   {home}/", file=sys.stderr)
    print("  This wizard is bound to localhost only. Press Ctrl-C to abort.",
          file=sys.stderr)
    print(line, file=sys.stderr)
    print(file=sys.stderr)


def _run_uvicorn(app, host: str, port: int, ready: threading.Event) -> None:
    import uvicorn
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Signal ready as soon as the server socket is up.
    orig_startup = server.startup
    async def _hooked_startup(*a, **kw):
        await orig_startup(*a, **kw)
        ready.set()
    server.startup = _hooked_startup

    try:
        server.run()
    except Exception:
        log.exception("wizard uvicorn crashed")
        ready.set()


def ensure_configured(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    wait_timeout: float | None = None,
) -> None:
    """Blocking. Returns pas als de daemon door mag met load_settings()."""
    if _wizard_disabled():
        log.info("bootstrap: wizard disabled via env, skipping")
        return
    if _dev_mode():
        log.info("bootstrap: ROSA_DEV=1, skipping wizard")
        return

    # Late imports: het is normaal in dev-mode dat FastAPI + uvicorn
    # niet geinstalleerd zijn (the user's setup gebruikt ze niet nodig).
    from core.config import get_rosa_home, is_configured
    if is_configured():
        return

    try:
        from wizard.server import (
            build_app,
            reset_finish_event,
            wait_until_finished,
        )
    except ImportError:
        log.exception("bootstrap: kon wizard-server niet importeren")
        raise SystemExit(
            "Rosa is not configured and the setup wizard could not be "
            "loaded (missing FastAPI/uvicorn). Please install requirements."
        )

    reset_finish_event()

    if not _port_is_free(host, port):
        raise SystemExit(
            f"Rosa wants to run the setup wizard on {host}:{port} but that "
            f"port is already in use. Free it, or set ROSA_DEV=1 if you "
            f"already have a config."
        )

    app = build_app()
    ready = threading.Event()
    t = threading.Thread(
        target=_run_uvicorn, args=(app, host, port, ready), daemon=True,
    )
    t.start()

    # Wacht tot de socket up is, of tot 5s zijn verstreken.
    ready.wait(timeout=5.0)
    time.sleep(0.1)  # net iets ademruimte voor de HTTP-loop

    _print_banner(f"http://{host}:{port}/", get_rosa_home())

    # Blokkeer tot user 'Finish setup' klikt (of Ctrl-C).
    try:
        finished = wait_until_finished(timeout=wait_timeout)
    except KeyboardInterrupt:
        raise SystemExit("Setup aborted by user.")

    if not finished:
        raise SystemExit("Setup wizard timed out before finishing.")

    log.info("bootstrap: wizard voltooid, resume normale startup")
    # Nette poging om de HTTP-server te stoppen zodat de port straks
    # weer vrij is als user Rosa herstart.
    time.sleep(0.2)
