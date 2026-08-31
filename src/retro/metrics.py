# -*- coding: utf-8 -*-
"""事件流 + DB 台账 → 单日运行指标（DayMetrics）。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .parse import Event

_GAP = re.compile(r"拟人间隔 (\d+) 秒（(贴图专用|文章) ")
_GOLD = re.compile(r"金标准通过：《(.+?)(?:…|》)")
_START = re.compile(r"开始发布草稿《(.+?)(?:…|》)")


@dataclass
class DayMetrics:
    """单日聚合指标——自复盘的全部输入。"""

    day: date
    gold_pass: int = 0                     # 金标准通过数（发布成功铁证）
    picker_seen: int = 0                   # 账号选择弹窗真出现次数
    button_missing: int = 0                # 「发表」按钮找不到（等待不足线索）
    selector_drift: int = 0                # 选择器漂移告警
    empty_breaks: int = 0                  # 空轮判终结次数（≈整轮空扫开销）
    session_lost: int = 0                  # 会话被平台重置
    accounts_done: int = 0                 # 账号完成数
    gap_article: list[int] = field(default_factory=list)
    gap_pic: list[int] = field(default_factory=list)
    flow_seconds: list[float] = field(default_factory=list)   # 单篇流程耗时
    db_published: int = 0                  # DB 当日 published 记录
    db_failed: int = 0                     # DB 当日 failed/pending 残留
    db_accounts: list[str] = field(default_factory=list)

    @property
    def flow_mean_s(self) -> float:
        return round(sum(self.flow_seconds) / len(self.flow_seconds), 1) \
            if self.flow_seconds else 0.0

    @property
    def gap_article_mean_s(self) -> float:
        return round(sum(self.gap_article) / len(self.gap_article), 1) \
            if self.gap_article else 0.0


def build_metrics(events: list[Event], day: date, db_path: Path) -> DayMetrics:
    """聚合单日事件 + 查询 DB 台账。"""
    m = DayMetrics(day=day)
    starts: list[tuple[float, str]] = []   # (ts, 标题前缀) 待与金标准配对
    for e in events:
        msg = e.msg
        if "金标准通过" in msg:
            m.gold_pass += 1
            hit = _GOLD.search(msg)
            if hit:
                key = hit.group(1)[:8]
                for ts, k in list(starts):
                    if k == key:
                        m.flow_seconds.append(max(0.0, (e.ts.timestamp() - ts)))
                        starts.remove((ts, k))
                        break
        elif "开始发布草稿《" in msg:
            hit = _START.search(msg)
            if hit:
                starts.append((e.ts.timestamp(), hit.group(1)[:8]))
        elif "检测到可见的「选择账号登录」弹窗" in msg:
            m.picker_seen += 1
        elif "「发表」按钮未找到" in msg:
            m.button_missing += 1
        elif "会话被平台重置" in msg or "需重新扫码" in msg:
            m.session_lost += 1
        elif "草稿+贴图全部完成" in msg:
            m.accounts_done += 1
        elif "拟人间隔" in msg:
            hit = _GAP.search(msg)
            if hit:
                (m.gap_pic if hit.group(2) == "贴图专用"
                 else m.gap_article).append(int(hit.group(1)))
        # 空轮终结与漂移可同现一行（文章tab空轮终结告警自带漂移
        # 提示），故独立计数不走 elif 链
        if "次解析到 0 张草稿卡片，结束本轮" in msg:
            m.empty_breaks += 1
        if "选择器可能漂移" in msg:
            m.selector_drift += 1
    _attach_db(m, db_path)
    return m


def _attach_db(m: DayMetrics, db_path: Path) -> None:
    """当日 DB 台账：published/failed 计数与涉及账号。"""
    if not db_path.exists():
        return
    try:
        db = sqlite3.connect(str(db_path))
        day_prefix = m.day.isoformat()
        rows = db.execute(
            "SELECT account, status FROM publish_records WHERE created_at LIKE ?",
            (day_prefix + "%",),
        ).fetchall()
    except sqlite3.Error:
        return
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    for account, status in rows:
        if status == "published":
            m.db_published += 1
            if account not in m.db_accounts:
                m.db_accounts.append(account)
        else:
            m.db_failed += 1
