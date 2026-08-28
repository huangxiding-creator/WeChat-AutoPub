"""Windows 任务计划程序：每日 09:00 自动运行，电脑重启不丢任务。

用 XML 方式注册（schtasks 命令行不支持 StartWhenAvailable）：
- StartWhenAvailable=true → 9点没开机，开机后立即补跑（重启存活关键）
- 登录时运行（交互环境，浏览器需要桌面）
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

TASK_NAME = "WeChatAutoPub_Daily_0900"

_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>WeChat-AutoPub 每日自动发布（公众号草稿与贴图）</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{date}T{time}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
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
    cmd = ["schtasks", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="gbk", errors="replace",
        timeout=30,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def install_daily_task(
    *,
    command: str,
    arguments: str = "--mode auto",
    workdir: str,
    run_time: str = "09:00",
    catch_up: bool = True,
) -> tuple[bool, str]:
    """注册每日定时任务。command 传 exe 或 python.exe 路径。

    返回 (成功?, 说明)。用 XML 注册以支持 StartWhenAvailable（错过补跑）。
    """
    hour, minute = run_time.split(":", 1)
    xml = _XML_TEMPLATE.format(
        date="2026-01-01",
        time=f"{int(hour):02d}:{int(minute):02d}",
        catch_up="true" if catch_up else "false",
        command=escape(command),
        arguments=escape(arguments),
        workdir=escape(workdir),
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16",
    ) as f:
        f.write(xml)
        xml_path = f.name
    try:
        code, out = _run_schtasks("/Create", "/F", "/TN", TASK_NAME, "/XML", xml_path)
    finally:
        Path(xml_path).unlink(missing_ok=True)

    if code == 0:
        logger.info("定时任务已注册: %s 每日 %s", TASK_NAME, run_time)
        return True, f"每日 {run_time} 自动运行已安装（错过补跑={'开' if catch_up else '关'}）"
    logger.error("注册定时任务失败: %s", out.strip())
    return False, f"注册失败: {out.strip()}"


def uninstall_daily_task() -> tuple[bool, str]:
    code, out = _run_schtasks("/Delete", "/F", "/TN", TASK_NAME)
    if code == 0:
        return True, "定时任务已卸载"
    return False, f"卸载失败: {out.strip()}"


def task_status() -> tuple[bool, str]:
    """查询任务是否已注册。"""
    code, out = _run_schtasks("/Query", "/TN", TASK_NAME)
    if code == 0:
        return True, out.strip()[:300]
    return False, "未安装"
