# core

## Doel
Het kernfundament dat de hele agent draaiend houdt: configuratie, persistente
state, orchestratie van Claude-conversaties met tool-use, scheduling van
proactieve taken, en het audit-spoor van externe calls.

`core/` mag níet importeren uit `extensions/` of `integrations/` — die
registreren zich bij core, niet andersom. (Vandaag importeert `briefings.py`
nog wel `extensions.reminders` en `integrations.gmail/gcal`; de plugin-laag
wordt later schoongetrokken — dit is bewuste tech-debt voor nu.)

## Modules
- `config.py` — `Settings` dataclass + `load_settings()` (YAML + .env)
- `db.py` — SQLite schema en helpers (processed_messages, conversation_turns,
  outgoing_queue)
- `orchestrator.py` — `converse()` driver: loopt Claude met tool-use tot
  `stop_reason != "tool_use"`
- `tools.py` — Anthropic-format schemas + `ToolExecutor` (Gmail, Calendar,
  Reminders, Plaud, get_current_time)
- `scheduler.py` — `Scheduler` thread: vuurt reminders, ochtendbriefing,
  Plaud-inbox-scan
- `briefings.py` — `generate_briefing()` voor de dagelijkse iMessage-briefing
- `audit.py` — JSONL egress-logger, één bestand per dag, *content-vrij*

## Public interface
- `from core.config import load_settings, Settings`
- `from core.audit import AuditLogger`
- `from core.scheduler import Scheduler`
- `from core import db, orchestrator`
- `from core.tools import ToolContext, ToolExecutor`

## Config-keys
Alles dat naar `core/` doorvloeit komt uit `Settings`. Zie
`config/settings.example.yaml` voor de volledige lijst.

## Privacy-implicaties
- `core/` zelf doet geen externe calls.
- `orchestrator.py` en `briefings.py` bellen alléén via
  `privacy.gateway.Gateway` — direct gebruik van `models.claude` is hier
  verboden en wordt door commits in `privacy.gateway` afgedwongen.
- `audit.py` schrijft naar `data/audit/egress-YYYY-MM-DD.jsonl` en bevat
  per definitie nooit prompt-, message- of tool-content.

## Testscenario's
- `tests/test_audit.py` — JSONL roundtrip, daily rotation, payload-leak guard.
- Orchestrator + scheduler hebben (nog) geen unit-tests; integratie-tests
  met fake-gateway volgen wanneer de privacy-laag uitgebreid wordt.
