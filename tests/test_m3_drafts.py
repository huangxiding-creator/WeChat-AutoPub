"""M3 单元测试：日期过滤 / 安全守卫 / 间隔配置。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser.safety import SafetyViolationError, assert_button_safe, assert_url_safe  # noqa: E402


# ---------- 安全守卫（红线核心）----------

def test_delete_url_blocked():
    with pytest.raises(SafetyViolationError):
        assert_url_safe("https://mp.weixin.qq.com/cgi-bin/appmsg?action=del&appid=1")


def test_edit_url_blocked():
    with pytest.raises(SafetyViolationError):
        assert_url_safe("https://mp.weixin.qq.com/cgi-bin/appmsg_edit?action=edit")


def test_batch_delete_blocked():
    with pytest.raises(SafetyViolationError):
        assert_url_safe("https://mp.weixin.qq.com/cgi-bin/batchdel?token=1")


def test_normal_publish_urls_pass():
    assert_url_safe("https://mp.weixin.qq.com/cgi-bin/home?t=home&token=123")
    assert_url_safe("https://mp.weixin.qq.com/cgi-bin/freepublish?token=123")
    assert_url_safe("https://mp.weixin.qq.com/cgi-bin/appmsgpublish?sub=list")


def test_delete_button_blocked():
    with pytest.raises(SafetyViolationError):
        assert_button_safe("删除")


def test_edit_button_blocked():
    with pytest.raises(SafetyViolationError):
        assert_button_safe("编辑")


def test_publish_button_allowed():
    assert_button_safe("发表")
    assert_button_safe("群发")


# ---------- 草稿日期解析与过滤（独立可测函数）----------

def _filter_recent(cards, days):
    """复刻 DraftPublisher._filter_recent 的纯逻辑做单测。"""
    from src.browser.drafts import DraftCard, _parse_date
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for c in cards:
        dt = _parse_date(c.time_text)
        if dt is None or dt >= cutoff.replace(hour=0, minute=0, second=0):
            recent.append(c)
    return recent


def test_parse_date_formats():
    from src.browser.drafts import _parse_date
    assert _parse_date("2026-08-27 10:00") == datetime(2026, 8, 27)
    assert _parse_date("更新于 2026/08/27") is None or _parse_date("更新于 2026-08-27") == datetime(2026, 8, 27)
    assert _parse_date("无日期文本") is None


def test_filter_recent_keeps_new_drops_old():
    from src.browser.drafts import DraftCard
    today = datetime.now().strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    cards = [
        DraftCard(title="新草稿", time_text=today, index=0),
        DraftCard(title="旧草稿", time_text=old, index=1),
        DraftCard(title="无日期", time_text="", index=2),   # 无日期保留（宁可多看一眼）
    ]
    got = _filter_recent(cards, days=2)
    titles = [c.title for c in got]
    assert "新草稿" in titles
    assert "旧草稿" not in titles
    assert "无日期" in titles


def test_filter_recent_custom_week():
    from src.browser.drafts import DraftCard
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cards = [DraftCard(title="七天前", time_text=week_ago, index=0)]
    assert len(_filter_recent(cards, days=2)) == 0     # 默认2天：不含
    assert len(_filter_recent(cards, days=10)) == 1    # 自定义10天：包含


def test_picpost_chash_namespace_split():
    """贴图与源文章同名——chash 必须分命名空间，否则文章发布记录误杀同名贴图。"""
    from src.browser.drafts import DraftCard
    art = DraftCard(title="EPC指南", time_text="更新于 08月11日", index=1)
    pic = DraftCard(title="EPC指南", time_text="更新于 08月11日", index=1,
                    is_picpost=True)
    assert art.chash != pic.chash
    assert art.is_picpost is False and pic.is_picpost is True


def test_looks_like_picpost_heuristic():
    from src.browser.drafts import DraftCard, DraftPublisher
    f = DraftPublisher._looks_like_picpost
    assert f(DraftCard(title="x", time_text="更新于 15:06", index=1))
    assert not f(DraftCard(title="x", time_text="更新于 08月11日", index=1))
    assert not f(DraftCard(title="x", time_text="更新于 昨天 20:32", index=1))
    assert not f(DraftCard(title="x", time_text="今天 09:00", index=1))
