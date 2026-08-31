# -*- coding: utf-8 -*-
"""Windows 任务计划程序：三层冗余定时，单点被清也照跑（生产级）。

背景（2026-08-30/31 连续两天实证）：主定时任务在触发时刻前后被外部
清除（疑似安全软件计划任务清理）。单任务=单点故障，故改为三任务异名：

  WeChatAutoPub_Daily_0900    每日 09:00（主）
  WeChatAutoPub_Backup_1137   每日 11:37（窗口内二保险）
  WeChatAutoPub_BootCatchup   每次登录（全天候兜底：早上没开机、白天重启）

三任务执行同一命令，幂等性由 run.py 保证（单实例锁 + 当日完成证据），
重复触发在数秒内安全退出。ensure_installed() 供每次运行自愈补装。
"""
from __future__ import annotations

import getpass
import logging
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

TASK_NAME = "WeChatAutoPub_Daily_0900"          # 兼容旧引用

# 任务名 → 触发器（time=每日HH:MM 列表；logon=登录触发）
TASKS: dict[str, dict] = {
    "WeChatAutoPub_Daily_0900": {"times": ["09:00"]},
    "WeChatAutoPub_Backup_1137": {"times": ["11:37"]},
    "WeChatAutoPub_BootCatchup": {"times": [], "logon": True},
}

_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>WeChat-AutoPub 每日自动发布（公众号草稿与贴图）</Description>
  </RegistrationInfo>
  <Triggers>{time_triggers}{logon_trigger}
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>{catch_up}</StartWhenAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run_schtasks(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["schtasks", *args], capture_output=True, text=True,
        encoding="gbk", errors="replace", timeout=30,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def build_xml(*, times: list[str], logon: bool, command: str, arguments: str,
              workdir: str, catch_up: bool, logon_user: str = "") -> str:
    """生成任务 XML（纯函数，可测）。支持多每日时刻 + 登录触发。

    logon_user：限定登录触发的用户（DOMAIN\\user）。不限定=任意用户登录
    都触发 → 注册需管理员权限（实测拒绝访问）；限定当前用户免提权。
    """
    def one(t: str) -> str:
        h, m = t.split(":", 1)
        return ("\n    <CalendarTrigger>"
                f"<StartBoundary>2026-01-01T{int(h):02d}:{int(m):02d}:00</StartBoundary>"
                "<Enabled>true</Enabled>"
                "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
                "</CalendarTrigger>")
    time_triggers = "".join(one(t) for t in times)
    user_xml = (f"<UserId>{escape(logon_user)}</UserId>" if logon_user else "")
    logon_trigger = (f"\n    <LogonTrigger><Enabled>true</Enabled>{user_xml}"
                     "</LogonTrigger>" if logon else "")
    return _XML_TEMPLATE.format(
        time_triggers=time_triggers, logon_trigger=logon_trigger,
        catch_up="true" if catch_up else "false",
        command=escape(command), arguments=escape(arguments),
        workdir=escape(workdir),
    )


def _register(name: str, xml: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16",
    ) as f:
        f.write(xml)
        xml_path = f.name
    try:
        code, out = _run_schtasks("/Create", "/F", "/TN", name, "/XML", xml_path)
    finally:
        Path(xml_path).unlink(missing_ok=True)
    if code == 0:
        logger.info("定时任务已注册: %s", name)
        return True, name
    logger.error("注册 %s 失败: %s", name, out.strip())
    return False, f"{name}: {out.strip()}"


def install_daily_task(*, command: str, arguments: str = "--mode auto",
                       workdir: str, run_time: str = "09:00",
                       catch_up: bool = True) -> tuple[bool, str]:
    """兼容旧签名：只装主任务（内部走多任务安装）。"""
    ok, msg, _ = install_all(command=command, arguments=arguments,
                             workdir=workdir, catch_up=catch_up)
    return ok, msg


def install_all(*, command: str, arguments: str, workdir: str,
                catch_up: bool = True) -> tuple[bool, str, list[str]]:
    """注册全部三任务（/F 幂等覆盖）。返回 (全成功?, 摘要, 失败清单)。"""
    installed: list[str] = []
    fails: list[str] = []
    for name, spec in TASKS.items():
        xml = build_xml(times=spec.get("times", []),
                        logon=spec.get("logon", False),
                        command=command, arguments=arguments,
                        workdir=workdir, catch_up=catch_up,
                        logon_user=getpass.getuser())
        ok, info = _register(name, xml)
        (installed if ok else fails).append(info)
    summary = (f"{len(installed)}/{len(TASKS)} 任务就绪（"
               + "、".join(installed) + "）"
               + (f"；失败: {fails}" if fails else ""))
    return not fails, summary, fails


def task_exists(name: str) -> bool:
    code, _ = _run_schtasks("/Query", "/TN", name)
    return code == 0


def ensure_installed(*, command: str, arguments: str, workdir: str,
                     catch_up: bool = True) -> list[str]:
    """自愈：补装缺失的任务，返回本次补装的任务名清单。"""
    repaired: list[str] = []
    for name, spec in TASKS.items():
        if task_exists(name):
            continue
        xml = build_xml(times=spec.get("times", []),
                        logon=spec.get("logon", False),
                        command=command, arguments=arguments,
                        workdir=workdir, catch_up=catch_up,
                        logon_user=getpass.getuser())
        ok, _ = _register(name, xml)
        if ok:
            repaired.append(name)
    if repaired:
        logger.warning("定时任务自愈：补装 %s", "、".join(repaired))
    return repaired


def uninstall_daily_task() -> tuple[bool, str]:
    fails = []
    for name in TASKS:
        code, out = _run_schtasks("/Delete", "/F", "/TN", name)
        if code != 0 and "找不到" not in out:
            fails.append(name)
    if fails:
        return False, f"卸载失败: {fails}"
    return True, f"已卸载 {len(TASKS)} 个定时任务"


def task_status() -> tuple[bool, str]:
    lines = []
    all_ok = True
    for name in TASKS:
        ok = task_exists(name)
        all_ok = all_ok and ok
        lines.append(f"{'[OK]' if ok else '[缺失]'} {name}")
    return all_ok, " | ".join(lines)
