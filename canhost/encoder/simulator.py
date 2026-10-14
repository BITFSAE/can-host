"""A software BRT encoder for source-run previews and tests.

``SimulatedEncoder`` models the documented command set at frame level; ``SimulatedEncoderBus`` offers
the small slice of the python-can ``Bus`` interface the service uses.  It reproduces the manual's
examples, not the vendor firmware: details the manual leaves open (what the position does when the
direction flips, the layout of auto-report frames beyond the query-answer layout) are assumptions.
"""

from __future__ import annotations

from collections import deque
import math
import random
import threading
import time
from typing import Any, Callable

from .. import decoders as d

try:
    import can
except ImportError:  # pragma: no cover - python-can ships with every supported install
    can = None  # type: ignore[assignment]

RES = d.BRT_RESOLUTION


def default_motion(now: float) -> float:
    """Shaft position in counts: 5 s standing still, then a smooth 270 degree turn, repeating.

    The pauses let a zero or midpoint write read back cleanly; the turns exercise the wrap-around,
    the speed curve and the "still moving" warning.
    """
    cycle, phase = divmod(now, 10.0)
    base = cycle * 3072.0
    if phase < 5.0:
        return base
    progress = (phase - 5.0) / 5.0
    return base + 3072.0 * (1.0 - math.cos(math.pi * progress)) / 2.0


def _ack(address: int, func: int, status: int = 0) -> bytes:
    return bytes([4, address, func, status])


def _value(address: int, func: int, value: int) -> bytes:
    return bytes([7, address, func]) + (value & 0xFFFFFFFF).to_bytes(4, "little")


class SimulatedEncoder:
    def __init__(self, address: int = d.BRT_DEFAULT_ADDRESS, *, motion: Callable[[float], float] = default_motion,
                 noise_counts: float = 0.6, seed: int = 25) -> None:
        self.address = address
        self.motion = motion
        self.noise_counts = noise_counts
        self._random = random.Random(seed)
        self.offset = 0.0
        self.sign = 1
        self.auto_report = False
        self.period_us = 5000
        self.speed_time_ms = 100
        self.pending_bitrate: int | None = None
        self.stall_ack = False
        self._next_report: float | None = None

    # -- position model ---------------------------------------------------

    def _shaft(self, now: float) -> float:
        return self.motion(now)

    def accumulated(self, now: float) -> float:
        return self._shaft(now) * self.sign + self.offset

    def raw(self, now: float) -> int:
        noise = self._random.uniform(-self.noise_counts, self.noise_counts) if self.noise_counts else 0.0
        return int(round(self.accumulated(now) + noise)) % RES

    def _speed_counts(self, now: float) -> int:
        step = 1e-3
        rate = (self.accumulated(now) - self.accumulated(now - step)) / step
        return int(round(rate * self.speed_time_ms / 1000.0))

    # -- commands ---------------------------------------------------------

    def handle(self, can_id: int, data: bytes, now: float) -> list[tuple[int, bytes]]:
        """Answer one frame addressed to the device; frames for other IDs or malformed ones get nothing."""
        if can_id != self.address or len(data) < 4 or data[0] != len(data) or data[1] != self.address:
            return []
        if self.stall_ack:
            return []
        func, address = data[2], self.address
        reply: list[tuple[int, bytes]] = []
        if func == d.BRT_FUNC_POSITION and len(data) == 4:
            reply = [(address, _value(address, func, self.raw(now)))]
        elif func == d.BRT_FUNC_MULTITURN and len(data) == 4:
            reply = [(address, _value(address, func, int(self.accumulated(now)) & 0xFFFFFFFF))]
        elif func == d.BRT_FUNC_TURNS and len(data) == 4:
            reply = [(address, _value(address, func, int(self.accumulated(now) // RES) & 0xFFFFFFFF))]
        elif func == d.BRT_FUNC_SPEED and len(data) == 4:
            reply = [(address, _value(address, func, self._speed_counts(now)))]
        elif func == d.BRT_FUNC_REPORT and len(data) == 4:
            self.auto_report = data[3] == d.BRT_REPORT_POSITION
            self._next_report = now if self.auto_report else None
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_PERIOD and len(data) == 5:
            period = data[3] | (data[4] << 8)
            if period < d.BRT_PERIOD_US_RANGE[0]:
                return [(address, _ack(address, func, 1))]
            self.period_us = period
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_ZERO and len(data) == 4:
            self.offset = -self._shaft(now) * self.sign
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_MIDPOINT and len(data) == 4:
            self.offset = RES // 2 - self._shaft(now) * self.sign
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_DIRECTION and len(data) == 4:
            self.sign = -1 if data[3] == 1 else 1
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_SPEED_TIME and len(data) == 5:
            self.speed_time_ms = max(1, data[3] | (data[4] << 8))
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_SET_COUNT and len(data) == 7:
            target = int.from_bytes(data[3:7], "little")
            if target >= RES:
                return [(address, _ack(address, func, 1))]
            self.offset = target - self._shaft(now) * self.sign
            reply = [(address, _ack(address, func))]
        elif func == d.BRT_FUNC_ADDRESS and len(data) == 4 and data[3] != 0:
            self.address = data[3]
            reply = [(self.address, _ack(self.address, func))]
        elif func == d.BRT_FUNC_BITRATE and len(data) == 4 and data[3] in d.BRT_BITRATE_CODES.values():
            self.pending_bitrate = next(rate for rate, code in d.BRT_BITRATE_CODES.items() if code == data[3])
            reply = [(address, _ack(address, func))]
        else:
            reply = [(address, _ack(address, func, 0xFF))]
        return reply

    def due_reports(self, now: float, limit: int = 64) -> list[tuple[float, bytes]]:
        """Auto-report frames scheduled up to ``now`` as (scheduled time, data)."""
        if not self.auto_report or self._next_report is None:
            return []
        frames: list[tuple[float, bytes]] = []
        period = self.period_us / 1_000_000.0
        while self._next_report <= now and len(frames) < limit:
            stamp = self._next_report
            frames.append((stamp, _value(self.address, d.BRT_FUNC_POSITION, self.raw(stamp))))
            self._next_report += period
        if self._next_report < now - 0.5:           # fell far behind: resume instead of replaying
            self._next_report = now
        return frames


class SimulatedEncoderBus:
    """Minimum python-can Bus surface: ``send``, ``recv`` and ``shutdown``."""

    def __init__(self, device: SimulatedEncoder | None = None, *, clock: Callable[[], float] = time.perf_counter,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.device = device or SimulatedEncoder()
        self._clock = clock
        self._wall = wall_clock
        self._origin = clock()
        self._wall_origin = wall_clock()
        self._lock = threading.Lock()
        self._queue: deque[Any] = deque()

    def _now(self) -> float:
        return self._clock() - self._origin

    def _message(self, can_id: int, data: bytes, at: float) -> Any:
        return can.Message(arbitration_id=can_id, data=data, is_extended_id=False,
                           timestamp=self._wall_origin + at, is_rx=True)

    def send(self, message: Any, timeout: float | None = None) -> None:
        now = self._now()
        with self._lock:
            for can_id, data in self.device.handle(int(message.arbitration_id), bytes(message.data), now):
                self._queue.append(self._message(can_id, data, now))

    def recv(self, timeout: float | None = None) -> Any:
        deadline = self._clock() + (timeout if timeout is not None else 0.1)
        while True:
            with self._lock:
                for stamp, data in self.device.due_reports(self._now()):
                    self._queue.append(self._message(self.device.address, data, stamp))
                if self._queue:
                    return self._queue.popleft()
            if self._clock() >= deadline:
                return None
            time.sleep(0.0005)

    def shutdown(self) -> None:
        return None


__all__ = ["SimulatedEncoder", "SimulatedEncoderBus", "default_motion"]
