"""GUI 入口：python main.py（或打包后双击 WeChatAutoPub.exe）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.gui.main_window import MainWindow  # noqa: E402
from src.logger import setup_logging        # noqa: E402


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
