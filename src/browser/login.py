"""扫码登录流程。

公众号后台（mp.weixin.qq.com）登录机制：
- 首页直接展示二维码，扫码 + 手机确认后跳转 /cgi-bin/home?...&token=XXXX
- cookie 持久化在每账号独立 profile 里，多数日子免扫码直接进
- 扫码无法自动化（微信风控）→ 企微通知用户来扫（on_action_needed 回调）

token 提取：URL 正则 token=(\\d+)，后续 CGI 快车道复用。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from .session import BrowserSession, is_real

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"token=(\d+)")

# 昵称提取多策略（DOM 选择器 → JS 变量 → 兜底）
_NICKNAME_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop_account__nickname",
    "css:.weui-desktop_name",
    "css:.account_box .nickname",
)
_NICKNAME_JS = (
    "return (window.wx && wx.commonData && wx.commonData.data "
    "&& wx.commonData.data.nick_name) || '';"
)


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    nickname: str = ""
    token: str = ""
    detail: str = ""


def extract_token(url: str) -> str:
    m = _TOKEN_RE.search(url or "")
    return m.group(1) if m else ""


def extract_nickname(session: BrowserSession) -> str:
    """多策略提取当前登录账号昵称。"""
    tab = session.tab
    for sel in _NICKNAME_SELECTORS:
        try:
            el = tab.ele(sel, timeout=1)
            if is_real(el) and el.text and el.text.strip():
                return el.text.strip()
        except Exception:  # noqa: BLE001
            continue
    try:
        val = session.run_js(_NICKNAME_JS)
        if isinstance(val, str) and val.strip():
            return val.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def ensure_login(
    session: BrowserSession,
    *,
    timeout_minutes: int = 30,
    poll_interval: float = 3.0,
    remind_interval: float = 120.0,
    on_action_needed: Callable[[str, str], None] | None = None,
) -> LoginResult:
    """确保已登录。cookie 有效直接复用；否则等扫码（企微提醒）。

    on_action_needed(action, detail)：触发时通知用户（企微）。
    """
    if session.is_logged_in():
        # cookie 复用路径同样清场僵尸 tab（旧 token 轮询会掐死本会话）
        session.close_stale_mp_tabs(keep=session.tab)
        session.retarget_capture()
        url = session.tab.url or ""
        token = extract_token(url)
        nickname = extract_nickname(session)
        # cookie 复用路径同样可能弹「选择账号登录」
        try:
            from . import nav
            nav.dismiss_account_picker(session.tab, nickname)
        except Exception as exc:  # noqa: BLE001
            logger.debug("账号选择弹窗检查失败: %s", exc)
        logger.info("cookie 有效，免扫码登录 nickname=%s token=%s", nickname, token[:6] + "…")
        session.minimize_window()          # 不打扰用户桌面
        return LoginResult(ok=True, nickname=nickname, token=token)

    def _notify(action: str, detail: str) -> None:
        if on_action_needed:
            try:
                on_action_needed(action, detail)
            except Exception as exc:  # noqa: BLE001 — 通知失败不影响登录等待
                logger.warning("on_action_needed 回调失败: %s", exc)

    # 扫码前清场：旧会话的 mp tab 带旧 token 轮询，会让平台把
    # 新登录当冲突掐死（实战：会话 2 分钟暴毙且越来越快）
    session.close_stale_mp_tabs(keep=session.tab)

    # 一键「登录」快路径（2026-08-28 实战发现）：会话过期落到登录页时，
    # 页面上有沿用上次身份的「登录」按钮（真按钮，非「使用账号登录」），
    # 点它可免扫码恢复会话。8 秒内没进 home 再走扫码提醒。
    try:
        from . import nav
        if nav.js_click_visible_text(session.tab, "登录", timeout=4):
            time.sleep(8)
            if session.is_logged_in():
                url = session.tab.url or ""
                nickname = extract_nickname(session)
                logger.info("一键「登录」恢复会话成功 nickname=%s", nickname)
                session.retarget_capture()
                session.minimize_window()
                return LoginResult(ok=True, nickname=nickname,
                                   token=extract_token(url))
            logger.info("一键「登录」未恢复会话，转扫码流程")
    except Exception as exc:  # noqa: BLE001 — 快路径失败不阻塞扫码
        logger.debug("一键登录尝试失败: %s", exc)

    _notify("请扫码登录公众号", "浏览器已打开公众号后台，请用微信扫码并确认登录。")

    deadline = time.time() + timeout_minutes * 60
    last_remind = time.time()
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            url = session.tab.url or ""
        except Exception:  # noqa: BLE001
            continue
        token = extract_token(url)
        if token and "cgi-bin/home" in url:
            nickname = extract_nickname(session)
            # 微信绑定多公众号时，登录后可能弹「选择账号登录」→ 自动选目标账号
            try:
                from . import nav
                nav.dismiss_account_picker(session.tab, nickname)
                nickname = extract_nickname(session) or nickname
            except Exception as exc:  # noqa: BLE001 — 弹窗处理失败不阻塞登录
                logger.debug("登录后账号选择弹窗检查失败: %s", exc)
            logger.info("扫码登录成功 nickname=%s token=%s", nickname, token[:6] + "…")
            session.retarget_capture()      # 监听挂到登录后的活跃 tab
            session.minimize_window()       # 扫码完成，归还桌面
            return LoginResult(ok=True, nickname=nickname, token=token)
        if time.time() - last_remind >= remind_interval:
            remain = int((deadline - time.time()) / 60)
            _notify("仍在等待扫码", f"距超时还有 {remain} 分钟，请扫码登录公众号。")
            last_remind = time.time()

    logger.error("登录超时（%d 分钟）", timeout_minutes)
    return LoginResult(ok=False, detail=f"等待扫码超时（{timeout_minutes}分钟）")
