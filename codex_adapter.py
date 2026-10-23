#!/usr/bin/env python3
"""Codex session discovery, terminal/background launch, and reply delivery."""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app_server_steer import (
    queued_item_ids,
    remove_queued_items,
    steer_active_turn,
    wait_for_queue_consumption,
)
from handoff_config import Config, sanitize_task_name


@dataclass
class DiscoveryResult:
    session_id: str
    rollout_path: Path
    cwd: str


class CodexAdapter:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _codex(self) -> str:
        return self.cfg.codex_bin

    @staticmethod
    def session_home() -> Path:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()

    def rollout_paths(self, codex_home: Path | None = None) -> Iterable[Path]:
        home = Path(codex_home or self.session_home()).expanduser()
        yield from home.glob("sessions/**/rollout-*.jsonl")

    def find_rollout(
        self, session_id: str, codex_home: Path | None = None
    ) -> Optional[Path]:
        if not session_id:
            return None
        for path in self.rollout_paths(codex_home):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    first = json.loads(fh.readline())
                payload = first.get("payload", {})
                if payload.get("session_id") == session_id or payload.get("id") == session_id:
                    return path
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def discover_session(
        self,
        cwd: str,
        since: float,
        codex_home: Path | None = None,
        require_tui: bool = False,
    ) -> Optional[DiscoveryResult]:
        candidates: List[DiscoveryResult] = []
        for path in self.rollout_paths(codex_home):
            try:
                if path.stat().st_mtime < since - 2:
                    continue
                with path.open("r", encoding="utf-8") as fh:
                    first = json.loads(fh.readline())
                payload = first.get("payload", {})
                if payload.get("cwd") != cwd:
                    continue
                if require_tui and payload.get("originator") != "codex-tui":
                    continue
                sid = payload.get("session_id") or payload.get("id")
                if sid:
                    candidates.append(DiscoveryResult(str(sid), path, cwd))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return max(candidates, key=lambda item: item.rollout_path.stat().st_mtime) if candidates else None

    def wait_for_session(
        self,
        cwd: str,
        since: float,
        timeout: int,
        codex_home: Path | None = None,
        require_tui: bool = False,
    ) -> Optional[DiscoveryResult]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.discover_session(cwd, since, codex_home, require_tui=require_tui)
            if found:
                return found
            time.sleep(1)
        return None

    def latest_user_text(self, rollout_path: Path) -> str:
        latest = ""
        try:
            with rollout_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = item.get("payload", {})
                    if (
                        item.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"
                    ):
                        texts = [
                            part.get("text", "")
                            for part in payload.get("content", [])
                            if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
                        ]
                        if texts:
                            latest = "\n".join(texts).strip()
        except OSError:
            pass
        return latest

    def read_new_completions(self, path: Path, offset: int) -> Tuple[int, List[str]]:
        messages: List[str] = []
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                while True:
                    start = fh.tell()
                    line = fh.readline()
                    if not line:
                        offset = start
                        break
                    if not line.endswith(b"\n"):
                        offset = start
                        break
                    offset = fh.tell()
                    try:
                        item = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    payload = item.get("payload", {})
                    if item.get("type") == "event_msg" and payload.get("type") == "task_complete":
                        msg = payload.get("last_agent_message")
                        if isinstance(msg, str) and msg.strip():
                            messages.append(msg)
        except OSError:
            pass
        return offset, messages

    def _write_launch_script(
        self, task_id: str, task_name: str, task: str, cwd: str
    ) -> Path:
        launch_dir = self.cfg.state_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        path = launch_dir / f"{task_id}.sh"
        codex_args = [self._codex()]
        if self.cfg.full_access:
            codex_args.append("--dangerously-bypass-approvals-and-sandbox")
        codex_args.extend(["--no-alt-screen", task])
        script = "\n".join(
            [
                "#!/bin/zsh",
                "set -e",
                f"cd {shlex.quote(cwd)}",
                f"export HANDOFF_TASK_ID={shlex.quote(task_id)}",
                f"export HANDOFF_TASK_NAME={shlex.quote(task_name)}",
                f"export HANDOFF_ROOT={shlex.quote(str(Path(__file__).resolve().parent))}",
                "exec " + " ".join(shlex.quote(arg) for arg in codex_args),
                "",
            ]
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)
        return path

    def start_terminal(self, task_id: str, task_name: str, task: str, cwd: str) -> Path:
        script = self._write_launch_script(task_id, task_name, task, cwd)
        applescript = (
            f'tell application "{self.cfg.terminal_app}" to '
            f'do script "exec /bin/zsh {str(script)}"'
        )
        subprocess.run(["osascript", "-e", applescript], check=True, timeout=15)
        return script

    def start_background(
        self, task_id: str, task_name: str, task: str, cwd: str
    ) -> subprocess.Popen:
        out_dir = self.cfg.state_dir / "runs" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "HANDOFF_TASK_ID": task_id,
                "HANDOFF_TASK_NAME": task_name,
                "HANDOFF_ROOT": str(Path(__file__).resolve().parent),
            }
        )
        cmd = [self._codex(), "exec"]
        if self.cfg.full_access:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.extend(["--skip-git-repo-check", "-C", cwd, "-o", str(out_dir / "out.txt"), "-"])
        stdout = (out_dir / "run.log").open("ab")
        stderr = (out_dir / "run.log").open("ab")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(task.encode("utf-8"))
        proc.stdin.close()
        return proc

    def background_log_path(self, task_id: str) -> Path:
        return self.cfg.state_dir / "runs" / task_id / "run.log"

    def wait_for_logged_session(self, task_id: str, timeout: int) -> Optional[str]:
        """Read the session id emitted by this process's own codex exec log."""
        path = self.background_log_path(task_id)
        deadline = time.time() + timeout
        pattern = re.compile(r"session id:\s*([0-9a-f-]{36})", re.IGNORECASE)
        while time.time() < deadline:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            match = pattern.search(text)
            if match:
                return match.group(1)
            time.sleep(0.2)
        return None

    def queue_message(self, session_id: str, cwd: str, text: str) -> subprocess.CompletedProcess:
        cmd = [self._codex(), "queue", "--thread", session_id, "--message", text]
        if self.cfg.full_access:
            cmd.insert(2, "--dangerously-bypass-approvals-and-sandbox")
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def session_is_active(self, record: Dict) -> bool:
        """A live TUI/app-server holds the rollout file open.

        `codex queue` accepts messages even when no writer is attached, leaving
        them pending forever. lsof is the reliable signal on macOS for whether a
        live process currently owns this thread's rollout.
        """
        rollout = Path(str(record.get("rollout_path", "")))
        if not rollout.exists():
            return False
        try:
            result = subprocess.run(
                ["lsof", "-t", str(rollout)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def resume_message(self, session_id: str, cwd: str, text: str, task_id: str) -> subprocess.Popen:
        run_dir = self.cfg.state_dir / "runs" / f"{task_id}-resume-{int(time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({"HANDOFF_TASK_ID": task_id})
        cmd = [self._codex(), "exec", "resume", session_id]
        if self.cfg.full_access:
            cmd.insert(2, "--dangerously-bypass-approvals-and-sandbox")
        cmd.extend(["--skip-git-repo-check", "-"])
        stdout = (run_dir / "run.log").open("ab")
        stderr = (run_dir / "run.log").open("ab")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
        return proc

    def route_message(self, record: Dict, text: str) -> Tuple[str, str]:
        session_id = str(record.get("session_id", ""))
        cwd = str(record.get("cwd") or Path.home())
        if not session_id:
            raise RuntimeError("record has no session_id")
        steered = steer_active_turn(self._codex(), session_id, text)
        if steered:
            return "steer", f"steered turn={steered['turn_id']}"

        codex_home = Path(str(record.get("codex_home") or self.session_home())).expanduser()
        pending_before = queued_item_ids(codex_home, session_id, text)
        queued = self.queue_message(session_id, cwd, text)
        if queued.returncode == 0:
            pending_after = queued_item_ids(codex_home, session_id, text)
            new_ids = pending_after - pending_before
            if new_ids and wait_for_queue_consumption(codex_home, session_id, new_ids):
                return "queue", queued.stdout.strip() or "queued"
            remove_queued_items(codex_home, pending_after)
        resumed = self.resume_message(session_id, cwd, text, str(record["id"]))
        return "resume", f"started pid={resumed.pid}"

