"""Deterministic BMS traffic source for UI development and training."""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable

from .protocol import CanFrame


class BmsSimulator:
    def __init__(self, sink: Callable[[CanFrame], None]) -> None:
        self.sink = sink
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.thresholds = [4190, 3100, 90, 30]
        self.switch_bytes = [0xF3, 0x3F, 0xE0]
        self.request_voltage = 5700
        self.request_current = 30
        self.current_inverted = False
        self.charger_type = 0
        self.tick = 0
        self.log_clear_pending_cycles = 0
        self.config_save_pending_cycles = 0
        self.direction_save_pending_cycles = 0
        self.random = random.Random(405)

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="bms-simulator", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def on_command(self, frame: CanFrame) -> None:
        data = frame.data
        if frame.arbitration_id != 0x18A350F5 and (len(data) < 2 or data[0] != 3):
            return
        sequence = data[1] if len(data) >= 2 else 0xFF
        command = 0
        result = 1
        detail = 0
        if frame.arbitration_id == 0x188050F5 and len(data) == 6:
            command = 1
            self.request_voltage = int.from_bytes(data[2:4], "big")
            self.request_current = int.from_bytes(data[4:6], "big")
            self.config_save_pending_cycles = 2
            detail = self.request_current
        elif frame.arbitration_id == 0x188150F5 and len(data) == 8:
            command = 2
            self.thresholds = [int.from_bytes(data[2:4], "big"), int.from_bytes(data[4:6], "big"), data[6], data[7]]
            self.config_save_pending_cycles = 2
            detail = self.thresholds[0]
        elif frame.arbitration_id == 0x188250F5 and len(data) == 5:
            command = 3
            self.switch_bytes = list(data[2:5])
            self.config_save_pending_cycles = 2
        elif frame.arbitration_id == 0x188350F5 and len(data) == 6:
            command = 4
            result = 0
        elif frame.arbitration_id == 0x18A150F5 and len(data) == 3:
            command = 5
            self.current_inverted = bool(data[2])
            self.direction_save_pending_cycles = 2
            detail = int(self.current_inverted)
        elif frame.arbitration_id == 0x18A350F5 and len(data) == 8:
            self.sink(CanFrame(0x18A450F4, bytes([0]) + data[:7], True))
            return
        elif frame.arbitration_id == 0x18A550F5 and len(data) >= 3:
            operation = data[2]
            command = 0x80 | operation
            if operation == 1 and len(data) == 3:
                if self.log_clear_pending_cycles:
                    self._send(0x18A650F4, bytes([3, sequence, command, 5, 3, 9, 0, 0]))
                    return
                result = 0
                self._send(0x18A650F4, bytes([3, sequence, command, result, 3, 1, 0, 0]))
                self._send(0x18A750F4, bytes([3, sequence, 1, 0, 0, 0, 0, 1]))
                return
            if operation == 3 and len(data) == 6:
                self.log_clear_pending_cycles = 2
                detail = 0
            elif operation == 4 and len(data) == 4:
                self.charger_type = data[3]
                self.config_save_pending_cycles = 2
                detail = self.charger_type
            else:
                result = 5
        else:
            return
        status_flags = (1
                        | (2 if self.config_save_pending_cycles else 0)
                        | (4 if self.direction_save_pending_cycles else 0)
                        | (8 if self.log_clear_pending_cycles else 0))
        self._send(0x18A650F4, bytes([3, sequence, command, result, 3, status_flags,
                                     (detail >> 8) & 0xFF, detail & 0xFF]))

    def _send(self, can_id: int, data: bytes, extended: bool = True) -> None:
        self.sink(CanFrame(can_id, data, extended))

    def _run(self) -> None:
        next_cells = 0.0
        next_summary = 0.0
        while not self.stop_event.wait(0.02):
            now = time.monotonic()
            if now >= next_cells:
                next_cells = now + 0.18
                self._emit_cells()
            if now >= next_summary:
                next_summary = now + 0.50
                self._emit_summary()
                self.tick += 1

    def _cell_value(self, index: int) -> int:
        slow = 23 * math.sin(self.tick / 18.0)
        module_bias = (index // 23 - 2.5) * 3
        ripple = 18 * math.sin(index * 0.43 + self.tick * 0.08)
        return round(3835 + slow + module_bias + ripple)

    def _temp_code(self, index: int) -> int:
        return round(30 + 29 + 5 * math.sin(index * 0.67 + self.tick * 0.04))

    def _emit_cells(self) -> None:
        for slave in range(6):
            values = [self._cell_value(slave * 23 + i) for i in range(23)]
            first = b"\x00\x00" + b"".join(value.to_bytes(2, "little") for value in values[:3])
            self._send(0x180050F3 + ((slave * 6) << 16), first)
            for frame_index in range(1, 6):
                start = 3 + (frame_index - 1) * 4
                payload = b"".join(value.to_bytes(2, "little") for value in values[start:start + 4])
                self._send(0x180050F3 + ((slave * 6 + frame_index) << 16), payload)
            temps = bytes(self._temp_code(slave * 8 + i) for i in range(8))
            self._send(0x184050F3 + (slave << 16), temps)

    def _emit_summary(self) -> None:
        cells = [self._cell_value(i) for i in range(138)]
        temps = [self._temp_code(i) for i in range(48)]
        voltage_01v = round(sum(cells) / 100)
        # A 90-second demo cycle: 30 s standby, 30 s precharge (PRE ramps to the
        # pack voltage), then 30 s high-voltage charging. The final phase keeps
        # charge_mode set so the overview timer and remaining-time estimate can
        # be checked without injecting a raw fault frame by hand.
        phase = (self.tick // 60) % 3
        progress = (self.tick % 60) / 60.0
        state = (3, 4, 5)[phase]
        current_01a = round(25 + 8 * math.sin(self.tick / 4.0)) if phase == 2 else round(
            10000 - 25 + 8 * math.sin(self.tick / 4.0))
        state_alarm = (state << 4) | 0
        if phase == 0:
            relay_byte0 = 0x00
            precharge_01v = 0
        elif phase == 1:
            relay_byte0 = 0x04
            precharge_01v = round(voltage_01v * progress)
        else:
            relay_byte0 = 0x50
            precharge_01v = voltage_01v
        self._send(0x186050F4, voltage_01v.to_bytes(2, "big") + current_01a.to_bytes(2, "big") + bytes([78, 0, state_alarm]))
        self._send(0x186750F4, voltage_01v.to_bytes(2, "big"))
        max_cell, min_cell = max(cells), min(cells)
        self._send(0x186150F4, max_cell.to_bytes(2, "big") + min_cell.to_bytes(2, "big") + bytes([cells.index(max_cell), cells.index(min_cell)]))
        max_temp, min_temp = max(temps), min(temps)
        self._send(0x186250F4, bytes([max_temp, min_temp, temps.index(max_temp), temps.index(min_temp), 1, 42, 31, 0x07]))
        self._send(0x186350F4, bytes([relay_byte0, 0x08]) + self.request_voltage.to_bytes(2, "big")
                   + self.request_current.to_bytes(2, "big") + max(0, precharge_01v).to_bytes(2, "big"))
        log_flags = (0x08 if self.log_clear_pending_cycles else 0) | (0x04 if phase == 2 else 0)
        self._send(0x187650F4, bytes([state_alarm, 0, 0, 0, 0, log_flags, 0, 2]))
        self._send(0x187850F4, bytes(8))
        self._send(0x187750F4, self.thresholds[0].to_bytes(2, "big") + self.thresholds[1].to_bytes(2, "big") + bytes(self.thresholds[2:]))
        self._send(0x187F50F4, bytes(self.switch_bytes) + bytes([3]))
        runtime_flags = ((1 if self.current_inverted else 0)
                         | (2 if self.charger_type else 0)
                         | (0x20 if self.config_save_pending_cycles else 0)
                         | (0x40 if self.direction_save_pending_cycles else 0)
                         | 0x90)
        self._send(0x186B50F4, bytes([3, runtime_flags]) + (5680).to_bytes(2, "big")
                   + (28).to_bytes(2, "big") + bytes([0, 0x80]))
        if self.log_clear_pending_cycles:
            self.log_clear_pending_cycles -= 1
        if self.config_save_pending_cycles:
            self.config_save_pending_cycles -= 1
        if self.direction_save_pending_cycles:
            self.direction_save_pending_cycles -= 1
        self._send(0x186C50F4, bytes.fromhex("03 80 12 34 56 78 9A BC"))
        self._send(0x186C51F4, bytes.fromhex("03 1A 08 03 00 00 00 00"))
        self._send(0x186D50F4, bytes.fromhex("03 F6 01 00 00 00 00 0C"))
        self._send(0x186850F4, bytes([0x01, 0xF8]) + (500).to_bytes(2, "big") + (820).to_bytes(2, "big") + (1000).to_bytes(2, "big"))
        if phase == 1:
            hv_byte0 = 0x40
            precharge_ms = round(progress * 5000)
        elif phase == 2:
            hv_byte0 = 0x34  # positive + negative closed, precharge result "成功"
            precharge_ms = 5000
        else:
            hv_byte0 = 0x00
            precharge_ms = 0
        self._send(0x186950F4, bytes([hv_byte0]) + precharge_ms.to_bytes(2, "big") + (0).to_bytes(2, "big") + bytes([0, 0, 0]))
        self._send(0x186A50F4, (1800).to_bytes(2, "big") + (80).to_bytes(2, "big") + (800).to_bytes(2, "big") + (80).to_bytes(2, "big"))
