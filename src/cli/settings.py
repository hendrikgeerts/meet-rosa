"""`rosa settings` — open the wizard-server in edit-mode in a browser.

Als Rosa's daemon draait pakt de daemon een SIGHUP na save zodat
config-changes zonder restart worden opgepikt (voor de fields die
hot-reload'baar zijn — zie main.py's reload block).

Usage:
    rosa settings                # open http://127.0.0.1:8765/?mode=edit
    rosa settings --port 8770    # andere port
    rosa settings --no-browser   # print URL, don't open
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser


def _find_free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            # find any free port
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa settings", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    port = _find_free_port(args.port)
    if port != args.port:
        print(f"note: port {args.port} busy, using {port}")

    # Import late so --help is snappy.
    import uvicorn
    from wizard.server import build_app, _SESSION_TOKEN
    app = build_app()

    url = f"http://{args.host}:{port}/?mode=edit"
    print(f"Settings UI: {url}")
    print("Ctrl-C to close when you're done.")

    # M-7: skip browser-open op remote SSH sessies — daar opent hij VM-
    # side (of faalt stil), en de user verwacht dat 't op zijn Mac
    # gebeurt via een SSH-tunnel. Detect via SSH_CONNECTION env-var.
    import os as _os
    is_ssh = bool(_os.environ.get("SSH_CONNECTION"))
    if is_ssh:
        print("\nRemote SSH session detected — not opening browser.")
        print("From your local machine, SSH-tunnel + open the URL:")
        print(f"  ssh -L {port}:localhost:{port} <this-host>")
        print(f"  # then browse http://localhost:{port}/?mode=edit")
    elif not args.no_browser:
        def _open():
            time.sleep(0.7)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    config = uvicorn.Config(
        app, host=args.host, port=port, log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
