"""🛡 安全红线守卫：URL 层拦截一切删除/编辑动作。

用户红线："请不要在微信公众号后台上执行任何删除、编辑动作，
只发送现有草稿文章和贴图。" —— 本模块把这句话变成代码。

两层拦截：
1. 子串级：路径/查询含 delete/remove/appmsg_edit 等关键词 → 阻断
2. 参数级：query 参数值恰为 del/batchdel/trash 等 → 阻断
   （防 "action=del" 这种值等于关键词但子串匹配不到的绕过）
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from ..constants import FORBIDDEN_URL_KEYWORDS

logger = logging.getLogger(__name__)


class SafetyViolationError(RuntimeError):
    """试图执行删除/编辑类动作，已被安全守卫拦截。"""


# query 参数值黑名单（精确匹配，小写）
_FORBIDDEN_PARAM_VALUES = frozenset({
    "del", "delete", "batchdel", "remove", "trash", "clear",
    "edit", "modify", "update", "revoke",
})


def assert_url_safe(url: str) -> None:
    """导航前检查：URL 含删除/编辑关键词或参数值 → 立即阻断。"""
    lowered = (url or "").lower()
    for kw in FORBIDDEN_URL_KEYWORDS:
        if kw in lowered:
            logger.error("🛡 安全守卫拦截危险URL（含 %r）: %s", kw, url)
            raise SafetyViolationError(
                f"安全红线：拒绝访问含「{kw}」的地址（永不删除/编辑）: {url}"
            )

    try:
        query = urlparse(lowered).query
        params = parse_qs(query)
    except ValueError:
        params = {}
    for key, values in params.items():
        for v in values:
            if v in _FORBIDDEN_PARAM_VALUES:
                logger.error("🛡 安全守卫拦截危险URL（参数 %s=%s）", key, v)
                raise SafetyViolationError(
                    f"安全红线：拒绝访问参数 {key}={v} 的地址（永不删除/编辑）: {url}"
                )


def assert_button_safe(text: str) -> None:
    """点击前检查：按钮文本含删除/编辑关键词 → 阻断。"""
    from ..constants import DANGEROUS_BUTTON_KEYWORDS
    t = text or ""
    for kw in DANGEROUS_BUTTON_KEYWORDS:
        if kw in t:
            logger.error("🛡 安全守卫拦截危险按钮（含 %r）: %s", kw, t)
            raise SafetyViolationError(
                f"安全红线：拒绝点击含「{kw}」的按钮: {t}"
            )
