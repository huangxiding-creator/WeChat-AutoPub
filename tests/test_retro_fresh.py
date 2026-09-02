# -*- coding: utf-8 -*-
"""复盘链根治测试（2026-09-02 用户指令：确保后续都用新代码）。

两日混版实证（09-01 RetroConfig.告警阈值分 / 09-02 DraftConfig.贴图
间隔最小秒）：运行中的进程缓存旧 config + 收官链惰性导入新 engine →
AttributeError → 复盘产物缺失。根治=收官复盘改子进程调用（全新解释器
全量加载磁盘最新代码，物理隔离版本）。
"""
import logging
from datetime import date
from pathlib import Path

import run as run_mod
from src.retro.engine import _suppress_tunes
from src.retro.rules import Tune


def _tunes():
    return [Tune(key="贴图渲染宽限秒", old=12, new=14, reason="渲染超时19次"),
            Tune(key="选择弹窗等待秒", old=12, new=12, reason="维持")]


def test_suppress_tunes_when_already_tuned_today(tmp_path):
    """当日 trend 行 tuned 非空 → 重复复盘决策全转维持（不再写 config）。"""
    (tmp_path / "trend.csv").write_text(
        "date,accounts,db_pub,db_fail,gold,flow,gap,picker,drift,session,"
        "empty,total,login,eff,stab,unatt,pwd,scan,tuned\n"
        "2026-09-02,3,21,0,21,197.5,123.4,0,0,0,4,88,100,53,98,100,3,0,"
        "贴图渲染宽限秒:10->12\n",
        encoding="utf-8")
    out = _suppress_tunes(_tunes(), tmp_path, date(2026, 9, 2))
    assert all(not t.changed for t in out)      # 全维持，幂等
    assert out[0].new == out[0].old == 12


def test_suppress_tunes_passthrough_when_not_tuned(tmp_path):
    """当日无行/未调参/文件缺失 → 决策原样放行。"""
    d = date(2026, 9, 2)
    assert all(t.changed == o.changed for t, o in
               zip(_suppress_tunes(_tunes(), tmp_path, d), _tunes()))
    (tmp_path / "trend.csv").write_text(
        "date,accounts,db_pub,db_fail,gold,flow,gap,picker,drift,session,"
        "empty,total,login,eff,stab,unatt,pwd,scan,tuned\n"
        "2026-09-02,3,21,0,21,197.5,123.4,0,0,0,4,88,100,53,98,100,3,0,\n",
        encoding="utf-8")
    out = _suppress_tunes(_tunes(), tmp_path, d)
    assert out[0].changed and out[0].new == 14   # tuned 空 → 正常调参
    other = _suppress_tunes(_tunes(), tmp_path, date(2026, 9, 1))
    assert other[0].new == 14                    # 非当日行不影响


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _spawn(monkeypatch, proc=None, explode=None):
    calls = {}

    def fake_run(cmd, **kw):
        calls.update(cmd=cmd, kw=kw)
        if explode:
            raise explode
        return proc or _Proc(stdout="金标准 21、总分 88（A 级）")

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def test_run_retro_safe_spawns_subprocess(monkeypatch, caplog):
    """收官复盘走子进程跑 run.py --retro（磁盘新代码物理隔离）。"""
    calls = _spawn(monkeypatch)
    with caplog.at_level(logging.INFO):
        run_mod._run_retro_safe()
    assert "--retro" in calls["cmd"]
    assert calls["cmd"][0].endswith("python") or "python" in calls["cmd"][0].lower()
    assert calls["kw"]["cwd"]
    assert "金标准 21" in caplog.text          # 子进程 summary 转发进主日志


def test_run_retro_safe_nonzero_exit_warns(monkeypatch, caplog):
    """子进程非零退出只告警，不外抛（不影响发布退出码）。"""
    _spawn(monkeypatch, proc=_Proc(returncode=1, stderr="Traceback …"))
    with caplog.at_level(logging.WARNING):
        run_mod._run_retro_safe()
    assert "退出码 1" in caplog.text


def test_run_retro_safe_crash_swallowed(monkeypatch, caplog):
    """子进程超时/起不来也只告警。"""
    _spawn(monkeypatch, explode=TimeoutError("subprocess timeout"))
    with caplog.at_level(logging.WARNING):
        run_mod._run_retro_safe()
    assert "自复盘异常" in caplog.text
