# -*- coding: utf-8 -*-
"""复盘报告落盘：data/retro/YYYY-MM-DD.md + trend.csv 趋势累积。"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from .metrics import DayMetrics
from .rules import Finding, Tune

_SEV_ORDER = {"risk": 0, "warn": 1, "info": 2}
_SEV_BADGE = {"risk": "🔴", "warn": "🟡", "info": "🔵"}

_CSV_HEADER = ("date,accounts_done,db_published,db_failed,gold_pass,"
               "flow_mean_s,gap_article_mean_s,picker_seen,selector_drift,"
               "session_lost,empty_breaks,tuned\n")


def render_report(day: date, m: DayMetrics, findings: list[Finding],
                  tune: Tune, applied: bool) -> str:
    """渲染单日复盘 Markdown。"""
    lines = [
        f"# 自复盘报告 {day.isoformat()}",
        "",
        "## 当日总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 账号完成 | {m.accounts_done} |",
        f"| DB published（当日） | {m.db_published} |",
        f"| 涉及账号 | {'、'.join(m.db_accounts) or '—'} |",
        f"| 金标准通过 | {m.gold_pass} |",
        f"| 非published残留 | {m.db_failed} |",
        f"| 单篇流程均值 | {m.flow_mean_s}s |",
        f"| 文章篇间均值 | {m.gap_article_mean_s}s |",
        f"| 选择弹窗出现 | {m.picker_seen} |",
        f"| 选择器漂移告警 | {m.selector_drift} |",
        f"| 会话重置 | {m.session_lost} |",
        f"| 空轮判终结 | {m.empty_breaks} |",
        "",
        "## 自调参",
        "",
        f"- `选择弹窗等待秒`：{tune.old}s → **{tune.new}s**"
        f"（{'已生效' if applied else '未写入'}）",
        f"- 依据：{tune.reason}",
        "",
        "## 发现与建议",
        "",
    ]
    if not findings:
        lines.append("（无异常发现）")
    for f in sorted(findings, key=lambda x: _SEV_ORDER.get(x.sev, 9)):
        lines.append(f"- {_SEV_BADGE.get(f.sev, '·')} **[{f.cat}]** {f.evidence}"
                     f" → {f.suggestion}")
    lines += ["", "## 下一步", "",
              "- 旋钮变化次日 09:00 定时运行自动生效；报告与本文件仅供追溯。",
              "- risk 级发现建议尽快在维护会话（Claude Code）中消化为代码修复。"]
    return "\n".join(lines) + "\n"


def write_outputs(report_dir: Path, m: DayMetrics, report: str,
                  tune: Tune) -> tuple[Path, Path]:
    """写 md + 追加 trend.csv，返回两个路径。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{m.day.isoformat()}.md"
    io.open(md_path, "w", encoding="utf-8", newline="\n").write(report)

    csv_path = report_dir / "trend.csv"
    if not csv_path.exists():
        io.open(csv_path, "w", encoding="utf-8", newline="\n").write(_CSV_HEADER)
    row = (f"{m.day.isoformat()},{m.accounts_done},{m.db_published},"
           f"{m.db_failed},{m.gold_pass},{m.flow_mean_s},"
           f"{m.gap_article_mean_s},{m.picker_seen},{m.selector_drift},"
           f"{m.session_lost},{m.empty_breaks},"
           f"{'picker:' + str(tune.old) + '->' + str(tune.new) if tune.changed else ''}\n")
    with io.open(csv_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(row)
    return md_path, csv_path
