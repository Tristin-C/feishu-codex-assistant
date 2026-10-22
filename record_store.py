#!/usr/bin/env python3
"""Atomic, durable task records."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class RecordStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
        if not safe or safe != task_id:
            raise ValueError(f"invalid task id: {task_id!r}")
        return self.root / f"{safe}.json"

    def put(self, record: Dict) -> Dict:
        with self._lock:
            path = self._path(str(record["id"]))
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            return record

    def get(self, task_id: str) -> Optional[Dict]:
        with self._lock:
            path = self._path(task_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def update(self, task_id: str, **changes) -> Dict:
        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise KeyError(task_id)
            record.update(changes)
            record["updated_at"] = time.time()
            return self.put(record)

    def list(self) -> List[Dict]:
        with self._lock:
            records = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
            return records

    def by_message_id(self, message_id: str) -> Optional[Dict]:
        if not message_id:
            return None
        for record in self.list():
            ids = record.get("feishu_message_ids", [])
            if record.get("feishu_message_id") == message_id or message_id in ids:
                return record
        return None

    def by_session_id(self, session_id: str) -> Optional[Dict]:
        """Return the original task record for a Codex session.

        Codex tool subprocesses do not reliably inherit HANDOFF_TASK_ID. Reuse
        the oldest record for the session so manual sends and rollout watchers
        share the same H-id and dedupe key.
        """
        if not session_id:
            return None
        matches = [
            record
            for record in self.list()
            if record.get("session_id") == session_id
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda record: (
                float(record.get("created_at", float("inf"))),
                str(record.get("id", "")),
            ),
        )

