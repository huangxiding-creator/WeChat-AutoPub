"""企业微信 Webhook 通知。

- send_text: 纯文本（适合手机推送提醒）
- send_markdown: markdown 摘要（适合日报）
- 通知失败绝不抛出（通知不能影响主流程），仅记录日志
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


class WecomNotifier:
    """企微群机器人通知器。"""

    def __init__(self, webhook: str, enabled: bool = True) -> None:
        if enabled and not webhook:
            raise ValueError("企微 Webhook 未配置：请在 config.ini [通知] 填写企微Webhook")
        self._webhook = webhook
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._webhook)

    def _post(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            logger.debug("通知已关闭，跳过: %s", payload)
            return False
        try:
            resp = requests.post(self._webhook, json=payload, timeout=_TIMEOUT_SECONDS)
            data = resp.json()
            if data.get("errcode") == 0:
                return True
            logger.warning("企微通知被拒绝: %s", data)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("企微通知发送失败: %s", exc)
        return False

    def send_text(self, text: str, mentioned: bool = False) -> bool:
        """发送文本消息；mentioned=True 时 @所有人。"""
        return self._post({
            "msgtype": "text",
            "text": {
                "content": text,
                "mentioned_list": ["@all"] if mentioned else [],
            },
        })

    def send_markdown(self, markdown: str) -> bool:
        """发送 markdown 消息（日报/战报）。"""
        return self._post({"msgtype": "markdown", "markdown": {"content": markdown}})

    def send_action_needed(self, action: str, detail: str = "") -> bool:
        """需要用户动手的提醒（扫码等），@所有人确保看到。"""
        lines = [f"📢 **WeChat-AutoPub 需要您操作**", "", f"**{action}**"]
        if detail:
            lines += ["", detail]
        return self.send_markdown("\n".join(lines))
