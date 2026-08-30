# -*- coding: utf-8 -*-
"""自复盘机制测试：解析 / 指标 / 调参规则 / 有界写回。"""
from datetime import date, datetime
from pathlib import Path

from src.retro.metrics import DayMetrics, build_metrics
from src.retro.parse import Event, filter_day, read_events
from src.retro.rules import analyze
from src.retro.rules import PICKER_BOUNDS, Tune
from src.retro.tuner import apply_tune


def _ev(ts: str, msg: str) -> Event:
    return Event(ts=datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), msg=msg)


def test_parse_line_and_day_filter(tmp_path: Path):
    log = tmp_path / "autopub.log"
    log.write_text(
        "2026-08-30 12:00:00,100 [INFO] src.browser.drafts: 金标准通过：《A…》\n"
        "2026-08-29 09:00:00,100 [INFO] x: 草稿+贴图全部完成\n"
        "garbage line\n", encoding="utf-8")
    events = read_events(log)
    assert len(events) == 2
    assert len(filter_day(events, date(2026, 8, 30))) == 1
    assert filter_day(events, date(2026, 8, 30))[0].msg.startswith("金标准通过")


def test_build_metrics_counts(tmp_path: Path):
    evs = [
        _ev("2026-08-30 12:00:00", "开始发布草稿《工程款争议：合同无效…》"),
        _ev("2026-08-30 12:01:30", "✅ 金标准通过：《工程款争议：合同无效…》"),
        _ev("2026-08-30 12:02:00", "拟人间隔 150 秒（文章 90~150 随机）"),
        _ev("2026-08-30 12:03:00", "拟人间隔 35 秒（贴图专用 20~50 随机）"),
        _ev("2026-08-30 12:04:00", "第 2 次解析到 0 张草稿卡片，结束本轮"),
        _ev("2026-08-30 12:05:00", "[acct05] 工程行业大脑 草稿+贴图全部完成"),
        _ev("2026-08-30 12:06:00", "未解析到草稿卡片——选择器可能漂移，建议 run.py --recon"),
    ]
    m = build_metrics(evs, date(2026, 8, 30), tmp_path / "no.db")
    assert m.gold_pass == 1 and m.accounts_done == 1
    assert m.gap_article == [150] and m.gap_pic == [35]
    assert m.empty_breaks == 1 and m.selector_drift == 1
    assert m.flow_seconds == [90.0]     # 12:00:00 → 12:01:30


def _win(**kw) -> list[DayMetrics]:
    base = dict(day=date(2026, 8, 30), gold_pass=5, db_published=5)
    base.update(kw)
    return [DayMetrics(**base)]


def test_rule_shrink_when_picker_never_seen():
    findings, tune = analyze(_win(picker_seen=0, button_missing=0), picker_now=25)
    assert tune.new == 20 and tune.changed
    assert not any(f.sev == "risk" for f in findings)


def test_rule_rollback_on_button_missing():
    findings, tune = analyze(_win(picker_seen=0, button_missing=2), picker_now=25)
    assert tune.new == 30 and "回撤" in tune.reason


def test_rule_floor_and_manual_override():
    _, tune = analyze(_win(), picker_now=PICKER_BOUNDS[0])     # 已在地板
    assert tune.new == PICKER_BOUNDS[0]
    findings, tune = analyze(_win(), picker_now=60)            # 人工越界值
    assert not tune.changed and any("超出自动区间" in f.evidence for f in findings)


def test_tuner_apply_and_bounds(tmp_path: Path):
    ini = tmp_path / "config.ini"
    ini.write_text("[草稿]\n每篇间隔最大秒 = 150\n选择弹窗等待秒 = 25\n",
                   encoding="utf-8")
    ok = apply_tune(Tune("选择弹窗等待秒", 25, 20, "测试"), ini)
    assert ok and "选择弹窗等待秒 = 20" in ini.read_text(encoding="utf-8")
    assert (tmp_path / "config.ini.bak").exists()
    # 无变化/键缺失 → 不动文件
    assert not apply_tune(Tune("选择弹窗等待秒", 20, 20, "同值"), ini)
    ini2 = tmp_path / "other.ini"
    ini2.write_text("[草稿]\n", encoding="utf-8")
    assert not apply_tune(Tune("选择弹窗等待秒", 25, 20, "缺键"), ini2)
