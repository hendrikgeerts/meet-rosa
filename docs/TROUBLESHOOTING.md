# Troubleshooting Rosa

If something isn't working, run this first:

```bash
rosa doctor
```

Paste the output into your bug-report. It contains everything a
maintainer needs to help you (no secrets are leaked — API keys are
masked).

Below are the failure-modes we've seen so far.

---

## Setup / first-run

### "Port already in use" when starting the wizard

```
Rosa wants to run the setup wizard on 127.0.0.1:8765 but that port
is already in use.
```

Something else is on 8765. Find it:

```bash
lsof -iTCP:8765 -sTCP:LISTEN
```

Kill it, or set `ROSA_WIZARD_DISABLED=1` and copy your config
manually to `~/Library/Application Support/Rosa/config.yaml`.

### Wizard opens but "Cannot reach api.anthropic.com"

Your Mac has no network to `api.anthropic.com`. Check corporate
firewall / VPN. Rosa needs outbound HTTPS 443 to:

- `api.anthropic.com` (required)
- `accounts.google.com` + `oauth2.googleapis.com` (if using Google)
- `slack.com` (if using Slack)
- `api.todoist.com` (if using Todoist)

### "Anthropic key rejected (401)"

The key you pasted is wrong or has been revoked. Generate a new one
at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

### "Ollama not reachable at http://localhost:11434"

Ollama isn't running:

```bash
ollama serve            # foreground
brew services start ollama   # background, auto-restart
```

If `ollama serve` says "port already in use", another Ollama is
running — check `ps aux | grep ollama`.

### "Ollama has no models pulled"

```bash
ollama pull llama3.1:8b-instruct-q4_K_M
ollama pull phi3:mini
ollama pull nomic-embed-text
```

The install script does this for you; if it failed (e.g. network
timeout), re-run manually.

---

## Google OAuth

### "Access blocked: your app hasn't completed the Google verification process"

You created an OAuth client in Google Cloud but didn't add yourself
as a test user. Go to your Cloud project → APIs & Services → OAuth
consent screen → Test users → add your Gmail address.

### "redirect_uri_mismatch"

The redirect URI you configured in Google Cloud doesn't match what
the wizard uses. The wizard shows the correct URI on the Google
step (something like `http://127.0.0.1:8765/oauth/google/callback`).
Copy that verbatim to your Google OAuth client's *Authorized redirect
URIs*.

### "Gmail API has not been used in project X before or it is disabled"

You forgot to enable Gmail (or Calendar) API for your Cloud project.
Go to APIs & Services → Library → search "Gmail API" → Enable.
Same for "Google Calendar API".

---

## iMessage

### Rosa doesn't respond to my messages

Three possible causes:

1. **Rosa isn't running**: `rosa doctor` → check "Rosa is up" line.
   If not: `python src/main.py` from the repo, or reload the
   LaunchAgent.
2. **Full Disk Access not granted**: `rosa doctor` will say so.
   Grant it in System Settings → Privacy & Security → Full Disk
   Access → toggle on the Python binary (or Terminal, if running
   from Terminal).
3. **Rosa doesn't recognise your handle**: your primary handle in
   the wizard doesn't match what iMessage sees. Check
   `secrets.env` → `OWNER_IMESSAGE_HANDLE`. It must be the exact
   string iMessage uses (`+31612345678` or `you@icloud.com`).

### Rosa's replies don't arrive on my phone

Rosa uses AppleScript to send via the Messages app on macOS. The
first outgoing message triggers an Automation permission prompt.
Approve it. If you missed it: System Settings → Privacy & Security
→ Automation → find Terminal/Python → check "Messages".

---

## Runtime

### Rosa crashes on start with "config.yaml missing"

You cleared config accidentally. Re-run the wizard:

```bash
rosa setup
python src/main.py
```

### Rosa runs but briefings are at the wrong time

Your timezone in `config.yaml` doesn't match your Mac's timezone.
Fix via the wizard (`rosa setup`) or edit `config.yaml` directly:

```yaml
user:
  timezone: "Europe/Amsterdam"     # IANA zone
```

### Rosa is slow / uses a lot of CPU

Ollama is running on CPU — that's normal on Intel Macs. Options:

- Switch main model to a smaller variant: edit `config.yaml`
  `runtime.local_model_main: "phi3:mini"` (much faster, less capable)
- Move to an Apple Silicon Mac (M-series) — Metal acceleration
  makes 5–10× difference

### "chat.db is locked"

Another process is exclusive-locking chat.db (rare). Restart Rosa:

```bash
launchctl unload ~/Library/LaunchAgents/com.rosa.pa-agent.plist
launchctl load   ~/Library/LaunchAgents/com.rosa.pa-agent.plist
```

---

## Data / backup

### I want to move Rosa to a new Mac

On the old Mac:

```bash
rosa backup                          # writes ~/rosa-backup-<ts>.tar.gz
```

Copy the tar.gz to the new Mac. Then:

```bash
git clone <repo> ~/rosa
cd ~/rosa
./install.sh
rosa restore ~/rosa-backup-<ts>.tar.gz
python src/main.py                   # boot
```

### I want to wipe and start over

```bash
rosa setup --reset       # wipes config + secrets; asks confirmation
# or, nuclear:
rm -rf ~/Library/Application\ Support/Rosa
```

---

## Still stuck?

Open an issue with `rosa doctor` output and the last 50 lines of
`~/Library/Application Support/Rosa/logs/agent.log`.
