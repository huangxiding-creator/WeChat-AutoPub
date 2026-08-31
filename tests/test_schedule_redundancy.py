# -*- coding: utf-8 -*-
"""定时可靠性测试：三任务冗余 XML / 幂等防重判据 / 完成标记。"""
import io
from datetime import date
from pathlib import Path

from run import _mark_today_done, _today_done
from src.scheduler.task_scheduler import TASKS, build_xml


def test_three_redundant_tasks_defined():
    """三任务异名：主 09:00 + 备份 11:37 + 登录兜底。"""
    assert len(TASKS) == 3
    assert "WeChatAutoPub_Daily_0900" in TASKS
    assert "WeChatAutoPub_Backup_1137" in TASKS
    assert "WeChatAutoPub_BootCatchup" in TASKS


def test_build_xml_multi_triggers_and_logon():
    xml = build_xml(times=["09:00", "11:37"], logon=True,
                    command="py", arguments="--mode auto",
                    workdir="w", catch_up=True)
    assert xml.count("<CalendarTrigger>") == 2
    assert "<LogonTrigger>" in xml
    assert "T09:00:00" in xml and "T11:37:00" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    boot = build_xml(times=[], logon=True, command="py",
                     arguments="a", workdir="w", catch_up=True)
    assert "<CalendarTrigger>" not in boot and "<LogonTrigger>" in boot


def test_today_done_marker_and_log_evidence(tmp_path: Path):
    today = date.today().isoformat()
    marker_dir = tmp_path / "daily_done"
    log = tmp_path / "autopub.log"

    # 无任何证据 → 未完成
    assert _today_done(log_path=log, marker_dir=marker_dir) is False

    # 当日「运行结束」日志行 → 完成（覆盖旧代码进程）
    io.open(log, "w", encoding="utf-8").write(
        f"{today} 12:00:00,000 [INFO] run: 运行结束：账号 3 个，成功 48，失败 0\n")
    assert _today_done(log_path=log, marker_dir=marker_dir) is True

    # 昨日的运行结束不算今日（幂等不能跨日误判）
    io.open(log, "w", encoding="utf-8").write(
        "2026-08-30 16:08:31,055 [INFO] run: 运行结束：账号 3 个，成功 48，失败 0\n")
    assert _today_done(log_path=log, marker_dir=marker_dir) is False

    # 标记文件独立生效
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{today}.ok").write_text("pid=1 09:00", encoding="utf-8")
    io.open(log, "w", encoding="utf-8").write("")
    assert _today_done(log_path=log, marker_dir=marker_dir) is True


def test_mark_today_done_writes_marker(tmp_path: Path):
    import run as run_mod
    old = run_mod._DONE_DIR
    run_mod._DONE_DIR = tmp_path
    try:
        run_mod._mark_today_done()
        assert (tmp_path / f"{date.today().isoformat()}.ok").exists()
    finally:
        run_mod._DONE_DIR = old
