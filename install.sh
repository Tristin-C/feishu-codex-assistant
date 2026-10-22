#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
CONFIG="${HANDOFF_CONFIG:-$ROOT/config.toml}"
LARK_CLI="${LARK_CLI:-$HOME/.local/bin/lark-cli}"

if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config.example.toml" "$CONFIG"
  chmod 600 "$CONFIG"
  print -u2 "Created $CONFIG. Fill in credentials, then rerun install.sh."
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  print -u2 "codex is not on PATH"
  exit 1
fi
if [[ ! -x "$LARK_CLI" ]]; then
  print -u2 "lark-cli not found at $LARK_CLI"
  exit 1
fi

chmod +x "$ROOT/handoff" "$ROOT/bridge.py" "$ROOT/codex_adapter.py"
mkdir -p "$ROOT/state" "$HOME/Library/LaunchAgents"
chmod 700 "$ROOT/state"

python3 - "$ROOT" "$CONFIG" "$LARK_CLI" <<'PY'
import os
import plistlib
import sys
from pathlib import Path
root = Path(sys.argv[1])
cfg = Path(sys.argv[2])
lark = Path(sys.argv[3])
label = "com.paige.handoff-bridge"
data = {
    "Label": label,
    "ProgramArguments": [
        str(root / "handoff"),
        "serve",
    ],
    "WorkingDirectory": str(root),
    "EnvironmentVariables": {
        "HANDOFF_CONFIG": str(cfg),
        "PATH": f"{Path.home() / '.local/bin'}:{Path.home() / '.homebrew/bin'}:/usr/local/bin:/usr/bin:/bin",
    },
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "StandardOutPath": str(root / "state" / "logs" / "launchd.out.log"),
    "StandardErrorPath": str(root / "state" / "logs" / "launchd.err.log"),
}
out = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("wb") as fh:
    plistlib.dump(data, fh)
print(out)
PY

print "Install complete."
print "Configure the bot long-connection in the Feishu console, then run:"
print "  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.paige.handoff-bridge.plist 2>/dev/null || true"
print "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.paige.handoff-bridge.plist"
print "  $ROOT/handoff list"

