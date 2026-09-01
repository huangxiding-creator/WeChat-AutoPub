# -*- coding: utf-8 -*-
"""有界自调参执行器：Tune 决策 → 安全写回 config.ini。

V2：双旋钮通用化——键名→硬边界映射（选择弹窗等待秒 / 贴图渲染宽限秒），
任何越界决策在写入前直接拒绝。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from ..config import ConfigError, load_config
from .rules import GRACE_BOUNDS, PICKER_BOUNDS, Tune

# 旋钮键名 → config 硬边界（护栏：apply 侧独立复核，不信任上游钳制）
_KNOB_BOUNDS = {
    "选择弹窗等待秒": PICKER_BOUNDS,
    "贴图渲染宽限秒": GRACE_BOUNDS,
}


def apply_tune(tune: Tune, ini_path: Path) -> bool:
    """把一次调参写回 config.ini；带边界复核，任何异常都放弃不改。

    返回 True=已写入；False=无变化/放弃（决策未变、键不存在、越界）。
    """
    if not tune.changed:
        return False
    bounds = _KNOB_BOUNDS.get(tune.key)
    if bounds and not (bounds[0] <= tune.new <= bounds[1]):
        return False                     # 越界决策：写前拒绝（纵深防御）
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
        _ = getattr(cfg.草稿, tune.key)   # 被调键确实存在且可读
    except (ConfigError, Exception):  # noqa: BLE001
        io.open(ini_path, "w", encoding="utf-8", newline="").write(text)
        return False
    return True
