"""现场诊断：接管 acct01 活浏览器，逐步走草稿发布流，拍摄真实弹窗。

目的：搞清点击「发表」后到底弹出什么（确认框结构），为修复
drafts._click_confirm_publish / nav.dismiss_account_picker 提供真相。
只做发布动作（用户授权的任务本身），绝不碰删除/编辑。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions

RECON = Path("data/recon")
RECON.mkdir(parents=True, exist_ok=True)

# 枚举页面上所有对话框/弹窗，标注可见性 + 文本
_ENUM_DIALOGS_JS = """
return (() => {
  const out = [];
  const sels = ['.weui-desktop-dialog__wrp', '.weui-desktop-modal__wrp',
                '.weui-desktop-dialog', '.switch-account-dialog',
                '.weui-desktop-modal', '[class*="dialog"]', '[class*="modal"]',
                '.weui-desktop-mask'];
  const seen = new Set();
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const st = getComputedStyle(el);
      const vis = st.display !== 'none' && st.visibility !== 'hidden'
                  && el.offsetWidth > 0 && el.offsetHeight > 0;
      const rect = el.getBoundingClientRect();
      const key = el.className + '|' + Math.round(rect.top) + '|' + Math.round(rect.left);
      if (seen.has(key)) continue;
      seen.add(key);
      const btns = [...el.querySelectorAll('button, a, [role="button"], .weui-desktop-btn')]
        .map(b => (b.innerText || '').trim().replace(/\\s+/g, ' '))
        .filter(Boolean).slice(0, 8);
      out.push({
        sel: sel, cls: String(el.className).slice(0, 120), visible: vis,
        w: Math.round(rect.width), h: Math.round(rect.height),
        text: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 200),
        buttons: btns,
      });
    }
  }
  return out;
})();
"""


def dump_dialogs(tab, tag: str) -> list[dict]:
    try:
        info = tab.run_js(_ENUM_DIALOGS_JS)
    except Exception as exc:  # noqa: BLE001
        print(f"[{tag}] JS枚举失败: {exc}")
        return []
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except Exception:  # noqa: BLE001
            info = []
    for d in info or []:
        mark = "👁VISIBLE" if d.get("visible") else "  hidden "
        print(f"[{tag}] {mark} {d.get('cls','')[:60]} | text={d.get('text','')[:60]} | btns={d.get('buttons')}")
    return info or []


def main() -> None:
    co = ChromiumOptions()
    co.set_address("127.0.0.1:19841")           # acct01 活浏览器
    b = Chromium(co)
    tab = b.latest_tab
    if "mp.weixin" not in (tab.url or ""):
        for tid in b.tab_ids:
            t = b.get_tab(tid)
            if "mp.weixin" in (t.url or ""):
                tab = t
                break
    print("URL:", (tab.url or "")[:80])
    if "cgi-bin/home" not in (tab.url or ""):
        tab.get("https://mp.weixin.qq.com/")
        time.sleep(3)
        tab.get("https://mp.weixin.qq.com/cgi-bin/appmsgtemplate?act=list&t=edit")
        time.sleep(2)
    print("登录页确认:", (tab.url or "")[:80])
    m = re.search(r"token=(\d+)", tab.url or "")
    print("token:", m.group(1) if m else "无")
    token = m.group(1) if m else ""

    # —— 进入草稿箱（工程内正典 URL）——
    tab.get("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_list&action=list_card")
    time.sleep(4)
    print("当前URL:", (tab.url or "")[:90])
    body = tab.html or ""
    RECON.joinpath("draftbox_page.html").write_text(body[:200000], encoding="utf-8")

    # 找草稿卡片
    cards = tab.eles("css:.weui-desktop-card")
    print(f"草稿卡片数: {len(cards)}")
    target = None
    for c in cards:
        txt = (c.text or "").replace("\n", " ")
        if "智能硬件立项" in txt:
            target = c
            print("命中目标卡片:", txt[:80])
            break
    if target is None and cards:
        target = cards[0]
        print("取第一张卡:", (target.text or "")[:80])
    if target is None:
        print("!! 没有卡片，草稿箱页可能没加载，看 draftbox_page.html")
        dump_dialogs(tab, "draftbox")
        return

    # —— 进编辑器 ——
    time.sleep(1.5)
    try:
        title_el = target.ele("css:.weui-desktop-card__title") or target.ele("tag:h2") \
            or target.ele("tag:h3") or target
        title_el.click()
    except Exception as exc:  # noqa: BLE001
        print("点标题失败:", exc)
        target.click(by_js=True)
    time.sleep(5)
    # 编辑器通常开新 tab
    editor = tab
    for tid in b.tab_ids:
        t = b.get_tab(tid)
        if "appmsg_edit" in (t.url or "") or ("cgi-bin/appmsg" in (t.url or "") and "appmsgtemplate" not in (t.url or "")):
            editor = t
            break
    print("编辑器URL:", (editor.url or "")[:90])
    editor.get_screenshot(path=str(RECON / "diag_editor.png"))
    dump_dialogs(editor, "editor初始")

    # —— 找「发表」按钮 ——
    btns = editor.eles("css:.weui-desktop-btn")
    pub = None
    for el in btns:
        txt = (el.text or "").strip()
        print("  编辑器按钮:", txt[:20], "|", (el.attr("class") or "")[:50])
        if txt == "发表":
            pub = el
    if pub is None:
        for el in editor.eles("tag:button") + editor.eles("tag:a"):
            if (el.text or "").strip() == "发表":
                pub = el
                break
    if pub is None:
        print("!! 没找到「发表」按钮")
        return

    # —— 点发表 + 连续 30 秒拍摄真实弹窗 ——
    print("\n=== 点击「发表」===")
    try:
        pub.click()
    except Exception:  # noqa: BLE001
        pub.click(by_js=True)
    seen_keys: set[str] = set()
    for i in range(30):
        time.sleep(1)
        for d in dump_dialogs(editor, f"+{i+1}s"):
            if d.get("visible"):
                key = str(d.get("cls"))[:80]
                if key not in seen_keys:
                    seen_keys.add(key)
                    shot = RECON / f"diag_dialog_{i+1}s.png"
                    editor.get_screenshot(path=str(shot))
                    print(f"    📸 新可见弹窗已截图: {shot.name}")
    # 最终全量 HTML 存档
    RECON.joinpath("after_publish_click.html").write_text(
        (editor.html or "")[:500000], encoding="utf-8")
    print("\n存档: after_publish_click.html / diag_editor.png")


if __name__ == "__main__":
    main()
