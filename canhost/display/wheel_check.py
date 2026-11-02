"""Reads what a physical steering wheel did, from the display's CANB output alone.

The wheel talks to the display on its private CAN2, which the bench does not
see.  The display relays both wheel frames verbatim to CANB: ``0x310`` is the
drive-mode request (held back while a timing session locks the mode) and
``0x703`` is the whole steering command the wheel built (slip level, exit-enable
request, clear key, lap-timer key; relayed even under the lock).  Each frame is
translated into plain words and checked against the central DBC and the wheel
firmware (repo ``Steering_wheel`` ``Core/Src/button_panel.c``): frame length,
reserved bits, mode-code range, at most one key bit per frame, and the
exit-enable request sent every 20 ms while held, at least 3 frames, ended by
one frame with the bit at 0.  Those repeated frames are folded into one row.
A control that was operated but produced no row means the display did not
send anything.  Pure logic with injected time; no I/O.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Callable

from .. import decoders

DEBOUNCE_S = 0.3          # CAN_BUTTON_REPEAT_GAP_MS in the display firmware
DEBOUNCE_TOLERANCE_S = 0.05   # reception jitter on the PC side
DISABLE_PERIOD_S = 0.02   # DISABLE_TX_PERIOD_MS in the wheel firmware
DISABLE_MIN_FRAMES = 3    # DISABLE_MIN_FRAMES in the wheel firmware
DISABLE_GAP_WARN_S = 0.1  # five periods without a frame: probably lost on the way
DISABLE_STALE_S = 1.0     # no frame at all for this long: the closing 0 frame never came
ROW_LIMIT = 40
SHOWN_ROWS = 20
KNOB_NAMES = {-1: "默认", 0: "S 直线", 1: "Q 避障", 2: "C 八字", 3: "E 耐久"}


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
        self._disable_last: float | None = None
        # The exit-enable request in progress: first/last frame time, frame count and its row.
        self._disable_run: dict[str, Any] | None = None
        self.lap_count = 0
        self.clear_count = 0
        self.disable_count = 0
        self.ok_count = 0
        self.bad_count = 0

    def display_frame(self, now: float, frame_id: int, data: bytes) -> None:
        """Interpret and check one ``0x703`` or ``0x310`` the display sent on CANB."""
        if frame_id == decoders.STEERING_COMMAND_ID:
            self._expire(now)
            self._command(now, data)
        elif frame_id == decoders.DRIVE_MODE_REQUEST_ID:
            self._add_row(now, frame_id, data, *self._mode_request(now, data))

    # -- rows -------------------------------------------------------------

    def _add_row(self, now: float, frame_id: int, data: bytes, action: str,
                 problems: list[str], warnings: list[str]) -> dict[str, Any]:
        self._sequence += 1
        row = {"id": self._sequence, "rev": 0,
               "time": datetime.fromtimestamp(self._wall_clock()).strftime("%H:%M:%S.%f")[:-3],
               "at": now, "action": action, "frame": "", "status": "ok", "notes": []}
        self._rows.appendleft(row)
        self.ok_count += 1
        self._amend(row, frame_id, data, action, problems, warnings)
        return row

    def _amend(self, row: dict[str, Any], frame_id: int | None, data: bytes | None, action: str,
               problems: list[str], warnings: list[str]) -> None:
        """Update a row in place; a row turning bad moves from the ok to the bad count."""
        if frame_id is not None and data is not None:
            row["frame"] = f"0x{frame_id:03X} · {_hex(data)}"
        row["action"] = action
        row["rev"] += 1
        notes = row["notes"]
        notes[:] = ([note for note in problems if note not in notes] + notes
                    + [note for note in warnings if note not in notes])
        if problems and row["status"] != "bad":
            row["status"] = "bad"
            self.ok_count -= 1
            self.bad_count += 1
        elif warnings and row["status"] == "ok":
            row["status"] = "warn"

    # -- 0x703 ------------------------------------------------------------

    def _command(self, now: float, data: bytes) -> None:
        frame_id = decoders.STEERING_COMMAND_ID
        if len(data) < 2:
            self._add_row(now, frame_id, data, "方向盘指令（长度异常）",
                          [f"DLC 应为 2，实际 {len(data)}；不足 2 字节的帧车手屏本应丢弃"], [])
            return
        problems: list[str] = []
        warnings: list[str] = []
        if len(data) != 2:
            warnings.append(f"DLC 为 {len(data)}，中央库约定 2；车手屏原样转发，需核对方向盘发送的长度")
        slip_byte, flags = data[0], data[1]
        slip = slip_byte & 7
        if slip_byte > 7:
            problems.append("滑级字节大于 7（车手屏原样转发但不显示）")
        if flags & 0xF8:
            problems.append("保留位不为 0")
        disable = bool(flags & decoders.STEERING_DISABLE_REQUEST)
        clear = bool(flags & decoders.STEERING_ERROR_CLEAR)
        lap = bool(flags & decoders.STEERING_LAP_TOGGLE)
        if disable + clear + lap > 1:
            problems.append("退使位、消错位、计时位中有两个同时为 1，方向盘每帧最多带一种按键动作")
        for pressed, last, name in ((lap, self._lap_last, "计时位"), (clear, self._clear_last, "消错位")):
            if pressed and last is not None and now - last < DEBOUNCE_S - DEBOUNCE_TOLERANCE_S:
                warnings.append(f"{name}在 {round((now - last) * 1000)} ms 内再次为 1，车手屏按 300 ms 去重只算一次")
        previous, self._slip = self._slip, slip
        self._slip_at = now
        if lap:
            self.lap_count += 1
            self._lap_last = now
        if clear:
            self.clear_count += 1
            self._clear_last = now

        run = self._disable_run
        if disable:
            self._disable_last = now
            if run is None:
                self.disable_count += 1
                row = self._add_row(now, frame_id, data, "", problems, warnings)
                self._disable_run = run = {"start": now, "last": now, "frames": 1, "row": row}
            else:
                gap = now - run["last"]
                if gap > DISABLE_GAP_WARN_S:
                    warnings.append(f"两帧退使请求相隔 {round(gap * 1000)} ms，方向盘每 20 ms 发一帧，可能丢帧")
                run["frames"] += 1
                run["last"] = now
            self._amend(run["row"], frame_id, data,
                        f"退使组合键 按住：DisableRequest=1 已 {run['frames']} 帧（当前滑级 {slip}）",
                        problems, warnings)
            return
        if run is not None:
            # The frame with the bit back at 0 closes the request; it is folded into the same row.
            self._disable_run = None
            if run["frames"] < DISABLE_MIN_FRAMES:
                problems.append(f"只收到 {run['frames']} 帧 DisableRequest=1，方向盘应至少发 {DISABLE_MIN_FRAMES} 帧")
            if lap or clear:
                problems.append("组合键结束帧不应带计时或消错位，两键同时按下过时松开不触发单键动作")
            duration = round((now - run["start"]) * 1000)
            self._amend(run["row"], frame_id, data,
                        f"退使组合键 已松开：DisableRequest=1 共 {run['frames']} 帧、约 {duration} ms，"
                        f"随后 1 帧为 0（当前滑级 {slip}）", problems, warnings)
            return

        if lap:
            action = f"圈速计时键 按下（当前滑级 {slip}）"
        elif clear:
            action = f"消错键 按下（当前滑级 {slip}）"
        elif previous is None:
            action = f"滑级 {slip}（电位器转动，或方向盘上电）"
        elif previous != slip:
            action = f"电位器 → 滑级 {slip}（原为 {previous}）"
        else:
            action = f"滑级 {slip} 未变（方向盘重新上电，或前面的退使请求帧没有收到）"
        self._add_row(now, frame_id, data, action, problems, warnings)

    def _expire(self, now: float) -> None:
        """Close a request whose frames stopped without the 0 frame the wheel must send."""
        run = self._disable_run
        if run is None or now - run["last"] <= DISABLE_STALE_S:
            return
        self._disable_run = None
        self._amend(run["row"], None, None,
                    f"退使组合键：DisableRequest=1 共 {run['frames']} 帧，之后没有结束帧",
                    [f"超过 {DISABLE_STALE_S:g} s 没有收到 DisableRequest=0 的结束帧"], [])

    # -- 0x310 ------------------------------------------------------------

    def _mode_request(self, now: float, data: bytes) -> tuple[str, list[str], list[str]]:
        if not data:
            return "旋钮（空帧）", ["帧没有数据"], []
        problems: list[str] = []
        warnings: list[str] = []
        mode = data[0] - decoders.DRIVE_MODE_ASCII_ZERO
        if not -1 <= mode <= 3:
            problems.append(f"模式码 0x{data[0]:02X} 不在 47..51，车手屏本应丢弃而不是转发")
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
        self._expire(now)
        age = lambda stamp: None if stamp is None else round(now - stamp, 1)
        rows = [{key: value for key, value in row.items() if key != "at"}
                | {"notes": list(row["notes"]), "age": age(row["at"])}
                for row in list(self._rows)[:SHOWN_ROWS]]
        return {
            "knob": None if self._knob is None else {"mode": self._knob[0], "name": KNOB_NAMES[self._knob[0]],
                                                     "age": age(self._knob[1])},
            "slip": None if self._slip is None else {"level": self._slip, "age": age(self._slip_at)},
            "lap": {"count": self.lap_count, "age": age(self._lap_last)},
            "clear": {"count": self.clear_count, "age": age(self._clear_last)},
            "disable": {"count": self.disable_count, "age": age(self._disable_last),
                        "active": self._disable_run is not None},
            "summary": {"ok": self.ok_count, "bad": self.bad_count},
            "rows": rows,
        }
