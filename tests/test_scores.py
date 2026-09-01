# -*- coding: utf-8 -*-
"""四维得分卡测试：保活登录/安全效率/稳定顺畅/无人值守（各 100 分）。"""
from datetime import date

from src.retro.metrics import DayMetrics
from src.retro.scores import score_window


def _day(**kw) -> DayMetrics:
    """近乎完美的一天（默认值），按需注入缺陷。"""
    base = dict(day=date(2026, 9, 1), gold_pass=20, db_published=20,
                accounts_done=3, passwordless_ok=3, window_delayed=1,
                run_ended=1)
    base.update(kw)
    return DayMetrics(**base)


def test_perfect_day_full_score():
    """全自动免扫码零异常：四维全 100，总评 S，零扣分项。"""
    sc = score_window([_day()])
    assert sc.login.score == 100 and sc.efficiency.score == 100
    assert sc.stability.score == 100 and sc.unattended.score == 100
    assert sc.total == 100 and sc.grade == "S"
    assert not any(d.points for p in sc.pillars for d in p.deductions)


def test_login_deductions_itemized():
    """扫码-12/登录超时-20/预检跳过-10/保活失效-15，逐条证据可追溯。"""
    m = _day(passwordless_ok=0, scan_ok=3, login_timeout=1,
             preflight_skip=1, keepalive_dead=1)
    sc = score_window([m])
    assert sc.login.score == 100 - 3 * 12 - 20 - 10 - 15
    reasons = " | ".join(d.reason for d in sc.login.deductions)
    assert "扫码" in reasons and "登录超时" in reasons


def test_efficiency_slowness_linear():
    """文章 540s（基线180/归零900）→ -30；贴图 300s（基线60/归零300）→ -25。"""
    m = _day(flow_article=[540.0], flow_pic=[300.0])
    sc = score_window([m])
    assert sc.efficiency.score == 100 - 30 - 25


def test_stability_penalties():
    """漂移-3/条（封顶30）、真实错误-8/条、失败台账-10/条。"""
    m = _day(selector_drift=5, errors=2, lock_blocked=1, db_failed=1)
    sc = score_window([m])     # 2 条 ERROR 中 1 条为设计内让路 → 只罚 1
    assert sc.stability.score == 100 - 15 - 8 - 10


def test_unattended_human_scans_penalized():
    """人工扫码即人工介入 -10/次；无定时启动证据 -15。"""
    sc = score_window([_day(scan_ok=2, window_delayed=0)])
    assert sc.unattended.score == 100 - 20 - 15
    sc2 = score_window([_day(scan_ok=0, window_delayed=1)])
    assert sc2.unattended.score == 100


def test_missing_scheduled_tasks_hit_unattended():
    """定时任务缺失 -25/个（engine 查询 schtasks 后传入）。"""
    sc = score_window([_day()], missing_tasks=2)
    assert sc.unattended.score == 50


def test_floor_zero():
    """重灾日也只会到 0 分，不出负数。"""
    sc = score_window([_day(scan_ok=9, login_timeout=5, db_failed=9)])
    assert sc.login.score == 0
    assert sc.stability.score == 10
    assert sc.total >= 0 and sc.total <= 100


def test_gap_violation_deduction():
    """文章间隔越出配置界（90~150）每条 -5；贴图界 20~50 同理。"""
    m = _day(gap_article=[95, 200, 160], gap_pic=[30, 80])
    sc = score_window([m], gap_bounds=(90, 150))
    assert sc.efficiency.score == 100 - 5 * 3      # 文章2条 + 贴图1条
