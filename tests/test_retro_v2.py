# -*- coding: utf-8 -*-
"""自复盘 V2 测试：登录/保活/值守观测埋点、trend 幂等去重、得分卡渲染、
旋钮2（贴图渲染宽限秒）、改进积压。"""
import io
from datetime import date
from pathlib import Path

from src.retro.metrics import DayMetrics, build_metrics
from src.retro.parse import read_events
from src.retro.report import render_report, update_backlog, write_outputs
from src.retro.rules import Finding, analyze
from src.retro.scores import score_window

_LOG = """2026-09-01 07:47:00,000 [INFO] src.browser.login: [预检] acct01 登录态失效，尝试一键「登录」恢复
2026-09-01 07:47:26,309 [INFO] src.browser.login: 一键「登录」未恢复会话，转扫码流程
2026-09-01 07:49:14,267 [INFO] src.browser.login: 扫码登录成功 nickname=总包说 token=168…
2026-09-01 07:57:55,114 [INFO] scan: [scan] 扫码成功 nickname=总包之声 → 已登记到 acct07
2026-09-01 08:15:14,412 [INFO] src.browser.login: cookie 有效，免扫码登录 nickname=总包说 token=19…
2026-09-01 08:15:35,723 [INFO] src.browser.login: cookie 有效，免扫码登录 nickname=工程行业大脑 token=34…
2026-09-01 08:20:00,000 [ERROR] src.browser.session: navigate 重试耗尽: https://mp.weixin.qq.com/
2026-09-01 09:00:03,331 [ERROR] run: 已有运行实例（PID 23872），本次退出
2026-09-01 09:01:00,000 [INFO] run: 触发来源=daily
2026-09-01 09:02:00,000 [INFO] run: 定时窗口 09:00-12:00：随机延时 1200 秒（预计 09:22 启动）
2026-09-01 09:03:00,000 [INFO] src.browser.drafts: [工程行业大脑] 开始发布贴图《莫高窟数字中心：如何应对1200万游客》
2026-09-01 09:06:00,000 [INFO] src.browser.drafts: 拟人间隔 35 秒（贴图专用 20~50 随机）
2026-09-01 09:06:30,000 [INFO] src.browser.drafts: ✅ 金标准通过：《莫高窟数字中心：如何应对1200万游客》已从草稿箱消失
2026-09-01 09:07:00,000 [WARNING] src.browser.drafts: 贴图渲染宽限超时（8s 未渲染出贴图卡片）
2026-09-01 09:08:00,000 [INFO] run: 保活巡检：2/3 登录有效，失效=['acct06']
2026-09-01 09:09:00,000 [INFO] run: 今日运行已完成（幂等防重），本触发安全退出
2026-09-01 09:10:00,000 [INFO] run: 定时任务自愈：补装 WeChatAutoPub_Daily_0900
2026-09-01 09:11:00,000 [INFO] run: 运行结束：账号 3 个，成功 20，失败 0
"""


def _metrics(tmp_path: Path) -> DayMetrics:
    p = tmp_path / "sample.log"
    p.write_text(_LOG, encoding="utf-8")
    return build_metrics(read_events(p), date(2026, 9, 1),
                         tmp_path / "no.db")


def test_login_observability_metrics(tmp_path):
    """免扫码/扫码/一键恢复失败/预检失效全部可数。"""
    m = _metrics(tmp_path)
    assert m.passwordless_ok == 2
    assert m.scan_ok == 2          # 预检扫码 + scan 工具扫码都算人工介入
    assert m.oneclick_failed == 1
    assert m.preflight_dead == 1
    assert m.login_timeout == 0


def test_unattended_observability_metrics(tmp_path):
    """错误分级/锁让路/触发来源/定时证据/幂等/自愈全部可数。"""
    m = _metrics(tmp_path)
    assert m.errors == 2 and m.lock_blocked == 1
    assert m.trigger_sources == {"daily"}
    assert m.window_delayed == 1 and m.run_ended == 1
    assert m.idempotent_exit == 1 and m.selfheal_fixed == 1
    assert m.keepalive_dead == 1


def test_flow_split_article_vs_pic(tmp_path):
    """贴图流程独立计时（旧版把贴图并进总流程无法分维）。"""
    m = _metrics(tmp_path)
    assert len(m.flow_pic) == 1 and m.flow_pic[0] == 210.0  # 09:03→09:06:30
    assert m.flow_article == []


def test_render_grace_timeout_counted(tmp_path):
    """贴图渲染宽限超时事件成为旋钮2的调参证据。"""
    m = _metrics(tmp_path)
    assert m.render_grace_timeouts == 1


def test_trend_csv_same_day_dedupe(tmp_path):
    """同日二次复盘（主运行+补跑双收官）只保留最后一行——幂等去重。"""
    m = DayMetrics(day=date(2026, 9, 1), gold_pass=5, db_published=5)
    _, csv = write_outputs(tmp_path, m, "r1", score_window([m]), [])
    m2 = DayMetrics(day=date(2026, 9, 1), gold_pass=20, db_published=20)
    write_outputs(tmp_path, m2, "r2", score_window([m2]), [])
    rows = csv.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2                       # 表头 + 唯一当日行
    assert rows[1].startswith("2026-09-01")
    assert ",20," in rows[1]                    # 保留最后一次（20 篇）


def test_trend_csv_migrates_old_header(tmp_path):
    """旧 12 列 trend.csv 自动迁移到新表头，历史行保留。"""
    csv = tmp_path / "trend.csv"
    io.open(csv, "w", encoding="utf-8", newline="").write(
        "date,accounts_done,db_published,db_failed,gold_pass,flow_mean_s,"
        "gap_article_mean_s,picker_seen,selector_drift,session_lost,"
        "empty_breaks,tuned\n"
        "2026-08-30,3,34,2,34,690.8,187.3,0,103,0,8,picker:25->20\n")
    m = DayMetrics(day=date(2026, 9, 1), gold_pass=1)
    write_outputs(tmp_path, m, "r", score_window([m]), [])
    lines = csv.read_text(encoding="utf-8").strip().splitlines()
    assert "score_total" in lines[0]
    assert len(lines) == 3                       # 新表头 + 迁移史行 + 今日行
    assert lines[1].startswith("2026-08-30")     # 历史不丢


def test_report_contains_scorecard():
    """复盘报告呈现四维得分卡与扣分证据（与100分差距可见）。"""
    m = DayMetrics(day=date(2026, 9, 1), gold_pass=20, db_published=20,
                   scan_ok=1)
    sc = score_window([m])
    text = render_report(date(2026, 9, 1), m, [], sc, [], [])
    assert "四维得分卡" in text and "保活登录" in text
    assert "差距" in text and "扫码" in text


def test_knob2_render_grace():
    """旋钮2：贴图渲染宽限秒（界 3~15 步 2），超时证据 +2、三日零超时 -2。"""
    m = DayMetrics(day=date(2026, 9, 1), render_grace_timeouts=2)
    _, tunes = analyze([m], picker_now=20, grace_now=8)
    g = [t for t in tunes if t.key == "贴图渲染宽限秒"]
    assert g and g[0].new == 10                  # 有超时 → 8+2

    clean = DayMetrics(day=date(2026, 9, 1))
    _, tunes2 = analyze([clean, clean, clean], picker_now=20, grace_now=8)
    g2 = [t for t in tunes2 if t.key == "贴图渲染宽限秒"]
    assert g2 and g2[0].new == 6                 # 三日零超时 → 8-2


def test_backlog_accumulates_and_expires():
    """改进积压：risk/warn 按日累积+账龄；当日未再现自动消项。"""
    f1 = [Finding("risk", "选择器漂移", "当日 2 条漂移告警", "…"),
          Finding("warn", "发布台账", "1 条残留", "…")]
    text = update_backlog("", f1, date(2026, 9, 1))
    assert "选择器漂移" in text and "2026-09-01" in text

    text3 = update_backlog(text, f1, date(2026, 9, 2))
    assert "| 2026-09-01 | 2 |" in text3          # 连续出现账龄增长

    text2 = update_backlog(text, [], date(2026, 9, 2))
    assert "选择器漂移" not in text2               # 未再现 → 自动消项
