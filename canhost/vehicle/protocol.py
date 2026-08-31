"""CANB vehicle-side protocol state.

One instance represents the dedicated vehicle CANB connection.  It decodes
every node the team monitors on the car: the BMS vehicle mirror (0x4B0/0x4B1),
SOP trio (0x4A0/0x4A3/0x4A4), competition energy meter
(0x521/0x522/0x526/0x528), PDM low-voltage telemetry (0x5A0/0x5A1),
FanController (0x5A2..0x5A7, including command acknowledgement tracking),
ECU motor debug frames (0x502/0x505..0x509) and tyre temperatures
(0x071..0x074).  Frame layouts come from canhost.decoders.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import time
from collections.abc import Callable
from typing import Any

from ..decoders import (ECU_WHEEL_NAMES, METER_IDS, CanFrame, age, canb_frame_name,
                        decode_ecu_sop_ack, decode_ecu_status, decode_ecu_wheels_i16,
                        decode_fan_ack, decode_fan_curve, decode_fan_diagnostic,
                        decode_fan_failsafe, decode_fan_status, decode_fan_power_status,
                        decode_fan_calib_status, decode_fault_fields,
                        decode_meter_result, decode_pack_status,
                        decode_pdm_side, decode_sop_limits, decode_sop_status,
                        decode_tire_temp_frame, format_raw_frame,
                        FAN_COMMAND_ACK_ID, FAN_CURVE_STATUS_ID, FAN_DIAGNOSTIC_ID,
                        FAN_FAILSAFE_STATUS_ID, FAN_STATUS_ID, FAN_POWER_STATUS_ID,
                        FAN_CALIB_STATUS_ID, PDM_BATTERY_ID, PDM_BUS_ID,
                        ALARM_LEVEL_NAMES, STATE_NAMES, TIRE_TEMP_IDS)


# Frames arriving slower than this are treated as timed out by the UI layer;
# the backend always reports the raw age and lets the frontend decide.
class VehicleProtocol:
    """Stateful decoder for the vehicle CANB channel."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.started_monotonic = self.clock()
        self.pack: dict[str, Any] = {}
        self.last_pack_monotonic: float | None = None
        self.fault: dict[str, Any] = {}
        self.last_fault_monotonic: float | None = None
        self.sop: dict[str, Any] = {"limits": {}, "status": {}, "ecu_ack": {}}
        self._sop_limits_data: bytes | None = None
        self.last_sop_limits_monotonic: float | None = None
        self.last_sop_status_monotonic: float | None = None
        self.last_sop_ack_monotonic: float | None = None
        self.meter: dict[str, dict[str, Any]] = {
            "current_a": {"value": None, "status": None, "counter": None},
            "u1_v": {"value": None, "status": None, "counter": None},
            "power_w": {"value": None, "status": None, "counter": None},
            "energy_wh": {"value": None, "status": None, "counter": None}}
        self.meter_seen: dict[str, float | None] = {key: None for key in self.meter}
        self.pdm: dict[str, dict[str, Any]] = {
            "bus": {"voltage_v": None, "current_a": None, "power_w": None,
                    "energy_wh": None, "offline": True},
            "battery": {"voltage_v": None, "current_a": None, "power_w": None,
                        "energy_wh": None, "offline": True}}
        self.pdm_seen: dict[str, float | None] = {"bus": None, "battery": None}
        self.fan: dict[str, Any] = {
            "status": {}, "diagnostic": {}, "curve": {}, "failsafe": {},
            "power_status": {}, "calib_status": {},
        }
        self.fan_acks: dict[int, dict[str, Any]] = {}
        self.fan_ack_history: deque[dict[str, Any]] = deque(maxlen=40)
        self.last_fan_status_monotonic: float | None = None
        self.last_fan_diagnostic_monotonic: float | None = None
        self.last_fan_curve_monotonic: float | None = None
        self.last_fan_failsafe_monotonic: float | None = None
        self.last_fan_power_monotonic: float | None = None
        self.last_fan_calib_monotonic: float | None = None
        self.ecu: dict[str, Any] = {
            "torque_pct": [None] * 4, "velocity_rpm": [None] * 4,
            "motor_temp_c": [None] * 4, "inverter_temp_c": [None] * 4,
            "igbt_temp_c": [None] * 4, "status": {},
        }
        self.last_ecu_monotonic: dict[str, float | None] = {
            "torque": None, "velocity": None, "motor_temp": None,
            "inverter_temp": None, "igbt_temp": None, "status": None}
        self.tires: dict[str, list[float | None] | None] = {f"0x{can_id:03X}": None for can_id in TIRE_TEMP_IDS}
        self.last_tire_monotonic: float | None = None
        self.raw_frames: deque[dict[str, Any]] = deque(maxlen=320)
        self.rx_count = 0
        self.tx_count = 0
        self.last_rx_monotonic: float | None = None
        self.trends: deque[dict[str, Any]] = deque(maxlen=240)
        self._last_trend = 0.0

    # -- ingest ------------------------------------------------------------

    def ingest(self, frame: CanFrame) -> None:
        now_mono = self.clock()
        if frame.direction == "tx":
            self.tx_count += 1
        else:
            self.rx_count += 1
            self.last_rx_monotonic = now_mono

        self.raw_frames.appendleft(format_raw_frame(frame, canb_frame_name(frame.arbitration_id)))
        if frame.direction == "tx":
            return

        data = frame.data
        can_id = frame.arbitration_id
        if frame.is_extended_id:
            return
        if can_id == 0x4B0 and len(data) >= 7:
            self.pack = decode_pack_status(data)
            self.last_pack_monotonic = now_mono
        elif can_id == 0x4B1 and len(data) >= 8:
            self.fault = decode_fault_fields(data)
            self.last_fault_monotonic = now_mono
        elif can_id == 0x4A0 and len(data) >= 8:
            self._sop_limits_data = bytes(data[:8])
            self.sop["limits"] = decode_sop_limits(data, little_endian=True)
            self.last_sop_limits_monotonic = now_mono
        elif can_id == 0x4A3 and len(data) >= 8:
            self.sop["status"] = decode_sop_status(data, self._sop_limits_data)
            self.last_sop_status_monotonic = now_mono
        elif can_id == 0x4A4 and len(data) >= 8:
            self.sop["ecu_ack"] = decode_ecu_sop_ack(data)
            self.last_sop_ack_monotonic = now_mono
        elif can_id in METER_IDS and len(data) >= 6:
            expected_mux, key, scale = METER_IDS[can_id]
            result = decode_meter_result(data, expected_mux)
            if result is not None:
                self.meter[key] = {"value": result["value"] * scale,
                                   "status": result["status"], "counter": result["counter"]}
                self.meter_seen[key] = now_mono
        elif can_id in (PDM_BUS_ID, PDM_BATTERY_ID) and len(data) >= 8:
            side = "bus" if can_id == PDM_BUS_ID else "battery"
            self.pdm[side] = decode_pdm_side(data)
            self.pdm_seen[side] = now_mono
        elif can_id == FAN_STATUS_ID and len(data) >= 8:
            self.fan["status"] = decode_fan_status(data)
            self.last_fan_status_monotonic = now_mono
        elif can_id == FAN_DIAGNOSTIC_ID and len(data) >= 8:
            self.fan["diagnostic"] = decode_fan_diagnostic(data)
            self.last_fan_diagnostic_monotonic = now_mono
        elif can_id == FAN_COMMAND_ACK_ID and len(data) >= 8:
            ack = decode_fan_ack(data)
            self.fan_acks[ack["sequence"]] = ack
            if len(self.fan_acks) > 64:
                self.fan_acks.pop(next(iter(self.fan_acks)))
            self.fan_ack_history.appendleft({
                "time": datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3],
                **{key: ack[key] for key in ("opcode_name", "sequence", "result", "result_name",
                                              "mode_name", "failsafe_name", "accepted")},
                "duty_pct": list(ack["duty_pct"]), "target_pct": list(ack["target_pct"]),
            })
        elif can_id == FAN_CURVE_STATUS_ID and len(data) >= 5:
            self.fan["curve"] = decode_fan_curve(data)
            self.last_fan_curve_monotonic = now_mono
        elif can_id == FAN_FAILSAFE_STATUS_ID and len(data) >= 7:
            self.fan["failsafe"] = decode_fan_failsafe(data)
            self.last_fan_failsafe_monotonic = now_mono
        elif can_id == FAN_POWER_STATUS_ID and len(data) >= 8:
            self.fan["power_status"] = decode_fan_power_status(data)
            self.last_fan_power_monotonic = now_mono
        elif can_id == FAN_CALIB_STATUS_ID and len(data) >= 6:
            self.fan["calib_status"] = decode_fan_calib_status(data)
            self.last_fan_calib_monotonic = now_mono
        elif can_id == 0x502 and len(data) >= 8:
            self.ecu["torque_pct"] = decode_ecu_wheels_i16(data, 0.1)
            self.last_ecu_monotonic["torque"] = now_mono
        elif can_id == 0x505 and len(data) >= 8:
            self.ecu["velocity_rpm"] = decode_ecu_wheels_i16(data, 1.0)
            self.last_ecu_monotonic["velocity"] = now_mono
        elif can_id == 0x506 and len(data) >= 8:
            self.ecu["motor_temp_c"] = decode_ecu_wheels_i16(data, 0.1)
            self.last_ecu_monotonic["motor_temp"] = now_mono
        elif can_id == 0x507 and len(data) >= 8:
            self.ecu["inverter_temp_c"] = decode_ecu_wheels_i16(data, 0.1)
            self.last_ecu_monotonic["inverter_temp"] = now_mono
        elif can_id == 0x508 and len(data) >= 8:
            self.ecu["igbt_temp_c"] = decode_ecu_wheels_i16(data, 0.1)
            self.last_ecu_monotonic["igbt_temp"] = now_mono
        elif can_id == 0x509 and len(data) >= 5:
            self.ecu["status"] = decode_ecu_status(data)
            self.last_ecu_monotonic["status"] = now_mono
        elif can_id in TIRE_TEMP_IDS and len(data) >= 8:
            self.tires[f"0x{can_id:03X}"] = decode_tire_temp_frame(data)
            self.last_tire_monotonic = now_mono

        if now_mono - self._last_trend >= 0.45:
            pack_age = age(now_mono, self.last_pack_monotonic)
            pdm_age = age(now_mono, self.pdm_seen["bus"])
            pack_fresh = pack_age is not None and pack_age <= 1.5
            pdm_fresh = pdm_age is not None and pdm_age <= 2.5 and not self.pdm["bus"]["offline"]
            if pack_fresh or pdm_fresh:
                self._last_trend = now_mono
                self.trends.append({
                    "t": round(now_mono - self.started_monotonic, 1),
                    "hv_voltage": self.pack.get("voltage_v") if pack_fresh and self.pack.get("voltage_valid") else None,
                    "hv_current": self.pack.get("current_a") if pack_fresh and self.pack.get("current_valid") else None,
                    "lv_voltage": self.pdm["bus"]["voltage_v"] if pdm_fresh else None,
                    "lv_current": self.pdm["bus"]["current_a"] if pdm_fresh else None,
                })

    # -- snapshots ----------------------------------------------------------

    def snapshot(self, connection: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        pack = dict(self.pack)
        if pack:
            pack["state_name"] = STATE_NAMES.get(pack.get("state"), f"未知 {pack.get('state')}")
            pack["alarm_level_name"] = ALARM_LEVEL_NAMES.get(pack.get("alarm_level"), "未知")
        pack["age"] = age(now, self.last_pack_monotonic)
        fault = dict(self.fault) if self.fault else {}
        fault["age"] = age(now, self.last_fault_monotonic)
        fault["received"] = self.last_fault_monotonic is not None
        sop = {key: dict(value) if isinstance(value, dict) else value for key, value in self.sop.items()}
        sop["limits_age"] = age(now, self.last_sop_limits_monotonic)
        sop["status_age"] = age(now, self.last_sop_status_monotonic)
        sop["ecu_ack_age"] = age(now, self.last_sop_ack_monotonic)
        meter = {key: {**dict(channel), "age": age(now, self.meter_seen[key])} for key, channel in self.meter.items()}
        pdm = {side: {**dict(values), "age": age(now, self.pdm_seen[side])} for side, values in self.pdm.items()}
        fan = {key: dict(value) for key, value in self.fan.items()}
        fan["status_age"] = age(now, self.last_fan_status_monotonic)
        fan["diagnostic_age"] = age(now, self.last_fan_diagnostic_monotonic)
        fan["curve_age"] = age(now, self.last_fan_curve_monotonic)
        fan["failsafe_age"] = age(now, self.last_fan_failsafe_monotonic)
        fan["power_status_age"] = age(now, self.last_fan_power_monotonic)
        fan["calib_status_age"] = age(now, self.last_fan_calib_monotonic)
        ecu = {key: (list(value) if isinstance(value, list) else dict(value))
               for key, value in self.ecu.items()}
        ecu["age"] = {key: age(now, seen) for key, seen in self.last_ecu_monotonic.items()}
        tires = {key: (list(value) if value is not None else None) for key, value in self.tires.items()}
        tire_age = age(now, self.last_tire_monotonic)
        return {
            "connection": {**connection, "rx_count": self.rx_count, "tx_count": self.tx_count,
                           "last_rx_age": age(now, self.last_rx_monotonic)},
            "pack": pack, "fault": fault, "sop": sop, "meter": meter, "pdm": pdm,
            "fan": {**fan, "ack_history": list(self.fan_ack_history)},
            "ecu": {**ecu, "wheel_names": list(ECU_WHEEL_NAMES)},
            "tires": {**tires, "age": tire_age},
            "raw_frames": list(self.raw_frames), "trends": list(self.trends),
        }

    def quick_values(self) -> dict[str, Any]:
        """Small subset polled by the always-visible quick-value strip."""
        now = self.clock()
        fan_rpm = self.fan.get("status", {}).get("rpm")
        return {
            "pdm": {"bus_voltage_v": self.pdm["bus"]["voltage_v"],
                    "bus_current_a": self.pdm["bus"]["current_a"],
                    "bus_power_w": self.pdm["bus"]["power_w"],
                    "bus_offline": self.pdm["bus"]["offline"],
                    "age": age(now, self.pdm_seen["bus"])},
            "sop": {"discharge_power_kw": self.sop["limits"].get("discharge_power_kw"),
                    "charge_power_kw": self.sop["limits"].get("charge_power_kw"),
                    "age": age(now, self.last_sop_limits_monotonic)},
            "fan_rpm_max": max(fan_rpm) if fan_rpm else None,
            "fan_age": age(now, self.last_fan_status_monotonic),
            "pack": {"voltage_v": self.pack.get("voltage_v"), "current_a": self.pack.get("current_a"),
                     "soc_pct": self.pack.get("soc_pct"), "voltage_valid": self.pack.get("voltage_valid", False),
                     "current_valid": self.pack.get("current_valid", False),
                     "soc_valid": self.pack.get("soc_valid", False),
                     "age": age(now, self.last_pack_monotonic)},
        }
