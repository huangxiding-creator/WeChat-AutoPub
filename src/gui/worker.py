"""GUI 后台工作线程：跑 Orchestrator，支持停止。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ..config import AppConfig
from ..core.orchestrator import Orchestrator
from ..core.state import StateDB
from ..notify.wecom import WecomNotifier


class RunWorker(QThread):
    """一次完整运行（多账号循环），后台执行。"""

    finished_with_summary = Signal(str)

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # noqa: D102 — QThread 入口
        try:
            notifier = WecomNotifier(self._cfg.通知.企微Webhook, self._cfg.通知.通知开关)
        except ValueError:
            notifier = None
        state = StateDB()
        orchestrator = Orchestrator(
            self._cfg, state, notifier, should_stop=self._stop_event.is_set,
        )
        reports = []
        try:
            reports = orchestrator.run()
        except Exception as exc:  # noqa: BLE001 — GUI 不允许崩
            self.finished_with_summary.emit(f"❌ 运行异常: {exc}")
            return
        ok = sum(r.ok_count for r in reports)
        fail = sum(r.fail_count for r in reports)
        names = "、".join(r.account.nickname for r in reports) or "无"
        self.finished_with_summary.emit(
            f"运行结束：账号({names}) 成功 {ok} · 失败 {fail}"
        )
