# -*- coding: utf-8 -*-
"""两阶段发布顺序测试（2026-09-03 用户指令）。

背景：公众号后台发布草稿后会自动生成贴图，但中间有时间间隔——
账号内闭环（草稿→贴图→新贴图）一次跑不完刚生成的贴图。用户指令：
先依次发布 3 个公众号的草稿，再依次发布 3 个公众号的贴图，让生成
间隔被阶段切换自然吸收。
"""
from types import SimpleNamespace

import src.core.orchestrator as orch
from src.core.models import ContentItem, PublishResult
from src.constants import CONTENT_TYPE_DRAFT, CONTENT_TYPE_PICPOST

_PROFILES = [("acct07", "总包之声"), ("acct05", "工程行业大脑"),
             ("acct01", "总包说")]


def _res(title, ok=True, ctype=CONTENT_TYPE_DRAFT):
    return PublishResult(item=ContentItem(ctype=ctype, title=title,
                                          content_hash=title), ok=ok)


class _FakeSession:
    def __init__(self, cfg, profile):
        self.profile = profile

    def start(self):
        pass

    def stop(self):
        pass


def _patch_all(monkeypatch, pic_ok=True):
    calls = []

    class _Draft:
        def __init__(self, session, cfg, state, notifier, account_name="",
                     should_stop=None):
            pass

        def publish_article_drafts(self):
            calls.append(("article", None))
            return [_res("文A")]

        def publish_picpost_drafts(self):
            calls.append(("picdraft", None))
            return [_res("图P", ctype=CONTENT_TYPE_PICPOST)]

    class _Pic:
        def __init__(self, session, cfg, state, notifier, account_name="",
                     should_stop=None):
            pass

        def publish_picposts(self):
            calls.append(("trigger", None))
            return [_res("触发", ok=pic_ok)]

    monkeypatch.setattr(orch, "BrowserSession", _FakeSession)
    monkeypatch.setattr(orch, "DraftPublisher", _Draft)
    monkeypatch.setattr(orch, "PicPostPublisher", _Pic)
    monkeypatch.setattr(
        orch, "ensure_login",
        lambda session, timeout_minutes=0, on_action_needed=None,
        target_nickname="": SimpleNamespace(
            ok=True, nickname=dict(_PROFILES)[session.profile]))
    monkeypatch.setattr("src.browser.login.preflight_logins",
                        lambda *a, **kw: {p: True for p, _ in _PROFILES})
    monkeypatch.setattr(
        orch.Orchestrator, "_profile_plan",
        lambda self: [p for p, _ in _PROFILES])
    return calls


def _state():
    return SimpleNamespace(list_profiles=lambda: list(_PROFILES),
                       register_profile=lambda *a, **kw: None)


def test_two_phase_order_all_drafts_then_all_pics(monkeypatch):
    """阶段1 三号文章草稿依次发完，才进阶段2 三号贴图。"""
    calls = _patch_all(monkeypatch)
    rep = orch.Orchestrator(SimpleNamespace(
        账号=SimpleNamespace(登录等待扫码超时分钟=1)), _state(), None)
    reports = rep.run()
    kinds = [c[0] for c in calls]
    assert kinds == ["article"] * 3 + ["trigger", "picdraft"] * 3
    assert len(reports) == 3
    by_nick = {r.account.nickname: r for r in reports}
    zs = by_nick["总包之声"]
    assert zs.ok_count == 2                    # 文A + 图P
    assert zs.trigger_count == 1               # 触发单列
    assert zs.picpost_count == 1


def test_no_picdraft_round_when_trigger_fails(monkeypatch):
    """阶段2 触发全失败（0 条落箱）→ 不跑贴图草稿轮。"""
    calls = _patch_all(monkeypatch, pic_ok=False)
    rep = orch.Orchestrator(SimpleNamespace(
        账号=SimpleNamespace(登录等待扫码超时分钟=1)), _state(), None)
    reports = rep.run()
    kinds = [c[0] for c in calls]
    assert "picdraft" not in kinds             # 触发失败即无贴图轮
    assert all(r.picpost_count == 0 for r in reports)
