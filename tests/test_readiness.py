"""分级就绪：分档评估、按车型失败策略、数据缺失与审计一致性。"""

from __future__ import annotations

import json
import urllib.request

import pytest

from hydrorefuel.config import (
    ReadinessLimits,
    ReadinessPolicy,
    StationConfig,
    config_to_mapping,
    default_config,
)
from hydrorefuel.console.api import Api
from hydrorefuel.console.server import ConsoleServer
from hydrorefuel.errors import ConfigError

LEGACY_CHECK_KEYS = (
    "running",
    "precool_ready",
    "temperature_in_band",
    "nozzle_latched",
    "leak_latch_clear",
    "confirm_idle",
)


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _tier(result: dict, name: str) -> dict:
    return next(tier for tier in result["tiers"] if tier["name"] == name)


def _make_valve_ready(platform) -> None:
    """把平台推进到开阀就绪：枪落座、预冷保持达标。"""

    platform.nozzle.seat(platform.config.dispenser.nozzle_seat_travel_mm)
    platform.precooler.start()
    platform.temperature.cool(200.0)
    platform.precooler.tick(platform.config.cool.precool_hold_s, platform.temperature.value_c)


def test_tiers_list_failing_checks_with_reasons(platform):
    platform.start()
    result = platform.tiered_readiness()
    assert [tier["name"] for tier in result["tiers"]] == ["accept", "valve", "handover"]
    valve = _tier(result, "valve")
    assert valve["ok"] is False
    assert set(valve["failed"]) == {"precool_ready", "temperature_in_band", "nozzle_latched"}
    for check in valve["checks"]:
        if check["ok"]:
            assert check["reason"] == ""
        else:
            assert check["reason"]
            assert check["data_missing"] is False
    # 不满足时默认车型给出可做的档位，而不是全拒。
    assert result["ready"] is False
    assert result["decision"] == "degraded"
    assert result["allowed_tiers"] == ["accept", "handover"]
    assert result["failed_tiers"] == ["valve"]


def test_strict_vehicle_type_rejects_everything(platform):
    platform.start()
    result = platform.tiered_readiness(vehicle_type="heavy_truck")
    assert result["policy"] == "strict"
    assert result["ready"] is False
    assert result["decision"] == "rejected"
    assert result["allowed_tiers"] == []
    # 全拒时各档不满足项仍然列得出。
    assert _tier(result, "valve")["failed"]


def test_all_tiers_ready(platform):
    platform.start()
    _make_valve_ready(platform)
    result = platform.tiered_readiness()
    assert result["ready"] is True
    assert result["decision"] == "ready"
    assert result["allowed_tiers"] == ["accept", "valve", "handover"]
    assert result["failed_tiers"] == []


def test_handover_blocked_while_filling(platform):
    platform.start()
    _make_valve_ready(platform)
    platform.dispenser.open_fill_valve()
    result = platform.tiered_readiness()
    handover = _tier(result, "handover")
    assert handover["ok"] is False
    assert handover["failed"] == ["dispenser_idle"]
    assert result["allowed_tiers"] == ["accept", "valve"]


def test_missing_data_counts_as_not_ready(platform):
    platform.start()
    platform.precooler = None  # 模拟预冷数据源丢失
    result = platform.tiered_readiness()
    valve = _tier(result, "valve")
    assert valve["ok"] is False
    assert valve["data_missing"] is True
    check = next(item for item in valve["checks"] if item["name"] == "precool_ready")
    assert check["ok"] is False
    assert check["data_missing"] is True
    assert "数据缺失" in check["reason"]
    # 部分失败按未就绪处理：总就绪为否，该档不进可做清单。
    assert result["ready"] is False
    assert result["data_missing"] is True
    assert "valve" not in result["allowed_tiers"]
    strict = platform.tiered_readiness(vehicle_type="heavy_truck")
    assert strict["decision"] == "rejected"
    assert strict["allowed_tiers"] == []


def test_last_event_carries_most_recent_reason(platform):
    platform.start()
    platform.sample_leak(4500.0)
    result = platform.tiered_readiness()
    valve = _tier(result, "valve")
    check = next(item for item in valve["checks"] if item["name"] == "leak_latch_clear")
    assert check["ok"] is False
    assert check["last_event"]["action"] == "sample"
    assert check["last_event"]["severity"] == "alarm"


def test_tiered_readiness_audit_matches_console_result(platform):
    platform.start()
    result = platform.tiered_readiness()
    entries = [entry for entry in platform.audit.entries() if entry["action"] == "tiered_readiness"]
    assert entries
    latest = entries[0]
    assert latest["component"] == "readiness"
    assert latest["severity"] == "warning"
    assert latest["payload"]["decision"] == result["decision"]
    assert latest["payload"]["failed_tiers"] == result["failed_tiers"]
    assert latest["payload"]["tiers"] == result["tiers"]

    _make_valve_ready(platform)
    ready_result = platform.tiered_readiness()
    latest = [entry for entry in platform.audit.entries() if entry["action"] == "tiered_readiness"][0]
    assert ready_result["ready"] is True
    assert latest["severity"] == "notice"
    assert latest["payload"]["ready"] is True


def test_legacy_readiness_shape_and_values_unchanged(platform):
    platform.start()
    legacy = platform.readiness()
    assert set(legacy) == {"ready", "checks", "stage", "outstanding_confirms", "last_audit_ts"}
    assert tuple(legacy["checks"]) == LEGACY_CHECK_KEYS
    assert legacy["ready"] is False
    # 旧接口取值与分级评估同源，同一个判据两侧结论一致。
    tiered = platform.tiered_readiness()
    evaluated = {check["name"]: check["ok"] for tier in tiered["tiers"] for check in tier["checks"]}
    assert legacy["checks"] == {name: evaluated[name] for name in LEGACY_CHECK_KEYS}

    _make_valve_ready(platform)
    assert platform.readiness()["ready"] is True


def test_console_api_serves_tiered_readiness(platform):
    platform.start()
    api = Api(platform)
    status, payload = api.handle("GET", "/api/readiness/tiers", {}, {})
    assert status == 200
    assert payload["policy"] == "degraded"
    status, payload = api.handle("GET", "/api/readiness/tiers", {"vehicle_type": "heavy_truck"}, {})
    assert status == 200
    assert payload["vehicle_type"] == "heavy_truck"
    assert payload["policy"] == "strict"
    assert payload["decision"] == "rejected"
    # 旧接口保持可用，返回结构不变。
    status, legacy = api.handle("GET", "/api/readiness", {}, {})
    assert status == 200
    assert tuple(legacy["checks"]) == LEGACY_CHECK_KEYS


def test_console_http_serves_tiered_readiness(platform):
    platform.start()
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        status, payload = _get(f"{base}/api/readiness/tiers?vehicle_type=heavy_truck")
        assert status == 200
        assert payload["policy"] == "strict"
        assert [tier["name"] for tier in payload["tiers"]] == ["accept", "valve", "handover"]
        status, legacy = _get(f"{base}/api/readiness")
        assert status == 200
        assert "ready" in legacy and "checks" in legacy
    finally:
        server.stop()


def test_readiness_config_requires_default_policy():
    with pytest.raises(ConfigError):
        ReadinessLimits.from_mapping({"vehicle_types": {}})


def test_readiness_config_rejects_unknown_mode():
    with pytest.raises(ConfigError):
        ReadinessPolicy.from_mapping({"on_failure": "sometimes"})


def test_station_config_requires_readiness_section():
    raw = config_to_mapping(default_config())
    del raw["limits"]["readiness"]
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)


def test_policy_resolution_falls_back_to_default():
    limits = default_config().readiness
    assert limits.policy_for("heavy_truck").on_failure == "strict"
    assert limits.policy_for("sedan").on_failure == "degraded"
    assert limits.policy_for(None).on_failure == "degraded"
