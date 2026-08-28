"""账号登出：左下角头像 → 退出登录。

只做「退出登录」这一个动作，绝不触碰其它菜单项。
登出后回到登录页，等待用户扫码登录下一个公众号。
"""
from __future__ import annotations

import logging
import time

from ..constants import MP_BASE_URL
from .selectors import AVATAR_SELECTORS, LOGOUT_MENU_TEXTS
from .session import BrowserSession, click_robust, is_real

logger = logging.getLogger(__name__)

# 登出确认弹窗按钮（文本「退出」或「退出登录」）
_LOGOUT_CONFIRM_TEXTS: tuple[str, ...] = ("退出登录", "退出", "确定")


def logout(session: BrowserSession) -> bool:
    """执行登出。返回 True 表示已回到登录页。"""
    tab = session.tab
    try:
        session.navigate(f"{MP_BASE_URL}/")
        session.wait_ready(timeout=15)
    except Exception as exc:  # noqa: BLE001
        logger.warning("登出前导航失败（继续尝试）: %s", exc)

    # 1. 点左下角头像
    avatar_clicked = False
    for sel in AVATAR_SELECTORS:
        try:
            el = tab.ele(sel, timeout=2)
            if is_real(el) and click_robust(el):
                avatar_clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not avatar_clicked:
        logger.warning("未找到头像入口，兜底：清空会话直达登录页")
        return _force_back_to_login(session)

    time.sleep(1.5)
    # 2. 点「退出登录」菜单
    for text in LOGOUT_MENU_TEXTS:
        try:
            el = tab.ele(f"@text()={text}", timeout=2)
            if is_real(el) and click_robust(el):
                time.sleep(1.0)
                break
        except Exception:  # noqa: BLE001
            continue

    # 3. 确认弹窗（如有）
    for text in _LOGOUT_CONFIRM_TEXTS:
        try:
            el = tab.ele(f"@@tag()=button@@text()={text}", timeout=1)
            if is_real(el) and click_robust(el):
                break
        except Exception:  # noqa: BLE001
            continue

    session.wait_ready(timeout=10)
    url = tab.url or ""
    if "loginpage" in url or "token=" not in url:
        logger.info("登出成功，已回到登录页")
        return True
    logger.warning("登出后未检测到登录页 url=%s（可能仍登录）", url[:60])
    return _force_back_to_login(session)


def _force_back_to_login(session: BrowserSession) -> bool:
    """兜底：导航到登录页并确认已离开后台。"""
    try:
        session.navigate(f"{MP_BASE_URL}/")
        session.wait_ready(timeout=15)
        return "cgi-bin/home" not in (session.tab.url or "")
    except Exception as exc:  # noqa: BLE001
        logger.error("兜底登出失败: %s", exc)
        return False
