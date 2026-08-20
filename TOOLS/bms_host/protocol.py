"""F405 BMS CAN protocol decoder and command encoder.

The definitions in this module follow ``App/bms_can_protocol.c``.  They are
kept local to the firmware repository so the host tool remains usable even
when the team-wide DBC repository is not checked out next to this project.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import time
import struct
from collections.abc import Callable
from typing import Any


CELL_COUNT = 138
TEMP_COUNT = 48
SLAVE_COUNT = 6

CAN1_CELL_VOLT_BASE = 0x180050F3
CAN1_CELL_TEMP_BASE = 0x184050F3
CAN1_COMMAND_REQ_EXT_ID = 0x18A050F5

CAN1_IDS = {
    0x18A050F5: "工具命令请求（统一）",
    0x186050F4: "电池总状态",
    0x186150F4: "单体电压极值",
    0x186250F4: "温度与风扇",
    0x186350F4: "继电器与充电请求",
    0x186450F4: "均衡位图 1/3",
    0x186550F4: "均衡位图 2/3",
    0x186650F4: "均衡位图 3/3",
    0x186750F4: "单体电压累加",
    0x186850F4: "IMD 诊断",
    0x186950F4: "高压状态",
    0x186A50F4: "SOP 限值",
    0x186B50F4: "运行配置与充电反馈",
    0x186C50F4: "固件身份",
    0x186C51F4: "固件构建日期",
    0x186D50F4: "IVT 与 SOC 诊断",
    0x187650F4: "统一故障状态",
    0x187750F4: "告警阈值",
    0x187850F4: "告警等级明细",
    0x187F50F4: "告警开关",
    0x18A450F4: "RTC 校时应答",
    0x18A650F4: "工具命令统一应答",
    0x18A750F4: "工具命令数据",
}

CAN1_TOOL_IDS = {0x18A050F5}
TOOL_PROTOCOL_VERSION = 4

CANB_IDS = {
    0x401: "Chroma 电压测量",
    0x402: "Chroma 电流测量",
    0x404: "Chroma 保护状态",
    0x405: "Chroma 输出状态",
    0x490: "Chroma 命令",
    0x491: "Chroma 应答",
    0x4A0: "SOP 限值",
    0x4B0: "BMS 包状态",
    0x4B1: "BMS 统一故障状态",
    0x4B2: "BMS 告警等级明细",
    0x4A3: "SOP 状态",
    0x4A4: "ECU SOP 确认",
    0x512: "IVT 电流",
    0x513: "IVT U1",
    0x514: "IVT U2",
    0x515: "IVT U3",
    0x516: "IVT 温度",
    0x517: "IVT 功率",
    0x518: "IVT 电荷计数",
    0x519: "IVT 能量计数",
    0x1806E5F4: "Legacy 充电请求",
    0x18FF50E5: "Legacy 充电反馈",
    0x506: "电机温度输入（四轮）",
    0x507: "逆变器温度输入（四轮）",
    0x508: "IGBT 温度输入（四轮）",
    0x5A2: "风扇实际状态",
    0x5A3: "风扇诊断",
    0x5A4: "风扇命令",
    0x5A5: "风扇命令应答",
    0x5A6: "风扇自动曲线状态",
    0x5A7: "风扇失联策略状态",
}

# FanController node on CANB (see FanController/Core/Src/fan_controller.c).
# Status frames use big-endian 16-bit fields; commands carry CRC-8/SAE-J1850.
FAN_STATUS_ID = 0x5A2
FAN_DIAGNOSTIC_ID = 0x5A3
FAN_COMMAND_ID = 0x5A4
FAN_COMMAND_ACK_ID = 0x5A5
FAN_CURVE_STATUS_ID = 0x5A6
FAN_FAILSAFE_STATUS_ID = 0x5A7

FAN_MODE_NAMES = {0: "自动", 1: "手动", 2: "关闭"}
FAN_FAILSAFE_NAMES = {0: "保持最后目标", 1: "固定保底", 2: "全速"}
FAN_RESULT_NAMES = {
    0: "成功", 1: "CRC 错误", 2: "长度错误", 3: "参数错误",
    4: "操作码不支持", 5: "模式超时，已回到自动",
}
FAN_FAULT_NAMES = [
    "风扇 1 无转速", "风扇 2 无转速", "风扇 3 无转速", "电机温度超时",
    "控制器温度超时", "PWM1 启动失败", "PWM2 启动失败", "测速启动失败",
]
FAN_COMMAND_CODES = {
    "fan_control": 0x01,
    "fan_curve": 0x02,
    "fan_failsafe": 0x03,
    "fan_restore_defaults": 0x04,
    "fan_query": 0x05,
}

# Keep the names aligned with the firmware state constants and the DOC status
# tables.  The short English names are easier to scan in the large overview
# state readout and remain unambiguous in command confirmations.
STATE_NAMES = {2: "SELF_TEST", 3: "STANDBY", 4: "PRECHARGE", 5: "HV_ON", 7: "FAULT"}
ALARM_LEVEL_NAMES = {0: "正常", 1: "一级故障", 2: "二级告警", 3: "保留值"}

ALARM_NAMES = [
    "单体过压", "单体欠压", "单体过温", "单体低温",
    "电压采样线断开", "温度采样线断开", "单体压差过大", "温差过大",
    "电池包总压过高", "电池包总压过低", "辅助外设异常", "SOC 过低",
    "充电持续过流", "放电持续过流", "充电瞬时过流（保留）", "放电瞬时过流（保留）",
    "从控数据未就绪", "预充失败", "关键控制外设异常", "总压测量异常",
    "电流传感器异常", "CAN 运行异常", "外部安全回路中断事件", "Chroma 测量失联",
    "Chroma 命令失败", "从控 1 离线", "从控 2 离线", "从控 3 离线",
    "从控 4 离线", "从控 5 离线", "从控 6 离线", "IVT 包电压通道失联",
]

SWITCH_DEFS = [
    # key, Chinese label, DOC/CLI short name, firmware variable, byte, bit
    ("cell_ov", "单体过压", "ov", "ALM_CELL_OV_SWITCH", 0, 7),
    ("cell_uv", "单体欠压", "uv", "ALM_CELL_UV_SWITCH", 0, 6),
    ("cell_ot", "单体过温", "ot", "ALM_CELL_OT_SWITCH", 0, 5),
    ("cell_ut", "单体低温", "ut", "ALM_CELL_UT_SWITCH", 0, 4),
    ("cell_dv", "单体压差", "dv", "ALM_BATT_DV_SWITCH", 0, 3),
    ("cell_dt", "温差", "dt", "ALM_BATT_DT_SWITCH", 0, 2),
    ("charge_ocs", "充电持续过流", "chgocs", "ALM_CHRG_OCS_SWITCH", 0, 1),
    ("discharge_ocs", "放电持续过流", "dischocs", "ALM_DSCH_OCS_SWITCH", 0, 0),
    ("aux", "辅助外设异常", "aux", "ALM_AUX_FAIL_SWITCH", 1, 7),
    ("can_runtime", "CAN 运行异常", "canruntime", "ALM_CAN_RUNTIME_FAIL_SWITCH", 1, 6),
    ("pack_measure", "总压测量异常", "hvrel", "ALM_HVREL_FAIL_SWITCH", 1, 5),
    ("current_sensor", "电流传感器异常", "hall", "ALM_HALL_BREAK_SWITCH", 1, 4),
    ("pack_ov", "电池包总压过压", "battov", "ALM_BATT_OV_SWITCH", 1, 3),
    ("pack_uv", "电池包总压欠压", "battuv", "ALM_BATT_UV_SWITCH", 1, 2),
    ("beep", "蜂鸣器", "beep", "BeepSwitch", 1, 1),
    ("soc_low", "SOC 过低", "soclo", "ALM_BATT_UC_SWITCH", 1, 0),
    ("ivt_voltage_loss", "IVT 包电压失联", "ivtloss", "ALM_IVT_VOLT_LOSS_SWITCH", 2, 7),
    ("lv1_blocked", "一级故障全部受阻", "lv1blk", "ALM_LV1_ALL_BLOCKED_SWITCH", 2, 6),
    ("lv2_blocked", "二级告警全部受阻", "lv2blk", "ALM_LV2_ALL_BLOCKED_SWITCH", 2, 5),
]

COMMAND_RESULT_NAMES = {
    0: "已接受", 1: "已生效，等待 Flash 保存", 2: "当前状态不允许",
    3: "DLC 错误", 4: "协议版本不匹配", 5: "参数无效",
    6: "确认码错误", 7: "Flash 不可用或读取失败", 8: "日志序号越界", 9: "上一条查询仍在发送",
    10: "日志记录版本或 CRC 错误",
}

COMMAND_CODES = {
    "charge_config": 0x01,
    "alarm_thresholds": 0x02,
    "alarm_switches": 0x03,
    "fault_reset": 0x04,
    "current_direction": 0x05,
    "rtc": 0x06,
    "log_info": 0x81,
    "log_read": 0x82,
    "log_clear": 0x83,
    "charger_type": 0x84,
}


def command_ack_matches(name: str, ack: dict[str, Any]) -> bool:
    """Return whether an ACK belongs to the named command, not only its 8-bit sequence."""
    expected = COMMAND_CODES.get(name)
    return expected is not None and int(ack.get("command", -1)) == expected


def _fan_opcode_name(opcode: int) -> str:
    for name, code in FAN_COMMAND_CODES.items():
        if code == opcode:
            return name
    return f"操作码 0x{opcode:02X}"


def fan_ack_matches(name: str, ack: dict[str, Any]) -> bool:
    """Return whether a FanController ACK echoes the named command's opcode and sequence."""
    expected = FAN_COMMAND_CODES.get(name)
    return expected is not None and int(ack.get("opcode", -1)) == expected

IMD_STATUS_NAMES = {
    0: "正常", 1: "OK_HS 故障", 2: "PWM 无效", 3: "绝缘不通过",
    4: "SST Bad", 5: "40Hz 设备错误", 6: "50Hz 接地线错误",
    7: "未知频率", 8: "PWM 状态无效",
}


@dataclass(slots=True)
class CanFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool
    timestamp: float = field(default_factory=time.time)
    direction: str = "rx"


def _u16be(data: bytes, offset: int = 0) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _u16le(data: bytes, offset: int = 0) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _age(now: float, seen: float | None) -> float | None:
    return None if seen is None else round(max(0.0, now - seen), 2)


def _crc8_sae_j1850(data: bytes) -> int:
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def frame_name(arbitration_id: int, extended: bool) -> str:
    if extended:
        delta = arbitration_id - CAN1_CELL_VOLT_BASE
        if 0 <= delta <= (35 << 16) and delta & 0xFFFF == 0:
            index = delta >> 16
            return f"从控 {index // 6 + 1} 电压 {index % 6 + 1}/6"
        delta = arbitration_id - CAN1_CELL_TEMP_BASE
        if 0 <= delta <= (5 << 16) and delta & 0xFFFF == 0:
            return f"从控 {delta // 0x10000 + 1} 温度"
    return CAN1_IDS.get(arbitration_id) or CANB_IDS.get(arbitration_id) or "未定义帧"


class BmsProtocol:
    """Stateful decoder. One instance represents the attached CAN channel."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.started_monotonic = self.clock()
        self.cells: list[int | None] = [None] * CELL_COUNT
        self.cell_seen: list[float | None] = [None] * CELL_COUNT
        self.cell_reason: list[str | None] = [None] * CELL_COUNT
        self.temps: list[int | None] = [None] * TEMP_COUNT
        self.temp_seen: list[float | None] = [None] * TEMP_COUNT
        self.temp_reason: list[str | None] = [None] * TEMP_COUNT
        self.volt_frame_seen: list[list[float | None]] = [[None] * 6 for _ in range(6)]
        self.temp_frame_seen: list[float | None] = [None] * 6
        self.overview: dict[str, Any] = {
            "voltage_v": None, "current_a": None, "soc_pct": None,
            "voltage_valid": False, "current_valid": False, "soc_valid": False,
            "cell_voltage_complete": False, "temperature_complete": False,
            "state": None, "state_name": "等待数据", "alarm_level": None,
            "alarm_level_name": "等待数据", "cell_sum_v": None,
            "max_cell_mv": None, "min_cell_mv": None, "max_cell_no": None,
            "min_cell_no": None, "max_temp_c": None, "min_temp_c": None,
            "max_temp_no": None, "min_temp_no": None,
        }
        self.relay: dict[str, Any] = {}
        self.hv: dict[str, Any] = {}
        self.imd: dict[str, Any] = {}
        self.sop: dict[str, Any] = {}
        self._canb_sop_limits_data: bytes | None = None
        self.ivt: dict[str, Any] = {}
        self.config: dict[str, Any] = {"thresholds": {}, "switches": {}, "switch_version": None}
        self.fault: dict[str, Any] = {"code": 0, "version": None, "flags": {}, "slave_offline": [False] * 6}
        self.alarm_levels: list[int] = [0] * 32
        self.alarm_levels_received = False
        self.balance: list[int] = [0] * 18
        self.rtc_reply: dict[str, Any] = {}
        self.rtc_replies: dict[int, dict[str, Any]] = {}
        self.runtime_diag: dict[str, Any] = {}
        self.sensor_diag: dict[str, Any] = {}
        self.firmware: dict[str, Any] = {}
        self.command_acks: dict[int, dict[str, Any]] = {}
        self.fan: dict[str, Any] = {"status": {}, "diagnostic": {}, "curve": {}, "failsafe": {}}
        self.fan_acks: dict[int, dict[str, Any]] = {}
        self.fan_ack_history: deque[dict[str, Any]] = deque(maxlen=40)
        self.flash_log_info: dict[str, Any] = {}
        self.flash_log_records: dict[int, dict[str, Any]] = {}
        self._flash_record_parts: dict[int, dict[int, bytes]] = {}
        self.fault_history: deque[dict[str, Any]] = deque(maxlen=200)
        self._fault_seen = False
        self.raw_frames: deque[dict[str, Any]] = deque(maxlen=320)
        self.rx_count = 0
        self.tx_count = 0
        self.last_rx_monotonic: float | None = None
        self.last_summary_monotonic: float | None = None
        self.last_cell_sum_monotonic: float | None = None
        self.last_cell_extremes_monotonic: float | None = None
        self.last_fault_monotonic: float | None = None
        self.last_alarm_levels_monotonic: float | None = None
        self.last_rtc_reply_monotonic: float | None = None
        self.last_relay_command_monotonic: float | None = None
        self.last_thermal_monotonic: float | None = None
        self.last_hv_monotonic: float | None = None
        self.last_imd_monotonic: float | None = None
        self.last_runtime_diag_monotonic: float | None = None
        self.last_thresholds_monotonic: float | None = None
        self.last_switches_monotonic: float | None = None
        self.last_fan_status_monotonic: float | None = None
        self.last_fan_diagnostic_monotonic: float | None = None
        self.last_fan_curve_monotonic: float | None = None
        self.last_fan_failsafe_monotonic: float | None = None
        # Production Debug/Release firmware uses 350 ms. The identity frame
        # switches this to the 750 ms Debug-Bringup window when received.
        self.slave_sample_timeout_s = 0.35
        self.trends: deque[dict[str, Any]] = deque(maxlen=240)
        self._last_trend = 0.0

    def ingest(self, frame: CanFrame) -> None:
        now_mono = self.clock()
        if frame.direction == "tx":
            self.tx_count += 1
        else:
            self.rx_count += 1
            self.last_rx_monotonic = now_mono

        self.raw_frames.appendleft({
            "time": datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3],
            "direction": frame.direction,
            "id": f"0x{frame.arbitration_id:08X}" if frame.is_extended_id else f"0x{frame.arbitration_id:03X}",
            "extended": frame.is_extended_id,
            "dlc": len(frame.data),
            "data": " ".join(f"{byte:02X}" for byte in frame.data),
            "name": frame_name(frame.arbitration_id, frame.is_extended_id),
        })
        if frame.direction == "tx":
            return

        data = frame.data
        can_id = frame.arbitration_id
        if frame.is_extended_id and self._decode_cells(can_id, data, now_mono):
            return
        if (can_id == 0x186050F4 or (can_id == 0x4B0 and not frame.is_extended_id)) and len(data) >= 7:
            current_raw = int.from_bytes(data[2:4], "big", signed=True)
            valid = data[5]
            cell_voltage_complete = bool(valid & 0x08)
            temperature_complete = bool(valid & 0x10)
            self.overview.update({
                "voltage_v": _u16be(data) / 10.0 if valid & 0x01 else None,
                "current_a": current_raw / 10.0 if valid & 0x02 else None,
                "soc_pct": data[4] if valid & 0x04 else None,
                "voltage_valid": bool(valid & 0x01),
                "current_valid": bool(valid & 0x02),
                "soc_valid": bool(valid & 0x04),
                "cell_voltage_complete": cell_voltage_complete,
                "temperature_complete": temperature_complete,
                "state": (data[6] >> 4) & 0x0F,
                "alarm_level": data[6] & 0x0F,
            })
            if not cell_voltage_complete:
                self.overview.update({"cell_sum_v": None, "max_cell_mv": None,
                                      "min_cell_mv": None, "max_cell_no": None,
                                      "min_cell_no": None})
            if not temperature_complete:
                self.overview.update({"max_temp_c": None, "min_temp_c": None,
                                      "max_temp_no": None, "min_temp_no": None})
            self._name_overview_state()
            self.last_summary_monotonic = now_mono
        elif can_id == 0x186750F4 and len(data) >= 2:
            self.last_cell_sum_monotonic = now_mono
            self.overview["cell_sum_v"] = (_u16be(data) / 10.0
                                            if self.overview["cell_voltage_complete"] else None)
        elif can_id == 0x186150F4 and len(data) >= 6:
            self.last_cell_extremes_monotonic = now_mono
            max_cell_mv, min_cell_mv = _u16be(data), _u16be(data, 2)
            valid = (self.overview["cell_voltage_complete"]
                     and 500 <= min_cell_mv <= max_cell_mv <= 5000
                     and data[4] < CELL_COUNT and data[5] < CELL_COUNT)
            self.overview.update({"max_cell_mv": max_cell_mv if valid else None,
                                  "min_cell_mv": min_cell_mv if valid else None,
                                  "max_cell_no": data[4] + 1 if valid else None,
                                  "min_cell_no": data[5] + 1 if valid else None})
        elif can_id == 0x186250F4 and len(data) >= 5:
            max_temp, min_temp = data[0], data[1]
            valid = (self.overview["temperature_complete"]
                     and 0 <= min_temp <= max_temp <= 129
                     and data[2] < TEMP_COUNT and data[3] < TEMP_COUNT)
            self.overview.update({"max_temp_c": max_temp - 30 if valid else None,
                                  "min_temp_c": min_temp - 30 if valid else None,
                                  "max_temp_no": data[2] + 1 if valid else None,
                                  "min_temp_no": data[3] + 1 if valid else None})
            self.relay.update({"cooling": bool(data[4]), "fan_duty_pct": data[5] if len(data) > 5 else None,
                               "fan_rpm": data[6] * 100 if len(data) > 6 else None,
                               "fan_flags": data[7] if len(data) > 7 else None})
            self.last_thermal_monotonic = now_mono
        elif can_id == 0x186350F4 and len(data) >= 8:
            self.relay.update({"positive": bool((data[0] >> 6) & 0x03), "negative": bool((data[0] >> 4) & 0x03),
                               "precharge": bool((data[0] >> 2) & 0x03), "charger_state": bool(data[1] & 0x10),
                               "charger_communication": bool(data[1] & 0x08), "request_voltage_v": _u16be(data, 2) / 10.0,
                               "request_current_a": _u16be(data, 4) / 10.0, "precharge_voltage_v": _u16be(data, 6) / 10.0})
            self.last_relay_command_monotonic = now_mono
        elif (can_id == 0x187650F4 or (can_id == 0x4B1 and not frame.is_extended_id)) and len(data) >= 8:
            self._decode_fault(data, frame.timestamp)
            self.last_fault_monotonic = now_mono
        elif (can_id == 0x187850F4 or (can_id == 0x4B2 and not frame.is_extended_id)) and len(data) >= 8:
            self.alarm_levels = [(data[index // 4] >> ((index % 4) * 2)) & 0x03 for index in range(32)]
            self.alarm_levels_received = True
            self.last_alarm_levels_monotonic = now_mono
        elif can_id == 0x187750F4 and len(data) >= 6:
            self.last_thresholds_monotonic = now_mono
            self.config["thresholds"] = {"ov_mv": _u16be(data), "uv_mv": _u16be(data, 2),
                                           "ot_c": data[4] - 30, "ut_c": data[5] - 30}
        elif can_id == 0x187F50F4 and len(data) >= 4:
            self.last_switches_monotonic = now_mono
            self.config["switches"] = {key: bool(data[byte] & (1 << bit)) for key, _, _, _, byte, bit in SWITCH_DEFS}
            self.config["switch_version"] = data[3]
        elif can_id == 0x186B50F4 and len(data) >= 8:
            flags = data[1]
            feedback_flags = data[7]
            self.runtime_diag = {
                "protocol_version": data[0], "current_direction_inverted": bool(flags & 0x01),
                "charger_type": "Chroma" if flags & 0x02 else "Legacy",
                "balance_compiled": bool(flags & 0x04), "balance_enabled": bool(flags & 0x08),
                "flash_ready": bool(flags & 0x10), "config_save_pending": bool(flags & 0x20),
                "current_direction_save_pending": bool(flags & 0x40), "rtc_valid": bool(flags & 0x80),
                "charger_feedback_voltage_v": _u16be(data, 2) / 10.0,
                "charger_feedback_current_a": _u16be(data, 4) / 10.0,
                "charger_feedback_state": data[6], "charger_feedback_fresh": bool(feedback_flags & 0x80),
                "chroma_voltage_fresh": bool(feedback_flags & 0x40),
                "chroma_current_fresh": bool(feedback_flags & 0x20),
                "chroma_protect_fresh": bool(feedback_flags & 0x10),
                "chroma_output_fresh": bool(feedback_flags & 0x08),
                "chroma_output_state": feedback_flags & 0x07,
            }
            self.last_runtime_diag_monotonic = now_mono
            self.config["current_direction_inverted"] = bool(flags & 0x01)
            self.config["charger_type"] = 1 if flags & 0x02 else 0
            self.relay.update({key: value for key, value in self.runtime_diag.items()
                               if key.startswith("charger_") or key == "chroma_output_state"})
        elif can_id == 0x186C50F4 and len(data) >= 8:
            variants = {0: "Debug", 1: "Release", 2: "Debug-Bringup"}
            charger_variants = {0: "Runtime", 1: "Legacy-fixed"}
            variant = data[1] & 0x03
            charger_variant_code = (data[1] >> 2) & 0x03
            self.slave_sample_timeout_s = 0.75 if variant == 2 else 0.35
            build_date = self.firmware.get("build_date")
            self.firmware = {"protocol_version": data[0], "variant_code": variant,
                             "variant": variants.get(variant, f"未知 {variant}"),
                             "charger_variant_code": charger_variant_code,
                             "charger_variant": charger_variants.get(charger_variant_code,
                                                                       f"未知 {charger_variant_code}"),
                             "dirty": bool(data[1] & 0x80), "git": data[2:8].hex(),
                             "build_date": build_date}
        elif can_id == 0x186C51F4 and len(data) >= 4:
            # Companion identity frame: Beijing build date (year-2000, month, day).
            if not isinstance(self.firmware.get("variant"), str):
                self.firmware = {"variant_code": None, "variant": None, "dirty": None, "git": None}
            year = 2000 + data[1]
            month_days = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                          7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
            leap_year = (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)
            if not leap_year:
                month_days[2] = 28
            if data[1] <= 99 and 1 <= data[2] <= 12 and 1 <= data[3] <= month_days[data[2]]:
                self.firmware["build_date"] = f"{year:04d}-{data[2]:02d}-{data[3]:02d}"
            else:
                self.firmware["build_date"] = None
        elif can_id == 0x186D50F4 and len(data) >= 8:
            flags = data[1]
            self.sensor_diag = {
                "protocol_version": data[0], "current_online": bool(flags & 0x80),
                "current_error": bool(flags & 0x40), "u1_ready": bool(flags & 0x20),
                "u2_ready": bool(flags & 0x10), "u3_online": bool(flags & 0x08),
                "as_online": bool(flags & 0x04), "power_online": bool(flags & 0x02),
                "wh_online": bool(flags & 0x01), "soc_source": data[2],
                "soc_zero_bias_ma": int.from_bytes(data[4:8], "big", signed=True),
            }
        elif can_id == 0x186850F4 and len(data) >= 8:
            status = (data[0] >> 4) & 0x0F
            flags = data[1]
            pwm_signal_ok = bool(flags & 0x40)
            insulation_valid = bool(flags & 0x20)
            resistance_raw = _u16be(data, 4)
            duty_raw = _u16be(data, 2)
            frequency_raw = _u16be(data, 6)
            self.imd = {"status": status, "status_name": IMD_STATUS_NAMES.get(status, f"未知 {status}"),
                        "frequency_class": data[0] & 0x0F, "flags": flags,
                        "digital_ok": bool(flags & 0x80), "pwm_signal_ok": pwm_signal_ok,
                        "insulation_valid": insulation_valid, "insulation_pass": bool(flags & 0x10),
                        "sst_good": bool(flags & 0x08), "sst_bad": bool(flags & 0x04),
                        "duty_integrity_ok": bool(flags & 0x02), "pa8_level": bool(flags & 0x01),
                        # Byte2..7 contain zeroed/cleared values when the
                        # corresponding validity bit is false. Do not expose
                        # those placeholders as measurements to the UI.
                        "duty_pct": duty_raw / 10.0 if pwm_signal_ok else None,
                        "frequency_hz": frequency_raw / 100.0 if pwm_signal_ok else None,
                        "resistance_kohm": (resistance_raw if insulation_valid and resistance_raw != 0xFFFF else None),
                        "resistance_saturated": bool(insulation_valid and resistance_raw == 0xFFFF),
                        "resistance_raw_kohm": resistance_raw}
            self.last_imd_monotonic = now_mono
        elif can_id == 0x186950F4 and len(data) >= 5:
            results = {0: "未发生", 1: "成功", 2: "失败"}
            self.hv = {"hv_acc": bool(data[0] & 1), "charge_button": bool(data[0] & 2),
                       "precharge_result": (data[0] >> 2) & 0x03,
                       "precharge_result_name": results.get((data[0] >> 2) & 0x03, "保留值"),
                       "success_ms": _u16be(data, 1), "failure_ms": _u16be(data, 3)}
            self.last_hv_monotonic = now_mono
        elif can_id == 0x186A50F4 and len(data) >= 8:
            self.sop = {"discharge_current_a": _u16be(data) / 10.0, "charge_current_a": _u16be(data, 2) / 10.0,
                        "discharge_power_kw": _u16be(data, 4) / 10.0, "charge_power_kw": _u16be(data, 6) / 10.0}
        elif can_id == 0x4A0 and not frame.is_extended_id and len(data) >= 8:
            self._canb_sop_limits_data = bytes(data[:8])
            self.sop.update({"discharge_current_a": _u16le(data) / 10.0, "charge_current_a": _u16le(data, 2) / 10.0,
                             "discharge_power_kw": _u16le(data, 4) / 10.0, "charge_power_kw": _u16le(data, 6) / 10.0,
                             "source": "CANB"})
        elif can_id == 0x4A3 and not frame.is_extended_id and len(data) >= 8:
            flags, intervention = data[1], data[6]
            crc_input = (bytes.fromhex("04 A0") + self._canb_sop_limits_data + bytes.fromhex("04 A3") + data[:7]
                         if self._canb_sop_limits_data is not None else None)
            self.sop["status"] = {
                "protocol_version": data[0] >> 4, "sequence": data[0] & 0x0F,
                "limits_valid": bool(flags & 0x01), "drive_allowed": bool(flags & 0x02),
                "regen_allowed": bool(flags & 0x04), "intervention_active": bool(flags & 0x08),
                "fault_latched": bool(flags & 0x10), "ack_required": bool(flags & 0x20),
                "limits_reduced": bool(flags & 0x40), "bms_state": data[2],
                "limit_reason": _u16le(data, 3), "input_health": data[5],
                "intervention_level": intervention & 0x03,
                "discharge_intervention": bool(intervention & 0x04), "charge_intervention": bool(intervention & 0x08),
                "waiting_ack": bool(intervention & 0x10), "ack_fresh": bool(intervention & 0x20),
                "current_below_exit": bool(intervention & 0x40),
                "crc_valid": crc_input is not None and _crc8_sae_j1850(crc_input) == data[7],
            }
        elif can_id == 0x4A4 and not frame.is_extended_id and len(data) >= 8:
            self.sop["ecu_ack"] = {
                "protocol_version": data[0] >> 4, "sequence": data[0] & 0x0F, "flags": data[1],
                "pair_valid": bool(data[1] & 0x01), "limits_applied": bool(data[1] & 0x02),
                "zero_torque": bool(data[1] & 0x04), "ecu_fault": bool(data[1] & 0x08),
                "discharge_power_kw": _u16le(data, 2) / 10.0, "regen_power_kw": _u16le(data, 4) / 10.0,
                "limit_source": data[6], "crc_valid": _crc8_sae_j1850(bytes.fromhex("04 A4") + data[:7]) == data[7],
            }
        elif can_id in (0x186450F4, 0x186550F4, 0x186650F4) and len(data) >= 6:
            offset = ((can_id - 0x186450F4) >> 16) * 6
            self.balance[offset:offset + 6] = list(data[:6])
        elif can_id == 0x18A450F4 and len(data) >= 8:
            reply = {"status": data[0], "sequence": data[1],
                     "year": 2000 + data[2], "month": data[3], "day": data[4],
                     "hour": data[5], "minute": data[6], "second": data[7]}
            self.rtc_reply = reply
            self.rtc_replies[data[1]] = dict(reply)
            self.last_rtc_reply_monotonic = now_mono
            if len(self.rtc_replies) > 64:
                self.rtc_replies.pop(next(iter(self.rtc_replies)))
        elif can_id == 0x18A650F4 and len(data) >= 8 and data[0] == TOOL_PROTOCOL_VERSION:
            flags = data[5]
            ack = {"protocol_version": data[0], "sequence": data[1], "command": data[2],
                   "result": data[3], "result_name": COMMAND_RESULT_NAMES.get(data[3], f"未知 {data[3]}"),
                   "accepted": data[3] in (0, 1), "bms_state": data[4],
                   "flags": {"flash_ready": bool(flags & 0x01), "config_save_pending": bool(flags & 0x02),
                             "current_direction_save_pending": bool(flags & 0x04),
                             "log_clear_pending": bool(flags & 0x08), "error_log_write_pending": bool(flags & 0x10),
                             "protection_disable_allowed": bool(flags & 0x20)},
                   "detail": _u16be(data, 6), "timestamp": frame.timestamp}
            self.command_acks[data[1]] = ack
            self.fault.setdefault("flags", {})["log_clear_pending"] = bool(flags & 0x08)
            if len(self.command_acks) > 64:
                self.command_acks.pop(next(iter(self.command_acks)))
        elif can_id == 0x18A750F4 and len(data) >= 8 and data[0] == TOOL_PROTOCOL_VERSION:
            sequence, response_type = data[1], data[2]
            if response_type == 1:
                self.flash_log_info = {"count": _u16be(data, 3), "dropped": _u16be(data, 5),
                                       "status_flags": data[7], "sequence": sequence}
            elif response_type == 2 and data[3] < 4:
                parts = self._flash_record_parts.setdefault(sequence, {})
                parts[data[3]] = bytes(data[4:8])
                if len(parts) == 4:
                    raw = b"".join(parts[index] for index in range(4))
                    ack = self.command_acks.get(sequence, {})
                    index = int(ack.get("detail", 0))
                    year = 2000 + raw[0] if raw[0] else None
                    self.flash_log_records[index] = {
                        "index": index, "timestamp": (f"{year:04d}-{raw[1]:02d}-{raw[2]:02d} "
                                                       f"{raw[3]:02d}:{raw[4]:02d}:{raw[5]:02d}") if year else "RTC 未校时",
                        "fault_code": f"0x{int.from_bytes(raw[6:10], 'big'):08X}",
                        "event_type": raw[10], "event_detail": raw[11], "record_version": raw[12],
                        "raw": raw.hex(" ").upper(),
                    }
                    self._flash_record_parts.pop(sequence, None)
        elif not frame.is_extended_id and 0x512 <= can_id <= 0x519 and len(data) >= 6:
            self._decode_ivt(can_id, data)
        elif can_id == 0x18FF50E5 and len(data) >= 5:
            self.relay.update({"charger_feedback_voltage_v": _u16be(data) / 10.0,
                               "charger_feedback_current_a": _u16be(data, 2) / 10.0,
                               "charger_feedback_state": data[4]})
        elif can_id in (0x401, 0x402) and not frame.is_extended_id and len(data) >= 7:
            charge_flags = self.fault.get("flags", {})
            if charge_flags.get("charge_mode") and charge_flags.get("charger_type") == "Chroma":
                value = struct.unpack("<f", data[3:7])[0]
                if value >= 0.0 and value < 10000.0:
                    key = "charger_feedback_voltage_v" if can_id == 0x401 else "charger_feedback_current_a"
                    self.relay[key] = round(value, 3)
        elif can_id == 0x404 and not frame.is_extended_id and len(data) >= 7:
            protect = int.from_bytes(data[3:7], "little")
            self.relay.update({"chroma_protect_bits": protect, "charger_feedback_state": 1 if protect else 0})
        elif can_id == 0x405 and not frame.is_extended_id and len(data) >= 4:
            self.relay["chroma_output_state"] = data[3]
        elif can_id == FAN_STATUS_ID and not frame.is_extended_id and len(data) >= 8:
            self.fan["status"] = {
                "rpm": [_u16be(data), _u16be(data, 2), _u16be(data, 4)],
                "duty_pct": [data[6], data[7]],
            }
            self.last_fan_status_monotonic = now_mono
        elif can_id == FAN_DIAGNOSTIC_ID and not frame.is_extended_id and len(data) >= 8:
            flags = data[1]
            motor_raw = int.from_bytes(data[2:4], "big", signed=True)
            controller_raw = int.from_bytes(data[4:6], "big", signed=True)
            self.fan["diagnostic"] = {
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
            self.last_fan_diagnostic_monotonic = now_mono
        elif can_id == FAN_COMMAND_ACK_ID and not frame.is_extended_id and len(data) >= 8:
            mode, failsafe = data[3] & 0x03, (data[3] >> 4) & 0x03
            ack = {
                "opcode": data[0], "opcode_name": _fan_opcode_name(data[0]),
                "sequence": data[1], "result": data[2],
                "result_name": FAN_RESULT_NAMES.get(data[2], f"未知 {data[2]}"),
                "accepted": data[2] == 0, "mode": mode,
                "mode_name": FAN_MODE_NAMES.get(mode, "未知"),
                "failsafe": failsafe,
                "failsafe_name": FAN_FAILSAFE_NAMES.get(failsafe, "未知"),
                "duty_pct": [data[4], data[5]], "target_pct": [data[6], data[7]],
            }
            self.fan_acks[data[1]] = ack
            if len(self.fan_acks) > 64:
                self.fan_acks.pop(next(iter(self.fan_acks)))
            self.fan_ack_history.appendleft({
                "time": datetime.fromtimestamp(frame.timestamp).strftime("%H:%M:%S.%f")[:-3],
                **{key: ack[key] for key in ("opcode_name", "sequence", "result", "result_name",
                                              "mode_name", "failsafe_name", "accepted")},
                "duty_pct": list(ack["duty_pct"]), "target_pct": list(ack["target_pct"]),
            })
        elif can_id == FAN_CURVE_STATUS_ID and not frame.is_extended_id and len(data) >= 5:
            self.fan["curve"] = {
                "temp_off_c": data[0], "temp_on_c": data[1], "temp_full_c": data[2],
                "min_duty_pct": data[3], "ramp_up_pct_per_s": data[4],
            }
            self.last_fan_curve_monotonic = now_mono
        elif can_id == FAN_FAILSAFE_STATUS_ID and not frame.is_extended_id and len(data) >= 7:
            self.fan["failsafe"] = {
                "failsafe": data[0],
                "failsafe_name": FAN_FAILSAFE_NAMES.get(data[0], f"未知 {data[0]}"),
                "fallback1_duty_pct": data[1], "fallback2_duty_pct": data[2],
                "stale_hold_s": data[3], "ramp_down_pct_per_s": data[4],
                "mode": data[5], "mode_name": FAN_MODE_NAMES.get(data[5], "未知"),
                "lease_remaining_s": data[6],
            }
            self.last_fan_failsafe_monotonic = now_mono

        if now_mono - self._last_trend >= 0.45:
            relay_age = _age(now_mono, self.last_relay_command_monotonic)
            summary_age = _age(now_mono, self.last_summary_monotonic)
            summary_fresh = summary_age is not None and summary_age <= 1.5
            relay_fresh = relay_age is not None and relay_age <= 1.5
            if summary_fresh or relay_fresh:
                self._last_trend = now_mono
                self.trends.append({
                    "t": round(now_mono - self.started_monotonic, 1),
                    "voltage": self.overview.get("voltage_v") if summary_fresh and self.overview.get("voltage_valid") else None,
                    "current": self.overview.get("current_a") if summary_fresh and self.overview.get("current_valid") else None,
                    "soc": self.overview.get("soc_pct") if summary_fresh and self.overview.get("soc_valid") else None,
                    "precharge": self.relay.get("precharge_voltage_v") if relay_fresh else None,
                })

    def _decode_cells(self, can_id: int, data: bytes, now: float) -> bool:
        delta = can_id - CAN1_CELL_VOLT_BASE
        if 0 <= delta <= (35 << 16) and delta & 0xFFFF == 0:
            index = delta >> 16
            slave, frame_index = divmod(index, 6)
            if frame_index == 0:
                start = slave * 23
                count = 3
            else:
                start = slave * 23 + 3 + (frame_index - 1) * 4
                count = 4
            if len(data) < 8:
                for offset in range(count):
                    cell = start + offset
                    self.cells[cell] = 0xFFFF
                    self.cell_seen[cell] = now
                    self.cell_reason[cell] = "DLC不足"
                self.volt_frame_seen[slave][frame_index] = None
                return True
            values = ([_u16le(data, 2), _u16le(data, 4), _u16le(data, 6)] if frame_index == 0
                      else [_u16le(data, offset) for offset in (0, 2, 4, 6)])
            valid = all(500 <= value <= 5000 for value in values)
            for offset, raw_value in enumerate(values):
                cell = start + offset
                self.cells[cell] = raw_value if valid else 0xFFFF
                self.cell_seen[cell] = now
                self.cell_reason[cell] = None if valid else "范围错误"
            self.volt_frame_seen[slave][frame_index] = now if valid else None
            return True
        delta = can_id - CAN1_CELL_TEMP_BASE
        if 0 <= delta <= (5 << 16) and delta & 0xFFFF == 0:
            slave = delta >> 16
            valid_length = len(data) >= 8
            valid_values = valid_length and all(value <= 129 for value in data[:8])
            reason = None if valid_values else ("DLC不足" if not valid_length else "范围错误")
            for offset in range(8):
                index = slave * 8 + offset
                self.temps[index] = data[offset] if valid_values else 0xFF
                self.temp_seen[index] = now
                self.temp_reason[index] = reason
            self.temp_frame_seen[slave] = now if valid_values else None
            return True
        return False

    def _decode_fault(self, data: bytes, timestamp: float) -> None:
        state, alarm_level = (data[0] >> 4) & 0x0F, data[0] & 0x0F
        code = int.from_bytes(data[1:5], "big")
        previous = int(self.fault.get("code", 0))
        if (self._fault_seen and code != previous) or (not self._fault_seen and code != 0):
            added_bits = code & ~previous
            cleared_bits = previous & ~code
            self.fault_history.appendleft({
                "time": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "code": f"0x{code:08X}", "previous": f"0x{previous:08X}",
                "added": [ALARM_NAMES[i] for i in range(32) if added_bits & (1 << i)],
                "cleared": [ALARM_NAMES[i] for i in range(32) if cleared_bits & (1 << i)],
                "state": STATE_NAMES.get(state, f"未知 {state}"),
            })
        self._fault_seen = True
        flags = data[5]
        self.overview.update({"state": state, "alarm_level": alarm_level})
        self._name_overview_state()
        self.fault = {
            "code": code, "code_hex": f"0x{code:08X}", "version": data[7],
            "state": state, "state_name": STATE_NAMES.get(state, f"未知 {state}"),
            "alarm_level": alarm_level,
            "alarm_level_name": ALARM_LEVEL_NAMES.get(alarm_level, "未知"),
            "flags": {"latched": bool(flags & 0x80), "bms_output_latched": bool(flags & 0x40),
                      "reset_pending": bool(flags & 0x20), "log_write_pending": bool(flags & 0x10),
                      "log_clear_pending": bool(flags & 0x08), "charge_mode": bool(flags & 0x04),
                      "charger_type": "Chroma" if flags & 0x02 else "Legacy"},
            "slave_offline": [bool(data[6] & (1 << i)) for i in range(6)],
        }

    def _decode_ivt(self, can_id: int, data: bytes) -> None:
        mux = can_id - 0x512
        if data[0] != mux:
            return
        status = (data[1] >> 4) & 0x0F
        value = int.from_bytes(data[2:6], "little", signed=True)
        keys = ["current_a", "u1_v", "u2_v", "u3_v", "temperature_c", "power_w", "charge_as", "energy_wh"]
        scales = [0.001, 0.001, 0.001, 0.001, 0.1, 1.0, 1.0, 1.0]
        self.ivt[keys[mux]] = value * scales[mux]
        self.ivt.update({"last_channel": keys[mux], "status": status, "counter": data[1] & 0x0F, "valid": status == 0})

    def _name_overview_state(self) -> None:
        self.overview["state_name"] = STATE_NAMES.get(self.overview.get("state"), f"未知 {self.overview.get('state')}")
        self.overview["alarm_level_name"] = ALARM_LEVEL_NAMES.get(self.overview.get("alarm_level"), "未知")

    def fan_state(self) -> dict[str, Any]:
        """Return the FanController view with freshness ages for the fan tool page."""
        now = self.clock()
        fan = {key: dict(value) for key, value in self.fan.items()}
        fan["status_age"] = _age(now, self.last_fan_status_monotonic)
        fan["diagnostic_age"] = _age(now, self.last_fan_diagnostic_monotonic)
        fan["curve_age"] = _age(now, self.last_fan_curve_monotonic)
        fan["failsafe_age"] = _age(now, self.last_fan_failsafe_monotonic)
        return {"fan": fan, "fan_ack_history": list(self.fan_ack_history)}

    def snapshot(self, connection: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        raw_cell_data_available = connection.get("bus_profile", "can1") == "can1"
        cell_values = []
        temp_values = []
        modules = []
        if raw_cell_data_available:
            for index, value in enumerate(self.cells):
                age = _age(now, self.cell_seen[index])
                valid = (value is not None and value != 0xFFFF and age is not None
                         and age <= self.slave_sample_timeout_s)
                cell_values.append({"no": index + 1, "module": index // 23 + 1, "local": index % 23 + 1,
                                    "value": value if valid else None, "raw": value, "age": age,
                                    "status": self.cell_reason[index] or ("断线" if value == 0xFFFF else ("过期" if value is not None and not valid else "正常" if valid else "未收到"))})
            for index, value in enumerate(self.temps):
                age = _age(now, self.temp_seen[index])
                valid = (value is not None and value != 0xFF and age is not None
                         and age <= self.slave_sample_timeout_s)
                temp_values.append({"no": index + 1, "module": index // 8 + 1, "local": index % 8 + 1,
                                    "value": value - 30 if valid else None, "raw": value, "age": age,
                                    "status": self.temp_reason[index] or ("断线" if value == 0xFF else ("过期" if value is not None and not valid else "正常" if valid else "未收到"))})
            for slave in range(6):
                ages = [_age(now, seen) for seen in self.volt_frame_seen[slave]]
                temp_age = _age(now, self.temp_frame_seen[slave])
                online = (all(age is not None and age <= self.slave_sample_timeout_s for age in ages)
                          and temp_age is not None and temp_age <= self.slave_sample_timeout_s)
                modules.append({"no": slave + 1, "online": online,
                                "voltage_frames": sum(age is not None and age <= self.slave_sample_timeout_s for age in ages),
                                "temperature_frame": temp_age is not None and temp_age <= self.slave_sample_timeout_s,
                                "age": max([age for age in ages if age is not None] + ([temp_age] if temp_age is not None else [0]))})
        alarms = [{"index": i, "name": ALARM_NAMES[i], "level": self.alarm_levels[i],
                   "level_name": ALARM_LEVEL_NAMES[self.alarm_levels[i]], "in_fault_code": bool(self.fault.get("code", 0) & (1 << i))}
                  for i in range(32)]
        for alarm in alarms:
            alarm["received"] = self.alarm_levels_received
            alarm["age"] = _age(now, self.last_alarm_levels_monotonic)
        hv = dict(self.hv)
        hv["age"] = _age(now, self.last_hv_monotonic)
        relay = dict(self.relay)
        relay["command_age"] = _age(now, self.last_relay_command_monotonic)
        relay["thermal_age"] = _age(now, self.last_thermal_monotonic)
        imd = dict(self.imd)
        imd["age"] = _age(now, self.last_imd_monotonic)
        runtime_diag = dict(self.runtime_diag)
        runtime_diag["age"] = _age(now, self.last_runtime_diag_monotonic)
        fault = dict(self.fault)
        fault["received"] = self.last_fault_monotonic is not None
        fault["age"] = _age(now, self.last_fault_monotonic)
        rtc_reply = dict(self.rtc_reply)
        rtc_reply["age"] = _age(now, self.last_rtc_reply_monotonic)
        overview = dict(self.overview)
        overview["cell_sum_age"] = _age(now, self.last_cell_sum_monotonic)
        overview["cell_extremes_age"] = _age(now, self.last_cell_extremes_monotonic)
        config = {
            **self.config,
            "thresholds": dict(self.config.get("thresholds", {})),
            "switches": dict(self.config.get("switches", {})),
            "thresholds_age": _age(now, self.last_thresholds_monotonic),
            "switches_age": _age(now, self.last_switches_monotonic),
            "runtime_age": _age(now, self.last_runtime_diag_monotonic),
        }
        return {
            "connection": {**connection, "rx_count": self.rx_count, "tx_count": self.tx_count,
                           "last_rx_age": _age(now, self.last_rx_monotonic),
                           "summary_age": _age(now, self.last_summary_monotonic)},
            "overview": overview, "relay": relay, "hv": hv, "imd": imd,
            "sop": dict(self.sop), "ivt": dict(self.ivt), "config": config, "fault": fault,
            "alarms": alarms, "cells": cell_values, "temps": temp_values, "modules": modules,
            "raw_cell_data_available": raw_cell_data_available,
            "slave_sample_timeout_s": self.slave_sample_timeout_s,
            "balance": list(self.balance), "rtc_reply": rtc_reply,
            "runtime_diag": runtime_diag, "sensor_diag": dict(self.sensor_diag),
            "firmware": dict(self.firmware), "flash_log_info": dict(self.flash_log_info),
            "flash_log_records": [self.flash_log_records[key] for key in sorted(self.flash_log_records)],
            "fault_history": list(self.fault_history), "raw_frames": list(self.raw_frames), "trends": list(self.trends),
        }


def build_command(name: str, values: dict[str, Any] | None = None) -> CanFrame:
    """Build a validated CAN1 tool command.

    Values presented by the UI use engineering units (V, A, degrees Celsius).
    """
    values = values or {}
    now = time.time()
    sequence = int(values.get("_sequence", 0)) & 0xFF
    operation = {
        "charge_config": 0x01,
        "alarm_thresholds": 0x02,
        "alarm_switches": 0x03,
        "fault_reset": 0x04,
        "current_direction": 0x05,
        "rtc": 0x06,
        "maintenance": 0x0F,
    }

    def request_header(code: int) -> bytearray:
        return bytearray([(TOOL_PROTOCOL_VERSION << 4) | (code & 0x0F), sequence])

    if name == "charge_config":
        voltage = round(float(values["voltage_v"]) * 10)
        current = round(float(values["current_a"]) * 10)
        if not 4154 <= voltage <= 5782 or not 0 <= current <= 45:
            raise ValueError("充电请求范围为 415.4..578.2 V、0..4.5 A")
        data = request_header(operation["charge_config"])
        data.extend(voltage.to_bytes(2, "big") + current.to_bytes(2, "big"))
        data.extend(b"\x00\x00")
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(data), True, now, "tx")
    if name == "alarm_thresholds":
        ov, uv = int(values["ov_mv"]), int(values["uv_mv"])
        ot, ut = int(values["ot_c"]) + 30, int(values["ut_c"]) + 30
        if not (3011 <= ov <= 4190 and 3010 <= uv <= 4189 and ov > uv):
            raise ValueError("电压阈值超出范围，或过压阈值未高于欠压阈值")
        if not (36 <= ot <= 95 and 5 <= ut <= 79 and ot > ut):
            raise ValueError("温度阈值超出范围，或过温阈值未高于低温阈值")
        data = request_header(operation["alarm_thresholds"])
        data.extend(ov.to_bytes(2, "big") + uv.to_bytes(2, "big") + bytes([ot, ut]))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(data), True, now, "tx")
    if name == "alarm_switches":
        switches = values.get("switches", values)
        data = bytearray(3)
        for key, _, _, _, byte, bit in SWITCH_DEFS:
            if bool(switches.get(key)):
                data[byte] |= 1 << bit
        request = request_header(operation["alarm_switches"])
        request.extend(data)
        request.extend(b"\x00\x00\x00")
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "fault_reset":
        request = request_header(operation["fault_reset"])
        request.extend(bytes.fromhex("A5 5A 3C 00 00 00"))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "current_direction":
        request = request_header(operation["current_direction"])
        request.extend(bytes([1 if values.get("inverted") else 0, 0, 0, 0, 0, 0]))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "log_info":
        request = request_header(operation["maintenance"])
        request.extend(bytes([1, 0, 0, 0, 0, 0]))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "log_read":
        index = int(values.get("index", -1))
        if not 0 <= index <= 0xFFFF:
            raise ValueError("故障日志序号必须在 0..65535")
        request = request_header(operation["maintenance"])
        request.extend(bytes([2]) + index.to_bytes(2, "big") + b"\x00\x00\x00")
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "log_clear":
        request = request_header(operation["maintenance"])
        request.extend(bytes.fromhex("03 C3 3C A5 00 00"))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "charger_type":
        charger_type = int(values.get("charger_type", -1))
        if charger_type not in (0, 1):
            raise ValueError("充电机类型必须是 0=Legacy 或 1=Chroma")
        request = request_header(operation["maintenance"])
        request.extend(bytes([4, charger_type, 0, 0, 0, 0]))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    if name == "rtc":
        value = values.get("datetime")
        dt = datetime.fromisoformat(value) if value else datetime.now()
        if not 2000 <= dt.year <= 2099:
            raise ValueError("RTC 年份必须在 2000..2099")
        request = request_header(operation["rtc"])
        request.extend(bytes([dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second]))
        return CanFrame(CAN1_COMMAND_REQ_EXT_ID, bytes(request), True, now, "tx")
    raise ValueError(f"未知命令：{name}")


def build_fan_command(name: str, values: dict[str, Any] | None = None) -> CanFrame:
    """Build a validated FanController command frame (CANB 0x5A4, DLC 8).

    Byte layout: opcode, sequence, five parameters, CRC-8/SAE-J1850 over
    bytes 0..6.  Ranges mirror fan_controller.c process_command().
    """
    values = values or {}
    sequence = int(values.get("_sequence", 0)) & 0xFF

    def command_frame(opcode: int, parameters: bytes) -> CanFrame:
        body = bytearray([opcode, sequence, *parameters])
        body.append(_crc8_sae_j1850(body))
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
    if name == "fan_curve":
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
        return command_frame(0x02, bytes([temp_off, temp_on, temp_full, min_duty, ramp_up]))
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
    if name == "fan_restore_defaults":
        return command_frame(0x04, bytes([0xA5, 0, 0, 0, 0]))
    if name == "fan_query":
        return command_frame(0x05, bytes([0, 0, 0, 0, 0]))
    raise ValueError(f"未知风扇命令：{name}")


def switch_catalog() -> list[dict[str, Any]]:
    return [{"key": key, "name": label, "code": code, "variable": variable}
            for key, label, code, variable, _, _ in SWITCH_DEFS]
