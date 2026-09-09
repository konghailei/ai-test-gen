# -*- coding: utf-8 -*-
"""数字工具（v3.2.2 收敛重复）。

原先 `_clamp` 在 review_agent / core/review_reports 各定义一份，
gen_agent 又内联了 3 处 `max(0.0, min(1.0, v))`——统一收敛到这里。
"""
from __future__ import annotations


def clamp01(value: float, ndigits: int = 3) -> float:
    """把数值收敛到 [0, 1] 并保留 ndigits 位小数。

    用于置信度归一化，保证边界与精度口径全仓一致。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    clamped = max(0.0, min(1.0, v))
    return round(clamped, ndigits) if ndigits is not None else clamped
