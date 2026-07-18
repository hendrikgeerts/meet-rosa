# ADR 0005: Wizard writes both `config.yaml` and per-feature YAMLs

**Status:** Accepted

## Context

Rosa's extensions historically read config from per-feature YAMLs:

- `config/vip_contacts.yaml` — the VIP list
- `config/uptime.yaml` — URLs to monitor
- `config/confidential_domains.yaml` — routing rules
- `config/imap_accounts.yaml` — IMAP connection info

Each loader is stable, tested, and used from multiple call-sites
(scheduler, orchestrator, tool handlers).

The wizard, on the other hand, works with a single `config.yaml`
that captures everything the user answered. Two problems:

1. If the wizard *only* writes `config.yaml`, the existing extension
   loaders never see the user's data — they still look at
   per-feature files that don't exist.
2. If we rewrite every extension loader to read from `config.yaml`,
   we break the user's dev-mode setup and add a lot of change surface.

## Decision

The wizard writes **both**:

- `ROSA_HOME/config.yaml` — the authoritative "what did the user
  answer" record. This is what `load_settings()` reads.
- `ROSA_HOME/config/<feature>.yaml` — one file per feature, in the
  format the existing loader expects. Written via
  `src/wizard/adapters.py`.

Adapters are one-way: wizard payload → YAML on disk. There is no
reverse path. If a user edits a per-feature YAML directly, the next
time they re-run the wizard step, the wizard overwrites their edit.

## Consequences

**Easier:**
- Existing extension code doesn't change — the loaders find their
  files at the same paths as before
- the user's dev-mode setup continues to work unchanged
- The wizard's UI can capture data in whatever format is convenient,
  and the adapter converts to the canonical file format

**Harder:**
- Two representations of "the same" data — `config.yaml` blocks
  vs. per-feature YAMLs. Divergence is possible.
- If someone edits a per-feature YAML by hand, the wizard doesn't
  round-trip that edit back into `config.yaml`. Documented in the
  wizard help-text.
- Adapters are a new module to keep tested (they are — see
  `tests/test_wizard_adapters.py`)

**Regret risks:**
- Long-term we may want to invert: extensions read from the
  canonical `config.yaml` block via a common loader, and the
  per-feature YAML files become a compatibility shim. That's a
  larger refactor to be done when it's cheap.
