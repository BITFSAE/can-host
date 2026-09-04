"""Session-level CAN monitor aggregation and raw-frame validation.

This module does not define any vehicle protocol.  It only summarizes frames
already accepted by a protocol service and validates operator-authored raw CAN
frames for the monitor workbench.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
import re
import time
from typing import Any, Callable, Iterable

from .decoders import CanFrame


MAX_GROUPS = 2048
MAX_VARIANTS_PER_ID = 8
MIN_PERIOD_MS = 20
MAX_PERIOD_MS = 60_000


def format_can_id(arbitration_id: int, extended: bool) -> str:
    return f"0x{arbitration_id:08X}" if extended else f"0x{arbitration_id:03X}"


def format_data(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def parse_can_id(value: Any, extended: bool) -> int:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("CAN ID 不能为空")
        cleaned = re.sub(r"h$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^0x", "", cleaned, flags=re.I)
        parsed = int(cleaned, 16)
    else:
        parsed = int(value)
    upper = 0x1FFFFFFF if extended else 0x7FF
    if not 0 <= parsed <= upper:
        label = "29 位扩展帧" if extended else "11 位标准帧"
        raise ValueError(f"{label} ID 必须在 0x0..0x{upper:X} 范围内")
    return parsed


def parse_data(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        payload = bytes(value)
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            payload = b""
        else:
            tokens = [re.sub(r"^0x", "", token, flags=re.I)
                      for token in re.split(r"[\s,;:_-]+", cleaned) if token]
            if len(tokens) == 1 and len(tokens[0]) > 2:
                compact = tokens[0]
                if len(compact) % 2:
                    raise ValueError("连续十六进制数据必须是偶数字符")
                tokens = [compact[index:index + 2] for index in range(0, len(compact), 2)]
            try:
                payload = bytes(int(token, 16) for token in tokens)
            except (ValueError, OverflowError) as exc:
                raise ValueError("数据必须由 00..FF 的十六进制字节组成") from exc
    elif isinstance(value, Iterable):
        try:
            payload = bytes(int(item) for item in value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("数据数组必须只包含 0..255") from exc
    else:
        raise ValueError("数据必须是十六进制字符串或字节数组")
    if len(payload) > 8:
        raise ValueError("当前上位机只发送 Classic CAN，数据长度不能超过 8 字节")
    return payload


def normalize_message_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("发送项必须是字典")
    extended = bool(spec.get("extended", False))
    arbitration_id = parse_can_id(spec.get("id", ""), extended)
    data = parse_data(spec.get("data", ""))
    cycle_value = spec.get("cycle_ms", 200)
    try:
        cycle_ms = int(cycle_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("周期必须是整数毫秒") from exc
    if not MIN_PERIOD_MS <= cycle_ms <= MAX_PERIOD_MS:
        raise ValueError(f"周期必须在 {MIN_PERIOD_MS}..{MAX_PERIOD_MS} ms 范围内")
    name = str(spec.get("name") or "未命名发送项").strip()[:80] or "未命名发送项"
    return {
        "name": name,
        "id": format_can_id(arbitration_id, extended),
        "arbitration_id": arbitration_id,
        "extended": extended,
        "data": format_data(data),
        "data_bytes": list(data),
        "cycle_ms": cycle_ms,
    }


@dataclass
class _Variant:
    direction: str
    data: bytes
    count: int = 0
    last_timestamp: float | None = None
    intervals_ms: deque[float] = field(default_factory=lambda: deque(maxlen=24))

    def ingest(self, timestamp: float) -> None:
        if self.last_timestamp is not None and timestamp >= self.last_timestamp:
            interval = (timestamp - self.last_timestamp) * 1000.0
            if 0.01 <= interval <= 120_000:
                self.intervals_ms.append(interval)
        self.last_timestamp = timestamp
        self.count += 1

    def snapshot(self) -> dict[str, Any]:
        cycle = sum(self.intervals_ms) / len(self.intervals_ms) if self.intervals_ms else None
        return {
            "direction": self.direction,
            "dlc": len(self.data),
            "data": format_data(self.data),
            "count": self.count,
            "cycle_ms": None if cycle is None else round(cycle, 2),
            "last_time": datetime.fromtimestamp(self.last_timestamp or 0).strftime("%H:%M:%S.%f")[:-3],
        }


@dataclass
class _Group:
    arbitration_id: int
    extended: bool
    name: str
    count: int = 0
    rx_count: int = 0
    tx_count: int = 0
    last_timestamp: float | None = None
    last_rx_timestamp: float | None = None
    last_tx_timestamp: float | None = None
    rx_intervals_ms: deque[float] = field(default_factory=lambda: deque(maxlen=32))
    tx_intervals_ms: deque[float] = field(default_factory=lambda: deque(maxlen=32))
    data: bytes = b""
    changed_indices: list[int] = field(default_factory=list)
    variants: OrderedDict[tuple[str, bytes], _Variant] = field(default_factory=OrderedDict)

    def ingest(self, frame: CanFrame, name: str) -> None:
        self.name = name or self.name
        self.count += 1
        self.changed_indices = [index for index in range(max(len(self.data), len(frame.data)))
                                if (self.data[index] if index < len(self.data) else None)
                                != (frame.data[index] if index < len(frame.data) else None)]
        self.data = bytes(frame.data)
        self.last_timestamp = frame.timestamp
        if frame.direction == "tx":
            self.tx_count += 1
            if self.last_tx_timestamp is not None and frame.timestamp >= self.last_tx_timestamp:
                self.tx_intervals_ms.append((frame.timestamp - self.last_tx_timestamp) * 1000.0)
            self.last_tx_timestamp = frame.timestamp
        else:
            self.rx_count += 1
            if self.last_rx_timestamp is not None and frame.timestamp >= self.last_rx_timestamp:
                self.rx_intervals_ms.append((frame.timestamp - self.last_rx_timestamp) * 1000.0)
            self.last_rx_timestamp = frame.timestamp
        key = (frame.direction, bytes(frame.data))
        variant = self.variants.pop(key, None) or _Variant(frame.direction, bytes(frame.data))
        variant.ingest(frame.timestamp)
        self.variants[key] = variant
        while len(self.variants) > MAX_VARIANTS_PER_ID:
            self.variants.popitem(last=False)

    def snapshot(self, now: float) -> dict[str, Any]:
        intervals = self.rx_intervals_ms if self.rx_count else self.tx_intervals_ms
        cycle = sum(intervals) / len(intervals) if intervals else None
        direction = "both" if self.rx_count and self.tx_count else "tx" if self.tx_count else "rx"
        return {
            "id": format_can_id(self.arbitration_id, self.extended),
            "arbitration_id": self.arbitration_id,
            "extended": self.extended,
            "direction": direction,
            "dlc": len(self.data),
            "data": format_data(self.data),
            "changed": self.changed_indices,
            "cycle_ms": None if cycle is None else round(cycle, 2),
            "count": self.count,
            "rx_count": self.rx_count,
            "tx_count": self.tx_count,
            "name": self.name or "未登记帧",
            "last_time": datetime.fromtimestamp(self.last_timestamp or 0).strftime("%H:%M:%S.%f")[:-3],
            "age_ms": None if self.last_timestamp is None else round(max(0.0, now - self.last_timestamp) * 1000.0, 1),
            "variant_count": len(self.variants),
            "variants": [variant.snapshot() for variant in reversed(self.variants.values())],
        }


class CanMonitor:
    """Bounded per-connection aggregation keyed by CAN ID and frame type."""

    def __init__(self, name_resolver: Callable[[int, bool], str]) -> None:
        self.name_resolver = name_resolver
        self.groups: OrderedDict[tuple[int, bool], _Group] = OrderedDict()
        self.recent_observed: deque[float] = deque(maxlen=4000)
        self.total_count = 0

    def clear(self) -> None:
        self.groups.clear()
        self.recent_observed.clear()
        self.total_count = 0

    def ingest(self, frame: CanFrame) -> None:
        key = (frame.arbitration_id, frame.is_extended_id)
        group = self.groups.pop(key, None)
        name = self.name_resolver(frame.arbitration_id, frame.is_extended_id)
        if group is None:
            group = _Group(frame.arbitration_id, frame.is_extended_id, name)
        group.ingest(frame, name)
        self.groups[key] = group
        while len(self.groups) > MAX_GROUPS:
            self.groups.popitem(last=False)
        self.total_count += 1
        self.recent_observed.append(time.monotonic())

    def snapshot(self) -> dict[str, Any]:
        observed_now = time.monotonic()
        while self.recent_observed and observed_now - self.recent_observed[0] > 1.0:
            self.recent_observed.popleft()
        now = time.time()
        groups = [group.snapshot(now) for group in self.groups.values()]
        groups.sort(key=lambda item: (item["arbitration_id"], item["extended"]))
        return {
            "groups": groups,
            "id_count": len(groups),
            "total_count": self.total_count,
            "frames_per_second": len(self.recent_observed),
        }
