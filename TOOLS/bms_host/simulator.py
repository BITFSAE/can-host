"""Deterministic BMS traffic source for UI development and training."""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime

from .protocol import CAN1_COMMAND_REQ_EXT_ID, TOOL_PROTOCOL_VERSION, CanFrame


class BmsSimulator:
    def __init__(self, sink: Callable[[CanFrame], None], bus_profile: str = "can1") -> None:
        if bus_profile not in {"can1", "canb", "canb_legacy"}:
            raise ValueError(f"未知模拟总线：{bus_profile}")
        self.sink = sink
        self.bus_profile = bus_profile
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

    def _state(self) -> int:
        return (3, 4, 5)[(self.tick // 60) % 3]

    def _ack(self, sequence: int, command: int, result: int, detail: int = 0) -> None:
        status_flags = (1
                        | (2 if self.config_save_pending_cycles else 0)
                        | (4 if self.direction_save_pending_cycles else 0)
                        | (8 if self.log_clear_pending_cycles else 0))
        self._send(0x18A650F4, bytes([TOOL_PROTOCOL_VERSION, sequence, command, result,
                                      self._state(), status_flags,
                                      (detail >> 8) & 0xFF, detail & 0xFF]))

    def on_command(self, frame: CanFrame) -> None:
        """Apply the same envelope, state, range, and reserved-byte checks as F405."""
        data = frame.data
        if self.bus_profile != "can1" or frame.arbitration_id != CAN1_COMMAND_REQ_EXT_ID \
                or not frame.is_extended_id:
            return
        sequence = data[1] if len(data) >= 2 else 0xFF
        operation = (data[0] & 0x0F) if data else 0
        if len(data) != 8:
            self._ack(sequence, operation, 3, 8)
            return
        if (data[0] >> 4) != TOOL_PROTOCOL_VERSION:
            self._ack(sequence, operation, 4, TOOL_PROTOCOL_VERSION)
            return

        if operation == 6:
            try:
                year = 2000 + data[2]
                if not 2000 <= year <= 2099:
                    raise ValueError
                datetime(year, data[3], data[4], data[5], data[6], data[7])
            except ValueError:
                self._send(0x18A450F4, bytes([3, sequence]) + data[2:8], True)
            else:
                self._send(0x18A450F4, bytes([0, sequence]) + data[2:8], True)
            return

        command = operation
        result = 1
        detail = 0
        if operation in {1, 2, 3, 4, 5} and self._state() not in {2, 3, 7}:
            self._ack(sequence, command, 2, self._state())
            return
        if operation == 0x0F and data[2] in {1, 2, 3, 4} and self._state() not in {2, 3, 7}:
            self._ack(sequence, 0x80 | data[2], 2, self._state())
            return
        if operation == 1:
            voltage = int.from_bytes(data[2:4], "big")
            current = int.from_bytes(data[4:6], "big")
            if (data[6:8] != b"\x00\x00"
                    or not 4154 <= voltage <= 5782 or not 0 <= current <= 45):
                result = 5
            else:
                self.request_voltage, self.request_current = voltage, current
                self.config_save_pending_cycles = 2
                detail = current
        elif operation == 2:
            ov = int.from_bytes(data[2:4], "big")
            uv = int.from_bytes(data[4:6], "big")
            ot, ut = data[6], data[7]
            if not (3011 <= ov <= 4190 and 3010 <= uv <= 4189 and ov > uv
                    and 36 <= ot <= 95 and 5 <= ut <= 79 and ot > ut):
                result = 5
            else:
                self.thresholds = [ov, uv, ot, ut]
                self.config_save_pending_cycles = 2
                detail = ov
        elif operation == 3:
            if data[4] & 0x1F or data[5:8] != b"\x00\x00\x00":
                result = 5
            else:
                self.switch_bytes = list(data[2:5])
                self.config_save_pending_cycles = 2
        elif operation == 4:
            if data[2:5] != bytes.fromhex("A5 5A 3C") or data[5:8] != b"\x00\x00\x00":
                result = 6
            else:
                result = 0
        elif operation == 5:
            if data[2] > 1 or data[3:8] != b"\x00\x00\x00\x00\x00":
                result = 5
            else:
                self.current_inverted = bool(data[2])
                self.direction_save_pending_cycles = 2
                detail = int(self.current_inverted)
        elif operation == 0x0F:
            subcommand = data[2]
            command = 0x80 | subcommand
            if subcommand == 1:
                if data[3:8] != b"\x00\x00\x00\x00\x00":
                    result = 5
                elif self.log_clear_pending_cycles:
                    result = 9
                else:
                    result = 0
                    self._ack(sequence, command, result)
                    self._send(0x18A750F4, bytes([TOOL_PROTOCOL_VERSION, sequence, 1, 0, 0, 0, 0, 1]))
                    return
            elif subcommand == 2:
                index = int.from_bytes(data[3:5], "big")
                if data[5:8] != b"\x00\x00\x00":
                    result = 5
                elif index != 0:
                    result = 8
                else:
                    detail = index
            elif subcommand == 3:
                if data[3:6] != bytes.fromhex("C3 3C A5") or data[6:8] != b"\x00\x00":
                    result = 6
                elif self.log_clear_pending_cycles:
                    result = 9
                else:
                    self.log_clear_pending_cycles = 2
            elif subcommand == 4:
                if data[4:8] != b"\x00\x00\x00\x00" or data[3] > 1:
                    result = 5
                else:
                    self.charger_type = data[3]
                    self.config_save_pending_cycles = 2
                    detail = self.charger_type
            else:
                result = 5
        else:
            result = 5

        self._ack(sequence, command, result, detail)

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
        if self.bus_profile != "can1":
            return
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

    def _emit_canb_summary(self, status: bytes, phase: int, voltage_01v: int, current_01a: int) -> None:
        """Emit only the frames available on the selected CANB simulation."""
        self._send(0x4B0, status, False)
        log_flags = 0x04 if phase == 2 else 0
        fault_data = bytes([(self._state() << 4), 0, 0, 0, 0, log_flags, 0, 2])
        self._send(0x4B1, fault_data, False)
        self._send(0x4B2, bytes(8), False)
        if self.bus_profile == "canb_legacy":
            self._send(0x18FF50E5, bytes.fromhex("16 30 00 1C 01"), True)
            return

        ivt_values = [
            current_01a * 100,
            voltage_01v * 100,
            voltage_01v * 100,
            voltage_01v * 100,
            250,
            int(round((voltage_01v / 10.0) * (current_01a / 10.0))),
            0,
            0,
        ]
        for mux, value in enumerate(ivt_values):
            raw = int(value).to_bytes(4, "little", signed=True)
            self._send(0x512 + mux, bytes([mux, 0]) + raw + b"\x00\x00", False)

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
            -25 + 8 * math.sin(self.tick / 4.0))
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
        status = (voltage_01v.to_bytes(2, "big")
                  + current_01a.to_bytes(2, "big", signed=True)
                  + bytes([78, 0x1F, state_alarm]))
        if self.bus_profile != "can1":
            self._emit_canb_summary(status, phase, voltage_01v, current_01a)
            return
        self._send(0x186050F4, status)
        self._send(0x186750F4, voltage_01v.to_bytes(2, "big"))
        max_cell, min_cell = max(cells), min(cells)
        self._send(0x186150F4, max_cell.to_bytes(2, "big") + min_cell.to_bytes(2, "big") + bytes([cells.index(max_cell), cells.index(min_cell)]))
        max_temp, min_temp = max(temps), min(temps)
        self._send(0x186250F4, bytes([max_temp, min_temp, temps.index(max_temp), temps.index(min_temp), 1, 42, 31, 0x07]))
        self._send(0x186350F4, bytes([relay_byte0, 0x08]) + self.request_voltage.to_bytes(2, "big")
                   + self.request_current.to_bytes(2, "big") + max(0, precharge_01v).to_bytes(2, "big"))
        log_flags = (0x08 if self.log_clear_pending_cycles else 0) | (0x04 if phase == 2 else 0)
        fault_data = bytes([state_alarm, 0, 0, 0, 0, log_flags, 0, 2])
        self._send(0x187650F4, fault_data)
        self._send(0x187850F4, bytes(8))
        self._send(0x4B1, fault_data, False)
        self._send(0x4B2, bytes(8), False)
        self._send(0x187750F4, self.thresholds[0].to_bytes(2, "big") + self.thresholds[1].to_bytes(2, "big") + bytes(self.thresholds[2:]))
        self._send(0x187F50F4, bytes(self.switch_bytes) + bytes([TOOL_PROTOCOL_VERSION]))
        runtime_flags = ((1 if self.current_inverted else 0)
                         | (2 if self.charger_type else 0)
                         | (0x20 if self.config_save_pending_cycles else 0)
                         | (0x40 if self.direction_save_pending_cycles else 0)
                         | 0x90)
        self._send(0x186B50F4, bytes([TOOL_PROTOCOL_VERSION, runtime_flags]) + (5680).to_bytes(2, "big")
                   + (28).to_bytes(2, "big") + bytes([0, 0x80]))
        if self.log_clear_pending_cycles:
            self.log_clear_pending_cycles -= 1
        if self.config_save_pending_cycles:
            self.config_save_pending_cycles -= 1
        if self.direction_save_pending_cycles:
            self.direction_save_pending_cycles -= 1
        self._send(0x186C50F4, bytes([TOOL_PROTOCOL_VERSION, 0x80, 0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC]))
        self._send(0x186C51F4, bytes([TOOL_PROTOCOL_VERSION, 0x1A, 0x08, 0x03, 0, 0, 0, 0]))
        self._send(0x186D50F4, bytes([TOOL_PROTOCOL_VERSION, 0xF6, 0x01, 0, 0, 0, 0, 0x0C]))
        self._send(0x186850F4, bytes([0x01, 0xF8]) + (500).to_bytes(2, "big") + (820).to_bytes(2, "big") + (1000).to_bytes(2, "big"))
        if phase == 1:
            hv_byte0 = 0x40
            precharge_ms = round(progress * 5000)
        elif phase == 2:
            hv_byte0 = 0x04  # precharge result "成功"; relay state is in 0x186350F4
            precharge_ms = 5000
        else:
            hv_byte0 = 0x00
            precharge_ms = 0
        self._send(0x186950F4, bytes([hv_byte0]) + precharge_ms.to_bytes(2, "big") + (0).to_bytes(2, "big") + bytes([0, 0, 0]))
        self._send(0x186A50F4, (1800).to_bytes(2, "big") + (80).to_bytes(2, "big") + (800).to_bytes(2, "big") + (80).to_bytes(2, "big"))
