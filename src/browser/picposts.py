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
from dataclasses import replace
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
        """翻最近 N 页发表记录，发布全部未发布的自动生成贴图。

        实战教训：每发布一条，列表就变化 → 旧快照里的「去查看」入口会失效。
        因此同一页内循环「重扫 → 发布」直到无新条目，再翻页。
        """
        if not self._open_publish_record():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_PICPOST, title="<打开发表记录失败>",
                                 content_hash=""),
                ok=False, detail="无法打开发表记录页面",
            )]

        results: list[PublishResult] = []
        for page in range(1, self._cfg.贴图.翻页数 + 1):
            if self._should_stop():
                break
            logger.info("扫描发表记录第 %d/%d 页", page, self._cfg.贴图.翻页数)
            scanned: set[str] = set()
            while not self._should_stop():
                todo: list[str] = []
                for title in self._find_picpost_entries():
                    if title in scanned:
                        continue
                    scanned.add(title)
                    if self._state.is_published(
                        self._account, CONTENT_TYPE_PICPOST,
                        content_hash(CONTENT_TYPE_PICPOST, title, self._account),
                    ):
                        logger.info("贴图《%s》已发布过，跳过", title[:30])
                        continue
                    todo.append(title)
                if not todo:
                    break                          # 本页无新贴图
                if (self._state.today_published_count(self._account)
                        >= self._cfg.账号.单账号单日最大发布数):
                    logger.warning("单日熔断触发，停止贴图发布")
                    return results
                for title in todo:
                    results.append(self._publish_one(title))
                    # 发布后列表变化 → 菜单路径重开记录页
                    if not self._open_publish_record():
                        return results
            if not self._goto_next_page():
                logger.info("没有更多页，停止翻页")
                break
        return results

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
                titles.append(self._extract_entry_title(el))
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

    def _extract_entry_title(self, marker_el) -> str:
        """从「已自动生成贴图草稿」标记元素向上提取条目标题。"""
        try:
            node = marker_el
            for _ in range(5):                     # 最多向上5层找标题
                parent = node.parent()
                if not is_real(parent):
                    break
                title_el = None
                for sel in ("css:.title", "css:.weui-desktop-media__title", "css:h3", "css:h4"):
                    try:
                        t = parent.ele(sel, timeout=0.5)
                        if is_real(t) and t.text and t.text.strip():
                            title_el = t
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if title_el:
                    return title_el.text.strip()[:60]
                node = parent
            return (marker_el.text or "").strip()[:40] or "贴图条目"
        except Exception:  # noqa: BLE001
            return "贴图条目"

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

    def _publish_one(self, title: str) -> PublishResult:
        chash = content_hash(CONTENT_TYPE_PICPOST, title, self._account)
        item = ContentItem(ctype=CONTENT_TYPE_PICPOST, title=title, content_hash=chash)
        logger.info("[%s] 开始发布贴图《%s》", self._account, title[:30])
        self._state.upsert(account=self._account, ctype=CONTENT_TYPE_PICPOST,
                           chash=chash, title=title, status="pending")
        evidence = self._s.screenshot_evidence("pic_before")

        try:
            ok, detail = self._do_publish_flow(title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("发布贴图异常")
            ok, detail = False, f"异常: {exc}"

        evidence_after = self._s.screenshot_evidence("pic_after")
        ev = evidence_after or evidence

        if ok:
            self._state.mark_published(account=self._account, ctype=CONTENT_TYPE_PICPOST,
                                       chash=chash, title=title, evidence=ev)
        else:
            self._state.upsert(account=self._account, ctype=CONTENT_TYPE_PICPOST,
                               chash=chash, title=title, status="failed", detail=detail)
        if self._notifier:
            mark = "✅" if ok else "❌"
            self._notifier.send_text(
                f"{mark} [{self._account}] 贴图《{title[:40]}》"
                + ("已发布" if ok else f"失败: {detail[:50]}")
            )

        return PublishResult(item=replace(item, detail=detail), ok=ok,
                             detail=detail, evidence=ev)

    def _do_publish_flow(self, title: str) -> tuple[bool, str]:
        """找到该条目 →「去查看」→ 等加载 → 编辑器内「发表」×2 → 验证。

        实战教训（2026-08-28）：
        - 贴图编辑器是新 tab，URL 为 appmsg_edit_v2（与文章编辑器同源组件）
          → 找 tab 必须匹配 "appmsg_edit"，猜的 appmsgalbum/picPage 永远 miss。
        - 发表确认弹窗与草稿发布同款（new_mass_send_dialog 双「发表」）。
        """
        from . import nav
        from .safety import assert_button_safe
        from .session import human_pause
        tab = self._s.tab

        # —— 1. 定位该条目的「去查看」 ——
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
        self._s.wait_ready(timeout=20)
        human_pause()

        # —— 2. 等贴图编辑器新 tab（实测 URL: appmsg_edit_v2）——
        editor = None
        deadline = time.time() + 15
        while time.time() < deadline and editor is None:
            editor = (self._s.find_tab_on("appmsg_edit")
                      or self._s.find_tab_on("appmsgalbum")
                      or self._s.find_tab_on("picPage"))
            if editor is None:
                time.sleep(1.5)
        if editor is None:
            return False, "「去查看」未打开编辑器页（15秒内无新 tab）"
        logger.info("贴图编辑器 tab: %s", (editor.url or "")[:70])
        try:
            # —— 3. 智能等待「草稿加载中」消失（最长 N 分钟，默认 6）——
            if not self._wait_loading_done(editor):
                return False, "贴图草稿加载超时（弹窗未消失）"
            nav.dismiss_account_picker(editor, self._account)
            human_pause()

            # —— 4. 发表：弹窗预开则直接点；否则先点编辑器底栏「发表」——
            assert_button_safe("发表")
            dialog_scope = ".new_mass_send_dialog, .weui-desktop-dialog, [class*=dialog]"
            if not nav.js_click_visible_text(editor, "发表", timeout=8, scope_css=dialog_scope):
                # 弹窗未预开 → 点编辑器底栏「发表」唤出弹窗
                toolbar_scope = ".mass_send, .tool_bar, [class*=publish], .footer"
                if not (nav.js_click_visible_text(editor, "发表", timeout=10, scope_css=toolbar_scope)
                        or nav.js_click_visible_text(editor, "发表", timeout=10)):
                    return False, "未找到贴图「发表」按钮"
                if not nav.js_click_visible_text(editor, "发表", timeout=25, scope_css=dialog_scope):
                    return False, "弹窗「发表」未点中"
            human_pause(1.5, 3.0)
            # 第二屏确认（按钮也叫「发表」，「继续发表」兜底；缺省也可接受）
            if not (nav.js_click_visible_text(editor, "发表", timeout=20, scope_css=dialog_scope)
                    or nav.js_click_visible_text(editor, "继续发表", timeout=8, scope_css=dialog_scope)):
                logger.warning("贴图第二个确认按钮未出现——可能单次确认即提交")
            human_pause()
            if self._s.capture:
                self._s.capture.drain("pic_publish")
            return self._verify_published(editor)
        finally:
            try:
                if editor is not tab and "appmsgpublish" not in (editor.url or ""):
                    editor.close()
                    time.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass
            self._open_publish_record()

    def _title_of_entry(self, el) -> str:
        return self._extract_entry_title(el)

    def _entry_container(self, el):
        try:
            return el.parent(3)
        except Exception:  # noqa: BLE001
            return None

    def _click_confirm(self, editor) -> None:
        for sel in CONFIRM_PUBLISH_SELECTORS:
            try:
                el = editor.ele(sel, timeout=3)
                if is_real(el) and click_robust(el):
                    return
            except Exception:  # noqa: BLE001
                continue

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

    def _verify_published(self, editor) -> tuple[bool, str]:
        for _ in range(20):
            time.sleep(1.5)
            if self._should_stop():
                return False, "用户停止"
            try:
                body = (editor.html or "")[:20000]
            except Exception:  # noqa: BLE001
                return True, "页面已跳转（视为成功，稍后人工核对）"
            if any(m in body for m in PUBLISH_SUCCESS_MARKERS):
                return True, "页面出现发表成功标记"
            try:
                if "appmsgpublish" in (editor.url or ""):
                    return True, "已返回发表记录页"
            except Exception:  # noqa: BLE001
                return True, "页面已关闭（视为成功）"
        return False, "未检测到贴图发表成功"
