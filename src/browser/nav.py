"""共享页面导航：草稿箱 / 发表记录 / 账号选择弹窗（菜单点击优先，URL 兜底）。

抽成公共模块：登录、草稿发布器、贴图发布器、金标准验证共用。
"""
from __future__ import annotations

import logging
import time

from ..constants import MP_BASE_URL
from .selectors import MENU_DRAFT_TEXTS, MENU_PUBLISH_RECORD_TEXTS
from .session import BrowserSession, click_robust, is_real

logger = logging.getLogger(__name__)

# 可见性判定 + 精确文本点击的 JS（实战验证：处理 display:none 模板、
# opacity 渐显、悬停工具栏三类陷阱；scope 支持逗号多容器）
_CLICK_VISIBLE_TEXT_JS = """
let roots = arguments[0] ? Array.from(document.querySelectorAll(arguments[0])) : [];
if (roots.length === 0) roots = [document];
// 实战教训（2026-08-28 贴图链）：确认弹窗的标题 DIV 文本恰为「发表」，
// 与底部真按钮同名——按 DOM 顺序首个命中是标题 → 点了没反应（7连点0提交）。
// → 两轮扫描：第一轮只找真按钮类元素，找不到才退回 div/span 兜底。
const SELS = ['button, [role=button], .weui-desktop-btn, a',
              'button, [role=button], .weui-desktop-btn, a, span, div'];
for (const sel of SELS) {
  for (const root of roots) {
    for (const b of root.querySelectorAll(sel)) {
      const r = b.getBoundingClientRect();
      if (r.width < 5 || r.height < 5) continue;
      let ok = true, op = 1;
      for (let n = b; n && n !== document.body; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') { ok = false; break; }
        op = Math.min(op, parseFloat(cs.opacity));
      }
      if (!ok || op < 0.5) continue;
      if ((b.innerText || '').trim() === arguments[1]) { b.click(); return true; }
    }
  }
}
return false;
"""


def js_click_visible_text(tab_el, text: str, timeout: float,
                          scope_css: str = "") -> bool:
    """轮询点击页面上可见的精确文本按钮（草稿/贴图发布共用）。"""
    from .safety import assert_button_safe
    assert_button_safe(text)
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            if tab_el.run_js(_CLICK_VISIBLE_TEXT_JS, scope_css, text):
                logger.info("点击成功: 「%s」(scope=%s)", text, scope_css or "全页")
                return True
        except Exception:  # noqa: BLE001
            pass
        _time.sleep(1.5)
    logger.warning("「%s」在 %.0f 秒内未出现（scope=%s）", text, timeout, scope_css or "全页")
    return False


_DRAFT_BOX_URL = f"{MP_BASE_URL}/cgi-bin/appmsg?t=media/appmsg_list&action=list_card"
_PUBLISH_RECORD_URL = f"{MP_BASE_URL}/cgi-bin/appmsgpublish?sub=list"


def open_draft_box(session: BrowserSession) -> bool:
    """先回首页重置 SPA → 点「草稿」菜单 → 兜底直达 URL。"""
    session.navigate(f"{MP_BASE_URL}/")
    session.wait_ready(timeout=15)
    tab = session.tab
    for text in MENU_DRAFT_TEXTS:
        try:
            for el in tab.eles(f"@text()={text}", timeout=2):
                if is_real(el) and click_robust(el):
                    session.wait_ready(timeout=10)
                    if "appmsg" in (tab.url or ""):
                        logger.info("已进入草稿箱（菜单点击）")
                        return True
        except Exception:  # noqa: BLE001
            continue
    logger.info("菜单未命中，兜底直达草稿箱 URL")
    if session.navigate(_DRAFT_BOX_URL):
        session.wait_ready(timeout=15)
        return "appmsg" in (session.tab.url or "")
    return False


def open_publish_record(session: BrowserSession) -> bool:
    """回首页 → （必要时展开「内容管理」）→ 点「发表记录」。

    实战教训（2026-08-28）：
    - 直达 URL appmsgpublish?sub=list 会被服务器回 15K 登录壳（带 token 也一样），
      只有 SPA 内部菜单路由才有效 → 兜底 URL 已删除。
    - 「发表记录」藏在「内容管理」子菜单里，SPA 状态重置后子菜单收起 →
      直接点文本必 miss，需先展开。
    - 成功判据加页面大小检查（壳页约 15K，真实列表 100K+）。
    """
    from .selectors import MENU_CONTENT_GROUP_TEXTS, MENU_PUBLISH_RECORD_TEXTS
    import time as _time

    def _try_menu() -> bool:
        tab = session.tab
        for text in MENU_PUBLISH_RECORD_TEXTS:
            try:
                for el in tab.eles(f"@text()={text}", timeout=2):
                    if is_real(el) and click_robust(el):
                        session.wait_ready(timeout=10)
                        if ("appmsgpublish" in (tab.url or "")
                                and len(tab.html or "") > 30000):
                            logger.info("已打开发表记录（菜单点击）")
                            return True
            except Exception:  # noqa: BLE001
                continue
        return False

    session.navigate(f"{MP_BASE_URL}/")
    session.wait_ready(timeout=15)
    if _try_menu():                       # 子菜单已展开的快路径
        return True
    # 展开父菜单后重试（点击收起态的组名=展开）
    for text in MENU_CONTENT_GROUP_TEXTS:
        try:
            el = session.tab.ele(f"@text()={text}", timeout=2)
            if is_real(el) and click_robust(el):
                _time.sleep(1.5)
                break
        except Exception:  # noqa: BLE001
            continue
    if _try_menu():
        return True
    # 整页重载一次再试（SPA 偶发不渲染菜单）
    session.navigate(f"{MP_BASE_URL}/")
    session.wait_ready(timeout=15)
    if _try_menu():
        return True
    logger.warning("菜单三次未命中「发表记录」——请人工检查页面")
    return False



def page_contains_text(session: BrowserSession, key: str) -> bool:
    """当前页是否包含关键词（金标准验证用）。"""
    try:
        return key in (session.tab.html or "")
    except Exception:  # noqa: BLE001
        return False


_PICKER_CONTAINER_SELECTORS = (
    "css:.switch-account-dialog",
    "@@text():选择账号登录",
)
_PICKER_ITEM_SELECTORS = (
    "css:.switch-account-dialog .weui-desktop-account",
    "css:.switch-account-dialog li",
    "css:.weui-desktop-account",
    "css:.switch-account-dialog__bd li",
    "css:.switch-account-dialog__bd div[role=button]",
    "css:.switch-account-dialog [class*=account]",
)

# 可见性检查：mp 后台的弹窗组件（如 switch-account-dialog）在 DOM 里常驻
# 隐藏模板（wrp style="display:none"），仅凭元素存在判断=误报。
# 必须用计算样式 + 尺寸确认真的弹出来了。
_VISIBLE_JS = """
return (() => {
  const el = arguments[0];
  if (!el) return false;
  const st = getComputedStyle(el);
  return st.display !== 'none' && st.visibility !== 'hidden'
      && el.offsetWidth > 0 && el.offsetHeight > 0;
})();
"""


def is_el_visible(scope, selector: str) -> bool:
    """指定选择器的元素当前是否真实可见（排除 display:none 隐藏模板）。

    检查元素本身及其最多 4 层祖先（弹窗常把 display:none 挂在包裹层上）。
    """
    try:
        el = scope.ele(selector, timeout=1)
        if not is_real(el):
            return False
        node = el
        for _ in range(4):
            try:
                if node.run_js(_VISIBLE_JS):
                    return True
            except Exception:  # noqa: BLE001
                pass
            parent = node.parent()
            if not is_real(parent) or parent.tag in ("html", "body"):
                break
            node = parent
        return False
    except Exception:  # noqa: BLE001
        return False


def dump_visible_dialogs(scope, tag: str) -> str:
    """把当前所有可见弹窗的 HTML 存档到 data/recon/（失败现场取证）。"""
    try:
        from ..constants import RECON_DIR
        RECON_DIR.mkdir(parents=True, exist_ok=True)
        html = scope.html or ""
        # 只要有可见弹窗就整体存档（后续人工分析）
        path = RECON_DIR / f"visibledialog_{tag}.html"
        path.write_text(html[:500000], encoding="utf-8")
        logging.info("可见弹窗现场已存档: %s", path.name)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logging.debug("弹窗存档失败: %s", exc)
        return ""


def dismiss_account_picker(scope, nickname: str = "", timeout: float = 12) -> bool:
    """处理「选择账号登录」弹窗（微信绑定多公众号时平台随机要求选身份）。

    弹窗可能在触发操作后 5~25 秒才弹出（账号列表走 get_acct_list
    AJAX 慢加载），因此整个 timeout 窗口内持续轮询可见性；只有隐藏
    模板、从未真正弹出的情况才返回 False。优先点与 nickname 匹配的
    账号项，无匹配点第一项。返回 True 表示已处理。
    """
    deadline = time.time() + timeout
    announced = False
    dumped = False
    while time.time() < deadline:
        visible = any(is_el_visible(scope, sel) for sel in _PICKER_CONTAINER_SELECTORS)
        if visible:
            if not announced:
                logging.info("检测到可见的「选择账号登录」弹窗，等待账号列表加载…")
                announced = True
            for sel in _PICKER_ITEM_SELECTORS:
                try:
                    items = [e for e in scope.eles(sel, timeout=1) if is_real(e)]
                    items = [e for e in items if (e.text or "").strip()]
                    if items:
                        if not nickname:
                            # 实战教训（2026-08-28）：无昵称时盲选第一项会选错账号
                            # （总包说被重复选中，目标号反而进不去）→ 等用户手选
                            logging.warning("账号选择弹窗已出现，但目标昵称未知——"
                                            "请在浏览器中手动选择账号")
                            continue
                        target = next(
                            (e for e in items if nickname in (e.text or "")),
                            items[0],
                        )
                        if click_robust(target):
                            logging.info("已选择账号身份: %s", (target.text or "").strip()[:20])
                            time.sleep(2)
                            return True
                except Exception:  # noqa: BLE001
                    continue
            if nickname:                   # 昵称兜底：弹窗内任何含昵称的可点元素
                try:
                    el = scope.ele(f"@@text():{nickname}", timeout=1)
                    if is_real(el) and click_robust(el):
                        logging.info("已按昵称选择账号身份: %s", nickname[:20])
                        time.sleep(2)
                        return True
                except Exception:  # noqa: BLE001
                    pass
            if not dumped:                 # 弹窗可见但列表未加载 → 存档供适配
                dump_visible_dialogs(scope, "picker")
                dumped = True
        time.sleep(1.5)
    if announced:
        logging.warning("账号选择弹窗 %d 秒内未出现可点的账号项（现场已存档）", int(timeout))
    return announced
