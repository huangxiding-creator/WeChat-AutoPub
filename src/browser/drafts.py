"""草稿箱发布器：列表解析 → 按天过滤 → 逐篇发表 → 弹窗处理 → 验证 → 拟人间隔。

安全红线落实：
- 全程不点击任何「编辑/删除」按钮（safety.assert_button_safe 兜底）
- 发表按钮必须精确匹配文本且通过危险词检查
- 每篇前后截图存证
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from ..config import AppConfig
from ..constants import CONTENT_TYPE_DRAFT, MP_BASE_URL
from ..core.models import ContentItem, PublishResult
from ..core.state import StateDB, content_hash
from ..notify.wecom import WecomNotifier
from .safety import SafetyViolationError, assert_button_safe
from .selectors import (
    CONFIRM_PUBLISH_SELECTORS,
    CONTINUE_PUBLISH_SELECTORS,
    DRAFT_CARD_SELECTORS,
    DRAFT_TIME_SELECTORS,
    DRAFT_TITLE_SELECTORS,
    EDITOR_PUBLISH_BUTTON_SELECTORS,
    MENU_DRAFT_TEXTS,
    NEXT_PAGE_SELECTORS,
    PUBLISH_SUCCESS_MARKERS,
    SECURITY_VERIFY_MARKERS,
)
from .session import BrowserSession, click_robust, human_pause, is_real

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 草稿箱直达 URL（菜单点击失败时的兜底）
_DRAFT_BOX_URL = f"{MP_BASE_URL}/cgi-bin/appmsg?t=media/appmsg_list&action=list_card"


def clean_title(raw: str) -> str:
    """从卡片原始文本提取干净标题：剔除删除/编辑等按钮文字行，取最长行。

    实战教训：标题选择器未命中时回退整卡文本，会把「删除/确定删除」
    按钮文字卷进标题——既污染通知文案，也有安全隐患。
    """
    from ..constants import DANGEROUS_BUTTON_KEYWORDS
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    candidates = [ln for ln in lines
                  if not any(kw in ln for kw in DANGEROUS_BUTTON_KEYWORDS)
                  and ln not in ("编辑", "删除", "确定删除")]
    pool = candidates or lines
    return max(pool, key=len).strip()[:60] if pool else ""


@dataclass(frozen=True)
class DraftCard:
    """草稿箱里一张草稿卡片。"""
    title: str
    time_text: str
    index: int                      # 在当前列表页中的序号（每篇发布后重新解析）

    @property
    def chash(self) -> str:
        return content_hash(CONTENT_TYPE_DRAFT, self.title)


def _parse_date(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def tab_js_click(tab: Any, needle_json: str) -> bool:
    """卡片上 JS 兜底：对含标题片段的卡片派发 mouseover 后点精确文本「发表」。

    needle_json: json.dumps 转义过的标题片段（防 JS 注入）。
    """
    js = r"""
const cards = document.querySelectorAll('.weui-desktop-card, .js_draft_card, li[class*=draft]');
for (const c of cards) {
  if (!c.innerText || !c.innerText.includes(__NEEDLE__)) continue;
  c.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  c.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
  for (const b of c.querySelectorAll('a, button, [role=button], .weui-desktop-btn, span')) {
    const t = (b.innerText || '').trim().replace(/[\s]+/g, '');
    if (t === '发表') { b.click(); return true; }
  }
}
return false;
""".replace('__NEEDLE__', needle_json)
    try:
        return bool(tab.run_js(js))
    except Exception:  # noqa: BLE001
        return False


class DraftPublisher:
    """草稿发布器（一个已登录账号）。"""

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

    def publish_recent_drafts(self) -> list[PublishResult]:
        """发布最近 N 天草稿（含熔断 + 去重 + 拟人间隔）。"""
        if not self._open_draft_box():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_DRAFT, title="<打开草稿箱失败>",
                                 content_hash=""),
                ok=False, detail="无法打开草稿箱页面",
            )]

        results: list[PublishResult] = []
        empty_streak = 0                    # 连续0卡片次数（页面没加载完≠没草稿）
        fail_streak = 0                     # 连续发布失败次数（熔断用）                    # 连续0卡片次数（页面没加载完≠没草稿）
        while not self._should_stop():
            # 验证环节会把页面带到发表记录页 → 每轮先确保回到草稿箱
            cur = ""
            try:
                cur = self._s.tab.url or ""
            except Exception:  # noqa: BLE001
                cur = ""
            on_draft_list = ("appmsg" in cur and "appmsgpublish" not in cur
                             and "appmsg_edit" not in cur)
            if not on_draft_list and not self._open_draft_box():
                results.append(PublishResult(
                    item=ContentItem(ctype=CONTENT_TYPE_DRAFT, title="<回草稿箱失败>",
                                     content_hash=""),
                    ok=False, detail="发布中途无法返回草稿箱",
                ))
                break
            parsed = self._parse_cards()
            if not parsed:
                empty_streak += 1
                if empty_streak >= 3:
                    logger.warning("连续 3 次解析到 0 张草稿卡片，结束本轮（url=%s）", cur)
                    break
                logger.warning("第 %d 次解析到 0 张草稿卡片，重开草稿箱重试（url=%s）",
                               empty_streak, cur)
                self._open_draft_box()
                continue
            empty_streak = 0
            cards = self._filter_recent(parsed)
            pending = [c for c in cards if not self._is_done(c)]
            if not pending:
                logger.info("[%s] 最近 %d 天草稿已全部处理完", self._account,
                            self._cfg.草稿.发布最近天数)
                break
            card = pending[0]

            if self._state.today_published_count(self._account) >= self._cfg.账号.单账号单日最大发布数:
                logger.warning("[%s] 触发单日熔断（%d 篇），停止发布", self._account,
                               self._cfg.账号.单账号单日最大发布数)
                break

            result = self._publish_one(card)
            results.append(result)
            if result.ok:
                fail_streak = 0
                if pending.index(card) < len(pending) - 1:
                    self._polite_wait()      # ← 3~5 分钟随机间隔（用户指定）
            else:
                fail_streak += 1
                if fail_streak >= 3:
                    logger.warning("连续失败 %d 次，停止草稿发布（防原地打转触发风控）",
                                   fail_streak)
                    break
            # 发布后列表索引漂移 → while 循环重新解析（WeVideo 实战教训）

        return results

    # —— 页面操作 ——

    def _open_draft_box(self) -> bool:
        """先回首页重置 SPA → 点侧栏「草稿」菜单 → 兜底直达 URL。"""
        self._s.navigate(f"{MP_BASE_URL}/")
        self._s.wait_ready(timeout=15)
        tab = self._s.tab
        for text in MENU_DRAFT_TEXTS:
            try:
                for el in tab.eles(f"@text()={text}", timeout=2):
                    if is_real(el) and click_robust(el):
                        self._s.wait_ready(timeout=10)
                        if "appmsg" in (tab.url or ""):
                            logger.info("已进入草稿箱（菜单点击）")
                            return True
            except Exception:  # noqa: BLE001
                continue
        logger.info("菜单未命中，兜底直达草稿箱 URL")
        if self._s.navigate(_DRAFT_BOX_URL):
            self._s.wait_ready(timeout=15)
            return "appmsg" in (self._s.tab.url or "")
        return False

    def _parse_cards(self) -> list[DraftCard]:
        """解析当前页草稿卡片（标题 + 时间）。"""
        tab = self._s.tab
        cards: list[DraftCard] = []
        for sel in DRAFT_CARD_SELECTORS:
            try:
                els = [e for e in tab.eles(sel, timeout=3) if is_real(e)]
            except Exception:  # noqa: BLE001
                continue
            for idx, el in enumerate(els):
                raw = self._first_text(el, DRAFT_TITLE_SELECTORS) or (el.text or "")
                title = clean_title(raw)
                time_text = self._first_text(el, DRAFT_TIME_SELECTORS) or ""
                if title:
                    cards.append(DraftCard(title=title, time_text=time_text, index=idx))
            if cards:
                logger.info("解析到 %d 张草稿卡片（选择器 %s）", len(cards), sel)
                return cards
        logger.warning("未解析到草稿卡片——选择器可能漂移，建议 run.py --recon 存档排查")
        return []

    def _filter_recent(self, cards: list[DraftCard]) -> list[DraftCard]:
        cutoff = datetime.now() - timedelta(days=self._cfg.草稿.发布最近天数)
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        recent, nodate = [], 0
        for c in cards:
            dt = _parse_date(c.time_text)
            if dt is None:
                nodate += 1          # 日期解析失败 → 排除（实战：2020老文章无日期被误判新稿）
                continue
            if dt >= cutoff:
                recent.append(c)
        logger.info("草稿过滤：%d 张中 %d 张在最近 %d 天内（%d 张日期无法解析已排除）",
                    len(cards), len(recent), self._cfg.草稿.发布最近天数, nodate)
        return recent

    def _is_done(self, card: DraftCard) -> bool:
        return self._state.is_published(self._account, CONTENT_TYPE_DRAFT, card.chash)

    # —— 单篇发布 ——

    def _publish_one(self, card: DraftCard) -> PublishResult:
        item = ContentItem(ctype=CONTENT_TYPE_DRAFT, title=card.title,
                           content_hash=card.chash)
        logger.info("[%s] 开始发布草稿《%s》", self._account, card.title[:30])
        self._state.upsert(account=self._account, ctype=CONTENT_TYPE_DRAFT,
                           chash=card.chash, title=card.title, status="pending")
        evidence = self._s.screenshot_evidence(f"draft_{card.index}_before")

        try:
            ok, detail = self._do_publish_flow(card)
        except Exception as exc:  # noqa: BLE001 — 任何异常都不允许炸掉整个循环
            logger.exception("发布草稿异常")
            ok, detail = False, f"异常: {exc}"

        evidence_after = self._s.screenshot_evidence(f"draft_{card.index}_after")
        ev = evidence_after or evidence

        if ok:
            self._state.mark_published(account=self._account, ctype=CONTENT_TYPE_DRAFT,
                                       chash=card.chash, title=card.title, evidence=ev)
            self._notify_result(card.title, ok=True)
        else:
            self._state.upsert(account=self._account, ctype=CONTENT_TYPE_DRAFT,
                               chash=card.chash, title=card.title, status="failed",
                               detail=detail)

        return PublishResult(item=replace(item, detail=detail), ok=ok,
                             detail=detail, evidence=ev)

    def _dismiss_account_picker(self, scope: Any, timeout: float = 12) -> bool:
        """处理「选择账号登录」弹窗（委托共享导航层实现）。"""
        from . import nav
        return nav.dismiss_account_picker(scope, self._account, timeout=timeout)

    # 登录页指纹：PAGE_MID 是登录页独有标记；发表被平台拦截时会
    # wxm-logout 清场并跳回此页（12:09 实战 CGI 轨迹证实）
    _LOGIN_PAGE_MARKERS = ("login/loginpage", "scanloginqrcode")

    def _session_lost(self, tab_el: Any) -> bool:
        """会话是否已被平台重置（跳回二维码登录页）。"""
        try:
            url = tab_el.url or ""
            if url.rstrip("/") == "https://mp.weixin.qq.com":
                return True
            head = (tab_el.html or "")[:8000]
            return any(m in head for m in self._LOGIN_PAGE_MARKERS)
        except Exception:  # noqa: BLE001 — tab 已死视为会话丢失
            return True

    def _do_publish_flow(self, card: DraftCard) -> tuple[bool, str]:
        """卡片「发表」→ 新tab编辑器(群发对话框预开) → 发表 → 继续发表 → 验证。

        13:06 亲手实走的完整链路（ground truth）：
        1. 草稿卡片 hover 唤出工具栏 → 点卡片「发表」
           → 新 tab 打开 appmsg_edit，且 new_mass_send_dialog 已预开
        2. 对话框内点第一个「发表」→ 第二屏（未开启群发通知）
        3. 第二屏再点第二个「发表」（位置不同，文本相同）→ 提交成功，弹窗消失
        旧版全部失败原因：在草稿列表 tab 上找弹窗（弹窗在编辑器 tab）。
        """
        tab = self._s.tab
        card_el = self._locate_card(card)
        if card_el is None:
            return False, "草稿卡片定位失败"

        # 1. 点卡片「发表」（hover 唤出工具栏；JS 兜底；绝不碰删除）
        if not self._click_card_publish(card_el, card):
            return False, "卡片「发表」按钮未点中"
        human_pause()

        # 2. 找新开的编辑器 tab（群发对话框在里面）
        editor = self._wait_editor_tab(timeout=15)
        if editor is None:
            return False, "点「发表」后未出现编辑器页（会话或风控问题）"
        logger.info("编辑器 tab 已打开（群发对话框应已预开）")
        try:
            # 3. 账号选择弹窗（风控路径可能出现，60s 慢加载）
            if self._dismiss_account_picker(editor, timeout=60):
                human_pause()
            if self._session_lost(editor):
                return False, "会话被平台重置，需重新扫码"
            # 4. 群发对话框：点主按钮「发表」→ 等第二屏「继续发表」→ 点
            if not self._js_click_text(editor, "发表", timeout=25,
                                       scope_css=".new_mass_send_dialog"):
                # 对话框可能未预开 → 退路：点编辑器底栏 mass_send「发表」
                if not self._js_click_text(editor, "发表", timeout=8,
                                           scope_css=".mass_send"):
                    return False, "群发对话框「发表」未点中"
                if not self._js_click_text(editor, "发表", timeout=25,
                                           scope_css=".new_mass_send_dialog"):
                    return False, "群发对话框「发表」未点中"
            human_pause()
            # 5. 第二屏确认：按钮仍叫「发表」但位置不同（用户实测 ground truth，
            #    不是「继续发表」——留作兜底文本）
            human_pause(1.5, 3.0)
            dialog_scope = ".new_mass_send_dialog, .weui-desktop-dialog, [class*=dialog]"
            if not (self._js_click_text(editor, "发表", timeout=20, scope_css=dialog_scope)
                    or self._js_click_text(editor, "继续发表", timeout=8, scope_css=dialog_scope)):
                return False, "第二个「发表」未点中（发布未完成）"
            human_pause()
            if self._session_lost(editor):
                return False, "确认发表后会话被重置，需人工核对发表记录"
            # 📡 捕获发布链路 CGI（接口复刻数据源）
            if self._s.capture:
                self._s.capture.drain("draft_publish")
            # 6. 金标准验证：发表记录能搜到最好；搜不到只告警（发表记录页有
            #    「请重新登录」瞬时故障，假阴性会诱发重复发布——已提交就认）
            vok, vdetail = self._verify_published(tab, card.title)
            if vok:
                return True, "发布成功（发表记录已确认）"
            logger.warning("已提交发布，但发表记录核对未通过（%s）——请人工抽查", vdetail)
            return True, "已提交（发表记录核对瞬时失败，建议人工抽查）"
        finally:
            if self._s.capture:
                self._s.capture.drain("draft_flow_end")
            self._close_editor_tab(editor)

    def _click_card_publish(self, card_el: Any, card: DraftCard) -> bool:
        """hover 唤出工具栏后点卡片「发表」（精确文本，绝不碰删除）。"""
        try:
            try:
                card_el.hover()
                time.sleep(random.uniform(0.6, 1.2))
            except Exception as exc:  # noqa: BLE001
                logger.debug("卡片悬停失败（继续尝试）: %s", exc)
            for btn in card_el.eles("text:发表", timeout=3):
                if not is_real(btn):
                    continue
                text = (btn.text or "").strip()
                if text not in ("发表", "发 表"):
                    continue
                assert_button_safe(text)
                if click_robust(btn):
                    logger.info("点击卡片「发表」按钮（悬停唤出后）")
                    return True
        except SafetyViolationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("卡片发表按钮点击异常: %s", exc)
        # JS 兜底：派发 mouseover + 全卡精确文本点击
        try:
            import json as _json
            needle = _json.dumps(card.title[:10], ensure_ascii=False)
            found = tab_js_click(self._s.tab, needle)
            if found:
                logger.info("JS 兜底点击卡片「发表」成功")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("JS 兜底点击失败: %s", exc)
        return False

    def _wait_editor_tab(self, timeout: float) -> Any:
        """等点「发表」后新开的编辑器 tab 出现。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            editor = self._find_editor_tab()
            if editor is not None:
                time.sleep(2.0)              # 等对话框渲染
                return editor
            time.sleep(1.5)
        return None

    def _js_click_text(self, tab_el: Any, text: str, timeout: float,
                       scope_css: str = "") -> bool:
        """轮询点击可见精确文本按钮（共享实现于 nav.js_click_visible_text）。"""
        from . import nav
        return nav.js_click_visible_text(tab_el, text, timeout, scope_css)


    def _open_card_editor(self, card: DraftCard) -> Any:
        """打开草稿进入编辑器，返回编辑器 tab（找不到返回 None）。

        实战教训：点卡片标题开的是文章预览页，不是编辑器——
        随后在草稿列表页上找「发表」按钮永远找不到确认弹窗。
        策略：A. 卡片 DOM 提取 appmsg_edit 直达链接（最可靠）
              B. 卡片内点「编辑」文字/图标按钮
              C. 兜底点标题（旧行为，兼容老版 UI）
        每个策略后按 URL 验证真的进了 appmsg_edit。
        """
        card_el = self._locate_card(card)
        if card_el is None:
            return None
        outer = ""
        try:
            outer = card_el.html or ""
        except Exception:  # noqa: BLE001
            outer = ""

        def _editor_tab() -> Any:
            self._s.wait_ready(timeout=10)
            time.sleep(3)
            return self._find_editor_tab()

        # 策略A：直达链接
        m = self._EDIT_URL_RE.search(outer)
        if m:
            url = MP_BASE_URL + m.group(1).replace("&amp;", "&")
            logger.info("策略A：编辑器直达链接 %s", url[:80])
            try:
                new_tab = self._s.chromium.new_tab(url)
                time.sleep(4)
                ed = new_tab if "appmsg_edit" in (new_tab.url or "") else _editor_tab()
                if ed is not None:
                    return ed
            except Exception as exc:  # noqa: BLE001
                logger.debug("策略A失败: %s", exc)
        # 策略B：卡片内「编辑」按钮
        for probe in ("@text()=编辑", "css:[class*=edit]", "@@title()=编辑"):
            try:
                btn = card_el.ele(probe, timeout=1)
                if is_real(btn) and click_robust(btn):
                    logger.info("策略B：点击卡片「编辑」(%s)", probe)
                    ed = _editor_tab()
                    if ed is not None:
                        return ed
            except Exception:  # noqa: BLE001
                continue
        # 策略C：点标题（老版 UI 行为）
        target = self._find_title_element(card_el) or card_el
        if click_robust(target):
            logger.info("策略C：点击卡片标题")
            ed = _editor_tab()
            if ed is not None:
                return ed
        # 全部失败 → 存档卡片 DOM 供离线适配
        try:
            from ..constants import RECON_DIR
            RECON_DIR.mkdir(parents=True, exist_ok=True)
            (RECON_DIR / "card_dump.html").write_text(outer[:100000], encoding="utf-8")
            logger.warning("三种策略均未进入编辑器，卡片DOM已存档 card_dump.html")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _locate_card(self, card: DraftCard) -> Any:
        """按解析时的标题重新定位卡片元素。"""
        tab = self._s.tab
        for sel in DRAFT_CARD_SELECTORS:
            try:
                els = [e for e in tab.eles(sel, timeout=2) if is_real(e)]
                if len(els) > card.index:
                    return els[card.index]
            except Exception:  # noqa: BLE001
                continue
        return None

    def _find_title_element(self, card_el: Any):
        """在卡片内找标题元素（优先 a 链接，其次标题类名）。"""
    # 标题元素点击比整卡中心点击更精准安全
        try:
            for sel in (*DRAFT_TITLE_SELECTORS, "css:a", "css:h2", "css:h3"):
                el = card_el.ele(sel, timeout=0.5)
                if is_real(el) and (el.text or "").strip():
                    return el
        except Exception:  # noqa: BLE001
            pass
        return None

    # 在可见弹窗里找确认按钮（两阶段：1找→安全校验→2点）。
    # 实战教训：switch-account-dialog 等弹窗组件以 display:none 模板常驻
    # DOM，仅凭选择器命中=误报；必须查计算样式。
    _FIND_CONFIRM_BTN_JS = """
return (() => {
  const texts = ['发表', '群发', '确 定', '确定'];
  const visible = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') return false;
    }
    return el.offsetWidth > 0 && el.offsetHeight > 0;
  };
  const dialogish = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const c = String(n.className || '');
      if (/dialog|modal|popover/i.test(c)) return true;
    }
    return false;
  };
  const btns = [...document.querySelectorAll(
      'button, a, [role="button"], .weui-desktop-btn, span, div')]
    .filter(el => texts.includes((el.innerText || '').trim()))
    .filter(el => (el.innerText || '').trim().length <= 4)
    .filter(visible);
  const inDialog = btns.filter(dialogish);
  const pool = inDialog.length ? inDialog : btns;   // 有弹窗优先弹窗内的
  const scored = pool.map(el => ({
    el,
    primary: /primary|main-btn/i.test(String(el.className || '')) ? 1 : 0,
    depth: 0,
  }));
  // 最内层（最具体）优先，主按钮类加分
  let p = el => { let d = 0; for (let n = el; n; n = n.parentElement) d++; return d; };
  scored.forEach(s => s.depth = p(s.el));
  scored.sort((a, b) => (b.primary - a.primary) || (b.depth - a.depth));
  if (!scored.length) return null;
  const best = scored[0].el;
  best.setAttribute('data-autopub-confirm', '1');
  return {text: (best.innerText || '').trim(),
          cls: String(best.className).slice(0, 80)};
})();
"""
    _CLICK_MARKED_BTN_JS = """
return (() => {
  const el = document.querySelector('[data-autopub-confirm="1"]');
  if (!el) return false;
  el.click();
  el.removeAttribute('data-autopub-confirm');
  return true;
})();
"""

    def _click_confirm_publish(self, editor: Any) -> bool:
        """确认面板「发表」：等可见弹窗出现 → JS 找精确文本按钮 → 安全校验 → 点击。

        实战教训：旧版用 css 选择器找对话框，命中了 display:none 的
        switch-account 隐藏模板（内部为空）→ 找不到按钮 → 发布中断。
        """
        from . import nav
        from .safety import SafetyViolationError, assert_button_safe
        deadline = time.time() + 15
        dumped = False
        while time.time() < deadline:
            found = None
            try:
                found = editor.run_js(self._FIND_CONFIRM_BTN_JS)
            except Exception:  # noqa: BLE001
                found = None
            if isinstance(found, dict):
                try:
                    assert_button_safe(found.get("text", ""))
                except SafetyViolationError:
                    editor.run_js(
                        "document.querySelector('[data-autopub-confirm]')"
                        ".removeAttribute('data-autopub-confirm');")
                    raise
                try:
                    if editor.run_js(self._CLICK_MARKED_BTN_JS):
                        logger.info("点击成功: 确认面板「%s」(%s)",
                                    found.get("text"), found.get("cls", "")[:50])
                        return True
                except SafetyViolationError:
                    raise
                except Exception:  # noqa: BLE001 — 标记丢失就重找
                    pass
            if not dumped:                     # 首轮无果即存档现场
                nav.dump_visible_dialogs(editor, "confirm")
                self._s.screenshot_evidence("confirm_miss")
                dumped = True
            time.sleep(1.5)
        logger.warning("15秒内未见可见确认按钮（现场已存档 visibledialog_confirm.html + 截图）")
        return False

    def _find_editor_tab(self) -> Any:
        """找真正的编辑器 tab（URL 含 appmsg_edit）。

        实战教训：旧版兜底返回当前 tab，把草稿列表页冒充编辑器，
        导致在错误页面找「发表」按钮。找不到就返回 None，让调用方
        换策略——绝不伪装。
        """
        cur_url = self._s.tab.url or ""
        if "appmsg_edit" in cur_url:
            return self._s.tab
        return self._s.find_tab_on("appmsg_edit")

    def _close_editor_tab(self, editor: Any) -> None:
        """发表完关闭编辑器 tab（若为独立 tab），回到草稿箱。"""
        try:
            if editor is not self._s.tab and "appmsg_edit" in (editor.url or ""):
                editor.close()
                time.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
        self._s.navigate(_DRAFT_BOX_URL)
        self._s.wait_ready(timeout=15)

    def _verify_published(self, editor: Any, title: str) -> tuple[bool, str]:
        """金标准验证：发表记录页必须出现这篇文章才算成功。

        实战教训：曾用「URL 离开编辑器即成功」判定，结果假成功
        （实际点了个按钮跳回列表、根本没发表）。现在一律查发表记录。
        """
        # A. 先看编辑器页有没有明确成功标记（快路径，最长 ~15s）
        deadline_fast = time.time() + 15
        while time.time() < deadline_fast and not self._should_stop():
            time.sleep(1.5)
            try:
                body = (editor.html or "")[:20000]
            except Exception:  # noqa: BLE001 — tab 关闭，直接进硬核对
                break
            if any(m in body for m in PUBLISH_SUCCESS_MARKERS):
                # 出现标记也要硬核对（防误读），但给平台 5s 落库时间
                time.sleep(5)
                break

        # B. 硬核对：去发表记录页搜标题（金标准）
        from . import nav
        for attempt in range(3):                 # 刚发表可能延迟出现，重试3次
            if self._session_lost(self._s.tab):  # 会话已死→快速止损，不空转重试
                return False, "验证时发现会话已失效（登录页），需重新扫码"
            if not nav.open_publish_record(self._s):
                return False, "无法打开发表记录页验证"
            time.sleep(2)
            key = title[:15]
            if nav.page_contains_text(self._s, key):
                logger.info("✅ 金标准验证通过：发表记录已收录《%s…》", key)
                return True, "发表记录已出现该文章（金标准验证）"
            logger.info("第 %d 次未在发表记录中找到，%d秒后重试",
                        attempt + 1, 10 if attempt < 2 else 0)
            time.sleep(10)
        return False, "发表记录中未找到该文章（发布未成功）"

    def _handle_security_verify(self, editor: Any, title: str = "") -> None:
        """随机安全验证（需手机确认）→ 企微通知用户，等待其完成。"""
        try:
            body = (editor.html or "")[:20000]
        except Exception:  # noqa: BLE001
            return
        if not any(m in body for m in SECURITY_VERIFY_MARKERS):
            return
        logger.warning("触发安全验证，通知用户手机确认")
        if self._notifier:
            self._notifier.send_action_needed(
                "公众号发布触发安全验证",
                f"账号「{self._account}」发布《{title[:30]}》时触发安全验证，"
                "请在手机微信上完成确认，工具会自动继续。",
            )
        deadline = time.time() + 300            # 等 5 分钟
        while time.time() < deadline and not self._should_stop():
            time.sleep(5)
            try:
                if not any(m in (editor.html or "")[:20000]
                           for m in SECURITY_VERIFY_MARKERS):
                    logger.info("安全验证已通过")
                    return
            except Exception:  # noqa: BLE001
                return

    # —— 工具 ——

    def _click_first(self, tab: Any, selectors: tuple[str, ...], *,
                     label: str, timeout: float) -> bool:
        """按候选列表点击第一个命中元素；文本按钮须过安全检查。

        SafetyViolationError（危险按钮）必须向上传播，绝不吞掉。
        """
        from .safety import SafetyViolationError
        for sel in selectors:
            try:
                el = tab.ele(sel, timeout=timeout)
                if is_real(el):
                    text = (el.text or "").strip()
                    if text:
                        assert_button_safe(text)     # 🛡 危险文本按钮直接抛异常
                    if click_robust(el):
                        logger.info("点击成功: %s (%s)", label, sel)
                        return True
            except SafetyViolationError:
                raise
            except Exception:  # noqa: BLE001 — 定位失败继续尝试下一候选
                continue
        logger.debug("%s 未命中任何选择器", label)
        return False

    def _first_text(self, parent: Any, selectors: tuple[str, ...]) -> str:
        for sel in selectors:
            try:
                el = parent.ele(sel, timeout=1)
                if is_real(el) and el.text and el.text.strip():
                    return el.text.strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _polite_wait(self) -> None:
        """3~5 分钟随机间隔（用户指定，INI 可调），支持随时停止。"""
        lo, hi = self._cfg.草稿.每篇间隔最小秒, self._cfg.草稿.每篇间隔最大秒
        wait = random.uniform(lo, hi)
        logger.info("拟人间隔 %.0f 秒（%d~%d 随机）", wait, lo, hi)
        deadline = time.time() + wait
        while time.time() < deadline and not self._should_stop():
            time.sleep(2.0)

    def _notify_result(self, title: str, ok: bool) -> None:
        if self._notifier:
            mark = "✅" if ok else "❌"
            self._notifier.send_text(
                f"{mark} [{self._account}] 草稿《{title[:40]}》"
                + ("已发布" if ok else "发布失败，详见日志")
            )
