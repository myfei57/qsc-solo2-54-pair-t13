"""分级就绪评估器。

评估器只做一件事：按站点配置的档位判据读现场，先在内存里把每档结果算成
一份快照，再原样发布审计事件并返回。控制台展示、接口响应、审计落盘用的是同一
份对象，不允许"判出来是一回事、记下去是另一回事"。

三档语义固定：``receive``（能收车）→ ``valve_open``（能开加注阀）→
``handoff``（能交接）。判据结果三态：满足（``ok``）、条件不满足
（``fail``）、数据缺失（``missing``）。任一判据 ``missing`` 属于部分失败：
该档按未就绪处理，即使车型选择降级策略，数据缺失的档位也不允许放行。

车型只决定策略，不决定判据：``reject_all`` 时目标档不满足整单拒绝；
``degrade`` 时给出当前确实可做的档位。判据清单与阈值全部来自站点统一配置。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock
from ..config import READINESS_TIERS, ReadinessConfig
from ..errors import InterlockError
from ..eventbus import Event, EventBus
from .checks import FAIL, MISSING, OK, CheckContext, CheckOutcome, run_check


@dataclass
class CheckState:
    """一条判据的最近一次结果，供控制台展示"最近一次原因"。"""

    status: str = MISSING
    reason: str = ""
    last_fail_reason: str = ""
    last_fail_ts: float | None = None
    last_missing_reason: str = ""
    last_missing_ts: float | None = None
    last_ok_ts: float | None = None

    def apply(self, outcome: CheckOutcome, ts: float) -> None:
        self.status = outcome.status
        self.reason = outcome.reason
        if outcome.status == FAIL:
            self.last_fail_reason = outcome.reason
            self.last_fail_ts = ts
        elif outcome.status == MISSING:
            self.last_missing_reason = outcome.reason
            self.last_missing_ts = ts
        else:
            self.last_ok_ts = ts

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "last_fail_reason": self.last_fail_reason,
            "last_fail_ts": self.last_fail_ts,
            "last_missing_reason": self.last_missing_reason,
            "last_missing_ts": self.last_missing_ts,
            "last_ok_ts": self.last_ok_ts,
        }


@dataclass
class TierEvaluation:
    """一个档位的本次评估结果。"""

    name: str
    label: str
    ready: bool
    partial_failure: bool
    checks: list[dict] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)
    missing_checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "ready": self.ready,
            "partial_failure": self.partial_failure,
            "checks": self.checks,
            "unsatisfied": self.unsatisfied,
            "missing_checks": self.missing_checks,
        }


class ReadinessEvaluator:
    """按站点配置评估分级就绪，并保证判定结果与审计一致。"""

    def __init__(
        self,
        config: ReadinessConfig,
        clock: Clock,
        bus: EventBus,
        station: str,
        pressure_signal_max_age_s: float = 30.0,
    ) -> None:
        # 装配期再核对一次目录，防止配置与判据实现脱节（正常由配置校验拦下）。
        from .checks import known_checks

        catalog = set(known_checks())
        referenced = {check for tier in config.tiers for check in tier.checks}
        for profile in config.vehicles:
            referenced.update(profile.extra_checks)
            referenced.update(check for tier in profile.tiers for check in tier.checks)
        unknown = referenced - catalog
        if unknown:
            raise ValueError(f"就绪配置引用了未登记的判据：{sorted(unknown)}")
        self._config = config
        self._clock = clock
        self._bus = bus
        self._station = station
        self._pressure_signal_max_age_s = pressure_signal_max_age_s
        self._states: dict[str, CheckState] = {check: CheckState() for check in sorted(catalog)}
        self._lock = threading.RLock()

    def _evaluate_check(self, check_id: str, ctx: CheckContext) -> CheckOutcome:
        """跑一条判据；判据自身抛错一律降级为数据缺失，绝不能放过。"""

        try:
            outcome = run_check(check_id, ctx)
        except Exception as error:  # 判据实现异常等价于读不到数据
            outcome = CheckOutcome(check_id, MISSING, f"判据 {check_id} 读取异常：{error}")
        return outcome

    def _tier(self, name: str, label: str, check_ids: tuple[str, ...], ctx: CheckContext, now: float) -> TierEvaluation:
        reports: list[dict] = []
        unsatisfied: list[str] = []
        missing: list[str] = []
        for check_id in check_ids:
            outcome = self._evaluate_check(check_id, ctx)
            state = self._states.setdefault(check_id, CheckState())
            state.apply(outcome, now)
            reports.append({**outcome.as_dict(), **state.as_dict()})
            if outcome.status == FAIL:
                unsatisfied.append(check_id)
            elif outcome.status == MISSING:
                missing.append(check_id)
        return TierEvaluation(
            name=name,
            label=label,
            # 数据缺失按未就绪：只有明确满足才算该档就绪。
            ready=not unsatisfied and not missing,
            partial_failure=bool(missing),
            checks=reports,
            unsatisfied=unsatisfied,
            missing_checks=missing,
        )

    def evaluate(
        self,
        ctx: CheckContext,
        *,
        vehicle_type: str,
        purpose: str = "readiness",
        emit_audit: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict:
        """评估某车型的三档就绪，返回快照；同一快照用于审计与展示。"""

        with self._lock:
            now = self._clock.now()
            profile = self._config.vehicle(vehicle_type)
            profile_found = self._config.knows_vehicle(vehicle_type)
            tiers: list[TierEvaluation] = []
            extra_results: list[dict] = []
            for tier_name in READINESS_TIERS:
                configured = profile.tier(tier_name)
                tiers.append(self._tier(configured.name, configured.label, configured.checks, ctx, now))
            for check_id in profile.extra_checks:
                outcome = self._evaluate_check(check_id, ctx)
                state = self._states.setdefault(check_id, CheckState())
                state.apply(outcome, now)
                extra_results.append({**outcome.as_dict(), **state.as_dict()})

            tier_by_name = {tier.name: tier for tier in tiers}
            ready_names = [tier.name for tier in tiers if tier.ready]
            allowed = self._allowed_tiers(tier_by_name, profile.policy)
            partial = any(tier.partial_failure for tier in tiers) or any(
                item["status"] == MISSING for item in extra_results
            )
            snapshot = {
                "ts": round(now, 3),
                "station": self._station,
                "vehicle": ctx.vehicle,
                "vehicle_type": vehicle_type,
                "profile_found": profile_found,
                "policy": profile.policy,
                "purpose": purpose,
                "order": list(READINESS_TIERS),
                "tiers": [tier.as_dict() for tier in tiers],
                "extra_checks": extra_results,
                "ready_tiers": ready_names,
                "allowed_tiers": allowed,
                "highest_tier": allowed[-1] if allowed else None,
                "all_ready": all(tier.ready for tier in tiers),
                "partial_failure": partial,
            }
            if extra:
                snapshot.update(extra)
            if emit_audit:
                severity = "warning" if partial else "notice"
                self._bus.publish(
                    Event.of(
                        "readiness",
                        "readiness",
                        f"readiness_{purpose}",
                        station=self._station,
                        severity=severity,
                        message=self._audit_message(snapshot),
                        evaluation=self._audit_payload(snapshot),
                    )
                )
            return snapshot

    @staticmethod
    def _allowed_tiers(tiers: dict[str, TierEvaluation], policy: str) -> list[str]:
        """按车型策略给出当前可做的档位。

        三档是作业链上先后独立的许可门，不层层包含：能收车不要求枪已落座，
        能交接时加注阀当然已关。``degrade`` 放行所有当前就绪的档；
        ``reject_all`` 的全拒只作用在 gate 的目标档上，这里仍只列就绪档。
        """

        return [name for name in READINESS_TIERS if tiers[name].ready]

    @staticmethod
    def _audit_payload(snapshot: dict) -> dict:
        """审计里只落结论与每条判据状态，不重复整份现场快照。"""

        return {
            "vehicle_type": snapshot["vehicle_type"],
            "vehicle": snapshot["vehicle"],
            "policy": snapshot["policy"],
            "profile_found": snapshot["profile_found"],
            "all_ready": snapshot["all_ready"],
            "partial_failure": snapshot["partial_failure"],
            "allowed_tiers": snapshot["allowed_tiers"],
            "highest_tier": snapshot["highest_tier"],
            "tiers": [
                {
                    "name": tier["name"],
                    "ready": tier["ready"],
                    "partial_failure": tier["partial_failure"],
                    "unsatisfied": tier["unsatisfied"],
                    "missing_checks": tier["missing_checks"],
                }
                for tier in snapshot["tiers"]
            ],
        }

    @staticmethod
    def _audit_message(snapshot: dict) -> str:
        vehicle_type = snapshot["vehicle_type"]
        if snapshot["partial_failure"]:
            return f"{vehicle_type} 分级就绪评估含数据缺失，相关档位按未就绪处理"
        if snapshot["all_ready"]:
            return f"{vehicle_type} 三档全部就绪"
        missing = [name for name in snapshot["order"] if name not in snapshot["ready_tiers"]]
        return f"{vehicle_type} 未就绪档位：{', '.join(missing)}"

    def gate(
        self,
        ctx: CheckContext,
        *,
        vehicle_type: str,
        requested_tier: str,
        purpose: str,
    ) -> dict:
        """按车型策略对某一档位放行，返回决策快照。

        判定与审计共用 :meth:`evaluate` 的同一份结果，这里只做策略结论，
        不再重算任何判据，避免放行结论与落审计的结论分叉。
        """

        if requested_tier not in READINESS_TIERS:
            raise InterlockError(f"未知的就绪档位：{requested_tier}", component="readiness")
        snapshot = self.evaluate(
            ctx,
            vehicle_type=vehicle_type,
            purpose=f"gate_{purpose}",
            emit_audit=False,
        )
        tiers = {tier["name"]: tier for tier in snapshot["tiers"]}
        target = tiers[requested_tier]
        allowed = snapshot["allowed_tiers"]
        accepted = requested_tier in allowed
        # 两档策略的差异只体现在"不满足时的回执"上：
        # reject_all 只回拒绝；degrade 同时给出当前可做的档位供调用方降级。
        decision = {
            "requested_tier": requested_tier,
            "accepted": accepted,
            "policy": snapshot["policy"],
            # 全拒：不满足时整单不给出可做档位；降级：明确给出当前可做档位。
            "allowed_tiers": [] if (not accepted and snapshot["policy"] == "reject_all") else allowed,
            "highest_tier": snapshot["highest_tier"] if (accepted or snapshot["policy"] == "degrade") else None,
            "degrade_offered": snapshot["policy"] == "degrade" and not accepted,
            "reason": self._gate_reason(target, snapshot, accepted),
        }
        snapshot["gate"] = decision
        # 一次 gate 只补一条审计：评估结论 + 放行结论同记，拒绝用告警档。
        self._bus.publish(
            Event.of(
                "readiness",
                "readiness",
                f"gate_{purpose}",
                station=self._station,
                severity="alarm" if not accepted else "notice",
                message=decision["reason"],
                evaluation=self._audit_payload(snapshot),
                gate=decision,
            )
        )
        return snapshot

    @staticmethod
    def _gate_reason(target: dict, snapshot: dict, accepted: bool) -> str:
        vehicle_type = snapshot["vehicle_type"]
        tier_label = target["label"]
        if accepted:
            return f"{vehicle_type} {tier_label} 放行"
        if target["partial_failure"]:
            return f"{vehicle_type} {tier_label} 判据数据缺失（{', '.join(target['missing_checks'])}），按未就绪拒绝"
        if snapshot["policy"] == "degrade":
            highest = snapshot["highest_tier"]
            return (
                f"{vehicle_type} {tier_label} 不满足，降级到可做档位：{highest}"
                if highest
                else f"{vehicle_type} {tier_label} 不满足，当前无可做档位"
            )
        return f"{vehicle_type} 全拒策略：{tier_label} 不满足（{', '.join(target['unsatisfied'])}）"

    def check_states(self) -> dict:
        """返回全部判据的最近一次状态，供控制台与巡检核对。"""

        with self._lock:
            return {check: state.as_dict() for check, state in self._states.items()}
