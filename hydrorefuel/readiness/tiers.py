"""分级就绪评估核心。

就绪分成三档：能收车（``accept``）、能开阀（``valve``）、能交接
（``handover``）。每档由若干判据组成，判据与物理联锁一一对应，在代码里
固定；按车型可配的是失败策略（见 :class:`hydrorefuel.config.ReadinessLimits`）。

评估只读探测表：任何一条判据取不到数（没有数据源、数据源抛错或返回
``None``）都算该档数据缺失——属于部分失败，整档按未就绪处理，不影响
其他档位的评估结果。
"""

from __future__ import annotations

from typing import Callable, Mapping

from ..config import STRICT
from .snapshot import CheckSnapshot, TierSnapshot

MISSING_REASON = "判据数据缺失，按未就绪处理"

# 档位定义：(名称, 展示标签)，顺序即控制台展示顺序。
TIER_ORDER: tuple[tuple[str, str], ...] = (
    ("accept", "能收车"),
    ("valve", "能开阀"),
    ("handover", "能交接"),
)

# 判据定义：名称 -> (展示标签, 不满足时的静态原因, 关联审计组件)。
CHECK_DEFS: dict[str, tuple[str, str, str]] = {
    "running": ("平台运行中", "平台未运行", "station"),
    "queue_has_capacity": ("加注队列有余量", "加注队列已满", "queue"),
    "confirm_idle": ("无挂起的加注确认", "存在未完成的加注确认", "queue"),
    "precool_ready": ("预冷已保持就绪", "预冷保持时长不足", "cool"),
    "temperature_in_band": ("枪口温度在允许带", "枪口温度未落到加注允许带", "cool"),
    "nozzle_latched": ("加注枪已落座咬合", "加注枪未落座咬合", "disp"),
    "leak_latch_clear": ("泄漏闩锁未触发", "泄漏闩锁保持中", "leak"),
    "dispenser_idle": ("加注机未在加注", "加注进行中，不能交接", "disp"),
}

# 各档包含的判据。
TIER_CHECKS: dict[str, tuple[str, ...]] = {
    "accept": ("running", "queue_has_capacity", "confirm_idle"),
    "valve": ("precool_ready", "temperature_in_band", "nozzle_latched", "leak_latch_clear"),
    "handover": ("dispenser_idle", "leak_latch_clear", "confirm_idle"),
}


def evaluate(
    probes: Mapping[str, Callable[[], bool]],
    last_event_for: Callable[[str], dict | None],
) -> tuple[TierSnapshot, ...]:
    """逐档评估就绪判据。

    ``probes`` 以判据名为键给出只读数据源；``last_event_for`` 按组件名返回
    最近一次相关审计记录（没有则为 ``None``），作为判据不满足的最近原因。
    """

    tiers = []
    for tier_name, tier_label in TIER_ORDER:
        checks = []
        for check_name in TIER_CHECKS[tier_name]:
            label, reason, component = CHECK_DEFS[check_name]
            probe = probes.get(check_name)
            value: object = None
            if probe is not None:
                try:
                    value = probe()
                except Exception:  # noqa: BLE001 - 数据源异常一律按数据缺失处理
                    value = None
            missing = value is None
            ok = not missing and bool(value)
            checks.append(
                CheckSnapshot(
                    name=check_name,
                    label=label,
                    ok=ok,
                    data_missing=missing,
                    reason="" if ok else (MISSING_REASON if missing else reason),
                    last_event=None if ok else last_event_for(component),
                )
            )
        tiers.append(
            TierSnapshot(
                name=tier_name,
                label=tier_label,
                ok=all(check.ok for check in checks),
                data_missing=any(check.data_missing for check in checks),
                checks=tuple(checks),
            )
        )
    return tuple(tiers)


def decide(tiers: tuple[TierSnapshot, ...], on_failure: str) -> dict:
    """按车型失败策略汇总档位评估结果。

    全部档位就绪时 ``decision`` 为 ``ready``；否则 ``strict`` 策略全拒
    （可做档位清空），``degraded`` 策略给出当前可做的档位。
    """

    failed = [tier.name for tier in tiers if not tier.ok]
    allowed = [tier.name for tier in tiers if tier.ok]
    ready = not failed
    if ready:
        decision = "ready"
    elif on_failure == STRICT:
        allowed = []
        decision = "rejected"
    else:
        decision = "degraded"
    return {
        "ready": ready,
        "decision": decision,
        "allowed_tiers": allowed,
        "failed_tiers": failed,
        "data_missing": any(tier.data_missing for tier in tiers),
    }
