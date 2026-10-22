#!/bin/zsh
set -euo pipefail

if [[ $# -lt 4 ]]; then
  print -u2 "Usage: $0 <app_id> <app_secret> <open_id> <chat_id> [cwd] [profile]"
  exit 2
fi

ROOT="${0:A:h}"
APP_ID="$1"
APP_SECRET="$2"
OPEN_ID="$3"
CHAT_ID="$4"
WORKDIR="${5:-$HOME}"
PROFILE="${6:-handoff-bridge}"
CONFIG="$ROOT/config.toml"
LARK_CLI="${LARK_CLI:-$HOME/.local/bin/lark-cli}"

export HANDOFF_APP_SECRET="$APP_SECRET"
python3 - "$CONFIG" "$ROOT" "$APP_ID" "$OPEN_ID" "$CHAT_ID" "$PROFILE" "$LARK_CLI" "$WORKDIR" <<'PY'
import json
import os
import sys
from pathlib import Path
config, root, app_id, open_id, chat_id, profile, lark, workdir = sys.argv[1:]
app_secret = os.environ["HANDOFF_APP_SECRET"]
content = f'''[feishu]
app_id = {json.dumps(app_id)}
app_secret = {json.dumps(app_secret)}
chat_id = {json.dumps(chat_id)}
allowed_open_ids = [{json.dumps(open_id)}]
profile = {json.dumps(profile)}
lark_cli = {json.dumps(lark)}
send_enabled = true

[bridge]
state_dir = {json.dumps(str(Path(root) / "state"))}
socket_path = {json.dumps(str(Path(root) / "state" / "bridge.sock"))}
codex_bin = "codex"
terminal_app = "Terminal"
default_cwd = {json.dumps(os.path.expanduser(workdir))}
full_access = true
startup_timeout_secs = 30
poll_secs = 1
dedupe_window_secs = 600
'''
path = Path(config)
path.write_text(content, encoding="utf-8")
os.chmod(path, 0o600)
PY
unset HANDOFF_APP_SECRET

if [[ ! -x "$LARK_CLI" ]]; then
  print -u2 "lark-cli not found at $LARK_CLI"
  exit 1
fi

print -n "$APP_SECRET" | "$LARK_CLI" config init \
  --app-id "$APP_ID" --app-secret-stdin --name "$PROFILE" --force-init >/dev/null

chmod +x "$ROOT/handoff" "$ROOT/bridge.py" "$ROOT/codex_adapter.py"
"$ROOT/install.sh"

CC_CONFIG="$HOME/.cc-connect/config.toml"
if [[ -f "$CC_CONFIG" ]] && grep -q "app_id = \"$APP_ID\"" "$CC_CONFIG"; then
  CC_CONNECT="${CC_CONNECT:-$HOME/.local/bin/cc-connect}"
  if [[ -x "$CC_CONNECT" ]]; then
    "$CC_CONNECT" daemon stop >/dev/null 2>&1 || true
    print "Stopped cc-connect because it owns the same bot connection."
  fi
fi

if [[ "${HANDOFF_SKIP_AGENTS:-0}" != "1" ]]; then
  "$ROOT/install_agents_rule.sh"
fi

print "Configured robot $APP_ID (profile: $PROFILE) for chat $CHAT_ID."
print "Starting bridge..."
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.paige.handoff-bridge.plist" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.paige.handoff-bridge.plist"
print "Check with: $ROOT/handoff list"


