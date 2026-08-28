"""极简主窗口：三个按钮 + 实时日志。"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, load_config
from ..scheduler import task_scheduler
from .worker import RunWorker


class _QtLogHandler(logging.Handler):
    """把日志转发到 GUI 文本框（信号安全）。"""

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:  # noqa: BLE001 — GUI 日志失败静默
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._cfg: AppConfig = load_config()
        self._worker: RunWorker | None = None
        self._build_ui()
        self._install_log_bridge()

    # —— UI ——

    def _build_ui(self) -> None:
        self.setWindowTitle("公众号草稿与贴图 · 全自动发布")
        self.resize(760, 520)

        status = QLabel(f"就绪 · 今日待发草稿最近 {self._cfg.草稿.发布最近天数} 天 · "
                        f"贴图翻 {self._cfg.贴图.翻页数} 页 · 间隔 "
                        f"{self._cfg.草稿.每篇间隔最小秒}~{self._cfg.草稿.每篇间隔最大秒}s")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_run = QPushButton("🚀 立即运行")
        self.btn_run.clicked.connect(self._on_run)
        self.btn_schedule = QPushButton("⏰ 启用每日9点")
        self.btn_schedule.clicked.connect(self._on_schedule)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)

        self.chk_notify = QCheckBox("企业微信通知")
        self.chk_notify.setChecked(self._cfg.通知.通知开关)
        self.chk_notify.toggled.connect(self._on_notify_toggled)

        btn_row = QHBoxLayout()
        for b in (self.btn_run, self.btn_schedule, self.btn_stop):
            btn_row.addWidget(b)
        opt_row = QHBoxLayout()
        opt_row.addWidget(self.chk_notify)
        opt_row.addStretch()

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)

        layout = QVBoxLayout()
        layout.addWidget(status)
        layout.addLayout(btn_row)
        layout.addLayout(opt_row)
        layout.addWidget(self.log_view)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _install_log_bridge(self) -> None:
        handler = _QtLogHandler(self._append_log)
        logging.getLogger().addHandler(handler)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    # —— 事件 ——

    def _on_run(self) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "运行中", "已在运行，请先停止")
            return
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._append_log(f"=== 开始运行 {datetime.now():%H:%M:%S} ===")
        self._worker = RunWorker(self._cfg, self)
        self._worker.finished_with_summary.connect(self._on_done)
        self._worker.start()

    def _on_done(self, summary: str) -> None:
        self._append_log(summary)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _on_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append_log("停止信号已发出（等待当前步骤安全退出）")

    def _on_schedule(self) -> None:
        import sys
        from pathlib import Path
        if getattr(sys, "frozen", False):
            command = sys.executable
            args = "--mode auto"
            workdir = str(Path(sys.executable).parent)
        else:
            command = sys.executable
            args = str(Path(__file__).resolve().parents[2] / "run.py") + " --mode auto"
            workdir = str(Path(__file__).resolve().parents[2])
        ok, msg = task_scheduler.install_daily_task(
            command=command, arguments=args, workdir=workdir,
            run_time=self._cfg.定时.运行时间, catch_up=self._cfg.定时.错过补跑,
        )
        self._append_log(("✅ " if ok else "❌ ") + msg)

    def _on_notify_toggled(self, checked: bool) -> None:
        from dataclasses import replace
        self._cfg = replace(self._cfg, 通知=replace(self._cfg.通知, 通知开关=checked))
