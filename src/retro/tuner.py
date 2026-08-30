# -*- coding: utf-8 -*-
"""有界自调参执行器：Tune 决策 → 安全写回 config.ini。"""
from __future__ import annotations

import io
import re
from pathlib import Path

from ..config import ConfigError, load_config
from .rules import Tune


def apply_tune(tune: Tune, ini_path: Path) -> bool:
    """把一次调参写回 config.ini；带边界复核，任何异常都放弃不改。

    返回 True=已写入；False=无变化/放弃（决策未变、键不存在、越界）。
    """
    if not tune.changed:
        return False
    if not ini_path.exists():
        return False
    key_re = re.compile(rf"^(\s*{re.escape(tune.key)}\s*=\s*)\d+\s*$", re.M)
    text = io.open(ini_path, encoding="utf-8").read()
    if not key_re.search(text):
        return False                    # 键不存在→用默认值，不动文件
    new_text = key_re.sub(rf"\g<1>{tune.new}", text, count=1)

    # 护栏 1：写前文件备份（原子性：先备份后写，失败可手工回滚）
    backup = ini_path.with_suffix(".ini.bak")
    io.open(backup, "w", encoding="utf-8", newline="").write(text)

    io.open(ini_path, "w", encoding="utf-8", newline="").write(new_text)

    # 护栏 2：重载校验（含 _bounds 硬边界），失败立即回滚
    try:
        cfg = load_config(ini_path)
        _ = cfg.草稿.选择弹窗等待秒
    except (ConfigError, Exception):  # noqa: BLE001
        io.open(ini_path, "w", encoding="utf-8", newline="").write(text)
        return False
    return True
