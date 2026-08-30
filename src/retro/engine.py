# -*- coding: utf-8 -*-
"""自复盘引擎入口：每日收官后调用（run.py finally 接线 / --retro 手动）。"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from ..config import load_config
from ..constants import PROJECT_ROOT
from .metrics import build_metrics
from .parse import filter_day, read_events
from .report import render_report, write_outputs
from .rules import analyze
from .tuner import apply_tune

logger = logging.getLogger(__name__)


def run_retro(day: date | None = None,
              log_path: Path | None = None,
              db_path: Path | None = None,
              ini_path: Path | None = None) -> str:
    """跑一次复盘：观测→诊断→调参→落盘。返回一句话摘要。

    任何内部异常都不外抛——复盘永远不能影响发布主流程。
    """
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

    findings, tune = analyze(window, picker_now=cfg.草稿.选择弹窗等待秒)
    applied = apply_tune(tune, ini_path)
    latest = window[-1]
    report = render_report(day, latest, findings, tune, applied)
    md_path, csv_path = write_outputs(
        Path(cfg.复盘.报告目录), latest, report, tune)

    logger.info("[复盘] %s：金标准 %d / DB %d，发现 %d 条，调参 %s（%s）",
                day, latest.gold_pass, latest.db_published, len(findings),
                f"{tune.old}->{tune.new}" if tune.changed else "维持",
                "已生效" if applied else "未写入")
    summary = (f"金标准 {latest.gold_pass}、发现 {len(findings)} 条、"
               f"弹窗等待 {tune.old}s→{tune.new}s"
               f"（{'已生效' if applied else '维持'}），报告 {md_path.name}")
    for f in findings:
        if f.sev == "risk":
            logger.warning("[复盘][%s] %s：%s", f.cat, f.evidence, f.suggestion)
    _ = csv_path
    return summary
