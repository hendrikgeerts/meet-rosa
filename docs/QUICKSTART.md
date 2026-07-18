# Rosa in 5 minutes

The absolute minimum to get from clone to your first iMessage.

## Before you start

You need:
- A Mac (macOS 13+)
- [Homebrew](https://brew.sh)
- Python 3.12: `brew install python@3.12`
- An [Anthropic API key](https://console.anthropic.com/settings/keys)
  (~$5/mo usage)

Optional but nice: a Google account for Gmail + Calendar.

## Step 1 — Install

```bash
git clone <this-repo-url> ~/rosa
cd ~/rosa
./install.sh
```

Grab a coffee — `install.sh` downloads Ollama models (~5 GB). This
runs once.

## Step 2 — First launch

```bash
PYTHONPATH=src ~/Library/Application\ Support/Rosa/venv/bin/python src/main.py
```

Your terminal will print:

```
======================================================================
  Rosa needs a one-time setup.

  Open this URL in a browser:  http://127.0.0.1:8765/

  Config will be written to:   ~/Library/Application Support/Rosa/
  This wizard is bound to localhost only. Press Ctrl-C to abort.
======================================================================
```

## Step 3 — The wizard

Open `http://127.0.0.1:8765/` in your browser. Fifteen steps, only
four are required:

1. **Welcome** — check the consent box.
2. **Identity** — your name, timezone, language.
3. **Anthropic** — paste your API key (starts with `sk-ant-`).
4. **iMessage** — the phone number or Apple ID iMessage uses when
   *you* send messages.
5-14. **Optional integrations** — connect Google, Slack, Todoist,
      etc. Skip anything you don't want yet.
15. **Confirm** — Rosa runs integration checks against everything
    you configured. Look for green checkmarks; yellow warnings are
    OK if you already know they'll fail (e.g. Ollama not started
    yet).

Click **Finish setup**.

## Step 4 — Your first iMessage from Rosa

Rosa launches automatically after "Finish setup". Within ~10 seconds
you should get an iMessage:

```
Hi <you> — Rosa here. I'm up and running.

A few quick things you can ask me:
  • 'help' — see what I can do
  • 'status' — check I'm still alive
  • 'test' — send me a short test scenario

Morning briefing lands in your inbox tomorrow at your configured time.
```

If you don't get the message within a minute:

1. First-time only, macOS asks for **Full Disk Access**. Grant it in
   System Settings → Privacy & Security → Full Disk Access — toggle
   on Python (or Terminal). Restart Rosa: `python src/main.py`.
2. Also first-time only, macOS asks for **Automation → Messages**.
   Approve it. This lets Rosa send iMessages.
3. Still nothing? Run `rosa doctor` in a fresh terminal — it'll
   diagnose the issue.

## Step 5 — Try a few commands

Text Rosa these on iMessage:

- `help` → what Rosa can do
- `status` → is she alive?
- `test` → a 3-step self-test

For a real task:

- `remind me to take out the trash in 5 minutes`
- `what's on my calendar today?` (needs Google connected)
- `who am I still owing a reply to?` (needs mail connected)

## Step 6 — Run 24/7

By default Rosa quits when you close the terminal. To keep her
running:

```bash
./scripts/install_launchagent.sh
```

This registers her as a macOS LaunchAgent — auto-starts on login,
restarts on crash. Logs live in `~/Library/Application Support/Rosa/logs/`.

## Done!

That's it. Rosa will now:

- Send you a morning briefing at 07:00 (change in wizard's
  "Notifications" step)
- Reply to any iMessage you send her
- Watch your inbox / calendar / Slack (whichever you connected)
- Nudge you on open threads

If something breaks: `rosa doctor`.

If you want to see more: [README.md](../README.md).
