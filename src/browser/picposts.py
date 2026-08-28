"""贴图发布器：发表记录 →「已自动生成贴图草稿」→「去查看」→ 智能等待 →「发表」。

贴图特性（用户实测描述）：
- 文章发布后，发表记录里出现「已自动生成贴图草稿」+ 右侧「去查看」入口
- 点开后贴图编辑页弹「草稿加载中」，约 5 分钟生成完成
- 生成完成后点页面下方的「发表」按钮

翻页：默认翻最近 5 页（INI 可调 10 页等）。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..config import AppConfig
from ..constants import CONTENT_TYPE_PICPOST, MP_BASE_URL
from ..core.models import ContentItem, PublishResult
from ..core.state import StateDB, content_hash
from ..notify.wecom import WecomNotifier
from .selectors import (
    CONFIRM_PUBLISH_SELECTORS,
    MENU_PUBLISH_RECORD_TEXTS,
    NEXT_PAGE_SELECTORS,
    PICPOST_ENTRY_TEXTS,
    PICPOST_LOADING_MARKERS,
    PICPOST_PUBLISH_SELECTORS,
    PICPOST_VIEW_LINK_TEXTS,
    PUBLISH_SUCCESS_MARKERS,
    SECURITY_VERIFY_MARKERS,
)
from .session import BrowserSession, click_robust, is_real

logger = logging.getLogger(__name__)

_PUBLISH_RECORD_URL = f"{MP_BASE_URL}/cgi-bin/appmsgpublish?sub=list"


class PicPostPublisher:
    """贴图发布器（一个已登录账号）。"""

    def __init__(
        self,
        session: BrowserSession,
        config: AppConfig,
        state: StateDB,
        notifier: Optional[WecomNotifier],
        account_name: str,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._s = session
        self._cfg = config
        self._state = state
        self._notifier = notifier
        self._account = account_name
        self._should_stop = should_stop

    # —— 入口 ——

    def publish_picposts(self) -> list[PublishResult]:
        """翻最近 N 页发表记录，逐条点「去查看」触发贴图草稿生成。

        实战真相：贴图必须经草稿箱发布——本方法只负责触发生成，
        真正发布由编排器紧随其后再跑一遍草稿发布器完成。
        返回结果条目仅表示「触发」成败，不代表发布。
        """
        results: list[PublishResult] = []
        if not self._open_publish_record():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_PICPOST, title="<打开发表记录失败>",
                                 content_hash=""),
                ok=False, detail="无法打开发表记录页面",
            )]
        triggered = 0
        for page in range(1, self._cfg.贴图.翻页数 + 1):
            if self._should_stop():
                break
            logger.info("扫描发表记录第 %d/%d 页", page, self._cfg.贴图.翻页数)
            scanned: set[str] = set()
            while not self._should_stop():
                todo = [t for t in self._find_picpost_entries() if t not in scanned]
                scanned.update(todo)
                if not todo:
                    break
                for title in todo:
                    ok, detail = self._trigger_picpost_draft(title)
                    triggered += 1 if ok else 0
                    results.append(PublishResult(
                        item=ContentItem(ctype=CONTENT_TYPE_PICPOST, title=title,
                                         content_hash=content_hash(
                                             CONTENT_TYPE_PICPOST, title, self._account)),
                        ok=ok, detail=detail,
                    ))
                    if not self._open_publish_record():
                        logger.warning("记录页重开失败，停止触发")
                        return results
            if not self._goto_next_page():
                logger.info("没有更多页，停止翻页")
                break
        logger.info("贴图草稿触发完成：%d 条（等待草稿发布器发布）", triggered)
        return results

    def _trigger_picpost_draft(self, title: str) -> tuple[bool, str]:
        """点该条目「去查看」打开编辑器并等待草稿生成，然后关闭。

        绝不点任何「发表/保存」按钮——点击会触发 a2p create（生成草稿）
        或其它副作用；打开-等待-关闭本身就完成了草稿生成。
        """
        from .session import human_pause
        tab = self._s.tab
        view_link = None
        for entry_text in PICPOST_ENTRY_TEXTS:
            try:
                els = [e for e in tab.eles(f"text:{entry_text}", timeout=3) if is_real(e)]
            except Exception:  # noqa: BLE001
                continue
            for el in els:
                if self._title_of_entry(el) != title:
                    continue
                container = self._entry_container(el)
                for link_text in PICPOST_VIEW_LINK_TEXTS:
                    link = None
                    try:
                        link = container.ele(f"@text()={link_text}", timeout=1) \
                            if container is not None else None
                    except Exception:  # noqa: BLE001
                        link = None
                    if not is_real(link):
                        try:
                            link = tab.ele(f"@text()={link_text}", timeout=1)
                        except Exception:  # noqa: BLE001
                            link = None
                    if is_real(link):
                        view_link = link
                        break
                if view_link:
                    break
            if view_link:
                break
        if view_link is None:
            return False, "未找到该贴图的「去查看」入口"
        if not click_robust(view_link):
            return False, "点击「去查看」失败"

        # 等编辑器 tab 打开（实测 appmsg_edit_v2），给草稿生成留时间
        editor = None
        deadline = time.time() + 15
        while time.time() < deadline and editor is None:
            editor = (self._s.find_tab_on("appmsg_edit")
                      or self._s.find_tab_on("appmsgalbum"))
            if editor is None:
                time.sleep(1.5)
        if editor is None:
            return False, "「去查看」未打开编辑器页"
        logger.info("贴图编辑器已打开，等待草稿生成: %s", (editor.url or "")[:60])
        try:
            self._wait_loading_done(editor)      # 等加载弹窗消失（最长6分钟）
            time.sleep(5)                         # 多留渲染余量
            return True, "已触发贴图草稿生成"
        finally:
            try:
                if editor is not tab:
                    editor.close()
                    time.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass

    # —— 页面操作 ——

    def _open_publish_record(self) -> bool:
        """菜单路径打开发表记录（共享 nav 实现；直达 URL 是登录壳死路）。"""
        from . import nav
        return nav.open_publish_record(self._s)

    def _find_picpost_entries(self) -> list[str]:
        """找当前页所有「已自动生成贴图草稿」条目的标题。"""
        tab = self._s.tab
        titles: list[str] = []
        for entry_text in PICPOST_ENTRY_TEXTS:
            try:
                els = [e for e in tab.eles(f"text:{entry_text}", timeout=3) if is_real(e)]
            except Exception:  # noqa: BLE001
                continue
            for el in els:
                t = self._extract_entry_title(el)
                if t:
                    titles.append(t)
                else:
                    logger.warning("一条贴图草稿标题提取失败，跳过（选择器待更新）")
            if titles:
                self._archive_page(tab, len(titles))
                logger.info("找到 %d 条自动生成贴图草稿", len(titles))
                return titles
        return []

    def _archive_page(self, tab, n: int) -> None:
        """存档记录页 HTML（标题结构漂移时离线分析用）。"""
        try:
            from ..constants import RECON_DIR
            RECON_DIR.mkdir(parents=True, exist_ok=True)
            (RECON_DIR / "pic_entries_page.html").write_text(
                tab.html or "", encoding="utf-8")
            logger.debug("贴图条目页已存档（%d 条）", n)
        except Exception:  # noqa: BLE001
            pass

    _ENTRY_TITLE_SELECTORS: tuple[str, ...] = (
        "css:.weui-desktop-mass-appmsg__title span",   # 实测：记录条目文章标题
        "css:.weui-desktop-mass-appmsg__title",
        "css:.weui-desktop-card__title",
        "css:.weui-desktop-media__title",
        "css:.title",
        "css:h3", "css:h4",
    )

    def _extract_entry_title(self, marker_el) -> str:
        """从「已自动生成贴图草稿」标记向上找条目块，取文章标题。

        实测 DOM（2026-08-28）：标记 span.article-to-image-tips__status 在
        div.article-to-image-tips（记录条目块尾部）；标题在块内
        a.weui-desktop-mass-appmsg__title > span，含「原创」兄弟标签需剔除。
        提取失败返回空串（调用方跳过该条）——绝不能把标记文本当标题，
        否则多条贴图同名互相误去重（实战踩过）。
        """
        try:
            node = marker_el
            for _ in range(8):                     # 最多向上8层找条目块
                parent = node.parent()
                if not is_real(parent):
                    break
                for sel in self._ENTRY_TITLE_SELECTORS:
                    try:
                        t = parent.ele(sel, timeout=0.3)
                        if is_real(t) and (t.text or "").strip():
                            txt = t.text.strip().replace("原创", "").strip()
                            if txt:
                                return txt[:60]
                    except Exception:  # noqa: BLE001
                        continue
                node = parent
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _goto_next_page(self) -> bool:
        for sel in NEXT_PAGE_SELECTORS:
            try:
                el = self._s.tab.ele(sel, timeout=2)
                if is_real(el):
                    cls = (el.attr("class") or "")
                    if "disabled" in cls:
                        return False
                    if click_robust(el):
                        self._s.wait_ready(timeout=10)
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # —— 单条发布 ——

    def _title_of_entry(self, el) -> str:
        return self._extract_entry_title(el)

    def _entry_container(self, el):
        try:
            return el.parent(3)
        except Exception:  # noqa: BLE001
            return None

    def _wait_loading_done(self, editor) -> bool:
        """等「草稿加载中」弹窗消失。轮询 + 期间检测安全验证。"""
        deadline = time.time() + self._cfg.贴图.草稿加载等待最长分钟 * 60
        notified_verify = False
        while time.time() < deadline and not self._should_stop():
            time.sleep(10)
            try:
                body = (editor.html or "")[:30000]
            except Exception:  # noqa: BLE001
                return True                          # tab 消失视为已跳转
            if any(m in body for m in PICPOST_LOADING_MARKERS):
                logger.info("贴图草稿仍在生成，继续等待（剩余 %.0f 分钟）",
                            (deadline - time.time()) / 60)
                continue
            if any(m in body for m in SECURITY_VERIFY_MARKERS) and not notified_verify:
                notified_verify = True
                if self._notifier:
                    self._notifier.send_action_needed(
                        "贴图发布触发安全验证",
                        f"账号「{self._account}」请在手机上完成验证，工具会自动继续。",
                    )
                continue
            logger.info("贴图草稿加载完成（弹窗已消失）")
            return True
        return False

