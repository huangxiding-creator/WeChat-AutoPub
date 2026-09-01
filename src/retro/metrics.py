# -*- coding: utf-8 -*-
"""事件流 + DB 台账 → 单日运行指标（DayMetrics）。

V2（2026-09-01 用户指令）：在原发布观测之上，补齐四大关键业务环节的
观测埋点——保活登录（免扫码/扫码/一键恢复/预检/保活巡检）、值守观测
（触发来源/定时证据/幂等让路/自愈补装/错误分级）、贴图与文章流程分离
计时、贴图渲染宽限超时（旋钮2证据）。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .parse import Event

_GAP = re.compile(r"拟人间隔 (\d+) 秒（(贴图专用|文章) ")
_GOLD = re.compile(r"金标准通过：《(.+?)(?:…|》)")
_START = re.compile(r"开始发布(草稿|贴图)《(.+?)(?:…|》)")
_TRIGGER = re.compile(r"触发来源=(\w+)")
_KEEPALIVE_DEAD = re.compile(r"保活巡检：\d+/\d+ 登录有效，失效=\[(.*?)\]")


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
    # —— V2：保活登录观测（环节①）——
    passwordless_ok: int = 0               # cookie 有效，免扫码登录
    oneclick_recovered: int = 0            # 一键「登录」恢复会话成功
    oneclick_failed: int = 0               # 一键「登录」未恢复会话
    scan_ok: int = 0                       # 人工扫码成功（预检内 + scan 工具）
    login_timeout: int = 0                 # 登录超时（等扫无人）
    preflight_ok: int = 0                  # 预检直接有效
    preflight_dead: int = 0                # 预检发现失效
    preflight_skip: int = 0                # 预检失效未救回、跳过档案
    keepalive_dead: int = 0                # 保活巡检晨检发现的失效账号数
    keepalive_yield: int = 0               # 保活让路主运行（设计内，健康信号）
    # —— V2：值守观测（环节④）——
    trigger_sources: set[str] = field(default_factory=set)  # 触发来源=daily…
    window_delayed: int = 0                # 定时窗口随机延时（定时启动证据）
    run_ended: int = 0                     # 「运行结束」收官行
    idempotent_exit: int = 0               # 幂等防重安全退出
    selfheal_fixed: int = 0                # 定时任务自愈补装
    lock_blocked: int = 0                  # 单实例锁让路（设计内 ERROR）
    # —— V2：稳定性与效率细化（环节②③）——
    errors: int = 0                        # ERROR 级行数（含设计内让路）
    warnings: int = 0                      # WARNING 级行数
    render_grace_timeouts: int = 0         # 贴图渲染宽限超时（旋钮2证据）
    flow_article: list[float] = field(default_factory=list)  # 文章单篇耗时
    flow_pic: list[float] = field(default_factory=list)      # 贴图单篇耗时

    @property
    def flow_mean_s(self) -> float:
        return round(sum(self.flow_seconds) / len(self.flow_seconds), 1) \
            if self.flow_seconds else 0.0

    @property
    def flow_article_mean_s(self) -> float:
        return round(sum(self.flow_article) / len(self.flow_article), 1) \
            if self.flow_article else 0.0

    @property
    def flow_pic_mean_s(self) -> float:
        return round(sum(self.flow_pic) / len(self.flow_pic), 1) \
            if self.flow_pic else 0.0

    @property
    def gap_article_mean_s(self) -> float:
        return round(sum(self.gap_article) / len(self.gap_article), 1) \
            if self.gap_article else 0.0


def build_metrics(events: list[Event], day: date, db_path: Path) -> DayMetrics:
    """聚合单日事件 + 查询 DB 台账。"""
    m = DayMetrics(day=day)
    starts: list[tuple[float, str, str]] = []  # (ts, 标题前缀, 草稿|贴图)
    for e in events:
        msg = e.msg
        if e.level == "ERROR":
            m.errors += 1
        elif e.level == "WARNING":
            m.warnings += 1
        if "金标准通过" in msg:
            m.gold_pass += 1
            hit = _GOLD.search(msg)
            if hit:
                key = hit.group(1)[:8]
                for ts, k, kind in list(starts):
                    if k == key:
                        dur = max(0.0, e.ts.timestamp() - ts)
                        m.flow_seconds.append(dur)
                        (m.flow_pic if kind == "贴图"
                         else m.flow_article).append(dur)
                        starts.remove((ts, k, kind))
                        break
        elif "开始发布草稿《" in msg or "开始发布贴图《" in msg:
            hit = _START.search(msg)
            if hit:
                starts.append((e.ts.timestamp(), hit.group(2)[:8], hit.group(1)))
        elif "检测到可见的「选择账号登录」弹窗" in msg:
            m.picker_seen += 1
        elif "「发表」按钮未找到" in msg:
            m.button_missing += 1
        elif "会话被平台重置" in msg or "需重新扫码" in msg:
            m.session_lost += 1
        elif "草稿+贴图全部完成" in msg:
            m.accounts_done += 1
        elif "cookie 有效，免扫码登录" in msg:
            m.passwordless_ok += 1
        elif "一键「登录」恢复会话成功" in msg:
            m.oneclick_recovered += 1
        elif "一键「登录」未恢复会话" in msg:
            m.oneclick_failed += 1
        elif "扫码登录成功 nickname=" in msg or "[scan] 扫码成功" in msg:
            m.scan_ok += 1
        elif "登录超时（" in msg:
            m.login_timeout += 1
        elif "[预检]" in msg and "登录有效 nickname=" in msg:
            m.preflight_ok += 1
        elif "[预检]" in msg and "登录态失效" in msg:
            m.preflight_dead += 1
        elif "[预检]" in msg and "仍失效" in msg:
            m.preflight_skip += 1
        elif "保活巡检：主运行进行中" in msg:
            m.keepalive_yield += 1
        elif "保活巡检：" in msg:
            hit = _KEEPALIVE_DEAD.search(msg)
            if hit and hit.group(1).strip():
                m.keepalive_dead += len(
                    [x for x in hit.group(1).split(",") if x.strip()])
        elif "贴图渲染宽限超时" in msg:
            m.render_grace_timeouts += 1
        elif "触发来源=" in msg:
            hit = _TRIGGER.search(msg)
            if hit:
                m.trigger_sources.add(hit.group(1))
        elif "定时窗口" in msg and "随机延时" in msg:
            m.window_delayed += 1
        elif "运行结束：" in msg:
            m.run_ended += 1
        elif "今日运行已完成（幂等防重）" in msg:
            m.idempotent_exit += 1
        elif "定时任务自愈：补装" in msg:
            m.selfheal_fixed += 1
        elif "已有运行实例" in msg:
            m.lock_blocked += 1
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
