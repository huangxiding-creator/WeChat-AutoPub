"""主编排器：多账号循环。

流程：登录账号N → 发布草稿(最近N天) → 发布贴图(翻N页) → 企微战报 → 下一账号。

多账号 cookie 档案：
  每账号独立 profile（acct01/acct02…）= 独立浏览器实例（独立端口），
  登记进状态库，按昵称去重后逐个处理；切换账号=切换浏览器实例。

永不登出（架构决策）：登出会销毁登录态、导致次日重新扫码；
每账号浏览器为独立常驻进程，不登出即可无缝共存，登录态跨天存活。
[账号] 开启新账号扫码窗口 = 是 时，末尾追加一个新档案等扫码。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional

from ..browser.drafts import DraftPublisher
from ..browser.login import ensure_login
from ..browser.picposts import PicPostPublisher
from ..browser.session import BrowserSession
from ..config import AppConfig
from ..constants import CONTENT_TYPE_DRAFT
from ..core.models import AccountInfo, AccountReport, build_report_markdown
from ..core.state import StateDB
from ..notify.wecom import WecomNotifier

logger = logging.getLogger(__name__)


class Orchestrator:
    """一次完整运行（多账号循环）。"""

    def __init__(
        self,
        config: AppConfig,
        state: StateDB,
        notifier: Optional[WecomNotifier],
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._cfg = config
        self._state = state
        self._notifier = notifier
        self._should_stop = should_stop

    # —— 主入口 ——

    def run(self) -> list[AccountReport]:
        reports: list[AccountReport] = []
        for idx, profile in enumerate(self._profile_plan(), start=1):
            if self._should_stop():
                logger.info("收到停止信号，收工")
                break
            report = self._process_account(profile, idx)
            if report is None:
                # 登录失败/超时：账号链自然结束（无人扫码即收工）
                logger.info("账号链结束（%s 未登录）", profile)
                break
            reports.append(report)
            self._push_account_report(report)

        self._push_daily_report(reports)
        return reports

    # —— 账号处理（会话内闭环：登录→发布→登出→退出）——

    def _process_account(self, profile: str, index: int) -> Optional[AccountReport]:
        session = BrowserSession(self._cfg, profile)
        try:
            session.start()
        except RuntimeError as exc:
            logger.warning("[%s] 浏览器会话启动失败，跳过该账号: %s", profile, exc)
            return None
        try:
            def _on_action(action: str, detail: str) -> None:
                if self._notifier:
                    self._notifier.send_action_needed(action, detail)

            login = ensure_login(
                session,
                timeout_minutes=self._cfg.账号.登录等待扫码超时分钟,
                on_action_needed=_on_action,
            )
            if not login.ok:
                logger.warning("[%s] 登录失败: %s", profile, login.detail)
                return None

            nickname = login.nickname or f"账号{index}"
            account = AccountInfo(index=index, nickname=nickname)
            self._state.register_profile(profile, nickname)

            drafts = DraftPublisher(
                session, self._cfg, self._state, self._notifier,
                account_name=nickname, should_stop=self._should_stop,
            ).publish_recent_drafts()

            picposts = PicPostPublisher(
                session, self._cfg, self._state, self._notifier,
                account_name=nickname, should_stop=self._should_stop,
            ).publish_picposts()

            # 永不登出：每账号独立浏览器实例，切换账号=切换浏览器，
            # 登出只会销毁登录态、换来明天再扫码（用户核心诉求=少扫码）
            logger.info("[%s] %s 草稿+贴图全部完成，保留登录态（不登出）",
                        profile, nickname)

            return AccountReport(account=account, results=tuple(drafts + picposts))
        finally:
            # 浏览器为独立常驻进程：不退出（登录态跨天存活），只断开本次接管
            session.stop()

    # —— 计划 ——

    def _profile_plan(self) -> list[str]:
        """运行计划：已登记账号（按昵称去重，cookie 复用）。

        同一公众号被多个档案登记过（历史重复登录）只处理一次，
        避免同一账号反复要求扫码。[开启新账号扫码窗口] 打开时
        额外留一个新档案窗口等用户扫码。
        """
        registered = self._state.list_profiles()      # [(profile, nickname)]
        deduped: list[str] = []
        seen: set[str] = set()
        for profile, nickname in registered:
            if nickname and nickname in seen:
                logger.info("跳过重复档案 %s（%s 已由其它档案处理）", profile, nickname)
                continue
            if nickname:
                seen.add(nickname)
            deduped.append(profile)
        if not deduped:
            return ["acct01"]
        if self._cfg.账号.开启新账号扫码窗口:
            nums = [int(p[4:]) for p, _ in registered if p.startswith("acct") and p[4:].isdigit()]
            deduped.append(f"acct{(max(nums) if nums else 0) + 1:02d}")
            logger.info("新账号扫码窗口已开启，末尾追加 %s", deduped[-1])
        return deduped

    # —— 通知 ——

    def _push_account_report(self, report: AccountReport) -> None:
        if not self._notifier:
            return
        lines = [
            f"📋 **账号战报 · {report.account.nickname}**",
            f"✅ 成功 {report.ok_count} · ❌ 失败 {report.fail_count}",
        ]
        for r in report.results:
            mark = "✅" if r.ok else "❌"
            kind = "草稿" if r.item.ctype == CONTENT_TYPE_DRAFT else "贴图"
            lines.append(f"- {mark} [{kind}] {r.item.title[:30]} {r.detail[:30]}")
        self._notifier.send_markdown("\n".join(lines))

    def _push_daily_report(self, reports: list[AccountReport]) -> None:
        if not self._notifier:
            return
        if not reports:
            self._notifier.send_text("⚠️ 本次运行没有任何账号完成（登录超时或被停止）")
            return
        md = build_report_markdown(tuple(reports), date.today().strftime("%Y-%m-%d"))
        self._notifier.send_markdown(md)
