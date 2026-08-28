# -*- coding: utf-8 -*-
"""一次性旁路发布：在指定闲置浏览器上用生产级 DraftPublisher 发布贴图草稿。

不动 run.py（用户要求少折腾）；独立进程附加既有浏览器（登录态现成）。
用法: python oneshot_publish.py acct01
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config                      # noqa: E402
from src.logger import setup_logging                   # noqa: E402
from src.browser.session import BrowserSession         # noqa: E402
from src.browser.drafts import DraftPublisher          # noqa: E402
from src.core.state import StateDB                     # noqa: E402

setup_logging()


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "acct01"
    account = sys.argv[2] if len(sys.argv) > 2 else "总包说"
    fast = len(sys.argv) > 3 and sys.argv[3] == "fast"
    cfg = load_config()
    state = StateDB()
    session = BrowserSession(cfg, profile)
    session.start()
    try:
        # 登录态自检（死则退出，绝不弹二维码）
        if not session.is_logged_in():
            print(f"[{profile}] 会话已失效，退出（不弹码）")
            return 1
        session.start_minimize_watchdog()   # 运行期间窗口始终保持最小化
        # fast=贴图批清专用：篇间 2~3.3 分钟（图片轻量内容），文章仍走 3~5 分钟
        pub = DraftPublisher(session, cfg, state, None, account_name=account,
                             gap_range=(120, 200) if fast else None)
        results = pub.publish_recent_drafts()
        for r in results:
            mark = "OK " if r.ok else "FAIL"
            print(f"{mark} {r.item.title[:32]} | {(r.detail or '')[:60]}")
        print(f"完成: {sum(1 for r in results if r.ok)}/{len(results)}")
        return 0
    finally:
        session.stop()


if __name__ == "__main__":
    raise SystemExit(main())
