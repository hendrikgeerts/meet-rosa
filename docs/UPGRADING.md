# Upgrading a legacy install

If you're running Rosa from a pre-generic-MVP checkout (installed with
`ROSA_DEV=1` in `~/pa-agent/`), you can pull in the public codebase
without disrupting your setup.

## The setup

Your legacy install has:

- Code + config + data all in the repo (`~/pa-agent/`)
- `ROSA_DEV=1` in the LaunchAgent environment
- Config at `config/settings.yaml`, data at `data/`

The public repo has:

- Code-only in the repo
- Config/data in `~/Library/Application Support/Rosa/`
- No `ROSA_DEV=1` needed for new installs

Because we designed the migration path in M4/M5, your legacy install
continues to work: `get_rosa_home()` returns the repo dir when
`ROSA_DEV=1` OR when it sees `config/settings.yaml` in the repo.

## Pulling the new code

```bash
cd ~/pa-agent

# Add the public repo as upstream
git remote add upstream https://github.com/hendrikgeerts/meet-rosa.git

# Fetch the latest
git fetch upstream

# Merge it into your local main (or rebase — your call)
git merge upstream/main

# Install any new dependencies
./venv/bin/pip install -r requirements.txt

# Restart the daemon
launchctl kickstart -k gui/$(id -u)/com.hendrik.pa-agent
```

## Handling merge conflicts

**Conflicts we expect:**

- `main.py` if you have local hotfixes not yet upstreamed
- `docs/STATUS.md`, `docs/CHANGELOG.md` — the public versions are
  minimal; feel free to overwrite with `git checkout --theirs`

**Files that won't conflict** (in `.gitignore` on both sides):

- `secrets.env`, `config/*.yaml` (except `.example.yaml`)
- `data/`, `logs/`, `audit/`
- `.env`, `.wizard_state.json`

## New config keys

New features are behind feature-flags — nothing is default-on for you.
The keys you can optionally add to `config/settings.yaml`:

```yaml
# Slack bidirectional bot (see docs/BYOC.md for Slack app setup)
features:
  slack_bidirectional: false     # default off — leave it as-is

# Where should proactive messages (briefings, day-close, reminders) go?
user:
  main_channel: "imessage"       # or "slack" if you enable the bot
```

And new secrets (in `secrets.env`) — only add if you want them:

```
# Slack bidirectional bot
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_OWNER_USER_ID=U0123456789

# Optional: monthly Claude budget cap (0 = disabled)
# (also settable via config.yaml → privacy.monthly_anthropic_budget_usd)
```

## Trying the new CLI

The public repo ships a `rosa` CLI — you can install it separately:

```bash
ln -sf ~/pa-agent/scripts/rosa /usr/local/bin/rosa

rosa doctor       # diagnostic dump
rosa cost         # Anthropic month-to-date spend
rosa settings     # open the wizard in edit-mode in your browser
rosa reload       # SIGHUP the running daemon after config edits
rosa backup       # tar.gz snapshot
```

None of these will disturb your existing setup.

## Verifying

```bash
# Should show your existing config, NOT wizard-defaults:
rosa doctor | grep "user_name"

# Tests still pass:
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q
```

## Rolling back

If anything breaks:

```bash
cd ~/pa-agent
git reset --hard <sha-before-merge>
launchctl kickstart -k gui/$(id -u)/com.hendrik.pa-agent
```

Your data at `~/pa-agent/data/` is unaffected by code rollbacks —
schema migrations are all additive.
