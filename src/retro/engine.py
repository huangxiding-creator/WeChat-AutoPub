# -*- coding: utf-8 -*-
"""自复盘引擎入口：每日收官后调用（run.py finally 接线 / --retro 手动）。

V2（2026-09-01 用户指令）闭环：观测→四维打分→诊断→有界调参→落盘→
改进积压→低分/风险企微预警。任何内部异常都不外抛——复盘永远不影响
发布主流程。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from ..config import load_config
from ..constants import PROJECT_ROOT
from .metrics import build_metrics
from .parse import filter_day, read_events
from .report import render_report, update_backlog, write_outputs
from .rules import analyze
from .scores import score_window
from .tuner import apply_tune

logger = logging.getLogger(__name__)


def _missing_scheduled_tasks() -> int:
    """定时任务在位数（安全包装：查询失败按 0 处理，不阻断复盘）。"""
    try:
        from ..scheduler import task_scheduler
        _ok, line = task_scheduler.task_status()
        return line.count("[缺失]")
    except Exception:  # noqa: BLE001 — 复盘绝不被外围拖垮
        return 0


def _notify_low_score(total: int, grade: str, threshold: int,
                      summary_lines: list[str]) -> None:
    """低分/风险企微预警（阈值 0=关；通知失败静默）。"""
    if threshold <= 0 or total >= threshold:
        return
    try:
        from ..notify.wecom import WecomNotifier
        cfg = load_config()
        notifier = WecomNotifier(cfg.通知.企微Webhook, cfg.通知.通知开关)
        notifier.send_action_needed(
            "复盘低分预警",
            f"今日自复盘总分 {total}（{grade} 级，阈值 {threshold}）\n"
            + "\n".join(summary_lines[:8]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[复盘] 低分预警发送失败（无害）: %s", exc)


def run_retro(day: date | None = None,
              log_path: Path | None = None,
              db_path: Path | None = None,
              ini_path: Path | None = None) -> str:
    """跑一次复盘：观测→打分→诊断→调参→落盘→积压→预警。返回一句话摘要。"""
    cfg = load_config()
    if not cfg.复盘.开关:
        return "复盘开关关闭，跳过"
    day = day or date.today()
    log_path = log_path or (PROJECT_ROOT / "data" / "logs" / "autopub.log")
    db_path = db_path or (PROJECT_ROOT / "data" / "state.db")
    ini_path = ini_path or (PROJECT_ROOT / "config.ini")

    events = read_events(log_path)
    window = [build_metrics(filter_day(events, day - timedelta(days=i)),
                            day - timedelta(days=i), db_path)
              for i in range(cfg.复盘.观察天数 - 1, -1, -1)]
    window = [w for w in window if w.gold_pass or w.db_published]
    if not window:                       # 观察窗内无运行痕迹→不调参不落盘
        logger.info("[复盘] 观察窗口内无运行记录，跳过")
        return "观察窗口无运行记录，跳过"

    findings, tunes = analyze(
        window, picker_now=cfg.草稿.选择弹窗等待秒,
        grace_now=cfg.草稿.贴图渲染宽限秒)
    applied = [apply_tune(t, ini_path) for t in tunes]
    scorecard = score_window(
        window,
        gap_bounds=(cfg.草稿.每篇间隔最小秒, cfg.草稿.每篇间隔最大秒),
        missing_tasks=_missing_scheduled_tasks())
    latest = window[-1]
    report = render_report(day, latest, findings, scorecard, tunes, applied)
    report_dir = Path(cfg.复盘.报告目录)
    md_path, _csv_path = write_outputs(report_dir, latest, report,
                                       scorecard, tunes)

    # 改进积压：risk/warn 按日累积账龄，未再现自动消项
    backlog_path = report_dir / "backlog.md"
    try:
        prev = backlog_path.read_text(encoding="utf-8") \
            if backlog_path.exists() else ""
        backlog_path.write_text(update_backlog(prev, findings, day),
                                encoding="utf-8", newline="\n")
    except OSError as exc:  # noqa: BLE001 — 积压失败不阻断
        logger.warning("[复盘] 改进积压更新失败（无害）: %s", exc)

    logger.info("[复盘] %s：金标准 %d / DB %d，四维 %d/%d/%d/%d 总分 %d（%s），"
                "发现 %d 条，调参 %s",
                day, latest.gold_pass, latest.db_published,
                scorecard.login.score, scorecard.efficiency.score,
                scorecard.stability.score, scorecard.unattended.score,
                scorecard.total, scorecard.grade, len(findings),
                "、".join(f"{t.key}:{t.old}->{t.new}" if t.changed
                          else f"{t.key}维持" for t in tunes))
    summary = (f"金标准 {latest.gold_pass}、总分 {scorecard.total}"
               f"（{scorecard.grade} 级：保活{scorecard.login.score}/效率"
               f"{scorecard.efficiency.score}/稳定{scorecard.stability.score}/"
               f"值守{scorecard.unattended.score}）、发现 {len(findings)} 条、"
               f"调参 {'；'.join(f'{t.key} {t.old}s→{t.new}s' for t in tunes)}"
               f"，报告 {md_path.name}")
    for f in findings:
        if f.sev == "risk":
            logger.warning("[复盘][%s] %s：%s", f.cat, f.evidence, f.suggestion)

    ded_lines = [f"-{d.points} {p.name}：{d.reason}"
                 for p in scorecard.pillars for d in p.deductions]
    _notify_low_score(scorecard.total, scorecard.grade,
                      cfg.复盘.告警阈值分, ded_lines or ["无扣分明细"])
    return summary
