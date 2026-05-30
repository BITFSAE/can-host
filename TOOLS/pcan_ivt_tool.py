#!/usr/bin/env python3
"""IVT-S PCAN CLI tool based on python-can.

常见用法:
    python3 pcan_ivt_tool.py wait-alive
    python3 pcan_ivt_tool.py info
    python3 pcan_ivt_tool.py get-config --channel all
    python3 pcan_ivt_tool.py set-config --channel all --mode cyclic --byte-order little
    python3 pcan_ivt_tool.py setup-intel
    python3 pcan_ivt_tool.py monitor --byte-order little --seconds 10
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Literal, Sequence

try:
    import can  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install python-can") from exc

if TYPE_CHECKING:
    ByteOrder = Literal["big", "little"]


DEFAULT_CMD_ID = 0x411
DEFAULT_RSP_ID = 0x511
DEFAULT_BITRATE = 500000
DEFAULT_CHANNEL = "PCAN_USBBUS1"

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

DEVICE_TYPE_NAME = {
    0x00: "unknown",
    0x01: "IVT",
    0x02: "IVT-S",
}
FEATURE_NAME = {
    0x00: "none",
    0x01: "switch",
    0x02: "relay",
    0x03: "isolation",
}
COMMUNICATION_NAME = {
    0x00: "CAN (no termination)",
    0x01: "CAN1 (with termination)",
}
SUPPLY_NAME = {
    0x00: "5V",
    0x01: "12/24V",
}


@dataclasses.dataclass(frozen=True)
class ResultChannel:
    index: int
    name: str
    set_mux: int
    get_mux: int
    rsp_mux: int
    can_id: int
    default_period_ms: int
    scale: float
    unit: str
    default_mode: str
    can_id_set_mux: int
    can_id_get_mux: int
    can_id_rsp_mux: int


CHANNELS: List[ResultChannel] = [
    ResultChannel(0, "I", 0x20, 0x60, 0xA0, 0x521, 20, 0.001, "A", "cyclic", 0x10, 0x50, 0x90),
    ResultChannel(1, "U1", 0x21, 0x61, 0xA1, 0x522, 60, 0.001, "V", "cyclic", 0x11, 0x51, 0x91),
    ResultChannel(2, "U2", 0x22, 0x62, 0xA2, 0x523, 60, 0.001, "V", "cyclic", 0x12, 0x52, 0x92),
    ResultChannel(3, "U3", 0x23, 0x63, 0xA3, 0x524, 60, 0.001, "V", "cyclic", 0x13, 0x53, 0x93),
    ResultChannel(4, "T", 0x24, 0x64, 0xA4, 0x525, 100, 0.1, "degC", "disabled", 0x14, 0x54, 0x94),
    ResultChannel(5, "W", 0x25, 0x65, 0xA5, 0x526, 30, 1.0, "W", "disabled", 0x15, 0x55, 0x95),
    ResultChannel(6, "As", 0x26, 0x66, 0xA6, 0x527, 30, 1.0, "As", "disabled", 0x16, 0x56, 0x96),
    ResultChannel(7, "Wh", 0x27, 0x67, 0xA7, 0x528, 30, 1.0, "Wh", "disabled", 0x17, 0x57, 0x97),
]

CHANNEL_BY_NAME = {channel.name.lower(): channel for channel in CHANNELS}
CHANNEL_BY_CAN_ID = {channel.can_id: channel for channel in CHANNELS}
CHANNEL_BY_INDEX = {channel.index: channel for channel in CHANNELS}

SPECIAL_CAN_TARGETS = {
    "command": (0x1D, 0x5D, 0x9D),
    "response": (0x1F, 0x5F, 0x9F),
}

INTEL_SETUP_DB1 = 0x42


def parse_hex_bytes(text: str) -> List[int]:
    cleaned = text.replace(",", " ").strip()
    if not cleaned:
        raise ValueError("empty byte string")
    values = []
    for part in cleaned.split():
        value = int(part, 16)
        if value < 0 or value > 0xFF:
            raise ValueError(f"byte out of range: {part}")
        values.append(value)
    return values


def parse_u32(value: str) -> int:
    parsed = int(value, 0)
    if parsed < 0 or parsed > 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("value must be in range 0..0xFFFFFFFF")
    return parsed


def parse_can_id(value: str) -> int:
    parsed = int(value, 0)
    if parsed < 0 or parsed > 0x7FF:
        raise argparse.ArgumentTypeError("CAN ID must be 11-bit standard ID (0..0x7FF)")
    return parsed


def bytes_text(data: Iterable[int]) -> str:
    return " ".join(f"{value:02X}" for value in data)


def encode_u32_be(value: int) -> List[int]:
    return [
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    ]


def parse_channel_selector(text: str) -> List[ResultChannel]:
    if text.lower() == "all":
        return CHANNELS
    names = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not names:
        raise argparse.ArgumentTypeError("channel selector is empty")
    selected = []
    for name in names:
        try:
            selected.append(CHANNEL_BY_NAME[name])
        except KeyError as exc:
            raise argparse.ArgumentTypeError(f"unknown channel: {name}") from exc
    return selected


def parse_can_target(text: str) -> tuple[str, int, int, int]:
    key = text.strip().lower()
    if key in SPECIAL_CAN_TARGETS:
        set_mux, get_mux, rsp_mux = SPECIAL_CAN_TARGETS[key]
        return key, set_mux, get_mux, rsp_mux
    if key in CHANNEL_BY_NAME:
        channel = CHANNEL_BY_NAME[key]
        return channel.name, channel.can_id_set_mux, channel.can_id_get_mux, channel.can_id_rsp_mux
    raise argparse.ArgumentTypeError(f"unknown CAN target: {text}")


class IvtProtocolError(RuntimeError):
    pass


class IvtPcanTool:
    def __init__(self, channel: str, bitrate: int, cmd_id: int, rsp_id: int) -> None:
        self.cmd_id = cmd_id
        self.rsp_id = rsp_id
        self.bus = can.Bus(interface="pcan", channel=channel, bitrate=bitrate)

    def close(self) -> None:
        self.bus.shutdown()

    def send(self, arbitration_id: int, data: Sequence[int]) -> None:
        if len(data) > 8:
            raise ValueError("CAN 2.0A payload must not exceed 8 bytes")
        self.bus.send(
            can.Message(
                arbitration_id=arbitration_id,
                is_extended_id=False,
                data=list(data),
            )
        )

    def recv_match(
        self,
        predicate: Callable[[can.Message], bool],
        timeout: float,
    ) -> can.Message:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timeout waiting for CAN frame")
            message = self.bus.recv(timeout=remaining)
            if message is None:
                continue
            if predicate(message):
                return message

    def request(self, data: Sequence[int], expect_mux: int, timeout: float = 0.5) -> can.Message:
        self.send(self.cmd_id, data)
        message = self.recv_match(
            lambda msg: (
                (not msg.is_extended_id)
                and msg.arbitration_id == self.rsp_id
                and len(msg.data) >= 1
                and msg.data[0] in {expect_mux, ILLEGAL_COMMAND_MUX}
            ),
            timeout,
        )
        if message.data[0] == ILLEGAL_COMMAND_MUX:
            bad_mux = message.data[1] if len(message.data) > 1 else None
            raise IvtProtocolError(
                f"device returned illegal-command response for mux 0x{bad_mux:02X}"
                if bad_mux is not None
                else "device returned illegal-command response"
            )
        return message

    def wait_alive(self, timeout: float = 3.0) -> can.Message:
        return self.recv_match(
            lambda msg: (
                (not msg.is_extended_id)
                and msg.arbitration_id == self.rsp_id
                and len(msg.data) >= 1
                and msg.data[0] == ALIVE_MUX
            ),
            timeout,
        )

    def set_mode(self, current: str, startup: str) -> can.Message:
        current_value = 0x00 if current == "stop" else 0x01
        startup_value = 0x00 if startup == "stop" else 0x01
        return self.request([0x34, current_value, startup_value, 0, 0, 0, 0, 0], SET_MODE_RSP_MUX)

    def get_mode(self) -> can.Message:
        return self.request([0x74, 0, 0, 0, 0, 0, 0, 0], SET_MODE_RSP_MUX)

    def set_channel_config(self, channel: ResultChannel, db1: int, period_ms: int) -> can.Message:
        high = (period_ms >> 8) & 0xFF
        low = period_ms & 0xFF
        payload = [channel.set_mux, db1, high, low, 0, 0, 0, 0]
        return self.request(payload, channel.rsp_mux)

    def get_channel_config(self, channel: ResultChannel) -> can.Message:
        return self.request([channel.get_mux, 0, 0, 0, 0, 0, 0, 0], channel.rsp_mux)

    def set_can_id(self, target: tuple[str, int, int, int], can_id: int, serial_number: int) -> can.Message:
        _, set_mux, _, rsp_mux = target
        payload = [set_mux, (can_id >> 8) & 0xFF, can_id & 0xFF, *encode_u32_be(serial_number), 0]
        return self.request(payload, rsp_mux)

    def get_can_id(self, target: tuple[str, int, int, int], serial_number: int) -> can.Message:
        _, _, get_mux, rsp_mux = target
        payload = [get_mux, 0, 0, *encode_u32_be(serial_number), 0]
        return self.request(payload, rsp_mux)

    def set_threshold(self, positive: bool, threshold_a: int, reset_threshold_a: int) -> can.Message:
        mux = 0x35 if positive else 0x36
        rsp_mux = THRESHOLD_POS_RSP_MUX if positive else THRESHOLD_NEG_RSP_MUX
        payload = [
            mux,
            (threshold_a >> 8) & 0xFF,
            threshold_a & 0xFF,
            (reset_threshold_a >> 8) & 0xFF,
            reset_threshold_a & 0xFF,
            0,
            0,
            0,
        ]
        return self.request(payload, rsp_mux)

    def get_threshold(self, positive: bool) -> can.Message:
        mux = 0x75 if positive else 0x76
        rsp_mux = THRESHOLD_POS_RSP_MUX if positive else THRESHOLD_NEG_RSP_MUX
        return self.request([mux, 0, 0, 0, 0, 0, 0, 0], rsp_mux)

    def trigger(self, channels: Sequence[ResultChannel]) -> can.Message:
        bitmap = 0
        for channel in channels:
            bitmap |= 1 << channel.index
        payload = [0x31, (bitmap >> 8) & 0xFF, bitmap & 0xFF, 0, 0, 0, 0, 0]
        return self.request(payload, TRIGGER_RSP_MUX)

    def store(self) -> can.Message:
        return self.request([0x32, 0, 0, 0, 0, 0, 0, 0], STORE_RSP_MUX, timeout=1.2)

    def restart(self) -> can.Message:
        self.send(self.cmd_id, [0x3F, 0, 0, 0, 0, 0, 0, 0])
        return self.wait_alive(timeout=3.0)

    def restart_to_bitrate(self, bitrate: int) -> can.Message:
        mapping = {250000: 0x08, 500000: 0x04, 1000000: 0x02}
        try:
            selector = mapping[bitrate]
        except KeyError as exc:
            raise ValueError("restart-to-bitrate only supports 250000/500000/1000000") from exc
        self.request([0x3A, selector, 0, 0, 0, 0, 0, 0], STORE_RSP_MUX, timeout=2.0)
        return self.wait_alive(timeout=4.0)

    def get_device_id(self) -> can.Message:
        return self.request([0x79, 0, 0, 0, 0, 0, 0, 0], DEVICE_ID_RSP_MUX)

    def get_sw_version(self) -> can.Message:
        return self.request([0x7A, 0, 0, 0, 0, 0, 0, 0], SW_VERSION_RSP_MUX)

    def get_serial_number(self) -> can.Message:
        return self.request([0x7B, 0, 0, 0, 0, 0, 0, 0], SERIAL_NUMBER_RSP_MUX)

    def get_article_number(self) -> can.Message:
        return self.request([0x7C, 0, 0, 0, 0, 0, 0, 0], ARTICLE_NUMBER_RSP_MUX)

    def setup_intel(self, startup: str) -> None:
        self.set_mode("stop", startup)
        time.sleep(0.01)
        for channel in CHANNELS:
            self.set_channel_config(channel, INTEL_SETUP_DB1, channel.default_period_ms)
            time.sleep(0.01)
        for channel in CHANNELS:
            parsed = parse_config_response(self.get_channel_config(channel))
            if parsed["db1"] != INTEL_SETUP_DB1 or parsed["period_ms"] != channel.default_period_ms:
                raise RuntimeError(f"config verify failed for {channel.name}")
            time.sleep(0.01)
        self.store()
        time.sleep(0.05)
        self.restart()
        time.sleep(0.05)
        for channel in CHANNELS:
            parsed = parse_config_response(self.get_channel_config(channel))
            if parsed["db1"] != INTEL_SETUP_DB1 or parsed["period_ms"] != channel.default_period_ms:
                raise RuntimeError(f"post-restart verify failed for {channel.name}")
            time.sleep(0.01)

    def monitor_results(self, byte_order: str, seconds: float, show_raw: bool = False) -> None:
        if byte_order not in {"big", "little"}:
            raise ValueError(f"unsupported byte order: {byte_order}")
        order: ByteOrder = "big" if byte_order == "big" else "little"
        deadline = None if seconds <= 0 else (time.monotonic() + seconds)
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            message = self.bus.recv(timeout=0.5)
            if message is None:
                continue
            if message.is_extended_id or message.arbitration_id not in CHANNEL_BY_CAN_ID:
                continue
            channel = CHANNEL_BY_CAN_ID[message.arbitration_id]
            decoded = decode_result(message, channel, order)
            raw_text = f" raw=[{bytes_text(message.data)}]" if show_raw else ""
            print(
                f"0x{message.arbitration_id:03X} {channel.name:<2} "
                f"count={decoded['msg_count']:>2} state=0x{decoded['state']:X} "
                f"raw={decoded['raw']:>11} value={decoded['value']:.3f} {channel.unit}{raw_text}"
            )


def parse_config_response(message: can.Message) -> Dict[str, int | List[int] | str | bool]:
    data = list(message.data)
    db1 = data[1]
    mode = db1 & 0x0F
    return {
        "mux": data[0],
        "db1": db1,
        "mode": mode,
        "mode_name": MODE_NAME.get(mode, f"unknown({mode})"),
        "report_errors": bool(db1 & 0x20),
        "byte_order": "little" if (db1 & 0x40) else "big",
        "invert_sign": bool(db1 & 0x80),
        "period_ms": (data[2] << 8) | data[3],
        "raw": data,
    }


def parse_mode_response(message: can.Message) -> Dict[str, int | str | List[int]]:
    data = list(message.data)
    current_value = data[1]
    startup_value = data[2]
    value_to_name = {0x00: "stop", 0x01: "run"}
    return {
        "current": current_value,
        "current_name": value_to_name.get(current_value, f"unknown({current_value})"),
        "startup": startup_value,
        "startup_name": value_to_name.get(startup_value, f"unknown({startup_value})"),
        "raw": data,
    }


def parse_threshold_response(message: can.Message) -> Dict[str, int | List[int] | bool]:
    data = list(message.data)
    threshold_a = (data[1] << 8) | data[2]
    reset_threshold_a = (data[3] << 8) | data[4]
    return {
        "threshold_a": threshold_a,
        "reset_threshold_a": reset_threshold_a,
        "threshold_enabled": threshold_a != 0,
        "reset_threshold_enabled": reset_threshold_a != 0,
        "raw": data,
    }


def parse_can_id_response(message: can.Message) -> Dict[str, int | List[int]]:
    data = list(message.data)
    return {
        "can_id": ((data[1] << 8) | data[2]) & 0x7FF,
        "serial_number": int.from_bytes(bytes(data[3:7]), byteorder="big", signed=False),
        "raw": data,
    }


def parse_device_id_response(message: can.Message) -> Dict[str, int | str | List[int]]:
    data = list(message.data)
    nominal_current_a = data[2] * 16 + ((data[3] >> 4) & 0x0F)
    voltage_channels = data[3] & 0x0F
    device_type = data[1]
    feature = data[4]
    communication = data[5]
    supply = data[6]
    return {
        "device_type": device_type,
        "device_type_name": DEVICE_TYPE_NAME.get(device_type, f"unknown(0x{device_type:02X})"),
        "nominal_current_a": nominal_current_a,
        "voltage_channels": voltage_channels,
        "feature": feature,
        "feature_name": FEATURE_NAME.get(feature, f"unknown(0x{feature:02X})"),
        "communication": communication,
        "communication_name": COMMUNICATION_NAME.get(communication, f"unknown(0x{communication:02X})"),
        "supply": supply,
        "supply_name": SUPPLY_NAME.get(supply, f"unknown(0x{supply:02X})"),
        "raw": data,
    }


def decode_result(message: can.Message, channel: ResultChannel, byte_order: "ByteOrder") -> Dict[str, int | float]:
    data = list(message.data)
    raw = int.from_bytes(bytes(data[2:6]), byteorder=byte_order, signed=True)
    return {
        "mux": data[0],
        "msg_count": data[1] & 0x0F,
        "state": (data[1] >> 4) & 0x0F,
        "raw": raw,
        "value": raw * channel.scale,
    }


def format_bool(enabled: bool) -> str:
    return "on" if enabled else "off"


def print_message(prefix: str, message: can.Message) -> None:
    print(f"{prefix} 0x{message.arbitration_id:03X}: {bytes_text(message.data)}")


def print_config(message: can.Message, channel: ResultChannel) -> None:
    parsed = parse_config_response(message)
    print(
        f"{channel.name:<2} mode={parsed['mode_name']:<9} order={parsed['byte_order']:<6} "
        f"period={parsed['period_ms']:>4}ms report_errors={format_bool(bool(parsed['report_errors']))} "
        f"invert_sign={format_bool(bool(parsed['invert_sign']))} raw=[{bytes_text(parsed['raw'])}]"
    )


def print_mode(message: can.Message) -> None:
    parsed = parse_mode_response(message)
    print(
        f"current={parsed['current_name']} startup={parsed['startup_name']} "
        f"raw=[{bytes_text(parsed['raw'])}]"
    )


def print_threshold(message: can.Message, direction: str) -> None:
    parsed = parse_threshold_response(message)
    print(
        f"{direction:<8} threshold={parsed['threshold_a']}A "
        f"reset={parsed['reset_threshold_a']}A raw=[{bytes_text(parsed['raw'])}]"
    )


def print_can_id(message: can.Message, target_name: str) -> None:
    parsed = parse_can_id_response(message)
    print(
        f"{target_name:<8} can-id=0x{parsed['can_id']:03X} "
        f"serial=0x{parsed['serial_number']:08X} raw=[{bytes_text(parsed['raw'])}]"
    )


def print_device_id(message: can.Message) -> None:
    parsed = parse_device_id_response(message)
    print(
        f"device={parsed['device_type_name']} "
        f"nominal_current={parsed['nominal_current_a']}A "
        f"voltage_channels={parsed['voltage_channels']} "
        f"feature={parsed['feature_name']} "
        f"communication={parsed['communication_name']} "
        f"supply={parsed['supply_name']} "
        f"raw=[{bytes_text(parsed['raw'])}]"
    )


def print_generic_info(label: str, message: can.Message) -> None:
    data = list(message.data)
    payload = data[1:]
    payload_hex = bytes_text(payload)
    payload_u32 = int.from_bytes(bytes(payload[0:4]), byteorder="big", signed=False) if len(payload) >= 4 else 0
    print(f"{label}: payload=[{payload_hex}] u32_be=0x{payload_u32:08X} raw=[{bytes_text(data)}]")


def build_db1(
    mode: str | None,
    byte_order: str | None,
    report_errors: bool | None,
    invert_sign: bool | None,
    base_db1: int | None,
) -> int:
    if base_db1 is not None:
        db1 = base_db1 & 0xFF
    else:
        db1 = 0
    if mode is not None:
        db1 = (db1 & 0xF0) | MODE_TO_VALUE[mode]
    if report_errors is not None:
        db1 = (db1 | 0x20) if report_errors else (db1 & ~0x20)
    if byte_order is not None:
        db1 = (db1 | 0x40) if byte_order == "little" else (db1 & ~0x40)
    if invert_sign is not None:
        db1 = (db1 | 0x80) if invert_sign else (db1 & ~0x80)
    return db1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channel", "--pcan-channel", default=DEFAULT_CHANNEL, help="PCAN channel name")
    parser.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE, help="CAN bitrate")
    parser.add_argument("--cmd-id", type=parse_can_id, default=DEFAULT_CMD_ID, help="command CAN ID")
    parser.add_argument("--rsp-id", type=parse_can_id, default=DEFAULT_RSP_ID, help="response CAN ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IVT-S PCAN configuration CLI")
    add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    alive = subparsers.add_parser("wait-alive", help="wait for alive frame")
    alive.add_argument("--timeout", type=float, default=3.0)

    raw = subparsers.add_parser("raw", help="send raw command bytes")
    raw.add_argument("data", help="8 hex bytes, ex: '34 00 01 00 00 00 00 00'")
    raw.add_argument("--expect-mux", type=lambda value: int(value, 0))
    raw.add_argument("--timeout", type=float, default=0.5)

    mode = subparsers.add_parser("set-mode", help="set current/startup mode")
    mode.add_argument("--current", choices=["stop", "run"], required=True)
    mode.add_argument("--startup", choices=["stop", "run"], required=True)

    subparsers.add_parser("get-mode", help="read current/startup mode")

    get_cfg = subparsers.add_parser("get-config", help="read channel config")
    get_cfg.add_argument("--channel", dest="result_channel", default="all", help="I,U1,U2,U3,T,W,As,Wh or all")

    set_cfg = subparsers.add_parser("set-config", help="set channel config")
    set_cfg.add_argument("--channel", dest="result_channel", required=True, help="I,U1,U2,U3,T,W,As,Wh or all")
    set_cfg.add_argument("--period-ms", type=int, help="period/trigger delay in ms")
    set_cfg.add_argument("--mode", choices=sorted(MODE_TO_VALUE), help="disabled/triggered/cyclic")
    set_cfg.add_argument("--byte-order", choices=["big", "little"], help="result payload byte order")
    set_cfg.add_argument("--report-errors", action="store_true", help="set DB1 bit5")
    set_cfg.add_argument("--no-report-errors", action="store_true", help="clear DB1 bit5")
    set_cfg.add_argument("--invert-sign", action="store_true", help="set DB1 bit7")
    set_cfg.add_argument("--no-invert-sign", action="store_true", help="clear DB1 bit7")
    set_cfg.add_argument("--db1", type=lambda value: int(value, 0), help="set DB1 directly")
    set_cfg.add_argument("--from-current", action="store_true", help="read current config first, then patch fields")

    get_can = subparsers.add_parser("get-can-id", help="read configured CAN ID")
    get_can.add_argument("--target", required=True, help="I/U1/U2/U3/T/W/As/Wh/command/response")
    get_can.add_argument("--serial-number", type=parse_u32, required=True)

    set_can = subparsers.add_parser("set-can-id", help="set configured CAN ID, stop mode only")
    set_can.add_argument("--target", required=True, help="I/U1/U2/U3/T/W/As/Wh/command/response")
    set_can.add_argument("--serial-number", type=parse_u32, required=True)
    set_can.add_argument("--can-id", type=parse_can_id, required=True)

    get_threshold = subparsers.add_parser("get-threshold", help="read OC threshold")
    get_threshold.add_argument("--direction", choices=["positive", "negative"], required=True)

    set_threshold = subparsers.add_parser("set-threshold", help="set OC threshold, stop mode only")
    set_threshold.add_argument("--direction", choices=["positive", "negative"], required=True)
    set_threshold.add_argument("--threshold-a", type=int, required=True, help="0 means off")
    set_threshold.add_argument("--reset-threshold-a", type=int, required=True, help="0 means off")

    trigger = subparsers.add_parser("trigger", help="trigger one or more result channels in run mode")
    trigger.add_argument("--channel", dest="result_channel", required=True, help="comma-separated list, or all")

    subparsers.add_parser("device-id", help="read device identification")
    subparsers.add_parser("sw-version", help="read software version")
    subparsers.add_parser("serial-number", help="read serial number")
    subparsers.add_parser("article-number", help="read article number")
    subparsers.add_parser("info", help="read device-id/mode/sw-version/serial/article in one shot")

    subparsers.add_parser("store", help="store config to non-volatile memory")
    subparsers.add_parser("restart", help="restart device and wait alive")

    restart_bitrate = subparsers.add_parser("restart-to-bitrate", help="restart to configured bitrate")
    restart_bitrate.add_argument("--target-bitrate", type=int, choices=[250000, 500000, 1000000], required=True)

    setup = subparsers.add_parser("setup-intel", help="full setup for 0..7 cyclic little-endian")
    setup.add_argument("--startup", choices=["stop", "run"], default="run")

    monitor = subparsers.add_parser("monitor", help="monitor result frames")
    monitor.add_argument("--byte-order", choices=["big", "little"], required=True)
    monitor.add_argument("--seconds", type=float, default=0.0, help="0 means forever")
    monitor.add_argument("--show-raw", action="store_true")

    return parser


def build_tool(args: argparse.Namespace) -> IvtPcanTool:
    return IvtPcanTool(
        channel=args.channel,
        bitrate=args.bitrate,
        cmd_id=args.cmd_id,
        rsp_id=args.rsp_id,
    )


def main(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    tool = build_tool(args)
    try:
        if args.command == "wait-alive":
            print_message("alive", tool.wait_alive(timeout=args.timeout))
            return 0

        if args.command == "raw":
            data = parse_hex_bytes(args.data)
            if len(data) != 8:
                raise SystemExit("raw command requires exactly 8 bytes")
            if args.expect_mux is None:
                tool.send(tool.cmd_id, data)
                print(f"tx 0x{tool.cmd_id:03X}: {bytes_text(data)}")
                return 0
            print_message("rsp", tool.request(data, args.expect_mux, timeout=args.timeout))
            return 0

        if args.command == "set-mode":
            print_message("rsp", tool.set_mode(args.current, args.startup))
            return 0

        if args.command == "get-mode":
            print_mode(tool.get_mode())
            return 0

        if args.command == "get-config":
            for channel in parse_channel_selector(args.result_channel):
                print_config(tool.get_channel_config(channel), channel)
            return 0

        if args.command == "set-config":
            if args.report_errors and args.no_report_errors:
                raise SystemExit("cannot use --report-errors and --no-report-errors together")
            if args.invert_sign and args.no_invert_sign:
                raise SystemExit("cannot use --invert-sign and --no-invert-sign together")
            if (
                args.db1 is None
                and args.mode is None
                and args.byte_order is None
                and not args.report_errors
                and not args.no_report_errors
                and not args.invert_sign
                and not args.no_invert_sign
            ):
                raise SystemExit("set-config needs --db1 or at least one friendly DB1 option")
            channels = parse_channel_selector(args.result_channel)
            for channel in channels:
                base_db1 = None
                period_ms = args.period_ms
                if args.from_current:
                    current = parse_config_response(tool.get_channel_config(channel))
                    base_db1 = int(current["db1"])
                    if period_ms is None:
                        period_ms = int(current["period_ms"])
                if period_ms is None:
                    raise SystemExit("--period-ms is required unless --from-current is used")
                db1 = build_db1(
                    mode=args.mode,
                    byte_order=args.byte_order,
                    report_errors=True if args.report_errors else False if args.no_report_errors else None,
                    invert_sign=True if args.invert_sign else False if args.no_invert_sign else None,
                    base_db1=args.db1 if args.db1 is not None else base_db1,
                )
                print_config(tool.set_channel_config(channel, db1, period_ms), channel)
            return 0

        if args.command == "get-can-id":
            target = parse_can_target(args.target)
            print_can_id(tool.get_can_id(target, args.serial_number), target[0])
            return 0

        if args.command == "set-can-id":
            target = parse_can_target(args.target)
            print_can_id(tool.set_can_id(target, args.can_id, args.serial_number), target[0])
            return 0

        if args.command == "get-threshold":
            positive = args.direction == "positive"
            print_threshold(tool.get_threshold(positive), args.direction)
            return 0

        if args.command == "set-threshold":
            positive = args.direction == "positive"
            print_threshold(
                tool.set_threshold(positive, args.threshold_a, args.reset_threshold_a),
                args.direction,
            )
            return 0

        if args.command == "trigger":
            print_message("rsp", tool.trigger(parse_channel_selector(args.result_channel)))
            return 0

        if args.command == "device-id":
            print_device_id(tool.get_device_id())
            return 0

        if args.command == "sw-version":
            print_generic_info("sw-version", tool.get_sw_version())
            return 0

        if args.command == "serial-number":
            print_generic_info("serial-number", tool.get_serial_number())
            return 0

        if args.command == "article-number":
            print_generic_info("article-number", tool.get_article_number())
            return 0

        if args.command == "info":
            print_device_id(tool.get_device_id())
            print_mode(tool.get_mode())
            print_generic_info("sw-version", tool.get_sw_version())
            print_generic_info("serial-number", tool.get_serial_number())
            print_generic_info("article-number", tool.get_article_number())
            return 0

        if args.command == "store":
            print_message("rsp", tool.store())
            return 0

        if args.command == "restart":
            print_message("alive", tool.restart())
            return 0

        if args.command == "restart-to-bitrate":
            print_message("alive", tool.restart_to_bitrate(args.target_bitrate))
            return 0

        if args.command == "setup-intel":
            tool.setup_intel(startup=args.startup)
            print("setup-intel done")
            return 0

        if args.command == "monitor":
            tool.monitor_results(args.byte_order, args.seconds, show_raw=args.show_raw)
            return 0

        raise SystemExit(f"unsupported command: {args.command}")
    finally:
        tool.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
