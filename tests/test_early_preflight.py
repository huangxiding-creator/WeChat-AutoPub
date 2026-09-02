# -*- coding: utf-8 -*-
"""早期预检测试（2026-09-02 用户指令）：持锁后、睡窗口前先弹码。"""
import logging

import run as run_mod


class _FakeState:
    def list_profiles(self):
        return [("acct01", "总包说"), ("acct05", "工程行业大脑")]


def _patch_env(monkeypatch, results, explode=False):
    calls = {}

    def fake_preflight(cfg, state, profiles, notifier, timeout_minutes,
                       wait_scan=True):
        calls.update(profiles=list(profiles), timeout=timeout_minutes,
                     wait_scan=wait_scan)
        if explode:
            raise RuntimeError("浏览器起不来")
        return results

    monkeypatch.setattr("src.core.state.StateDB", _FakeState)
    monkeypatch.setattr("src.browser.login.preflight_logins", fake_preflight)
    return calls


def test_early_preflight_all_alive(monkeypatch, caplog):
    calls = _patch_env(monkeypatch, {"acct01": "总包说", "acct05": "工程行业大脑"})
    with caplog.at_level(logging.INFO):
        run_mod._early_preflight()
    assert calls["profiles"] == ["acct01", "acct05"]
    assert calls["wait_scan"] is True            # 必须弹码等扫（用户尽早处理）
    assert calls["timeout"] == 30                # 来自配置 登录等待扫码超时分钟
    assert any("早期预检：2/2 登录就绪，失效=无" in r.message for r in caplog.records)


def test_early_preflight_reports_dead(monkeypatch, caplog):
    _patch_env(monkeypatch, {"acct01": "总包说", "acct05": ""})
    with caplog.at_level(logging.INFO):
        run_mod._early_preflight()               # 失效档案等扫超时后如实上报
    assert any("早期预检：1/2 登录就绪，失效=acct05" in r.message
               for r in caplog.records)


def test_early_preflight_swallows_errors(monkeypatch, caplog):
    _patch_env(monkeypatch, {}, explode=True)
    with caplog.at_level(logging.WARNING):
        run_mod._early_preflight()               # 异常不外抛：二次预检兜底
    assert any("早期预检异常" in r.message for r in caplog.records)


def test_early_preflight_no_profiles(monkeypatch):
    monkeypatch.setattr("src.core.state.StateDB",
                        type("E", (), {"list_profiles": lambda s: []}))
    called = []
    monkeypatch.setattr("src.browser.login.preflight_logins",
                        lambda *a, **k: called.append(1))
    run_mod._early_preflight()
    assert not called                            # 无档案不开浏览器
