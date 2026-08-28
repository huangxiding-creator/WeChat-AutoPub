# -*- coding: utf-8 -*-
"""贴图落箱恢复：贴图「去查看」入口被消费但草稿未保存时的兜底路径。

背景（2026-08-28 实战）：isNew=1&createType=8 变体的 a2p 编辑器不自动
落草稿箱，而「去查看」入口点一次即从发表记录消失（消费制）。若当时
「保存为草稿」没点中，贴图草稿就悬空丢失。

恢复法（探针实证可行）：
    用源文章内部 msgid 直构 a2p 编辑器 URL 重新打开 →
    等生成完成 → 真实点击「保存为草稿」→ 关 tab → 下一条。

msgids 来源：data/recon/acct05_pages.json（发表记录 API 抓包）里
publish_info.msgid，标题在 appmsgex[0].title（get_ids.py 已建映射，
本脚本直接读 data/recon/sticker_regen_plan.json）。

用法:
    python recover_stickers.py acct05 工程行业大脑
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config                      # noqa: E402
from src.logger import setup_logging                   # noqa: E402
from src.browser.session import BrowserSession         # noqa: E402
from src.browser.drafts import DraftPublisher          # noqa: E402
from src.browser.picposts import PicPostPublisher      # noqa: E402
from src.browser import nav                            # noqa: E402
from src.constants import MP_BASE_URL                  # noqa: E402
from src.core.state import StateDB                     # noqa: E402

PLAN_PATH = Path("data/recon/sticker_regen_plan.json")
TOKEN_FALLBACK = "1165462775"          # 本浏览器会话上次抓包值（仅兜底）
EDITOR_URL = (f"{MP_BASE_URL}/cgi-bin/appmsg?t=media/appmsg_edit_v2"
              "&action=edit&isNew=1&type=77&createType=8"
              "&a2p_appmsgid={mid}&token={token}&lang=zh_CN")

_TOKEN_PATTERNS = (
    r'token["\']\s*[:=]\s*["\'](\d{9,11})["\']',
    r'token=(\d{9,11})',
)


def extract_token(session: BrowserSession) -> str:
    """从首页 HTML 提取会话 token（a2p 编辑器 URL 必带）。"""
    session.navigate(f"{MP_BASE_URL}/")
    session.wait_ready(timeout=15)
    html = session.tab.html or ""
    for pat in _TOKEN_PATTERNS:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return TOKEN_FALLBACK


def wait_generation(pub: PicPostPublisher, editor,
                    timeout_min: float = 10.0) -> bool:
    """等贴图真正生成完（可见性判据；用户实测生成较久，别抢跑）。"""
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        if not pub._a2p_toast_visible(editor):
            time.sleep(2.5)                     # 复查一次防换屏抖动
            if not pub._a2p_toast_visible(editor):
                return True
        else:
            time.sleep(6)
    return False


def _save_verified(editor, click_fn) -> tuple[bool, str]:
    """点保存并用网络监听验证 operate_appmsg 提交真成功（ret=0）。"""
    try:
        editor.listen.start("operate_appmsg")
    except Exception:  # noqa: BLE001
        editor.listen = None                     # type: ignore[attr-defined]
    try:
        if not click_fn():
            return False, "「保存为草稿」按钮未出现"
        confirm_toast(editor, ("确定", "知道了", "保存", "好的"))
        if getattr(editor, "listen", None) is None:
            return True, "已点击保存（监听不可用，未验证POST）"
        packet = editor.listen.wait(timeout=30)
        editor.listen.stop()
        if packet is None:
            return False, "点击后30秒无 operate_appmsg 提交"
        body = getattr(getattr(packet, "response", None), "body", None) or ""
        txt = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        if '"ret":0' in txt.replace(" ", "") or '"ret":0,' in txt:
            return True, "保存提交成功（ret=0）"
        return False, f"保存提交异常: {txt[:120]}"
    finally:
        try:
            editor.listen.stop()
        except Exception:  # noqa: BLE001
            pass


def confirm_toast(editor, texts: tuple[str, ...], timeout: float = 10.0) -> None:
    """保存后若弹确认框（确定/知道了），真实元素点击关掉。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in ("tag:button", "css:.weui-desktop-btn"):
            try:
                for e in editor.eles(sel, timeout=1):
                    try:
                        w, _ = e.rect.size
                        if not w or w < 40:
                            continue
                        if ((e.text or "").strip() in texts
                                and e.states.is_displayed):
                            e.click()
                            time.sleep(1.5)
                            return
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1.0)


def save_one(pub: PicPostPublisher, url: str, title: str,
             nickname: str) -> tuple[bool, str]:
    """开编辑器 tab → 等真生成完 → 点「保存为草稿」并验证 → 关 tab。"""
    editor = pub._s.new_tab(url)
    for _ in range(12):                          # 等页面真实渲染（壳页~15K）
        if len(editor.html or "") > 20000:
            break
        time.sleep(1.5)
    nav.dismiss_account_picker(editor, nickname, timeout=4)
    try:
        if not editor.html or len(editor.html or "") < 20000:
            return False, "编辑器未加载（可能 msgid 失效或登录态异常）"
        if not wait_generation(pub, editor):
            return False, "贴图生成等待超时（10分钟仍在加载）"
        return _save_verified(editor, lambda: pub._click_save_draft(editor, 45))
    finally:
        try:
            editor.close()
            time.sleep(1.5)                        # 拟人间隔
        except Exception:  # noqa: BLE001
            pass


def box_titles(dp: DraftPublisher) -> list[str]:
    """读当前草稿箱全部标题（双解析取大已内建）。"""
    if not dp._open_draft_box():
        return []
    return [c.title for c in dp._parse_cards()]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    profile, nickname = sys.argv[1], sys.argv[2]

    plan = [tuple(x) for x in json.loads(
        PLAN_PATH.read_text(encoding="utf-8"))]
    print(f"恢复计划: {len(plan)} 张贴图")

    cfg = load_config()
    state = StateDB()
    session = BrowserSession(cfg, profile)
    session.start()
    try:
        if not session.is_logged_in():
            print(f"[{profile}] 会话已失效，退出（不弹码）")
            return 1
        session.start_minimize_watchdog()
        dp = DraftPublisher(session, cfg, state, None, account_name=nickname)
        pub = PicPostPublisher(session, cfg, state, None, account_name=nickname)

        token = extract_token(session)
        print(f"token: {token}")
        existing = set(box_titles(dp))
        print(f"草稿箱现有 {len(existing)} 张卡")

        ok_cnt, fail = 0, []
        for i, (mid, title) in enumerate(plan, 1):
            if title in existing:
                print(f"[{i}/{len(plan)}] 已在草稿箱，跳过: {title[:30]}")
                ok_cnt += 1
                continue
            url = EDITOR_URL.format(mid=mid, token=token)
            try:
                ok, detail = save_one(pub, url, title, nickname)
            except Exception as exc:  # noqa: BLE001 — 单条失败不拖垮整批
                ok, detail = False, f"异常: {exc}"
            mark = "OK " if ok else "FAIL"
            print(f"[{i}/{len(plan)}] {mark} {title[:32]} | {detail}")
            if ok:
                ok_cnt += 1
            else:
                fail.append((mid, title, detail))
            time.sleep(1.0 + (i % 3))              # 每条之间 1~3 秒

        final = set(box_titles(dp))
        landed = sum(1 for _mid, t in plan if t in final)
        print(f"完成: 点击保存 {ok_cnt}/{len(plan)}；草稿箱核对命中 {landed}/{len(plan)}")
        for mid, title, detail in fail:
            print(f"  FAIL {mid} {title[:30]} | {detail}")
        return 0 if landed == len(plan) else 1
    finally:
        session.stop()


if __name__ == "__main__":
    raise SystemExit(main())
