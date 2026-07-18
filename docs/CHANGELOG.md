# Changelog

## 0.1.0 — Initial public release

First release of Rosa as a general-purpose personal AI assistant for
macOS. Everything ships behind a setup wizard and feature flags —
nothing runs that you didn't opt into.

### Added

- **Setup wizard** on `http://localhost:8765/` — browser-based,
  15 steps, only 4 required (welcome, identity, Anthropic key,
  confirm)
- **Bootstrap flow** — `main.py` detects missing config and starts
  the wizard automatically
- **LaunchAgent installer** (`scripts/install_launchagent.sh`) — run
  Rosa 24/7 with auto-restart
- **Google OAuth integration** via wizard — bring your own OAuth
  client; callback lands in the same wizard-server
- **Slack / Todoist / IMAP** integrations via token-based wizard steps
- **Optional integrations** as skippable wizard steps: Plaud voice
  recorder, VIP contacts, uptime monitor, news feeds, notifications,
  confidential-domain routing, feature flags
- **Privacy gateway** — single chokepoint that classifies, redacts,
  routes to local model or Claude, and audit-logs egress
- **Local-first models** via Ollama (Llama 3.1 8B main, Phi-3 mini
  fast, nomic-embed-text vectors)
- **iMessage bridge** for two-way conversation

### Documented

- `README.md` — introduction and quick-start
- `docs/INSTALL.md` — full setup guide
- `docs/AGENT_SPEC.md` — feature specification
- `docs/PRIVACY_LAYER.md` — classification, routing, redaction
- `docs/HYBRID_ARCHITECTURE.md` — how local + Claude cooperate
- `docs/AGENT_SPEC_EXTENSIONS.md` — optional extension list
- `docs/DESIGN.md` — house-style tokens for the wizard UI
- `docs/PUBLISH_CHECKLIST.md` — for maintainers publishing new versions

### Security / privacy

- All secrets (`.env`, `secrets.env`, `google_credentials.json`,
  `google_token.json`) in `.gitignore`
- Wizard binds to `127.0.0.1` only; session-token verification on
  every API call
- Google OAuth state-token bound to wizard session-token
- `secrets.env` written `chmod 0600`
- No telemetry, no external analytics, no third-party crash reporting

### Test coverage

1400+ tests including full end-to-end wizard flow, integration
against `load_settings()`, per-extension smoke tests, and regression
guards for the `ROSA_DEV=1` compatibility path.
