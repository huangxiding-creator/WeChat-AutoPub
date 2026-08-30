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

def parse_wmic_list_output(text: str, profile_root: Path,
                           main_only: bool = False) -> list[int]:
    """解析 `wmic process get /format:list` 输出，返回命令行含 profile_root 的 PID。

    /format:list 按 KEY=VALUE 空行分块，规避命令行内逗号破坏 CSV 解析。
    匹配：CommandLine 含 profile_root 路径（不区分大小写、斜杠归一）。
    main_only=True 时只取带 --remote-debugging-port 的主进程（渲染/GPU
    等子进程虽同 user-data-dir 但无端口参数，交给主进程树杀连带处理）。
    """
    root_norm = str(profile_root).replace("/", "\\").lower()
    pids: list[int] = []
    # wmic /format:list 行尾是 \r\r\n（经典怪癖），全部归一为 \n 再分块
    for block in (text.replace("\r\r\n", "\n")
                      .replace("\r\n", "\n").replace("\r", "\n")
                      .split("\n\n")):
        cmdline = ""
        pid = 0
        for line in block.splitlines():
            if line.startswith("CommandLine="):
                cmdline = line[len("CommandLine="):]
            elif line.startswith("ProcessId="):
                try:
                    pid = int(line[len("ProcessId="):].strip())
                except ValueError:
                    pid = 0
        if not pid:
            continue
        norm = cmdline.replace("/", "\\").lower()
        is_main = ("--remote-debugging-port=" in norm
                   and "--type=" not in norm)   # 子进程带 --type=renderer 等
        if root_norm in norm and not (main_only and not is_main):
            pids.append(pid)
    return pids


def close_project_browsers(profile_root: Path) -> int:
    """关闭本工具启动的所有浏览器实例（收工收口）。

    用户指令（2026-08-30）：任务全部完成后关闭工具打开的所有浏览器窗口。
    识别规则：命令行 --user-data-dir 含本项目 profile 路径——用户自己的
    浏览器不含该路径，零误伤。只杀主进程并 /T 树杀（连带全部子进程）。
    登录态 cookie 存于 profile 磁盘目录，进程关闭不丢，下次运行自动复活
    （2026-08-30 多次进程重启均 cookie 免扫码实证）。wmic 不可用时失败
    开放：浏览器保留，无害。
    """
    query = ("wmic process where \"name='chrome.exe' or name='msedge.exe'\" "
             "get CommandLine,ProcessId /format:list")
    try:
        out = subprocess.run(query, capture_output=True, timeout=20,
                             check=False).stdout
    except Exception as exc:  # noqa: BLE001
        logger.warning("枚举浏览器进程失败（浏览器保留不关）: %s", exc)
        return 0
    pids = parse_wmic_list_output(out.decode("gbk", errors="replace"),
                                  profile_root, main_only=True)
    if not pids:
        logger.info("收口：无本工具浏览器进程需要关闭")
        return 0
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    logger.info("收口：已关闭本工具浏览器 %d 个实例（树杀；登录态保留于磁盘）",
                len(pids))
    return len(pids)


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
