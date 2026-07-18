"""Setup-wizard voor nieuwe Rosa-installaties.

Draait als kortstondige FastAPI-app op localhost:8765 tot alle
verplichte configuratie is ingevoerd. Daarna: schrijft config.yaml
+ secrets.env naar ROSA_HOME, sluit af, launcht de daemon.

Zie src/wizard/server.py voor de app-factory.
"""
from __future__ import annotations

__all__ = ["build_app", "WIZARD_PORT"]

WIZARD_PORT = 8765

from wizard.server import build_app  # noqa: E402
