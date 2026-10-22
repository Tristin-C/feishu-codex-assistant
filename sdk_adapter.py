#!/usr/bin/env python3
"""Headless Codex client integration backed by the official Python SDK."""
from __future__ import annotations

import threading
from typing import Dict, List

from handoff_config import Config


class CodexSDKAdapter:
    """Create and resume Codex threads without launching a terminal UI."""

    def __init__(self, cfg: Config):
        try:
            from openai_codex import Codex, CodexConfig
        except ImportError as exc:
            raise RuntimeError(
                "client mode requires Python 3.10+ and `pip install openai-codex`"
            ) from exc

        self.cfg = cfg
        self._sdk = __import__("openai_codex")
        self._codex = Codex(
            CodexConfig(
                cwd=str(cfg.default_cwd),
                client_name="feishu_handoff_bridge",
                client_title="Feishu Codex Bridge",
                client_version="0.1.0",
            )
        )
        self._run_lock = threading.Lock()

    def _sandbox(self):
        key = "full_access" if self.cfg.full_access else "workspace_write"
        return getattr(self._sdk.Sandbox, key)

    def _approval_mode(self):
        return getattr(self._sdk.ApprovalMode, "auto_review")

    def start_thread(self, task_name: str, cwd: str) -> Dict[str, str]:
        thread = self._codex.thread_start(
            cwd=cwd,
            sandbox=self._sandbox(),
            approval_mode=self._approval_mode(),
            service_name="feishu_handoff_bridge",
        )
        if task_name:
            try:
                thread.set_name(task_name[:120])
            except Exception:
                pass
        return {"thread_id": thread.id}

    def run_thread(self, thread_id: str, text: str, cwd: str) -> Dict:
        thread = self._codex.thread_resume(thread_id, cwd=cwd)
        with self._run_lock:
            result = thread.run(text, cwd=cwd)
        return {
            "turn_id": result.id,
            "status": getattr(result.status, "value", str(result.status)),
            "final_response": result.final_response or "",
            "error": getattr(result.error, "message", None) if result.error else None,
        }

    def list_threads(self, cwd: str, limit: int = 20) -> List[Dict]:
        response = self._codex.thread_list(limit=max(1, min(limit, 50)), cwd=cwd)
        threads = []
        for thread in response.data:
            status = getattr(getattr(thread, "status", None), "root", None)
            thread_cwd = getattr(thread.cwd, "root", thread.cwd)
            threads.append(
                {
                    "id": thread.id,
                    "name": thread.name or "",
                    "preview": thread.preview or "",
                    "cwd": str(thread_cwd or ""),
                    "created_at": thread.created_at,
                    "updated_at": thread.updated_at,
                    "status": getattr(status, "type", "unknown"),
                }
            )
        return threads

