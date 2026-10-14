"""Value table, frame catalog and scenarios of the driver-display bench.

The bench stands in for the CANB nodes that feed the display (BMS, ECU, PDM,
FanController, tyre module).  Every payload is produced by the builders in
:mod:`canhost.decoders`; this module only decides *which* value goes into
*which* frame, how often, and which deliberately broken variants exist.  What
the display accepts is defined by HMI_Display ``Core/Src/can.c`` and
``sop.c``; the central interface lives in vehicle-interfaces.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
from typing import Any

from ..decoders import (
    DATA_LOGGER_ID, ECU_DIAG_FL_FR_ID, ECU_DIAG_RL_RR_ID, ECU_IGBT_TEMP_ID, ECU_INVERTER_TEMP_ID,
    ECU_MOTOR_TEMP_ID, ECU_STATUS_ID, ECU_TORQUE_ID, ECU_VELOCITY_ID, PACK_STATUS_ID, PDM_BATTERY_ID,
    PDM_BUS_ID, FAN_STATUS_ID, SOP_POWER_ID, SOP_STATUS_ID, TIRE_TEMP_IDS,
    build_amk_diagnostic, build_data_logger, build_ecu_status, build_ecu_wheels_i16, build_fan_status,
    brake_pct_from_oil, build_pack_status, build_pdm_side, build_sop_limits, build_sop_status,
    build_tire_frame,
)

WHEELS = ("fl", "fr", "rl", "rr")
WHEEL_LABELS = {"fl": "左前 FL", "fr": "右前 FR", "rl": "左后 RL", "rr": "右后 RR"}

NODES = ("bms", "sop", "ecu", "pdm", "fan", "tire")
NODE_LABELS = {
    "bms": "BMS 包状态", "sop": "BMS 功率限值", "ecu": "ECU", "pdm": "PDM 低压",
    "fan": "风扇控制器", "tire": "胎温模块",
}

# Display firmware freshness limits (CAN_TIMEOUT_* in can.c); the page shows
# them next to each frame so a stopped frame's effect is predictable.
DISPLAY_TIMEOUT_MS = {
    0x4B0: 2000, 0x4A0: 300, 0x4A3: 1500, 0x305: 1000, 0x502: 1000, 0x503: 1000, 0x504: 1000,
    0x505: 1000, 0x506: 1000, 0x507: 1000, 0x508: 1000, 0x509: 1000, 0x5A0: 1500, 0x5A2: 1500,
}

ECU_PERIODS_MS = (10, 20, 50, 100)
DEFAULT_ECU_PERIOD_MS = 10
DEFECT_NAMES = {"none": "正常", "short": "DLC 过短", "crc": "CRC 错误", "version": "版本错误",
                "reserved": "保留位非零"}


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValueSpec:
    kind: str                 # float | int | bool | hex
    default: Any
    minimum: float | None = None
    maximum: float | None = None


VALUE_SPECS: dict[str, ValueSpec] = {}


def _add(key: str, kind: str, default: Any, minimum: float | None = None,
         maximum: float | None = None) -> None:
    VALUE_SPECS[key] = ValueSpec(kind, default, minimum, maximum)


_add("bms.voltage_v", "float", 548.0, 0.0, 6553.5)
_add("bms.current_a", "float", -1.5, -3276.8, 3276.7)
_add("bms.soc_pct", "int", 76, 0, 255)
_add("bms.voltage_valid", "bool", True)
_add("bms.current_valid", "bool", True)
_add("bms.soc_valid", "bool", True)
_add("sop.discharge_kw", "float", 80.0, 0.0, 6553.5)
_add("sop.regen_kw", "float", 8.0, 0.0, 6553.5)
_add("sop.discharge_a", "float", 180.0, 0.0, 6553.5)
_add("sop.regen_a", "float", 8.0, 0.0, 6553.5)
_add("sop.limits_valid", "bool", True)
_add("sop.inputs_valid", "bool", True)
_add("pedal.throttle_pct", "float", 0.0, 0.0, 100.0)
_add("pedal.oil_kpa", "float", 1.0, 0.0, 65.535)
_add("pedal.steering_deg", "float", 0.0, -3276.8, 3276.7)
for _wheel in WHEELS:
    _add(f"wheel.{_wheel}.torque_nm", "int", 0, -32768, 32767)
    _add(f"wheel.{_wheel}.rpm", "int", 0, -32768, 32767)
    _add(f"wheel.{_wheel}.motor_c", "float", 35.0, -3276.8, 3276.7)
    _add(f"wheel.{_wheel}.inverter_c", "float", 33.0, -3276.8, 3276.7)
    _add(f"wheel.{_wheel}.igbt_c", "float", 34.0, -3276.8, 3276.7)
    _add(f"wheel.{_wheel}.fault", "hex", 0, 0, 0xFFFFFFFF)
    _add(f"wheel.{_wheel}.enabled", "bool", True)
_add("ecu.mode", "int", -1, -8, 7)
_add("pdm.bus_v", "float", 24.2, -32.768, 32.767)
_add("pdm.bus_a", "float", 8.0, -327.68, 327.67)
_add("pdm.bus_w", "float", 193.6, 0.0, 6553.5)
_add("pdm.power_auto", "bool", True)
_add("pdm.energy_wh", "float", 12.0, 0.0, 655.35)
_add("pdm.offline", "bool", False)
_add("fan.rpm1", "int", 2520, 0, 65535)
_add("fan.rpm2", "int", 2640, 0, 65535)
_add("fan.rpm3", "int", 1800, 0, 65535)
_add("fan.duty1", "int", 42, 0, 255)
_add("fan.duty2", "int", 38, 0, 255)
for _wheel in WHEELS:
    for _point in range(1, 5):
        _add(f"tire.{_wheel}.t{_point}", "float", 45.0, 0.0, 255.99)
# -1: the bench has not sent this wheel input yet (the real board sends both at power-up).
_add("wheelsw.mode", "int", -1, -1, 3)
_add("wheelsw.slip", "int", -1, -1, 7)

# Everything except these keys is rewritten by the drive cycle while it runs.
_CYCLE_MANUAL_SUFFIXES = (".enabled", ".fault", "_valid", ".offline", ".power_auto")
_CYCLE_MANUAL_KEYS = {"ecu.mode", "wheelsw.mode", "wheelsw.slip", "pdm.bus_w"}


def cycle_drives(key: str) -> bool:
    return not (key in _CYCLE_MANUAL_KEYS or key.endswith(_CYCLE_MANUAL_SUFFIXES))


def value_catalog() -> dict[str, dict[str, Any]]:
    """Per-key kind, limits and whether the drive cycle owns it (rendered by the page)."""
    return {key: {"kind": spec.kind, "min": spec.minimum, "max": spec.maximum, "default": spec.default,
                  "driven": cycle_drives(key)} for key, spec in VALUE_SPECS.items()}


def default_values() -> dict[str, Any]:
    return {key: spec.default for key, spec in VALUE_SPECS.items()}


def normalize_value(key: str, raw: Any) -> Any:
    """Return the stored form of ``raw`` for ``key`` (clamped to the field range)."""
    spec = VALUE_SPECS.get(key)
    if spec is None:
        raise KeyError(f"未知参数：{key}")
    if spec.kind == "bool":
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in {"1", "true", "on", "yes"}:
                return True
            if text in {"0", "false", "off", "no"}:
                return False
            raise ValueError(f"{key} 需要开关值")
        return bool(raw)
    if spec.kind == "hex" and isinstance(raw, str):
        text = raw.strip().lower().removeprefix("0x") or "0"
        try:
            number: float = int(text, 16)
        except ValueError as exc:
            raise ValueError(f"{key} 需要十六进制数") from exc
    else:
        if isinstance(raw, bool):
            raise ValueError(f"{key} 需要数值")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{key} 需要有限数值")
    number = max(spec.minimum, min(spec.maximum, number))  # type: ignore[type-var]
    if spec.kind in {"int", "hex"}:
        return int(math.floor(number + 0.5))
    return float(number)


# ---------------------------------------------------------------------------
# Frame catalog
# ---------------------------------------------------------------------------

Builder = Callable[[Mapping[str, Any], int, str], bytes]


@dataclass(frozen=True, slots=True)
class FrameSpec:
    frame_id: int
    name: str
    node: str
    period_ms: int | None     # None: follows the ECU period setting
    min_dlc: int              # shortest DLC the display accepts
    build: Builder
    defects: tuple[str, ...] = ("short",)

    @property
    def key(self) -> str:
        return f"0x{self.frame_id:03X}"

    @property
    def note(self) -> str:
        """One plain sentence on what the frame carries (shown under its name)."""
        return FRAME_NOTES.get(self.frame_id, "")


FRAME_NOTES: dict[int, str] = {
    PACK_STATUS_ID: "电池总压、总电流、SOC，各带一个有效位",
    SOP_POWER_ID: "SOP：电池此刻允许的放电/回收功率上限，带 CRC",
    SOP_STATUS_ID: "SOP：允许的放电/回充电流和输入是否有效",
    DATA_LOGGER_ID: "油门开度、刹车油压、方向盘转角",
    ECU_TORQUE_ID: "四个电机的实际扭矩",
    ECU_DIAG_RL_RR_ID: "左后、右后电机驱动器故障码",
    ECU_DIAG_FL_FR_ID: "左前、右前电机驱动器故障码",
    ECU_VELOCITY_ID: "四个电机的转速",
    ECU_MOTOR_TEMP_ID: "四个电机的温度",
    ECU_INVERTER_TEMP_ID: "四个逆变器的温度",
    ECU_IGBT_TEMP_ID: "四个 IGBT 的温度",
    ECU_STATUS_ID: "ECU 回报的驾驶模式和四个电机的使能",
    PDM_BUS_ID: "低压母线的电压、电流、功率",
    PDM_BATTERY_ID: "低压电池支路的电压、电流、功率（车手屏收下但不显示）",
    FAN_STATUS_ID: "三路风扇转速和两路实测占空比",
    **{frame_id: "该轮胎面的 4 个温度测点" for frame_id in TIRE_TEMP_IDS},
}


def _wheel_list(values: Mapping[str, Any], field: str) -> list[Any]:
    return [values[f"wheel.{wheel}.{field}"] for wheel in WHEELS]


def pdm_power_w(values: Mapping[str, Any]) -> float:
    """Power field as sent: V x I when automatic (never negative, saturating at the 16-bit field limit)."""
    if values["pdm.power_auto"]:
        return min(6553.5, max(0.0, values["pdm.bus_v"] * values["pdm.bus_a"]))
    return values["pdm.bus_w"]


def _build_pack(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_pack_status(
        v["bms.voltage_v"], v["bms.current_a"], v["bms.soc_pct"],
        voltage_valid=v["bms.voltage_valid"], current_valid=v["bms.current_valid"],
        soc_valid=v["bms.soc_valid"])


def _build_sop_limits(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_sop_limits(
        v["sop.discharge_kw"], v["sop.regen_kw"], n, limits_valid=v["sop.limits_valid"],
        version=3 if d == "version" else 2, reserved_bits=0x02 if d == "reserved" else 0,
        crc_error=d == "crc")


def _build_sop_status(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_sop_status(v["sop.discharge_a"], v["sop.regen_a"], inputs_valid=v["sop.inputs_valid"],
                            version=3 if d == "version" else 2)


def _build_data_logger(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_data_logger(v["pedal.steering_deg"], v["pedal.throttle_pct"], v["pedal.oil_kpa"])


def _wheels_frame(field: str, scale: float) -> Builder:
    return lambda v, n, d: build_ecu_wheels_i16(_wheel_list(v, field), scale)


def _build_diag_rl_rr(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_amk_diagnostic(v["wheel.rl.fault"], v["wheel.rr.fault"])


def _build_diag_fl_fr(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_amk_diagnostic(v["wheel.fl.fault"], v["wheel.fr.fault"])


def _build_ecu_status(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_ecu_status(v["ecu.mode"], _wheel_list(v, "enabled"), _wheel_list(v, "fault"))


def _build_pdm_bus(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_pdm_side(v["pdm.bus_v"], v["pdm.bus_a"], pdm_power_w(v), v["pdm.energy_wh"],
                          offline=v["pdm.offline"])


def _build_pdm_battery(v: Mapping[str, Any], n: int, d: str) -> bytes:
    # Battery branch mirrors the bus values; the display parses it but never shows it.
    return build_pdm_side(v["pdm.bus_v"], v["pdm.bus_a"], pdm_power_w(v), v["pdm.energy_wh"])


def _build_fan(v: Mapping[str, Any], n: int, d: str) -> bytes:
    return build_fan_status([v["fan.rpm1"], v["fan.rpm2"], v["fan.rpm3"]],
                            [v["fan.duty1"], v["fan.duty2"]])


# Frame to wheel assignment follows the display (can.c: 0x071 RF, 0x072 LF, 0x073 LR, 0x074 RR).
TIRE_FRAME_WHEEL = {0x071: "fr", 0x072: "fl", 0x073: "rl", 0x074: "rr"}
TIRE_POINT_ORDER = {0x071: "外→内", 0x072: "内→外", 0x073: "外→内", 0x074: "内→外"}


def _tire_frame(frame_id: int) -> Builder:
    wheel = TIRE_FRAME_WHEEL[frame_id]
    return lambda v, n, d: build_tire_frame([v[f"tire.{wheel}.t{point}"] for point in range(1, 5)])


FRAMES: tuple[FrameSpec, ...] = (
    FrameSpec(PACK_STATUS_ID, "BMS 包状态", "bms", 500, 7, _build_pack),
    FrameSpec(SOP_POWER_ID, "SOP 功率限值", "sop", 10, 8, _build_sop_limits,
              ("short", "crc", "version", "reserved")),
    FrameSpec(SOP_STATUS_ID, "SOP 电流限值", "sop", 500, 8, _build_sop_status, ("short", "version")),
    FrameSpec(DATA_LOGGER_ID, "踏板与油压", "ecu", 50, 6, _build_data_logger),
    FrameSpec(ECU_TORQUE_ID, "四轮扭矩", "ecu", None, 8, _wheels_frame("torque_nm", 1.0)),
    FrameSpec(ECU_DIAG_RL_RR_ID, "AMK 故障码 RL/RR", "ecu", None, 8, _build_diag_rl_rr),
    FrameSpec(ECU_DIAG_FL_FR_ID, "AMK 故障码 FL/FR", "ecu", None, 8, _build_diag_fl_fr),
    FrameSpec(ECU_VELOCITY_ID, "四轮转速", "ecu", None, 8, _wheels_frame("rpm", 1.0)),
    FrameSpec(ECU_MOTOR_TEMP_ID, "电机温度", "ecu", None, 8, _wheels_frame("motor_c", 0.1)),
    FrameSpec(ECU_INVERTER_TEMP_ID, "逆变器温度", "ecu", None, 8, _wheels_frame("inverter_c", 0.1)),
    FrameSpec(ECU_IGBT_TEMP_ID, "IGBT 温度", "ecu", None, 8, _wheels_frame("igbt_c", 0.1)),
    FrameSpec(ECU_STATUS_ID, "驾驶模式与使能", "ecu", None, 5, _build_ecu_status),
    FrameSpec(PDM_BUS_ID, "低压总线", "pdm", 100, 8, _build_pdm_bus),
    FrameSpec(PDM_BATTERY_ID, "低压电池支路", "pdm", 100, 8, _build_pdm_battery),
    FrameSpec(FAN_STATUS_ID, "风扇转速与占空比", "fan", 100, 8, _build_fan),
    *(FrameSpec(frame_id, f"胎温 {WHEEL_LABELS[TIRE_FRAME_WHEEL[frame_id]]}", "tire", 100, 8,
                _tire_frame(frame_id)) for frame_id in TIRE_TEMP_IDS),
)
FRAME_BY_ID: dict[int, FrameSpec] = {spec.frame_id: spec for spec in FRAMES}
FRAMES_BY_NODE: dict[str, tuple[FrameSpec, ...]] = {
    node: tuple(spec for spec in FRAMES if spec.node == node) for node in NODES}
# Frame IDs the bench impersonates on CANB; seeing one from another sender means a real node is present.
IMPERSONATED_IDS = frozenset(FRAME_BY_ID)


def build_frame(spec: FrameSpec, values: Mapping[str, Any], counter: int, defect: str = "none") -> bytes:
    """Payload of ``spec`` for the current values, optionally with a deliberate defect."""
    if defect not in spec.defects and defect != "none":
        defect = "none"
    payload = spec.build(values, counter, defect)
    if defect == "short":
        return payload[:spec.min_dlc - 1]
    return payload


def default_controls() -> dict[str, Any]:
    return {
        "run": "manual",
        "paused": False,
        "ecu_period_ms": DEFAULT_ECU_PERIOD_MS,
        "ecu_follow_mode": True,
        "nodes": {node: True for node in NODES},
        "frames": {spec.key: True for spec in FRAMES},
        "defects": {spec.key: "none" for spec in FRAMES},
    }


def frame_period_ms(spec: FrameSpec, controls: Mapping[str, Any]) -> int:
    return int(spec.period_ms if spec.period_ms is not None else controls["ecu_period_ms"])


# ---------------------------------------------------------------------------
# Derived readouts (what the display should show for the current values)
# ---------------------------------------------------------------------------

def brake_percent(oil_kpa: float) -> int:
    """What the display's temporary oil-pressure brake bar shows for ``oil_kpa`` (mapping in decoders)."""
    return brake_pct_from_oil(max(0, min(0xFFFF, int(math.floor(oil_kpa / 0.001 + 0.5)))))


def derive(values: Mapping[str, Any]) -> dict[str, Any]:
    """Readings the display derives from the raw frames, for the page's inline hints."""
    # The display keeps whole degrees: the 0.1 degC raw count divided by 10, truncated like C.
    temps = [math.trunc(math.floor(values[f"wheel.{wheel}.{field}"] / 0.1 + 0.5) / 10)
             for wheel in WHEELS for field in ("motor_c", "inverter_c", "igbt_c")]
    return {
        "brake_pct": brake_percent(values["pedal.oil_kpa"]),
        "throttle_pct": int(math.floor(values["pedal.throttle_pct"] / 0.1 + 0.5)) // 10,
        "hv_power_kw": round(max(0.0, -values["bms.voltage_v"] * values["bms.current_a"]) / 1000.0, 1),
        "pdm_power_w": round(pdm_power_w(values), 1),
        "wheel_power_kw": {wheel: round(values[f"wheel.{wheel}.torque_nm"]
                                        * values[f"wheel.{wheel}.rpm"] / 9549.0, 1) for wheel in WHEELS},
        "tmax_c": max(temps) if temps else None,
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Scenario:
    label: str
    description: str
    values: Mapping[str, Any]
    paused: bool = False


def _wheels(field: str, *numbers: float) -> dict[str, float]:
    return {f"wheel.{wheel}.{field}": number for wheel, number in zip(WHEELS, numbers, strict=True)}


SCENARIOS: dict[str, Scenario] = {
    "idle": Scenario("待机停车", "高压已接通、车辆静止：踏板为零，电机已使能。", {}),
    "launch": Scenario("全力加速", "油门 100%，四轮高扭矩，大电流放电。", {
        "pedal.throttle_pct": 100.0, "pedal.oil_kpa": 1.0,
        "bms.voltage_v": 520.3, "bms.current_a": -142.0, "bms.soc_pct": 72,
        "sop.discharge_kw": 75.0, **_wheels("torque_nm", 20, 20, 20, 20),
        **_wheels("rpm", 9000, 9100, 9000, 9100), **_wheels("motor_c", 58, 60, 57, 59),
        **_wheels("inverter_c", 52, 53, 51, 52), **_wheels("igbt_c", 54, 55, 53, 54)}),
    "brake": Scenario("急刹回收", "油压 4.9 kPa（刹车条约 90%），负扭矩回收，电流转为充电。", {
        "pedal.throttle_pct": 0.0, "pedal.oil_kpa": 4.9,
        "bms.voltage_v": 556.0, "bms.current_a": 12.0,
        **_wheels("torque_nm", -6, -6, -6, -6), **_wheels("rpm", 8000, 8100, 8000, 8100)}),
    "overheat": Scenario("温度告警", "电机、逆变器、IGBT 跨过 70 ℃ 与 85 ℃ 两档着色阈值。", {
        **_wheels("motor_c", 72, 88, 95, 66), **_wheels("inverter_c", 71, 75, 80, 68),
        **_wheels("igbt_c", 70, 86, 90, 60)}),
    "lowsoc": Scenario("低电量", "SOC 18%（低于 20% 红色），电压偏低，放电限值降低。", {
        "bms.soc_pct": 18, "bms.voltage_v": 468.0, "sop.discharge_kw": 30.0, "sop.discharge_a": 64.0}),
    "amkfault": Scenario("驱动故障", "左前、右后 AMK 故障码非零；右前电机未使能。", {
        "wheel.fl.fault": 0x0419, "wheel.rr.fault": 0x0F5C, "wheel.fr.enabled": False}),
    "sop_zero": Scenario("禁止动力", "SOP 放电上限为 0，放电行红色。", {
        "sop.discharge_kw": 0.0, "sop.discharge_a": 0.0}),
    "sop_invalid": Scenario("SOP 无效", "LimitsValid 与 InputHealth 均为 0，SOP 行显示 --。", {
        "sop.limits_valid": False, "sop.inputs_valid": False}),
    "pdm_offline": Scenario("低压离线", "PDM 四个字段同时发无效占位值，低压行显示 --。", {
        "pdm.offline": True}),
    "no_soc": Scenario("SOC 无效", "SOC 有效位清零且数值大于 100，SOC 显示 --。", {
        "bms.soc_valid": False, "bms.soc_pct": 255}),
    "extremes": Scenario("极值排版", "各读数取接近上限的值，检查数字是否溢出或截断。", {
        "bms.voltage_v": 599.9, "bms.current_a": -399.9, "bms.soc_pct": 100,
        "pedal.throttle_pct": 100.0, "pedal.oil_kpa": 6.0,
        "sop.discharge_kw": 99.9, "sop.regen_kw": 99.9, "sop.discharge_a": 399.9, "sop.regen_a": 99.9,
        **_wheels("torque_nm", 99, -99, 99, -99), **_wheels("rpm", 20000, -20000, 20000, -20000),
        **_wheels("motor_c", 150, 150, 150, 150), **_wheels("inverter_c", 140, 140, 140, 140),
        **_wheels("igbt_c", 130, 130, 130, 130), **_wheels("fault", 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF),
        "pdm.bus_v": 29.9, "pdm.bus_a": 99.9, "fan.rpm1": 12000, "fan.rpm2": 12000,
        "fan.rpm3": 12000, "fan.duty1": 100, "fan.duty2": 100}),
    "silence": Scenario("总线静默", "暂停全部发送，车手屏各读数应在各自超时后变为 --。", {}, paused=True),
}


def scenario_catalog() -> list[dict[str, str]]:
    return [{"key": key, "label": item.label, "description": item.description}
            for key, item in SCENARIOS.items()]


def node_catalog() -> list[dict[str, Any]]:
    return [{"key": node, "label": NODE_LABELS[node], "frames": [spec.key for spec in FRAMES_BY_NODE[node]]}
            for node in NODES]


def frame_catalog() -> list[dict[str, Any]]:
    return [{"id": spec.key, "name": spec.name, "node": spec.node, "period_ms": spec.period_ms,
             "note": spec.note, "min_dlc": spec.min_dlc, "timeout_ms": DISPLAY_TIMEOUT_MS.get(spec.frame_id),
             "defects": ["none", *spec.defects]} for spec in FRAMES]
