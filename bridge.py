#!/usr/bin/env python3
"""Deterministic Feishu <-> Codex CLI bridge.

No model is used to classify or route messages. Incoming replies are matched by
Feishu message id or H-id, then queued into the owning Codex session.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from codex_adapter import CodexAdapter, DiscoveryResult
from feishu_client import FeishuClient, FeishuResult
from handoff_config import Config, sanitize_task_name, validate_config
from record_store import RecordStore


TASK_ID_RE = re.compile(r"\bH-\d{8}-\d{6}-\d+\b")
HISTORY_COMMAND_RE = re.compile(
    r"^\s*(?:/history|历史任务|历史记录)(?:\s+(.*?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
START_RE = re.compile(
    r"^\s*(?:/start|/new|start)\s+"
    r"(terminal|background|client|app|desktop|终端|后台|客户端)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
THREADS_COMMAND_RE = re.compile(
    r"^\s*/(?:threads|sessions)(?:\s+(.+?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
RESUME_COMMAND_RE = re.compile(
    r"^\s*/(?:resume|continue)\s+"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
CHINESE_START_HINT_RE = re.compile(
    r"(帮我|给我|请)?\s*(新开|开|启动|起)(一个|个)?\s*(codex|终端|后台)",
    re.IGNORECASE,
)


def new_task_id() -> str:
    return f"H-{time.strftime('%Y%m%d-%H%M%S')}-{random.randint(0, 9999):04d}"


def normalize_message_for_dedupe(text: str) -> str:
    """Normalize presentation-only Markdown differences between send paths."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"`+", "", text)
    text = re.sub(r"(\*\*|__|~~|==)", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_mode(value: str) -> str:
    v = value.strip().lower()
    if v in {"terminal", "终端", "term", "tui"}:
        return "terminal"
    if v in {"background", "后台", "bg", "exec"}:
        return "background"
    if v in {"client", "app", "desktop", "客户端"}:
        return "client"
    raise ValueError("mode must be terminal, background, or client")


def parse_start_command(text: str) -> Tuple[Optional[str], Optional[str], str]:
    """Return (mode, task, error). Supports an explicit deterministic command set."""
    text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.IGNORECASE)
    match = START_RE.match(text)
    if match:
        return normalize_mode(match.group(1)), match.group(2).strip(), ""
    if CHINESE_START_HINT_RE.search(text):
        lowered = text.lower()
        mode = None
        if "后台" in text or "background" in lowered:
            mode = "background"
        elif "终端" in text or "terminal" in lowered:
            mode = "terminal"
        task = ""
        task_match = re.search(r"任务\s*(?:是|为|[:：])?\s*(.+)$", text, re.DOTALL)
        if task_match:
            task = task_match.group(1).strip()
        elif mode:
            prefix = re.sub(
                r"^.*?(terminal|background|终端|后台)(?:会话|窗口|cli)?[，,：:\s]*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            task = prefix
        if not mode:
            return None, None, "请指定启动方式：终端窗口或后台会话。"
        if not task:
            return mode, None, "请补充具体任务内容。"
        return mode, task, ""
    return None, None, ""


def parse_history_command(text: str) -> Tuple[bool, Optional[str], str]:
    """Return (is_history, task_id, error)."""
    match = HISTORY_COMMAND_RE.match(text)
    if not match:
        return False, None, ""
    argument = (match.group(1) or "").strip()
    if not argument:
        return True, None, ""
    if TASK_ID_RE.fullmatch(argument):
        return True, argument, ""
    return True, None, "用法：/history 或 /history H-编号"


def parse_threads_command(text: str) -> Tuple[bool, str]:
    match = THREADS_COMMAND_RE.match(text)
    if not match:
        return False, ""
    return True, (match.group(1) or "").strip()


def parse_resume_command(text: str) -> Tuple[Optional[str], Optional[str]]:
    match = RESUME_COMMAND_RE.match(text)
    if not match:
        return None, None
    return match.group(1), match.group(2).strip()


class ClaimStore:
    """Persistent time-window dedupe."""

    def __init__(self, root: Path, window_secs: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.window = window_secs
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        marker = self.root / digest
        with self._lock:
            now = time.time()
            if marker.exists() and now - marker.stat().st_mtime < self.window:
                return False
            marker.write_text(str(now), encoding="utf-8")
            # Opportunistic cleanup.
            for child in self.root.iterdir():
                try:
                    if now - child.stat().st_mtime > max(self.window * 4, 3600):
                        child.unlink()
                except OSError:
                    pass
            return True

    def release(self, key: str) -> None:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            (self.root / digest).unlink()
        except OSError:
            pass


class Bridge:
    def __init__(self, cfg: Config, local_only: bool = False):
        self.cfg = cfg
        self.local_only = local_only
        cfg.ensure_dirs()
        self.store = RecordStore(cfg.records_dir)
        self.adapter = CodexAdapter(cfg)
        self.feishu = FeishuClient(cfg)
        self.claims = ClaimStore(cfg.state_dir / "dedupe", cfg.dedupe_window_secs)
        self._stop = threading.Event()
        self._watchers_lock = threading.Lock()
        self._watchers: Dict[str, Dict] = {}
        self._route_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._sdk_adapter = None
        self._sdk_adapter_lock = threading.Lock()
        self._event_process: Optional[subprocess.Popen] = None
        self._log_lock = threading.Lock()
        self.log_path = cfg.logs_dir / "bridge.log"

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        with self._log_lock:
            try:
                print(line, flush=True)
            except (AttributeError, OSError):
                pass
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def stop(self) -> None:
        self._stop.set()
        proc = self._event_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def send_direct_text(self, text: str) -> Optional[FeishuResult]:
        if self.local_only or not self.cfg.send_enabled:
            self.log(f"LOCAL TEXT: {text}")
            return None
        return self.feishu.send_text(text)

    def sdk_adapter(self):
        with self._sdk_adapter_lock:
            if self._sdk_adapter is None:
                from sdk_adapter import CodexSDKAdapter

                self._sdk_adapter = CodexSDKAdapter(self.cfg)
            return self._sdk_adapter

    def _run_sdk_turn(self, task_id: str, text: str) -> None:
        record = self.store.get(task_id)
        if not record:
            return
        try:
            result = self.sdk_adapter().run_thread(
                str(record["session_id"]),
                text,
                str(record.get("cwd") or self.cfg.default_cwd),
            )
            final = str(result.get("final_response") or "").strip()
            if not final:
                final = str(result.get("error") or "Codex turn completed without output")
            self._send_record(self.store.get(task_id), final)
        except Exception as exc:
            self.log(f"SDK turn failed {task_id}: {exc}\n{traceback.format_exc()}")
            try:
                failed = self.store.get(task_id)
                self._send_record(failed, f"Codex 客户端执行失败：{exc}", force=True)
            except Exception:
                pass

    def _record_footer(self, record: Dict) -> str:
        sid = str(record.get("session_id", ""))
        cwd = str(record.get("cwd", ""))
        mode = {
            "terminal": "终端",
            "background": "后台",
            "client": "客户端",
            "attached": "已关联",
        }.get(str(record.get("mode", "")), str(record.get("mode") or "unknown"))
        return f"{record['id']} · 模式 {mode} · 会话 {sid or 'pending'} · {cwd}"

    def _send_record(
        self,
        record: Dict,
        text: str,
        *,
        title: Optional[str] = None,
        body_lines: Optional[List[str]] = None,
        force: bool = False,
        watch_from_start: bool = False,
        next_status: str = "waiting",
    ) -> Dict:
        text = text.strip()
        if not text:
            raise ValueError("empty result")
        dedupe_text = normalize_message_for_dedupe(text)
        claim_key = f"{record['id']}|{dedupe_text}"
        previous_text = normalize_message_for_dedupe(str(record.get("last_output") or ""))
        if not force and (
            record.get("last_output_dedupe_key") == dedupe_text
            or (previous_text and previous_text == dedupe_text)
            or not self.claims.claim(claim_key)
        ):
            return {"status": "duplicate", "id": record["id"]}
        title = title or sanitize_task_name(text)
        if body_lines is None:
            lines = [line for line in text.splitlines() if line.strip()]
            body_lines = lines[1:]
        footer = self._record_footer(record)
        try:
            if self.local_only or not self.cfg.send_enabled:
                sent = FeishuResult(message_id=f"local-{record['id']}-{int(time.time())}", raw={})
                self.log(f"LOCAL SEND {record['id']}: {title}\n{text}")
            else:
                sent = self.feishu.send_post(title, body_lines, footer)
        except Exception:
            if not force:
                self.claims.release(claim_key)
            raise
        ids = list(record.get("feishu_message_ids", []))
        if sent.message_id:
            ids.append(sent.message_id)
        record.update(
            {
                "last_output": text,
                "last_output_dedupe_key": dedupe_text,
                "status": next_status,
                "feishu_message_id": sent.message_id,
                "feishu_message_ids": ids[-100:],
                "last_sent_at": time.time(),
            }
        )
        self.store.put(record)
        self.ensure_watcher(record, from_start=watch_from_start)
        return {"status": "sent", "id": record["id"], "message_id": sent.message_id}

    def ensure_watcher(self, record: Dict, from_start: bool) -> None:
        sid = str(record.get("session_id", ""))
        rollout = Path(str(record.get("rollout_path", "")))
        if not sid or not rollout.exists():
            return
        with self._watchers_lock:
            if sid in self._watchers:
                return
            offset = 0 if from_start else rollout.stat().st_size
            self._watchers[sid] = {
                "task_id": record["id"],
                "path": rollout,
                "offset": offset,
            }
            threading.Thread(target=self._watch_loop, args=(sid,), daemon=True).start()

    def _watch_loop(self, sid: str) -> None:
        while not self._stop.is_set():
            with self._watchers_lock:
                watcher = dict(self._watchers.get(sid, {}))
            if not watcher:
                return
            path = Path(watcher["path"])
            offset, messages = self.adapter.read_new_completions(path, int(watcher["offset"]))
            with self._watchers_lock:
                if sid in self._watchers:
                    self._watchers[sid]["offset"] = offset
            for message in messages:
                record = self.store.get(watcher["task_id"])
                if not record:
                    continue
                try:
                    self._send_record(record, message)
                except Exception as exc:
                    self.log(f"watch send failed {watcher['task_id']}: {exc}")
            self._stop.wait(self.cfg.poll_secs)

    def create_record(
        self,
        session_id: str,
        cwd: str,
        mode: str,
        task_name: str,
        *,
        task_id: Optional[str] = None,
        rollout_path: Optional[Path] = None,
        codex_home: Optional[Path] = None,
    ) -> Dict:
        task_id = task_id or new_task_id()
        rollout = rollout_path or self.adapter.find_rollout(session_id, codex_home)
        record = self.store.get(task_id) or {
            "id": task_id,
            "created_at": time.time(),
        }
        record.update(
            {
                "task_name": task_name,
                "session_id": session_id,
                "codex_home": str(codex_home or self.adapter.session_home()),
                "cwd": cwd,
                "mode": mode,
                "status": "running",
                "rollout_path": str(rollout or ""),
                "feishu_message_ids": list(record.get("feishu_message_ids", [])),
            }
        )
        return self.store.put(record)

    def handle_send(self, request: Dict) -> Dict:
        sid = str(request.get("session_id") or os.environ.get("CODEX_THREAD_ID") or "")
        if not sid:
            sid = str(os.environ.get("CODEX_SESSION_ID") or "")
        cwd = str(request.get("cwd") or os.getcwd())
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        if not sid:
            raise RuntimeError("cannot determine Codex session id; set CODEX_THREAD_ID or pass --session")
        rollout = self.adapter.find_rollout(sid, codex_home)
        task_id = request.get("task_id") or os.environ.get("HANDOFF_TASK_ID")
        task_name = str(
            request.get("task_name")
            or os.environ.get("HANDOFF_TASK_NAME")
            or (self.adapter.latest_user_text(rollout) if rollout else "")
            or Path(cwd).name
        )
        task_name = sanitize_task_name(task_name)
        record = self.store.get(task_id) if task_id else self.store.by_session_id(sid)
        if record is None:
            record = self.create_record(
                sid,
                cwd,
                str(request.get("mode") or "attached"),
                task_name,
                task_id=task_id,
                rollout_path=rollout,
                codex_home=codex_home,
            )
        else:
            record.update(
                {
                    "session_id": sid,
                    "cwd": cwd,
                    "rollout_path": str(rollout or record.get("rollout_path", "")),
                }
            )
            self.store.put(record)
        return self._send_record(record, str(request.get("text", "")))

    def handle_start(self, request: Dict) -> Dict:
        # Terminal discovery is based on cwd + time. Serialize starts so two
        # concurrent requests cannot claim each other's newly created rollout.
        with self._start_lock:
            return self._handle_start_locked(request)

    def _handle_start_locked(self, request: Dict) -> Dict:
        try:
            mode = normalize_mode(str(request.get("mode", "")))
        except ValueError:
            raise RuntimeError(
                "请指定启动方式：client（Codex 客户端）、"
                "terminal（终端窗口）或 background（后台会话）"
            )
        task = str(request.get("task", "")).strip()
        if not task:
            raise RuntimeError("任务内容不能为空")
        cwd = str(
            Path(str(request.get("cwd") or self.cfg.default_cwd)).expanduser().resolve()
        )
        if not Path(cwd).is_dir():
            raise RuntimeError(f"working directory does not exist: {cwd}")
        task_id = new_task_id()
        task_name = sanitize_task_name(task)
        since = time.time()
        if mode == "client":
            info = self.sdk_adapter().start_thread(task_name, cwd)
            record = self.create_record(
                info["thread_id"],
                cwd,
                mode,
                task_name,
                task_id=task_id,
            )
            record.update({"sdk_thread_id": info["thread_id"]})
            self.store.put(record)
            startup = (
                f"任务：{task}\n"
                f"模式：Codex 客户端线程\n"
                f"线程：{record['session_id']}\n"
                f"目录：{cwd}"
            )
            sent = self._send_record(
                record,
                startup,
                title="Codex 客户端任务已启动",
                body_lines=[
                    f"任务：{task_name}",
                    "任务已开始执行，完成后会自动回传结果。",
                ],
                force=True,
                next_status="running",
            )
            threading.Thread(
                target=self._run_sdk_turn,
                args=(task_id, task),
                daemon=True,
            ).start()
            return {
                "status": "started",
                "record": self.store.get(task_id),
                "feishu": sent,
            }
        if mode == "terminal":
            self.adapter.start_terminal(task_id, task_name, task, cwd)
            found = self.adapter.wait_for_session(
                cwd,
                since,
                self.cfg.startup_timeout_secs,
                require_tui=True,
            )
            if not found:
                record = self.create_record("", cwd, mode, task_name, task_id=task_id)
                record.update({"status": "start_failed", "last_output": "terminal did not create a rollout"})
                self.store.put(record)
                raise RuntimeError(
                    "Terminal opened, but no matching Codex session appeared. "
                    "Check that codex starts in the terminal and the task entered."
                )
            record = self.create_record(
                found.session_id,
                cwd,
                mode,
                task_name,
                task_id=task_id,
                rollout_path=found.rollout_path,
            )
        else:
            proc = self.adapter.start_background(task_id, task_name, task, cwd)
            record = self.create_record("", cwd, mode, task_name, task_id=task_id)
            record.update({"pid": proc.pid})
            self.store.put(record)
            session_id = self.adapter.wait_for_logged_session(
                task_id, self.cfg.startup_timeout_secs
            )
            found = None
            if session_id:
                rollout = self.adapter.find_rollout(session_id)
                if rollout:
                    found = DiscoveryResult(session_id, rollout, cwd)
            if found:
                record.update(
                    {
                        "session_id": found.session_id,
                        "rollout_path": str(found.rollout_path),
                    }
                )
                self.store.put(record)
            else:
                proc.terminate()
                record.update({"status": "start_failed", "last_output": "background session not discovered"})
                self.store.put(record)
                raise RuntimeError("background Codex started but its session id was not discovered")
        startup = (
            f"任务：{task}\n"
            f"模式：{'终端窗口' if mode == 'terminal' else '后台会话'}\n"
            f"会话：{record['session_id']}\n"
            f"目录：{cwd}"
        )
        sent = self._send_record(
            record,
            startup,
            title="Codex 任务已启动",
            body_lines=[
                f"任务：{task_name}",
                "任务已开始执行，完成后会自动回传结果。",
            ],
            force=True,
            watch_from_start=True,
            next_status="running",
        )
        return {"status": "started", "record": self.store.get(task_id), "feishu": sent}

    def handle_reply(self, task_id: str, text: str) -> Dict:
        record = self.store.get(task_id)
        if not record:
            raise RuntimeError(f"unknown task id: {task_id}")
        if not text.strip():
            raise RuntimeError("reply text is empty")
        if record.get("mode") == "client":
            self.store.update(task_id, status="running", last_route="client")
            self.send_direct_text(f"已转达 {task_id} · Codex 客户端线程")
            threading.Thread(
                target=self._run_sdk_turn,
                args=(task_id, text.strip()),
                daemon=True,
            ).start()
            return {"status": "client", "id": task_id, "detail": "started"}
        with self._route_lock:
            route, detail = self.adapter.route_message(record, text.strip())
        self.store.update(
            task_id,
            status="running" if route in {"queue", "steer"} else "resuming",
            last_route=route,
            last_route_at=time.time(),
        )
        route_label = {
            "queue": "运行中会话",
            "steer": "已注入运行中会话",
            "resume": "已续接会话",
        }.get(route, "已转达")
        send_direct_text = f"已转达 {task_id} · {route_label}"
        try:
            self.send_direct_text(send_direct_text)
        except Exception as exc:
            self.log(f"ack failed: {exc}")
        return {"status": route, "id": task_id, "detail": detail}

    def _extract_task_id(self, event: Dict) -> Optional[str]:
        reply_to = str(event.get("reply_to") or event.get("root_id") or "")
        record = self.store.by_message_id(reply_to)
        if record:
            return str(record["id"])
        content = str(event.get("content") or "")
        match = TASK_ID_RE.search(content)
        return match.group(0) if match else None

    @staticmethod
    def _format_time(value: object) -> str:
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return "时间未知"

    def handle_history(self, task_id: Optional[str] = None) -> Dict:
        if task_id:
            record = self.store.get(task_id)
            if not record:
                self.send_direct_text(f"找不到历史任务：{task_id}")
                return {"status": "not_found", "id": task_id}
            output = str(record.get("last_output") or "").strip()
            if len(output) > 1800:
                output = output[:1800].rstrip() + "\n…（内容已截断）"
            lines = [
                f"任务：{record.get('id', task_id)}",
                f"名称：{record.get('task_name') or '未命名任务'}",
                f"状态：{record.get('status') or 'unknown'}",
                f"模式：{record.get('mode') or 'unknown'}",
                f"创建：{self._format_time(record.get('created_at'))}",
                f"目录：{record.get('cwd') or ''}",
                f"会话：{record.get('session_id') or '尚未建立'}",
            ]
            if record.get("last_route"):
                lines.append(f"最近路由：{record.get('last_route')}")
            if output:
                lines.extend(["", "最近结果：", output])
            lines.extend(["", "直接回复本条消息可继续此会话。"])
            sent = self.send_direct_text("\n".join(lines))
            if sent and sent.message_id:
                current = self.store.get(task_id)
                if current:
                    message_ids = list(current.get("feishu_message_ids", []))
                    if sent.message_id not in message_ids:
                        message_ids.append(sent.message_id)
                    self.store.update(
                        task_id,
                        feishu_message_ids=message_ids[-100:],
                    )
            return {"status": "sent", "id": task_id}

        records = sorted(
            self.store.list(),
            key=lambda record: float(record.get("created_at", 0)),
            reverse=True,
        )
        if not records:
            self.send_direct_text("还没有历史任务。")
            return {"status": "sent", "count": 0}
        shown = records[:20]
        lines = [f"历史任务（最近 {len(shown)} / 共 {len(records)} 条）", ""]
        for record in shown:
            task_name = str(record.get("task_name") or "未命名任务").replace("\n", " ")
            if len(task_name) > 42:
                task_name = task_name[:42].rstrip() + "…"
            status = str(record.get("status") or "unknown")
            mode = str(record.get("mode") or "unknown")
            lines.append(
                f"{self._format_time(record.get('created_at'))}  "
                f"{record.get('id', '')}  {status:10}  "
                f"{mode:8}  {task_name}"
            )
        lines.extend(["", "查看详情：/history H-编号"])
        self.send_direct_text("\n".join(lines))
        return {"status": "sent", "count": len(records)}

    def handle_threads(self, search: str = "") -> Dict:
        threads = self.sdk_adapter().list_threads(
            str(self.cfg.default_cwd),
            limit=20,
        )
        if search:
            needle = search.casefold()
            threads = [
                thread
                for thread in threads
                if needle in str(thread.get("name", "")).casefold()
                or needle in str(thread.get("preview", "")).casefold()
            ]
        if not threads:
            self.send_direct_text("没有找到 Codex 客户端历史线程。")
            return {"status": "sent", "count": 0}
        lines = [f"Codex 客户端线程（{len(threads)} 条）", ""]
        for thread in threads:
            title = str(thread.get("name") or thread.get("preview") or "未命名线程")
            title = title.replace("\n", " ")
            if len(title) > 36:
                title = title[:36].rstrip() + "…"
            status = str(thread.get("status") or "unknown")
            lines.append(
                f"{self._format_time(thread.get('updated_at'))}  "
                f"{thread.get('id')}  {status:10}  {title}"
            )
        lines.extend(["", "继续线程：/resume <线程ID> <指令>"])
        self.send_direct_text("\n".join(lines))
        return {"status": "sent", "count": len(threads)}

    def handle_event(self, event: Dict) -> None:
        try:
            if event.get("sender_type") == "bot":
                return
            sender = str(event.get("sender_id", ""))
            if self.cfg.allowed_open_ids and sender not in self.cfg.allowed_open_ids:
                self.log(f"ignored unauthorized sender {sender}")
                return
            content = str(event.get("content") or "").strip()
            if not content:
                return
            thread_id, resume_text = parse_resume_command(content)
            if thread_id and resume_text:
                record = self.store.by_session_id(thread_id)
                if record is None:
                    record = self.create_record(
                        thread_id,
                        str(self.cfg.default_cwd),
                        "client",
                        sanitize_task_name(resume_text),
                    )
                elif record.get("mode") != "client":
                    self.store.update(record["id"], mode="client")
                self.handle_reply(str(record["id"]), resume_text)
                return
            is_threads, thread_search = parse_threads_command(content)
            if is_threads:
                self.handle_threads(thread_search)
                return
            is_history, history_task_id, history_error = parse_history_command(content)
            if is_history:
                if history_error:
                    self.send_direct_text(history_error)
                else:
                    self.handle_history(history_task_id)
                return
            task_id = self._extract_task_id(event)
            if task_id:
                self.handle_reply(task_id, content)
                return
            mode, task, error = parse_start_command(content)
            if mode and task:
                self.handle_start({"mode": mode, "task": task})
                return
            if mode or error:
                self.send_direct_text(
                    (error or "缺少任务内容。")
                    + "\n请发送：/start terminal 任务内容 或 /start background 任务内容"
                )
                return
            self.send_direct_text(
                "请回复带 H-编号的任务消息来继续执行。\n"
                "新建客户端任务：/start client 任务内容\n"
                "查看客户端线程：/threads\n"
                "其他模式：/start terminal 或 /start background"
            )
        except Exception as exc:
            self.log(f"event handler failed: {exc}\n{traceback.format_exc()}")
            try:
                self.send_direct_text(f"处理失败：{exc}")
            except Exception:
                pass

    def _event_loop(self) -> None:
        if self.local_only:
            return
        while not self._stop.is_set():
            cmd = [
                self.cfg.lark_cli,
                "--profile",
                self.cfg.profile,
                "event",
                "consume",
                "im.message.receive_v1",
                "--as",
                "bot",
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                self._event_process = proc
                assert proc.stdout is not None and proc.stderr is not None

                def read_stderr() -> None:
                    for line in proc.stderr:
                        self.log(f"event: {line.rstrip()}")

                threading.Thread(target=read_stderr, daemon=True).start()
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        self.log(f"invalid event line: {line[:200]}")
                        continue
                    threading.Thread(
                        target=self.handle_event, args=(event,), daemon=True
                    ).start()
                rc = proc.wait()
                self.log(f"event consumer exited rc={rc}")
            except Exception as exc:
                self.log(f"event consumer failed: {exc}")
            self._stop.wait(min(10, max(1, self.cfg.poll_secs * 3)))

    def _dispatch(self, request: Dict) -> Dict:
        action = request.get("action")
        if action == "ping":
            return {"ok": True, "pid": os.getpid()}
        if action == "send":
            return {"ok": True, **self.handle_send(request)}
        if action == "start":
            return {"ok": True, **self.handle_start(request)}
        if action == "reply":
            return {
                "ok": True,
                **self.handle_reply(str(request.get("task_id", "")), str(request.get("text", ""))),
            }
        if action == "list":
            return {"ok": True, "records": self.store.list()}
        if action == "show":
            record = self.store.get(str(request.get("task_id", "")))
            if not record:
                return {"ok": False, "error": "task not found"}
            return {"ok": True, "record": record}
        return {"ok": False, "error": f"unknown action: {action}"}

    def _socket_server(self) -> None:
        if os.name == "nt":
            self._tcp_server()
            return
        sock_path = self.cfg.socket_path
        try:
            if sock_path.exists():
                sock_path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(20)
        server.settimeout(1)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            server.close()
            try:
                sock_path.unlink()
            except OSError:
                pass

    def _tcp_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.cfg.ipc_host, self.cfg.ipc_port))
        server.listen(20)
        server.settimeout(1)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            server.close()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                reader = conn.makefile("r", encoding="utf-8")
                writer = conn.makefile("w", encoding="utf-8")
                line = reader.readline()
                try:
                    request = json.loads(line) if line else {}
                    response = self._dispatch(request)
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                writer.write(json.dumps(response, ensure_ascii=False) + "\n")
                writer.flush()
        except Exception as exc:
            self.log(f"socket request failed: {exc}")

    def process_outbox(self) -> None:
        for path in sorted(self.cfg.outbox_dir.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                result = self._dispatch(request)
                path.unlink()
                self.log(f"outbox {path.name}: {result.get('status', result)}")
            except Exception as exc:
                self.log(f"outbox {path.name} failed: {exc}")

    def serve(self) -> None:
        errors = validate_config(self.cfg)
        if errors and not self.local_only:
            raise RuntimeError("; ".join(errors))
        self.log(f"bridge starting pid={os.getpid()} local_only={self.local_only}")
        self.process_outbox()
        threading.Thread(target=self._socket_server, daemon=True).start()
        threading.Thread(target=self._event_loop, daemon=True).start()
        while not self._stop.is_set():
            self.process_outbox()
            self._stop.wait(5)


def ipc_request(cfg: Config, request: Dict, queue_on_failure: bool = True) -> Dict:
    try:
        if os.name == "nt":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        if os.name == "nt":
            sock.connect((cfg.ipc_host, cfg.ipc_port))
        else:
            sock.connect(str(cfg.socket_path))
        with sock:
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            response = b""
            while not response.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        return json.loads(response.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if queue_on_failure:
            cfg.ensure_dirs()
            path = cfg.outbox_dir / f"{time.time_ns()}-{os.getpid()}.json"
            path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            return {
                "ok": False,
                "queued": True,
                "error": f"bridge unavailable ({exc}); request queued at {path}",
            }
        return {"ok": False, "error": str(exc)}

