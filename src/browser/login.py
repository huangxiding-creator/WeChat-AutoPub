"""扫码登录流程。

公众号后台（mp.weixin.qq.com）登录机制：
- 首页直接展示二维码，扫码 + 手机确认后跳转 /cgi-bin/home?...&token=XXXX
- cookie 持久化在每账号独立 profile 里，多数日子免扫码直接进
- 扫码无法自动化（微信风控）→ 企微通知用户来扫（on_action_needed 回调）

token 提取：URL 正则 token=(\\d+)，后续 CGI 快车道复用。
"""
from __future__ import annotations

import logging
import random
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


def preflight_logins(
    cfg,
    state,
    profiles: list[str],
    notifier=None,
    timeout_minutes: int = 30,
    wait_scan: bool = True,
) -> dict[str, str]:
    """逐档案核登录态。两种模式（2026-09-01 用户指令）：

    wait_scan=True（运行启动预检）：失效→企微提醒+二维码当场等扫，
      扫完才进发布；超时未扫→跳过该档案不断链。
    wait_scan=False（保活巡检）：失效→只尝试一键「登录」恢复，救不回
      即记入失效清单（由调用方汇总告警），不等待扫码。
    共同点：cookie 有效直接 ✓；等待/检测过程绝不重复导航刷新页面。
    返回 {profile: 昵称}，昵称为空=该档案仍失效。
    """
    from . import nav

    results: dict[str, str] = {}
    for profile in profiles:
        try:
            session = BrowserSession(cfg, profile)
            session.start()
        except RuntimeError as exc:
            logger.warning("[预检] %s 浏览器启动失败: %s", profile, exc)
            results[profile] = ""
            continue
        try:
            logged = session.is_logged_in()
            if not logged:
                logger.info("[预检] %s 登录态失效，尝试一键「登录」恢复", profile)
                try:
                    if nav.js_click_visible_text(session.tab, "登录", timeout=4):
                        time.sleep(8)          # 一键登录生效窗口（实战 8 秒）
                        logged = session.is_logged_in()
                except Exception as exc:       # noqa: BLE001 — 快路径失败走扫码
                    logger.debug("[预检] 一键恢复尝试失败: %s", exc)
            if not logged and wait_scan:
                logger.warning("[预检] %s 需扫码（等待 %d 分钟）", profile,
                               timeout_minutes)
                if notifier:
                    notifier.send_action_needed(
                        "请扫码登录公众号",
                        f"启动预检：{profile} 登录态失效，请扫码恢复"
                        f"（{timeout_minutes} 分钟内），扫完自动继续。")
                deadline = time.time() + timeout_minutes * 60
                while time.time() < deadline:
                    time.sleep(3)
                    # 等待期绝不导航（is_logged_in 会导航首页把二维码刷掉，
                    # 用户无法扫——09-01 实战踩坑）。纯被动读 URL 判跳转
                    try:
                        url = session.tab.url or ""
                    except Exception:          # noqa: BLE001
                        url = ""
                    if "cgi-bin/home" in url or "token=" in url:
                        logged = True
                        break
            if logged:
                nickname = extract_nickname(session)
                if not nickname:
                    time.sleep(2)
                    nickname = extract_nickname(session)   # 昵称渲染稍慢再取一次
                state.register_profile(profile, nickname)
                logger.info("[预检] %s 登录有效 nickname=%s", profile, nickname)
                results[profile] = nickname
                try:
                    session.minimize_window()   # 扫码完成归还桌面
                except Exception:  # noqa: BLE001
                    pass
            else:
                logger.warning("[预检] %s 仍失效%s，跳过该档案", profile,
                               "（超时未扫码）" if wait_scan
                               else "（一键恢复未成功）")
                results[profile] = ""
        finally:
            session.stop()       # 只断开接管，浏览器进程保留（登录态常驻）
        time.sleep(random.uniform(1, 3))        # 账号间拟人间隔
    alive = [p for p, nick in results.items() if nick]
    logger.info("[预检] 完成：%d/%d 有效（失效跳过：%s）", len(alive),
                len(profiles), "、".join(p for p in profiles if not results.get(p))
                or "无")
    return results


def ensure_login(
    session: BrowserSession,
    *,
    timeout_minutes: int = 30,
    poll_interval: float = 3.0,
    remind_interval: float = 120.0,
    on_action_needed: Callable[[str, str], None] | None = None,
    target_nickname: str = "",
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
            nav.dismiss_account_picker(session.tab, target_nickname or nickname)
        except Exception as exc:  # noqa: BLE001
            logger.debug("账号选择弹窗检查失败: %s", exc)
        logger.info("cookie 有效，免扫码登录 nickname=%s token=%s", nickname, token[:6] + "…")
        session.minimize_window()
        session.start_minimize_watchdog()          # 不打扰用户桌面
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
                session.start_minimize_watchdog()
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
        if target_nickname:
            # 扫码确认后可能弹「选择账号登录」——自动选目标号
            try:
                from . import nav
                nav.dismiss_account_picker(session.tab, target_nickname,
                                           timeout=2)
            except Exception as exc:  # noqa: BLE001
                logger.debug("扫码等待中选择弹窗处理失败: %s", exc)
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
                nav.dismiss_account_picker(session.tab, target_nickname or nickname)
                nickname = extract_nickname(session) or nickname
            except Exception as exc:  # noqa: BLE001 — 弹窗处理失败不阻塞登录
                logger.debug("登录后账号选择弹窗检查失败: %s", exc)
            logger.info("扫码登录成功 nickname=%s token=%s", nickname, token[:6] + "…")
            session.retarget_capture()      # 监听挂到登录后的活跃 tab
            session.minimize_window()       # 扫码完成，归还桌面
            session.start_minimize_watchdog()
            return LoginResult(ok=True, nickname=nickname, token=token)
        if time.time() - last_remind >= remind_interval:
            remain = int((deadline - time.time()) / 60)
            _notify("仍在等待扫码", f"距超时还有 {remain} 分钟，请扫码登录公众号。")
            last_remind = time.time()

    logger.error("登录超时（%d 分钟）", timeout_minutes)
    return LoginResult(ok=False, detail=f"等待扫码超时（{timeout_minutes}分钟）")
