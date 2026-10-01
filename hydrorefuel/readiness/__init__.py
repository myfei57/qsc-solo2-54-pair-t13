"""分级就绪评估。

把加注就绪从单一总状态拆成三档：能收车、能开阀、能交接。每档列出具体
哪条判据不满足与最近一次原因；判据数据缺失属于部分失败，整档按未就绪
处理。评估结果供控制台展示并落审计，两处共用同一份结果。
"""

from __future__ import annotations

from .snapshot import CheckSnapshot, TierSnapshot
from .tiers import CHECK_DEFS, TIER_CHECKS, TIER_ORDER, decide, evaluate

__all__ = [
    "CHECK_DEFS",
    "TIER_CHECKS",
    "TIER_ORDER",
    "CheckSnapshot",
    "TierSnapshot",
    "decide",
    "evaluate",
]
