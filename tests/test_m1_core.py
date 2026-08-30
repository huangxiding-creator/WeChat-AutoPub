"""M1 骨架单元测试：配置校验 / 状态库去重 / 内容指纹 / 模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig, ConfigError, load_config          # noqa: E402
from src.constants import ALLOWED_FREE_MODELS                       # noqa: E402
from src.core.models import AccountInfo, ContentItem, PublishResult  # noqa: E402
from src.core.state import StateDB, content_hash                    # noqa: E402


# ---------- 配置 ----------

def test_load_real_config():
    cfg = load_config()
    assert cfg.草稿.每篇间隔最小秒 == 120
    assert cfg.草稿.每篇间隔最大秒 == 180      # 2026-08-30 用户提速：2~3 分钟
    assert cfg.草稿.发布最近天数 == 3
    assert cfg.贴图.翻页数 == 5
    assert cfg.定时.运行时间 == "09:00"
    assert cfg.定时.最晚运行时间 == "12:00"
    assert cfg.定时.错过补跑 is True


def test_default_config_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.ini")
    assert isinstance(cfg, AppConfig)
    assert cfg.引擎.优先模式 == "自动"


def test_interval_min_greater_than_max_rejected(tmp_path):
    p = tmp_path / "bad.ini"
    p.write_text("[草稿]\n每篇间隔最小秒 = 400\n每篇间隔最大秒 = 300\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_paid_model_rejected(tmp_path):
    p = tmp_path / "bad.ini"
    p.write_text("[大模型]\n免费模型 = glm-4-plus\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_free_model_whitelist_no_paid_models():
    assert "glm-4-flashx" in ALLOWED_FREE_MODELS
    assert "glm-4-flash" in ALLOWED_FREE_MODELS
    for m in ALLOWED_FREE_MODELS:
        assert m.startswith("glm-4-flash")


def test_bad_schedule_time_rejected(tmp_path):
    p = tmp_path / "bad.ini"
    p.write_text("[定时]\n运行时间 = 25:99\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


# ---------- 状态库 ----------

@pytest.fixture()
def db(tmp_path):
    return StateDB(tmp_path / "state.db")


def test_dedup_across_sessions(db):
    h = content_hash("我的文章标题")
    db.upsert(account="测试号", ctype="draft", chash=h, title="我的文章标题", status="pending")
    assert not db.is_published("测试号", "draft", h)
    db.mark_published(account="测试号", ctype="draft", chash=h, title="我的文章标题")
    assert db.is_published("测试号", "draft", h)
    # 新会话（新实例）依然去重
    db2 = StateDB(db._path)
    assert db2.is_published("测试号", "draft", h)


def test_published_never_regresses(db):
    h = content_hash("x")
    db.upsert(account="a", ctype="draft", chash=h, title="x", status="published")
    db.upsert(account="a", ctype="draft", chash=h, title="x", status="failed")
    assert db.is_published("a", "draft", h)


def test_daily_circuit_breaker_count(db):
    for i in range(3):
        h = content_hash(f"t{i}")
        db.upsert(account="a", ctype="draft", chash=h, title=f"t{i}", status="pending")
        db.mark_published(account="a", ctype="draft", chash=h, title=f"t{i}")
    assert db.today_published_count("a") == 3
    assert db.today_published_count("b") == 0


def test_different_account_same_content_not_deduped(db):
    h = content_hash("共同标题")
    db.upsert(account="a", ctype="draft", chash=h, title="共同标题", status="published")
    db.mark_published(account="a", ctype="draft", chash=h, title="共同标题")
    assert db.is_published("a", "draft", h)
    assert not db.is_published("b", "draft", h)


# ---------- 模型 ----------

def test_account_profile_name_isolated():
    a1 = AccountInfo(index=1, nickname="甲号")
    a2 = AccountInfo(index=2, nickname="乙号")
    assert a1.profile_name != a2.profile_name
    assert a1.profile_name.startswith("acct01_")


def test_report_counts():
    item_ok = ContentItem(ctype="draft", title="t", content_hash="h1")
    item_fail = ContentItem(ctype="picpost", title="p", content_hash="h2")
    results = (
        PublishResult(item=item_ok, ok=True),
        PublishResult(item=item_fail, ok=False, detail="风控拦截"),
    )
    from src.core.models import AccountReport
    rep = AccountReport(account=AccountInfo(index=1, nickname="n"), results=results)
    assert rep.ok_count == 1
    assert rep.fail_count == 1
    assert rep.draft_count == 1
    assert rep.picpost_count == 1
