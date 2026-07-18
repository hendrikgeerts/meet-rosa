#!/usr/bin/env bash
# Install Rosa as a LaunchAgent so she starts on login and stays up 24/7.
#
# Idempotent — safe to re-run. Uses the template at scripts/rosa.plist.template
# and substitutes {{PYTHON}}, {{REPO_DIR}}, {{ROSA_HOME}}.

set -euo pipefail

ROSA_HOME="${ROSA_HOME:-$HOME/Library/Application Support/Rosa}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROSA_HOME/venv/bin/python}"
LABEL="com.rosa.pa-agent"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$REPO_DIR/scripts/rosa.plist.template"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template not found at $TEMPLATE" >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "error: python not executable at $PYTHON" >&2
  echo "hint: run ./install.sh first to create the venv." >&2
  exit 1
fi

mkdir -p "$ROSA_HOME/logs"
mkdir -p "$HOME/Library/LaunchAgents"

# If already loaded, unload first so we install a clean copy.
if launchctl list | grep -q "$LABEL"; then
  echo "→ unloading existing $LABEL"
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

echo "→ writing $PLIST_DEST"
sed \
  -e "s|{{PYTHON}}|$PYTHON|g" \
  -e "s|{{REPO_DIR}}|$REPO_DIR|g" \
  -e "s|{{ROSA_HOME}}|$ROSA_HOME|g" \
  "$TEMPLATE" > "$PLIST_DEST"

chmod 0644 "$PLIST_DEST"

echo "→ loading $LABEL"
launchctl load "$PLIST_DEST"

sleep 1
if launchctl list | grep -q "$LABEL"; then
  echo "✓ Rosa LaunchAgent installed and running."
  echo "  logs:    $ROSA_HOME/logs/"
  echo "  stop:    launchctl unload $PLIST_DEST"
  echo "  status:  launchctl list | grep $LABEL"
else
  echo "! LaunchAgent load reported OK but not listed; check Console.app for errors." >&2
  exit 1
fi
