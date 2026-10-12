"""Coherent drive cycle that animates the values the display shows.

A 42 s lap-like loop (launch, cruise, regen braking, slalom, hard stop) drives
a small vehicle model: speed -> wheel rpm -> torque (limited by the SOP the
bench itself publishes) -> pack power -> voltage, current and SOC, plus slowly
heating motors, fans that follow the hottest part, and tyres that warm with
speed and steering.  It is a plausibility model for exercising the display,
not a vehicle simulation: constants are rounded demo values.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from .model import WHEELS

PERIOD_S = 42.0
TORQUE_MAX_NM = 21.0
MOTOR_POWER_CAP_KW = 25.0
PACK_POWER_CAP_KW = 80.0
SPEED_MAX_MPS = 30.0
ACCEL_DRIVE = 8.0
ACCEL_BRAKE = 12.0
RPM_PER_MPS = 573.0          # wheel radius ~0.2 m, gear ratio ~12
CELL_COUNT = 138
PACK_RESISTANCE_OHM = 0.12
PACK_CAPACITY_AH = 13.5
DRIVE_EFFICIENCY = 0.93
AUX_LOAD_KW = 0.4
REGEN_FULL_AT_BRAKE = 0.6
STEERING_FULL_DEG = 540.0

_HEAT = {"motor_c": (2.0, 0.012), "inverter_c": (1.0, 0.015), "igbt_c": (1.3, 0.015)}
_TIRE_GRADIENT = (-1.5, -0.5, 0.5, 1.5)
_LEFT = {"fl", "rl"}


def _ramp(value: float) -> float:
    return max(0.0, min(1.0, value))


def profile(local_s: float) -> tuple[float, float, float, str]:
    """Pedal and steering demand at ``local_s`` seconds into the loop."""
    if local_s < 3:
        return 0.0, 0.0, 0.0, "停车待机"
    if local_s < 10:
        return _ramp(local_s - 3), 0.0, 0.0, "起步加速"
    if local_s < 16:
        return 0.30, 0.0, 0.0, "巡航"
    if local_s < 19:
        return 0.0, 0.6 * _ramp(local_s - 16), 0.0, "制动回收"
    if local_s < 22:
        return 0.0, 0.0, 0.0, "滑行"
    if local_s < 30:
        return 0.7 * _ramp(local_s - 22), 0.0, 90.0 * math.sin((local_s - 22) * math.pi / 2), "绕桩加速"
    if local_s < 35:
        return 0.25, 0.0, 0.0, "巡航"
    if local_s < 40:
        return 0.0, _ramp((local_s - 35) / 0.8), 0.0, "急刹"
    return 0.0, 0.0, 0.0, "停车待机"


class DriveCycle:
    """Stateful generator; call :meth:`step` at a fixed tick with the current values."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        soc = float(values["bms.soc_pct"])
        self.soc = soc if 0 <= soc <= 100 else 76.0
        self.energy_wh = float(values["pdm.energy_wh"])
        self.t = 0.0
        self.speed_mps = 0.0
        self.phase = "停车待机"
        self.temps = {
            (wheel, field): float(values[f"wheel.{wheel}.{field}"])
            for wheel in WHEELS for field in _HEAT}
        self.tires = {
            (wheel, point): float(values[f"tire.{wheel}.t{point + 1}"])
            for wheel in WHEELS for point in range(4)}

    @property
    def speed_kmh(self) -> float:
        return self.speed_mps * 3.6

    def step(self, dt: float, values: Mapping[str, Any]) -> dict[str, Any]:
        self.t += dt
        throttle, brake, steer, self.phase = profile(self.t % PERIOD_S)
        moving = self.speed_mps > 0.05
        accel = (ACCEL_DRIVE * throttle * max(0.0, 1.0 - self.speed_mps / SPEED_MAX_MPS)
                 - (ACCEL_BRAKE * brake + 0.15 + 0.002 * self.speed_mps ** 2 if moving else 0.0))
        self.speed_mps = max(0.0, self.speed_mps + accel * dt)

        base_rpm = self.speed_mps * RPM_PER_MPS
        differential = steer / STEERING_FULL_DEG * 0.10
        rpm = {wheel: base_rpm * (1 + (differential if wheel in _LEFT else -differential))
               for wheel in WHEELS}
        enabled = {wheel: bool(values[f"wheel.{wheel}.enabled"]) for wheel in WHEELS}
        sop_discharge_kw = values["sop.discharge_kw"] if values["sop.limits_valid"] else 0.0
        sop_regen_kw = values["sop.regen_kw"] if values["sop.limits_valid"] else 0.0

        torque = {wheel: 0.0 for wheel in WHEELS}
        for wheel in WHEELS:
            if enabled[wheel] and throttle > 0:
                demand = throttle * TORQUE_MAX_NM
                if rpm[wheel] > 1:
                    demand = min(demand, MOTOR_POWER_CAP_KW * 9549.0 / rpm[wheel])
                torque[wheel] = demand
        requested_kw = sum(torque[wheel] * rpm[wheel] for wheel in WHEELS) / 9549.0
        allowed_kw = min(PACK_POWER_CAP_KW, sop_discharge_kw) * DRIVE_EFFICIENCY
        if requested_kw > allowed_kw:
            scale = allowed_kw / requested_kw if requested_kw > 0 else 0.0
            torque = {wheel: value * scale for wheel, value in torque.items()}
        if brake > 0 and self.speed_mps > 1.5 and sop_regen_kw > 0:
            regen_kw = sop_regen_kw * min(1.0, brake / REGEN_FULL_AT_BRAKE)
            for wheel in WHEELS:
                if enabled[wheel] and rpm[wheel] > 300:
                    torque[wheel] = -min(12.0, regen_kw / 4.0 * 9549.0 / rpm[wheel])

        mechanical_kw = sum(torque[wheel] * rpm[wheel] for wheel in WHEELS) / 9549.0
        electrical_kw = (mechanical_kw / DRIVE_EFFICIENCY if mechanical_kw > 0
                         else mechanical_kw * DRIVE_EFFICIENCY) + AUX_LOAD_KW
        open_circuit_v = CELL_COUNT * (3.3 + 0.009 * self.soc)
        discriminant = max(0.0, open_circuit_v ** 2 - 4 * PACK_RESISTANCE_OHM * electrical_kw * 1000.0)
        discharge_a = (open_circuit_v - math.sqrt(discriminant)) / (2 * PACK_RESISTANCE_OHM)
        pack_v = open_circuit_v - PACK_RESISTANCE_OHM * discharge_a
        self.soc = max(0.0, min(100.0, self.soc - discharge_a * dt / 3600.0 / PACK_CAPACITY_AH * 100.0))

        updates: dict[str, Any] = {
            "pedal.throttle_pct": round(throttle * 100.0, 1),
            "pedal.oil_kpa": round(1.0 + brake * 4.4, 3),
            "pedal.steering_deg": round(steer, 1),
            "bms.voltage_v": round(pack_v, 1),
            "bms.current_a": round(-discharge_a, 1),
            "bms.soc_pct": int(round(self.soc)),
        }

        for wheel in WHEELS:
            load = (torque[wheel] / TORQUE_MAX_NM) ** 2
            updates[f"wheel.{wheel}.torque_nm"] = int(round(torque[wheel]))
            updates[f"wheel.{wheel}.rpm"] = int(round(rpm[wheel]))
            for field, (heat, cool) in _HEAT.items():
                temperature = self.temps[(wheel, field)]
                temperature += (heat * load - cool * (temperature - 30.0)) * dt
                self.temps[(wheel, field)] = temperature
                updates[f"wheel.{wheel}.{field}"] = round(temperature, 1)
        hottest = max(self.temps.values())

        derate_soc = 1.0 if self.soc >= 25 else self.soc / 25.0
        derate_temp = 1.0 if hottest <= 75 else max(0.0, (95.0 - hottest) / 20.0)
        discharge_limit_kw = round(PACK_POWER_CAP_KW * min(derate_soc, derate_temp), 1)
        regen_limit_kw = 8.0 if self.soc < 97 else 0.0
        updates.update({
            "sop.discharge_kw": discharge_limit_kw,
            "sop.regen_kw": regen_limit_kw,
            "sop.discharge_a": round(min(180.0, discharge_limit_kw * 1000.0 / max(pack_v, 1.0)), 1),
            "sop.regen_a": round(min(8.0, regen_limit_kw * 1000.0 / max(pack_v, 1.0)), 1),
        })

        duty1 = int(max(20, min(100, round(22 + (hottest - 35) * 1.8))))
        duty2 = max(20, duty1 - 4)
        updates.update({"fan.duty1": duty1, "fan.duty2": duty2, "fan.rpm1": duty1 * 60,
                        "fan.rpm2": duty2 * 62, "fan.rpm3": duty1 * 50})

        lv_v = 24.2 + 0.3 * math.sin(self.t * 0.9)
        lv_a = 6.5 + duty1 / 100.0 * 3.0 + 0.8 * math.sin(self.t * 0.5) + (0.5 if brake > 0 else 0.0)
        self.energy_wh = (self.energy_wh + lv_v * lv_a * dt / 3600.0) % 655.36
        updates.update({"pdm.bus_v": round(lv_v, 3), "pdm.bus_a": round(lv_a, 2),
                        "pdm.energy_wh": round(self.energy_wh, 2)})

        lateral = abs(steer) / 90.0
        for wheel in WHEELS:
            outer = (wheel in _LEFT) == (steer > 0)
            target = 30.0 + self.speed_kmh * 0.25 + (6.0 * lateral if outer else 1.5 * lateral)
            for point in range(4):
                current = self.tires[(wheel, point)]
                current += (target + _TIRE_GRADIENT[point] - current) * min(1.0, dt / 15.0)
                self.tires[(wheel, point)] = current
                updates[f"tire.{wheel}.t{point + 1}"] = round(current, 2)
        return updates
