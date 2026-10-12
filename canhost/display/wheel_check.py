"""Reads what a physical steering wheel did, from the display's CANB output alone.

The wheel talks to the display on its private CAN2, which the bench does not
see.  What the display then puts on CANB is enough to tell which control moved:
``0x310`` is the drive-mode request relayed verbatim, ``0x703`` merges slip
level, lap-timer key and clear key.  Each frame is translated into plain words
and checked against the rules the display firmware (HMI_Display
``Core/Src/can.c``) and the central DBC promise: frame length, reserved bits,
mode-code range, never both keys in one frame, and the 300 ms rising-edge
debounce.  It cannot know what the wheel sent, so a control that was operated
but produced no row means the display did not send anything.  Pure logic with
injected time; no I/O.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Callable

from .. import decoders

DEBOUNCE_S = 0.3          # CAN_BUTTON_REPEAT_GAP_MS in the display firmware
DEBOUNCE_TOLERANCE_S = 0.05   # reception jitter on the PC side
ROW_LIMIT = 40
SHOWN_ROWS = 20
KNOB_NAMES = {0: "S 直线", 1: "Q 避障", 2: "C 八字", 3: "E 耐久"}


def _hex(data: bytes) -> str:
    return data.hex(" ").upper() or "（空）"


class WheelObserver:
    def __init__(self, *, wall_clock: Callable[[], float]) -> None:
        self._wall_clock = wall_clock
        self.reset()

    def reset(self) -> None:
        self._rows: deque[dict[str, Any]] = deque(maxlen=ROW_LIMIT)
        self._sequence = 0
        self._slip: int | None = None
        self._knob: tuple[int, float] | None = None
        self._slip_at: float | None = None
        self._lap_last: float | None = None
        self._clear_last: float | None = None
        self.lap_count = 0
        self.clear_count = 0
        self.ok_count = 0
        self.bad_count = 0

    def display_frame(self, now: float, frame_id: int, data: bytes) -> None:
        """Interpret and check one ``0x703`` or ``0x310`` the display sent on CANB."""
        if frame_id == decoders.STEERING_COMMAND_ID:
            action, problems, warnings = self._command(now, data)
        elif frame_id == decoders.DRIVE_MODE_REQUEST_ID:
            action, problems, warnings = self._mode_request(now, data)
        else:
            return
        self._sequence += 1
        status = "bad" if problems else "warn" if warnings else "ok"
        if problems:
            self.bad_count += 1
        else:
            self.ok_count += 1
        self._rows.appendleft({
            "id": self._sequence, "time": datetime.fromtimestamp(self._wall_clock()).strftime("%H:%M:%S.%f")[:-3],
            "at": now, "action": action, "frame": f"0x{frame_id:03X} · {_hex(data)}",
            "status": status, "notes": problems + warnings,
        })

    def _command(self, now: float, data: bytes) -> tuple[str, list[str], list[str]]:
        if len(data) != 2:
            return "方向盘指令（长度异常）", [f"DLC 应为 2，实际 {len(data)}"], []
        problems: list[str] = []
        slip, flags = data[0], data[1]
        if slip > 7:
            problems.append("滑级字节大于 7")
        if flags & 0xFC:
            problems.append("保留位不为 0")
        lap, clear = bool(flags & 1), bool(flags & 2)
        if lap and clear:
            problems.append("计时位与消错位同时为 1，车手屏不会把两个键合并在一帧里")
        for pressed, last, name in ((lap, self._lap_last, "计时位"), (clear, self._clear_last, "消错位")):
            if pressed and last is not None and now - last < DEBOUNCE_S - DEBOUNCE_TOLERANCE_S:
                problems.append(f"{name}在 {round((now - last) * 1000)} ms 内再次为 1，车手屏本应按 300 ms 去重")
        previous, self._slip = self._slip, slip & 7
        self._slip_at = now
        if lap:
            self.lap_count += 1
            self._lap_last = now
        if clear:
            self.clear_count += 1
            self._clear_last = now
        if lap and clear:
            action = "圈速计时键 + 消错键（异常）"
        elif lap:
            action = f"圈速计时键 按下（当前滑级 {slip & 7}）"
        elif clear:
            action = f"消错键 按下（当前滑级 {slip & 7}）"
        elif previous is None:
            action = f"滑级 {slip & 7}（电位器转动，或按键的重复帧）"
        elif previous != (slip & 7):
            action = f"电位器 → 滑级 {slip & 7}（原为 {previous}）"
        else:
            action = f"滑级 {slip & 7} 未变（按键的重复帧，或电位器停在原位）"
        return action, problems, []

    def _mode_request(self, now: float, data: bytes) -> tuple[str, list[str], list[str]]:
        if not data:
            return "旋钮（空帧）", ["帧没有数据"], []
        problems: list[str] = []
        warnings: list[str] = []
        mode = data[0] - decoders.DRIVE_MODE_ASCII_ZERO
        if not 0 <= mode <= 3:
            problems.append(f"模式码 0x{data[0]:02X} 不在 '0'..'3'，车手屏本应丢弃而不是转发")
            action = f"旋钮 → 无效模式码 0x{data[0]:02X}"
        else:
            self._knob = (mode, now)
            action = f"旋钮 → {KNOB_NAMES[mode]}（车手屏原样转发给 ECU）"
        if len(data) != 8:
            warnings.append(f"DLC 为 {len(data)}，中央库约定 8；车手屏原样转发，需核对方向盘发送的长度")
        elif any(data[1:]):
            warnings.append("Byte1..7 不为 0，中央库约定为 0")
        return action, problems, warnings

    def snapshot(self, now: float) -> dict[str, Any]:
        age = lambda stamp: None if stamp is None else round(now - stamp, 1)
        rows = [{key: value for key, value in row.items() if key != "at"} | {"age": age(row["at"])}
                for row in list(self._rows)[:SHOWN_ROWS]]
        return {
            "knob": None if self._knob is None else {"mode": self._knob[0], "name": KNOB_NAMES[self._knob[0]],
                                                     "age": age(self._knob[1])},
            "slip": None if self._slip is None else {"level": self._slip, "age": age(self._slip_at)},
            "lap": {"count": self.lap_count, "age": age(self._lap_last)},
            "clear": {"count": self.clear_count, "age": age(self._clear_last)},
            "summary": {"ok": self.ok_count, "bad": self.bad_count},
            "rows": rows,
        }
