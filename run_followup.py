# -*- coding: utf-8 -*-
"""维护工具：补跑单个档案（当日补发/新号首发），不动主链与幂等标记。

用法：python run_followup.py <profile> <目标昵称>
例：  python run_followup.py acct07 总包之声

场景：主运行已在跑或已收官（当日完成标记已写），某个档案是新扫码接入
（如 2026-09-01 的 acct07），需要在当日串行补发——此工具直接走
orchestrator.run_for_profile 单档案管线，绕过 run.py 的幂等防重检查
（不会误判「今日已完成」），并自带错号守卫与收口复盘。

红线：必须等其它运行结束（锁已放）后再跑——串行红线不允许并行。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.config import load_config                 # noqa: E402
from src.logger import setup_logging, get_logger   # noqa: E402
from src.core.state import StateDB                 # noqa: E402
from src.core.orchestrator import Orchestrator     # noqa: E402


def _build_notifier(cfg):
    from src.notify.wecom import WecomNotifier
    try:
        return WecomNotifier(cfg.通知.企微Webhook, cfg.通知.通知开关)
    except ValueError:
        return None


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python run_followup.py <profile> <目标昵称>")
        return 2
    profile, nickname = sys.argv[1], sys.argv[2]
    setup_logging()
    logger = get_logger("followup")
    lock = Path("data/run.lock")
    if lock.exists():
        logger.error("[补跑] data/run.lock 存在（主运行可能未结束），拒绝并行")
        return 2
    cfg = load_config()
    orch = Orchestrator(cfg, StateDB(), _build_notifier(cfg))
    report = orch.run_for_profile(profile, target_nickname=nickname)
    ok = report.ok_count if report else 0
    fail = report.fail_count if report else 0
    logger.info("[补跑] %s(%s) 完成：发布成功 %d，失败 %d（触发 %d）",
                profile, nickname, ok, fail,
                report.trigger_count if report else 0)
    try:                                  # 收口：按配置关工具浏览器
        if cfg.浏览器.运行结束关闭浏览器:
            from src.browser.driver import close_project_browsers
            closed = close_project_browsers(Path(cfg.浏览器.Profile根目录))
            logger.info("[补跑] 收口：关闭 %d 个实例", closed)
    except Exception as exc:              # noqa: BLE001 — 收口失败无害
        logger.warning("[补跑] 收口失败（无害）: %s", exc)
    try:                                  # 收官复盘（安全包装）
        from src.retro import run_retro
        logger.info("[补跑] 复盘：%s", run_retro())
    except Exception as exc:              # noqa: BLE001
        logger.warning("[补跑] 复盘失败（不影响结果）: %s", exc)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
