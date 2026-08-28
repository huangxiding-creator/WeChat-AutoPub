"""智谱 GLM 客户端：只用免费模型（硬白名单），轮换 + 失败降级。

用途：
- 贴图「草稿加载中」智能等待判断（页面状态文本分类）
- 发布失败异常分类（风控/网络/内容）
- 企微日报润色
红线：模型名必须命中 ALLOWED_FREE_MODELS，收费模型物理不可达。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Optional

import requests

from ..config import LLMConfig
from ..constants import ALLOWED_FREE_MODELS

logger = logging.getLogger(__name__)

_ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
_TIMEOUT = 30


class LLMError(RuntimeError):
    pass


class ZhipuClient:
    """免费模型轮换客户端。"""

    def __init__(self, cfg: LLMConfig) -> None:
        bad = [m for m in cfg.免费模型 if m not in ALLOWED_FREE_MODELS]
        if bad:
            raise LLMError(f"非白名单模型 {bad}：只允许 {sorted(ALLOWED_FREE_MODELS)}")
        if not cfg.智谱Key:
            raise LLMError("未配置智谱Key")
        self._cfg = cfg
        self._token: Optional[str] = None
        self._token_expire: float = 0

    # —— 对外 ——

    def chat(self, system: str, user: str, temperature: float = 0.3,
             max_tokens: int = 1024) -> str:
        """轮换免费模型调用；全部失败抛 LLMError。"""
        last_err: Optional[Exception] = None
        for model in self._cfg.免费模型:
            try:
                return self._call(model, system, user, temperature, max_tokens)
            except Exception as exc:  # noqa: BLE001 — 轮换下一个
                logger.debug("模型 %s 失败: %s", model, exc)
                last_err = exc
        raise LLMError(f"所有免费模型调用失败: {last_err}")

    def chat_json(self, system: str, user: str) -> Any:
        return _extract_json(self.chat(system, user))

    def classify(self, text: str, labels: tuple[str, ...]) -> str:
        """把文本分类到给定标签（用于等待判断/异常分类）。失败返回空。"""
        prompt = (
            f"把下面的页面状态文本分类为其中一个标签：{'、'.join(labels)}。"
            '只输出标签本身，不要其它内容。\n\n文本：' + text[:500]
        )
        try:
            answer = self.chat("你是严格的分类器", prompt, temperature=0.0, max_tokens=20)
            for label in labels:
                if label in answer:
                    return label
        except LLMError as exc:
            logger.debug("分类失败: %s", exc)
        return ""

    # —— 内部 ——

    def _call(self, model: str, system: str, user: str,
              temperature: float, max_tokens: int) -> str:
        headers = self._headers()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = requests.post(_ZHIPU_URL, json=payload, headers=headers, timeout=_TIMEOUT)
        data = resp.json()
        if "error" in data:
            raise LLMError(f"GLM API: {data['error']}")
        return data["choices"][0]["message"]["content"]

    def _headers(self) -> dict[str, str]:
        """JWT token（1小时缓存）；无 PyJWT 时直接用原始 Key。"""
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._auth_headers(self._token)

        key = self._cfg.智谱Key
        parts = key.split(".")
        if len(parts) != 2:
            return self._auth_headers(key)

        try:
            import jwt as _jwt
            payload = {
                "api_key": parts[0],
                "exp": int(now * 1000) + 3600 * 1000,
                "timestamp": int(now * 1000),
            }
            token = _jwt.encode(
                payload, parts[1], algorithm="HS256",
                headers={"alg": "HS256", "sign_type": "SIGN"},
            )
        except ImportError:
            token = key

        self._token = token
        self._token_expire = now + 3600
        return self._auth_headers(token)

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _extract_json(text: str) -> Any:
    """从 LLM 输出提取 JSON（剥 markdown 代码块 + 子串兜底）。"""
    cleaned = re.sub(r"```(?:json)?\s*\n?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(open_ch), cleaned.rfind(close_ch)
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"JSON解析失败: {text[:200]}")
