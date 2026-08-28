# -*- coding: utf-8 -*-
"""注册新公众号档案并立即跑完整发布管线（旁路，不动 run.py 主链）。

用法:
    python register_publish.py <profile> <目标公众号昵称>
    python register_publish.py acct05 工程行业大脑

流程：拉起该档案的独立浏览器 → 显示二维码等扫码（等待期间窗口
不最小化，方便扫码）→ 「选择账号」弹窗自动选目标号 → 若登成
别的号提示头像切换（弹窗自动点）→ 草稿发布 → 贴图触发 →
贴图草稿发布 → 汇总打印。全程不登出，登录态留在该档案浏览器里。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config                      # noqa: E402
from src.logger import setup_logging                   # noqa: E402
from src.core.state import StateDB                     # noqa: E402
from src.core.orchestrator import Orchestrator         # noqa: E402

setup_logging()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    profile, target = sys.argv[1], sys.argv[2]
    cfg = load_config()
    report = Orchestrator(cfg, StateDB(), None).run_for_profile(
        profile, target_nickname=target)
    if report is None:
        print(f"[{profile}] 未完成（登录失败或超时）")
        return 1
    for r in report.results:
        mark = "OK " if r.ok else "FAIL"
        print(f"{mark} {r.item.title[:32]} | {(r.detail or '')[:60]}")
    ok = sum(1 for r in report.results if r.ok)
    print(f"完成: {ok}/{len(report.results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
