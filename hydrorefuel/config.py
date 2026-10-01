"""平台配置。

配置来自 JSON 文件（标准库解析），覆盖槽车卸氢压力与放散确认阈值、压缩机
入口与目标压力、瓶组压力带与基线有效期、预冷目标温度、流量阶梯公差、加注
枪落座行程、泄漏阀值与加注队列容量。任何缺失或越界取值都在装配阶段抛出
:class:`ConfigError`，不允许带着半份配置启动控制链。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConfigError

_MISSING = object()

# 分级就绪的三档是定死的：站点只能配置每档挂哪些判据、用什么文案，
# 不能新增或删减档位。顺序即作业先后：收车 → 开阀 → 交接。
READINESS_TIERS: tuple[str, ...] = ("receive", "valve_open", "handoff")
READINESS_TIER_LABELS: dict[str, str] = {
    "receive": "能收车",
    "valve_open": "能开阀",
    "handoff": "能交接",
}
# 车型策略只有两种：不满足时整单全拒，或降级给出当前可做的档位。
READINESS_POLICIES: tuple[str, ...] = ("reject_all", "degrade")
# 判据目录是站点级的唯一事实来源；车型覆盖只能引用这里登记过的判据。
READINESS_CHECKS: tuple[str, ...] = (
    "platform_running",
    "leak_clear",
    "precool_ready",
    "temperature_in_band",
    "nozzle_latched",
    "nozzle_unseated",
    "confirm_idle",
    "buffer_in_band",
    "baseline_fresh",
    "queue_has_room",
    "fill_valve_closed",
    "line_pressure_safe",
    "meter_has_reading",
    "buffer_pressure_fresh",
)


def _section(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if value is None:
        raise ConfigError(f"配置缺少段落 {key!r}", component="config")
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段落 {key!r} 必须是映射", component="config")
    return value


def _number(section: Mapping[str, Any], key: str, *, minimum: float | None = None) -> float:
    value = section.get(key, _MISSING)
    if value is _MISSING:
        raise ConfigError(f"配置缺少字段 {key!r}", component="config")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置字段 {key!r} 必须是数值", component="config")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置字段 {key!r} 不得小于 {minimum}", component="config")
    return number


def _integer(section: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = section.get(key, _MISSING)
    if value is _MISSING:
        raise ConfigError(f"配置缺少字段 {key!r}", component="config")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置字段 {key!r} 必须是整数", component="config")
    if value < minimum:
        raise ConfigError(f"配置字段 {key!r} 不得小于 {minimum}", component="config")
    return value


@dataclass(frozen=True)
class TruckLimits:
    """槽车卸氢压力与放散确认约束。"""

    hose_pressure_max_mpa: float
    vent_confirm_mpa: float
    vent_timeout_s: float
    unload_rate_kg_per_min: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "TruckLimits":
        limits = cls(
            hose_pressure_max_mpa=_number(section, "hose_pressure_max_mpa", minimum=1.0),
            vent_confirm_mpa=_number(section, "vent_confirm_mpa", minimum=0.0),
            vent_timeout_s=_number(section, "vent_timeout_s", minimum=1.0),
            unload_rate_kg_per_min=_number(section, "unload_rate_kg_per_min", minimum=0.1),
        )
        if limits.vent_confirm_mpa >= limits.hose_pressure_max_mpa:
            raise ConfigError("放散确认压力必须小于软管最高压力", component="config")
        return limits

    def discharge_ready(self, pressure_mpa: float) -> bool:
        """软管压力是否已降到可以拔管的水平。"""

        return pressure_mpa <= self.vent_confirm_mpa


@dataclass(frozen=True)
class VentLimits:
    """放散阀孔径与闩锁约束。"""

    orifice_mm: float
    rate_coeff: float
    ambient_mpa: float
    latch_travel_mm: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "VentLimits":
        limits = cls(
            orifice_mm=_number(section, "orifice_mm", minimum=0.5),
            rate_coeff=_number(section, "rate_coeff", minimum=0.1),
            ambient_mpa=_number(section, "ambient_mpa", minimum=0.0),
            latch_travel_mm=_number(section, "latch_travel_mm", minimum=1.0),
        )
        if limits.rate_coeff > 1.0:
            raise ConfigError("放散系数不得超过 1.0", component="config")
        return limits

    def vent_rate_mpa_per_s(self, pressure_mpa: float) -> float:
        """按孔口流量估算的压降速率，压力越低放散越慢。"""

        overpressure = max(0.0, pressure_mpa - self.ambient_mpa)
        return self.rate_coeff * self.orifice_mm * overpressure / 100.0


@dataclass(frozen=True)
class CompressorLimits:
    """压缩机入口、目标压力与喘振约束。"""

    inlet_min_mpa: float
    outlet_target_mpa: float
    ramp_mpa_per_s: float
    surge_band_mpa: float
    min_flow_kg_per_min: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "CompressorLimits":
        limits = cls(
            inlet_min_mpa=_number(section, "inlet_min_mpa", minimum=0.0),
            outlet_target_mpa=_number(section, "outlet_target_mpa", minimum=1.0),
            ramp_mpa_per_s=_number(section, "ramp_mpa_per_s", minimum=0.05),
            surge_band_mpa=_number(section, "surge_band_mpa", minimum=0.1),
            min_flow_kg_per_min=_number(section, "min_flow_kg_per_min", minimum=0.1),
        )
        if limits.outlet_target_mpa <= limits.inlet_min_mpa:
            raise ConfigError("压缩机目标压力必须高于入口最低压力", component="config")
        return limits


@dataclass(frozen=True)
class BufferLimits:
    """储氢瓶组压力带与基线有效期。"""

    target_pressure_mpa: float
    pressure_tolerance_mpa: float
    baseline_ttl_s: float
    capacity_kg: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "BufferLimits":
        limits = cls(
            target_pressure_mpa=_number(section, "target_pressure_mpa", minimum=1.0),
            pressure_tolerance_mpa=_number(section, "pressure_tolerance_mpa", minimum=0.1),
            baseline_ttl_s=_number(section, "baseline_ttl_s", minimum=1.0),
            capacity_kg=_number(section, "capacity_kg", minimum=1.0),
        )
        if limits.pressure_tolerance_mpa >= limits.target_pressure_mpa:
            raise ConfigError("瓶组压力公差必须小于目标压力", component="config")
        return limits

    def within(self, pressure_mpa: float) -> bool:
        """压力是否落在目标带内。"""

        return abs(pressure_mpa - self.target_pressure_mpa) <= self.pressure_tolerance_mpa


@dataclass(frozen=True)
class CoolLimits:
    """预冷目标温度与保持时长约束。"""

    target_temp_c: float
    temp_band_c: float
    cool_rate_c_per_s: float
    precool_hold_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "CoolLimits":
        limits = cls(
            target_temp_c=_number(section, "target_temp_c"),
            temp_band_c=_number(section, "temp_band_c", minimum=0.5),
            cool_rate_c_per_s=_number(section, "cool_rate_c_per_s", minimum=0.05),
            precool_hold_s=_number(section, "precool_hold_s", minimum=1.0),
        )
        if limits.target_temp_c >= 0.0:
            raise ConfigError("预冷目标温度必须低于 0 度", component="config")
        return limits

    def reached(self, temperature_c: float) -> bool:
        """温度是否已落到加注允许带。"""

        return abs(temperature_c - self.target_temp_c) <= self.temp_band_c


@dataclass(frozen=True)
class FlowLimits:
    """质量流量阶梯与速率约束。"""

    step_target_kg: float
    step_tolerance_kg: float
    max_rate_kg_per_min: float
    min_rate_kg_per_min: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "FlowLimits":
        limits = cls(
            step_target_kg=_number(section, "step_target_kg", minimum=0.1),
            step_tolerance_kg=_number(section, "step_tolerance_kg", minimum=0.01),
            max_rate_kg_per_min=_number(section, "max_rate_kg_per_min", minimum=0.1),
            min_rate_kg_per_min=_number(section, "min_rate_kg_per_min", minimum=0.01),
        )
        if not limits.min_rate_kg_per_min < limits.max_rate_kg_per_min:
            raise ConfigError("流量下限必须小于上限", component="config")
        if limits.step_tolerance_kg >= limits.step_target_kg:
            raise ConfigError("阶梯公差必须小于阶梯目标", component="config")
        return limits

    def within_step(self, mass_kg: float) -> bool:
        """本阶梯累计质量是否落在公差内。"""

        return abs(mass_kg - self.step_target_kg) <= self.step_tolerance_kg


@dataclass(frozen=True)
class DispenserLimits:
    """加注机枪位、落座与隔离约束。"""

    nozzle_seat_travel_mm: float
    seat_timeout_s: float
    max_line_pressure_mpa: float
    isolate_lead_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "DispenserLimits":
        limits = cls(
            nozzle_seat_travel_mm=_number(section, "nozzle_seat_travel_mm", minimum=1.0),
            seat_timeout_s=_number(section, "seat_timeout_s", minimum=1.0),
            max_line_pressure_mpa=_number(section, "max_line_pressure_mpa", minimum=1.0),
            isolate_lead_s=_number(section, "isolate_lead_s", minimum=0.0),
        )
        if limits.isolate_lead_s >= limits.seat_timeout_s:
            raise ConfigError("隔离提前量必须小于落座超时", component="config")
        return limits


@dataclass(frozen=True)
class LeakLimits:
    """泄漏阀值与闩锁保持约束。"""

    threshold_ppm: float
    sample_window_s: float
    hold_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "LeakLimits":
        limits = cls(
            threshold_ppm=_number(section, "threshold_ppm", minimum=1.0),
            sample_window_s=_number(section, "sample_window_s", minimum=0.1),
            hold_s=_number(section, "hold_s", minimum=1.0),
        )
        if limits.hold_s <= limits.sample_window_s:
            raise ConfigError("泄漏闩锁保持时长必须大于采样窗口", component="config")
        return limits

    def tripped(self, concentration_ppm: float) -> bool:
        """浓度是否达到泄漏阀值。"""

        return concentration_ppm >= self.threshold_ppm


@dataclass(frozen=True)
class QueueLimits:
    """加注队列容量与确认超时约束。"""

    max_waiting: int
    confirm_timeout_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "QueueLimits":
        return cls(
            max_waiting=_integer(section, "max_waiting", minimum=1),
            confirm_timeout_s=_number(section, "confirm_timeout_s", minimum=1.0),
        )


@dataclass(frozen=True)
class ReadinessTier:
    """一个就绪档位：固定 ID、可配文案与判据清单（按站点统一配置）。"""

    name: str
    label: str
    checks: tuple[str, ...]


@dataclass(frozen=True)
class VehicleReadiness:
    """车型策略：只允许覆盖各档判据引用并选择全拒/降级，不允许改判据本身。"""

    vehicle_type: str
    policy: str
    tiers: tuple[ReadinessTier, ...]
    extra_checks: tuple[str, ...] = ()

    def tier(self, name: str) -> ReadinessTier:
        """按档位名取车型覆盖后的配置，不存在时抛出 ``KeyError``。"""

        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise KeyError(name)


@dataclass(frozen=True)
class ReadinessConfig:
    """分级就绪配置：站点统一的档位判据 + 按车型的拒绝策略。"""

    tiers: tuple[ReadinessTier, ...]
    vehicles: tuple[VehicleReadiness, ...]

    def tier(self, name: str) -> ReadinessTier:
        """按档位名取配置，不存在时抛出 ``KeyError``。"""

        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise KeyError(name)

    def vehicle(self, vehicle_type: str) -> VehicleReadiness:
        """按车型取策略，未知车型回落到 ``default``。"""

        for profile in self.vehicles:
            if profile.vehicle_type == vehicle_type:
                return profile
        return self.vehicle("default")

    def knows_vehicle(self, vehicle_type: str) -> bool:
        """车型是否有显式配置（未显式配置的走默认策略）。"""

        return any(profile.vehicle_type == vehicle_type for profile in self.vehicles)

    @classmethod
    def from_mapping(
        cls,
        section: Mapping[str, Any] | None,
        fallback: "ReadinessConfig | None" = None,
    ) -> "ReadinessConfig":
        """解析 ``readiness`` 段落；段落整体缺失时使用内置默认值。

        档位与判据由站点统一配置，车型段只能引用判据、选择策略，任何未知
        档位、未知判据或未知策略都在装配期拒绝，不允许带半份配置启动。
        """

        if section is None:
            if fallback is not None:
                return fallback
            section = {}
        if not isinstance(section, Mapping):
            raise ConfigError("配置段落 'readiness' 必须是映射", component="config")

        def parse_checks(raw: Any, *, allow_empty: bool = False) -> tuple[str, ...]:
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ConfigError("就绪判据必须是字符串列表", component="config")
            checks = tuple(raw)
            if not allow_empty and not checks:
                raise ConfigError("就绪档位至少要配置一条判据", component="config")
            unknown = [item for item in checks if item not in READINESS_CHECKS]
            if unknown:
                raise ConfigError(f"未知的就绪判据: {unknown}", component="config")
            if len(set(checks)) != len(checks):
                raise ConfigError("同一档位的就绪判据不得重复", component="config")
            return checks

        default_labels = dict(READINESS_TIER_LABELS)
        defaults_by_name = {tier.name: tier for tier in (fallback.tiers if fallback else ())}

        tiers_raw = section.get("tiers")
        if tiers_raw is None:
            tier_sources = {tier.name: tier for tier in (fallback.tiers if fallback else ())}
        else:
            if not isinstance(tiers_raw, Mapping):
                raise ConfigError("readiness.tiers 必须是映射", component="config")
            tier_sources = {}
            for name in READINESS_TIERS:
                entry = tiers_raw.get(name)
                if entry is None:
                    if name in defaults_by_name:
                        tier_sources[name] = defaults_by_name[name]
                        continue
                    raise ConfigError(f"配置缺少就绪档位 {name!r}", component="config")
                if not isinstance(entry, Mapping):
                    raise ConfigError(f"就绪档位 {name!r} 必须是映射", component="config")
                label = entry.get("label", default_labels.get(name, name))
                if not isinstance(label, str) or not label:
                    raise ConfigError(f"就绪档位 {name!r} 的 label 必须是非空字符串", component="config")
                checks = parse_checks(entry.get("checks", ()))
                tier_sources[name] = ReadinessTier(name=name, label=label, checks=checks)
            unknown_tiers = [name for name in tiers_raw if name not in READINESS_TIERS]
            if unknown_tiers:
                raise ConfigError(f"未知的就绪档位（档位定死为 {list(READINESS_TIERS)}）: {unknown_tiers}", component="config")
        missing_tiers = [name for name in READINESS_TIERS if name not in tier_sources]
        if missing_tiers:
            raise ConfigError(f"配置缺少就绪档位: {missing_tiers}", component="config")
        tiers = tuple(tier_sources[name] for name in READINESS_TIERS)
        site_checks = {check for tier in tiers for check in tier.checks}

        vehicles_raw = section.get("vehicles", {})
        if not isinstance(vehicles_raw, Mapping):
            raise ConfigError("readiness.vehicles 必须是映射", component="config")

        def parse_vehicle(vehicle_type: str, entry: Any) -> VehicleReadiness:
            if not isinstance(entry, Mapping):
                raise ConfigError(f"车型 {vehicle_type!r} 的就绪配置必须是映射", component="config")
            policy = entry.get("policy", "reject_all")
            if policy not in READINESS_POLICIES:
                raise ConfigError(
                    f"车型 {vehicle_type!r} 的拒绝策略必须是 {list(READINESS_POLICIES)} 之一",
                    component="config",
                )
            overrides = entry.get("tiers", {})
            if not isinstance(overrides, Mapping):
                raise ConfigError(f"车型 {vehicle_type!r} 的 tiers 必须是映射", component="config")
            unknown = [name for name in overrides if name not in READINESS_TIERS]
            if unknown:
                raise ConfigError(f"车型 {vehicle_type!r} 出现未知档位: {unknown}", component="config")

            def tier_checks(tier: ReadinessTier) -> tuple[str, ...]:
                override = overrides.get(tier.name)
                if override is None:
                    return tier.checks
                if not isinstance(override, Mapping) or "checks" not in override:
                    raise ConfigError(
                        f"车型 {vehicle_type!r} 的档位 {tier.name!r} 必须是带 checks 的映射",
                        component="config",
                    )
                return parse_checks(override["checks"])

            profile_tiers = tuple(
                ReadinessTier(name=tier.name, label=tier.label, checks=tier_checks(tier)) for tier in tiers
            )
            extra = parse_checks(entry.get("extra_checks", ()), allow_empty=True)
            collide = [check for check in extra if check in site_checks]
            if collide:
                raise ConfigError(
                    f"车型 {vehicle_type!r} 的额外判据 {collide} 已包含在站点档位中，无需重复声明",
                    component="config",
                )
            return VehicleReadiness(
                vehicle_type=vehicle_type,
                policy=policy,
                tiers=profile_tiers,
                extra_checks=extra,
            )

        profiles: list[VehicleReadiness] = []
        default_entry = vehicles_raw.get("default", {"policy": "reject_all"})
        profiles.append(parse_vehicle("default", default_entry))
        for vehicle_type, entry in vehicles_raw.items():
            if vehicle_type == "default":
                continue
            profiles.append(parse_vehicle(vehicle_type, entry))
        return cls(tiers=tuple(tiers), vehicles=tuple(profiles))


@dataclass(frozen=True)
class StationConfig:
    """一份完整的平台配置。"""

    name: str
    storage_root: str
    truck: TruckLimits
    vent: VentLimits
    compressor: CompressorLimits
    buffer: BufferLimits
    cool: CoolLimits
    flow: FlowLimits
    dispenser: DispenserLimits
    leak: LeakLimits
    queue: QueueLimits
    readiness: ReadinessConfig

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "StationConfig":
        meta = _section(mapping, "station")
        name = meta.get("name")
        storage_root = meta.get("storage_root")
        if not isinstance(name, str) or not name:
            raise ConfigError("station.name 必须是非空字符串", component="config")
        if not isinstance(storage_root, str) or not storage_root:
            raise ConfigError("station.storage_root 必须是非空字符串", component="config")

        limits = _section(mapping, "limits")
        # readiness 段落整体缺失时回落到内置默认值，旧配置文件无需修改即可启动。
        readiness = ReadinessConfig.from_mapping(mapping.get("readiness"), fallback=default_readiness_config())
        return cls(
            name=name,
            storage_root=storage_root,
            truck=TruckLimits.from_mapping(_section(limits, "truck")),
            vent=VentLimits.from_mapping(_section(limits, "vent")),
            compressor=CompressorLimits.from_mapping(_section(limits, "comp")),
            buffer=BufferLimits.from_mapping(_section(limits, "buffer")),
            cool=CoolLimits.from_mapping(_section(limits, "cool")),
            flow=FlowLimits.from_mapping(_section(limits, "flow")),
            dispenser=DispenserLimits.from_mapping(_section(limits, "disp")),
            leak=LeakLimits.from_mapping(_section(limits, "leak")),
            queue=QueueLimits.from_mapping(_section(limits, "queue")),
            readiness=readiness,
        )


def load_config(path: str) -> StationConfig:
    """从 JSON 文件读取配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件顶层必须是映射", component="config")
    return StationConfig.from_mapping(raw)


def default_readiness_config() -> ReadinessConfig:
    """内置分级就绪配置：档位判据站点统一，车型只定全拒/降级策略。"""

    receive = ReadinessTier(
        "receive",
        READINESS_TIER_LABELS["receive"],
        (
            "platform_running",
            "leak_clear",
            "queue_has_room",
            "buffer_in_band",
            "baseline_fresh",
        ),
    )
    valve_open = ReadinessTier(
        "valve_open",
        READINESS_TIER_LABELS["valve_open"],
        (
            "platform_running",
            "leak_clear",
            "precool_ready",
            "temperature_in_band",
            "nozzle_latched",
            "line_pressure_safe",
            "confirm_idle",
        ),
    )
    handoff = ReadinessTier(
        "handoff",
        READINESS_TIER_LABELS["handoff"],
        (
            "platform_running",
            "leak_clear",
            "fill_valve_closed",
            "nozzle_unseated",
            "meter_has_reading",
        ),
    )
    tiers = (receive, valve_open, handoff)
    return ReadinessConfig(
        tiers=tiers,
        vehicles=(
            # 默认全拒：任何一档不满足都不给放行；保守车型不显式配置时也走它。
            VehicleReadiness("default", "reject_all", tiers),
            # 公交线车型示范降级策略：开不了阀也允许收车交接，按可做档位执行。
            # 额外判据仍然来自站点登记目录，车型不自带阈值。
            VehicleReadiness(
                "citybus",
                "degrade",
                tiers,
                extra_checks=("buffer_pressure_fresh",),
            ),
        ),
    )


def default_config() -> StationConfig:
    """内置默认配置，供未指定配置文件时装配使用。"""

    return StationConfig(
        name="refueling-control",
        storage_root="var/state",
        truck=TruckLimits(25.0, 1.5, 90.0, 6.0),
        vent=VentLimits(6.0, 0.9, 0.1, 40.0),
        compressor=CompressorLimits(3.0, 43.0, 0.8, 1.2, 1.5),
        buffer=BufferLimits(42.0, 1.5, 3600.0, 350.0),
        cool=CoolLimits(-40.0, 3.0, 0.6, 20.0),
        flow=FlowLimits(1.8, 0.15, 3.6, 0.4),
        dispenser=DispenserLimits(55.0, 15.0, 44.0, 2.0),
        leak=LeakLimits(4000.0, 5.0, 30.0),
        queue=QueueLimits(12, 45.0),
        readiness=default_readiness_config(),
    )


def config_to_mapping(cfg: StationConfig) -> dict:
    """把配置还原成可打印的映射。"""

    return {
        "station": {"name": cfg.name, "storage_root": cfg.storage_root},
        "limits": {
            "truck": {
                "hose_pressure_max_mpa": cfg.truck.hose_pressure_max_mpa,
                "vent_confirm_mpa": cfg.truck.vent_confirm_mpa,
                "vent_timeout_s": cfg.truck.vent_timeout_s,
                "unload_rate_kg_per_min": cfg.truck.unload_rate_kg_per_min,
            },
            "vent": {
                "orifice_mm": cfg.vent.orifice_mm,
                "rate_coeff": cfg.vent.rate_coeff,
                "ambient_mpa": cfg.vent.ambient_mpa,
                "latch_travel_mm": cfg.vent.latch_travel_mm,
            },
            "comp": {
                "inlet_min_mpa": cfg.compressor.inlet_min_mpa,
                "outlet_target_mpa": cfg.compressor.outlet_target_mpa,
                "ramp_mpa_per_s": cfg.compressor.ramp_mpa_per_s,
                "surge_band_mpa": cfg.compressor.surge_band_mpa,
                "min_flow_kg_per_min": cfg.compressor.min_flow_kg_per_min,
            },
            "buffer": {
                "target_pressure_mpa": cfg.buffer.target_pressure_mpa,
                "pressure_tolerance_mpa": cfg.buffer.pressure_tolerance_mpa,
                "baseline_ttl_s": cfg.buffer.baseline_ttl_s,
                "capacity_kg": cfg.buffer.capacity_kg,
            },
            "cool": {
                "target_temp_c": cfg.cool.target_temp_c,
                "temp_band_c": cfg.cool.temp_band_c,
                "cool_rate_c_per_s": cfg.cool.cool_rate_c_per_s,
                "precool_hold_s": cfg.cool.precool_hold_s,
            },
            "flow": {
                "step_target_kg": cfg.flow.step_target_kg,
                "step_tolerance_kg": cfg.flow.step_tolerance_kg,
                "max_rate_kg_per_min": cfg.flow.max_rate_kg_per_min,
                "min_rate_kg_per_min": cfg.flow.min_rate_kg_per_min,
            },
            "disp": {
                "nozzle_seat_travel_mm": cfg.dispenser.nozzle_seat_travel_mm,
                "seat_timeout_s": cfg.dispenser.seat_timeout_s,
                "max_line_pressure_mpa": cfg.dispenser.max_line_pressure_mpa,
                "isolate_lead_s": cfg.dispenser.isolate_lead_s,
            },
            "leak": {
                "threshold_ppm": cfg.leak.threshold_ppm,
                "sample_window_s": cfg.leak.sample_window_s,
                "hold_s": cfg.leak.hold_s,
            },
            "queue": {
                "max_waiting": cfg.queue.max_waiting,
                "confirm_timeout_s": cfg.queue.confirm_timeout_s,
            },
        },
        "readiness": {
            "tiers": {
                tier.name: {"label": tier.label, "checks": list(tier.checks)}
                for tier in cfg.readiness.tiers
            },
            "vehicles": {
                profile.vehicle_type: {
                    "policy": profile.policy,
                    "tiers": {
                        tier.name: {"checks": list(tier.checks)}
                        for tier in profile.tiers
                    },
                    "extra_checks": list(profile.extra_checks),
                }
                for profile in cfg.readiness.vehicles
            },
        },
    }
