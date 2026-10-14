"""Pure helpers of the encoder page: unwrapping, speed, sample buffer and statistics.

Nothing here touches a bus or a clock, so every rule can be tested with plain numbers.
"""

from __future__ import annotations

from collections import deque
import math
from typing import Any

from .. import decoders

RESOLUTION = decoders.BRT_RESOLUTION
DEG_PER_COUNT = decoders.BRT_DEG_PER_COUNT
SPEED_WINDOW_S = 0.05
# Below this derived speed the shaft counts as standing still: one count per 50 ms window is 1.76 deg/s.
STILL_DPS = 2.0
SERIES_CAPACITY = 120_000
SERIES_TRIM_SLACK = 20_000
# An oil frame older than this (against the encoder sample) is not attached to the sample.  It is the
# driver display's own 0x305 timeout, so a value shown here would still be shown on the display.
OIL_FRESH_S = 1.0
CSV_HEADER = "time_s,raw_count,angle_deg,accumulated_deg,speed_rpm,oil_kpa,brake_pct,oil_age_ms"


def oil_kpa(oil_raw: int | None) -> float | None:
    return None if oil_raw is None else round(oil_raw * decoders.OIL_KPA_PER_COUNT, 3)


def brake_pct(oil_raw: int | None) -> int | None:
    return None if oil_raw is None else decoders.brake_pct_from_oil(oil_raw)


def circular_distance(a: int, b: int) -> int:
    """Shortest distance between two counts on the 12-bit circle."""
    delta = abs(int(a) - int(b)) % RESOLUTION
    return min(delta, RESOLUTION - delta)


def max_trackable_rpm(period_s: float) -> float:
    """Highest speed whose per-sample step stays under half a turn (beyond it unwrapping is ambiguous)."""
    return 0.5 * 60.0 / period_s if period_s > 0 else math.inf


class MotionTracker:
    """Unwraps the single-turn count and derives speed over a short differencing window."""

    def __init__(self, window_s: float = SPEED_WINDOW_S) -> None:
        self.window_s = window_s
        self.reset()

    def reset(self) -> None:
        self._last_raw: int | None = None
        self._unwrapped = 0
        self._history: deque[tuple[float, int]] = deque()

    @property
    def unwrapped(self) -> int:
        return self._unwrapped

    def push(self, t: float, raw: int) -> tuple[int, float | None]:
        """Return the accumulated count (starts at the first raw) and the speed in deg/s."""
        if self._last_raw is None:
            self._unwrapped = raw
        else:
            delta = raw - self._last_raw
            if delta > RESOLUTION // 2:
                delta -= RESOLUTION
            elif delta < -(RESOLUTION // 2):
                delta += RESOLUTION
            self._unwrapped += delta
        self._last_raw = raw
        history = self._history
        history.append((t, self._unwrapped))
        while len(history) > 2 and history[1][0] <= t - self.window_s:
            history.popleft()
        if len(history) < 2:
            return self._unwrapped, None
        old_t, old_u = history[0]
        span = t - old_t
        if span <= 0:
            return self._unwrapped, None
        return self._unwrapped, (self._unwrapped - old_u) * DEG_PER_COUNT / span


def motion_label(speed_dps: float | None) -> str | None:
    if speed_dps is None:
        return None
    if speed_dps >= STILL_DPS:
        return "increasing"
    if speed_dps <= -STILL_DPS:
        return "decreasing"
    return "still"


class Timebase:
    """Maps python-can hardware timestamps onto the service clock.

    The offset is fixed by the first frame and re-fixed when the two clocks drift apart by more than
    ``resync_s``; the result never goes backwards.  Without a usable stamp the arrival time is used.
    """

    def __init__(self, resync_s: float = 1.0) -> None:
        self.resync_s = resync_s
        self.reset()

    def reset(self) -> None:
        self._offset: float | None = None
        self._last: float | None = None

    def map(self, stamp: Any, arrival: float) -> float:
        try:
            value = float(stamp)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value) or value <= 0:
            mapped = arrival
        else:
            offset = arrival - value
            if self._offset is None or abs(offset - self._offset) > self.resync_s:
                self._offset = offset
            mapped = value + self._offset
        if self._last is not None and mapped < self._last:
            mapped = self._last
        self._last = mapped
        return mapped


class SampleSeries:
    """Column store for the curves with absolute cursors, trimmed in blocks.

    The page asks for everything after its cursor, so the refresh interval never changes the sample
    density.  ``epoch`` changes on clear(); a reader holding another epoch, or a cursor that was
    already trimmed, gets ``reset`` and starts again from what is kept.

    Each sample carries the newest CANB oil-pressure count (0x305) seen no more than ``OIL_FRESH_S``
    before it, and how old that count was; both are ``None`` when there was no such frame, so a
    missing pressure is never filled with zero.
    """

    def __init__(self, capacity: int = SERIES_CAPACITY, slack: int = SERIES_TRIM_SLACK) -> None:
        self.capacity = capacity
        self.slack = slack
        self.epoch = 0
        self._clear()

    def _clear(self) -> None:
        self._base = 0
        self.t: list[float] = []
        self.raw: list[int] = []
        self.position: list[int] = []
        self.speed: list[float | None] = []
        self.oil: list[int | None] = []
        self.oil_age: list[float | None] = []

    def clear(self) -> None:
        self.epoch += 1
        self._clear()

    def __len__(self) -> int:
        return len(self.t)

    @property
    def total(self) -> int:
        return self._base + len(self.t)

    def append(self, t: float, raw: int, position: int, speed: float | None,
               oil: int | None = None, oil_age: float | None = None) -> None:
        self.t.append(t)
        self.raw.append(raw)
        self.position.append(position)
        self.speed.append(speed)
        self.oil.append(oil)
        self.oil_age.append(oil_age)
        if len(self.t) > self.capacity + self.slack:
            drop = len(self.t) - self.capacity
            for column in (self.t, self.raw, self.position, self.speed, self.oil, self.oil_age):
                del column[:drop]
            self._base += drop

    def fetch(self, cursor: int, epoch: int, limit: int = 20_000) -> dict[str, Any]:
        reset = epoch != self.epoch or cursor < self._base or cursor > self.total
        start = 0 if reset else cursor - self._base
        end = min(len(self.t), start + limit)
        oil = self.oil[start:end]
        return {
            "epoch": self.epoch, "reset": reset, "cursor": self._base + end,
            "t": self.t[start:end], "raw": self.raw[start:end],
            "position": self.position[start:end], "speed": self.speed[start:end],
            "oil_kpa": [oil_kpa(value) for value in oil], "brake_pct": [brake_pct(value) for value in oil],
        }

    def stability(self, now_t: float, window_s: float) -> dict[str, Any]:
        """Spread of the accumulated count over the last ``window_s`` seconds."""
        if not self.t:
            return {"window_s": window_s, "samples": 0}
        lo = len(self.t)
        while lo > 0 and self.t[lo - 1] >= now_t - window_s:
            lo -= 1
        values = self.position[lo:]
        count = len(values)
        if count < 2:
            return {"window_s": window_s, "samples": count}
        mean = sum(values) / count
        variance = sum((value - mean) ** 2 for value in values) / count
        spread = max(values) - min(values)
        return {
            "window_s": window_s, "samples": count, "ptp_counts": spread,
            "ptp_deg": round(spread * DEG_PER_COUNT, 4), "std_counts": round(math.sqrt(variance), 3),
            "moving": spread > 8,
        }

    def csv(self) -> str:
        """One row per encoder sample; the three oil columns stay empty where no fresh oil frame was held."""
        if not self.t:
            return CSV_HEADER + "\n"
        origin = self.t[0]
        rows = [CSV_HEADER]
        for t, raw, position, speed, oil, age in zip(self.t, self.raw, self.position, self.speed,
                                                    self.oil, self.oil_age):
            rows.append(f"{t - origin:.6f},{raw},{raw * DEG_PER_COUNT:.4f},"
                        f"{position * DEG_PER_COUNT:.4f},{'' if speed is None else f'{speed / 6.0:.3f}'},"
                        + (",," if oil is None else
                           f"{oil * decoders.OIL_KPA_PER_COUNT:.3f},{decoders.brake_pct_from_oil(oil)},"
                           f"{(age or 0.0) * 1000.0:.1f}"))
        return "\n".join(rows) + "\n"


__all__ = ["CSV_HEADER", "DEG_PER_COUNT", "MotionTracker", "OIL_FRESH_S", "RESOLUTION", "SPEED_WINDOW_S",
           "SampleSeries", "STILL_DPS", "Timebase", "brake_pct", "circular_distance", "max_trackable_rpm",
           "motion_label", "oil_kpa"]
