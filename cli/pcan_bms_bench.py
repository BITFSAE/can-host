#!/usr/bin/env python3
"""BMS F405 主控台架联调脚本（PCAN 版）.

用途：
1. 在 CAN1 500kbps 总线上模拟 6 个从控电压/温度报文；
2. 监听主控发出的状态帧，便于台架联调；
3. 通过简单命令行交互注入从控离线、断线和温度故障；
4. 查看主控最近一次回帧摘要，辅助告警/Flash 持久化测试。

IVT-S 不由本脚本模拟。测试时把真实 IVT-S 接到 F405 的 CAN1；本脚本不发送
`0x512`。需要在 CANB 上模拟 ECU SOP 确认时，可使用 `--canb-sop-ack`，该模式
仍然只发送 `0x4A4`，不伪造 IVT 结果帧。

依赖：
    pip install python-can

运行前需安装 PEAK 驱动与 PCANBasic。
默认覆盖 CAN1 从控台架测试。加 `--canb-sop-ack` 后切换到 CANB，只校验 SOP
并发送 ECU 确认。
"""

from __future__ import annotations

import argparse
import dataclasses
import queue
import shlex
import sys
import threading
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:
    import can
except ImportError:
    can = None


CAN1_BITRATE = 500000
SLAVE_VOLT_BASE_ID = 0x180050F3
SLAVE_TEMP_BASE_ID = 0x184050F3

# 主控发送帧 ID（F405）
BMS_TOTAL_ID      = 0x186050F4   # 总压/电流/SOC/状态
BMS_CELL_MAX_V_ID = 0x186150F4   # 最高/最低单体电压
BMS_CELL_MAX_T_ID = 0x186250F4   # 最高/最低温度
BMS_RELAY_ID      = 0x186350F4   # 继电器/充电请求
BMS_CELL_SUM_ID   = 0x186750F4   # 单体累加电压
BMS_IMD_DIAG_ID   = 0x186850F4   # IMD 诊断
BMS_FAULT_ID      = 0x187650F4   # 统一故障状态帧
BMS_ALARM_LEVEL_ID = 0x187850F4  # 告警等级明细
CAN2_PACK_STATUS_ID = 0x4B0   # CAN2/CANB BMS 包状态
CAN2_FAULT_ID      = 0x4B1       # CAN2/CANB 统一故障状态镜像
CAN2_ALARM_LEVEL_ID = 0x4B2      # CAN2/CANB 告警等级明细镜像
CAN2_SOP_LIMITS_ID   = 0x4A0
CAN2_SOP_STATUS_ID   = 0x4A3
CAN2_SOP_ACK_ID      = 0x4A4
BMS_THRESHOLD_ID  = 0x187750F4   # 告警阈值
BMS_SWITCH_ID     = 0x187F50F4   # 告警开关

CELL_COUNT_PER_SLAVE = 23
TEMP_COUNT_PER_SLAVE = 8
SLAVE_COUNT = 6

TX_VOLT_PERIOD_S = 0.20
TX_TEMP_PERIOD_S = 0.50


@dataclasses.dataclass
class SlaveState:
    slave_id: int
    online: bool = True
    base_cell_mv: int = 3850
    base_temp_c: int = 25
    cell_offsets_mv: List[int] = dataclasses.field(
        default_factory=lambda: [0] * CELL_COUNT_PER_SLAVE
    )
    temp_offsets_c: List[int] = dataclasses.field(
        default_factory=lambda: [0] * TEMP_COUNT_PER_SLAVE
    )


@dataclasses.dataclass
class BmsMonitorState:
    total_data: Optional[List[int]] = None
    relay_data: Optional[List[int]] = None
    fault_data: Optional[List[int]] = None
    alarm_level_data: Optional[List[int]] = None
    threshold_data: Optional[List[int]] = None
    switch_data: Optional[List[int]] = None
    cell_sum_data: Optional[List[int]] = None
    imd_diag_data: Optional[List[int]] = None
    cell_max_v_data: Optional[List[int]] = None
    cell_max_t_data: Optional[List[int]] = None
    sop_limits_data: Optional[List[int]] = None
    sop_status_data: Optional[List[int]] = None
    sop_ack_count: int = 0


def crc8_sae_j1850(data: Sequence[int]) -> int:
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def build_sop_ack(limits: Sequence[int], status: Sequence[int]) -> can.Message:
    seq_version = status[0]
    limits_valid = bool(status[1] & 0x01)
    p_dis = limits[4] | (limits[5] << 8)
    p_chg = limits[6] | (limits[7] << 8)
    data = [seq_version, 0x03,
            p_dis & 0xFF, (p_dis >> 8) & 0xFF,
            p_chg & 0xFF, (p_chg >> 8) & 0xFF,
            0 if limits_valid else 7]
    data.append(crc8_sae_j1850([0x04, 0xA4, *data]))
    return can.Message(arbitration_id=CAN2_SOP_ACK_ID,
                       is_extended_id=False, data=data)


class BenchModel:
    """F405 CAN1 从控台架模型；IVT 使用同一 CAN1 上的真实设备。"""

    def __init__(self) -> None:
        self.slaves: List[SlaveState] = [
            SlaveState(slave_id=index + 1) for index in range(SLAVE_COUNT)
        ]
        self.open_wire_cells: Set[int] = set()
        self.open_wire_temps: Set[int] = set()

    def reset(self) -> None:
        self.__init__()

    def _cell_position(self, global_cell_index: int) -> Tuple[SlaveState, int]:
        if global_cell_index < 1 or global_cell_index > SLAVE_COUNT * CELL_COUNT_PER_SLAVE:
            raise ValueError("单体编号超出范围，应为 1..138")
        zero_based = global_cell_index - 1
        slave = self.slaves[zero_based // CELL_COUNT_PER_SLAVE]
        local = zero_based % CELL_COUNT_PER_SLAVE
        return slave, local

    def _temp_position(self, global_temp_index: int) -> Tuple[SlaveState, int]:
        if global_temp_index < 1 or global_temp_index > SLAVE_COUNT * TEMP_COUNT_PER_SLAVE:
            raise ValueError("温度编号超出范围，应为 1..48")
        zero_based = global_temp_index - 1
        slave = self.slaves[zero_based // TEMP_COUNT_PER_SLAVE]
        local = zero_based % TEMP_COUNT_PER_SLAVE
        return slave, local

    def get_cell_mv(self, global_cell_index: int) -> int:
        slave, local = self._cell_position(global_cell_index)
        return slave.base_cell_mv + slave.cell_offsets_mv[local]

    def set_cell_mv(self, global_cell_index: int, cell_mv: int) -> None:
        slave, local = self._cell_position(global_cell_index)
        slave.cell_offsets_mv[local] = cell_mv - slave.base_cell_mv

    def get_temp_c(self, global_temp_index: int) -> int:
        slave, local = self._temp_position(global_temp_index)
        return slave.base_temp_c + slave.temp_offsets_c[local]

    def set_temp_c(self, global_temp_index: int, temp_c: int) -> None:
        slave, local = self._temp_position(global_temp_index)
        slave.temp_offsets_c[local] = temp_c - slave.base_temp_c

    def reset_cell_mv(self, global_cell_index: int) -> None:
        slave, _ = self._cell_position(global_cell_index)
        self.set_cell_mv(global_cell_index, slave.base_cell_mv)

    def reset_temp_c(self, global_temp_index: int) -> None:
        slave, _ = self._temp_position(global_temp_index)
        self.set_temp_c(global_temp_index, slave.base_temp_c)

    def set_open_wire_cell(self, global_cell_index: int, enabled: bool) -> None:
        self._cell_position(global_cell_index)
        if enabled:
            self.open_wire_cells.add(global_cell_index)
        else:
            self.open_wire_cells.discard(global_cell_index)

    def set_open_wire_temp(self, global_temp_index: int, enabled: bool) -> None:
        self._temp_position(global_temp_index)
        if enabled:
            self.open_wire_temps.add(global_temp_index)
        else:
            self.open_wire_temps.discard(global_temp_index)

    def estimated_pack_voltage_v(self) -> float:
        total_mv = 0
        for ci in range(1, SLAVE_COUNT * CELL_COUNT_PER_SLAVE + 1):
            if ci not in self.open_wire_cells:
                total_mv += self.get_cell_mv(ci)
        return total_mv / 1000.0


def u16_le(value: int) -> Tuple[int, int]:
    value = max(0, min(0xFFFF, int(value)))
    return value & 0xFF, (value >> 8) & 0xFF


# ── 发送帧构建 ──────────────────────────────────────────────

def build_slave_voltage_frames(model: BenchModel, slave: SlaveState) -> List[can.Message]:
    frames: List[can.Message] = []
    if not slave.online:
        return frames
    si = slave.slave_id - 1
    cell_values: List[int] = []
    for li in range(CELL_COUNT_PER_SLAVE):
        gc = si * CELL_COUNT_PER_SLAVE + li + 1
        cell_values.append(0xFFFF if gc in model.open_wire_cells else model.get_cell_mv(gc))
    chunks = [cell_values[0:3], cell_values[3:7], cell_values[7:11],
              cell_values[11:15], cell_values[15:19], cell_values[19:23]]
    for fi, chunk in enumerate(chunks):
        can_id = SLAVE_VOLT_BASE_ID + ((si * 6 + fi) << 16)
        payload = [0] * 8
        offset = 2 if fi == 0 else 0
        for ci, mv in enumerate(chunk):
            lo, hi = u16_le(mv)
            payload[offset + ci * 2] = lo
            payload[offset + ci * 2 + 1] = hi
        frames.append(can.Message(arbitration_id=can_id, is_extended_id=True, data=payload))
    return frames


def build_slave_temp_frame(model: BenchModel, slave: SlaveState) -> Optional[can.Message]:
    if not slave.online:
        return None
    si = slave.slave_id - 1
    payload: List[int] = []
    for li in range(TEMP_COUNT_PER_SLAVE):
        gt = si * TEMP_COUNT_PER_SLAVE + li + 1
        if gt in model.open_wire_temps:
            payload.append(0xFF)
            continue
        payload.append(max(0, min(0xFE, model.get_temp_c(gt) + 30)))
    return can.Message(
        arbitration_id=SLAVE_TEMP_BASE_ID + (si << 16),
        is_extended_id=True, data=payload)


# ── BMS 回帧解码（F405 格式）────────────────────────────────

FAULT_BIT_NAMES = [
    (0,  "OV"), (1,  "UV"), (2,  "OT"), (3,  "UT"),
    (4,  "LBK"), (5,  "TBK"), (6,  "DV"), (7,  "DT"),
    (8,  "BATTOV"), (9,  "BATTUV"), (10, "AUX"), (11, "SOCLO"),
    (12, "CHG_OCS"), (13, "DSCH_OCS"), (14, "CHG_OCT"), (15, "DSCH_OCT"),
    (16, "BSU_NRDY"), (17, "PRECHG"), (18, "CRITICAL_IO"), (19, "HVREL"),
    (20, "ISA"), (21, "CAN_RUNTIME"), (22, "SAFETY"), (23, "CHR_TELEM"),
    (24, "CHR_CMD"), (25, "SLAVE1_OFF"), (26, "SLAVE2_OFF"), (27, "SLAVE3_OFF"),
    (28, "SLAVE4_OFF"), (29, "SLAVE5_OFF"), (30, "SLAVE6_OFF"), (31, "IVT_U1"),
]

BAT_STATE_NAMES = {2: "自检", 3: "待机", 4: "预充", 5: "高压", 7: "故障"}


def decode_fault_frame(data: Sequence[int]) -> Dict[str, object]:
    """解码统一故障状态帧 0x187650F4"""
    if len(data) < 8:
        return {}
    bat_state = (data[0] >> 4) & 0x0F
    alm_level = data[0] & 0x0F
    fault_code = (data[1] << 24) | (data[2] << 16) | (data[3] << 8) | data[4]
    latched = (data[5] >> 7) & 0x01
    charge_mode = (data[5] >> 2) & 0x01
    slave_offline = data[6] & 0x3F
    version = data[7]

    active_faults = []
    for bit, name in FAULT_BIT_NAMES:
        if fault_code & (1 << bit):
            active_faults.append(name)

    return {
        "state": BAT_STATE_NAMES.get(bat_state, str(bat_state)),
        "level": alm_level,
        "fault_code": f"0x{fault_code:08X}",
        "faults": active_faults,
        "latched": latched,
        "charge_mode": charge_mode,
        "slave_offline": f"0x{slave_offline:02X}",
        "version": version,
    }


def decode_threshold_frame(data: Sequence[int]) -> Dict[str, int]:
    if len(data) < 6:
        return {}
    return {
        "OV_mV": (data[0] << 8) | data[1],
        "UV_mV": (data[2] << 8) | data[3],
        "OT_raw": data[4],
        "UT_raw": data[5],
    }


def decode_alarm_level_frame(data: Sequence[int]) -> Dict[str, int]:
    if len(data) < 8:
        return {}
    levels: Dict[str, int] = {}
    for bit, name in FAULT_BIT_NAMES:
        byte = bit // 4
        shift = (bit % 4) * 2
        level = (data[byte] >> shift) & 0x03
        if level:
            levels[name] = level
    return levels


def format_alarm_levels(levels: Dict[str, int]) -> str:
    if not levels:
        return "无"
    return " ".join(f"{name}=L{level}" for name, level in levels.items())


def decode_switch_frame(data: Sequence[int]) -> Dict[str, int]:
    if len(data) < 2:
        return {}
    return {
        "OV": (data[0] >> 7) & 1, "UV": (data[0] >> 6) & 1,
        "OT": (data[0] >> 5) & 1, "UT": (data[0] >> 4) & 1,
        "DV": (data[0] >> 3) & 1, "DT": (data[0] >> 2) & 1,
        "CHG_OCS": (data[0] >> 1) & 1, "DSCH_OCS": data[0] & 1,
        "AUX": (data[1] >> 7) & 1, "CAN_RUNTIME": (data[1] >> 6) & 1,
        "HVREL": (data[1] >> 5) & 1,
        "ISA": (data[1] >> 4) & 1, "BATTOV": (data[1] >> 3) & 1,
        "BATTUV": (data[1] >> 2) & 1, "BEEP": (data[1] >> 1) & 1,
        "SOCLO": data[1] & 1,
    }


def format_faults(faults: List[str]) -> str:
    return " ".join(faults) if faults else "无"


def decode_bms_message(msg: can.Message) -> Optional[str]:
    data = list(msg.data)
    aid = msg.arbitration_id

    if aid in (BMS_TOTAL_ID, CAN2_PACK_STATUS_ID) and len(data) >= 7:
        tv = ((data[0] << 8) | data[1]) / 10.0
        ca = int.from_bytes(bytes(data[2:4]), "big", signed=True) / 10.0
        soc = data[4] if data[5] & 0x04 else None
        bs = (data[6] >> 4) & 0x0F
        al = data[6] & 0x0F
        voltage = f"{tv:.1f}V" if data[5] & 0x01 else "总压无效"
        current = f"{ca:+.1f}A" if data[5] & 0x02 else "电流无效"
        return f"总览: {voltage} {current} SOC={soc if soc is not None else '无效'}% 状态={BAT_STATE_NAMES.get(bs, str(bs))} 告警级别={al}"

    if aid == BMS_RELAY_ID and len(data) >= 8:
        pos = (data[0] >> 6) & 1
        neg = (data[0] >> 4) & 1
        pre = (data[0] >> 2) & 1
        rv = ((data[2] << 8) | data[3]) / 10.0
        ri = ((data[4] << 8) | data[5]) / 10.0
        pv = ((data[6] << 8) | data[7]) / 10.0
        return f"继电器: POS={pos} PRE={pre} NEG={neg} 充电请求={rv:.1f}V/{ri:.1f}A 预充电压={pv:.1f}V"

    if aid in (BMS_FAULT_ID, CAN2_FAULT_ID) and len(data) >= 8:
        info = decode_fault_frame(data)
        prefix = "CANB故障状态" if aid == CAN2_FAULT_ID else "故障状态"
        return (f"{prefix}: state={info['state']} level={info['level']} "
                f"fault_code={info['fault_code']} latched={info['latched']} "
                f"charge={info['charge_mode']} slave_off={info['slave_offline']} "
                f"ver={info['version']}\n  活跃: {format_faults(info['faults'])}")

    if aid in (BMS_ALARM_LEVEL_ID, CAN2_ALARM_LEVEL_ID) and len(data) >= 8:
        levels = decode_alarm_level_frame(data)
        prefix = "CANB告警等级" if aid == CAN2_ALARM_LEVEL_ID else "告警等级"
        return f"{prefix}: {format_alarm_levels(levels)}"

    if aid == CAN2_SOP_LIMITS_ID and len(data) >= 8:
        return (f"SOP限值: 放电={(data[0]|data[1]<<8)/10.0:.1f}A/"
                f"{(data[4]|data[5]<<8)/10.0:.1f}kW 回充="
                f"{(data[2]|data[3]<<8)/10.0:.1f}A/"
                f"{(data[6]|data[7]<<8)/10.0:.1f}kW")

    if aid == CAN2_SOP_STATUS_ID and len(data) >= 8:
        return (f"SOP状态: ver={data[0]>>4} seq={data[0]&0x0F} "
                f"flags=0x{data[1]:02X} state={data[2]} "
                f"reason=0x{(data[3]|data[4]<<8):04X} "
                f"health=0x{data[5]:02X} intervention=0x{data[6]:02X} "
                f"crc=0x{data[7]:02X}")

    if aid == BMS_CELL_MAX_V_ID and len(data) >= 6:
        return (f"单体极值: Max={((data[0]<<8)|data[1])}mV#{data[4]} "
                f"Min={((data[2]<<8)|data[3])}mV#{data[5]}")

    if aid == BMS_CELL_MAX_T_ID and len(data) >= 5:
        fan = ""
        if len(data) >= 8:
            fan = f" duty={data[5]}% rpm≈{data[6]*100} flags=0x{data[7]:02X}"
        return (f"温度极值: Max={data[0]-30}C#{data[2]} "
                f"Min={data[1]-30}C#{data[3]} CoolingCtl={data[4]}{fan}")

    if aid == BMS_CELL_SUM_ID and len(data) >= 2:
        return f"累加电压: {((data[0]<<8)|data[1])/10.0:.1f}V"

    if aid == BMS_IMD_DIAG_ID and len(data) >= 8:
        status = (data[0] >> 4) & 0x0F
        cls = data[0] & 0x0F
        return (f"IMD: status={status} class={cls} flags=0x{data[1]:02X} "
                f"duty={((data[2]<<8)|data[3])/10.0:.1f}% "
                f"Rf={((data[4]<<8)|data[5])}kOhm freq={((data[6]<<8)|data[7])/100.0:.2f}Hz")

    if aid == BMS_THRESHOLD_ID and len(data) >= 6:
        tv = decode_threshold_frame(data)
        return f"告警阈值: OV={tv['OV_mV']}mV UV={tv['UV_mV']}mV OT={tv['OT_raw']-30}C UT={tv['UT_raw']-30}C"

    if aid == BMS_SWITCH_ID and len(data) >= 3:
        sw = decode_switch_frame(data)
        on_list = [k for k, v in sw.items() if v]
        return f"告警开关: ver={data[2]} {' '.join(on_list) if on_list else '全关'}"

    return None


# ── 命令处理 ────────────────────────────────────────────────

class CommandProcessor:
    def __init__(self, model: BenchModel, monitor: BmsMonitorState) -> None:
        self.model = model
        self.monitor = monitor

    def handle(self, line: str) -> str:
        parts = shlex.split(line)
        if not parts:
            return ""
        cmd = parts[0].lower()

        if cmd in {"help", "?"}:
            return self._help()
        if cmd == "status":
            return self._status()
        if cmd == "pack":
            return f"估算单体累加: {self.model.estimated_pack_voltage_v():.3f}V"
        if cmd == "bms" and (len(parts) == 1 or (len(parts) == 2 and parts[1] == "show")):
            return self._bms_show()
        if cmd == "slave" and len(parts) >= 4:
            return self._slave_cmd(parts)
        if cmd == "cell" and len(parts) == 3:
            self.model.set_cell_mv(int(parts[1]), int(parts[2]))
            return f"单体{parts[1]} 已设为 {parts[2]}mV"
        if cmd == "temp" and len(parts) == 3:
            self.model.set_temp_c(int(parts[1]), int(parts[2]))
            return f"温度{parts[1]} 已设为 {parts[2]}C"
        if cmd == "openwire" and len(parts) == 3:
            enabled = self._on_off(parts[2])
            index = int(parts[1])
            self.model.set_open_wire_cell(index, enabled)
            return f"单体{index} 断线 {'开启' if enabled else '关闭'}"
        if cmd == "opentemp" and len(parts) == 3:
            enabled = self._on_off(parts[2])
            index = int(parts[1])
            self.model.set_open_wire_temp(index, enabled)
            return f"温度{index} 断线 {'开启' if enabled else '关闭'}"
        if cmd == "scenario":
            return self._scenario(parts)
        if cmd == "reset":
            self.model.reset()
            return "所有模拟量已恢复默认"
        raise ValueError("未知命令，输入 help 查看支持项")

    def _slave_cmd(self, parts: List[str]) -> str:
        si = int(parts[1])
        if si < 1 or si > SLAVE_COUNT:
            raise ValueError("从控编号 1..6")
        slave = self.model.slaves[si - 1]
        action = parts[2].lower()
        if action == "online":
            slave.online = self._on_off(parts[3])
            return f"从控{si} {'在线' if slave.online else '离线'}"
        if action == "basev" and len(parts) == 4:
            slave.base_cell_mv = int(parts[3])
            slave.cell_offsets_mv = [0] * CELL_COUNT_PER_SLAVE
            return f"从控{si} 基准电压设为 {slave.base_cell_mv}mV"
        if action == "baset" and len(parts) == 4:
            slave.base_temp_c = int(parts[3])
            slave.temp_offsets_c = [0] * TEMP_COUNT_PER_SLAVE
            return f"从控{si} 基准温度设为 {slave.base_temp_c}C"
        raise ValueError("slave 子命令: online/basev/baset")

    @staticmethod
    def _on_off(text: str) -> bool:
        v = text.lower()
        if v in {"1", "on", "true", "yes"}:
            return True
        if v in {"0", "off", "false", "no"}:
            return False
        raise ValueError("开关参数仅支持 on/off")

    def _status(self) -> str:
        lines = [
            f"估算累加: {self.model.estimated_pack_voltage_v():.3f}V",
            "IVT-S: 使用 CAN1 上的真实设备；脚本不发送 0x512",
            f"单体断线: {len(self.model.open_wire_cells)} 温度断线: {len(self.model.open_wire_temps)}",
        ]
        for s in self.model.slaves:
            lines.append(f"从控{s.slave_id}: 在线={s.online} baseV={s.base_cell_mv}mV baseT={s.base_temp_c}C")
        return "\n".join(lines)

    def _bms_show(self) -> str:
        lines: List[str] = []
        m = self.monitor

        if m.total_data and len(m.total_data) >= 7:
            d = m.total_data
            tv = ((d[0] << 8) | d[1]) / 10.0
            ca = int.from_bytes(bytes(d[2:4]), "big", signed=True) / 10.0
            voltage = f"{tv:.1f}V" if d[5] & 0x01 else "总压无效"
            current = f"{ca:+.1f}A" if d[5] & 0x02 else "电流无效"
            soc = f"{d[4]}%" if d[5] & 0x04 else "无效"
            bs = (d[6] >> 4) & 0x0F
            lines.append(f"总览: {voltage} {current} SOC={soc} 状态={BAT_STATE_NAMES.get(bs, str(bs))}")
        else:
            lines.append("总览: 尚未收到")

        if m.relay_data and len(m.relay_data) >= 8:
            d = m.relay_data
            lines.append(f"继电器: POS={(d[0]>>6)&1} PRE={(d[0]>>2)&1} NEG={(d[0]>>4)&1} "
                         f"ReqV={((d[2]<<8)|d[3])/10.0:.1f}V ReqI={((d[4]<<8)|d[5])/10.0:.1f}A")

        if m.cell_sum_data:
            lines.append(f"累加: {((m.cell_sum_data[0]<<8)|m.cell_sum_data[1])/10.0:.1f}V")

        if m.cell_max_v_data and len(m.cell_max_v_data) >= 6:
            d = m.cell_max_v_data
            lines.append(f"单体极值: Max={((d[0]<<8)|d[1])}mV#{d[4]} Min={((d[2]<<8)|d[3])}mV#{d[5]}")

        if m.fault_data and len(m.fault_data) >= 8:
            info = decode_fault_frame(m.fault_data)
            lines.append(f"故障: state={info['state']} level={info['level']} "
                         f"code={info['fault_code']} latched={info['latched']}")
            lines.append(f"  活跃: {format_faults(info['faults'])}")
        else:
            lines.append("故障: 尚未收到")

        if m.alarm_level_data and len(m.alarm_level_data) >= 8:
            levels = decode_alarm_level_frame(m.alarm_level_data)
            lines.append(f"告警等级: {format_alarm_levels(levels)}")

        if m.threshold_data and len(m.threshold_data) >= 6:
            tv = decode_threshold_frame(m.threshold_data)
            lines.append(f"阈值: OV={tv['OV_mV']}mV UV={tv['UV_mV']}mV "
                         f"OT={tv['OT_raw']-30}C UT={tv['UT_raw']-30}C")

        if m.switch_data and len(m.switch_data) >= 2:
            sw = decode_switch_frame(m.switch_data)
            on_list = [k for k, v in sw.items() if v]
            lines.append(f"开关: {' '.join(on_list) if on_list else '全关'}")

        if m.sop_limits_data and m.sop_status_data:
            lines.append(decode_bms_message(can.Message(
                arbitration_id=CAN2_SOP_LIMITS_ID,
                is_extended_id=False,
                data=m.sop_limits_data)) or "")
            lines.append(decode_bms_message(can.Message(
                arbitration_id=CAN2_SOP_STATUS_ID,
                is_extended_id=False,
                data=m.sop_status_data)) or "")
            lines.append(f"SOP确认已发送: {m.sop_ack_count}")

        return "\n".join(lines)

    def _scenario(self, parts: List[str]) -> str:
        if len(parts) < 2:
            raise ValueError("usage: scenario <name> on|off [val]")
        name = parts[1].lower()
        enabled = self._on_off(parts[2]) if len(parts) >= 3 else True

        if name == "cellov":
            mv = int(parts[3]) if len(parts) >= 4 else 4250
            if enabled:
                self.model.set_cell_mv(1, mv)
            else:
                self.model.reset_cell_mv(1)
            return f"cellov: cell1={'='+str(mv)+'mV' if enabled else '恢复'}"

        if name == "celluv":
            mv = int(parts[3]) if len(parts) >= 4 else 3000
            ci = SLAVE_COUNT * CELL_COUNT_PER_SLAVE
            if enabled:
                self.model.set_cell_mv(ci, mv)
            else:
                self.model.reset_cell_mv(ci)
            return f"celluv: cell{ci}={'='+str(mv)+'mV' if enabled else '恢复'}"

        if name == "cellot":
            tc = int(parts[3]) if len(parts) >= 4 else 65
            if enabled:
                self.model.set_temp_c(1, tc)
            else:
                self.model.reset_temp_c(1)
            return f"cellot: temp1={'='+str(tc)+'C' if enabled else '恢复'}"

        if name == "cellut":
            tc = int(parts[3]) if len(parts) >= 4 else -5
            ti = SLAVE_COUNT * TEMP_COUNT_PER_SLAVE
            if enabled:
                self.model.set_temp_c(ti, tc)
            else:
                self.model.reset_temp_c(ti)
            return f"cellut: temp{ti}={'='+str(tc)+'C' if enabled else '恢复'}"

        if name == "openwire" and len(parts) >= 4:
            ci = int(parts[3])
            self.model.set_open_wire_cell(ci, enabled)
            return f"openwire: cell{ci}={'on' if enabled else 'off'}"

        if name == "opentemp" and len(parts) >= 4:
            ti = int(parts[3])
            self.model.set_open_wire_temp(ti, enabled)
            return f"opentemp: temp{ti}={'on' if enabled else 'off'}"

        if name == "slaveoff" and len(parts) >= 4:
            si = int(parts[3])
            self.model.slaves[si - 1].online = not enabled
            return f"slaveoff: slave{si}={'off' if enabled else 'on'}"

        if name == "clear":
            self.model.reset()
            return "所有场景已清除"

        raise ValueError(f"未知 scenario: {name}")

    @staticmethod
    def _help() -> str:
        return """支持命令:
  status                      查看当前模拟状态
  pack                        计算单体累加电压
  bms show                    查看主控最近一次回帧摘要
  slave <1..6> online on|off  设置从控在线/离线
  slave <1..6> basev <mV>     设置从控基准单体电压
  slave <1..6> baset <C>      设置从控基准温度
  cell <1..138> <mV>          设置某串单体电压
  temp <1..48> <C>            设置某路温度
  openwire <1..138> on|off    对某串注入 0xFFFF 断线码
  opentemp <1..48> on|off     对某路注入 0xFF 断线码
  scenario <name> on|off [v]  快速注入场景: cellov/celluv/cellot/cellut/
                              openwire/opentemp/slaveoff/clear
  reset                       恢复默认值
  help                        查看帮助
  quit / exit                 退出"""


# ── 应用主类 ────────────────────────────────────────────────

class PcanBenchApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.model = BenchModel()
        self.monitor = BmsMonitorState()
        self.cmd = CommandProcessor(self.model, self.monitor)
        self.cmd_queue: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.bus = None
        try:
            self.bus = can.Bus(interface="pcan", channel=args.channel, bitrate=args.bitrate)
        except Exception:
            if self.bus is not None:
                self.bus.shutdown()
                self.bus = None
            raise
        self._tx_ids = self._build_tx_ids()
        self._last_tx_err = ""
        self._last_tx_err_ts = 0.0

    def _build_tx_ids(self) -> Set[Tuple[bool, int]]:
        ids: Set[Tuple[bool, int]] = set()
        if self.args.sop_ack:
            ids.add((False, CAN2_SOP_ACK_ID))
        else:
            for si in range(SLAVE_COUNT):
                ids.add((True, SLAVE_TEMP_BASE_ID + (si << 16)))
                for fi in range(6):
                    ids.add((True, SLAVE_VOLT_BASE_ID + ((si * 6 + fi) << 16)))
        return ids

    def run(self) -> None:
        print(f"PCAN: {self.args.channel} @ {self.args.bitrate}bps")
        if self.args.sop_ack:
            print("总线用途: CANB · ECU SOP 确认")
        else:
            print("总线用途: CAN1 · 六个从控台架")
        print("IVT-S: 接入 F405 CAN1 的真实设备；本脚本不发送 0x512")
        print("ECU SOP确认: " + ("开启" if self.args.sop_ack else "关闭"))
        quiet = not self.args.live_rx
        print("脚本已启动。" + (" 安静模式，输入命令查看结果。" if quiet else " 实时打印主控回帧。"))
        print("输入 help 查看命令列表。\n")

        threads = [
            threading.Thread(target=self._sender, name="tx", daemon=True),
            threading.Thread(target=self._receiver, name="rx", daemon=True),
            threading.Thread(target=self._stdin, name="in", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while not self.stop_event.is_set():
                try:
                    line = self.cmd_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line.strip().lower() in {"quit", "exit"}:
                    self.stop_event.set()
                    break
                try:
                    result = self.cmd.handle(line)
                    if result:
                        print(result)
                except Exception as exc:
                    print(f"错误: {exc}")
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C, 退出...")
        finally:
            self.stop_event.set()
            if self.bus is not None:
                self.bus.shutdown()
                self.bus = None
            print("已退出。")

    def _send(self, frame: can.Message, bus: object | None = None, label: str = "TX") -> None:
        target = bus or self.bus
        if target is None:
            return
        try:
            target.send(frame)
        except can.CanError as exc:
            now = time.monotonic()
            msg = str(exc)
            if msg != self._last_tx_err or now - self._last_tx_err_ts >= 1.0:
                print(f"{label} 错误: {msg}")
                self._last_tx_err = msg
                self._last_tx_err_ts = now
            time.sleep(0.05)

    def _sender(self) -> None:
        nv = time.monotonic()
        nt = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if (not self.args.sop_ack) and now >= nv:
                for s in self.model.slaves:
                    for f in build_slave_voltage_frames(self.model, s):
                        self._send(f)
                nv += TX_VOLT_PERIOD_S
            if (not self.args.sop_ack) and now >= nt:
                for s in self.model.slaves:
                    f = build_slave_temp_frame(self.model, s)
                    if f:
                        self._send(f)
                nt += TX_TEMP_PERIOD_S
            time.sleep(0.01)

    def _receiver(self) -> None:
        while not self.stop_event.is_set():
            msg = self.bus.recv(timeout=0.2)
            if msg is None:
                continue
            key = (bool(msg.is_extended_id), int(msg.arbitration_id))
            if key in self._tx_ids:
                continue
            self._update_monitor(msg)
            decoded = decode_bms_message(msg)
            if decoded:
                if self.args.live_rx:
                    print(decoded)
            elif self.args.verbose_rx:
                ft = "EXT" if msg.is_extended_id else "STD"
                dh = " ".join(f"{b:02X}" for b in msg.data)
                print(f"RX {ft} 0x{msg.arbitration_id:X}: {dh}")

    def _update_monitor(self, msg: can.Message) -> None:
        data = list(msg.data)
        aid = msg.arbitration_id
        if aid in (BMS_TOTAL_ID, CAN2_PACK_STATUS_ID):
            self.monitor.total_data = data
        elif aid == BMS_RELAY_ID:
            self.monitor.relay_data = data
        elif aid in (BMS_FAULT_ID, CAN2_FAULT_ID):
            self.monitor.fault_data = data
        elif aid in (BMS_ALARM_LEVEL_ID, CAN2_ALARM_LEVEL_ID):
            self.monitor.alarm_level_data = data
        elif aid == BMS_THRESHOLD_ID:
            self.monitor.threshold_data = data
        elif aid == BMS_SWITCH_ID:
            self.monitor.switch_data = data
        elif aid == BMS_CELL_SUM_ID:
            self.monitor.cell_sum_data = data
        elif aid == BMS_IMD_DIAG_ID:
            self.monitor.imd_diag_data = data
        elif aid == BMS_CELL_MAX_V_ID:
            self.monitor.cell_max_v_data = data
        elif aid == BMS_CELL_MAX_T_ID:
            self.monitor.cell_max_t_data = data
        elif aid == CAN2_SOP_LIMITS_ID and len(data) == 8:
            self.monitor.sop_limits_data = data
        elif aid == CAN2_SOP_STATUS_ID and len(data) == 8:
            self.monitor.sop_status_data = data
            self._send_sop_ack_if_valid()

    def _send_sop_ack_if_valid(self) -> None:
        if not self.args.sop_ack:
            return
        limits = self.monitor.sop_limits_data
        status = self.monitor.sop_status_data
        if limits is None or status is None:
            return
        crc_data = [0x04, 0xA0, *limits, 0x04, 0xA3, *status[:7]]
        if (status[0] >> 4) != 1 or crc8_sae_j1850(crc_data) != status[7]:
            return
        self._send(build_sop_ack(limits, status))
        self.monitor.sop_ack_count += 1

    def _stdin(self) -> None:
        while not self.stop_event.is_set():
            try:
                line = input("pcan> ")
            except EOFError:
                self.stop_event.set()
                return
            self.cmd_queue.put(line)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BMS F405 从控台架联调脚本 (PCAN)")
    p.add_argument("--channel", default="PCAN_USBBUS1", help="PCAN 通道名")
    p.add_argument("--bitrate", type=int, default=CAN1_BITRATE, help="CAN 波特率")
    p.add_argument("--live-rx", action="store_true", help="实时打印已解码的主控回帧")
    p.add_argument("--verbose-rx", action="store_true", help="打印未解码的接收帧（配合 --live-rx）")
    p.add_argument("--canb-sop-ack", "--sop-ack", dest="sop_ack", action="store_true",
                   help="CANB ECU确认模式：校验 0x4A0/0x4A3 并发送 0x4A4")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if can is None:
        raise SystemExit("缺少 python-can，请先执行: pip install python-can")
    app = PcanBenchApp(args)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
