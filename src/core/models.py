"""不可变数据模型。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..constants import CONTENT_TYPE_DRAFT, CONTENT_TYPE_PICPOST


@dataclass(frozen=True)
class AccountInfo:
    """一个公众号账号（按登录顺序编号）。"""
    index: int
    nickname: str

    @property
    def profile_name(self) -> str:
        """独立浏览器 profile 目录名（多账号 cookie 永不串号）。"""
        safe = "".join(c for c in self.nickname if c.isalnum() or c in "-_") or "unknown"
        return f"acct{self.index:02d}_{safe}"


@dataclass(frozen=True)
class ContentItem:
    """待发布内容（草稿文章或贴图）。"""
    ctype: str                      # CONTENT_TYPE_DRAFT / CONTENT_TYPE_PICPOST
    title: str
    content_hash: str
    update_time: Optional[datetime] = None
    url: str = ""                   # 发表成功后的验证 URL
    detail: str = ""


@dataclass(frozen=True)
class PublishResult:
    """单篇发布结果。"""
    item: ContentItem
    ok: bool
    detail: str = ""
    evidence: str = ""              # 截图路径


@dataclass(frozen=True)
class AccountReport:
    """单账号战报。

    results 只收「真发布」结果（口径对齐 DB 台账与金标准）；
    贴图「触发落箱」结果放 triggers 单独统计——2026-08-31 复盘发现
    混在一起导致每张贴图双计（成功 30 vs 实发 20）。
    """
    account: AccountInfo
    results: tuple[PublishResult, ...]
    triggers: tuple[PublishResult, ...] = ()

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def trigger_count(self) -> int:
        """贴图触发成功数（生成草稿，非发布）。"""
        return sum(1 for r in self.triggers if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def draft_count(self) -> int:
        return sum(1 for r in self.results if r.item.ctype == CONTENT_TYPE_DRAFT)

    @property
    def picpost_count(self) -> int:
        return sum(1 for r in self.results if r.item.ctype == CONTENT_TYPE_PICPOST)


def build_report_markdown(reports: tuple[AccountReport, ...], run_date: str) -> str:
    """汇总战报 → 企微 markdown。"""
    lines = [f"📊 **WeChat-AutoPub 日报** {run_date}", ""]
    total_ok = total_fail = 0
    for rep in reports:
        total_ok += rep.ok_count
        total_fail += rep.fail_count
        trig = f" · 触发生成 {rep.trigger_count}" if rep.trigger_count else ""
        lines.append(
            f"- **{rep.account.nickname}**：✅{rep.ok_count} ❌{rep.fail_count}"
            f"（草稿 {rep.draft_count} · 贴图 {rep.picpost_count}{trig}）"
        )
        for r in rep.results:
            mark = "✅" if r.ok else "❌"
            lines.append(f"  - {mark} {r.item.title[:30]} {r.detail[:40]}")
    lines += ["", f"**合计**：成功 {total_ok} · 失败 {total_fail}"]
    return "\n".join(lines)
