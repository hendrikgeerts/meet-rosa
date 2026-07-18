#!/usr/bin/env bash
# Rosa installer — first-time setup on macOS.
#
# What this does (in order):
#   1. Verify prereqs (macOS 13+, Python 3.12, Homebrew, git).
#   2. Create ROSA_HOME (~/Library/Application Support/Rosa/) with subdirs.
#   3. Create a Python venv there and install requirements.
#   4. Install Ollama via Homebrew (if missing) and pull the default models.
#   5. Print how to start Rosa — which triggers the web-based wizard on
#      http://localhost:8765/ for the actual configuration (API keys,
#      iMessage handle, feature toggles).
#
# No API keys or personal data are asked for here — those come from the
# wizard so users can review each field and its explanation.

set -euo pipefail

ROSA_HOME="${ROSA_HOME:-$HOME/Library/Application Support/Rosa}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- pretty output ---------------------------------------------------------

C_BOLD="$(printf '\033[1m')"
C_DIM="$(printf '\033[2m')"
C_GREEN="$(printf '\033[32m')"
C_RED="$(printf '\033[31m')"
C_RESET="$(printf '\033[0m')"

say()  { printf "%s%s%s\n" "$C_BOLD" "$*" "$C_RESET"; }
ok()   { printf "  %s✓%s %s\n" "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf "  %s!%s %s\n" "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf "\n%s→ %s%s\n" "$C_BOLD" "$*" "$C_RESET"; }
dim()  { printf "%s%s%s\n" "$C_DIM" "$*" "$C_RESET"; }

# --- prereq checks ---------------------------------------------------------

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    warn "Missing '$1'. $2"
    exit 1
  fi
  ok "$1"
}

step "Checking prerequisites"

if [[ "$(uname -s)" != "Darwin" ]]; then
  warn "Rosa is macOS-only for now. Detected: $(uname -s)"
  exit 1
fi
ok "macOS $(sw_vers -productVersion)"

require git    "Install via Xcode Command Line Tools: xcode-select --install"
require brew   "Install Homebrew from https://brew.sh"

PY_BIN="${PY_BIN:-python3.12}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  warn "Python 3.12 not found. Install with: brew install python@3.12"
  exit 1
fi
PY_VERSION="$("$PY_BIN" --version 2>&1)"
ok "$PY_VERSION"

# --- ROSA_HOME setup -------------------------------------------------------

step "Preparing Rosa home directory"
dim "$ROSA_HOME"

mkdir -p "$ROSA_HOME"/{config,data,logs,audit}
chmod 700 "$ROSA_HOME"
ok "Created $ROSA_HOME (chmod 700)"

# --- Python venv + requirements --------------------------------------------

step "Creating Python virtual environment"
VENV="$ROSA_HOME/venv"
if [[ ! -d "$VENV" ]]; then
  "$PY_BIN" -m venv "$VENV"
  ok "venv → $VENV"
else
  ok "venv already exists → $VENV"
fi

step "Installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
ok "requirements.txt installed"

# --- Ollama + models -------------------------------------------------------

step "Checking Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  say "Ollama not found — installing via Homebrew…"
  brew install ollama
fi
ok "ollama $(ollama --version 2>&1 | head -1)"

# Start Ollama in background if not running.
if ! pgrep -x ollama >/dev/null 2>&1; then
  say "Starting Ollama in background…"
  ollama serve >/dev/null 2>&1 &
  sleep 2
fi

step "Pulling local models (this may take a while on first run)"
for MODEL in llama3.1:8b-instruct-q4_K_M phi3:mini nomic-embed-text; do
  say "→ pulling $MODEL"
  ollama pull "$MODEL" || warn "Failed to pull $MODEL — you can retry later"
done

# --- Copy example config ---------------------------------------------------

EXAMPLE_CFG="$REPO_DIR/config/config.example.yaml"
if [[ -f "$EXAMPLE_CFG" && ! -f "$ROSA_HOME/config.example.yaml" ]]; then
  cp "$EXAMPLE_CFG" "$ROSA_HOME/config.example.yaml"
  ok "Copied config example (wizard will write actual config.yaml)"
fi

# --- Install `rosa` CLI symlink -------------------------------------------

step "Installing 'rosa' CLI"
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
ln -sf "$REPO_DIR/scripts/rosa" "$LOCAL_BIN/rosa"
ok "Symlinked rosa → $LOCAL_BIN/rosa"

# Warn if $LOCAL_BIN isn't on PATH.
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
  warn "$LOCAL_BIN is not on your PATH."
  echo "  Add to ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# --- Done ------------------------------------------------------------------

step "Installation complete"

cat <<EOF

  Rosa is installed at:
    $ROSA_HOME

  Diagnose your install any time with:

    rosa doctor

  Start Rosa for the first time with:

    cd $REPO_DIR
    PYTHONPATH=src "$VENV/bin/python" src/main.py

  On first launch Rosa detects there is no config.yaml and opens a
  browser-based setup wizard at:

    ${C_BOLD}http://localhost:8765/${C_RESET}

  There you enter your Anthropic API key, iMessage handle, and toggle
  which integrations (Gmail, Slack, Todoist, …) you want.

  After the wizard finishes, Rosa starts as a background daemon and
  will iMessage you.

  Logs:     $ROSA_HOME/logs/
  Data:     $ROSA_HOME/data/
  Secrets:  $ROSA_HOME/secrets.env  (chmod 0600)

EOF
