"""WeChat-AutoPub CLI 入口。

用法：
  python run.py --mode run              # 立即运行完整流程（多账号循环）
  python run.py --mode auto             # 定时任务调用（同 run + 启动通知）
  python run.py --mode auto --window 09:00-12:00   # 窗口内随机时刻启动（拟人化）
  python run.py --install-schedule      # 安装每日 09:00 定时任务（重启存活）
  python run.py --uninstall-schedule    # 卸载定时任务
  python run.py --recon                 # 侦察模式：dump 页面 HTML 辅助选择器调试
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
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


def wait_window(window: str) -> None:
    """在 HH:MM-HH:MM 窗口内随机挑一个时刻启动（拟人化，避开固定指纹）。

    错过补跑场景（开机晚于窗口起点）自动收窄随机区间；已过窗口则立即运行。
    """
    try:
        start_s, end_s = window.split("-", 1)
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except ValueError:
        logger.warning("窗口参数 %r 非法（应为 HH:MM-HH:MM），跳过随机延迟直接运行", window)
        return
    now = datetime.now()
    lo_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    hi_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if now >= hi_dt:
        logger.info("已过 %s 窗口，立即运行", window)
        return
    lo = max(0.0, (lo_dt - now).total_seconds())
    hi = (hi_dt - now).total_seconds()
    delay = random.uniform(lo, hi)
    start_at = now + timedelta(seconds=delay)
    logger.info("定时窗口 %s：随机延时 %.0f 秒（预计 %s 启动）",
                window, delay, start_at.strftime("%H:%M:%S"))
    deadline = time.time() + delay
    while time.time() < deadline:
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def _pid_alive(pid: int) -> bool:
    """OpenProcess 探活（只查询不注入；Windows 上 os.kill(pid,0) 会真杀进程，禁用）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:                                        # noqa: BLE001 — 探活失败宁可不跑
        return True


def _acquire_run_lock() -> bool:
    """单实例锁：串行红线（绝不同时跑两套管线抢同一批浏览器）。"""
    lock = Path("data/run.lock")
    if lock.exists():
        try:
            old = int(lock.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old = 0
        if old and _pid_alive(old):
            logger.error("已有运行实例（PID %d），本次退出", old)
            return False
        logger.info("清理残留锁（PID %s 已不存在）", old or "?")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _run_retro_safe() -> None:
    """收官自复盘（安全包装：任何异常不影响退出码与发布结果）。"""
    try:
        from src.retro import run_retro
        summary = run_retro()
        logging.info("[收官] 自复盘：%s", summary)
    except Exception as exc:  # noqa: BLE001
        logging.warning("自复盘异常（不影响发布结果）: %s", exc)


def _retro_only() -> int:
    """--retro：只跑复盘，不碰浏览器不发布。"""
    from src.retro import run_retro
    print(run_retro())
    return 0


def _keepalive() -> int:
    """--keepalive：登录保活巡检（每日 07:00 定时，续命+预警，不发布）。

    逐档案串行访问后台首页刷新会话；失效的尝试一键「登录」恢复；
    救不回的汇总企微预警（下次运行启动预检会要求扫码）。与主运行
    共用 data/run.lock——先到先得，撞车即让路（串行红线）。
    """
    from src.browser.login import preflight_logins
    from src.core.state import StateDB

    cfg = load_config()
    if not _acquire_run_lock():
        logger.info("保活巡检：主运行进行中，本次让路退出")
        return 0
    try:
        state = StateDB()
        profiles = [p for p, _n in state.list_profiles()]
        notifier = _build_notifier(cfg)
        results = preflight_logins(cfg, state, profiles, notifier,
                                   timeout_minutes=0, wait_scan=False)
        dead = [p for p, n in results.items() if not n]
        logger.info("保活巡检：%d/%d 登录有效，失效=%s", len(profiles) - len(dead),
                    len(profiles), "、".join(dead) or "无")
        if dead and notifier:
            notifier.send_action_needed(
                "登录态失效预警",
                "保活巡检：" + "、".join(dead)
                + " 登录态失效（一键恢复未成功）。下次运行启动预检会弹出"
                  "二维码，请在 30 分钟内扫码；或提前用 scan_login.py 补登。")
        if cfg.浏览器.运行结束关闭浏览器:      # 持锁独占，全量收口安全
            try:
                from src.browser.driver import close_project_browsers
                close_project_browsers(Path(cfg.浏览器.Profile根目录))
            except Exception as exc:          # noqa: BLE001
                logger.warning("保活收口失败（无害）: %s", exc)
    finally:
        _release_run_lock()
    return 0


def _close_browsers_if_configured() -> None:
    """任务全部完成后关闭本工具打开的浏览器（用户指令 2026-08-30）。

    只关命令行 --user-data-dir 含本项目 profile 的实例，用户自己的
    浏览器不受影响；登录态存磁盘，下次运行 cookie 免扫码复活。
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — 配置读不了就不收口，无害
        return
    if not cfg.浏览器.运行结束关闭浏览器:
        return
    try:
        from src.browser.driver import close_project_browsers
        close_project_browsers(Path(cfg.浏览器.Profile根目录))
    except Exception as exc:  # noqa: BLE001 — 收口失败不改变退出码
        logging.warning("浏览器收口失败（无害，浏览器保留）: %s", exc)


def _release_run_lock() -> None:
    Path("data/run.lock").unlink(missing_ok=True)


_DONE_DIR = Path(__file__).resolve().parent / "data" / "daily_done"


def _today_done(log_path: Path | None = None,
                marker_dir: Path | None = None) -> bool:
    """当日完成证据（幂等防重跑的判据，三触发共用的安全阀）。

    任一为真即视为今日已完成：① data/daily_done/<今日>.ok 标记文件
    （run_once 成功后写入）；② 当日日志出现「运行结束」（覆盖旧代码
    进程与标记写入前的崩溃残留）。当日无证据 → 允许备份触发重跑
    （content_hash 去重 + 金标准验证天然幂等，重跑无副作用）。
    """
    marker = (marker_dir or _DONE_DIR) / f"{date.today().isoformat()}.ok"
    if marker.exists():
        return True
    log = log_path or (Path(__file__).resolve().parent
                       / "data" / "logs" / "autopub.log")
    if not log.exists():
        return False
    needle = f"{date.today().isoformat()} "
    try:
        return any(needle in ln and "运行结束" in ln
                   for ln in log.read_text(
                       encoding="utf-8", errors="replace").splitlines()[-400:])
    except OSError:
        return False


def _mark_today_done() -> None:
    """当日成功标记（备份/登录触发后续触发据此秒退）。"""
    try:
        _DONE_DIR.mkdir(parents=True, exist_ok=True)
        (_DONE_DIR / f"{date.today().isoformat()}.ok").write_text(
            f"pid={os.getpid()} {datetime.now():%H:%M:%S}", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — 标记失败不影响运行
        logger.warning("写当日完成标记失败: %s", exc)


def _ensure_schedule_health() -> None:
    """三任务定时自愈：被安全软件清除后自动补装（生产级兜底）。

    每次运行开始与收官各查一次——只要当天有任一触发成功过，被清的
    任务就会被自动找回；三任务异名冗余，全灭概率极低。
    """
    try:
        cfg = load_config()
        if not cfg.定时.启用:
            return
        window = f"{cfg.定时.运行时间}-{cfg.定时.最晚运行时间}"
        task_scheduler.ensure_installed(
            command=sys.executable,
            arguments=str(Path(__file__).resolve())
                      + f" --mode auto --window {window}",
            workdir=str(Path(__file__).resolve().parent),
            catch_up=cfg.定时.错过补跑)
    except Exception as exc:  # noqa: BLE001 — 自愈失败不阻断发布
        logger.warning("定时任务自愈检查失败（不影响本次运行）: %s", exc)


def install_schedule() -> int:
    cfg = load_config()
    if not cfg.定时.启用:
        print("config.ini [定时] 启用=否，请改为 是 后重试")
        return 1
    # 打包后用 exe；源码模式用当前 venv 的 python + run.py
    window = f"{cfg.定时.运行时间}-{cfg.定时.最晚运行时间}"
    if getattr(sys, "frozen", False):
        command, args = sys.executable, f"--mode auto --window {window}"
        workdir = str(Path(sys.executable).parent)
    else:
        command = sys.executable
        args = str(Path(__file__).resolve()) + f" --mode auto --window {window}"
        workdir = str(Path(__file__).resolve().parent)
    ok, msg = task_scheduler.install_daily_task(
        command=command, arguments=args, workdir=workdir,
        run_time=cfg.定时.运行时间, catch_up=cfg.定时.错过补跑,
    )
    print(("[OK] " if ok else "[FAIL] ") + msg)
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
    parser.add_argument("--retro", action="store_true",
                        help="仅运行自复盘（观测→诊断→有界调参→报告），不发布")
    parser.add_argument("--keepalive", action="store_true",
                        help="登录保活巡检：逐号刷新会话+失效预警（不发布，定时 07:00）")
    parser.add_argument("--window", default=None, metavar="HH:MM-HH:MM",
                        help="定时随机窗口（如 09:00-12:00）：窗口内随机时刻启动")
    args = parser.parse_args()

    setup_logging()

    if args.install_schedule:
        return install_schedule()
    if args.uninstall_schedule:
        ok, msg = task_scheduler.uninstall_daily_task()
        print(("[OK] " if ok else "[FAIL] ") + msg)
        return 0 if ok else 1
    if args.schedule_status:
        ok, msg = task_scheduler.task_status()
        print(("[OK] " if ok else "[FAIL] ") + msg)
        return 0 if ok else 1
    if args.recon:
        return recon()
    if args.retro:
        return _retro_only()
    if args.keepalive:
        return _keepalive()
    if args.mode:
        _ensure_schedule_health()             # 开跑先自愈：被清的定时任务补回来
        if _today_done():
            logger.info("今日运行已完成（幂等防重），本触发安全退出")
            print("今日运行已完成，无需重复")
            return 0
        if not _acquire_run_lock():           # 锁先于窗口睡眠：重复触发数秒即退
            return 2
        try:
            if args.window:
                wait_window(args.window)      # 只有持锁实例才睡窗口
            rc = run_once(args.mode, max_publish=args.max)
            if rc == 0:
                _mark_today_done()            # 成功标记：后续触发秒退
            return rc
        finally:
            _close_browsers_if_configured()   # 先关浏览器再放锁（单飞覆盖收口）
            _run_retro_safe()                 # 收官自复盘：观测→诊断→有界调参→报告
            _ensure_schedule_health()         # 收官再自愈一次（明日保险）
            _release_run_lock()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
