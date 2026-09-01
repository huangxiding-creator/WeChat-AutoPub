# -*- coding: utf-8 -*-
"""运行日志解析：autopub.log → 带时间戳的事件流。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# "2026-08-30 12:49:21,543 [INFO] src.browser.drafts: 消息"
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\.]\d+ \[(\w+)\] [\w\.]+: (.*)$"
)


@dataclass(frozen=True)
class Event:
    """一条日志事件（时间 + 级别 + 消息；级别供 ERROR 计数分级归因）。"""

    ts: datetime
    msg: str
    level: str = "INFO"


def read_events(log_path: Path) -> list[Event]:
    """全量读取日志文件为事件流；解析失败的行静默跳过。"""
    if not log_path.exists():
        return []
    events: list[Event] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LOG_LINE.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        events.append(Event(ts=ts, msg=m.group(3), level=m.group(2)))
    return events


def filter_day(events: list[Event], day: date) -> list[Event]:
    """只留某天的事件。"""
    return [e for e in events if e.ts.date() == day]
