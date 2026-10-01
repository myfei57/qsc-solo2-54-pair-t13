"""分级就绪：分档判据、车型策略、部分失败与审计一致性。"""

from __future__ import annotations

import pytest

from hydrorefuel.config import (
    READINESS_CHECKS,
    READINESS_TIERS,
    StationConfig,
    config_to_mapping,
    default_config,
)
from hydrorefuel.errors import ConfigError
from hydrorefuel.readiness.checks import MISSING, known_checks


def _tiers(payload: dict) -> dict:
    return {tier["name"]: tier for tier in payload["tiers"]}


def test_readiness_keeps_legacy_contract(platform):
    report = platform.readiness()
    for key in ("ready", "checks", "stage", "outstanding_confirms", "last_audit_ts"):
        assert key in report
    assert set(report["checks"]) == {
        "running",
        "precool_ready",
        "temperature_in_band",
        "nozzle_latched",
        "leak_latch_clear",
        "confirm_idle",
    }
    assert report["ready"] is False
    assert report["tier_order"] == list(READINESS_TIERS)


def test_tier_readiness_lists_each_unmet_check_with_reason(platform):
    platform.start()
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    platform.pressurize(inlet_pressure_mpa=12.0, flow_kg_per_min=4.0, charge_kg=12.0)
    platform.cycle(dt=1.0)
    report = platform.readiness_tiers()
    tiers = _tiers(report)
    assert tiers["receive"]["ready"]
    assert not tiers["valve_open"]["ready"]
    assert "nozzle_latched" in tiers["valve_open"]["unsatisfied"]
    nozzle = next(c for c in tiers["valve_open"]["checks"] if c["check"] == "nozzle_latched")
    assert nozzle["status"] == "fail"
    assert nozzle["last_fail_reason"]
    assert nozzle["last_fail_ts"] is not None


def test_reject_all_gate_denies_without_alternative(platform):
    platform.start()
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    platform.pressurize(inlet_pressure_mpa=12.0, flow_kg_per_min=4.0, charge_kg=12.0)
    decision = platform.readiness_gate("valve_open", purpose="fill", vehicle_type="default")
    assert decision["gate"]["accepted"] is False
    assert decision["gate"]["allowed_tiers"] == []
    assert decision["gate"]["highest_tier"] is None
    assert decision["gate"]["degrade_offered"] is False


def test_degrade_policy_offers_available_tier(platform):
    platform.start()
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    platform.pressurize(inlet_pressure_mpa=12.0, flow_kg_per_min=4.0, charge_kg=12.0)
    platform.cycle(dt=1.0)
    decision = platform.readiness_gate("valve_open", purpose="fill", vehicle_type="citybus")
    assert decision["gate"]["accepted"] is False
    assert decision["gate"]["degrade_offered"] is True
    assert "receive" in decision["gate"]["allowed_tiers"]
    assert decision["gate"]["highest_tier"] == "receive"


def test_unknown_vehicle_falls_back_to_default_profile(platform):
    platform.start()
    report = platform.readiness_tiers(vehicle_type="unknown-trailer")
    assert report["profile_found"] is False
    assert report["policy"] == "reject_all"


def test_missing_data_is_partial_failure_and_blocks_even_on_degrade(platform):
    platform.start()
    # 未跑过控制周期：压力趋势没有采样，citybus 的额外判据缺数据。
    report = platform.readiness_tiers(vehicle_type="citybus")
    assert report["partial_failure"] is True
    extra = {c["check"]: c for c in report["extra_checks"]}
    assert extra["buffer_pressure_fresh"]["status"] == MISSING
    decision = platform.readiness_gate("receive", purpose="fill", vehicle_type="citybus")
    assert decision["partial_failure"] is True
    assert decision["gate"]["accepted"] is False
    assert "receive" not in decision["gate"]["allowed_tiers"]


def test_handoff_tier_ready_after_full_sequence(platform):
    platform.start()
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    platform.pressurize(inlet_pressure_mpa=12.0, flow_kg_per_min=4.0, charge_kg=12.0)
    platform.fill(vehicle="vehicle01", rate_kg_per_min=3.0, steps=2)
    platform.stop_fill()
    decision = platform.readiness_gate("handoff", purpose="handoff", vehicle_type="default")
    assert decision["gate"]["accepted"], decision["gate"]["reason"]


def test_readiness_decision_and_audit_use_same_result(platform):
    platform.start()
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    platform.pressurize(inlet_pressure_mpa=12.0, flow_kg_per_min=4.0, charge_kg=12.0)
    decision = platform.readiness_gate("receive", purpose="fill", vehicle_type="default")
    assert decision["gate"]["accepted"], decision["gate"]["reason"]
    audit = next(e for e in platform.audit.entries() if e["action"] == "gate_fill")
    assert audit["payload"]["gate"] == decision["gate"]
    assert audit["payload"]["evaluation"]["tiers"]
    assert audit["severity"] == "notice"

    blocked = platform.readiness_gate("valve_open", purpose="fill", vehicle_type="default")
    deny = next(
        e
        for e in platform.audit.entries()
        if e["action"] == "gate_fill" and e["payload"]["gate"]["accepted"] is False
    )
    assert deny["severity"] == "alarm"
    assert deny["payload"]["gate"]["reason"] == blocked["gate"]["reason"]


def test_explicit_readiness_is_audited_but_snapshot_is_silent(platform):
    platform.readiness()
    platform.readiness_tiers()
    assert any(e["action"] == "readiness_overview" for e in platform.audit.entries())
    assert any(e["action"] == "readiness_tiers" for e in platform.audit.entries())
    total = platform.audit.summary()["total"]
    platform.snapshot()
    assert platform.audit.summary()["total"] == total


def test_partial_failure_is_warning_severity(platform):
    platform.start()
    platform.readiness_tiers(vehicle_type="citybus")  # 无压力采样 -> 缺数据
    event = next(e for e in platform.audit.entries() if e["action"] == "readiness_tiers")
    assert event["severity"] == "warning"


def test_bad_readiness_config_is_rejected_at_assembly():
    base = config_to_mapping(default_config())
    cases = [
        {"readiness": {"tiers": {"ghost": {"checks": ["leak_clear"]}}}},
        {"readiness": {"tiers": {
            "receive": {"checks": ["leak_clear"]},
            "valve_open": {"checks": ["no_such_check"]},
            "handoff": {"checks": ["leak_clear"]},
        }}},
        {"readiness": {"vehicles": {"default": {"policy": "banana"}}}},
    ]
    for override in cases:
        with pytest.raises(ConfigError):
            StationConfig.from_mapping({**base, **override})


def test_readiness_section_round_trips_through_mapping():
    cfg = default_config()
    parsed = StationConfig.from_mapping(config_to_mapping(cfg))
    assert parsed == cfg


def test_check_catalog_matches_config_constants():
    # 配置常量是站点判据的唯一目录，实现必须与之一一对齐，防止漂移。
    assert set(known_checks()) == set(READINESS_CHECKS)
