"""M6 单元测试：LLM JSON 提取 / 模型白名单 / profile 注册表。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.client import LLMError, ZhipuClient, _extract_json  # noqa: E402
from src.config import LLMConfig                                    # noqa: E402
from src.core.state import StateDB                                  # noqa: E402


# ---------- JSON 自愈 ----------

def test_extract_plain_json():
    assert _extract_json('{"ok": true}') == {"ok": True}


def test_extract_json_in_codeblock():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prose():
    assert _extract_json('好的，结果是 {"a": 2} 请查收') == {"a": 2}


def test_extract_json_array():
    assert _extract_json('```json\n[1, 2]\n```') == [1, 2]


def test_extract_json_garbage_raises():
    with pytest.raises(LLMError):
        _extract_json("完全不是JSON")


# ---------- 白名单防线（收费模型物理不可达）----------

def test_zhipu_client_rejects_paid_model():
    with pytest.raises(LLMError):
        ZhipuClient(LLMConfig(智谱Key="k.k", 免费模型=("glm-4-plus",)))


def test_zhipu_client_rejects_empty_key():
    with pytest.raises(LLMError):
        ZhipuClient(LLMConfig(智谱Key="", 免费模型=("glm-4-flash",)))


def test_zhipu_client_accepts_free_models():
    c = ZhipuClient(LLMConfig(智谱Key="id.secret", 免费模型=("glm-4-flashx", "glm-4-flash")))
    assert c is not None


# ---------- profile 注册表 ----------

def test_profile_registry_roundtrip(tmp_path):
    db = StateDB(tmp_path / "s.db")
    assert db.list_profiles() == []
    db.register_profile("acct01", "甲号")
    db.register_profile("acct02", "乙号")
    db.register_profile("acct01", "甲号")        # 重复登记=更新
    got = db.list_profiles()
    assert got == [("acct01", "甲号"), ("acct02", "乙号")]
