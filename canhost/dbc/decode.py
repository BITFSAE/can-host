"""Decoding and encoding of CAN payloads with cantools.

The decode database is built from the generated DBC text with cantools' default
signal ordering.  (Loading with ``sort_signals=None`` keeps file order, which is
what an editor wants, but cantools then fails to unpack messages whose Intel
signals are listed from high to low start bit; the team's CAN-A/CAN-B/CAN-1 files
contain such messages.)  Messages are indexed by ``(id, extended)`` because
cantools' own lookup distinguishes the two only through bit 31.
"""

from __future__ import annotations

import math
from typing import Any

import cantools
from cantools.database.can import Message

SAFE_INTEGER = 1 << 53


class DbcLoadError(ValueError):
    """The text cannot be turned into a cantools database."""


class DbcEncodeError(ValueError):
    pass


class DecodeDb:
    def __init__(self, text: str) -> None:
        self.strict_error: str | None = None
        try:
            database = cantools.database.load_string(text, database_format="dbc", strict=True)
        except Exception as exc:  # cantools raises several error types
            self.strict_error = str(exc)
            try:
                database = cantools.database.load_string(text, database_format="dbc", strict=False)
            except Exception as second:
                raise DbcLoadError(str(second)) from second
        self.database = database
        self.messages: dict[tuple[int, bool], Message] = {
            (message.frame_id, message.is_extended_frame): message for message in database.messages
        }
        self.by_name: dict[str, Message] = {message.name: message for message in database.messages}

    def message(self, frame_id: int, extended: bool) -> Message | None:
        return self.messages.get((frame_id, extended))

    # -- decode -----------------------------------------------------------

    def decode(self, message: Message, data: bytes) -> dict[str, Any]:
        """Decode one payload.

        ``status`` is ``ok``, ``dlc_short`` (fewer bytes than the DBC length;
        only signals that fit completely are returned) or ``dlc_long``
        (extra bytes are ignored but reported).  Signals of an inactive
        multiplexer branch are simply absent from ``values``."""
        status = "ok"
        if len(data) < message.length:
            status = "dlc_short"
        elif len(data) > message.length:
            status = "dlc_long"
        try:
            raw = message.decode(bytes(data), decode_choices=False, scaling=False,
                                 allow_truncated=True, allow_excess=True)
        except Exception as exc:
            return {"status": "error", "error": str(exc), "values": {}}
        values: dict[str, dict[str, Any]] = {}
        for signal in message.signals:
            if signal.name not in raw:
                continue
            raw_value = raw[signal.name]
            physical = raw_value * signal.scale + signal.offset
            choice = signal.choices.get(raw_value) if signal.choices and isinstance(raw_value, int) else None
            in_range = True
            if signal.minimum is not None and signal.maximum is not None and (signal.minimum or signal.maximum):
                tolerance = 1e-9 * (abs(signal.maximum - signal.minimum) or 1.0)
                in_range = signal.minimum - tolerance <= physical <= signal.maximum + tolerance
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                in_range = False
            values[signal.name] = {
                "raw": raw_value if not isinstance(raw_value, int) or abs(raw_value) < SAFE_INTEGER
                else str(raw_value),
                "raw_text": _raw_text(raw_value, signal.length, signal.is_signed),
                "value": physical if not isinstance(physical, int) or abs(physical) < SAFE_INTEGER
                else str(physical),
                "label": str(choice) if choice is not None else None,
                "in_range": in_range,
                "has_label": bool(signal.choices),
            }
        return {"status": status, "values": values}

    # -- encode -----------------------------------------------------------

    def encode(self, message: Message, values: dict[str, Any]) -> bytes:
        """Encode physical values (or value-table labels) into the payload.

        Signals that are not given default to their initial value, or 0.  Values
        outside the raw range are rejected; values outside [min|max] are not,
        because a bench sender is allowed to test out-of-range behaviour."""
        data: dict[str, Any] = {}
        for signal in message.signals:
            if signal.name in values and values[signal.name] is not None:
                value = values[signal.name]
                if isinstance(value, str) and signal.choices:
                    for number, label in signal.choices.items():
                        if str(label) == value:
                            value = number * signal.scale + signal.offset
                            break
                    else:
                        try:
                            value = float(value)
                        except ValueError as exc:
                            raise DbcEncodeError(f"{signal.name}：值表中没有“{value}”") from exc
                elif isinstance(value, str):
                    try:
                        value = float(value)
                    except ValueError as exc:
                        raise DbcEncodeError(f"{signal.name}：不是数字：{value}") from exc
                data[signal.name] = value
            elif signal.initial is not None:
                data[signal.name] = signal.initial
            else:
                data[signal.name] = 0 if signal.offset == 0 else signal.offset
        try:
            return bytes(message.encode(data, scaling=True, padding=False, strict=False))
        except Exception as exc:
            raise DbcEncodeError(_friendly_encode_error(str(exc), message)) from exc


def _raw_text(raw: Any, length: int, signed: bool) -> str:
    if isinstance(raw, float):
        return f"{raw:g}"
    unsigned = raw & ((1 << length) - 1) if signed else raw
    digits = max(1, (length + 3) // 4)
    return f"{raw} (0x{unsigned:0{digits}X})"


def _friendly_encode_error(text: str, message: Message) -> str:
    if "out of range" in text:
        return f"取值超出原始值范围：{text}"
    return f"编码 {message.name} 失败：{text}"
