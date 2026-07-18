# Installing Rosa

Rosa is a privacy-first personal assistant for macOS. Everything runs on
your Mac. She only reaches out to third-party APIs (Anthropic, Google,
Slack, Todoist) when you connect them yourself through the setup wizard.

## What you need before you start

| Requirement | How to check | Install |
|---|---|---|
| macOS 13 or newer | `sw_vers -productVersion` | — |
| Homebrew | `brew --version` | https://brew.sh |
| Python 3.12 | `python3.12 --version` | `brew install python@3.12` |
| Git | `git --version` | `xcode-select --install` |
| An Anthropic API key | — | https://console.anthropic.com/settings/keys |

Optional but recommended:

- ~15 GB free disk space (Ollama models are large).
- A Gmail account, if you want Rosa to read your mail.
- A Todoist account, if you want Rosa to sync tasks.

Cost estimate: expect to spend **$5–15 per month** on your Anthropic
API key. There is no other subscription. Rosa herself is free.

## Install

```bash
git clone <this repo> ~/pa-agent
cd ~/pa-agent
./install.sh
```

`install.sh` does the following, and only the following:

1. Verifies macOS + Python 3.12 + Homebrew + git are present.
2. Creates `~/Library/Application Support/Rosa/` (chmod 700).
3. Creates a Python virtual environment inside it.
4. Installs `requirements.txt` into that venv.
5. Installs Ollama via Homebrew (if not already installed).
6. Pulls the default local models: `llama3.1:8b-instruct-q4_K_M`,
   `phi3:mini`, `nomic-embed-text`.
7. Copies the config template.

It does **not** ask for any personal data. All configuration (API keys,
your iMessage handle, feature toggles) happens in the browser-based
wizard on the very first launch.

## First launch — the setup wizard

```bash
cd ~/pa-agent
PYTHONPATH=src ~/Library/Application\ Support/Rosa/venv/bin/python src/main.py
```

Rosa detects that `~/Library/Application Support/Rosa/config.yaml`
doesn't exist yet and opens a wizard at:

    http://localhost:8765/

The wizard is bound to `127.0.0.1` — only your Mac can reach it. It
walks you through:

- **Welcome & consent** — one checkbox confirming you understand keys
  live locally.
- **Identity** — your name, timezone, language, home city. Used so
  Rosa addresses you correctly and doesn't redact your own city name
  from her notes.
- **Anthropic API key** — pasted into `secrets.env` (chmod 0600);
  never sent anywhere except `api.anthropic.com`.
- **iMessage** — the phone number or Apple ID that iMessage uses when
  *you* message. Rosa treats messages from this handle as "from you".
- **Optional integrations** — Google, IMAP, Slack, Todoist, Plaud,
  VIPs, uptime monitor, news feeds, notifications, confidential
  domains, feature toggles. You can skip any of these and connect
  them later.
- **Confirm** — review and finish.

When you press **Finish setup**, the wizard closes itself and Rosa
starts up. She will iMessage you shortly after.

## Where things live

    ~/Library/Application Support/Rosa/
    ├── config.yaml            # what you configured via the wizard
    ├── secrets.env            # API keys (chmod 0600)
    ├── venv/                  # Python virtualenv
    ├── data/                  # SQLite database + vectors
    ├── logs/                  # daily log rotation
    ├── audit/                 # egress audit trail (JSONL)
    └── .wizard_state.json     # wizard progress (chmod 0600)

## Running Rosa as a background daemon

To keep Rosa running 24/7 (auto-start on login, auto-restart on crash),
install her as a macOS LaunchAgent:

```bash
./scripts/install_launchagent.sh
```

The script substitutes your paths into `scripts/rosa.plist.template`,
writes it to `~/Library/LaunchAgents/com.rosa.pa-agent.plist`, and
loads it. Logs land in `~/Library/Application Support/Rosa/logs/`.

To stop:

```bash
launchctl unload ~/Library/LaunchAgents/com.rosa.pa-agent.plist
```

Re-run `install_launchagent.sh` any time to reinstall (it's idempotent).

## macOS permissions Rosa needs

You'll be asked for these on first use:

- **Full Disk Access** — required to read
  `~/Library/Messages/chat.db` (the iMessage source of truth). Grant
  via System Settings → Privacy & Security → Full Disk Access.
- **Automation → Messages** — required to *send* iMessages. Grant via
  System Settings → Privacy & Security → Automation, then approve the
  prompt on the first outgoing message.
- **Microphone (optional)** — only if you enable voice-in later.

## Reset / uninstall

Delete `~/Library/Application Support/Rosa/`. That's it — Rosa keeps
nothing outside that directory. Your models remain in `~/.ollama/`.

## Troubleshooting

**Wizard shows "port already in use"**
Something else is on port 8765. Either free it, or:
```bash
lsof -i :8765
```
to identify the culprit.

**"Rosa is not configured and the wizard could not be loaded"**
The venv is missing FastAPI / uvicorn. Re-run `install.sh`.

**Wizard opens but I can't type**
Check that the URL is exactly `http://localhost:8765/` — not `https`,
not a different port. Rosa binds only to loopback.

**I want to change something after setup**
Edit `config.yaml` directly. For secrets, re-run the wizard by
deleting `.wizard_state.json`.
