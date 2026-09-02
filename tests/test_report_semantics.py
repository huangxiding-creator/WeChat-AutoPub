# -*- coding: utf-8 -*-
"""复盘迭代落地项测试：战报口径（触发≠发布）/ 企微超长截断。

2026-08-31 自复盘产出：成功 30 vs DB 实发 20 的口径差根因=贴图触发
结果被并入发布计数（每张贴图双计）；同日修复企微 markdown 超 4096
字节被拒收（errcode 40058）。
"""
from src.constants import CONTENT_TYPE_PICPOST
from src.core.models import (AccountInfo, AccountReport, ContentItem,
                             PublishResult, build_report_markdown)
from src.notify.wecom import truncate_markdown


def _res(title: str, ok: bool = True) -> PublishResult:
    return PublishResult(item=ContentItem(ctype=CONTENT_TYPE_PICPOST,
                                          title=title, content_hash="h"),
                         ok=ok)


def test_triggers_do_not_inflate_ok_count():
    """触发结果单独统计：成功/失败数只算真发布。"""
    report = AccountReport(
        account=AccountInfo(index=1, nickname="总包之声"),
        results=(_res("发布1"), _res("发布2", ok=False)),
        triggers=(_res("触发1"), _res("触发2"), _res("触发3")),
    )
    assert report.ok_count == 1 and report.fail_count == 1
    assert report.trigger_count == 3


def test_default_triggers_empty_backcompat():
    """不传 triggers 的旧构造路径不受影响。"""
    report = AccountReport(account=AccountInfo(1, "x"), results=(_res("a"),))
    assert report.trigger_count == 0 and report.ok_count == 1


def test_recovered_retry_not_counted_as_fail():
    """自愈重试（同名先败后成）不计失败、单列自愈数（09-02 实证：
    弹窗 25s 未出现判失败 → 下轮扫描同名重试成功，内容零损失）。"""
    report = AccountReport(
        account=AccountInfo(index=1, nickname="总包之声"),
        results=(_res("EPC超概", ok=False), _res("EPC超概"),
                 _res("排污许可", ok=False)),
    )
    assert report.recovered_count == 1
    assert report.fail_count == 1        # 只剩真失败（排污许可）
    assert report.ok_count == 1


def test_report_markdown_shows_trigger_line():
    report = AccountReport(account=AccountInfo(1, "总包之声"),
                           results=(_res("发布1"),),
                           triggers=(_res("触发1"),))
    md = build_report_markdown((report,), "2026-08-31")
    assert "触发生成 1" in md


def test_truncate_markdown_short_passthrough():
    assert truncate_markdown("短消息") == "短消息"


def test_truncate_markdown_long_cut_on_char_boundary():
    md = "战报：" + "字" * 5000
    out = truncate_markdown(md)
    assert len(out.encode("utf-8")) <= 3800
    assert "已截断" in out
    out.encode("utf-8").decode("utf-8")      # 不抛异常=没切坏多字节字符
