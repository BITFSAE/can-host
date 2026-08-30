"""CAN frame formats shared by the BMS and vehicle decoders.

Every frame format is defined exactly once in this module.  The authoritative
sources are the sibling firmware repositories (BMS-MASTER-F405,
FanController, PDM) and the team-wide DBC repository vehicle-interfaces
(can/Vehicle_CanB.dbc); when they disagree, the firmware wins and both this
module and the DBC get updated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


# ---------------------------------------------------------------------------
# Shared value frame
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CanFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool
    timestamp: float = field(default_factory=time.time)
    direction: str = "rx"


def u16be(data: bytes, offset: int = 0) -> int:
    return (data[offset] << 8) | data[offset + 1]


def u16le(data: bytes, offset: int = 0) -> int:
    return data[offset] | (data[offset + 1] << 8)


def age(now: float, seen: float | None) -> float | None:
    return None if seen is None else round(max(0.0, now - seen), 2)


def crc8_sae_j1850(data: bytes) -> int:
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def format_raw_frame(frame: CanFrame, name: str) -> dict[str, Any]:
    """Build the monitor-page row dict shared by both protocol states."""
    from datetime import datetime
    return {
        "time": datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3],
        "direction": frame.direction,
        "id": f"0x{frame.arbitration_id:08X}" if frame.is_extended_id else f"0x{frame.arbitration_id:03X}",
        "extended": frame.is_extended_id,
        "dlc": len(frame.data),
        "data": " ".join(f"{byte:02X}" for byte in frame.data),
        "name": name,
    }


# ---------------------------------------------------------------------------
# CANB frame names (monitor page + CSV recording)
# ---------------------------------------------------------------------------

# Chroma IDs follow one panel node base. Keep this equal to the firmware's
# BMS_CHROMA_NODE_BASE_STD_ID and the team CANB DBC.
CHROMA_NODE_BASE_STD_ID = 0x200
CHROMA_VOLT_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x01
CHROMA_CURR_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x02
CHROMA_PROTECT_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x04
CHROMA_OUTPUT_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x05
CHROMA_CMD_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x90
CHROMA_ACK_STD_ID = CHROMA_NODE_BASE_STD_ID + 0x91
CHROMA_DERIVED_STD_IDS = {
    CHROMA_VOLT_STD_ID, CHROMA_CURR_STD_ID, CHROMA_PROTECT_STD_ID,
    CHROMA_OUTPUT_STD_ID, CHROMA_CMD_STD_ID, CHROMA_ACK_STD_ID,
}
CANB_OCCUPIED_WITHOUT_CHROMA = {
    0x050, *range(0x060, 0x06B), *range(0x071, 0x075),
    0x300, 0x301, 0x305, 0x310, 0x430,
    0x4A0, 0x4A3, 0x4A4, 0x4B0, 0x4B1, 0x4B2,
    *range(0x502, 0x50A), *range(0x512, 0x51A),
    0x521, 0x522, 0x526, 0x528, *range(0x5A0, 0x5AA), 0x700, 0x784,
}
if CHROMA_ACK_STD_ID > 0x7FF or len(CHROMA_DERIVED_STD_IDS) != 6:
    raise RuntimeError("Chroma 节点基准派生出的 ID 超出 11 位范围或发生内部重复")
if CHROMA_DERIVED_STD_IDS & CANB_OCCUPIED_WITHOUT_CHROMA:
    occupied = ", ".join(f"0x{value:03X}" for value in sorted(
        CHROMA_DERIVED_STD_IDS & CANB_OCCUPIED_WITHOUT_CHROMA))
    raise RuntimeError(f"Chroma 节点基准与现有 CANB ID 冲突：{occupied}")

CANB_IDS = {
    0x071: "胎温测点 1-4（轮位待确认）",
    0x072: "胎温测点 5-8（轮位待确认）",
    0x073: "胎温测点 9-12（轮位待确认）",
    0x074: "胎温测点 13-16（轮位待确认）",
    0x305: "ECU 数据记录（转向/踏板/油压）",
    CHROMA_VOLT_STD_ID: "Chroma 电压测量",
    CHROMA_CURR_STD_ID: "Chroma 电流测量",
    CHROMA_PROTECT_STD_ID: "Chroma 保护状态",
    CHROMA_OUTPUT_STD_ID: "Chroma 输出状态",
    0x430: "赛会数据记录器状态",
    CHROMA_CMD_STD_ID: "Chroma 命令",
    CHROMA_ACK_STD_ID: "Chroma 应答",
    0x4A0: "SOP 限值",
    0x4A3: "SOP 状态",
    0x4A4: "ECU SOP 确认",
    0x4B0: "BMS 包状态",
    0x4B1: "BMS 统一故障状态",
    0x4B2: "BMS 告警等级明细",
    0x502: "ECU 四轮实际扭矩",
    0x503: "ECU 诊断号 1/2",
    0x504: "ECU 诊断号 3/4",
    0x505: "ECU 四轮实际转速",
    0x506: "ECU 四轮电机温度",
    0x507: "ECU 四轮逆变器温度",
    0x508: "ECU 四轮 IGBT 温度",
    0x509: "ECU 四轮状态",
    0x512: "IVT 电流",
    0x513: "IVT U1",
    0x514: "IVT U2",
    0x515: "IVT U3",
    0x516: "IVT 温度",
    0x517: "IVT 功率",
    0x518: "IVT 电荷计数",
    0x519: "IVT 能量计数",
    0x521: "赛会能量计 电流",
    0x522: "赛会能量计 U1",
    0x526: "赛会能量计 功率",
    0x528: "赛会能量计 能量",
    0x5A0: "PDM 低压总线侧",
    0x5A1: "PDM 低压电池侧",
    0x5A2: "风扇实际状态",
    0x5A3: "风扇诊断",
    0x5A4: "风扇命令",
    0x5A5: "风扇命令应答",
    0x5A6: "风扇自动曲线状态",
    0x5A7: "风扇失联策略状态",
    0x5A8: "风扇功率仲裁状态",
    0x5A9: "风扇标定状态",
    0x1806E5F4: "Legacy 充电请求",
    0x18FF50E5: "Legacy 充电反馈",
}

def canb_frame_name(can_id: int) -> str:
    return CANB_IDS.get(can_id, "未定义帧")


STATE_NAMES = {2: "SELF_TEST", 3: "STANDBY", 4: "PRECHARGE", 5: "HV_ON", 7: "FAULT"}
ALARM_LEVEL_NAMES = {0: "正常", 1: "一级故障", 2: "二级告警", 3: "保留值"}


# ---------------------------------------------------------------------------
# BMS pack status 0x4B0 / CAN1 0x186050F4 (identical payload layout)
# ---------------------------------------------------------------------------

def decode_pack_status(data: bytes) -> dict[str, Any]:
    valid = data[5]
    return {
        "voltage_v": u16be(data) / 10.0 if valid & 0x01 else None,
        "current_a": int.from_bytes(data[2:4], "big", signed=True) / 10.0 if valid & 0x02 else None,
        "soc_pct": data[4] if valid & 0x04 else None,
        "voltage_valid": bool(valid & 0x01),
        "current_valid": bool(valid & 0x02),
        "soc_valid": bool(valid & 0x04),
        "cell_voltage_complete": bool(valid & 0x08),
        "temperature_complete": bool(valid & 0x10),
        "state": (data[6] >> 4) & 0x0F,
        "alarm_level": data[6] & 0x0F,
    }


def decode_fault_fields(data: bytes) -> dict[str, Any]:
    state, alarm_level = (data[0] >> 4) & 0x0F, data[0] & 0x0F
    code = int.from_bytes(data[1:5], "big")
    flags = data[5]
    return {
        "code": code,
        "code_hex": f"0x{code:08X}",
        "version": data[7],
        "state": state,
        "state_name": STATE_NAMES.get(state, f"未知 {state}"),
        "alarm_level": alarm_level,
        "alarm_level_name": ALARM_LEVEL_NAMES.get(alarm_level, "未知"),
        "flags": {
            "latched": bool(flags & 0x80), "bms_output_latched": bool(flags & 0x40),
            "reset_pending": bool(flags & 0x20), "log_write_pending": bool(flags & 0x10),
            "log_clear_pending": bool(flags & 0x08), "charge_mode": bool(flags & 0x04),
            "charger_type": "Chroma" if flags & 0x02 else "Legacy",
        },
        "slave_offline": [bool(data[6] & (1 << i)) for i in range(6)],
    }


def decode_alarm_levels(data: bytes) -> list[int]:
    return [(data[index // 4] >> ((index % 4) * 2)) & 0x03 for index in range(32)]


# ---------------------------------------------------------------------------
# SOP 0x4A0 / 0x4A3 / 0x4A4 (CANB little-endian; CAN1 mirror is big-endian)
# ---------------------------------------------------------------------------

def decode_sop_limits(data: bytes, little_endian: bool) -> dict[str, Any]:
    reader = u16le if little_endian else u16be
    return {
        "discharge_current_a": reader(data) / 10.0,
        "charge_current_a": reader(data, 2) / 10.0,
        "discharge_power_kw": reader(data, 4) / 10.0,
        "charge_power_kw": reader(data, 6) / 10.0,
    }


def decode_sop_status(data: bytes, limits_data: bytes | None) -> dict[str, Any]:
    flags, intervention = data[1], data[6]
    crc_input = (bytes.fromhex("04 A0") + limits_data + bytes.fromhex("04 A3") + data[:7]
                 if limits_data is not None else None)
    return {
        "protocol_version": data[0] >> 4, "sequence": data[0] & 0x0F,
        "limits_valid": bool(flags & 0x01), "drive_allowed": bool(flags & 0x02),
        "regen_allowed": bool(flags & 0x04), "intervention_active": bool(flags & 0x08),
        "fault_latched": bool(flags & 0x10), "ack_required": bool(flags & 0x20),
        "limits_reduced": bool(flags & 0x40), "bms_state": data[2],
        "limit_reason": u16le(data, 3), "input_health": data[5],
        "intervention_level": intervention & 0x03,
        "discharge_intervention": bool(intervention & 0x04), "charge_intervention": bool(intervention & 0x08),
        "waiting_ack": bool(intervention & 0x10), "ack_fresh": bool(intervention & 0x20),
        "current_below_exit": bool(intervention & 0x40),
        "crc_valid": crc_input is not None and crc8_sae_j1850(crc_input) == data[7],
    }


def decode_ecu_sop_ack(data: bytes) -> dict[str, Any]:
    return {
        "protocol_version": data[0] >> 4, "sequence": data[0] & 0x0F, "flags": data[1],
        "pair_valid": bool(data[1] & 0x01), "limits_applied": bool(data[1] & 0x02),
        "zero_torque": bool(data[1] & 0x04), "ecu_fault": bool(data[1] & 0x08),
        "discharge_power_kw": u16le(data, 2) / 10.0, "regen_power_kw": u16le(data, 4) / 10.0,
        "limit_source": data[6],
        "crc_valid": crc8_sae_j1850(bytes.fromhex("04 A4") + data[:7]) == data[7],
    }


# ---------------------------------------------------------------------------
# Result frames: own IVT-S (0x512..0x519, little-endian) and the competition
# energy meter (0x521/0x522/0x526/0x528, big-endian).  Same envelope, opposite
# byte order.
# ---------------------------------------------------------------------------

IVT_RESULT_KEYS = ["current_a", "u1_v", "u2_v", "u3_v", "temperature_c", "power_w", "charge_as", "energy_wh"]
IVT_RESULT_SCALES = [0.001, 0.001, 0.001, 0.001, 0.1, 1.0, 1.0, 1.0]


def decode_ivt_result(data: bytes, expected_mux: int) -> dict[str, Any] | None:
    if len(data) < 6 or data[0] != expected_mux:
        return None
    return {
        "status": (data[1] >> 4) & 0x0F,
        "counter": data[1] & 0x0F,
        "value": int.from_bytes(data[2:6], "little", signed=True),
    }


def decode_meter_result(data: bytes, expected_mux: int) -> dict[str, Any] | None:
    if len(data) < 6 or data[0] != expected_mux:
        return None
    return {
        "status": (data[1] >> 4) & 0x0F,
        "counter": data[1] & 0x0F,
        "value": int.from_bytes(data[2:6], "big", signed=True),
    }


METER_IDS = {0x521: (0, "current_a", 0.001), 0x522: (1, "u1_v", 0.001),
             0x526: (5, "power_w", 1.0), 0x528: (7, "energy_wh", 1.0)}


# ---------------------------------------------------------------------------
# PDM 0x5A0 / 0x5A1 (big-endian; all four invalid sentinels = side offline)
# ---------------------------------------------------------------------------

def decode_pdm_side(data: bytes) -> dict[str, Any]:
    voltage_raw = u16be(data)
    current_raw = int.from_bytes(data[2:4], "big", signed=True)
    power_raw = u16be(data, 4)
    energy_raw = u16be(data, 6)
    offline = (voltage_raw == 0x7FFF and current_raw == 0x7FFF
               and power_raw == 0xFFFF and energy_raw == 0xFFFF)
    return {
        "voltage_v": None if offline else voltage_raw / 1000.0,
        "current_a": None if offline else current_raw / 100.0,
        "power_w": None if offline else power_raw / 10.0,
        "energy_wh": None if offline else energy_raw / 100.0,
        "offline": offline,
    }


PDM_BUS_ID = 0x5A0
PDM_BATTERY_ID = 0x5A1


# ---------------------------------------------------------------------------
# FanController 0x5A2..0x5A9 (status big-endian; power/calib/command little-endian)
# ---------------------------------------------------------------------------

FAN_STATUS_ID = 0x5A2
FAN_DIAGNOSTIC_ID = 0x5A3
FAN_COMMAND_ID = 0x5A4
FAN_COMMAND_ACK_ID = 0x5A5
FAN_CURVE_STATUS_ID = 0x5A6
FAN_FAILSAFE_STATUS_ID = 0x5A7
FAN_POWER_STATUS_ID = 0x5A8
FAN_CALIB_STATUS_ID = 0x5A9

FAN_MODE_NAMES = {0: "自动", 1: "手动", 2: "关闭"}
FAN_FAILSAFE_NAMES = {0: "保持最后目标", 1: "固定保底", 2: "全速"}
FAN_RESULT_NAMES = {
    0: "成功", 1: "CRC 错误", 2: "长度错误", 3: "参数错误",
    4: "操作码不支持", 5: "模式超时，已回到自动",
    6: "安全门控拦截（非DCDC就绪或超温）",
}
FAN_FAULT_NAMES = [
    "风扇 1 无转速", "风扇 2 无转速", "风扇 3 无转速", "电机温度超时",
    "控制器温度超时", "PWM1 启动失败", "PWM2 启动失败", "测速启动失败",
]
FAN_POWER_SUPPLY_NAMES = {
    0: "未知", 1: "低压电池", 2: "DCDC接管中", 3: "DCDC就绪",
    4: "功率受限", 5: "数据故障",
}
FAN_POWER_LIMIT_NAMES = {
    0: "无限制", 1: "总线电流限制", 2: "电池电流限制", 3: "PDM超时",
    4: "DCDC切换保持", 5: "停转保护", 6: "超温保护", 7: "安全中止",
}
FAN_CALIB_STATE_NAMES = {
    0: "未激活", 1: "标定中", 2: "已中止", 3: "已完成",
}
FAN_CALIB_ABORT_NAMES = {
    0: "无", 1: "DCDC供电丢失", 2: "PDM遥测超时", 3: "总线功率受限",
    4: "温度超限", 5: "风扇停转", 6: "租约超时", 7: "用户停止",
}
FAN_COMMAND_CODES = {
    "fan_control": 0x01,
    "fan_curve": 0x02,
    "fan_failsafe": 0x03,
    "fan_restore_defaults": 0x04,
    "fan_query": 0x05,
    "fan_curve_ch2": 0x06,
    "fan_calib": 0x08,
}


def fan_opcode_name(opcode: int) -> str:
    for name, code in FAN_COMMAND_CODES.items():
        if code == opcode:
            return name
    return f"操作码 0x{opcode:02X}"


def fan_ack_matches(name: str, ack: dict[str, Any]) -> bool:
    """Return whether a FanController ACK echoes the named command's opcode and sequence."""
    expected = FAN_COMMAND_CODES.get(name)
    return expected is not None and int(ack.get("opcode", -1)) == expected


def decode_fan_status(data: bytes) -> dict[str, Any]:
    return {
        "rpm": [u16be(data), u16be(data, 2), u16be(data, 4)],
        "duty_pct": [data[6], data[7]],
    }


def decode_fan_diagnostic(data: bytes) -> dict[str, Any]:
    flags = data[1]
    motor_raw = int.from_bytes(data[2:4], "big", signed=True)
    controller_raw = int.from_bytes(data[4:6], "big", signed=True)
    return {
        "faults": data[0],
        "fault_names": [FAN_FAULT_NAMES[bit] for bit in range(8) if data[0] & (1 << bit)],
        "motor_temp_valid": bool(flags & 0x01),
        "inverter_temp_valid": bool(flags & 0x02),
        "igbt_temp_valid": bool(flags & 0x04),
        "group1_running": bool(flags & 0x08),
        "group2_running": bool(flags & 0x10),
        "mode": (flags >> 5) & 0x03,
        "mode_name": FAN_MODE_NAMES.get((flags >> 5) & 0x03, "未知"),
        # 0x7FFF marks a stale source; do not show it as a temperature.
        "motor_temp_c": motor_raw / 10.0 if motor_raw != 0x7FFF else None,
        "controller_temp_c": controller_raw / 10.0 if controller_raw != 0x7FFF else None,
        "target_pct": [data[6], data[7]],
    }


def decode_fan_ack(data: bytes) -> dict[str, Any]:
    mode, failsafe = data[3] & 0x03, (data[3] >> 4) & 0x03
    return {
        "opcode": data[0], "opcode_name": fan_opcode_name(data[0]),
        "sequence": data[1], "result": data[2],
        "result_name": FAN_RESULT_NAMES.get(data[2], f"未知 {data[2]}"),
        "accepted": data[2] == 0, "mode": mode,
        "mode_name": FAN_MODE_NAMES.get(mode, "未知"),
        "failsafe": failsafe,
        "failsafe_name": FAN_FAILSAFE_NAMES.get(failsafe, "未知"),
        "duty_pct": [data[4], data[5]], "target_pct": [data[6], data[7]],
    }


def decode_fan_curve(data: bytes) -> dict[str, Any]:
    critical_c = data[5] if len(data) > 5 else 75
    start_duty = data[6] if len(data) > 6 else 30
    channel = data[7] if len(data) > 7 else 1
    return {
        "temp_off_c": data[0], "temp_on_c": data[1], "temp_full_c": data[2],
        "min_duty_pct": data[3], "ramp_up_pct_per_s": data[4],
        "critical_temp_c": critical_c, "start_duty_pct": start_duty,
        "channel": channel,
    }


def decode_fan_failsafe(data: bytes) -> dict[str, Any]:
    version = data[7] if len(data) > 7 else 1
    return {
        "failsafe": data[0],
        "failsafe_name": FAN_FAILSAFE_NAMES.get(data[0], f"未知 {data[0]}"),
        "fallback1_duty_pct": data[1], "fallback2_duty_pct": data[2],
        "stale_hold_s": data[3], "ramp_down_pct_per_s": data[4],
        "mode": data[5], "mode_name": FAN_MODE_NAMES.get(data[5], "未知"),
        "lease_remaining_s": data[6],
        "protocol_version": version,
    }


def decode_fan_power_status(data: bytes) -> dict[str, Any]:
    supply_state = data[0] & 0x0F
    limit_reason = (data[0] >> 4) & 0x0F
    budget_a = data[5] * 0.1
    predicted_a = u16le(data, 6) * 0.1
    return {
        "power_supply_state": supply_state,
        "power_supply_name": FAN_POWER_SUPPLY_NAMES.get(supply_state, f"未知 ({supply_state})"),
        "power_limit_reason": limit_reason,
        "power_limit_name": FAN_POWER_LIMIT_NAMES.get(limit_reason, f"未知 ({limit_reason})"),
        "thermal_req_pct": [data[1], data[2]],
        "power_limited_target_pct": [data[3], data[4]],
        "current_budget_a": round(budget_a, 1),
        "predicted_current_a": round(predicted_a, 1),
    }


def decode_fan_calib_status(data: bytes) -> dict[str, Any]:
    calib_state = data[0] & 0x0F
    abort_reason = (data[0] >> 4) & 0x0F
    return {
        "calib_state": calib_state,
        "calib_state_name": FAN_CALIB_STATE_NAMES.get(calib_state, f"未知 ({calib_state})"),
        "calib_abort_reason": abort_reason,
        "calib_abort_name": FAN_CALIB_ABORT_NAMES.get(abort_reason, f"未知 ({abort_reason})"),
        "step": data[1],
        "calib_target_pct": [data[2], data[3]],
        "lease_remaining_s": data[4],
        "param_version": data[5],
        "flags": u16le(data, 6) if len(data) >= 8 else 0,
    }


def build_fan_command(name: str, values: dict[str, Any] | None = None) -> CanFrame:
    """Build a validated FanController command frame (CANB 0x5A4, DLC 8).

    Byte layout: opcode, sequence, five parameters, CRC-8/SAE-J1850 over
    bytes 0..6.  Ranges mirror fan_controller.c process_command().
    """
    values = values or {}
    sequence = int(values.get("_sequence", 0)) & 0xFF

    def command_frame(opcode: int, parameters: bytes) -> CanFrame:
        body = bytearray([opcode, sequence, *parameters])
        body.append(crc8_sae_j1850(body))
        return CanFrame(FAN_COMMAND_ID, bytes(body), False, time.time(), "tx")

    if name == "fan_control":
        mode = int(values.get("mode", -1))
        duty1 = int(values.get("duty1_pct", 0))
        duty2 = int(values.get("duty2_pct", 0))
        lease_s = int(values.get("lease_s", 0))
        if mode not in (0, 1, 2):
            raise ValueError("风扇模式必须是 0=自动、1=手动 或 2=关闭")
        if not (0 <= duty1 <= 100 and 0 <= duty2 <= 100):
            raise ValueError("占空比必须在 0..100 %")
        if mode != 0 and not 1 <= lease_s <= 60:
            raise ValueError("手动/关闭模式的有效时间必须在 1..60 秒")
        if mode == 2 and (duty1 != 0 or duty2 != 0):
            raise ValueError("关闭模式的两路占空比必须为 0")
        if mode == 0:
            duty1 = duty2 = lease_s = 0
        return command_frame(0x01, bytes([mode, duty1, duty2, lease_s, 0]))
    if name in ("fan_curve", "fan_curve_ch2"):
        opcode = 0x02 if name == "fan_curve" else 0x06
        temp_off, temp_on, temp_full = int(values["temp_off_c"]), int(values["temp_on_c"]), int(values["temp_full_c"])
        min_duty = int(values["min_duty_pct"])
        ramp_up = int(values["ramp_up_pct_per_s"])
        if not (0 <= temp_off <= 150 and 0 <= temp_on <= 150 and 0 <= temp_full <= 150
                and temp_off < temp_on < temp_full):
            raise ValueError("温控曲线必须满足 关闭 < 启动 < 全速，且都不超过 150 ℃")
        if not 10 <= min_duty <= 100:
            raise ValueError("最低运行占空比必须在 10..100 %")
        if not 10 <= ramp_up <= 100:
            raise ValueError("占空比上升速度必须在 10..100 %/s")
        return command_frame(opcode, bytes([temp_off, temp_on, temp_full, min_duty, ramp_up]))
    if name == "fan_failsafe":
        strategy = int(values["strategy"])
        fallback1 = int(values["fallback1_duty_pct"])
        fallback2 = int(values["fallback2_duty_pct"])
        hold_s = int(values["stale_hold_s"])
        ramp_down = int(values["ramp_down_pct_per_s"])
        if strategy not in (0, 1, 2):
            raise ValueError("失联策略必须是 0=保持最后目标、1=固定保底 或 2=全速")
        if not (0 <= fallback1 <= 100 and 0 <= fallback2 <= 100):
            raise ValueError("保底占空比必须在 0..100 %")
        if not 0 <= hold_s <= 30:
            raise ValueError("保持最后目标的时间必须在 0..30 秒")
        if not 10 <= ramp_down <= 100:
            raise ValueError("占空比下降速度必须在 10..100 %/s")
        return command_frame(0x03, bytes([strategy, fallback1, fallback2, hold_s, ramp_down]))
    if name == "fan_calib":
        action = int(values.get("action", 1)) # 1=Start, 2=Lease/Update, 3=Stop, 4=ConfirmDcdc
        step = int(values.get("step", 0)) & 0xFF
        duty1 = int(values.get("duty1_pct", 0))
        duty2 = int(values.get("duty2_pct", 0))
        lease_s = int(values.get("lease_s", 10))
        if action not in (1, 2, 3, 4):
            raise ValueError("标定动作必须是 1=启动、2=续约、3=停止 或 4=确认DCDC就绪")
        if not (0 <= duty1 <= 100 and 0 <= duty2 <= 100):
            raise ValueError("占空比必须在 0..100 %")
        if action not in (3,) and not 1 <= lease_s <= 60:
            raise ValueError("标定租约必须在 1..60 秒")
        if action == 3:
            duty1 = duty2 = lease_s = 0
        if action == 4:
            step = duty1 = duty2 = 0
        return command_frame(0x08, bytes([action, step, duty1, duty2, lease_s]))
    if name == "fan_restore_defaults":
        return command_frame(0x04, bytes([0xA5, 0, 0, 0, 0]))
    if name == "fan_query":
        return command_frame(0x05, bytes([0, 0, 0, 0, 0]))
    raise ValueError(f"未知风扇命令：{name}")


# ---------------------------------------------------------------------------
# ECU motor debug frames 0x502/0x505..0x509 (little-endian int16, wheel byte
# order RL, RR, FL, FR; returned in display order FL, FR, RL, RR)
# ---------------------------------------------------------------------------

ECU_WHEEL_NAMES = ["FL", "FR", "RL", "RR"]


def decode_ecu_wheels_i16(data: bytes, scale: float) -> list[float]:
    raw = [int.from_bytes(data[offset:offset + 2], "little", signed=True) for offset in (0, 2, 4, 6)]
    ordered = [raw[2], raw[3], raw[0], raw[1]]
    return [round(value * scale, 1) for value in ordered]


def decode_ecu_status(data: bytes) -> dict[str, Any]:
    def nibble(byte: int) -> dict[str, bool]:
        return {"FR": bool(byte & 0x01), "FL": bool(byte & 0x02),
                "RR": bool(byte & 0x04), "RL": bool(byte & 0x08)}

    mode_raw = (data[0] >> 4) & 0x0F
    mode_signed = mode_raw - 16 if mode_raw >= 8 else mode_raw
    return {
        "error": nibble(data[0]),
        "mode_flag": mode_signed,
        "system_ready": nibble(data[1]),
        "quit_dc_on": nibble(data[1] >> 4),
        "quit_inverter_on": nibble(data[2]),
        "enable": nibble(data[2] >> 4),
        "logic_state": {"RR": data[3] & 0x0F, "RL": (data[3] >> 4) & 0x0F,
                        "FR": data[4] & 0x0F, "FL": (data[4] >> 4) & 0x0F},
    }


# Tire temperature frames 0x071..0x074: four points per frame, each an integer
# byte plus a fraction byte scaled 0.01 degC.  The frame-to-wheel mapping is
# still pending physical confirmation in vehicle-interfaces.
TIRE_TEMP_IDS = (0x071, 0x072, 0x073, 0x074)


def decode_tire_temp_frame(data: bytes) -> list[float | None] | None:
    if len(data) < 8:
        return None
    return [data[index * 2] + data[index * 2 + 1] / 100.0 for index in range(4)]
