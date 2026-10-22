#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
OUTPUT_ROOT="${1:-$ROOT/dist}"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/feishu-codex-assistant.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
PACKAGE="$STAGING/Feishu-Codex-Assistant-Windows"
ZIP="$OUTPUT_ROOT/Feishu-Codex-Assistant-Windows.zip"

mkdir -p "$OUTPUT_ROOT"
mkdir -p "$PACKAGE/app"
cp "$ROOT"/bridge.py "$PACKAGE/app/"
cp "$ROOT"/codex_adapter.py "$PACKAGE/app/"
cp "$ROOT"/feishu_client.py "$PACKAGE/app/"
cp "$ROOT"/handoff_config.py "$PACKAGE/app/"
cp "$ROOT"/record_store.py "$PACKAGE/app/"
cp "$ROOT"/sdk_adapter.py "$PACKAGE/app/"
cp "$ROOT"/windows/Install-Feishu-Codex.cmd "$PACKAGE/"
cp "$ROOT"/windows/Open-Setup.cmd "$PACKAGE/"
cp "$ROOT"/windows/install.ps1 "$PACKAGE/"
cp "$ROOT"/windows/setup_gui.py "$PACKAGE/"
cp "$ROOT"/windows/run_bridge.vbs "$PACKAGE/"
cp "$ROOT"/windows/uninstall.ps1 "$PACKAGE/"
cp "$ROOT"/windows/README.md "$PACKAGE/"
cp "$ROOT"/windows/VALIDATION.md "$PACKAGE/"

rm -f "$ZIP"
(
  cd "$STAGING"
  zip -qry "$(basename "$ZIP")" "$(basename "$PACKAGE")"
)
mv "$STAGING/$(basename "$ZIP")" "$ZIP"

print "$ZIP"
