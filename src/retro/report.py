# -*- coding: utf-8 -*-
"""复盘报告落盘：data/retro/YYYY-MM-DD.md + trend.csv 趋势累积 + backlog.md。

V2（2026-09-01）：
- 报告呈现四维得分卡与逐条扣分证据（与 100 分差距可见）；
- trend.csv 同日幂等去重（主运行+补跑双收官不再重复记行）；
- 旧 12 列表头自动迁移新表头，历史行保留；
- backlog.md 改进积压：risk/warn 未消化项按日累积账龄，未再现自动消项。
"""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path

from .metrics import DayMetrics
from .rules import Finding, Tune
from .scores import Scorecard

_SEV_ORDER = {"risk": 0, "warn": 1, "info": 2}
_SEV_BADGE = {"risk": "🔴", "warn": "🟡", "info": "🔵"}

_CSV_HEADER = ("date,accounts_done,db_published,db_failed,gold_pass,"
               "flow_mean_s,gap_article_mean_s,picker_seen,selector_drift,"
               "session_lost,empty_breaks,score_total,score_login,"
               "score_eff,score_stab,score_unatt,passwordless_n,scan_n,"
               "tuned\n")

# 旧表头（12 列）识别：迁移时旧行补空列到新宽度
_OLD_HEADER_PREFIX = "date,accounts_done,db_published,db_failed,gold_pass,"


def render_report(day: date, m: DayMetrics, findings: list[Finding],
                  scorecard: Scorecard, tunes: list[Tune],
                  applied: list[bool]) -> str:
    """渲染单日复盘 Markdown（含四维得分卡与差距清单）。"""
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
        f"| 单篇流程均值 | {m.flow_mean_s}s（文章 {m.flow_article_mean_s}s / "
        f"贴图 {m.flow_pic_mean_s}s） |",
        f"| 文章篇间均值 | {m.gap_article_mean_s}s |",
        f"| 免扫码登录 | {m.passwordless_ok} 次 |",
        f"| 人工扫码 | {m.scan_ok} 次 |",
        f"| 选择弹窗出现 | {m.picker_seen} |",
        f"| 选择器漂移告警 | {m.selector_drift} |",
        f"| 会话重置 | {m.session_lost} |",
        f"| 空轮判终结 | {m.empty_breaks} |",
        "",
        "## 四维得分卡（各 100 分）",
        "",
        "| 环节 | 得分 |",
        "|---|---|",
    ]
    for p in scorecard.pillars:
        lines.append(f"| {p.name} | {p.score} |")
    lines.append(f"| **总分** | **{scorecard.total}（{scorecard.grade} 级）** |")
    lines += ["", "### 与 100 分差距（逐条扣分证据）", ""]
    any_ded = False
    for p in scorecard.pillars:
        if p.deductions:
            any_ded = True
            lines.append(f"**{p.name}（{p.score}）**：")
            for d in p.deductions:
                lines.append(f"- -{d.points}：{d.reason}")
    if not any_ded:
        lines.append("（四维零扣分——满分校准日）")
    lines += ["", "## 自调参", ""]
    if tunes:
        for t, ok in zip(tunes, applied):
            lines.append(f"- `{t.key}`：{t.old}s → **{t.new}s**"
                         f"（{'已生效' if ok else '未写入'}）")
            lines.append(f"  - 依据：{t.reason}")
    else:
        lines.append("（本次无调参决策）")
    lines += ["", "## 发现与建议", ""]
    if not findings:
        lines.append("（无异常发现）")
    for f in sorted(findings, key=lambda x: _SEV_ORDER.get(x.sev, 9)):
        lines.append(f"- {_SEV_BADGE.get(f.sev, '·')} **[{f.cat}]** {f.evidence}"
                     f" → {f.suggestion}")
    lines += ["", "## 下一步", "",
              "- 旋钮变化次日定时运行自动生效；报告与本文件仅供追溯。",
              "- risk 级发现自动进入 backlog.md 改进积压，由维护会话消化为"
              "代码修复；未再现的项会自动消项。"]
    return "\n".join(lines) + "\n"


def write_outputs(report_dir: Path, m: DayMetrics, report: str,
                  scorecard: Scorecard, tunes: list[Tune]) -> tuple[Path, Path]:
    """写 md + 追加 trend.csv（同日去重、旧表头迁移），返回两个路径。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{m.day.isoformat()}.md"
    io.open(md_path, "w", encoding="utf-8", newline="\n").write(report)

    csv_path = report_dir / "trend.csv"
    day_prefix = m.day.isoformat()
    tuned = ";".join(f"{t.key}:{t.old}->{t.new}" for t in tunes if t.changed)
    row = (f"{day_prefix},{m.accounts_done},{m.db_published},"
           f"{m.db_failed},{m.gold_pass},{m.flow_mean_s},"
           f"{m.gap_article_mean_s},{m.picker_seen},{m.selector_drift},"
           f"{m.session_lost},{m.empty_breaks},{scorecard.total},"
           f"{scorecard.login.score},{scorecard.efficiency.score},"
           f"{scorecard.stability.score},{scorecard.unattended.score},"
           f"{m.passwordless_ok},{m.scan_ok},{tuned}\n")
    existing = ""
    if csv_path.exists():
        existing = csv_path.read_text(encoding="utf-8")
    out_lines: list[str] = [_CSV_HEADER.rstrip("\n")]
    for ln in existing.splitlines():
        if not ln.strip() or ln.startswith("date,"):
            continue                      # 旧表头丢弃，换新表头
        fields = ln.split(",")
        if fields and fields[0] == day_prefix:
            continue                      # 同日旧行丢弃（幂等去重）
        if len(fields) < 12:
            continue                      # 残缺行防御
        out_lines.append(_migrate_old_row(fields))
    out_lines.append(row.rstrip("\n"))
    io.open(csv_path, "w", encoding="utf-8", newline="\n").write(
        "\n".join(out_lines) + "\n")
    return md_path, csv_path


def _migrate_old_row(fields: list[str]) -> str:
    """旧 12 列行 → 新 19 列行（新增列补空，保留历史）。"""
    padded = fields + [""] * (19 - len(fields))
    return ",".join(padded[:19])


_BACKLOG_ROW = re.compile(r"^\| ([^|]+?) \| (\w+) \| (\S+) \| (\d+) \| (.+) \|$")


def update_backlog(prev_text: str, findings: list[Finding],
                   today: date) -> str:
    """改进积压纯函数：risk/warn 分类按日累积账龄；当日未再现自动消项。"""
    entries: dict[str, tuple[str, str, date]] = {}   # cat -> (sev, 证据, 首见)
    for ln in prev_text.splitlines():
        hit = _BACKLOG_ROW.match(ln.strip())
        if hit:
            cat, sev, first, _age, _ev = hit.groups()
            try:
                entries[cat] = (sev, _ev, date.fromisoformat(first))
            except ValueError:
                continue
    for f in findings:
        if f.sev in ("risk", "warn") and f.cat not in entries:
            entries[f.cat] = (f.sev, f.evidence, today)
    fresh = {f.cat for f in findings if f.sev in ("risk", "warn")}
    entries = {c: v for c, v in entries.items() if c in fresh}

    lines = ["# 改进积压（自动维护：risk/warn 未消化项，未再现自动消项）", "",
             "| 分类 | 级别 | 首见 | 账龄天 | 最新证据 |", "|---|---|---|---|---|"]
    for cat, (sev, ev, first) in sorted(entries.items()):
        age = (today - first).days + 1
        lines.append(f"| {cat} | {sev} | {first.isoformat()} | {age} | {ev} |")
    if len(entries) == 0:
        lines.append("（当前无未消化 risk/warn 项——全部闭环）")
    return "\n".join(lines) + "\n"
