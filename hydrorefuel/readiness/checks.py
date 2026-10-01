"""分级就绪判据目录。

档位与阈值在哪配是定死的：档位判据按站点统一配置，车型只能引用判据 ID
并选择全拒/降级策略，不允许车型自带阈值。这里是判据 ID 到现场读法的唯一
映射；评估器只按配置里的 ID 调用，保证控制台展示与审计落的是同一份读法。

每条判据返回三态而不是布尔：

``ok``       判据满足，可以继续；
``fail``     数据齐全但条件不满足，原因写明是哪一条；
``missing``  判据依赖的数据缺失或读取异常，属于部分失败，档位一律按未就绪。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..buffer.bank import BufferBank
from ..buffer.baseline import BufferBaseline
from ..clock import Clock
from ..cool.precool import Precooler
from ..cool.temperature import TemperatureModel
from ..disp.machine import Dispenser
from ..disp.nozzle import Nozzle
from ..flow.meter import MassFlowMeter
from ..leak.latch import LeakLatch
from ..queue.confirm import Confirmation
from ..queue.queue import FillQueue
from ..telemetry import TrendBuffer

OK = "ok"
FAIL = "fail"
MISSING = "missing"


@dataclass(frozen=True)
class CheckOutcome:
    """一条判据的本次结果与原因。"""

    check: str
    status: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"check": self.check, "status": self.status, "reason": self.reason}


def passed(check: str) -> CheckOutcome:
    """判据满足。"""

    return CheckOutcome(check, OK)


def blocked(check: str, reason: str) -> CheckOutcome:
    """数据齐全但条件不满足，带上现场原因。"""

    return CheckOutcome(check, FAIL, reason)


def absent(check: str, reason: str) -> CheckOutcome:
    """依赖数据缺失或读取异常，按部分失败处理。"""

    return CheckOutcome(check, MISSING, reason)


@dataclass
class CheckContext:
    """判据读取现场所需的组件集合；判据本身不持有运行态。"""

    running: bool
    clock: Clock
    nozzle: Nozzle
    dispenser: Dispenser
    precooler: Precooler
    temperature: TemperatureModel
    leak_latch: LeakLatch
    confirm: Confirmation
    queue: FillQueue
    bank: BufferBank
    baseline: BufferBaseline
    meter: MassFlowMeter
    trend: TrendBuffer
    vehicle: str
    max_line_pressure_mpa: float
    pressure_signal_max_age_s: float = 30.0


Check = Callable[[CheckContext], CheckOutcome]


def _platform_running(ctx: CheckContext) -> CheckOutcome:
    return passed("platform_running") if ctx.running else blocked("platform_running", "平台未启动")


def _leak_clear(ctx: CheckContext) -> CheckOutcome:
    try:
        engaged = ctx.leak_latch.is_engaged(ctx.clock.now())
        remaining = ctx.leak_latch.remaining_s(ctx.clock.now())
    except Exception as error:  # 判据读不到现场就是部分失败，不能当成条件不满足
        return absent("leak_clear", f"泄漏闩锁状态读取失败：{error}")
    if engaged:
        return blocked("leak_clear", f"泄漏闩锁保持中，剩余 {round(remaining, 1)} 秒")
    return passed("leak_clear")


def _precool_ready(ctx: CheckContext) -> CheckOutcome:
    try:
        ready = ctx.precooler.is_ready()
    except Exception as error:
        return absent("precool_ready", f"预冷状态读取失败：{error}")
    if not ready:
        return blocked("precool_ready", f"预冷未保持够时长，当前保持 {round(ctx.precooler.held_s, 1)} 秒")
    return passed("precool_ready")


def _temperature_in_band(ctx: CheckContext) -> CheckOutcome:
    try:
        value = ctx.temperature.value_c
        reached = ctx.temperature.reached()
    except Exception as error:
        return absent("temperature_in_band", f"枪口温度读取失败：{error}")
    if not reached:
        return blocked("temperature_in_band", f"枪口温度 {round(value, 2)} ℃ 未进入加注允许带")
    return passed("temperature_in_band")


def _nozzle_latched(ctx: CheckContext) -> CheckOutcome:
    try:
        seated = ctx.nozzle.is_seated()
        latched = ctx.nozzle.is_latched()
    except Exception as error:
        return absent("nozzle_latched", f"加注枪状态读取失败：{error}")
    if not seated:
        return blocked("nozzle_latched", "加注枪未落座")
    if not latched:
        return blocked("nozzle_latched", "加注枪已落座但闩锁未咬合")
    return passed("nozzle_latched")


def _nozzle_unseated(ctx: CheckContext) -> CheckOutcome:
    try:
        seated = ctx.nozzle.is_seated()
    except Exception as error:
        return absent("nozzle_unseated", f"加注枪状态读取失败：{error}")
    if seated:
        return blocked("nozzle_unseated", "交接前加注枪仍处于落座状态，必须先回枪")
    return passed("nozzle_unseated")


def _confirm_idle(ctx: CheckContext) -> CheckOutcome:
    try:
        pending = ctx.confirm.pending(ctx.vehicle)
        outstanding = ctx.confirm.outstanding()
    except Exception as error:
        return absent("confirm_idle", f"加注确认状态读取失败：{error}")
    if pending:
        return blocked("confirm_idle", f"车辆 {ctx.vehicle} 仍有未完成的加注确认")
    if outstanding:
        return blocked("confirm_idle", f"存在未完成确认的车辆：{', '.join(outstanding)}")
    return passed("confirm_idle")


def _buffer_in_band(ctx: CheckContext) -> CheckOutcome:
    try:
        within = ctx.bank.within_band()
        pressure = ctx.bank.pressure_mpa
    except Exception as error:
        return absent("buffer_in_band", f"瓶组压力读取失败：{error}")
    if not within:
        return blocked("buffer_in_band", f"瓶组压力 {round(pressure, 2)} MPa 偏离目标带")
    return passed("buffer_in_band")


def _baseline_fresh(ctx: CheckContext) -> CheckOutcome:
    try:
        now = ctx.clock.now()
        age = ctx.baseline.age_s(now)
        expired = ctx.baseline.expired(now)
    except Exception as error:
        return absent("baseline_fresh", f"压力基线读取失败：{error}")
    if expired:
        return blocked("baseline_fresh", f"瓶组压力基线已过期，基线龄 {round(age, 1)} 秒")
    return passed("baseline_fresh")


def _queue_has_room(ctx: CheckContext) -> CheckOutcome:
    try:
        size = ctx.queue.size()
        capacity = ctx.queue.limited.max_waiting
    except Exception as error:
        return absent("queue_has_room", f"加注队列读取失败：{error}")
    if size >= capacity:
        return blocked("queue_has_room", f"加注队列已满（{size}/{capacity}）")
    return passed("queue_has_room")


def _fill_valve_closed(ctx: CheckContext) -> CheckOutcome:
    try:
        open_valve = ctx.dispenser.fill_valve_open()
    except Exception as error:
        return absent("fill_valve_closed", f"加注阀状态读取失败：{error}")
    if open_valve:
        return blocked("fill_valve_closed", "加注阀仍开启，交接前必须先隔离")
    return passed("fill_valve_closed")


def _line_pressure_safe(ctx: CheckContext) -> CheckOutcome:
    try:
        pressure = ctx.bank.pressure_mpa
        safe = pressure <= ctx.max_line_pressure_mpa
    except Exception as error:
        return absent("line_pressure_safe", f"管路压力读取失败：{error}")
    if not safe:
        return blocked(
            "line_pressure_safe",
            f"加注管压力 {round(pressure, 2)} MPa 超过上限 {ctx.max_line_pressure_mpa} MPa",
        )
    return passed("line_pressure_safe")


def _meter_has_reading(ctx: CheckContext) -> CheckOutcome:
    try:
        total = ctx.meter.total_kg()
    except Exception as error:
        return absent("meter_has_reading", f"质量流量计读取失败：{error}")
    if total <= 0.0:
        return blocked("meter_has_reading", "质量流量计累计为 0，本次加注没有计量读数，不能交接")
    return passed("meter_has_reading")


def _buffer_pressure_fresh(ctx: CheckContext) -> CheckOutcome:
    """压力采样是否新鲜：趋势缓冲里没有近期样本即判数据缺失（部分失败）。"""

    try:
        sample = ctx.trend.latest("buffer_pressure")
        now = ctx.clock.now()
    except Exception as error:
        return absent("buffer_pressure_fresh", f"瓶组压力趋势读取失败：{error}")
    if sample is None:
        return absent("buffer_pressure_fresh", "瓶组压力尚无采样点，无法确认趋势新鲜度")
    age = now - sample.ts
    if age > ctx.pressure_signal_max_age_s:
        return blocked(
            "buffer_pressure_fresh",
            f"瓶组压力采样过期，最近采样距今 {round(age, 1)} 秒",
        )
    return passed("buffer_pressure_fresh")


_CHECKS: dict[str, Check] = {
    "platform_running": _platform_running,
    "leak_clear": _leak_clear,
    "precool_ready": _precool_ready,
    "temperature_in_band": _temperature_in_band,
    "nozzle_latched": _nozzle_latched,
    "nozzle_unseated": _nozzle_unseated,
    "confirm_idle": _confirm_idle,
    "buffer_in_band": _buffer_in_band,
    "baseline_fresh": _baseline_fresh,
    "queue_has_room": _queue_has_room,
    "fill_valve_closed": _fill_valve_closed,
    "line_pressure_safe": _line_pressure_safe,
    "meter_has_reading": _meter_has_reading,
    "buffer_pressure_fresh": _buffer_pressure_fresh,
}


def known_checks() -> tuple[str, ...]:
    """返回目录内的全部判据 ID。"""

    return tuple(_CHECKS)


def run_check(check_id: str, ctx: CheckContext) -> CheckOutcome:
    """按 ID 执行一条判据；未登记的判据属于装配期就该拦住的接线问题。"""

    handler = _CHECKS.get(check_id)
    if handler is None:
        raise KeyError(f"未登记的就绪判据：{check_id}")
    return handler(ctx)
