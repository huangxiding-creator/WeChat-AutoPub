"""主编排器：多账号循环。

流程（2026-09-03 两阶段，用户指令）：阶段1 依次发布各号文章草稿 →
阶段2 依次发布各号贴图（存量+触发生成+新落箱）→ 收官按号合并战报。
阶段切换耗时天然吸收平台自动生成贴图的延迟，一次运行全收。

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
        """两阶段发布（2026-09-03 用户指令）：先三号草稿、再三号贴图。

        公众号后台发布草稿后会自动生成贴图，但中间有时间间隔——账号
        内闭环（草稿→贴图→新贴图）一次跑不完刚生成的贴图。改为全局
        两阶段：阶段切换的耗时天然吸收贴图生成延迟，一次运行全收。
        全程仍单号串行（任一时刻只有一个号的浏览器在操作）。
        """
        plan = self._profile_plan()
        # 启动预检（2026-09-01 用户指令）：先逐档案查登录态，失效的当场
        # 弹码等扫，扫完才进发布；仍失效的跳过该档案（不再断账号链）
        from ..browser.login import preflight_logins
        preflight = preflight_logins(
            self._cfg, self._state, plan, self._notifier,
            timeout_minutes=self._cfg.账号.登录等待扫码超时分钟,
        )
        ready: list[str] = []
        for profile in plan:
            if self._should_stop():
                logger.info("收到停止信号，收工")
                break
            if not preflight.get(profile):
                logger.warning("[%s] 预检失效且未扫码，本次跳过", profile)
                continue
            ready.append(profile)
        nicknames: dict[str, str] = {}     # profile → 实时昵称（两阶段共用）

        # —— 阶段1：依次发布各号文章草稿 ——
        drafts_map: dict[str, list[PublishResult]] = {}
        handled: set[str] = set()          # 阶段内实时昵称判重
        for idx, profile in enumerate(ready, start=1):
            if self._should_stop():
                break
            session = self._open_logged_session(profile, idx, handled, nicknames)
            if session is None:
                continue
            nickname = nicknames[profile]
            drafts_map[profile] = self._draft_phase(session, nickname)
            logger.info("[%s] %s 草稿阶段完成（文章 %d 篇）", profile,
                        nickname, sum(1 for r in drafts_map[profile] if r.ok))
            session.stop()

        # —— 阶段2：依次发布各号贴图（存量贴图 + 触发生成 + 新落箱） ——
        handled.clear()                    # 阶段间清空：判重只防同阶段重复档案
        triggers_map: dict[str, list[PublishResult]] = {}
        pic_results_map: dict[str, list[PublishResult]] = {}
        for idx, profile in enumerate(ready, start=1):
            if self._should_stop():
                break
            session = self._open_logged_session(profile, idx, handled, nicknames)
            if session is None:
                continue
            nickname = nicknames[profile]
            pics, picdrafts = self._picpost_phase(session, nickname)
            triggers_map[profile] = pics
            pic_results_map[profile] = picdrafts
            logger.info("[%s] %s 贴图阶段完成，保留登录态（不登出）",
                        profile, nickname)
            session.stop()

        # 收官合并战报（每号一条：阶段1 草稿 + 阶段2 贴图草稿/触发）
        reports: list[AccountReport] = []
        for idx, profile in enumerate(ready, start=1):
            if profile not in nicknames:
                continue
            reports.append(AccountReport(
                account=AccountInfo(index=idx, nickname=nicknames[profile]),
                results=tuple(drafts_map.get(profile, [])
                              + pic_results_map.get(profile, [])),
                triggers=tuple(triggers_map.get(profile, []))))
            self._push_account_report(reports[-1])

        self._push_daily_report(reports)
        return reports

    def _open_logged_session(self, profile: str, index: int,
                             handled: set[str],
                             nicknames: dict[str, str]) -> Optional[BrowserSession]:
        """起浏览器并登录，成功返回会话（调用方用完 stop），失败 None。

        阶段2 复用阶段1 已知昵称作错号守卫（target_nickname）。
        """
        session = BrowserSession(self._cfg, profile)
        try:
            session.start()
        except RuntimeError as exc:
            logger.warning("[%s] 浏览器会话启动失败，跳过该账号: %s", profile, exc)
            return None

        def _on_action(action: str, detail: str) -> None:
            if self._notifier:
                self._notifier.send_action_needed(action, detail)

        try:
            login = ensure_login(
                session,
                timeout_minutes=self._cfg.账号.登录等待扫码超时分钟,
                on_action_needed=_on_action,
                target_nickname=nicknames.get(profile, ""),
            )
            if not login.ok:
                logger.warning("[%s] 登录失败: %s", profile, login.detail)
                session.stop()
                return None
            nickname = login.nickname or f"账号{index}"
            self._state.register_profile(profile, nickname)
            if nickname in handled:
                # 实时判重（历史存档昵称可能过期：浏览器里换号登录后，
                # 旧存档昵称会误导静态去重跳过本该处理的账号）
                logger.info("[%s] %s 已由其它档案处理过，跳过", profile, nickname)
                session.stop()
                return None
            handled.add(nickname)
            nicknames[profile] = nickname
            return session
        except Exception:                  # noqa: BLE001 — 会话级异常不拖垮整轮
            session.stop()
            return None

    def _draft_phase(self, session: BrowserSession,
                     nickname: str) -> list[PublishResult]:
        """阶段1：文章草稿（贴图留给阶段2）。"""
        return DraftPublisher(
            session, self._cfg, self._state, self._notifier,
            account_name=nickname, should_stop=self._should_stop,
        ).publish_article_drafts()

    def _picpost_phase(self, session: BrowserSession,
                       nickname: str) -> tuple[list, list]:
        """阶段2：存量贴图/触发生成 → 新落箱贴图再发一轮。

        返回 (触发结果, 贴图草稿发布结果)——触发≠发布，口径分列。
        """
        pics = PicPostPublisher(
            session, self._cfg, self._state, self._notifier,
            account_name=nickname, should_stop=self._should_stop,
        ).publish_picposts()
        picdrafts: list = []
        if any(r.ok for r in pics):
            # 贴图草稿由「去查看」触发生成，落在草稿箱贴图 tab——
            # 只发贴图 tab（新落箱贴图），间隔由 _polite_wait 按内容
            # 类型取 config.ini [草稿] 对应配置（2026-09-02 起 5~10 秒）
            picdrafts = DraftPublisher(
                session, self._cfg, self._state, self._notifier,
                account_name=nickname, should_stop=self._should_stop,
            ).publish_picpost_drafts()
        return pics, picdrafts

    def _run_account_pipeline(self, session: BrowserSession, profile: str,
                              nickname: str, index: int) -> AccountReport:
        """账号内闭环：草稿发布 → 贴图触发 → 贴图草稿再发布一轮。"""
        account = AccountInfo(index=index, nickname=nickname)

        drafts = DraftPublisher(
            session, self._cfg, self._state, self._notifier,
            account_name=nickname, should_stop=self._should_stop,
        ).publish_recent_drafts()

        picposts = PicPostPublisher(
            session, self._cfg, self._state, self._notifier,
            account_name=nickname, should_stop=self._should_stop,
        ).publish_picposts()

        # 贴图草稿由上面的「去查看」触发生成，落在草稿箱里——
        # 用已实战验证的草稿发布链再发布一轮（贴图即今日新草稿）
        picposts_drafts: list = []
        if any(r.ok for r in picposts):
            # 贴图/文章间隔均由 _polite_wait 按内容类型取 config.ini
            # [草稿] 对应配置（2026-09-02 起贴图 5~10 秒，历史演变见
            # config.py DraftConfig 注释）；此处不再传 gap_range 覆盖
            picposts_drafts = DraftPublisher(
                session, self._cfg, self._state, self._notifier,
                account_name=nickname, should_stop=self._should_stop,
            ).publish_recent_drafts()

        # 永不登出：每账号独立浏览器实例，切换账号=切换浏览器，
        # 登出只会销毁登录态、换来明天再扫码（用户核心诉求=少扫码）
        logger.info("[%s] %s 草稿+贴图全部完成，保留登录态（不登出）",
                    profile, nickname)
        # 触发结果只进 triggers（触发生成≠发布），成功数口径对齐
        # DB 台账与金标准（2026-08-31 复盘：混计致每张贴图双计）
        return AccountReport(account=account,
                             results=tuple(drafts + picposts_drafts),
                             triggers=tuple(picposts))

    def run_for_profile(self, profile: str, target_nickname: str = "",
                        index: int = 99) -> Optional[AccountReport]:
        """旁路入口：单档案完整管线（注册新号/补跑，不动 run.py 主链）。

        target_nickname 非空时启用错号守卫：扫码后登进来的若不是目标号
        （平台会自动沿用上次账号），提示用户点右上角头像菜单切换账号；
        轮询期间自动点掉「选择账号登录」弹窗里的目标项。
        """
        session = BrowserSession(self._cfg, profile)
        try:
            session.start()
        except RuntimeError as exc:
            logger.warning("[%s] 浏览器会话启动失败: %s", profile, exc)
            return None

        def _on_action(action: str, detail: str) -> None:
            if self._notifier:
                self._notifier.send_action_needed(action, detail)
            logger.info("[%s] 需要人工: %s — %s", profile, action, detail)

        try:
            login = ensure_login(
                session,
                timeout_minutes=self._cfg.账号.登录等待扫码超时分钟,
                on_action_needed=_on_action,
                target_nickname=target_nickname,
            )
            if not login.ok:
                logger.warning("[%s] 登录失败: %s", profile, login.detail)
                return None
            nickname = login.nickname or profile

            if target_nickname and target_nickname not in nickname:
                logger.warning(
                    "[%s] 登进来的是「%s」不是目标「%s」——请在浏览器右上角"
                    "头像菜单切换账号，弹出的选择框会自动点（等 5 分钟）",
                    profile, nickname, target_nickname)
                import time as _time
                from ..browser import nav
                from ..browser.login import extract_nickname
                deadline = _time.time() + 300
                while _time.time() < deadline:
                    nav.dismiss_account_picker(session.tab, target_nickname,
                                               timeout=4)
                    nickname = extract_nickname(session) or nickname
                    if target_nickname in nickname:
                        logger.info("[%s] 已切换到目标账号 %s",
                                    profile, nickname)
                        break
                    _time.sleep(5)

            self._state.register_profile(profile, nickname)
            return self._run_account_pipeline(session, profile, nickname,
                                              index)
        finally:
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
        for profile, _nickname in registered:
            # 不做静态昵称去重——存档昵称可能过期（浏览器里换号后失效），
            # 改为登录后按实时昵称判重（见 _process_account 的 handled）
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
        trig = (f"\n🧩 触发生成 {report.trigger_count}（落箱后已由贴图轮发布）"
                if report.trigger_count else "")
        lines = [
            f"📋 **账号战报 · {report.account.nickname}**",
            f"✅ 成功 {report.ok_count} · ❌ 失败 {report.fail_count}"
            + (f" · ♻️ 自愈 {report.recovered_count}"
               if report.recovered_count else "") + trig,
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
