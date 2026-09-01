"""Deterministic CANB vehicle traffic source for UI development.

Emits representative frames from every node the vehicle page monitors:
BMS mirror 0x4B0/0x4B1, the SOP pair 0x4A0/0x4A3, own IVT results, the
competition meter (current and U1 only - the power/energy frames are
commonly not sent by the device), PDM low-voltage telemetry, FanController
status frames, the ECU debug frames at their real 10 ms cadence, and the
four tyre-temperature frames.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable

from ..decoders import CanFrame, crc8_sae_j1850


class VehicleSimulator:
    def __init__(self, sink: Callable[[CanFrame], None]) -> None:
        self.sink = sink
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.tick = 0
        self.random = random.Random(519)

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="vehicle-simulator", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def _send(self, can_id: int, data: bytes) -> None:
        self.sink(CanFrame(can_id, data, False))

    def _run(self) -> None:
        next_fast = 0.0    # 10 ms: ECU debug frames
        next_sop = 0.0     # 10 ms: SOP pair
        next_mid = 0.0     # 100 ms: competition meter, tyres
        next_slow = 0.0    # 500 ms: pack, fault, PDM, fan
        while not self.stop_event.wait(0.005):
            now = time.monotonic()
            if now >= next_fast:
                next_fast = now + 0.01
                self._emit_ecu()
            if now >= next_sop:
                next_sop = now + 0.01
                self._emit_sop()
            if now >= next_mid:
                next_mid = now + 0.10
                self._emit_meter()
                self._emit_tires()
            if now >= next_slow:
                next_slow = now + 0.50
                self._emit_pack_and_fault()
                self._emit_pdm()
                self._emit_fan()
                self.tick += 1

    # -- emitters -----------------------------------------------------------

    def _pack(self) -> tuple[float, float]:
        voltage = 548.0 + 4.0 * math.sin(self.tick / 20.0)
        current = 28.0 + 12.0 * math.sin(self.tick / 4.0)
        return voltage, current

    def _emit_pack_and_fault(self) -> None:
        voltage, current = self._pack()
        voltage_raw = round(voltage * 10)
        current_raw = round(current * 10)
        status = (voltage_raw.to_bytes(2, "big") + current_raw.to_bytes(2, "big", signed=True)
                  + bytes([76, 0x1F, (5 << 4) | 0]))
        self._send(0x4B0, status)
        self._send(0x4B1, bytes([(5 << 4) | 0, 0, 0, 0, 0, 0, 0, 4]))

    def _emit_sop(self) -> None:
        limits = (1800).to_bytes(2, "little") + (900).to_bytes(2, "little") \
            + (740).to_bytes(2, "little") + (370).to_bytes(2, "little")
        self._send(0x4A0, limits)
        header = bytes([(1 << 4) | (self.tick & 0x0F), 0x47, 5]) \
            + (0).to_bytes(2, "little") + bytes([0xFF])
        body = header + bytes([0x20])
        crc_input = bytes.fromhex("04 A0") + limits + bytes.fromhex("04 A3") + body
        self._send(0x4A3, body + bytes([crc8_sae_j1850(crc_input)]))

    def _emit_meter(self) -> None:
        _, current = self._pack()
        voltage, _ = self._pack()
        # Competition meter frames are big-endian and only current/U1 are
        # reliably present on the car.
        for can_id, mux, value in ((0x521, 0x00, round(current * 1000)), (0x522, 0x01, round(voltage * 1000))):
            raw = int(value).to_bytes(4, "big", signed=True)
            self._send(can_id, bytes([mux, self.tick & 0x0F]) + raw + b"\x00\x00")

    def _emit_pdm(self) -> None:
        def side(voltage: float, current: float) -> bytes:
            # The power field is unsigned; the PDM firmware clamps negative
            # products to zero, so the simulator does the same.
            power_w = max(0, round(voltage * current * 10))
            return (round(voltage * 1000).to_bytes(2, "big")
                    + round(current * 100).to_bytes(2, "big", signed=True)
                    + power_w.to_bytes(2, "big")
                    + (17 + self.tick % 5).to_bytes(2, "big"))

        self._send(0x5A0, side(23.8 + 0.2 * math.sin(self.tick / 9.0), 9.5 + 3 * math.sin(self.tick / 3.0)))
        self._send(0x5A1, side(25.9 + 0.1 * math.sin(self.tick / 15.0), -2.0 + 0.8 * math.sin(self.tick / 6.0)))

    def _emit_fan(self) -> None:
        rpm1 = 2400 + round(600 * math.sin(self.tick / 5.0))
        rpm2 = rpm1 + 120
        rpm3 = max(0, 1800 + round(700 * math.sin(self.tick / 7.0)))
        self._send(0x5A2, rpm1.to_bytes(2, "big") + rpm2.to_bytes(2, "big")
                   + rpm3.to_bytes(2, "big") + bytes([42, 38]))
        motor_temp = round((52 + 8 * math.sin(self.tick / 11.0)) * 10)
        controller_temp = round((41 + 4 * math.sin(self.tick / 13.0)) * 10)
        self._send(0x5A3, bytes([0x00, 0x2F]) + motor_temp.to_bytes(2, "big", signed=True)
                   + controller_temp.to_bytes(2, "big", signed=True) + bytes([42, 38]))
        self._send(0x5A6, bytes([35, 40, 60, 30, 20, 0, 0, 0]))
        self._send(0x5A7, bytes([1, 50, 50, 5, 50, 0, 0, 0]))
        self._send(0x5A8, bytes([0x03, 42, 38, 42, 38, 180, 95, 0]))
        self._send(0x5A9, bytes([0, 0, 0, 0, 0, 2, 0, 0]))
        self._send(0x5AE, bytes([1, 15, 55, 1, 55, 3, 0, 0]))
        battery_rpm = 2100 + round(300 * math.sin(self.tick / 6.0))
        self._send(0x5AA, battery_rpm.to_bytes(2, "big") + bytes([40, 55, 0x08, 0x27, 0, 1]))
        self._send(0x5AD, bytes([1, 35, 70, 35, 70, 0, 0, 0]))

    def _emit_ecu(self) -> None:
        phase = (self.tick % 60) / 60.0
        torque = round(300 * math.sin(phase * math.pi))
        velocity = round(1800 * math.sin(phase * math.pi))
        motor = round(380 + 60 * phase)
        inverter = round(310 + 40 * phase)
        igbt = round(290 + 35 * phase)
        for frame_id, base in ((0x502, torque), (0x505, velocity), (0x506, motor),
                               (0x507, inverter), (0x508, igbt)):
            values = [base, base - 15, base + 10, base - 5]
            payload = b"".join(value.to_bytes(2, "little", signed=True) for value in values)
            self._send(frame_id, payload)
        status = bytes([0x10 | 0x00, 0x0F, 0xF0, 0x44, 0x33])
        self._send(0x509, status)

    def _emit_tires(self) -> None:
        for index, can_id in enumerate((0x071, 0x072, 0x073, 0x074)):
            payload = bytearray(8)
            for point in range(4):
                temperature = 48 + index * 2 + point + round(3 * math.sin((self.tick + point * 3) / 8.0))
                payload[point * 2] = temperature
                payload[point * 2 + 1] = self.random.randrange(100)
            self._send(can_id, bytes(payload))
