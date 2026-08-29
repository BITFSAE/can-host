"""Decode the team ``TelemetryFrame`` used on ``fsae/telemetry``.

The generated module beside this file is a synchronized artifact of
``vehicle-interfaces/telemetry/fsae_telemetry.proto``.  This module owns only
host-side presentation and fault-word interpretation; it does not define a
second telemetry contract.
"""

from __future__ import annotations

from typing import Any

from ..bms.protocol import ALARM_NAMES


SEVERITY_NAMES = {
    0: "未知",
    1: "提示",
    2: "警告",
    3: "错误",
    4: "严重故障",
}

BMS_STATE_NAMES = {
    2: "自检",
    3: "待机",
    4: "预充",
    5: "高压接通",
    7: "故障保持",
}

BMS_ALARM_LEVEL_NAMES = {
    0: "正常",
    1: "一级故障",
    2: "二级告警",
}


def _optional(message: Any, field: str) -> Any | None:
    """Return an optional proto3 scalar only when its presence bit is set."""
    try:
        return getattr(message, field) if message.HasField(field) else None
    except ValueError:
        return getattr(message, field, None)


def _message_present(message: Any, field: str) -> bool:
    try:
        return message.HasField(field)
    except ValueError:
        return False


def decode_telemetry_payload(payload: bytes) -> dict[str, Any]:
    """Parse one MQTT payload into the compact snapshot needed by the UI."""
    if not payload:
        raise ValueError("TelemetryFrame Payload 为空")
    try:
        from . import fsae_telemetry_pb2
        from google.protobuf.message import DecodeError
    except ImportError as exc:  # pragma: no cover - exercised by connection error path
        raise RuntimeError("缺少 Protobuf 运行库，请重新安装上位机依赖") from exc

    frame = fsae_telemetry_pb2.TelemetryFrame()
    try:
        frame.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError(f"TelemetryFrame Protobuf 解码失败：{exc}") from exc

    present_domains = [field.name for field, _ in frame.ListFields()]
    if not present_domains:
        raise ValueError("Payload 中没有可识别的 TelemetryFrame 字段")

    # During the compatibility period the gateway fills both fields from the
    # same BMS alarm word. Prefer the dedicated BMS field, while still accepting
    # an older sender that only fills the legacy field.
    legacy_fault = int(frame.fault_code)
    battery_fault = int(frame.battery_fault_code)
    if battery_fault != 0 or legacy_fault == 0:
        fault_code = battery_fault
        fault_source = "battery_fault_code"
    else:
        fault_code = legacy_fault
        fault_source = "fault_code（兼容）"

    active_faults = [
        {"bit": bit, "name": ALARM_NAMES[bit]}
        for bit in range(32)
        if fault_code & (1 << bit)
    ]

    alarms = [{
        "id": int(item.alarm_id),
        "id_hex": f"0x{int(item.alarm_id):X}",
        "severity": int(item.severity),
        "severity_name": SEVERITY_NAMES.get(int(item.severity), f"未知 {int(item.severity)}"),
        "message": item.message,
    } for item in frame.alarms]

    header = frame.header if _message_present(frame, "header") else None
    bms = frame.bms_telemetry if _message_present(frame, "bms_telemetry") else None
    battery_state = _optional(bms, "battery_state") if bms is not None else None
    alarm_level = _optional(bms, "battery_alarm_level") if bms is not None else None
    bms_state_valid = battery_state is not None and alarm_level is not None
    # The two legacy fault scalars have no proto3 presence. G473 only adds the
    # optional BMS state/alarm fields while its upstream CAN data is fresh. A
    # non-zero word is still actionable for an older sender; zero without the
    # presence-bearing BMS fields must remain unknown rather than "normal".
    fault_valid = fault_code != 0 or bms_state_valid

    return {
        "timestamp_ms": int(frame.timestamp_ms),
        "frame_id": int(frame.frame_id),
        "header": {
            "timestamp_ms": int(header.timestamp_ms),
            "sequence": int(header.seq),
            "source_id": int(header.source_id),
        } if header is not None else None,
        "fault": {
            "code": fault_code,
            "code_hex": f"0x{fault_code:08X}",
            "source": fault_source,
            "legacy_code_hex": f"0x{legacy_fault:08X}",
            "battery_code_hex": f"0x{battery_fault:08X}",
            # A zero proto3 scalar has no presence information. Treat a true
            # conflict only when both compatibility fields are non-zero.
            "sources_mismatch": legacy_fault != 0 and battery_fault != 0
            and legacy_fault != battery_fault,
            "valid": fault_valid,
            "active": active_faults,
        },
        "bms": {
            "valid": bms_state_valid,
            "state": battery_state,
            "state_name": BMS_STATE_NAMES.get(battery_state, f"未知 {battery_state}")
            if battery_state is not None else None,
            "alarm_level": alarm_level,
            "alarm_level_name": BMS_ALARM_LEVEL_NAMES.get(alarm_level, f"未知 {alarm_level}")
            if alarm_level is not None else None,
        },
        "alarms": alarms,
        "summary": {
            "hv_voltage_v": float(frame.hv_voltage),
            "hv_current_a": float(frame.hv_current),
            "soc_pct": int(frame.battery_soc),
            "battery_temp_max_c": float(frame.battery_temp_max),
        },
        "present_domains": present_domains,
        "payload_bytes": len(payload),
    }


__all__ = [
    "BMS_ALARM_LEVEL_NAMES",
    "BMS_STATE_NAMES",
    "SEVERITY_NAMES",
    "decode_telemetry_payload",
]
