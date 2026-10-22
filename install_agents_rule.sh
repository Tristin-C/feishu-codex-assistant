#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
AGENTS="${CODEX_HOME:-$HOME/.codex}/AGENTS.md"
mkdir -p "${AGENTS:h}"

python3 - "$AGENTS" "$ROOT/handoff" <<'PY'
import os
import shutil
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
handoff = Path(sys.argv[2])
marker_start = "<!-- handoff-bridge:start -->"
marker_end = "<!-- handoff-bridge:end -->"
block = f"""{marker_start}
## Feishu handoff bridge

- At the end of every turn where you did work for the user, call exactly once:
  `{handoff} send "<your final answer verbatim>"`
- Do not paraphrase the result, send a second summary, or send intermediate steps.
- The bridge attaches the task id, task name, and current Codex session id. A reply
  to the resulting Feishu message returns to that exact session.
- For a new task initiated by the user, do not choose terminal/background silently.
  Ask them to choose, then use:
  `{handoff} start terminal "<task>"`
  or `{handoff} start background "<task>"`.
{marker_end}
"""
old = path.read_text(encoding="utf-8") if path.exists() else ""
if marker_start in old and marker_end in old:
    before, rest = old.split(marker_start, 1)
    _, after = rest.split(marker_end, 1)
    new = before + block + after.lstrip("\n")
else:
    if path.exists():
        backup = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    new = old.rstrip() + ("\n\n" if old.strip() else "") + block
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(path.name + ".handoff.tmp")
tmp.write_text(new, encoding="utf-8")
os.replace(tmp, path)
print(f"updated {path}")
PY

