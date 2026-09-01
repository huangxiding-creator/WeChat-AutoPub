# -*- coding: utf-8 -*-
"""维护工具：为新档案打开公众号登录页等待扫码，成功后登记 profile_registry。

用法：python scan_login.py [profile名]   # 缺省 acct07

只驱动自己的独立档案（独立 profile + 固定端口），绝不触碰其它账号的
浏览器实例；扫码成功→登记→只关闭本档案实例（按 user-data-dir 精准识别）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.config import load_config                    # noqa: E402
from src.logger import setup_logging, get_logger      # noqa: E402
from src.core.state import StateDB                    # noqa: E402
from src.browser.session import BrowserSession        # noqa: E402
from src.browser.login import extract_nickname        # noqa: E402

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "acct07"
KEEP = "--keep" in sys.argv        # 扫完保留浏览器实例（主运行即将接管该档案）

setup_logging()
logger = get_logger("scan")

cfg = load_config()
state = StateDB()
session = BrowserSession(cfg, PROFILE)
session.start()
try:
    session.navigate("https://mp.weixin.qq.com/")
    logger.info("[scan] 二维码已就绪（profile=%s），等待扫码（最多 15 分钟）", PROFILE)
    deadline = time.time() + 15 * 60
    ok = False
    while time.time() < deadline:
        time.sleep(3)
        # 关键：等待期绝不导航/刷新（is_logged_in 会导航首页，会把二维码
        # 刚渲染就刷掉，用户无法扫码——09-01 实战踩坑）。纯被动读 URL，
        # 扫码确认后页面自动跳 home 即判定成功
        try:
            url = session.tab.url or ""
        except Exception:                                   # noqa: BLE001
            continue
        if "cgi-bin/home" not in url and "token=" not in url:
            continue
        nickname = extract_nickname(session)
        if not nickname:
            time.sleep(2)
            nickname = extract_nickname(session)           # 昵称渲染稍慢再取一次
        if nickname:
            state.register_profile(PROFILE, nickname)
            logger.info("[scan] 扫码成功 nickname=%s → 已登记到 %s",
                        nickname, PROFILE)
            ok = True
            break
    if not ok:
        logger.error("[scan] 15 分钟未扫码，退出（未登记，无副作用）")
finally:
    if KEEP:
        try:
            session.stop()          # 只断开接管，浏览器保留给主运行
            logger.info("[scan] --keep：%s 浏览器实例保留", PROFILE)
        except Exception:                                # noqa: BLE001
            pass
        sys.exit(0 if ok else 1)
    try:
        session.chromium.quit()
    except Exception:                                    # noqa: BLE001
        pass
    # 兜底：只杀本档案主实例（user-data-dir 含 profile 名且带调试端口）
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get",
             "CommandLine,ProcessId", "/format:list"],
            capture_output=True, text=True, timeout=30,
        ).stdout or ""
        for block in (out.replace("\r\r\n", "\n").replace("\r\n", "\n")
                      .replace("\r", "\n").split("\n\n")):
            cl = pid = ""
            for ln in block.splitlines():
                if ln.startswith("CommandLine="):
                    cl = ln.split("=", 1)[1]
                elif ln.startswith("ProcessId="):
                    pid = ln.split("=", 1)[1].strip()
            norm = cl.lower().replace("/", "\\")
            if (pid and PROFILE.lower() in norm
                    and "--remote-debugging-port=" in norm
                    and "--type=" not in norm):
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                               capture_output=True)
                logger.info("[scan] 已关闭 %s 浏览器实例 PID=%s", PROFILE, pid)
    except Exception as exc:                             # noqa: BLE001
        logger.warning("[scan] 收口兜底失败（无害）: %s", exc)

sys.exit(0 if ok else 1)
