#!/usr/bin/env python3
"""Small TOML-subset loader; the project intentionally has no third-party deps."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # Sufficient for config strings; handles the standard JSON escapes.
        import json

        return json.loads(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def load_toml_subset(path: Path) -> Dict[str, Dict[str, Any]]:
    data: Dict[str, Dict[str, Any]] = {}
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            raise ValueError(f"invalid config line in {path}: {raw!r}")
        key, value = line.split("=", 1)
        data.setdefault(section, {})[key.strip()] = _parse_scalar(value)
    return data


@dataclass
class Config:
    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""
    allowed_open_ids: List[str] = field(default_factory=list)
    profile: str = "handoff-bridge"
    lark_cli: str = str(Path.home() / ".local/bin/lark-cli")
    send_enabled: bool = True
    state_dir: Path = ROOT / "state"
    socket_path: Path = ROOT / "state/bridge.sock"
    ipc_host: str = "127.0.0.1"
    ipc_port: int = 47653
    codex_bin: str = "codex"
    terminal_app: str = "Terminal"
    default_cwd: Path = Path.home()
    full_access: bool = True
    startup_timeout_secs: int = 30
    poll_secs: float = 1.0
    dedupe_window_secs: int = 600
    config_path: Path = DEFAULT_CONFIG

    @property
    def records_dir(self) -> Path:
        return self.state_dir / "records"

    @property
    def outbox_dir(self) -> Path:
        return self.state_dir / "outbox"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.state_dir, self.records_dir, self.outbox_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        config_path = Path(os.environ.get("HANDOFF_CONFIG", path or DEFAULT_CONFIG)).expanduser()
        raw = load_toml_subset(config_path)
        f = raw.get("feishu", {})
        b = raw.get("bridge", {})
        state = Path(str(b.get("state_dir", ROOT / "state"))).expanduser()
        socket = Path(str(b.get("socket_path", state / "bridge.sock"))).expanduser()
        return cls(
            app_id=str(f.get("app_id", "")),
            app_secret=str(f.get("app_secret", "")),
            chat_id=str(f.get("chat_id", "")),
            allowed_open_ids=list(f.get("allowed_open_ids", [])),
            profile=str(f.get("profile", "handoff-bridge")),
            lark_cli=str(f.get("lark_cli", Path.home() / ".local/bin/lark-cli")),
            send_enabled=bool(f.get("send_enabled", True)),
            state_dir=state,
            socket_path=socket,
            ipc_host=str(b.get("ipc_host", "127.0.0.1")),
            ipc_port=int(b.get("ipc_port", 47653)),
            codex_bin=str(b.get("codex_bin", "codex")),
            terminal_app=str(b.get("terminal_app", "Terminal")),
            default_cwd=Path(str(b.get("default_cwd", Path.home()))).expanduser(),
            full_access=bool(b.get("full_access", True)),
            startup_timeout_secs=int(b.get("startup_timeout_secs", 30)),
            poll_secs=float(b.get("poll_secs", 1)),
            dedupe_window_secs=int(b.get("dedupe_window_secs", 600)),
            config_path=config_path,
        )


def validate_config(cfg: Config) -> List[str]:
    errors: List[str] = []
    if cfg.send_enabled:
        if not cfg.app_id or not cfg.app_secret:
            errors.append("feishu app_id/app_secret are required when send_enabled=true")
        if not cfg.chat_id:
            errors.append("feishu chat_id is required when send_enabled=true")
    if not cfg.allowed_open_ids:
        errors.append("feishu allowed_open_ids must not be empty")
    if not cfg.socket_path.parent.exists():
        try:
            cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"cannot create socket directory: {exc}")
    return errors


def sanitize_task_name(text: str, limit: int = 60) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first = re.sub(r"^[#>*\-\s]+", "", first)
    return (first[:limit].rstrip() or "Codex task")

