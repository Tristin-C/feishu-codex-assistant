#!/usr/bin/env python3
"""Minimal app-server control client for steering active Codex turns."""
from __future__ import annotations

import base64
import json
import os
import socket
import sqlite3
import struct
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class AppServerError(RuntimeError):
    pass


class AppServerControl:
    def __init__(self, codex_bin: str, timeout: float = 5.0):
        version = subprocess.run(
            [codex_bin, "app-server", "daemon", "version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if version.returncode != 0:
            raise AppServerError(version.stderr.strip() or "app-server daemon unavailable")
        data = json.loads(version.stdout)
        if data.get("status") != "running" or not data.get("socketPath"):
            raise AppServerError("app-server daemon is not running")
        self.socket_path = data["socketPath"]
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._next_id = 0

    def __enter__(self) -> "AppServerControl":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise AppServerError("app-server closed during WebSocket upgrade")
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise AppServerError(status_line.decode("utf-8", errors="replace"))
        self.sock = sock
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "feishu_handoff_bridge",
                    "title": "Feishu Handoff Bridge",
                    "version": "0.1.0",
                }
            },
        )
        if "error" in result:
            raise AppServerError(str(result["error"]))
        self.notify("initialized", {})
        return self

    def __exit__(self, *_args) -> None:
        if self.sock:
            try:
                self._send_frame(8, b"")
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _read_exact(self, count: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise EOFError("app-server WebSocket closed")
            data += chunk
        return data

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if second & 0x80 else b""
        payload = self._read_exact(length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read_json(self) -> Dict:
        while True:
            opcode, payload = self._read_frame()
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 9:
                self._send_frame(10, payload)
            elif opcode == 8:
                raise EOFError("app-server WebSocket closed")

    def notify(self, method: str, params: Dict) -> None:
        self._send_frame(1, json.dumps({"method": method, "params": params}).encode("utf-8"))

    def request(self, method: str, params: Dict) -> Dict:
        self._next_id += 1
        request_id = self._next_id
        message = {"method": method, "id": request_id, "params": params}
        self._send_frame(1, json.dumps(message, ensure_ascii=False).encode("utf-8"))
        while True:
            response = self._read_json()
            if response.get("id") == request_id:
                return response

    def steer(self, thread_id: str, text: str) -> Optional[Dict]:
        resumed = self.request(
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        if "error" in resumed:
            return None
        thread = (resumed.get("result") or {}).get("thread") or {}
        status = thread.get("status") or {}
        if status.get("type") != "active" or thread.get("canAcceptDirectInput") is False:
            return None
        turns = self.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 5,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        )
        if "error" in turns:
            return None
        active_turn = next(
            (
                turn
                for turn in ((turns.get("result") or {}).get("data") or [])
                if turn.get("status") == "inProgress"
            ),
            None,
        )
        if not active_turn or not active_turn.get("id"):
            return None
        steered = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": active_turn["id"],
            },
        )
        if "error" in steered:
            return None
        return {
            "turn_id": str((steered.get("result") or {}).get("turnId") or active_turn["id"]),
            "status": "active",
        }


def steer_active_turn(
    codex_bin: str,
    thread_id: str,
    text: str,
) -> Optional[Dict]:
    try:
        with AppServerControl(codex_bin) as client:
            return client.steer(thread_id, text)
    except (AppServerError, EOFError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def _queue_rows(codex_home: Path, thread_id: str) -> List[tuple[str, str]]:
    db = codex_home / "queue_1.sqlite"
    if not db.exists():
        return []
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        return connection.execute(
            "SELECT id, payload_json FROM queued_items WHERE thread_id=? ORDER BY queue_order",
            (thread_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def queued_item_ids(
    codex_home: Path,
    thread_id: str,
    text: Optional[str] = None,
) -> set[str]:
    matches: set[str] = set()
    for item_id, payload_json in _queue_rows(codex_home, thread_id):
        try:
            payload = json.loads(payload_json)
            content = ((payload.get("UserInput") or {}).get("content") or [])
            payload_text = "\n".join(str(part.get("text", "")) for part in content)
        except (ValueError, TypeError):
            continue
        if text is None or payload_text == text:
            matches.add(str(item_id))
    return matches


def wait_for_queue_consumption(
    codex_home: Path,
    thread_id: str,
    item_ids: Iterable[str],
    timeout: float = 2.0,
) -> bool:
    pending = set(item_ids)
    if not pending:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = queued_item_ids(codex_home, thread_id)
        if not (pending & remaining):
            return True
        time.sleep(0.2)
    return False


def remove_queued_items(codex_home: Path, item_ids: Iterable[str]) -> None:
    ids = [str(item_id) for item_id in item_ids]
    if not ids:
        return
    db = codex_home / "queue_1.sqlite"
    connection = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=5)
    try:
        connection.executemany("DELETE FROM queued_items WHERE id=?", [(item_id,) for item_id in ids])
        connection.commit()
    finally:
        connection.close()

