# ADR 0002: `ROSA_HOME` as the single per-installation directory

**Status:** Accepted

## Context

Historically Rosa lived in one place: the repo directory. Config
files, database, secrets, and logs all next to the code. That
worked for a single developer but has three problems:

1. Cloning the repo doesn't destroy your data (`data/` is
   gitignored), but *moving* the repo does.
2. Multiple users on one Mac can't have separate installs.
3. A user who installs Rosa via a package manager expects data in
   `~/Library/Application Support/` — that's the macOS convention.

## Decision

There is one environment variable, `ROSA_HOME`, that resolves to the
per-installation directory. Priority order:

1. `ROSA_HOME` env-var if explicitly set
2. `ROSA_DEV=1` → `<repo-root>` (dev-mode backwards compat)
3. `<repo>/config/settings.yaml` exists → `<repo-root>` (implicit
   dev-mode; protects legacy installations)
4. `~/Library/Application Support/Rosa/` (new-user default)

Everything Rosa reads and writes for a given installation lives
under `ROSA_HOME`:

```
ROSA_HOME/
├── config.yaml
├── config/                    # per-feature YAMLs
├── secrets.env                # chmod 0600
├── venv/                      # Python virtualenv
├── data/                      # SQLite, vectors, audit
└── logs/
```

All Settings paths are resolved relative to `ROSA_HOME`, not relative
to the repo. `core.config._resolve()` implements this.

## Consequences

**Easier:**
- Moving the repo doesn't move your data
- A user can wipe their Rosa install with one `rm -rf` of a well-known
  directory
- Two users on one Mac can each set their own `ROSA_HOME`
- Package-manager installers (`brew install rosa`) become possible

**Harder:**
- Every path in `Settings` had to change from "relative to repo" to
  "relative to ROSA_HOME"
- Backups can no longer just tar the repo dir
- The wizard, adapters, and CLI all needed a helper (`get_rosa_home()`)

**Regret risks:**
- Two paths for logs (LaunchAgent stdout vs. app agent.log) used to
  be inconsistent — see ADR-fix in log_file default.
- Users who ran Rosa before this change may still have data in the
  repo dir; migration is manual (`mv <repo>/data ~/Library/Application\ Support/Rosa/`).
