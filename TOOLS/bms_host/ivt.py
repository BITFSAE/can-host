"""IVT-S configuration protocol shared by the host and the PCAN tool.

The module contains no python-can or GUI code.  A transport supplies two
callbacks: send one standard CAN frame and receive the next frame.  This
keeps the IVT request/response protocol on the same receive path as the BMS
monitor instead of opening a competing ``recv()`` loop.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Iterable, Sequence


DEFAULT_CMD_ID = 0x411
DEFAULT_RSP_ID = 0x511
DEFAULT_BITRATE = 500000
BMS_CANB_CMD_ID = 0x410
BMS_CANB_RSP_ID = 0x51A
BMS_CANB_RESULT_IDS = (0x512, 0x513, 0x514, 0x515, 0x516, 0x517, 0x518, 0x519)

SET_MODE_RSP_MUX = 0xB4
STORE_RSP_MUX = 0xB2
TRIGGER_RSP_MUX = 0xB1
THRESHOLD_POS_RSP_MUX = 0xB5
THRESHOLD_NEG_RSP_MUX = 0xB6
DEVICE_ID_RSP_MUX = 0xB9
SW_VERSION_RSP_MUX = 0xBA
SERIAL_NUMBER_RSP_MUX = 0xBB
ARTICLE_NUMBER_RSP_MUX = 0xBC
ALIVE_MUX = 0xBF
ILLEGAL_COMMAND_MUX = 0xFF

MODE_TO_VALUE = {"disabled": 0x0, "triggered": 0x1, "cyclic": 0x2}
MODE_NAME = {value: name for name, value in MODE_TO_VALUE.items()}

DEVICE_TYPE_NAME = {0x00: "unknown", 0x01: "IVT", 0x02: "IVT-S"}
FEATURE_NAME = {0x00: "none", 0x01: "switch", 0x02: "relay", 0x03: "isolation"}
COMMUNICATION_NAME = {0x00: "CAN (no termination)", 0x01: "CAN1 (with termination)"}
SUPPLY_NAME = {0x00: "5V", 0x01: "12/24V"}


@dataclasses.dataclass(frozen=True)
class IvtFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False


@dataclasses.dataclass(frozen=True)
class ResultChannel:
    index: int
    name: str
    set_mux: int
    get_mux: int
    rsp_mux: int
    default_can_id: int
    default_period_ms: int
    scale: float
    unit: str
    default_mode: str
    can_id_set_mux: int
    can_id_get_mux: int
    can_id_rsp_mux: int


CHANNELS = (
    ResultChannel(0, "I", 0x20, 0x60, 0xA0, 0x521, 20, 0.001, "A", "cyclic", 0x10, 0x50, 0x90),
    ResultChannel(1, "U1", 0x21, 0x61, 0xA1, 0x522, 60, 0.001, "V", "cyclic", 0x11, 0x51, 0x91),
    ResultChannel(2, "U2", 0x22, 0x62, 0xA2, 0x523, 60, 0.001, "V", "cyclic", 0x12, 0x52, 0x92),
    ResultChannel(3, "U3", 0x23, 0x63, 0xA3, 0x524, 60, 0.001, "V", "cyclic", 0x13, 0x53, 0x93),
    ResultChannel(4, "T", 0x24, 0x64, 0xA4, 0x525, 100, 0.1, "degC", "disabled", 0x14, 0x54, 0x94),
    ResultChannel(5, "W", 0x25, 0x65, 0xA5, 0x526, 30, 1.0, "W", "disabled", 0x15, 0x55, 0x95),
    ResultChannel(6, "As", 0x26, 0x66, 0xA6, 0x527, 30, 1.0, "As", "disabled", 0x16, 0x56, 0x96),
    ResultChannel(7, "Wh", 0x27, 0x67, 0xA7, 0x528, 30, 1.0, "Wh", "disabled", 0x17, 0x57, 0x97),
)
CHANNEL_BY_NAME = {channel.name.lower(): channel for channel in CHANNELS}
CHANNEL_BY_INDEX = {channel.index: channel for channel in CHANNELS}
DEFAULT_CHANNEL_BY_CAN_ID = {channel.default_can_id: channel for channel in CHANNELS}
BMS_CANB_CHANNEL_BY_CAN_ID = {can_id: CHANNEL_BY_INDEX[index] for index, can_id in enumerate(BMS_CANB_RESULT_IDS)}

SPECIAL_CAN_TARGETS = {
    "command": (0x1D, 0x5D, 0x9D),
    "response": (0x1F, 0x5F, 0x9F),
}

INTEL_SETUP_DB1 = 0x42
BITRATE_PRESETS = (250000, 500000)
IVT_RESPONSE_MUXES = {
    SET_MODE_RSP_MUX, STORE_RSP_MUX, TRIGGER_RSP_MUX,
    THRESHOLD_POS_RSP_MUX, THRESHOLD_NEG_RSP_MUX,
    DEVICE_ID_RSP_MUX, SW_VERSION_RSP_MUX, SERIAL_NUMBER_RSP_MUX,
    ARTICLE_NUMBER_RSP_MUX, ALIVE_MUX, ILLEGAL_COMMAND_MUX,
    0x9D, 0x9F,
    *(channel.rsp_mux for channel in CHANNELS),
    *(channel.can_id_rsp_mux for channel in CHANNELS),
}


class IvtProtocolError(RuntimeError):
    """The IVT returned an invalid or rejected response."""


def _data(message: Any) -> list[int]:
    try:
        return list(message.data)
    except (AttributeError, TypeError) as exc:
        raise IvtProtocolError("IVT 应答没有有效数据") from exc


def _require_response(message: Any, minimum: int) -> list[int]:
    data = _data(message)
    if len(data) < minimum:
        raise IvtProtocolError(f"IVT 应答长度不足：需要 {minimum} 字节，收到 {len(data)} 字节")
    if getattr(message, "is_extended_id", False):
        raise IvtProtocolError("IVT 应答必须使用标准 CAN ID")
    return data


def _frame(arbitration_id: int, data: Sequence[int]) -> IvtFrame:
    if not 0 <= arbitration_id <= 0x7FF:
        raise ValueError("IVT CAN ID 必须是 11 bit 标准 ID")
    if len(data) > 8:
        raise ValueError("CAN 2.0A payload 不能超过 8 字节")
    return IvtFrame(arbitration_id, bytes(data), False)


def encode_u32_be(value: int) -> list[int]:
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("序列号必须在 0..0xFFFFFFFF 范围内")
    return [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]


def parse_config_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 4)
    db1 = data[1]
    mode = db1 & 0x0F
    return {
        "mux": data[0], "db1": db1, "mode": mode,
        "mode_name": MODE_NAME.get(mode, f"unknown({mode})"),
        "report_errors": bool(db1 & 0x20), "byte_order": "little" if db1 & 0x40 else "big",
        "invert_sign": bool(db1 & 0x80), "period_ms": (data[2] << 8) | data[3],
        "raw": data,
    }


def parse_mode_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 3)
    current_value, startup_value = data[1], data[2]
    values = {0x00: "stop", 0x01: "run"}
    return {
        "current": current_value, "current_name": values.get(current_value, f"unknown({current_value})"),
        "startup": startup_value, "startup_name": values.get(startup_value, f"unknown({startup_value})"),
        "raw": data,
    }


def parse_threshold_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 5)
    threshold_a = (data[1] << 8) | data[2]
    reset_threshold_a = (data[3] << 8) | data[4]
    return {
        "threshold_a": threshold_a, "reset_threshold_a": reset_threshold_a,
        "threshold_enabled": threshold_a != 0, "reset_threshold_enabled": reset_threshold_a != 0,
        "raw": data,
    }


def parse_can_id_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 7)
    return {
        "can_id": ((data[1] << 8) | data[2]) & 0x7FF,
        "serial_number": int.from_bytes(bytes(data[3:7]), "big", signed=False), "raw": data,
    }


def parse_device_id_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 7)
    nominal_current_a = data[2] * 16 + ((data[3] >> 4) & 0x0F)
    device_type, feature, communication, supply = data[1], data[4], data[5], data[6]
    return {
        "device_type": device_type, "device_type_name": DEVICE_TYPE_NAME.get(device_type, f"unknown(0x{device_type:02X})"),
        "nominal_current_a": nominal_current_a, "voltage_channels": data[3] & 0x0F,
        "feature": feature, "feature_name": FEATURE_NAME.get(feature, f"unknown(0x{feature:02X})"),
        "communication": communication, "communication_name": COMMUNICATION_NAME.get(communication, f"unknown(0x{communication:02X})"),
        "supply": supply, "supply_name": SUPPLY_NAME.get(supply, f"unknown(0x{supply:02X})"),
        "raw": data,
    }


def parse_generic_response(message: Any) -> dict[str, Any]:
    data = _require_response(message, 1)
    payload = bytes(data[1:])
    return {
        "payload": list(payload), "payload_hex": payload.hex(" ").upper(),
        "value_u32": int.from_bytes(payload[:4], "big", signed=False) if len(payload) >= 4 else None,
        "value_u56": int.from_bytes(payload[:7], "big", signed=False) if payload else None,
        "raw": data,
    }


def parse_serial_number_response(message: Any) -> dict[str, Any]:
    parsed = parse_generic_response(message)
    if len(parsed["payload"]) < 4:
        raise IvtProtocolError("IVT 序列号应答长度不足")
    serial = int(parsed["value_u32"])
    parsed.update({"serial_number": serial, "serial_number_hex": f"0x{serial:08X}"})
    return parsed


def parse_channel_selector(text: str) -> list[ResultChannel]:
    if text.lower() == "all":
        return list(CHANNELS)
    names = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not names:
        raise ValueError("通道选择为空")
    try:
        return [CHANNEL_BY_NAME[name] for name in names]
    except KeyError as exc:
        raise ValueError(f"未知 IVT 通道：{exc.args[0]}") from exc


def parse_can_target(text: str) -> tuple[str, int, int, int]:
    key = text.strip().lower()
    if key in SPECIAL_CAN_TARGETS:
        set_mux, get_mux, rsp_mux = SPECIAL_CAN_TARGETS[key]
        return key, set_mux, get_mux, rsp_mux
    channel = CHANNEL_BY_NAME.get(key)
    if channel is None:
        raise ValueError(f"未知 IVT CAN 目标：{text}")
    return channel.name, channel.can_id_set_mux, channel.can_id_get_mux, channel.can_id_rsp_mux


def build_db1(
    mode: str | None = None,
    byte_order: str | None = None,
    report_errors: bool | None = None,
    invert_sign: bool | None = None,
    base_db1: int | None = None,
) -> int:
    db1 = (base_db1 or 0) & 0xFF
    if mode is not None:
        if mode not in MODE_TO_VALUE:
            raise ValueError(f"未知 IVT 通道模式：{mode}")
        db1 = (db1 & 0xF0) | MODE_TO_VALUE[mode]
    if report_errors is not None:
        db1 = db1 | 0x20 if report_errors else db1 & ~0x20
    if byte_order is not None:
        if byte_order not in {"big", "little"}:
            raise ValueError("字节序必须是 big 或 little")
        db1 = db1 | 0x40 if byte_order == "little" else db1 & ~0x40
    if invert_sign is not None:
        db1 = db1 | 0x80 if invert_sign else db1 & ~0x80
    return db1


def _channel_expected(channel: ResultChannel, db1: int, period_ms: int | None = None) -> dict[str, Any]:
    mode = db1 & 0x0F
    return {
        "db1": db1, "mode": mode, "mode_name": MODE_NAME.get(mode, f"unknown({mode})"),
        "byte_order": "little" if db1 & 0x40 else "big", "report_errors": bool(db1 & 0x20),
        "invert_sign": bool(db1 & 0x80), "period_ms": channel.default_period_ms if period_ms is None else period_ms,
    }


def expected_bms_canb_config(
    startup: str = "run",
    bitrate: int = DEFAULT_BITRATE,
    positive_threshold_a: int = 0,
    positive_reset_threshold_a: int = 0,
    negative_threshold_a: int = 0,
    negative_reset_threshold_a: int = 0,
) -> dict[str, Any]:
    if startup not in {"stop", "run"}:
        raise ValueError("IVT 上电模式必须是 stop 或 run")
    channels = {channel.name: _channel_expected(channel, INTEL_SETUP_DB1) for channel in CHANNELS}
    can_ids = {channel.name: can_id for channel, can_id in zip(CHANNELS, BMS_CANB_RESULT_IDS)}
    can_ids.update({"command": BMS_CANB_CMD_ID, "response": BMS_CANB_RSP_ID})
    return {
        "bitrate": int(bitrate), "mode": {"current": startup, "startup": startup},
        "channels": channels, "can_ids": can_ids,
        "thresholds": {
            "positive": {"threshold_a": int(positive_threshold_a), "reset_threshold_a": int(positive_reset_threshold_a)},
            "negative": {"threshold_a": int(negative_threshold_a), "reset_threshold_a": int(negative_reset_threshold_a)},
        },
    }


def factory_config() -> dict[str, Any]:
    channels = {channel.name: _channel_expected(channel, MODE_TO_VALUE[channel.default_mode]) for channel in CHANNELS}
    can_ids = {channel.name: channel.default_can_id for channel in CHANNELS}
    can_ids.update({"command": DEFAULT_CMD_ID, "response": DEFAULT_RSP_ID})
    return {
        "channels": channels,
        "can_ids": can_ids,
        "thresholds": {
            "positive": {"threshold_a": 0, "reset_threshold_a": 0},
            "negative": {"threshold_a": 0, "reset_threshold_a": 0},
        },
    }


def _actual_channel_map(readback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in readback.get("channels", [])}


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "启用" if value else "关闭"
    if isinstance(value, int) and value >= 0x100:
        return f"0x{value:X}"
    return str(value)


def compare_readback(readback: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Compare a readback with the target and classify factory defaults."""
    differences: list[dict[str, Any]] = []
    checked: list[str] = []

    def check(path: str, actual: Any, target: Any) -> None:
        checked.append(path)
        if actual != target:
            differences.append({"field": path, "actual": actual, "expected": target,
                                "actual_text": _value_text(actual), "expected_text": _value_text(target)})

    mode_expected = expected.get("mode", {})
    mode_actual = readback.get("mode", {})
    for key in ("current", "startup"):
        if key in mode_expected:
            check(f"mode.{key}", mode_actual.get(f"{key}_name"), mode_expected[key])

    if "bitrate" in expected and readback.get("bitrate") is not None:
        check("bitrate", readback.get("bitrate"), expected["bitrate"])

    actual_channels = _actual_channel_map(readback)
    for name, target in expected.get("channels", {}).items():
        actual = actual_channels.get(name, {})
        for key in ("db1", "mode_name", "byte_order", "report_errors", "invert_sign", "period_ms"):
            if key in target:
                check(f"channel.{name}.{key}", actual.get(key), target[key])

    actual_ids = readback.get("can_ids", {})
    for name, target in expected.get("can_ids", {}).items():
        check(f"can_id.{name}", actual_ids.get(name), target)

    actual_thresholds = readback.get("thresholds", {})
    for direction, target in expected.get("thresholds", {}).items():
        actual = actual_thresholds.get(direction, {})
        for key in ("threshold_a", "reset_threshold_a"):
            if key in target:
                check(f"threshold.{direction}.{key}", actual.get(key), target[key])

    factory = factory_config()
    factory_diffs = []
    factory_channels = _actual_channel_map(readback)
    for name, target in factory["channels"].items():
        actual = factory_channels.get(name, {})
        for key in ("db1", "period_ms"):
            if actual.get(key) != target[key]:
                factory_diffs.append((name, key))
    for name, target in factory["can_ids"].items():
        if actual_ids.get(name) != target:
            factory_diffs.append(("can_id", name))
    for direction, target in factory["thresholds"].items():
        actual = actual_thresholds.get(direction, {})
        for key in ("threshold_a", "reset_threshold_a"):
            if actual.get(key) != target[key]:
                factory_diffs.append(("threshold", direction, key))

    if not differences:
        status = "configured"
        status_name = "已配置且一致"
    elif not factory_diffs:
        status = "unconfigured"
        status_name = "未配置"
    else:
        status = "mismatch"
        status_name = "配置不符"
    return {
        "status": status, "status_name": status_name, "matches": not differences,
        "checked_count": len(checked), "differences": differences,
    }


class IvtClient:
    """Synchronous IVT request client over callbacks supplied by a transport."""

    def __init__(
        self,
        send: Callable[[int, Sequence[int]], None],
        receive: Callable[[float], IvtFrame],
        cmd_id: int = DEFAULT_CMD_ID,
        rsp_id: int = DEFAULT_RSP_ID,
    ) -> None:
        self._send_callback = send
        self._receive_callback = receive
        self.cmd_id = int(cmd_id)
        self.rsp_id = int(rsp_id)

    def send(self, arbitration_id: int, data: Sequence[int]) -> None:
        self._send_callback(arbitration_id, data)

    def recv_match(self, predicate: Callable[[IvtFrame], bool], timeout: float) -> IvtFrame:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 IVT 应答超时")
            frame = self._receive_callback(remaining)
            if predicate(frame):
                return frame

    def request(self, data: Sequence[int], expect_mux: int, timeout: float = 0.8) -> IvtFrame:
        self.send(self.cmd_id, data)
        message = self.recv_match(
            lambda frame: not frame.is_extended_id and frame.arbitration_id == self.rsp_id and len(frame.data) >= 1
            and frame.data[0] in {expect_mux, ILLEGAL_COMMAND_MUX}, timeout)
        self._raise_if_illegal(message)
        return message

    def request_on_response_ids(
        self, data: Sequence[int], expect_mux: int, response_ids: Sequence[int], timeout: float = 0.8
    ) -> IvtFrame:
        accepted_ids = set(response_ids)
        self.send(self.cmd_id, data)
        message = self.recv_match(
            lambda frame: not frame.is_extended_id and frame.arbitration_id in accepted_ids and len(frame.data) >= 1
            and frame.data[0] in {expect_mux, ILLEGAL_COMMAND_MUX}, timeout)
        self._raise_if_illegal(message)
        return message

    @staticmethod
    def _raise_if_illegal(message: IvtFrame) -> None:
        if message.data[0] == ILLEGAL_COMMAND_MUX:
            bad_mux = message.data[1] if len(message.data) > 1 else None
            suffix = f"（命令复用码 0x{bad_mux:02X}）" if bad_mux is not None else ""
            raise IvtProtocolError(f"IVT 拒绝命令{suffix}")

    def wait_alive(self, timeout: float = 4.0) -> IvtFrame:
        return self.recv_match(lambda frame: not frame.is_extended_id and frame.arbitration_id == self.rsp_id
                               and len(frame.data) >= 1 and frame.data[0] == ALIVE_MUX, timeout)

    def set_mode(self, current: str, startup: str) -> IvtFrame:
        values = {"stop": 0x00, "run": 0x01}
        if current not in values or startup not in values:
            raise ValueError("IVT 模式必须是 stop 或 run")
        return self.request([0x34, values[current], values[startup], 0, 0, 0, 0, 0], SET_MODE_RSP_MUX)

    def get_mode(self) -> IvtFrame:
        return self.request([0x74, 0, 0, 0, 0, 0, 0, 0], SET_MODE_RSP_MUX)

    def set_channel_config(self, channel: ResultChannel, db1: int, period_ms: int) -> IvtFrame:
        if not 0 <= int(db1) <= 0xFF or not 0 <= int(period_ms) <= 0xFFFF:
            raise ValueError("IVT 通道配置超出范围")
        return self.request([channel.set_mux, db1, (period_ms >> 8) & 0xFF, period_ms & 0xFF, 0, 0, 0, 0], channel.rsp_mux)

    def get_channel_config(self, channel: ResultChannel) -> IvtFrame:
        return self.request([channel.get_mux, 0, 0, 0, 0, 0, 0, 0], channel.rsp_mux)

    def set_can_id(self, target: tuple[str, int, int, int], can_id: int, serial_number: int) -> IvtFrame:
        if not 0 <= int(can_id) <= 0x7FF:
            raise ValueError("IVT CAN ID 必须是 11 bit 标准 ID")
        _, set_mux, _, rsp_mux = target
        return self.request([set_mux, (can_id >> 8) & 0xFF, can_id & 0xFF, *encode_u32_be(serial_number), 0], rsp_mux)

    def set_command_can_id(self, can_id: int, serial_number: int) -> IvtFrame:
        frame = self.set_can_id(parse_can_target("command"), can_id, serial_number)
        self.cmd_id = int(can_id)
        return frame

    def set_response_can_id(self, can_id: int, serial_number: int) -> IvtFrame:
        target = parse_can_target("response")
        old_rsp_id = self.rsp_id
        _, set_mux, _, rsp_mux = target
        frame = self.request_on_response_ids(
            [set_mux, (can_id >> 8) & 0xFF, can_id & 0xFF, *encode_u32_be(serial_number), 0],
            rsp_mux, [old_rsp_id, can_id])
        self.rsp_id = int(can_id)
        return frame

    def get_can_id(self, target: tuple[str, int, int, int], serial_number: int) -> IvtFrame:
        _, _, get_mux, rsp_mux = target
        return self.request([get_mux, 0, 0, *encode_u32_be(serial_number), 0], rsp_mux)

    def set_threshold(self, positive: bool, threshold_a: int, reset_threshold_a: int) -> IvtFrame:
        if not 0 <= threshold_a <= 0xFFFF or not 0 <= reset_threshold_a <= 0xFFFF:
            raise ValueError("IVT 过流阈值必须在 0..65535 A")
        mux = 0x35 if positive else 0x36
        rsp_mux = THRESHOLD_POS_RSP_MUX if positive else THRESHOLD_NEG_RSP_MUX
        return self.request([mux, (threshold_a >> 8) & 0xFF, threshold_a & 0xFF,
                             (reset_threshold_a >> 8) & 0xFF, reset_threshold_a & 0xFF, 0, 0, 0], rsp_mux)

    def get_threshold(self, positive: bool) -> IvtFrame:
        mux = 0x75 if positive else 0x76
        rsp_mux = THRESHOLD_POS_RSP_MUX if positive else THRESHOLD_NEG_RSP_MUX
        return self.request([mux, 0, 0, 0, 0, 0, 0, 0], rsp_mux)

    def get_device_id(self) -> IvtFrame:
        return self.request([0x79, 0, 0, 0, 0, 0, 0, 0], DEVICE_ID_RSP_MUX)

    def get_sw_version(self) -> IvtFrame:
        return self.request([0x7A, 0, 0, 0, 0, 0, 0, 0], SW_VERSION_RSP_MUX)

    def get_serial_number(self) -> IvtFrame:
        return self.request([0x7B, 0, 0, 0, 0, 0, 0, 0], SERIAL_NUMBER_RSP_MUX)

    def get_article_number(self) -> IvtFrame:
        return self.request([0x7C, 0, 0, 0, 0, 0, 0, 0], ARTICLE_NUMBER_RSP_MUX)

    def read_serial_number(self) -> int:
        return parse_serial_number_response(self.get_serial_number())["serial_number"]

    def store(self) -> IvtFrame:
        return self.request([0x32, 0, 0, 0, 0, 0, 0, 0], STORE_RSP_MUX, timeout=1.5)

    def restart(self) -> IvtFrame:
        self.send(self.cmd_id, [0x3F, 0, 0, 0, 0, 0, 0, 0])
        return self.wait_alive()

    def restart_to_bitrate(self, bitrate: int, reopen: Callable[[int], None]) -> tuple[IvtFrame, IvtFrame]:
        mapping = {250000: 0x08, 500000: 0x04, 1000000: 0x02}
        if bitrate not in mapping:
            raise ValueError("IVT 位率预设只支持 250000、500000、1000000")
        response = self.request([0x3A, mapping[bitrate], 0, 0, 0, 0, 0, 0], STORE_RSP_MUX, timeout=2.0)
        reopen(int(bitrate))
        return response, self.wait_alive()

    def readback(self, bitrate: int | None = None) -> dict[str, Any]:
        serial_message = self.get_serial_number()
        serial = parse_serial_number_response(serial_message)["serial_number"]
        result: dict[str, Any] = {
            "command_id": self.cmd_id, "response_id": self.rsp_id,
            "bitrate": bitrate, "serial_number": serial,
            "serial_number_hex": f"0x{serial:08X}",
            "device_id": parse_device_id_response(self.get_device_id()),
            "software_version": parse_generic_response(self.get_sw_version()),
            "serial_response": parse_generic_response(serial_message),
            "article_number": parse_generic_response(self.get_article_number()),
            "mode": parse_mode_response(self.get_mode()),
            "channels": [], "can_ids": {}, "thresholds": {},
        }
        for channel in CHANNELS:
            parsed = parse_config_response(self.get_channel_config(channel))
            parsed["name"] = channel.name
            parsed["unit"] = channel.unit
            parsed["index"] = channel.index
            parsed["default_can_id"] = channel.default_can_id
            result["channels"].append(parsed)
        for channel in CHANNELS:
            parsed = parse_can_id_response(self.get_can_id(parse_can_target(channel.name), serial))
            result["can_ids"][channel.name] = parsed["can_id"]
            result.setdefault("can_id_details", {})[channel.name] = parsed
        for target_name in ("command", "response"):
            parsed = parse_can_id_response(self.get_can_id(parse_can_target(target_name), serial))
            result["can_ids"][target_name] = parsed["can_id"]
            result.setdefault("can_id_details", {})[target_name] = parsed
        result["thresholds"] = {
            "positive": parse_threshold_response(self.get_threshold(True)),
            "negative": parse_threshold_response(self.get_threshold(False)),
        }
        return result

    def setup_bms_canb(self, startup: str = "run", serial_number: int | None = None,
                       reopen: Callable[[int], None] | None = None,
                       bitrate: int = DEFAULT_BITRATE,
                       thresholds: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
        serial = self.read_serial_number() if serial_number is None else int(serial_number)
        self.set_mode("stop", startup)
        time.sleep(0.01)
        for channel in CHANNELS:
            self.set_channel_config(channel, INTEL_SETUP_DB1, channel.default_period_ms)
            time.sleep(0.01)
        for channel in CHANNELS:
            parsed = parse_config_response(self.get_channel_config(channel))
            expected = _channel_expected(channel, INTEL_SETUP_DB1)
            if any(parsed[key] != expected[key] for key in ("db1", "period_ms")):
                raise IvtProtocolError(f"IVT 通道 {channel.name} 配置读回不一致")
            time.sleep(0.01)
        for channel, can_id in zip(CHANNELS, BMS_CANB_RESULT_IDS):
            parsed = parse_can_id_response(self.set_can_id(parse_can_target(channel.name), can_id, serial))
            if parsed["can_id"] != can_id or parsed["serial_number"] != serial:
                raise IvtProtocolError(f"IVT 通道 {channel.name} CAN ID 写入读回不一致")
            time.sleep(0.01)
        self.set_command_can_id(BMS_CANB_CMD_ID, serial)
        time.sleep(0.01)
        self.set_response_can_id(BMS_CANB_RSP_ID, serial)
        time.sleep(0.01)
        expected_targets = [(channel.name, can_id) for channel, can_id in zip(CHANNELS, BMS_CANB_RESULT_IDS)]
        expected_targets.extend([("command", BMS_CANB_CMD_ID), ("response", BMS_CANB_RSP_ID)])
        for target_name, can_id in expected_targets:
            parsed = parse_can_id_response(self.get_can_id(parse_can_target(target_name), serial))
            if parsed["can_id"] != can_id or parsed["serial_number"] != serial:
                raise IvtProtocolError(f"IVT 目标 {target_name} CAN ID 写入读回不一致")
            time.sleep(0.01)
        if thresholds is not None:
            for positive, direction in ((True, "positive"), (False, "negative")):
                target = thresholds.get(direction, {})
                self.set_threshold(positive, int(target.get("threshold_a", 0)),
                                   int(target.get("reset_threshold_a", 0)))
                time.sleep(0.01)
        self.store()
        time.sleep(0.05)
        self.restart()
        time.sleep(0.05)
        readback = self.readback(bitrate=bitrate)
        readback["setup_serial_number"] = serial
        return readback


def frame_to_dict(frame: IvtFrame) -> dict[str, Any]:
    return {"id": frame.arbitration_id, "data": list(frame.data), "extended": frame.is_extended_id}


__all__ = [
    "ALIVE_MUX", "BMS_CANB_CMD_ID", "BMS_CANB_RESULT_IDS", "BMS_CANB_RSP_ID", "BITRATE_PRESETS",
    "CHANNELS", "DEFAULT_BITRATE", "DEFAULT_CMD_ID", "DEFAULT_RSP_ID", "IvtClient", "IvtFrame",
    "IvtProtocolError", "IVT_RESPONSE_MUXES", "ResultChannel", "BMS_CANB_CHANNEL_BY_CAN_ID",
    "DEFAULT_CHANNEL_BY_CAN_ID", "ARTICLE_NUMBER_RSP_MUX", "MODE_NAME", "MODE_TO_VALUE",
    "build_db1", "compare_readback", "expected_bms_canb_config", "factory_config",
    "parse_can_id_response", "parse_can_target", "parse_channel_selector", "parse_config_response",
    "parse_device_id_response", "parse_generic_response", "parse_mode_response", "parse_serial_number_response",
    "parse_threshold_response",
]
