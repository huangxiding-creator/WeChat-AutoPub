# -*- coding: utf-8 -*-
"""诊断规则：指标窗口 → 发现清单 + 有界自调参决策。

红线约束：自调参只允许动 选择弹窗等待秒 一个旋钮，界 12~40、
逐日 ±5；出现任何"等待不足"证据立即 +5 回撤。其余一切只出报告。
"""
from __future__ import annotations

from dataclasses import dataclass

from .metrics import DayMetrics

PICKER_BOUNDS = (12, 40)
PICKER_STEP = 5


@dataclass(frozen=True)
class Finding:
    """一条诊断发现（info/warn/risk 三级）。"""

    sev: str          # info / warn / risk
    cat: str          # 分类标签
    evidence: str     # 数据证据
    suggestion: str   # 建议动作


@dataclass(frozen=True)
class Tune:
    """一次有界自调参决策。"""

    key: str          # config.ini 键名
    old: int
    new: int
    reason: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def analyze(window: list[DayMetrics], picker_now: int) -> tuple[list[Finding], Tune]:
    """观察窗口（含当日）→ (发现, 调参决策)。"""
    findings: list[Finding] = []
    lo, hi = PICKER_BOUNDS
    if window:
        latest = window[-1]
        picker_seen = sum(w.picker_seen for w in window)
        button_missing = sum(w.button_missing for w in window)
        days = len(window)

        # —— 旋钮 1：选择弹窗等待秒（唯一自动调参项）——
        if button_missing > 0:
            new = _clamp(picker_now + PICKER_STEP, lo, hi)
            reason = f"窗口内 {button_missing} 次「发表」按钮未找到→疑似等待不足，+{PICKER_STEP}s 回撤"
        elif picker_seen == 0:
            new = _clamp(picker_now - PICKER_STEP, lo, hi)
            reason = f"近 {days} 天账号选择弹窗 0 次出现且无按钮失败→等待窗收窄 -{PICKER_STEP}s"
        else:
            new, reason = picker_now, f"近 {days} 天弹窗出现 {picker_seen} 次，维持现状"
        if lo <= picker_now <= hi:
            tune = Tune(key="选择弹窗等待秒", old=picker_now, new=new, reason=reason)
        else:  # 越界值（人工覆盖）不动，只提示
            tune = Tune(key="选择弹窗等待秒", old=picker_now, new=picker_now,
                        reason=f"当前值 {picker_now}s 在自动边界外，视为人工设定，不改")
            findings.append(Finding(
                "warn", "调参护栏",
                f"选择弹窗等待秒={picker_now}s 超出自动区间 {lo}~{hi}",
                "确认是否人工有意设定；如否请修正 config.ini"))

        # —— 只报告不自动改的发现 ——
        if latest.selector_drift:
            findings.append(Finding(
                "risk", "选择器漂移", f"当日 {latest.selector_drift} 条漂移告警",
                "运行 run.py --recon 存档页面结构，由维护会话更新选择器"))
        if latest.session_lost:
            findings.append(Finding(
                "risk", "会话重置", f"当日 {latest.session_lost} 次会话被重置",
                "核对 cookie 有效期与登录环境；必要时人工扫码一次"))
        if latest.db_failed:
            findings.append(Finding(
                "warn", "发布台账", f"当日 {latest.db_failed} 条非 published 残留",
                "次日自然重试；连续两天同名残留需人工核查"))
        if latest.empty_breaks >= 3:
            findings.append(Finding(
                "info", "空轮开销",
                f"当日 {latest.empty_breaks} 轮空扫判终结（每轮约 1~2 分钟）",
                "属正常兜底；若持续偏高可下调 空轮重试上限（界 1~3）"))
        if latest.flow_seconds:
            findings.append(Finding(
                "info", "效率画像",
                f"单篇流程均值 {latest.flow_mean_s}s / 篇间均值 "
                f"{latest.gap_article_mean_s}s（文章）",
                "对照历史趋势观察改进效果"))
        if latest.gold_pass and latest.db_published \
                and latest.gold_pass != latest.db_published:
            findings.append(Finding(
                "warn", "账实核对",
                f"日志金标准 {latest.gold_pass} vs DB published {latest.db_published}",
                "两源不一致，检查是否存在跨日时间戳或日志轮转"))
    else:
        tune = Tune(key="选择弹窗等待秒", old=picker_now, new=picker_now,
                    reason="无历史数据，维持现状")
        findings.append(Finding("info", "数据", "观察窗口为空",
                                "首次运行属正常，次日起开始积累"))
    return findings, tune
