"""分级就绪结果快照。

评估结果分成两层：单条判据（:class:`CheckSnapshot`）与一档就绪
（:class:`TierSnapshot`）。判据不满足时携带静态原因与最近一次相关审计
记录；取不到数的判据单独标记 ``data_missing``，供上层按部分失败处理。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckSnapshot:
    """一条就绪判据的评估结果。"""

    name: str
    label: str
    ok: bool
    data_missing: bool
    reason: str
    last_event: dict | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "ok": self.ok,
            "data_missing": self.data_missing,
            "reason": self.reason,
            "last_event": self.last_event,
        }


@dataclass(frozen=True)
class TierSnapshot:
    """一档就绪的评估结果（能收车 / 能开阀 / 能交接）。"""

    name: str
    label: str
    ok: bool
    data_missing: bool
    checks: tuple[CheckSnapshot, ...]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "ok": self.ok,
            "data_missing": self.data_missing,
            "failed": [check.name for check in self.checks if not check.ok],
            "checks": [check.as_dict() for check in self.checks],
        }
