"""浏览器启动：自己拉起 Chrome 进程（完整参数）+ DrissionPage 按地址接管。

确定性原则（绕开一切启动魔法）：
- 启动参数由本模块 100% 控制：--user-data-dir + --remote-debugging-port 固定
- 端口活 → 已有实例（同 profile），直接接管
- 端口死 → 用完整参数启动新进程，等端口就绪再接管
- 教训链：auto_port 随机端口→临时 profile；DrissionPage 自动定位会复用
  其它项目的浏览器实例——都不能用
"""
from __future__ import annotations

import logging
import socket
import subprocess
import time
import zlib
from pathlib import Path

from DrissionPage import ChromiumOptions

logger = logging.getLogger(__name__)

_PORT_BASE = 19000
_PORT_SPAN = 2000
_STARTUP_TIMEOUT = 20

_BROWSER_CANDIDATES: tuple[Path, ...] = (
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
)

_ANTI_DETECT_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
)


def locate_browser(custom_path: str = "") -> Path | None:
    """按优先级探测本机 Chrome / Edge。"""
    if custom_path:
        p = Path(custom_path)
        if p.exists():
            return p
        logger.warning("INI 浏览器路径不存在: %s", custom_path)
    for c in _BROWSER_CANDIDATES:
        if c.exists():
            return c
    return None


def fixed_port(profile_name: str) -> int:
    """profile 名 → 固定调试端口（同 profile 永远同端口）。"""
    return _PORT_BASE + zlib.crc32(profile_name.encode("utf-8")) % _PORT_SPAN


def port_alive(port: int) -> bool:
    """端口是否已有浏览器调试服务在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_browser_running(profile_dir: Path, port: int,
                           browser_path: Path | None) -> bool:
    """确保指定端口上有浏览器实例（同 profile）。已活→跳过；死了→启动。"""
    if port_alive(port):
        logger.info("端口 %d 已有实例（同 profile），直接接管", port)
        return True

    browser = browser_path or locate_browser()
    if browser is None:
        raise RuntimeError(
            "未找到 Chrome/Edge，请在 config.ini [浏览器] 浏览器路径 填入 exe 路径"
        )
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(browser),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--start-maximized",
        "--new-window",
        "about:blank",
        *_ANTI_DETECT_ARGS,
    ]
    logger.info("启动浏览器: %s profile=%s port=%d", browser.name, profile_dir.name, port)
    # DETACHED：独立进程，python 退出/重启浏览器都活着（会话永不丢）
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    deadline = time.time() + _STARTUP_TIMEOUT
    while time.time() < deadline:
        if port_alive(port):
            time.sleep(1.0)                     # 端口起好后 CDP 还需片刻就绪
            logger.info("浏览器就绪 port=%d", port)
            return True
        time.sleep(0.5)
    logger.error("浏览器启动超时（%ds）port=%d", _STARTUP_TIMEOUT, port)
    return False


def build_chromium_options(profile_dir: Path, port: int,
                           headless: bool = False) -> ChromiumOptions:
    """构造接管用的 options（只连地址，不再让 DrissionPage 自己启动）。"""
    co = ChromiumOptions()
    co.set_address(f"127.0.0.1:{port}")
    co.headless(headless)
    co.set_user_data_path(str(profile_dir))     # 记录用途，实际目录由启动参数保证
    return co
