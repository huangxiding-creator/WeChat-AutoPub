"""浏览器会话：启动、导航（含安全守卫）、断线重连、稳健点击、存证截图。

生产级经验（源自 WeVideo-AutoPub 实战）：
- 优先复用已登录 tab，避免 latest_tab 拿到 newtab
- "断开/Disconnected/Target closed" → 自动重连重试
- 原生点击失败 → click(by_js=True) 兜底
- 导航前过安全守卫（永不删除/编辑）
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from DrissionPage import Chromium
from DrissionPage._elements.none_element import NoneElement

from ..config import AppConfig
from ..constants import EVIDENCE_DIR, MP_BASE_URL
from .driver import build_chromium_options
from .safety import assert_url_safe

logger = logging.getLogger(__name__)

_RECONNECT_MARKERS = ("断开", "Disconnected", "Target closed", "Page crashed")


def human_pause(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """拟人微停顿：每步操作后随机等 1~3 秒（防平台风控，用户指定）。"""
    time.sleep(random.uniform(min_s, max_s))


def is_real(el: Any) -> bool:
    return el is not None and not isinstance(el, NoneElement)


def click_robust(el: Any) -> bool:
    """稳健点击：原生失败 → JS 兜底。"""
    try:
        el.click()
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(by_js=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("JS 点击也失败: %s", exc)
        return False


class BrowserSession:
    """封装一个账号的浏览器会话（每账号独立 profile）。"""

    def __init__(self, config: AppConfig, profile_name: str) -> None:
        self._config = config
        self._profile_name = profile_name
        self._chromium: Optional[Chromium] = None
        self._tab: Any = None
        self.capture: Any = None            # PacketCapture（接口侦察）
        self._mini_watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    # —— 生命周期 ——

    def start(self) -> None:
        """启动/接管浏览器。每账号独立 profile + 固定端口 → 会话真正持久化。"""
        from .driver import (build_chromium_options, ensure_browser_running,
                             fixed_port, locate_browser)
        profile_dir = self._config.profile_root / self._profile_name
        port = fixed_port(self._profile_name)
        browser_path = locate_browser(self._config.浏览器.浏览器路径) \
            if self._config.浏览器.浏览器路径 else None
        if not ensure_browser_running(profile_dir, port, browser_path):
            raise RuntimeError(f"浏览器启动失败（profile={self._profile_name}）")
        try:
            co = build_chromium_options(profile_dir, port,
                                        headless=self._config.浏览器.无头模式)
            self._chromium = Chromium(co)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"浏览器接管失败（端口 {port}）: {exc}。"
                "请确认 Chrome/Edge 已安装，或在 INI 配置浏览器路径。"
            ) from exc
        self._tab = self.find_tab_on("mp.weixin.qq.com") or self._chromium.latest_tab
        try:
            self._tab.set.auto_handle_alert(on_off=True, accept=True)
        except Exception:  # noqa: BLE001
            pass
        # 📡 数据包侦察（接口复刻的数据来源，失败不阻塞）
        from .capture import PacketCapture
        self.capture = PacketCapture(self._tab)
        self.capture.start()
        logger.info("[%s] 浏览器会话就绪 url=%s", self._profile_name,
                    getattr(self._tab, "url", "?"))

    def stop(self) -> None:
        """停止会话引用。浏览器是独立常驻进程——**绝不退出**，登录态跨运行存活。"""
        self._watchdog_stop.set()         # 停最小化看门狗
        if self.capture is not None:
            try:
                self.capture.stop()
            except Exception:  # noqa: BLE001
                pass
        self._tab = None
        self._chromium = None

    @property
    def tab(self) -> Any:
        if self._tab is None:
            raise RuntimeError("BrowserSession 未启动")
        return self._tab

    def close_stale_mp_tabs(self, keep: Any = None) -> int:
        """关闭除 keep 外所有公众号 tab（僵尸 tab 清场）。

        实战推断：旧会话的 mp 页面带着旧 token 持续轮询，新登录后
        平台检测到多 token 冲突 → 掐死会话（2 分钟暴毙的头号嫌疑，
        且每轮失败留更多僵尸 tab，死得越来越快）。
        """
        closed = 0
        if self._chromium is None:
            return 0
        keep_tab = keep or self._tab
        try:
            keep_id = keep_tab.tab_id if keep_tab is not None else None
            for tid in list(self._chromium.tab_ids):
                if tid == keep_id:
                    continue
                try:
                    t = self._chromium.get_tab(tid)
                    url = t.url or ""
                except Exception:  # noqa: BLE001 — tab 已死
                    continue
                if "mp.weixin.qq.com" in url:
                    try:
                        t.close()
                        closed += 1
                        logger.info("已关闭僵尸tab: %s", url[:60])
                    except Exception:  # noqa: BLE001 — 关不掉就断轮询
                        try:
                            t.get("about:blank")
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("清场僵尸tab失败: %s", exc)
        if closed:
            logger.info("僵尸tab清场完成，关闭 %d 个", closed)
        return closed

    def retarget_capture(self) -> None:
        """把抓包器挂到当前活跃 tab（监听是 tab 级；登录/跳页后必须重挂）。

        实战教训：挂在旧 tempkey 文章页上，整轮会话零捕获。
        """
        if self.capture is not None:
            try:
                self.capture.stop()
            except Exception:  # noqa: BLE001
                pass
        from .capture import PacketCapture
        self.capture = PacketCapture(self._tab)
        self.capture.start()
        logger.info("[%s] 抓包器已重挂到当前 tab url=%s", self._profile_name,
                    getattr(self._tab, "url", "?")[:60])

    @property
    def chromium(self) -> Chromium:
        if self._chromium is None:
            raise RuntimeError("BrowserSession 未启动")
        return self._chromium

    def minimize_window(self) -> None:
        """最小化浏览器窗口（用户要求：运行时不抢占桌面焦点）。

        CDP 点击/JS 不依赖窗口可见，最小化不影响自动化。
        """
        try:
            self._tab.set.window.mini()
            logger.info("[%s] 浏览器窗口已最小化", self._profile_name)
        except Exception as exc:  # noqa: BLE001 — 最小化失败不影响主流程
            logger.debug("窗口最小化失败: %s", exc)

    def start_minimize_watchdog(self, interval: float = 10.0) -> None:
        """运行期间持续把浏览器窗口压回最小化（用户红线：不抢桌面）。

        hover/导航等操作偶发把窗口唤到前台；CDP 自动化不受最小化
        影响（2026-08-28 三篇贴图均在最小化状态下发布成功）。
        """
        if self._mini_watchdog is not None and self._mini_watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._mini_watchdog = threading.Thread(
            target=self._mini_loop, args=(interval,),
            name=f"mini-{self._profile_name}", daemon=True)
        self._mini_watchdog.start()
        logger.info("[%s] 最小化看门狗已启动（每 %.0f 秒）",
                    self._profile_name, interval)

    def _mini_loop(self, interval: float) -> None:
        while not self._watchdog_stop.is_set():
            try:
                if self._tab is not None:
                    self._tab.set.window.mini()
            except Exception:  # noqa: BLE001 — 窗口可能已关
                pass
            self._watchdog_stop.wait(interval)

    # —— tab 定位 ——

    def find_tab_on(self, url_substr: str) -> Any | None:
        if self._chromium is None:
            return None
        for tid in self._chromium.tab_ids:
            try:
                t = self._chromium.get_tab(tid)
                if url_substr in (t.url or ""):
                    return t
            except Exception:  # noqa: BLE001
                continue
        return None

    # —— 导航 / JS（带安全守卫 + 断线重连）——

    def navigate(self, url: str, retries: int = 3) -> bool:
        assert_url_safe(url)                       # 🛡 红线：危险URL直接抛异常
        for attempt in range(retries):
            try:
                self.tab.get(url)
                return True
            except Exception as exc:  # noqa: BLE001
                if any(m in str(exc) for m in _RECONNECT_MARKERS):
                    logger.warning("navigate 连接断开，重连重试 %d/%d", attempt + 1, retries)
                    time.sleep(2)
                    self._reconnect_tab()
                    continue
                raise
        logger.error("navigate 重试耗尽: %s", url)
        return False

    def run_js(self, script: str, *args, retries: int = 3):
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return self.tab.run_js(script, *args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if any(m in str(exc) for m in _RECONNECT_MARKERS):
                    logger.warning("run_js 断开，重连重试 %d/%d", attempt + 1, retries)
                    time.sleep(2)
                    self._reconnect_tab()
                    continue
                raise
        logger.error("run_js 重试耗尽: %s", last_exc)
        return None

    def _reconnect_tab(self) -> None:
        if self._chromium is None:
            return
        try:
            for tid in self._chromium.tab_ids:
                t = self._chromium.get_tab(tid)
                if "mp.weixin.qq.com" in (t.url or ""):
                    self._tab = t
                    try:
                        self._tab.set.auto_handle_alert(on_off=True, accept=True)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info("已重连 tab: %s", (t.url or "")[:60])
                    return
        except Exception as exc:  # noqa: BLE001
            logger.warning("重连 tab 失败: %s", exc)
        try:
            self._tab = self._chromium.latest_tab
        except Exception:  # noqa: BLE001
            pass

    # —— 等待 ——

    def wait_ready(self, timeout: float = 20) -> None:
        try:
            self.tab.wait.doc_loaded(timeout=timeout)
        except Exception:  # noqa: BLE001
            time.sleep(2)
        time.sleep(1.0)

    # —— 存证 ——

    def screenshot_evidence(self, label: str, timeout: float = 5.0) -> str:
        """发布前后截图存档（事后可追溯）。快速失败：5s 拿不到就放弃。"""
        try:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = EVIDENCE_DIR / f"{stamp}_{self._profile_name}_{label}.png"
            self.tab.get_screenshot(path=str(path), full_page=False, timeout=timeout)
            logger.info("存证截图: %s", path.name)
            return str(path)
        except Exception as exc:  # noqa: BLE001 — 存证失败不阻塞主流程
            logger.debug("截图存证跳过: %s", str(exc)[:80])
            return ""

    # —— 登录态 ——

    def is_logged_in(self) -> bool:
        """导航到首页实测 cookie 真伪（不信任缓存 URL）。"""
        try:
            self.navigate(f"{MP_BASE_URL}/")
            self.wait_ready(timeout=15)
            url = self.tab.url or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("登录态检测异常: %s", exc)
            return False
        logged = "mp.weixin.qq.com/cgi-bin/home" in url or "token=" in url
        logger.info("登录态检测: %s (url=%s)", "有效" if logged else "失效", url[:70])
        return logged
