# -*- coding: utf-8 -*-
"""诊断规则：指标窗口 → 发现清单 + 有界自调参决策。

红线约束：自调参只允许动两个旋钮——
  旋钮1 选择弹窗等待秒（界 12~40、逐日 ±5；「等待不足」证据立即 +5 回撤）
  旋钮2 贴图渲染宽限秒（界 3~15、逐日 ±2；超时证据 +2，三日零超时 -2）
拟人间隔（风控核心）永不自动调整；其余一切只出报告。
"""
from __future__ import annotations

from dataclasses import dataclass

from .metrics import DayMetrics

PICKER_BOUNDS = (12, 40)
PICKER_STEP = 5
GRACE_BOUNDS = (3, 15)
GRACE_STEP = 2


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


def _guard_manual(key: str, now: int, bounds: tuple[int, int],
                  findings: list[Finding]) -> bool:
    """越界值（人工覆盖）不动，只提示。返回 True=越界。"""
    lo, hi = bounds
    if not (lo <= now <= hi):
        findings.append(Finding(
            "warn", "调参护栏",
            f"{key}={now}s 超出自动区间 {lo}~{hi}",
            "确认是否人工有意设定；如否请修正 config.ini"))
        return True
    return False


def _tune_picker(window: list[DayMetrics], picker_now: int,
                 findings: list[Finding]) -> Tune:
    """旋钮1：选择弹窗等待秒（等待不足→回撤；弹窗消失→收窄）。"""
    latest = window[-1]
    picker_seen = sum(w.picker_seen for w in window)
    button_missing = sum(w.button_missing for w in window)
    days = len(window)
    if button_missing > 0:
        new = _clamp(picker_now + PICKER_STEP, *PICKER_BOUNDS)
        reason = (f"窗口内 {button_missing} 次「发表」按钮未找到→疑似等待不足，"
                  f"+{PICKER_STEP}s 回撤")
    elif picker_seen == 0:
        new = _clamp(picker_now - PICKER_STEP, *PICKER_BOUNDS)
        reason = (f"近 {days} 天账号选择弹窗 0 次出现且无按钮失败"
                  f"→等待窗收窄 -{PICKER_STEP}s")
    else:
        new, reason = picker_now, f"近 {days} 天弹窗出现 {picker_seen} 次，维持现状"
    if _guard_manual("选择弹窗等待秒", picker_now, PICKER_BOUNDS, findings):
        new = picker_now
        reason = f"当前值 {picker_now}s 在自动边界外，视为人工设定，不改"
    return Tune(key="选择弹窗等待秒", old=picker_now, new=new, reason=reason)


def _tune_grace(window: list[DayMetrics], grace_now: int) -> Tune:
    """旋钮2：贴图渲染宽限秒（超时证据→放宽；三日零超时→收窄）。"""
    timeouts = sum(w.render_grace_timeouts for w in window)
    if timeouts > 0:
        new = _clamp(grace_now + GRACE_STEP, *GRACE_BOUNDS)
        reason = (f"窗口内贴图渲染宽限超时 {timeouts} 次→贴图 tab 渲染偏慢，"
                  f"+{GRACE_STEP}s 放宽")
    elif len(window) >= 3:
        new = _clamp(grace_now - GRACE_STEP, *GRACE_BOUNDS)
        reason = (f"近 {len(window)} 天零宽限超时→收窄 -{GRACE_STEP}s 省时")
    else:
        new, reason = grace_now, "观察窗口不足 3 天，维持现状"
    return Tune(key="贴图渲染宽限秒", old=grace_now, new=new, reason=reason)


def analyze(window: list[DayMetrics], picker_now: int,
            grace_now: int | None = None
            ) -> tuple[list[Finding], list[Tune]]:
    """观察窗口（含当日）→ (发现, 调参决策列表)。"""
    findings: list[Finding] = []
    if not window:
        tunes = [Tune(key="选择弹窗等待秒", old=picker_now, new=picker_now,
                      reason="无历史数据，维持现状")]
        if grace_now is not None:
            tunes.append(Tune(key="贴图渲染宽限秒", old=grace_now,
                              new=grace_now, reason="无历史数据，维持现状"))
        findings.append(Finding("info", "数据", "观察窗口为空",
                                "首次运行属正常，次日起开始积累"))
        return findings, tunes

    latest = window[-1]
    tunes = [_tune_picker(window, picker_now, findings)]
    if grace_now is not None:
        if _guard_manual("贴图渲染宽限秒", grace_now, GRACE_BOUNDS, findings):
            tunes.append(Tune(key="贴图渲染宽限秒", old=grace_now,
                              new=grace_now,
                              reason=f"当前值 {grace_now}s 在自动边界外，"
                                     f"视为人工设定，不改"))
        else:
            tunes.append(_tune_grace(window, grace_now))

    # —— 只报告不自动改的发现 ——
    if latest.selector_drift:
        findings.append(Finding(
            "risk", "选择器漂移", f"当日 {latest.selector_drift} 条漂移告警",
            "运行 run.py --recon 存档页面结构，由维护会话更新选择器"))
    if latest.session_lost:
        findings.append(Finding(
            "risk", "会话重置", f"当日 {latest.session_lost} 次会话被重置",
            "核对 cookie 有效期与登录环境；必要时人工扫码一次"))
    if latest.login_timeout:
        findings.append(Finding(
            "risk", "登录超时", f"当日 {latest.login_timeout} 次等扫超时",
            "保活未起效或用户不在场；核查 23:00 保活是否执行"))
    if latest.scan_ok:
        findings.append(Finding(
            "warn", "保活登录", f"当日人工扫码 {latest.scan_ok} 次",
            "隔夜失效未消除：核查 23:00 睡前保活日志；连续多日则维护会话"
            "介入（如加密保活频次）"))
    if latest.keepalive_dead:
        findings.append(Finding(
            "warn", "保活晨检", f"晨检发现失效账号 {latest.keepalive_dead} 个",
            "07:00 预警已发企微；当日 09:00 预检需现场扫码兜底"))
    errors_real = latest.errors - latest.lock_blocked
    if errors_real > 0:
        findings.append(Finding(
            "warn", "异常归因", f"当日 ERROR {errors_real} 条（设计内让路已剔除）",
            "逐条核对 autopub.log；同类错误连续两日出现即进维护会话"))
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
            f"单篇流程均值 {latest.flow_mean_s}s（文章 {latest.flow_article_mean_s}s"
            f" / 贴图 {latest.flow_pic_mean_s}s）/ 篇间均值 "
            f"{latest.gap_article_mean_s}s（文章）",
            "对照历史趋势观察改进效果"))
    if latest.gold_pass and latest.db_published \
            and latest.gold_pass != latest.db_published:
        findings.append(Finding(
            "warn", "账实核对",
            f"日志金标准 {latest.gold_pass} vs DB published {latest.db_published}",
            "两源不一致，检查是否存在跨日时间戳或日志轮转"))
    return findings, tunes
