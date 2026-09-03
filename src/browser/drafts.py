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
from ..constants import CONTENT_TYPE_DRAFT, CONTENT_TYPE_PICPOST, MP_BASE_URL
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

# 平台日期格式实测：ISO(2026-08-28)、中文(2026年8月28日 / 08月28日)、相对(今天/昨天)
_DATE_PATTERNS = (
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
)
_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_TIME_ONLY_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")     # 纯时间=今天
_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _weekday_date(text: str, now: datetime) -> Optional[datetime]:
    """星期X/周X → 6天内对应日期（平台只对近一周条目用周几标注）。"""
    m = re.search(r"[星期周]([一二三四五六日天])", text)
    if not m:
        return None
    target = _WEEKDAY_MAP[m.group(1)]
    for back in range(7):
        d = now - timedelta(days=back)
        if d.weekday() == target:
            return datetime(d.year, d.month, d.day)
    return None

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
    is_picpost: bool = False        # 贴图草稿（贴图tab权威标记 / 时间-only启发式）

    @property
    def chash(self) -> str:
        # 贴图独立命名空间：贴图草稿与源文章同名（实战必然），共用 draft 哈希
        # 会让文章发布记录把同名贴图误判"已发"跳过（749.5案/总包之声13张贴图）
        return content_hash(
            CONTENT_TYPE_PICPOST if self.is_picpost else CONTENT_TYPE_DRAFT,
            self.title)


# 贴图专用篇间间隔：2026-08-29 用户明令 1 分钟之内；2026-09-02 收紧为 5 秒左右
# ——现为配置驱动（[草稿] 贴图间隔最小秒/最大秒，默认 5~10s 随机）


def _parse_date(text: str) -> Optional[datetime]:
    text = text or ""
    now = datetime.now()
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    if "今天" in text:
        return now
    if "昨天" in text:
        return now - timedelta(days=1)
    wd = _weekday_date(text, now)
    if wd is not None:
        return wd
    m = _MD_RE.search(text)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if dt > now + timedelta(days=2):     # 未来日期 → 去年（跨年贴）
            try:
                dt = datetime(now.year - 1, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return None
        return dt
    if _TIME_ONLY_RE.search(text):      # 「更新于 15:06」无日期词=今天
        return now
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


# 可见确认弹窗的文本指纹（空串=当前无可见弹窗）。确认链用它判断
# 「屏幕已实际切换」与「弹窗已关闭」——连点过快会空点旧屏、或在提交
# 完成后的界面误点出新一轮弹窗（2026-08-28 贴图链实战教训）。
_DIALOG_FINGERPRINT_JS = r"""
return (() => {
  const scopes = ['.new_mass_send_dialog', '.weui-desktop-dialog', '[class*=dialog]'];
  for (const sel of scopes) {
    for (const d of document.querySelectorAll(sel)) {
      const r = d.getBoundingClientRect();
      if (r.width < 50 || r.height < 50) continue;
      let ok = true, op = 1;
      for (let n = d; n && n !== document.body; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') { ok = false; break; }
        op = Math.min(op, parseFloat(cs.opacity));
      }
      if (!ok || op < 0.5) continue;
      const t = (d.innerText || '').trim().replace(/\s+/g, ' ');
      if (t) return t.slice(0, 300);
    }
  }
  return '';
})();
"""


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
        gap_range: Optional[tuple[float, float]] = None,
    ) -> None:
        self._s = session
        self._cfg = config
        self._state = state
        self._notifier = notifier
        self._gap_range = gap_range      # 贴图批清等场景的专用篇间间隔
        self._account = account_name
        self._should_stop = should_stop

    # —— 入口 ——

    def publish_recent_drafts(self) -> list[PublishResult]:
        """全闭环发布：文章 tab + 贴图 tab（旁路单档案兼容入口）。"""
        if not self._open_draft_box():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_DRAFT, title="<打开草稿箱失败>",
                                 content_hash=""),
                ok=False, detail="无法打开草稿箱页面",
            )]

        results: list[PublishResult] = []
        results = self._publish_loop(results, picpost_tab=False)
        results = self._publish_loop(results, picpost_tab=True)
        return results

    def publish_article_drafts(self) -> list[PublishResult]:
        """阶段1 专用：只发文章 tab（2026-09-03 用户指令两阶段顺序——
        先三号草稿再三号贴图，贴图留给阶段2 统一收）。"""
        if not self._open_draft_box():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_DRAFT, title="<打开草稿箱失败>",
                                 content_hash=""),
                ok=False, detail="无法打开草稿箱页面",
            )]
        return self._publish_loop([], picpost_tab=False)

    def publish_picpost_drafts(self) -> list[PublishResult]:
        """阶段2 贴图轮专用：只发贴图 tab（触发生成后新落箱的贴图）。"""
        if not self._open_draft_box():
            return [PublishResult(
                item=ContentItem(ctype=CONTENT_TYPE_DRAFT, title="<打开草稿箱失败>",
                                 content_hash=""),
                ok=False, detail="无法打开草稿箱页面",
            )]
        return self._publish_loop([], picpost_tab=True)

    def _publish_loop(self, results: list[PublishResult],
                      picpost_tab: bool) -> list[PublishResult]:
        """单tab发布循环（文章tab=窗口过滤；贴图tab=全发+快间隔）。"""
        if picpost_tab:
            if not self._open_draft_box() or not self._ensure_picpost_tab():
                return results           # 老UI没有贴图tab，跳过
        empty_streak = 0                    # 连续0卡片次数（页面没加载完≠没草稿）
        published_here = 0                  # 本轮已成功发布数（0=真空轮，重试可收敛）
        fail_streak = 0                     # 连续发布失败次数（熔断用）
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
            if picpost_tab and not self._ensure_picpost_tab():
                logger.warning("贴图tab失活且无法重新切换，结束贴图轮（贴图箱可能已空）")
                break
            parsed = (self._parse_sticker_tab() if picpost_tab
                      else self._parse_cards())
            if not parsed:
                empty_streak += 1
                # 2026-08-30 提速：真空轮（本轮还没发布过）2 次即止；刚发布过的
                # 轮次可能撞懒加载未渲染，保留 3 次恢复窗口（09:39 实证第 3 次捞回）
                limit = (self._cfg.草稿.空轮重试上限
                         if published_here == 0 else 3)
                if empty_streak >= limit:
                    if picpost_tab:
                        # 贴图箱发空=贴图轮正常收官（08-31 实证：箱空时
                        # 连「贴图」tab 按钮都会消失，非异常）
                        logger.info("贴图箱已空（连续 %d 次解析到 0 张草稿卡片），"
                                    "贴图轮完成", limit)
                    else:
                        logger.warning(
                            "连续 %d 次解析到 0 张草稿卡片，结束本轮——若箱内"
                            "仍有草稿则选择器可能漂移，建议 run.py --recon 存档"
                            "排查（url=%s）", limit, cur)
                    break
                logger.warning("第 %d 次解析到 0 张草稿卡片，刷新页面重试（url=%s）",
                               empty_streak, cur)
                try:                       # 先轻量刷新（SPA 假死常见），不行再重开
                    self._s.tab.refresh()
                    self._s.wait_ready(timeout=12)
                except Exception:  # noqa: BLE001
                    pass
                if not self._parse_cards():
                    self._open_draft_box()
                continue
            empty_streak = 0
            if picpost_tab:
                parsed = [replace(c, is_picpost=True) for c in parsed]
            else:
                parsed = [replace(c, is_picpost=True) if self._looks_like_picpost(c)
                          else c for c in parsed]
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
                published_here += 1
                if pending.index(card) < len(pending) - 1:
                    # 下一张是贴图卡 → 贴图专用快间隔；文章维持1.5~2.5分钟
                    nxt = pending[pending.index(card) + 1]
                    self._polite_wait(fast=nxt.is_picpost or self._looks_like_picpost(nxt))
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

    def _parse_cards(self, stop_titles: set[str] | None = None) -> list[DraftCard]:
        """解析草稿箱全部卡片（懒加载 + 服务端分页双陷阱防御）。

        stop_titles：命中任一标题即停止翻页（金标准复核用——标题
        仍在第 1 页时无需扫全量，省 5~10 页 × ~2s）。

        - 懒加载（2026-08-28）：滚到底触发渲染，双解析取大
        - 分页（2026-08-29 总包之声实战）：草稿箱服务端分页（每页
          ~10张），昨夜贴图在第 2 页而解析只看第 1 页 → 用户看见
          贴图在箱里、工具却判"0 张待发"。逐页点「下一页」累积解析。
        """
        all_cards: list[DraftCard] = []
        seen_titles: set[str] = set()
        for page in range(1, 11):                   # 硬上限 10 页防失控
            self._scroll_load()
            cards = self._parse_cards_once()
            fresh = [c for c in cards if c.title not in seen_titles]
            for c in fresh:
                seen_titles.add(c.title)
            all_cards.extend(fresh)
            if stop_titles and not seen_titles.isdisjoint(stop_titles):
                logger.info("解析早停：目标标题已在第 %d 页出现", page)
                break
            if not cards or not self._goto_next_page():
                break
            time.sleep(1.2)
        if all_cards:
            logger.info("草稿箱全量解析：%d 张（含翻页）", len(all_cards))
        return all_cards

    def _scroll_load(self) -> None:
        """滚到底再回顶，触发当前页懒加载渲染。"""
        tab = self._s.tab
        try:
            tab.scroll.to_bottom()
            time.sleep(1.2)
            tab.scroll.to_top()
            time.sleep(0.6)
        except Exception:  # noqa: BLE001 — 滚动失败不影响解析
            pass

    def _goto_next_page(self) -> bool:
        """草稿箱翻到下一页（有下一页且可点才 True）。"""
        tab = self._s.tab
        for sel in NEXT_PAGE_SELECTORS:
            try:
                el = tab.ele(sel, timeout=2)
                if not is_real(el):
                    continue
                cls = (el.attr("class") or "")
                if "disabled" in cls:
                    return False
                if click_robust(el):
                    self._s.wait_ready(timeout=10)
                    time.sleep(1.0)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _parse_cards_once(self) -> list[DraftCard]:
        """单次解析草稿卡片。"""
        tab = self._s.tab
        cards: list[DraftCard] = []
        for sel in DRAFT_CARD_SELECTORS:
            try:
                els = [e for e in tab.eles(sel, timeout=1.2) if is_real(e)]
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
        # 0 卡片≠漂移：贴图箱发空 / SPA 慢渲染都会走到这里。真漂移信号
        # 由 _publish_loop 的轮次结束告警承担（2026-08-31 复盘：旧告警
        # 日均 68 条全为假阳性，把真风险淹没）
        logger.info("本页未解析到草稿卡片（未加载完或该 tab 箱已空）")
        return []

    def _parse_sticker_tab(self) -> list[DraftCard]:
        """贴图tab解析（带渲染宽限）：贴图列表为 XHR 异步渲染，切 tab 后
        立即解析常拿到 0 张（08-31 实测：随即走刷新重入链，每次多花
        1~2 分钟且刷出假漂移告警）。短轮询等渲染，宽限内拿到即返回。
        """
        grace = float(self._cfg.草稿.贴图渲染宽限秒)
        deadline = time.time() + grace
        while True:
            cards = self._parse_cards()
            if cards or time.time() >= deadline:
                if not cards:
                    # 旋钮2（自复盘可调 界3~15）的调参证据：宽限耗尽仍 0 张
                    logger.warning(
                        "贴图渲染宽限超时（%.0fs 未渲染出贴图卡片）", grace)
                return cards
            time.sleep(2.0)

    def _filter_recent(self, cards: list[DraftCard]) -> list[DraftCard]:
        """贴图全发 + 文章只发最近 N 天（2026-08-29 用户双指令）。

        贴图卡（「更新于 HH:MM」时间-only）不受日期窗口限制；
        文章卡走窗口过滤；陈年文章草稿（如 01月13日 的讲座材料）留箱不动。
        """
        cutoff = datetime.now() - timedelta(days=self._cfg.草稿.发布最近天数)
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        recent, nodate, pics = [], 0, 0
        for c in cards:
            if c.is_picpost or self._looks_like_picpost(c):
                recent.append(c)    # 贴图：发布所有的（不限日期）
                pics += 1
                continue
            dt = _parse_date(c.time_text)
            if dt is None:
                nodate += 1          # 日期解析失败 → 排除（实战：2020老文章无日期被误判新稿）
                continue
            if dt >= cutoff:
                recent.append(c)
        logger.info("草稿过滤：%d 张中 %d 张待发（贴图 %d 张不限日期 + 文章 %d 张在最近 %d 天内；%d 张日期无法解析已排除）",
                    len(cards), len(recent), pics, len(recent) - pics,
                    self._cfg.草稿.发布最近天数, nodate)
        return recent

    def _is_done(self, card: DraftCard) -> bool:
        ctype = CONTENT_TYPE_PICPOST if card.is_picpost else CONTENT_TYPE_DRAFT
        return self._state.is_published(self._account, ctype, card.chash)

    def _ensure_picpost_tab(self) -> bool:
        """新版草稿箱：切到「贴图」tab（URL 参数 item_show_type=8，页面级跳转）。

        实测：点「贴图 13」按钮=整页刷新（ContextLost）；URL 直达最稳。
        老UI无此tab（直达后仍无卡片变化）由调用方的解析结果兜底。
        """
        tab = self._s.tab
        url = tab.url or ""
        if "item_show_type=8" in url:
            return True                    # 已在贴图tab
        if "action=list" in url and "appmsg" in url and "appmsgpublish" not in url:
            new_url = re.sub(r"([?&])item_show_type=\d+", r"item_show_type=8", url)
            if new_url == url:
                new_url = url + "&item_show_type=8"
            try:
                tab.get(new_url)
                self._s.wait_ready(timeout=15)
                time.sleep(1.0)
                if "item_show_type=8" in (tab.url or ""):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("贴图tab URL直达失败: %s", exc)
        # 兜底：按钮点击（会整页刷新，必须 wait_ready）
        try:
            for el in tab.eles("tag:button", timeout=1.5):
                txt = (el.text or "").strip()
                if txt.startswith("贴图"):
                    el.click()
                    try:
                        self._s.wait_ready(timeout=15)
                    except Exception:  # noqa: BLE001
                        time.sleep(2.0)
                    time.sleep(1.0)
                    return "item_show_type=8" in (tab.url or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("贴图tab查找失败: %s", exc)
        return False

    @staticmethod
    def _looks_like_picpost(card: DraftCard) -> bool:
        """贴图草稿卡判别：时间文本是「更新于 HH:MM」且无任何日期词。
        误判代价不对称——误当贴图只是间隔略快（无害），误当文章只是慢。"""
        t = (card.time_text or "").strip()
        if "更新于" not in t:
            return False
        return not any(k in t for k in ("今天", "昨天", "月", "日", "周", "-"))

    # —— 单篇发布 ——

    def _publish_one(self, card: DraftCard) -> PublishResult:
        ctype = CONTENT_TYPE_PICPOST if card.is_picpost else CONTENT_TYPE_DRAFT
        item = ContentItem(ctype=ctype, title=card.title,
                           content_hash=card.chash)
        logger.info("[%s] 开始发布%s《%s》", self._account,
                    "贴图" if card.is_picpost else "草稿", card.title[:30])
        self._state.upsert(account=self._account, ctype=ctype,
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
            self._state.mark_published(account=self._account, ctype=ctype,
                                       chash=card.chash, title=card.title, evidence=ev)
            self._notify_result(card.title, ok=True)
        else:
            self._state.upsert(account=self._account, ctype=ctype,
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
        2. 对话框内连点「发表」（文章2次/贴图3次，按钮连续换屏）
        3. 再无确认按钮=提交完成，弹窗消失
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
            # 2026-08-30 提速：弹窗慢加载实测 5~25s 出现；缺席时把 60s
            # 窗口轮询完是纯浪费（每篇 ~35s）。25s 仍覆盖慢加载上界；
            # 若极端延迟出现弹窗挡确认链 → 按钮找不到 → 干净失败，下次重试。
            if self._dismiss_account_picker(editor,
                                            timeout=self._cfg.草稿.选择弹窗等待秒):
                human_pause()
            if self._session_lost(editor):
                return False, "会话被平台重置，需重新扫码"
            # 4. 确认链：弹窗内连点「发表」直到再无确认按钮
            #    用户实测：文章草稿 2 次、贴图草稿 3 次（按钮连续换屏出现，
            #    文本都叫「发表」）——完成判据=整个等待窗内无可见确认按钮
            dialog_scope = ".new_mass_send_dialog, .weui-desktop-dialog, [class*=dialog]"
            first_hit = self._click_dialog_button(editor, ("发表", "继续发表"), 25)
            if not first_hit:
                # 弹窗未预开 → 点编辑器底栏「发表」唤出弹窗后重试
                if not (self._js_click_text(editor, "发表", timeout=10,
                                            scope_css=".mass_send, .tool_bar, [class*=publish], .footer")
                        or self._js_click_text(editor, "发表", timeout=8)):
                    return False, "「发表」按钮未找到（弹窗未预开且底栏无按钮）"
                if not self._click_dialog_button(editor, ("发表", "继续发表"), 25):
                    return False, "点底栏「发表」后弹窗未出现"
            clicks = 1
            # 每点一屏后等弹窗状态稳定再点下一屏；完成判据=「安静期」：
            # 弹窗关闭后连续 8 秒无新弹窗才算提交完成（换屏空窗期
            # 2~5 秒，只看瞬时关闭会漏掉第二屏「继续发表」）。
            fp = self._dialog_fingerprint(editor)
            for _ in range(6):          # 实测 2 屏（发表+继续发表），留余量
                state = self._wait_settle(editor, fp)
                if state == "done":
                    break
                hit = self._click_dialog_button(editor, ("发表", "继续发表"), 10)
                if not hit:
                    # 无按钮：可能正处换屏空窗，再等一轮安静期
                    if self._wait_settle(editor, fp,
                                         max_wait=15) == "done":
                        break
                    if not self._click_dialog_button(editor, ("发表", "继续发表"), 8):
                        break
                clicks += 1
                fp = self._dialog_fingerprint(editor)
            logger.info("确认链完成：共点 %d 次（安静期判据：关闭8秒无新弹窗）", clicks)
            human_pause()
            if self._session_lost(editor):
                return False, "确认发表后会话被重置，需人工核对发表记录"
            # 📡 捕获发布链路 CGI（接口复刻数据源）
            if self._s.capture:
                self._s.capture.drain("draft_publish")
            # 6. 金标准验证：草稿从草稿箱消失才是真发布。贴图草稿与源文章
            #    同名 → 发表记录按标题核对必然假阳性（2026-08-28 三次假阳性
            #    台账教训）；消失=成功；未消失=失败，下次运行自然重试（若
            #    其实已发布，草稿不会再出现在箱里，天然防重复发布）。
            if self._verify_box_gone(tab, card):
                return True, "发布成功（草稿已从草稿箱消失）"
            return False, "已点发表但草稿仍在草稿箱（缓存延迟或未生效），下次自动重试"
        finally:
            if self._s.capture:
                self._s.capture.drain("draft_flow_end")
            self._close_editor_tab(editor)

    def _verify_box_gone(self, tab: Any, card: DraftCard,
                         tolerance: float = 180.0) -> bool:
        """金标准：发布成功的草稿会从草稿箱消失（列表缓存有分钟级延迟）。

        刷新草稿箱轮询至 tolerance 秒；True=已消失（真发布）。
        """
        deadline = time.time() + tolerance
        opened = False
        while time.time() < deadline:
            try:
                if not opened or "appmsg" not in (tab.url or ""):
                    if not self._open_draft_box():
                        return False
                    opened = True
                else:
                    tab.refresh()
                self._s.wait_ready(timeout=15)
                titles = [c.title for c in
                          self._parse_cards(stop_titles={card.title})]
                if card.title not in titles:
                    logger.info("✅ 金标准通过：《%s…》已从草稿箱消失", card.title[:16])
                    return True
                logger.info("草稿仍在箱中（列表缓存延迟），继续等待确认…")
            except Exception as exc:  # noqa: BLE001
                logger.debug("草稿箱复核异常: %s", exc)
            time.sleep(20)
        logger.warning("金标准未通过：《%s…》%.0f 秒后仍在草稿箱", card.title[:16], tolerance)
        return False

    def _click_dialog_button(self, editor: Any, texts: tuple[str, ...],
                             timeout: float) -> Optional[str]:
        """真实点击可见弹窗底栏主按钮（返回点中的按钮文本）。

        实战教训（2026-08-28 工程行业大脑首篇）：run_js 合成 click 对
        Vue 表单弹窗可能完全无响应（点了没反应也不报错），必须用
        DrissionPage 元素点击走 CDP 真实鼠标事件。第二屏按钮可能是
        「继续发表」（账号无通知次数时）而非「发表」。
        """
        from .safety import assert_button_safe
        for t in texts:
            assert_button_safe(t)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for sel in ("css:.weui-desktop-dialog__ft button",
                            "css:button.weui-desktop-btn_primary"):
                    for e in editor.eles(sel, timeout=1):
                        try:
                            w, _h = e.rect.size
                            if not w or w < 40:
                                continue
                            txt = (e.text or "").strip()
                            if txt in texts and e.states.is_displayed:
                                e.click()
                                logger.info("真实点击弹窗按钮: 「%s」", txt)
                                return txt
                        except Exception:  # noqa: BLE001 — 单元素失败跳过
                            continue
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        logger.warning("弹窗主按钮 %s 在 %.0f 秒内未出现", texts, timeout)
        return None

    def _wait_settle(self, editor: Any, fp: str, quiet_secs: float | None = None,
                     max_wait: float = 30.0) -> str:
        """点击后等待弹窗状态稳定。

        返回："changed"（切到下一确认屏，可点击）/ "done"（弹窗关闭且
        安静 quiet_secs 秒无新弹窗 = 提交完成）/ "same"（同屏无变化，
        需重试点击当前屏）。
        """
        start = time.time()
        if quiet_secs is None:
            quiet_secs = float(self._cfg.草稿.安静期秒)
        closed_since = None
        while time.time() - start < max_wait:
            time.sleep(2.0)
            cur = self._dialog_fingerprint(editor)
            if cur:
                closed_since = None
                if cur != fp:
                    return "changed"
            else:
                if closed_since is None:
                    closed_since = time.time()
                elif time.time() - closed_since >= quiet_secs:
                    return "done"
        return "same"

    def _dialog_fingerprint(self, editor: Any) -> str:
        """当前可见确认弹窗的文本指纹（空串=无可见弹窗=已关闭/已提交）。"""
        try:
            return (editor.run_js(_DIALOG_FINGERPRINT_JS) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _confirm_dialog_open(self, editor: Any) -> bool:
        """确认弹窗当前是否可见（指纹非空）。"""
        return bool(self._dialog_fingerprint(editor))

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
                el = parent.ele(sel, timeout=0.3)
                if is_real(el) and el.text and el.text.strip():
                    return el.text.strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _polite_wait(self, fast: bool = False) -> None:
        """篇间随机间隔（文章/贴图各自配置驱动），支持随时停止。"""
        if fast:
            lo, hi = (self._cfg.草稿.贴图间隔最小秒,
                      self._cfg.草稿.贴图间隔最大秒)
            tag = "贴图专用"
        else:
            lo, hi = (self._gap_range
                      or (self._cfg.草稿.每篇间隔最小秒,
                          self._cfg.草稿.每篇间隔最大秒))
            tag = "文章"
        wait = random.uniform(lo, hi)
        logger.info("拟人间隔 %.0f 秒（%s %d~%d 随机）", wait, tag, lo, hi)
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
