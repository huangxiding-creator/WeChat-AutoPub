"""WeChat-AutoPub CLI 入口。

用法：
  python run.py --mode run              # 立即运行完整流程（多账号循环）
  python run.py --mode auto             # 定时任务调用（同 run + 启动通知）
  python run.py --install-schedule      # 安装每日 09:00 定时任务（重启存活）
  python run.py --uninstall-schedule    # 卸载定时任务
  python run.py --recon                 # 侦察模式：dump 页面 HTML 辅助选择器调试
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config                      # noqa: E402
from src.logger import setup_logging, get_logger        # noqa: E402
from src.core.orchestrator import Orchestrator          # noqa: E402
from src.core.state import StateDB                      # noqa: E402
from src.notify.wecom import WecomNotifier              # noqa: E402
from src.scheduler import task_scheduler                # noqa: E402

logger = get_logger("run")


def _build_notifier(cfg) -> WecomNotifier | None:
    try:
        return WecomNotifier(cfg.通知.企微Webhook, cfg.通知.通知开关)
    except ValueError as exc:
        logger.warning("%s（本次运行不发通知）", exc)
        return None


def run_once(mode: str, max_publish: int | None = None) -> int:
    cfg = load_config()
    if max_publish is not None:
        from dataclasses import replace
        cfg = replace(cfg, 账号=replace(cfg.账号, 单账号单日最大发布数=max_publish))
        logger.info("首跑限流：单账号最多发布 %d 篇", max_publish)
    notifier = _build_notifier(cfg)
    state = StateDB()

    if mode == "auto" and notifier:
        notifier.send_text(
            f"⏰ WeChat-AutoPub 定时任务已启动 {datetime.now():%Y-%m-%d %H:%M}"
        )

    orchestrator = Orchestrator(cfg, state, notifier)
    reports = orchestrator.run()

    ok = sum(r.ok_count for r in reports)
    fail = sum(r.fail_count for r in reports)
    logger.info("运行结束：账号 %d 个，成功 %d，失败 %d", len(reports), ok, fail)
    return 0 if fail == 0 else 1


def recon() -> int:
    """侦察模式：登录后手动浏览目标页面，自动存 HTML/截图到 data/recon/。"""
    from src.browser.session import BrowserSession
    from src.browser.login import ensure_login

    cfg = load_config()
    recon_dir = Path("data/recon")
    recon_dir.mkdir(parents=True, exist_ok=True)
    session = BrowserSession(cfg, "recon")
    session.start()
    try:
        result = ensure_login(session, timeout_minutes=10)
        if not result.ok:
            logger.error("侦察需先登录")
            return 1
        logger.info("登录成功，请在浏览器中手动打开要侦察的页面（草稿箱/发表记录/贴图编辑），"
                    "然后回到终端按回车")
        input("回车后保存当前页面 → ")
        url = session.tab.url or "page"
        safe = "".join(c if c.isalnum() else "_" for c in url[-60:])
        html_path = recon_dir / f"{safe}.html"
        html_path.write_text(session.tab.html or "", encoding="utf-8")
        session.screenshot_evidence("recon")
        logger.info("已保存 %s（%d 字符），请把关键结构反馈给选择器维护", html_path,
                    len(html_path.read_text(encoding='utf-8')))
        return 0
    finally:
        try:
            session.chromium.quit()
        except Exception:  # noqa: BLE001
            pass


def install_schedule() -> int:
    cfg = load_config()
    if not cfg.定时.启用:
        print("config.ini [定时] 启用=否，请改为 是 后重试")
        return 1
    # 打包后用 exe；源码模式用当前 venv 的 python + run.py
    if getattr(sys, "frozen", False):
        command, args = sys.executable, "--mode auto"
        workdir = str(Path(sys.executable).parent)
    else:
        command, args = sys.executable, str(Path(__file__).resolve()) + " --mode auto"
        workdir = str(Path(__file__).resolve().parent)
    ok, msg = task_scheduler.install_daily_task(
        command=command, arguments=args, workdir=workdir,
        run_time=cfg.定时.运行时间, catch_up=cfg.定时.错过补跑,
    )
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="公众号草稿与贴图自动发布")
    parser.add_argument("--mode", choices=["run", "auto"], help="运行模式")
    parser.add_argument("--max", type=int, default=None,
                        help="首跑限流：单账号最多发布 N 篇（如 --max 1）")
    parser.add_argument("--install-schedule", action="store_true", help="安装每日定时任务")
    parser.add_argument("--uninstall-schedule", action="store_true", help="卸载定时任务")
    parser.add_argument("--schedule-status", action="store_true", help="查询定时任务状态")
    parser.add_argument("--recon", action="store_true", help="页面侦察模式（选择器调试）")
    args = parser.parse_args()

    setup_logging()

    if args.install_schedule:
        return install_schedule()
    if args.uninstall_schedule:
        ok, msg = task_scheduler.uninstall_daily_task()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if args.schedule_status:
        ok, msg = task_scheduler.task_status()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if args.recon:
        return recon()
    if args.mode:
        return run_once(args.mode, max_publish=args.max)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
