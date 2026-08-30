# -*- coding: utf-8 -*-
"""自复盘模块：每日收官后 观测→诊断→有界自调参→报告。

设计原则（对齐 We-AIPO 审计环）：
- 数据源：autopub.log 全量事件 + state.db publish_records 台账
- 自调参只动白名单参数（选择弹窗等待秒），且严格有界、逐日 ±5、
  任何异常证据立即回撤——"安全无封禁"永远优先于"更快"
- 其余发现只出报告不自动改码，供人工/Agent 会话消化后落生产代码
"""
from .engine import run_retro

__all__ = ["run_retro"]
