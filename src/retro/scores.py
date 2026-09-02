# -*- coding: utf-8 -*-
"""四维得分卡引擎：单日运行 → 保活登录/安全效率/稳定顺畅/无人值守。

用户 2026-09-01 指令：复盘须给出"各关键环节得分与 100 分差距之处"。
设计原则：
- **只评最近一次全量跑**（窗口最后一天）——历史已修复的问题不重复
  追责（09-01 实证：3 日聚合把前两日 196 条已修复漂移算进当日账，
  总分虚低至 38）；观察窗口仅供旋钮调参与趋势参考；
- 每维 100 分起步，只做**有证据的扣分**（每条扣分附人可读理由）；
- 分数下限 0，绝不出现负分；纯函数，无 IO、无副作用、可测。
"""
from __future__ import annotations

from dataclasses import dataclass

from .metrics import DayMetrics

# 效率基线：文章单篇 ≤180s 视为满分（900s 归零）；贴图 ≤60s 满分（300s 归零）
_ARTICLE_BASE_S, ARTICLE_ZERO_S = 180.0, 900.0
_PIC_BASE_S, PIC_ZERO_S = 60.0, 300.0
_ARTICLE_MAX_DED, _PIC_MAX_DED = 60, 25
_PIC_GAP_BOUNDS = (5, 10)       # 贴图专用间隔界（默认同 config 贴图间隔秒）


@dataclass(frozen=True)
class Deduction:
    """一条扣分（理由 + 分值），报告中的"与100分差距"逐项来源。"""

    reason: str
    points: int


@dataclass(frozen=True)
class PillarScore:
    """单维得分。"""

    name: str
    score: int
    deductions: tuple[Deduction, ...]


@dataclass(frozen=True)
class Scorecard:
    """四维得分卡（各 100 分 + 加权总分）。"""

    login: PillarScore            # ① 保活三个公众号的成功率和自动登录
    efficiency: PillarScore       # ② 安全前提下的发布效率
    stability: PillarScore        # ③ 运行顺畅度、生产稳定性与成功率
    unattended: PillarScore       # ④ 定时无人值守稳定运行水平

    @property
    def pillars(self) -> tuple[PillarScore, ...]:
        return (self.login, self.efficiency, self.stability, self.unattended)

    @property
    def total(self) -> int:
        return round(sum(p.score for p in self.pillars) / 4)

    @property
    def grade(self) -> str:
        return ("S" if self.total >= 95 else "A" if self.total >= 85
                else "B" if self.total >= 70 else "C" if self.total >= 55
                else "D")


def _pillar(name: str, deductions: list[Deduction]) -> PillarScore:
    return PillarScore(name=name, score=max(0, 100 - sum(d.points for d in deductions)),
                       deductions=tuple(deductions))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _out_of_bounds(values: list[int], bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    return sum(1 for v in values if v < lo or v > hi)


def _score_login(m: DayMetrics) -> PillarScore:
    """① 保活登录：扫码/登录超时/预检跳过/保活晨检失效都是失分项。"""
    d: list[Deduction] = []
    if m.scan_ok:
        d.append(Deduction(f"人工扫码 {m.scan_ok} 次（每次 -12）",
                           m.scan_ok * 12))
    if m.login_timeout:
        d.append(Deduction(f"登录超时 {m.login_timeout} 次（每次 -20）",
                           m.login_timeout * 20))
    if m.preflight_skip:
        d.append(Deduction(f"预检失效未救回跳过 {m.preflight_skip} 个档案"
                           f"（每个 -10）", m.preflight_skip * 10))
    if m.keepalive_dead:
        d.append(Deduction(f"保活晨检发现失效 {m.keepalive_dead} 个"
                           f"（每个 -15）", m.keepalive_dead * 15))
    return _pillar("保活登录", d)


def _score_efficiency(m: DayMetrics,
                      gap_bounds: tuple[int, int],
                      pic_gap_bounds: tuple[int, int]) -> PillarScore:
    """② 安全效率：流程耗时线性计分 + 间隔越界 + 弹窗风控信号。"""
    d: list[Deduction] = []
    a_mean, p_mean = _mean(m.flow_article), _mean(m.flow_pic)
    if a_mean > _ARTICLE_BASE_S:
        pts = min(_ARTICLE_MAX_DED,
                  round((a_mean - _ARTICLE_BASE_S)
                        / (ARTICLE_ZERO_S - _ARTICLE_BASE_S) * _ARTICLE_MAX_DED))
        d.append(Deduction(f"文章单篇均值 {a_mean:.0f}s 超基线 180s（-{pts}）",
                           pts))
    if p_mean > _PIC_BASE_S:
        pts = min(_PIC_MAX_DED,
                  round((p_mean - _PIC_BASE_S)
                        / (PIC_ZERO_S - _PIC_BASE_S) * _PIC_MAX_DED))
        d.append(Deduction(f"贴图单篇均值 {p_mean:.0f}s 超基线 60s（-{pts}）",
                           pts))
    if m.picker_seen:
        d.append(Deduction(f"账号选择弹窗出现 {m.picker_seen} 次"
                           f"（风控信号，每次 -8）", m.picker_seen * 8))
    art_bad = _out_of_bounds(m.gap_article, gap_bounds)
    pic_bad = _out_of_bounds(m.gap_pic, pic_gap_bounds)
    if art_bad + pic_bad:
        d.append(Deduction(
            f"拟人间隔越界 文章{art_bad}条/贴图{pic_bad}条（每条 -5）",
            (art_bad + pic_bad) * 5))
    return _pillar("安全效率", d)


def _score_stability(m: DayMetrics) -> PillarScore:
    """③ 稳定顺畅：台账失败/真实错误/漂移/空轮/会话重置/账实不符。"""
    d: list[Deduction] = []
    if m.db_failed:
        d.append(Deduction(f"发布台账非published残留 {m.db_failed} 条"
                           f"（每条 -10）", m.db_failed * 10))
    errors_real = m.errors - m.lock_blocked
    if errors_real > 0:
        d.append(Deduction(f"ERROR 级异常 {errors_real} 条（设计内让路已剔除，"
                           f"每条 -8）", errors_real * 8))
    if m.selector_drift:
        d.append(Deduction(f"选择器漂移告警 {m.selector_drift} 条"
                           f"（每条 -3，封顶 30）", min(30, m.selector_drift * 3)))
    empty_extra = max(0, m.empty_breaks - 3)
    if empty_extra:
        d.append(Deduction(f"空轮判终结超出常态 {empty_extra} 轮（每轮 -2，"
                           f"封顶 10）", min(10, empty_extra * 2)))
    if m.session_lost:
        d.append(Deduction(f"会话被平台重置 {m.session_lost} 次（每次 -20）",
                           m.session_lost * 20))
    if m.gold_pass and m.db_published and m.gold_pass != m.db_published:
        d.append(Deduction("日志金标准与 DB 台账不一致（-10）", 10))
    return _pillar("稳定顺畅", d)


def _score_unattended(m: DayMetrics, missing_tasks: int) -> PillarScore:
    """④ 无人值守：人工介入次数 + 定时启动证据 + 定时任务在位率。

    定时证据三源任一即可：窗口随机延时（定时窗口路径）、触发来源埋点、
    锁让路（定时任务准点开火但因主运行在跑而让路——定时系统活着的铁证）。
    """
    d: list[Deduction] = []
    if m.scan_ok:
        d.append(Deduction(f"人工扫码介入 {m.scan_ok} 次（每次 -10）",
                           m.scan_ok * 10))
    scheduled = bool(m.window_delayed or m.trigger_sources or m.lock_blocked)
    ran = bool(m.run_ended or m.gold_pass or m.db_published)
    if ran and not scheduled:
        d.append(Deduction("无定时启动证据（人工/维护启动，-15）", 15))
    if missing_tasks:
        d.append(Deduction(f"定时任务缺失 {missing_tasks} 个（每个 -25）",
                           missing_tasks * 25))
    return _pillar("无人值守", d)


def score_window(window: list[DayMetrics],
                 gap_bounds: tuple[int, int] = (90, 150),
                 pic_gap_bounds: tuple[int, int] = _PIC_GAP_BOUNDS,
                 missing_tasks: int = 0) -> Scorecard:
    """观察窗口 → 四维得分卡（只评窗口最后一天；引擎传入任务缺失数）。"""
    m = window[-1]
    return Scorecard(
        login=_score_login(m),
        efficiency=_score_efficiency(m, gap_bounds, pic_gap_bounds),
        stability=_score_stability(m),
        unattended=_score_unattended(m, missing_tasks),
    )
