#!/usr/bin/env python3
"""Minimal Feishu OpenAPI client for rich-post messages and task replies."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List

from handoff_config import Config


@dataclass
class FeishuResult:
    message_id: str
    raw: Dict


class FeishuClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token = ""
        self._token_expires = 0.0

    def _call(self, url: str, payload: Dict, token: str = "") -> Dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Feishu network error: {exc}") from exc

    def tenant_token(self) -> str:
        import time

        if self._token and time.time() < self._token_expires:
            return self._token
        response = self._call(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.cfg.app_id, "app_secret": self.cfg.app_secret},
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Feishu token failed: {response.get('code')} {response.get('msg')}")
        self._token = response["tenant_access_token"]
        self._token_expires = time.time() + max(60, int(response.get("expire", 7200)) - 120)
        return self._token

    def send_post(self, title: str, body_lines: List[str], footer: str) -> FeishuResult:
        if not self.cfg.send_enabled:
            raise RuntimeError("Feishu sending is disabled")
        content_rows = []
        for line in body_lines:
            if line.strip():
                shown = line
                if line.lstrip().startswith(("- ", "* ")):
                    shown = "• " + line.lstrip()[2:]
                content_rows.append([{"tag": "text", "text": shown}])
        content_rows.append([{"tag": "text", "text": "────────"}])
        content_rows.append([{"tag": "text", "text": footer}])
        content_rows.append([{"tag": "text", "text": "回复本条消息继续"}])
        post = {
            "zh_cn": {
                "title": title[:100],
                "content": content_rows,
            }
        }
        response = self._call(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": self.cfg.chat_id,
                "msg_type": "post",
                "content": json.dumps(post, ensure_ascii=False),
            },
            token=self.tenant_token(),
        )
        if response.get("code") != 0:
            raise RuntimeError(
                f"Feishu send failed: {response.get('code')} {response.get('msg')} "
                f"{response.get('error', {})}"
            )
        data = response.get("data") or {}
        return FeishuResult(message_id=str(data.get("message_id", "")), raw=response)

    def send_text(self, text: str) -> FeishuResult:
        if not self.cfg.send_enabled:
            raise RuntimeError("Feishu sending is disabled")
        response = self._call(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": self.cfg.chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token=self.tenant_token(),
        )
        if response.get("code") != 0:
            raise RuntimeError(f"Feishu send failed: {response.get('code')} {response.get('msg')}")
        data = response.get("data") or {}
        return FeishuResult(message_id=str(data.get("message_id", "")), raw=response)

