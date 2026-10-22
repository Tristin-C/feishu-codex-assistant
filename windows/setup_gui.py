#!/usr/bin/env python3
"""Windows setup wizard for the Feishu Codex bridge."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox


APP_DIR = Path(__file__).resolve().parent
PROFILE = "feishu-codex-bridge"
URL_RE = re.compile(r"https://[^\s\"']+")


def cli_command(path: str, *args: str) -> list[str]:
    if os.name == "nt" and Path(path).suffix.lower() in {".cmd", ".bat"}:
        return ["cmd.exe", "/d", "/c", path, *args]
    return [path, *args]


def find_lark_cli() -> str:
    candidates = [
        os.environ.get("HANDOFF_LARK_CLI", ""),
        shutil.which("lark-cli.cmd") or "",
        shutil.which("lark-cli") or "",
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "lark-cli.cmd"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    raise RuntimeError("未找到 lark-cli，请重新运行安装程序")


class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("飞书 × Codex 一键配置")
        self.geometry("760x680")
        self.minsize(700, 620)
        self.configure(bg="#f5f6f8")
        self._qr_photo = None
        self._busy = False
        self.cwd_var = tk.StringVar(value=str(Path.home() / "Documents"))
        self.status_var = tk.StringVar(value="准备开始")
        self.url_var = tk.StringVar(value="")
        self._build_ui()

    def _build_ui(self) -> None:
        outer = tk.Frame(self, bg="#f5f6f8", padx=28, pady=24)
        outer.pack(fill="both", expand=True)
        tk.Label(
            outer,
            text="飞书 × Codex",
            font=("Microsoft YaHei UI", 24, "bold"),
            bg="#f5f6f8",
            fg="#1f2328",
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="一次性完成机器人创建、Codex 登录、飞书配对和开机启动。",
            font=("Microsoft YaHei UI", 10),
            bg="#f5f6f8",
            fg="#57606a",
        ).pack(anchor="w", pady=(4, 18))

        card = tk.Frame(outer, bg="white", highlightthickness=1, highlightbackground="#d8dee4", padx=22, pady=20)
        card.pack(fill="x")
        tk.Label(card, text="默认工作目录", font=("Microsoft YaHei UI", 10, "bold"), bg="white").pack(anchor="w")
        row = tk.Frame(card, bg="white")
        row.pack(fill="x", pady=(7, 16))
        entry = tk.Entry(row, textvariable=self.cwd_var, font=("Microsoft YaHei UI", 10))
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(row, text="选择文件夹", command=self.choose_folder).pack(side="left", padx=(8, 0))

        self.start_button = tk.Button(
            card,
            text="开始一键安装",
            command=self.start_setup,
            font=("Microsoft YaHei UI", 11, "bold"),
            bg="#1677ff",
            fg="white",
            activebackground="#0e63d8",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=9,
        )
        self.start_button.pack(anchor="w")

        tk.Label(outer, textvariable=self.status_var, font=("Microsoft YaHei UI", 10), bg="#f5f6f8", fg="#57606a").pack(
            anchor="w", pady=(16, 6)
        )
        tk.Label(
            outer,
            textvariable=self.url_var,
            font=("Consolas", 9),
            bg="#f5f6f8",
            fg="#0969da",
            wraplength=690,
            justify="left",
        ).pack(anchor="w")
        self.qr_label = tk.Label(outer, bg="#f5f6f8")
        self.qr_label.pack(pady=10)
        self.log = tk.Text(
            outer,
            height=12,
            font=("Microsoft YaHei UI", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.log.pack(fill="both", expand=True, pady=(10, 0))
        self._append("请确认 Codex 已安装并登录。若未登录，稍后会打开浏览器。")
        self._append("完成授权后，私聊机器人发送任意消息即可完成配对。")

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.cwd_var.get() or str(Path.home()))
        if selected:
            self.cwd_var.set(selected)

    def ui(self, fn, *args) -> None:
        self.after(0, lambda: fn(*args))

    def _append(self, text: str) -> None:
        self.log.insert("end", f"{text}\n")
        self.log.see("end")

    def append(self, text: str) -> None:
        self._append(text)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def show_url(self, url: str) -> None:
        self.url_var.set(url)
        self.clipboard_clear()
        self.clipboard_append(url)
        self.append("授权链接已复制到剪贴板，二维码如下。")

    def show_qr(self, image_path: Path) -> None:
        if not image_path.exists():
            return
        try:
            photo = tk.PhotoImage(file=str(image_path))
            if photo.width() > 280:
                factor = max(1, photo.width() // 280)
                photo = photo.subsample(factor, factor)
            self._qr_photo = photo
            self.qr_label.configure(image=photo)
        except Exception as exc:
            self.append(f"二维码显示失败，请使用上方链接：{exc}")

    def start_setup(self) -> None:
        if self._busy:
            return
        cwd = Path(self.cwd_var.get()).expanduser()
        if not cwd.is_dir():
            messagebox.showerror("目录无效", "请选择一个存在的工作目录。")
            return
        self._busy = True
        self.start_button.configure(state="disabled", text="正在安装…")
        threading.Thread(target=self.run_setup, args=(cwd,), daemon=True).start()

    def run_cli_capture(self, cmd: list[str], timeout: float | None = None, input_text: str | None = None):
        return subprocess.run(
            cmd,
            cwd=APP_DIR,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def ensure_codex_auth(self) -> None:
        self.ui(self.set_status, "检查 Codex 登录状态…")
        from openai_codex import Codex

        with Codex() as codex:
            account = codex.account()
            if account.requires_openai_auth and account.account is None:
                self.ui(self.append, "Codex 尚未登录，正在打开浏览器…")
                handle = codex.login_chatgpt()
                self.ui(self.show_url, handle.auth_url)
                handle.wait()
                account = codex.account()
                if account.requires_openai_auth and account.account is None:
                    raise RuntimeError("Codex 登录未完成")
            self.ui(self.append, "Codex 登录状态正常。")

    def create_feishu_app(self, lark_cli: str) -> None:
        self.ui(self.set_status, "正在创建飞书机器人，请在浏览器完成授权…")
        cmd = cli_command(
            lark_cli,
            "--profile",
            PROFILE,
            "config",
            "init",
            "--new",
            "--name",
            PROFILE,
            "--lang",
            "zh_cn",
        )
        proc = subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output_queue: queue.Queue[tuple[str, str]] = queue.Queue()

        def read_stream(stream, prefix: str) -> None:
            if stream is None:
                return
            for line in stream:
                output_queue.put((prefix, line.rstrip()))

        threading.Thread(target=read_stream, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=read_stream, args=(proc.stderr, "err"), daemon=True).start()
        found_url = ""
        deadline = time.time() + 600
        while proc.poll() is None and time.time() < deadline:
            try:
                _, line = output_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            match = URL_RE.search(line)
            if match and not found_url:
                found_url = match.group(0).rstrip(".,);")
                self.ui(self.show_url, found_url)
                try:
                    qr = self.run_cli_capture(
                        cli_command(
                            lark_cli,
                            "auth",
                            "qrcode",
                            found_url,
                            "--output",
                            "feishu-setup-qr.png",
                        ),
                        timeout=30,
                    )
                    if qr.returncode == 0:
                        self.ui(self.show_qr, APP_DIR / "feishu-setup-qr.png")
                except Exception as exc:
                    self.ui(self.append, f"二维码生成失败：{exc}")
            elif line and not any(x in line.lower() for x in ("secret", "token")):
                self.ui(self.append, line[:500])
        if proc.poll() is None:
            proc.terminate()
            raise RuntimeError("飞书应用创建超时")
        if proc.returncode != 0:
            raise RuntimeError("飞书应用创建失败，请检查浏览器授权后重试")
        self.ui(self.append, "飞书应用创建完成。")

    def read_feishu_config(self, lark_cli: str) -> dict:
        result = self.run_cli_capture(
            cli_command(lark_cli, "--profile", PROFILE, "config", "show"),
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("读取飞书应用配置失败")
        data = json.loads(result.stdout)
        if not data.get("appId") or not data.get("appSecret"):
            raise RuntimeError("飞书配置缺少 appId/appSecret")
        return data

    def pair_chat(self, lark_cli: str) -> tuple[str, str]:
        self.ui(self.set_status, "请在飞书私聊机器人发送任意消息…")
        self.ui(self.append, "正在等待飞书消息，超时时间 5 分钟。")
        result = self.run_cli_capture(
            cli_command(
                lark_cli,
                "--profile",
                PROFILE,
                "event",
                "consume",
                "im.message.receive_v1",
                "--as",
                "bot",
                "--max-events",
                "1",
                "--timeout",
                "5m",
            ),
            timeout=330,
            input_text="",
        )
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("sender_type") == "user" and event.get("sender_id") and event.get("chat_id"):
                self.ui(self.append, f"配对成功：{event.get('chat_id')}")
                return str(event["sender_id"]), str(event["chat_id"])
        detail = (result.stderr or "")[-800:]
        raise RuntimeError(f"没有收到飞书配对消息。{detail}")

    def free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def write_config(self, feishu: dict, open_id: str, chat_id: str, cwd: Path, lark_cli: str) -> int:
        port = self.free_port()
        config = f'''[feishu]
app_id = {json.dumps(feishu["appId"])}
app_secret = {json.dumps(feishu["appSecret"])}
chat_id = {json.dumps(chat_id)}
allowed_open_ids = [{json.dumps(open_id)}]
profile = {json.dumps(PROFILE)}
lark_cli = {json.dumps(lark_cli)}
send_enabled = true

[bridge]
state_dir = {json.dumps(str(APP_DIR / "state"))}
socket_path = {json.dumps(str(APP_DIR / "state" / "bridge.sock"))}
ipc_host = "127.0.0.1"
ipc_port = {port}
codex_bin = "codex"
default_cwd = {json.dumps(str(cwd))}
full_access = false
startup_timeout_secs = 30
poll_secs = 1
dedupe_window_secs = 600
'''
        path = APP_DIR / "config.toml"
        path.write_text(config, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return port

    def start_bridge(self, port: int) -> None:
        self.ui(self.set_status, "正在启动后台服务…")
        subprocess.Popen(
            ["wscript.exe", str(APP_DIR / "run_bridge.vbs")],
            cwd=APP_DIR,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    self.ui(self.append, "后台服务已启动。")
                    return
            except OSError:
                time.sleep(1)
        raise RuntimeError("后台服务启动超时，请查看 state/logs/bridge.log")

    def send_test_message(self) -> None:
        from feishu_client import FeishuClient
        from handoff_config import Config

        cfg = Config.load(APP_DIR / "config.toml")
        FeishuClient(cfg).send_text(
            "飞书 × Codex 助手安装成功。\n"
            "新建客户端任务：/start client 任务内容\n"
            "查看客户端线程：/threads"
        )

    def run_setup(self, cwd: Path) -> None:
        try:
            lark_cli = find_lark_cli()
            self.ensure_codex_auth()
            self.create_feishu_app(lark_cli)
            self.ui(self.set_status, "正在读取机器人配置…")
            feishu = self.read_feishu_config(lark_cli)
            open_id, chat_id = self.pair_chat(lark_cli)
            port = self.write_config(feishu, open_id, chat_id, cwd, lark_cli)
            self.start_bridge(port)
            self.send_test_message()
            self.ui(self.set_status, "安装完成，可以关闭此窗口")
            self.ui(self.append, "已注册开机启动；以后无需打开终端。")
            self.ui(messagebox.showinfo, "安装完成", "飞书 × Codex 助手已安装并开始后台运行。")
        except Exception as exc:
            self.ui(self.set_status, "安装失败")
            self.ui(self.append, f"错误：{exc}")
            self.ui(messagebox.showerror, "安装失败", str(exc))
        finally:
            self._busy = False
            self.ui(self.start_button.configure, state="normal", text="重新尝试")


if __name__ == "__main__":
    app = SetupApp()
    app.mainloop()

