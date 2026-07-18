# Rosa — Privacy-first Personal AI Assistant

Rosa is a personal AI assistant that lives on your Mac. She reads
your mail and calendar, watches recordings from your voice recorder,
keeps track of what matters, and talks to you over iMessage — all
without a cloud subscription and without your data leaving your
hardware unless you explicitly connect a third-party service.

- **Bring your own keys** — Anthropic API, Google OAuth, Slack,
  Todoist, IMAP: you configure each one, Rosa uses only what you enable.
- **Local by default** — sensitive content stays on your Mac. The
  privacy gateway is the single chokepoint that talks to Claude, and
  it redacts names, emails, and confidential-domain mail *before*
  they leave your device.
- **iMessage-native UI** — no separate app, no browser tab that needs
  to stay open. Rosa is a background daemon; you talk to her in the
  Messages app.
- **One installer, one wizard, one config file** — no YAML
  archaeology.

Built for entrepreneurs and knowledge workers who want a serious 24/7
assistant without renting one from OpenAI/Google/etc.

---

## Requirements

- macOS 13 or newer
- Homebrew — [brew.sh](https://brew.sh)
- Python 3.12 — `brew install python@3.12`
- git — comes with Xcode Command Line Tools
- An [Anthropic API key](https://console.anthropic.com/settings/keys)
  (expect $5–15/month usage)
- ~15 GB free disk space for local models

Optional (connect during setup, or later): a Google account (for Gmail
+ Calendar), a Slack workspace, a Todoist account, one or more IMAP
mailboxes, a Plaud voice recorder.

---

## Install

```bash
git clone <this-repo-url> ~/rosa
cd ~/rosa
./install.sh
```

`install.sh` verifies your prerequisites, creates a Python virtual
environment in `~/Library/Application Support/Rosa/venv/`, installs
dependencies, and pulls the local Ollama models. It **never** asks
for any personal data — that all happens in the setup wizard on
first launch.

## First run — the setup wizard

```bash
cd ~/rosa
PYTHONPATH=src ~/Library/Application\ Support/Rosa/venv/bin/python src/main.py
```

Rosa detects there is no `config.yaml` yet and opens a browser-based
wizard at:

```
http://localhost:8765/
```

The wizard walks you through 15 steps. Only four are required
(welcome, identity, Anthropic key, confirm); everything else is
skip-able and can be added later.

When you press *Finish setup*, Rosa starts as a background daemon and
iMessages you.

Full setup guide: **[docs/INSTALL.md](docs/INSTALL.md)**.
5-minute quick-start: **[docs/QUICKSTART.md](docs/QUICKSTART.md)**.
Something wrong? **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — or run `rosa doctor`.

## Running Rosa 24/7

```bash
./scripts/install_launchagent.sh
```

Registers Rosa as a macOS LaunchAgent — auto-starts on login,
restarts on crash. See `docs/INSTALL.md` for details.

---

## What Rosa can do

- **Morning briefing**, **midday heads-up**, **day-close** — three
  daily iMessages that summarise your inbox, calendar, and open loops
- **Weekly retrospective** (Saturday) and **weekend prep** (Sunday)
- **CEO-letter** (Friday) — a short weekly reflection on your business
- **Reminders + Todoist sync** — natural-language reminders that also
  land in Todoist
- **VIP-aware triage** — mails from your VIP contacts never slip
- **Communication intelligence** — cross-channel "who is waiting for
  what" across Mail + Slack + Plaud
- **Voice recorder integration** — drop Plaud recordings in a folder,
  Rosa transcribes + summarises + extracts action items
- **Uptime monitor** — watches URLs, pings you when they go down
- **Confidential-domain routing** — mail from domains you mark as
  sensitive is processed on your Mac only, never sent to Claude
- **Meeting prep** — 30 minutes before an external meeting, a concise
  brief on the attendees

All of these are behind feature flags in the wizard. Nothing runs
you didn't turn on.

---

## Architecture

```
Inputs (local)             Privacy layer          Reasoning          Output (local)
─────────────              ─────────────          ──────────         ─────────────
Gmail     ┐                Classifier             Local LLM   ┐      iMessage → 📱
Outlook   │                   ↓                   (Ollama)     │
IMAP      │─→ Local     ─→  Redactor       ─→                  │─→   Dashboard
Calendar  │   model            ↓                  Claude API   │
Plaud     │   (extract)     Gateway         ─→   (anonymised)  │
iMessage  │                   ↓                                 │
Voice     ┘                Reconstructor ←──────────────────── ┘
```

Everything stays on your hardware. The Claude API only ever sees
placeholders (`[PERSON_001]`, `[ORG_001]`), never real names or data.
Reconstruction to real values happens locally, after the response
comes back.

Deeper docs:

- [`docs/AGENT_SPEC.md`](docs/AGENT_SPEC.md) — full feature spec
- [`docs/HYBRID_ARCHITECTURE.md`](docs/HYBRID_ARCHITECTURE.md) — how
  local + Claude cooperate
- [`docs/PRIVACY_LAYER.md`](docs/PRIVACY_LAYER.md) — classification,
  routing, redaction, audit
- [`docs/AGENT_SPEC_EXTENSIONS.md`](docs/AGENT_SPEC_EXTENSIONS.md) —
  the optional extensions

---

## Where your data lives

Everything Rosa keeps ends up in a single directory you control:

```
~/Library/Application Support/Rosa/
├── config.yaml              # what you configured (managed by wizard)
├── config/                  # per-feature YAML files
├── secrets.env              # API keys (chmod 0600)
├── venv/                    # Python virtualenv
├── data/                    # SQLite database + audit logs
└── logs/                    # daily rotating logs
```

To reset: `rm -rf ~/Library/Application\ Support/Rosa`. That's it.

---

## Contributing

Rosa is a working system built for a single-user, self-hosted use
case. If you find something broken, open an issue with clear repro
steps. If you want to add a whole new integration, open an issue
first so we can talk about scope — the goal is to keep the core
readable, not to become LangChain.

Coding conventions live in [`CLAUDE.md`](CLAUDE.md) at the repo root
(that file is the source of truth for both humans and AI collaborators).

---

## License

See [`LICENSE`](LICENSE) at the repo root.
