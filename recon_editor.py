"""外科手术式侦察：一次扫码后，完整拍下草稿发布链路的真实页面结构。

绝不点「发表」——只取证：
1. 清场僵尸 tab → 等扫码 → 抓包挂活跃 tab（含响应体）
2. 草稿箱全量 HTML + 卡片按钮清单
3. 点第一张卡标题 → 枚举全部 tab → 找到真编辑器
4. 编辑器工具栏按钮清单（文本/类名/可见性）+ 全量 HTML + 截图
5. 发表记录页金标准核对（《智能硬件立项》到底发没发出去过）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions

RECON = Path(__file__).resolve().parent / "data" / "recon"
RECON.mkdir(parents=True, exist_ok=True)
PORT = 19841

_ENUM_VISIBLE_JS = """
return (() => {
  const out = [];
  const els = document.querySelectorAll(
    'button, a, [role="button"], .weui-desktop-btn, .weui-desktop-btn_primary');
  const vis = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') return false;
    }
    return el.offsetWidth > 0;
  };
  for (const el of els) {
    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 20) continue;
    if (!vis(el)) continue;
    out.push({text: t, tag: el.tagName,
              cls: String(el.className).slice(0, 90)});
  }
  return out.slice(0, 40);
})();
"""


def save(name: str, content: str) -> None:
    (RECON / name).write_text(content, encoding="utf-8")
    print(f"💾 已存 {name} ({len(content)} 字符)")


def dump_buttons(tab, label: str) -> None:
    try:
        btns = tab.run_js(_ENUM_VISIBLE_JS)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] 按钮枚举失败: {exc}")
        return
    if isinstance(btns, str):
        try:
            btns = json.loads(btns)
        except Exception:  # noqa: BLE001
            btns = []
    print(f"\n=== [{label}] 可见按钮清单 ===")
    for b in btns or []:
        print(f"  {b.get('tag','?'):8} | {b.get('text','')[:16]:16} | {b.get('cls','')[:70]}")
    save(f"buttons_{label}.json", json.dumps(btns, ensure_ascii=False, indent=1))


def main() -> None:
    co = ChromiumOptions()
    co.set_address(f"127.0.0.1:{PORT}")
    b = Chromium(co)
    tab = b.latest_tab
    if "mp.weixin" not in (tab.url or ""):
        for tid in b.tab_ids:
            t = b.get_tab(tid)
            if "mp.weixin" in (t.url or ""):
                tab = t
                break

    # —— 1. 僵尸 tab 清场 ——
    keep_id = tab.tab_id
    for tid in list(b.tab_ids):
        if tid == keep_id:
            continue
        try:
            t = b.get_tab(tid)
            if "mp.weixin.qq.com" in (t.url or ""):
                print("关闭僵尸tab:", (t.url or "")[:60])
                t.close()
        except Exception:  # noqa: BLE001
            pass

    # —— 2. 清场后必须主动导航到登录页，二维码才会显示 ——
    tab.get("https://mp.weixin.qq.com/")
    time.sleep(3)
    print("\n>>> 请扫码登录（等待中，最长10分钟）<<<")
    deadline = time.time() + 600
    token = ""
    while time.time() < deadline:
        time.sleep(3)
        try:
            url = tab.url or ""
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"token=(\d+)", url)
        if m and "cgi-bin/home" in url:
            token = m.group(1)
            break
    if not token:
        print("!! 10分钟未登录，退出")
        return
    print(f"✅ 登录成功 token={token}")
    tab.listen.start("mp.weixin.qq.com/cgi-bin")
    time.sleep(3)

    # —— 3. 草稿箱 ——
    tab.get("https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_list&action=list_card")
    time.sleep(5)
    save("recon_draftbox.html", tab.html or "")
    dump_buttons(tab, "draftbox")
    tab.get_screenshot(path=str(RECON / "recon_draftbox.png"))
    for pkt in tab.listen.steps(timeout=1.5):
        print("📡", (pkt.url or "").split("qq.com")[-1][:80])

    cards = tab.eles("css:.weui-desktop-card")
    print(f"\n草稿卡片数: {len(cards)}")
    if not cards:
        print("!! 0 卡片，看 recon_draftbox.html")
        return

    # —— 4. 点第一张卡标题，枚举 tab ——
    first = cards[0]
    print("第一张卡文本:", (first.text or "").replace("\n", " ")[:80])
    title_el = None
    for sel in ("css:.weui-desktop-card__title", "css:a", "css:h2", "css:h3"):
        el = first.ele(sel, timeout=0.5)
        try:
            if el and (el.text or "").strip():
                title_el = el
                break
        except Exception:  # noqa: BLE001
            continue
    (title_el or first).click()
    print("\n已点卡片标题，等 8 秒…")
    time.sleep(8)

    print("\n=== 点击后全部 tab ===")
    editor = None
    for tid in b.tab_ids:
        t = b.get_tab(tid)
        url = (t.url or "")[:100]
        print(f"  {tid[:8]} | {url}")
        if "appmsg_edit" in url:
            editor = t
    editor = editor or tab

    # —— 5. 编辑器取证 ——
    print(f"\n编辑器认定: {editor.url[:90]}")
    save("recon_editor.html", editor.html or "")
    dump_buttons(editor, "editor")
    editor.get_screenshot(path=str(RECON / "recon_editor.png"))
    for pkt in editor.listen.steps(timeout=1.5) if hasattr(editor, "listen") else []:
        print("📡", (pkt.url or "").split("qq.com")[-1][:80])
    try:
        tab.listen.stop()
    except Exception:  # noqa: BLE001
        pass

    # —— 6. 金标准：《智能硬件立项》发过没有 ——
    print("\n=== 发表记录金标准核对 ===")
    tab.get("https://mp.weixin.qq.com/cgi-bin/appmsgpublish?sub=list")
    time.sleep(5)
    h = tab.html or ""
    for key in ["智能硬件立项", "光伏电站"]:
        print(f"  「{key}」:", "✅ 已发布过！" if key in h else "❌ 未发布")
    save("recon_publish_record.html", h)
    tab.get_screenshot(path=str(RECON / "recon_publish_record.png"))

    print("\n=== 侦察完成，会话保留（不登出）===")


if __name__ == "__main__":
    main()
