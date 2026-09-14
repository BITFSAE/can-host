"""Read-only discovery of PCAN channels exposed by PCAN-Basic."""

from __future__ import annotations

from typing import Any


MANUAL_PCAN_CHANNELS = [f"PCAN_USBBUS{i}" for i in range(1, 9)]


def _integer(value: Any) -> int:
    return int(getattr(value, "value", value))


def _decoded_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    return str(value or "").strip()


def _fallback(error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "automatic": False,
        "channels": list(MANUAL_PCAN_CHANNELS),
        "channel_details": [],
        "message": "当前 PCAN 驱动不支持自动枚举，已显示手动通道列表",
        "error": error,
    }


def discover_pcan_channels(api: Any | None = None) -> dict[str, Any]:
    """Return attached PCAN channels without opening or changing a CAN bus.

    PCAN_ATTACHED_CHANNELS is authoritative on current PCAN-Basic releases.  A
    manual USB channel list is retained only for older MacCAN/PCAN-Basic
    implementations that reject that read-only parameter.
    """
    try:
        from can.interfaces.pcan.basic import (
            FEATURE_FD_CAPABLE,
            FEATURE_IO_CAPABLE,
            PCAN_ATTACHED_CHANNELS,
            PCAN_CHANNEL_AVAILABLE,
            PCAN_CHANNEL_NAMES,
            PCAN_CHANNEL_OCCUPIED,
            PCAN_CHANNEL_PCANVIEW,
            PCAN_ERROR_OK,
            PCAN_NONEBUS,
            PCANBasic,
        )

        backend = api if api is not None else PCANBasic()
        status, attached = backend.GetValue(PCAN_NONEBUS, PCAN_ATTACHED_CHANNELS)
        if _integer(status) != _integer(PCAN_ERROR_OK):
            detail = f"PCAN-Basic 状态 0x{_integer(status):08X}"
            try:
                text_status, raw_text = backend.GetErrorText(status)
                if _integer(text_status) == _integer(PCAN_ERROR_OK) and _decoded_text(raw_text):
                    detail = _decoded_text(raw_text)
            except Exception:
                pass
            return _fallback(detail)

        names_by_handle = {
            _integer(handle): name
            for name, handle in PCAN_CHANNEL_NAMES.items()
            if name != "PCAN_NONEBUS"
        }
        conditions = {
            _integer(PCAN_CHANNEL_AVAILABLE): ("available", "可用"),
            _integer(PCAN_CHANNEL_OCCUPIED): ("occupied", "已被占用"),
            _integer(PCAN_CHANNEL_PCANVIEW): ("pcanview", "PCAN-View 占用"),
        }
        details: list[dict[str, Any]] = []
        for info in attached:
            handle = _integer(info.channel_handle)
            channel = names_by_handle.get(handle)
            if not channel:
                continue
            condition_value = _integer(info.channel_condition)
            condition, condition_name = conditions.get(
                condition_value, ("unavailable", "不可用")
            )
            device_name = _decoded_text(info.device_name) or "PCAN"
            controller_number = _integer(info.controller_number)
            features_value = _integer(info.device_features)
            features = []
            if features_value & _integer(FEATURE_FD_CAPABLE):
                features.append("FD")
            if features_value & _integer(FEATURE_IO_CAPABLE):
                features.append("IO")
            label = f"{channel} · {device_name} · CAN {controller_number + 1} · {condition_name}"
            details.append({
                "channel": channel,
                "label": label,
                "device_name": device_name,
                "controller_number": controller_number,
                "device_id": _integer(info.device_id),
                "condition": condition,
                "condition_name": condition_name,
                "available": condition == "available",
                "features": features,
            })

        details.sort(key=lambda item: _integer(PCAN_CHANNEL_NAMES[item["channel"]]))
        occupied = sum(not item["available"] for item in details)
        message = f"检测到 {len(details)} 个 PCAN 通道"
        if occupied:
            message += f"，其中 {occupied} 个已占用"
        if not details:
            message = "未检测到已连接的 PCAN 通道"
        return {
            "ok": True,
            "automatic": True,
            "channels": [item["channel"] for item in details],
            "channel_details": details,
            "message": message,
            "error": None,
        }
    except Exception as exc:
        return _fallback(str(exc))
