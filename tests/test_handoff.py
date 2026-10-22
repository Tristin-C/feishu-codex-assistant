#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import (
    Bridge,
    ClaimStore,
    parse_history_command,
    parse_resume_command,
    parse_start_command,
    parse_threads_command,
)
from codex_adapter import CodexAdapter, DiscoveryResult
from feishu_client import FeishuResult
from handoff_config import Config
from record_store import RecordStore


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.counter = 0

    def send_post(self, title, body_lines, footer):
        self.counter += 1
        mid = f"om_fake_{self.counter}"
        self.calls.append(("post", title, body_lines, footer, mid))
        return FeishuResult(mid, {})

    def send_text(self, text):
        self.counter += 1
        mid = f"om_fake_{self.counter}"
        self.calls.append(("text", text, mid))
        return FeishuResult(mid, {})


class FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid

    def terminate(self):
        pass


class FakeAdapter(CodexAdapter):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.started = []
        self.routes = []
        self.discovery = None

    def start_terminal(self, task_id, task_name, task, cwd):
        self.started.append(("terminal", task_id, task_name, task, cwd))
        return self.cfg.state_dir / "launch" / f"{task_id}.sh"

    def start_background(self, task_id, task_name, task, cwd):
        self.started.append(("background", task_id, task_name, task, cwd))
        return FakeProc()

    def wait_for_session(self, *args, **kwargs):
        return self.discovery

    def wait_for_logged_session(self, *args, **kwargs):
        return self.discovery.session_id if self.discovery else None

    def find_rollout(self, *args, **kwargs):
        return self.discovery.rollout_path if self.discovery else None

    def route_message(self, record, text):
        self.routes.append((record["id"], record["session_id"], text))
        return "queue", "queued"


class FakeSDKAdapter:
    def __init__(self, thread_id="01a16388-51f3-76f3-9be6-30b3e88e942d"):
        self.thread_id = thread_id
        self.runs = []

    def start_thread(self, task_name, cwd):
        return {"thread_id": self.thread_id}

    def run_thread(self, thread_id, text, cwd):
        self.runs.append((thread_id, text, cwd))
        return {
            "turn_id": "turn-1",
            "status": "completed",
            "final_response": f"done: {text}",
            "error": None,
        }

    def list_threads(self, cwd, limit=20):
        return [
            {
                "id": self.thread_id,
                "name": "Client history",
                "preview": "previous prompt",
                "cwd": cwd,
                "created_at": 1,
                "updated_at": 2,
                "status": "idle",
            }
        ]


def make_bridge(tmp: Path, *, local_only=True) -> Bridge:
    cfg = Config(
        app_id="cli_test",
        app_secret="secret",
        chat_id="oc_test",
        allowed_open_ids=["ou_test"],
        state_dir=tmp / "state",
        socket_path=tmp / "state" / "bridge.sock",
        send_enabled=True,
        startup_timeout_secs=1,
        poll_secs=0.05,
    )
    cfg.ensure_dirs()
    bridge = Bridge(cfg, local_only=local_only)
    bridge.feishu = FakeFeishu()
    bridge.adapter = FakeAdapter(cfg)
    return bridge


class ParseStartTests(unittest.TestCase):
    def test_explicit_commands(self):
        self.assertEqual(parse_start_command("/start terminal run tests")[:2], ("terminal", "run tests"))
        self.assertEqual(parse_start_command("start 后台 修复构建")[:2], ("background", "修复构建"))
        self.assertEqual(parse_start_command("/start client 分析项目")[:2], ("client", "分析项目"))

    def test_chinese_mode_prompt(self):
        mode, task, error = parse_start_command("帮我开个 codex")
        self.assertIsNone(mode)
        self.assertIsNone(task)
        self.assertIn("终端窗口", error)

    def test_chinese_with_task(self):
        mode, task, error = parse_start_command("帮我开个终端 codex，任务是跑回归")
        self.assertEqual(mode, "terminal")
        self.assertEqual(task, "跑回归")
        self.assertFalse(error)


class ParseHistoryTests(unittest.TestCase):
    def test_history_commands(self):
        self.assertEqual(parse_history_command("/history"), (True, None, ""))
        self.assertEqual(parse_history_command("历史任务"), (True, None, ""))
        self.assertEqual(
            parse_history_command("/history H-20261022-195038-8817"),
            (True, "H-20261022-195038-8817", ""),
        )
        self.assertEqual(parse_history_command("/history nope")[0], True)
        self.assertIn("/history", parse_history_command("/history nope")[2])
        self.assertEqual(parse_history_command("继续执行"), (False, None, ""))

    def test_client_thread_commands(self):
        self.assertEqual(parse_threads_command("/threads"), (True, ""))
        self.assertEqual(parse_threads_command("/threads 项目"), (True, "项目"))
        self.assertEqual(
            parse_resume_command(
                "/resume 01a16388-51f3-76f3-9be6-30b3e88e942d 继续测试"
            ),
            ("01a16388-51f3-76f3-9be6-30b3e88e942d", "继续测试"),
        )


class ClaimTests(unittest.TestCase):
    def test_dedupe_window(self):
        with tempfile.TemporaryDirectory() as td:
            claims = ClaimStore(Path(td), 600)
            self.assertTrue(claims.claim("a|b"))
            self.assertFalse(claims.claim("a|b"))
            self.assertTrue(claims.claim("a|c"))


class RecordTests(unittest.TestCase):
    def test_atomic_store_and_message_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            store = RecordStore(Path(td))
            store.put({"id": "H-1", "feishu_message_ids": ["om_1"]})
            self.assertEqual(store.get("H-1")["id"], "H-1")
            self.assertEqual(store.by_message_id("om_1")["id"], "H-1")
            with self.assertRaises(ValueError):
                store.get("../bad")

    def test_session_lookup_reuses_oldest_task(self):
        with tempfile.TemporaryDirectory() as td:
            store = RecordStore(Path(td))
            store.put({"id": "H-2", "session_id": "sid", "created_at": 2})
            store.put({"id": "H-1", "session_id": "sid", "created_at": 1})
            store.put({"id": "H-3", "session_id": "other", "created_at": 0})
            self.assertEqual(store.by_session_id("sid")["id"], "H-1")
            self.assertIsNone(store.by_session_id("missing"))


class BridgeTests(unittest.TestCase):
    def test_client_start_and_reply_use_sdk_backend(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            bridge._sdk_adapter = FakeSDKAdapter()
            result = bridge.handle_start(
                {"mode": "client", "task": "分析项目", "cwd": td}
            )
            task = result["record"]
            self.assertEqual(task["mode"], "client")
            self.assertEqual(task["session_id"], bridge._sdk_adapter.thread_id)
            self.assertEqual(result["feishu"]["status"], "sent")
            import time

            deadline = time.time() + 2
            while time.time() < deadline and not bridge._sdk_adapter.runs:
                time.sleep(0.02)
            bridge.handle_reply(task["id"], "继续修复")
            deadline = time.time() + 2
            while time.time() < deadline and len(bridge._sdk_adapter.runs) < 2:
                time.sleep(0.02)
            self.assertEqual(
                [run[:2] for run in bridge._sdk_adapter.runs],
                [
                    (bridge._sdk_adapter.thread_id, "分析项目"),
                    (bridge._sdk_adapter.thread_id, "继续修复"),
                ],
            )
            self.assertEqual(
                len({run[2] for run in bridge._sdk_adapter.runs}),
                1,
            )

    def test_threads_list_and_resume_command(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            sdk = FakeSDKAdapter()
            bridge._sdk_adapter = sdk
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": "/threads client",
                }
            )
            listing = bridge.feishu.calls[-1][1]
            self.assertIn("Client history", listing)
            self.assertIn(sdk.thread_id, listing)
            self.assertIn("继续线程：/resume", listing)

            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": f"/resume {sdk.thread_id} 继续历史会话",
                }
            )
            import time

            deadline = time.time() + 2
            while time.time() < deadline and not sdk.runs:
                time.sleep(0.02)
            self.assertEqual(
                sdk.runs[-1],
                (sdk.thread_id, "继续历史会话", str(bridge.cfg.default_cwd)),
            )

    def test_feishu_history_list_and_detail(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            first = bridge.create_record(
                "session-old",
                td,
                "attached",
                "旧任务",
                task_id="H-20261022-190000-0001",
            )
            second = bridge.create_record(
                "session-new",
                td,
                "terminal",
                "新任务",
                task_id="H-20261022-200000-0002",
            )
            bridge.store.update(first["id"], created_at=1, status="waiting")
            bridge.store.update(
                second["id"],
                created_at=2,
                status="running",
                last_output="这是详细结果",
            )
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": "/history",
                }
            )
            listing = bridge.feishu.calls[-1][1]
            self.assertIn("共 2 条", listing)
            self.assertLess(listing.index(second["id"]), listing.index(first["id"]))
            self.assertEqual(bridge.adapter.routes, [])

            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": f"/history {second['id']}",
                }
            )
            detail = bridge.feishu.calls[-1][1]
            self.assertIn("新任务", detail)
            self.assertIn("这是详细结果", detail)
            self.assertIn("terminal", detail)
            self.assertIn("直接回复本条消息可继续此会话", detail)
            self.assertEqual(bridge.adapter.routes, [])
            detail_message_id = bridge.feishu.calls[-1][2]
            self.assertIn(
                detail_message_id,
                bridge.store.get(second["id"])["feishu_message_ids"],
            )

            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "reply_to": detail_message_id,
                    "content": "继续执行下一步",
                }
            )
            self.assertEqual(
                bridge.adapter.routes,
                [(second["id"], "session-new", "继续执行下一步")],
            )

    def test_feishu_history_unknown_and_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": "/history H-20261022-195038-8817",
                }
            )
            self.assertIn("找不到历史任务", bridge.feishu.calls[-1][1])
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "content": "/history nope",
                }
            )
            self.assertIn("用法", bridge.feishu.calls[-1][1])
            self.assertEqual(bridge.adapter.routes, [])

    def test_send_is_deduped_and_titled_once(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            record = bridge.create_record("sid-1", "/tmp", "attached", "demo")
            first = bridge._send_record(record, "TITLE\n- one\n- two")
            second = bridge._send_record(record, "TITLE\n- one\n- two")
            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(len(bridge.feishu.calls), 1)
            call = bridge.feishu.calls[0]
            self.assertEqual(call[1], "TITLE")
            self.assertNotIn("TITLE", call[2])
            self.assertIn(record["id"], call[3])

    def test_failed_send_releases_claim(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            record = bridge.create_record("sid-1", "/tmp", "attached", "demo")
            failing = FakeFeishu()
            original = failing.send_post
            calls = {"n": 0}

            def send_post(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("network down")
                return original(*args, **kwargs)

            failing.send_post = send_post
            bridge.feishu = failing
            with self.assertRaisesRegex(RuntimeError, "network down"):
                bridge._send_record(record, "same")
            self.assertEqual(bridge._send_record(record, "same")["status"], "sent")

    def test_start_background_records_own_session(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            rollout = Path(td) / "rollout-test.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
            bridge.adapter.discovery = DiscoveryResult("01a-session", rollout, str(Path(td)))
            result = bridge.handle_start(
                {"mode": "background", "task": "say hello", "cwd": td}
            )
            self.assertEqual(result["status"], "started")
            record = result["record"]
            self.assertEqual(record["session_id"], "01a-session")
            self.assertEqual(record["mode"], "background")
            self.assertEqual(bridge.adapter.started[0][0], "background")

    def test_start_requires_mode(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td))
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                bridge.handle_start({"task": "x", "cwd": td})

    def test_two_session_reply_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            a = bridge.create_record("session-a", td, "attached", "A")
            b = bridge.create_record("session-b", td, "attached", "B")
            bridge.store.update(a["id"], feishu_message_ids=["om_a"])
            bridge.store.update(b["id"], feishu_message_ids=["om_b"])
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "reply_to": "om_a",
                    "content": "continue A",
                }
            )
            self.assertEqual(
                bridge.adapter.routes,
                [(a["id"], "session-a", "continue A")],
            )

    def test_send_from_existing_session_creates_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            rollout = Path(td) / "rollout-existing.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(bridge.adapter, "find_rollout", return_value=rollout), mock.patch.object(
                bridge.adapter, "latest_user_text", return_value="fix login"
            ):
                result = bridge.handle_send(
                    {
                        "session_id": "session-existing",
                        "cwd": td,
                        "text": "done",
                    }
                )
            self.assertEqual(result["status"], "sent")
            record = bridge.store.get(result["id"])
            self.assertEqual(record["task_name"], "fix login")
            self.assertEqual(record["session_id"], "session-existing")

    def test_send_reuses_session_task_without_env_task_id(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            rollout = Path(td) / "rollout-existing.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
            task = bridge.create_record(
                "session-existing",
                td,
                "terminal",
                "started from Feishu",
                task_id="H-20261022-195038-8817",
                rollout_path=rollout,
            )
            with mock.patch.object(bridge.adapter, "find_rollout", return_value=rollout):
                result = bridge.handle_send(
                    {
                        "session_id": "session-existing",
                        "cwd": td,
                        "text": "飞书桥接已连通",
                    }
                )
            self.assertEqual(result["id"], task["id"])
            self.assertEqual(len(bridge.store.list()), 1)
            record = bridge.store.get(task["id"])
            self.assertEqual(record["mode"], "terminal")
            self.assertEqual(record["task_name"], "started from Feishu")
            self.assertEqual(record["last_output"], "飞书桥接已连通")

    def test_unknown_reply_does_not_guess(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            bridge.handle_event(
                {
                    "sender_type": "user",
                    "sender_id": "ou_test",
                    "reply_to": "om_unknown",
                    "content": "do something",
                }
            )
            self.assertEqual(bridge.adapter.routes, [])
            self.assertEqual(bridge.feishu.calls[-1][0], "text")
            self.assertIn("H-编号", bridge.feishu.calls[-1][1])

    def test_completion_watcher_reads_last_agent_message(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = make_bridge(Path(td), local_only=False)
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": "FINAL_RESULT",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            record = bridge.create_record("sid-watch", td, "terminal", "watch", rollout_path=rollout)
            bridge.ensure_watcher(record, from_start=True)
            import time

            deadline = time.time() + 2
            while time.time() < deadline and not bridge.feishu.calls:
                time.sleep(0.02)
            bridge.stop()
            self.assertTrue(bridge.feishu.calls)
            self.assertEqual(bridge.feishu.calls[0][1], "FINAL_RESULT")


class AdapterTests(unittest.TestCase):
    def test_discover_tui_by_cwd_and_time(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "codex"
            session_dir = home / "sessions" / "2026" / "10" / "22"
            session_dir.mkdir(parents=True)
            rollout = session_dir / "rollout-2026-10-22T10-00-00-sid.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": "sid",
                            "id": "sid",
                            "cwd": td,
                            "originator": "codex-tui",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = Config(state_dir=Path(td) / "state", socket_path=Path(td) / "state/s.sock")
            adapter = CodexAdapter(cfg)
            result = adapter.discover_session(td, 0, home, require_tui=True)
            self.assertIsNotNone(result)
            self.assertEqual(result.session_id, "sid")

    def test_queue_then_resume_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(state_dir=Path(td) / "state", socket_path=Path(td) / "state/s.sock")
            adapter = CodexAdapter(cfg)
            with mock.patch.object(
                adapter, "queue_message", return_value=subprocess_result(1, "", "not running")
            ), mock.patch.object(adapter, "resume_message", return_value=FakeProc(999)) as resume:
                route, detail = adapter.route_message(
                    {"id": "H-1", "session_id": "sid", "cwd": td}, "go"
                )
            self.assertEqual(route, "resume")
            self.assertIn("pid=999", detail)
            resume.assert_called_once()

    def test_active_session_uses_queue(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(state_dir=Path(td) / "state", socket_path=Path(td) / "state/s.sock")
            adapter = CodexAdapter(cfg)
            with mock.patch.object(adapter, "session_is_active", return_value=True), mock.patch.object(
                adapter, "queue_message", return_value=subprocess_result(0, "queued", "")
            ), mock.patch.object(adapter, "resume_message") as resume:
                route, detail = adapter.route_message(
                    {"id": "H-1", "session_id": "sid", "cwd": td}, "go"
                )
            self.assertEqual(route, "queue")
            resume.assert_not_called()


def subprocess_result(rc, out, err):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


if __name__ == "__main__":
    unittest.main(verbosity=2)

