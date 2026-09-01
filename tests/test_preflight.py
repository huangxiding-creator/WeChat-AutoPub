# -*- coding: utf-8 -*-
"""启动预检测试：浏览器启动失败降级为跳过（不断链）。"""
from src.browser import login as login_mod


class _StartFailSession:
    """模拟浏览器启动即失败的档案。"""

    def __init__(self, cfg, profile, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("浏览器启动失败")


def test_preflight_browser_start_failure_skips(monkeypatch):
    """启动失败的档案返回空昵称（主循环据此跳过，不抛异常不断链）。"""
    monkeypatch.setattr(login_mod, "BrowserSession", _StartFailSession)
    results = login_mod.preflight_logins(None, None, ["acct01", "acct07"])
    assert results == {"acct01": "", "acct07": ""}


class _DeadSession:
    """模拟 cookie 已失效且一键恢复救不回的档案。"""

    def __init__(self, cfg, profile, *args, **kwargs):
        pass

    def start(self):
        return None

    def is_logged_in(self):
        return False

    def stop(self):
        pass


def test_preflight_keepalive_mode_never_waits(monkeypatch):
    """wait_scan=False（保活巡检）：失效只记录，不通知、不等待扫码。"""
    from src.browser import nav as nav_mod

    monkeypatch.setattr(login_mod, "BrowserSession", _DeadSession)
    monkeypatch.setattr(nav_mod, "js_click_visible_text", lambda *a, **k: False)
    notified: list[tuple] = []

    class _Notifier:
        def send_action_needed(self, *a, **k):
            notified.append(a)

    results = login_mod.preflight_logins(
        None, None, ["acct01"], notifier=_Notifier(),
        timeout_minutes=30, wait_scan=False)
    assert results == {"acct01": ""} and not notified   # 不弹码不告警
