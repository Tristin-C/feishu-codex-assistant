#!/usr/bin/env python3
"""Minimal Feishu OpenAPI client for rich-post messages and task replies."""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List

from handoff_config import Config


@dataclass
class FeishuResult:
    message_id: str
    raw: Dict


CITATION_RE = re.compile(
    r':codex-file-citation\{path="([^"]+)"[^}]*\}',
    re.IGNORECASE,
)


def _card_text(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _result_summary(body: str, limit: int = 420) -> str:
    bullet_lines = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*[-*+]\s+(.+)$", body)
    ]
    if len(bullet_lines) >= 2:
        summary = "\n".join(f"- {line}" for line in bullet_lines[:3])
    else:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
        summary = paragraphs[0] if paragraphs else body
    if len(summary) > limit:
        summary = summary[:limit].rstrip() + "…"
    return summary


def build_result_card(title: str, body_lines: List[str], footer: str) -> Dict:
    """Build a compact Card 2.0 result while preserving reply routing."""
    title = str(title or "Codex 结果").strip()[:100]
    body = "\n".join(line for line in body_lines if line.strip())
    body = CITATION_RE.sub(lambda match: f"来源：`{match.group(1)}`", body)
    if len(body) > 7000:
        body = body[:7000].rstrip() + "\n\n…（内容过长，已截断）"

    lowered = title.casefold()
    if any(word in lowered for word in ("失败", "错误", "error", "failed")):
        template = "red"
        subtitle = "Codex · 执行失败"
    elif "已启动" in title:
        template = "blue"
        subtitle = "Codex · 任务已启动"
    else:
        template = "green"
        subtitle = "Codex · 结果回传"

    parts = [part.strip() for part in str(footer or "").split(" · ") if part.strip()]
    task_id = parts[0] if parts else ""
    mode = next((part.removeprefix("模式 ") for part in parts if part.startswith("模式 ")), "unknown")
    session = next((part.removeprefix("会话 ") for part in parts if part.startswith("会话 ")), "pending")
    cwd = parts[-1] if parts else ""

    elements: List[Dict] = []
    line_count = len([line for line in body.splitlines() if line.strip()])
    is_long = len(body) > 420 or line_count > 6
    if body and is_long:
        summary = _result_summary(body)
        elements.append(
            {
                "tag": "column_set",
                "element_id": "result_summary",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "background_style": f"{template}-50",
                        "corner_radius": "8px",
                        "padding": "12px",
                        "vertical_spacing": "4px",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": (
                                    f"**<font color='{template}'>核心内容</font>**\n"
                                    f"{summary}"
                                ),
                            }
                        ],
                    }
                ],
            }
        )
        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": "full_result",
                "expanded": False,
                "background_color": "grey-50",
                "border": {"color": "grey-200", "corner_radius": "8px"},
                "padding": "12px",
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"查看完整结果 · {line_count} 行",
                    }
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body,
                    }
                ],
            }
        )
    elif body:
        elements.append(
            {
                "tag": "column_set",
                "element_id": "result_summary",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "background_style": f"{template}-50",
                        "corner_radius": "8px",
                        "padding": "12px",
                        "vertical_spacing": "4px",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": (
                                    f"**<font color='{template}'>核心内容</font>**\n"
                                    f"{body}"
                                ),
                            }
                        ],
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "div",
            "element_id": "task_meta",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**任务**\n`{_card_text(task_id)}`",
                        "lines": 1,
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**模式**\n{_card_text(mode)}",
                        "lines": 1,
                    },
                },
                {
                    "is_short": False,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**会话**\n`{_card_text(session)}`",
                        "lines": 2,
                    },
                },
                {
                    "is_short": False,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**目录**\n`{_card_text(cwd)}`",
                        "lines": 2,
                    },
                },
            ],
        }
    )
    elements.append(
        {
            "tag": "markdown",
            "element_id": "reply_hint",
            "content": "<font color='grey'>回复本卡继续此线程</font>",
            "text_size": "notation",
        }
    )

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": title},
        },
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
            "icon": {"tag": "standard_icon", "token": "ai-common_colorful"},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": "Codex"},
                    "color": template,
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


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
        card = build_result_card(title, body_lines, footer)
        response = self._call(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": self.cfg.chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
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

