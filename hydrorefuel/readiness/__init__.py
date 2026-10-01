"""分级就绪。

档位（能收车、能开阀、能交接）与每档判据按站点统一配置；车型只决定不满足
时的策略（全拒或降级到可做档位），不能自带判据与阈值。评估器先在内存里得出
唯一结论，再把同一份结论落审计并返回，控制台展示、接口响应、审计流水三者口径
一致。判据读不到现场数据属于部分失败，相关档位一律按未就绪处理。
"""

from __future__ import annotations

from .checks import (
    FAIL,
    MISSING,
    OK,
    CheckContext,
    CheckOutcome,
    known_checks,
)
from .evaluator import ReadinessEvaluator

__all__ = [
    "OK",
    "FAIL",
    "MISSING",
    "CheckContext",
    "CheckOutcome",
    "ReadinessEvaluator",
    "known_checks",
]
