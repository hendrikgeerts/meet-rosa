#!/usr/bin/env bash
# Local security-audit script for pa-agent.
#
# Replays the checks from the ISO_AUDIT_2026-05 baseline as a single
# command so you (or CI later) can verify in one go that:
#   - no known CVEs in pinned deps        (via pip-audit)
#   - sensitive files are mode 0600        (yaml configs, .env, db, audit)
#   - sensitive dirs are mode 0700         (data/, data/audit/)
#   - no traceback leaks in tool layer     (defensive grep)
#   - no plaintext-secret grep hits        (defensive grep)
#   - FileVault status is reported         (operator visibility)
#   - Time Machine destination configured  (operator visibility)
#
# Exit code:
#   0 — all checks pass
#   1 — at least one finding (per-check output explains)
#
# Usage:
#   ./scripts/security_audit.sh            # one-shot
#   ./scripts/security_audit.sh --quiet    # only print failures + summary
#
# This is a *local* audit. It does not replace an external review and it
# does not phone home. All output goes to stdout.

set -u
# Do NOT set -e — we want to keep running every check, then summarise.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 2

QUIET=0
if [[ "${1:-}" == "--quiet" ]]; then QUIET=1; fi

PASS=0
FAIL=0
WARN=0

green()  { printf "\033[32m%s\033[0m" "$1"; }
red()    { printf "\033[31m%s\033[0m" "$1"; }
yellow() { printf "\033[33m%s\033[0m" "$1"; }

ok()   { (( QUIET )) || echo "  $(green '✓') $1";  PASS=$((PASS+1)); }
bad()  {                  echo "  $(red   '✗') $1";  FAIL=$((FAIL+1)); }
warn() {                  echo "  $(yellow '!') $1"; WARN=$((WARN+1)); }

section() { (( QUIET )) || echo; (( QUIET )) || echo "▸ $1"; }

# ---------------------------------------------------------------------------
section "Dependency CVEs (pip-audit)"

if [[ ! -x "./venv/bin/pip-audit" ]]; then
  warn "pip-audit not installed in ./venv — skipping (run: ./venv/bin/pip install pip-audit)"
else
  audit_out=$(./venv/bin/pip-audit 2>&1)
  if echo "$audit_out" | grep -q "No known vulnerabilities found"; then
    ok "pip-audit clean"
  else
    bad "pip-audit reports vulnerabilities:"
    echo "$audit_out" | sed 's/^/      /'
  fi
fi

# ---------------------------------------------------------------------------
section "File modes (sensitive files must be 0600)"

check_mode_file() {
  local path="$1" expected="$2"
  if [[ ! -e "$path" ]]; then
    (( QUIET )) || warn "$path — not present (skip)"
    return
  fi
  local actual
  actual=$(stat -f "%Lp" "$path" 2>/dev/null || stat -c "%a" "$path" 2>/dev/null)
  if [[ "$actual" == "$expected" ]]; then
    ok "$path mode=$actual"
  else
    bad "$path mode=$actual (expected $expected)"
  fi
}

check_mode_file .env 600
check_mode_file secrets.env 600
check_mode_file google_credentials.json 600
check_mode_file config/vip_contacts.yaml 600
check_mode_file config/morning_extras.yaml 600
check_mode_file config/confidential_domains.yaml 600
check_mode_file config/imap_accounts.yaml 600
check_mode_file config/slack_workspaces.yaml 600
check_mode_file config/uptime.yaml 600
check_mode_file data/memory.db 600
check_mode_file data/google_token.json 600

# ---------------------------------------------------------------------------
section "Directory modes (sensitive dirs must be 0700)"

check_mode_dir() {
  local path="$1" expected="$2"
  if [[ ! -d "$path" ]]; then
    (( QUIET )) || warn "$path — not present (skip)"
    return
  fi
  local actual
  actual=$(stat -f "%Lp" "$path" 2>/dev/null || stat -c "%a" "$path" 2>/dev/null)
  if [[ "$actual" == "$expected" ]]; then
    ok "$path mode=$actual"
  else
    bad "$path mode=$actual (expected $expected)"
  fi
}

check_mode_dir data 700
check_mode_dir data/audit 700
check_mode_dir data/logs 700

# ---------------------------------------------------------------------------
section "Audit-log birth mode (newest egress file)"

newest_egress=$(ls -t data/audit/egress-*.jsonl 2>/dev/null | head -1 || true)
if [[ -n "$newest_egress" ]]; then
  check_mode_file "$newest_egress" 600
else
  warn "no egress-*.jsonl found yet"
fi

# ---------------------------------------------------------------------------
section "Defensive code-grep (no regressions)"

if grep -rn "traceback.format_exc" src/ 2>/dev/null | grep -v "log_scrub.py" >/dev/null; then
  bad "traceback.format_exc() in src/ — tracebacks may leak to Claude / iMessage"
  grep -rn "traceback.format_exc" src/ | sed 's/^/      /'
else
  ok "no traceback.format_exc leaks in src/"
fi

# Direct Claude/anthropic imports outside privacy/
forbidden=$(grep -rln "from anthropic\|^import anthropic" src/ 2>/dev/null \
              | grep -v "privacy/gateway.py\|models/claude.py" || true)
if [[ -n "$forbidden" ]]; then
  bad "anthropic SDK imported outside the gateway chokepoint:"
  echo "$forbidden" | sed 's/^/      /'
else
  ok "anthropic SDK isolated to privacy/gateway.py + models/claude.py"
fi

# Plain `print(token)` / debug leaks
if grep -rn "print.*api_key\|print.*token" src/ 2>/dev/null | grep -v "test_\|.example.\|password=" >/dev/null; then
  bad "possible secret print() in src/"
  grep -rn "print.*api_key\|print.*token" src/ | grep -v "test_" | sed 's/^/      /'
else
  ok "no print(api_key|token) debug leaks"
fi

# ---------------------------------------------------------------------------
section "Test suite (sanity — must be green to publish security claims)"

if PYTHONPATH=src ./venv/bin/python -m pytest tests/ --tb=no -q >/tmp/pa_audit_pytest.log 2>&1; then
  passed=$(grep -oE '[0-9]+ passed' /tmp/pa_audit_pytest.log | tail -1)
  ok "pytest: ${passed:-all tests green}"
else
  bad "pytest FAILED — see /tmp/pa_audit_pytest.log"
  tail -5 /tmp/pa_audit_pytest.log | sed 's/^/      /'
fi

# ---------------------------------------------------------------------------
section "Host-level posture (operator visibility — informational)"

if command -v fdesetup >/dev/null 2>&1; then
  fv_status=$(fdesetup status 2>/dev/null | head -1)
  if echo "$fv_status" | grep -q "FileVault is On"; then
    ok "$fv_status"
  else
    bad "$fv_status — laptop disk is not encrypted (ISO A.10)"
  fi
fi

if command -v tmutil >/dev/null 2>&1; then
  tm_status=$(tmutil destinationinfo 2>&1 | head -1)
  if echo "$tm_status" | grep -qi "no destinations"; then
    bad "Time Machine: no destination configured (ISO A.17)"
  else
    ok "Time Machine destination present"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "─────────────────────────────────────────────"
echo "  $(green "$PASS pass")    $(red "$FAIL fail")    $(yellow "$WARN warn")"
echo "─────────────────────────────────────────────"

if (( FAIL > 0 )); then exit 1; fi
exit 0
