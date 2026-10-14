"""Per-frame signal recording for the DBC plot.

The page polls a few times per second, but a plot must show every sample.  A
subscribed signal is therefore recorded from the receive path (cheap lookup on
the frame ID, decode only when someone subscribed) into a bounded ring and
fetched incrementally by sequence number.
"""

from __future__ import annotations

from array import array
import math
import threading
from typing import Any, Callable

DEFAULT_CAPACITY = 120_000
MAX_SERIES = 24


class Ring:
    """Fixed-size ring of ``(time, value)`` samples addressed by sequence number."""

    __slots__ = ("capacity", "times", "values", "total")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = capacity
        self.times = array("d", bytes(8 * capacity))
        self.values = array("d", bytes(8 * capacity))
        self.total = 0

    def append(self, timestamp: float, value: float) -> None:
        slot = self.total % self.capacity
        self.times[slot] = timestamp
        self.values[slot] = value
        self.total += 1

    def since(self, cursor: int, limit: int = 20_000) -> tuple[int, list[float], list[float], bool]:
        """Samples with sequence >= ``cursor``; ``lost`` when the ring wrapped past it."""
        oldest = max(0, self.total - self.capacity)
        lost = cursor < oldest
        start = max(cursor, oldest)
        end = min(self.total, start + limit)
        times: list[float] = []
        values: list[float] = []
        for sequence in range(start, end):
            slot = sequence % self.capacity
            times.append(self.times[slot])
            values.append(self.values[slot])
        return end, times, values, lost

    def clear(self) -> None:
        self.total = 0


class Route:
    """One subscribed signal of one message."""

    __slots__ = ("sid", "signal", "ring")

    def __init__(self, sid: str, signal: str, ring: Ring) -> None:
        self.sid = sid
        self.signal = signal
        self.ring = ring


class SeriesRecorder:
    """Records subscribed signals for one data source (``main`` or ``vehicle``).

    ``resolver(frame_id, extended)`` returns a ``(decoder, message)`` pair or
    ``None``; it is called when routes are rebuilt, not per frame.
    """

    def __init__(self, resolver: Callable[[int, bool], tuple[Any, Any] | None]) -> None:
        self.resolver = resolver
        self.lock = threading.Lock()
        self.rings: dict[str, Ring] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        self._routes: dict[tuple[int, bool], list[Route]] = {}
        self._decoders: dict[tuple[int, bool], tuple[Any, Any]] = {}
        self.dirty = True

    @property
    def active(self) -> bool:
        return bool(self.specs)

    def set_series(self, specs: list[dict[str, Any]]) -> list[str]:
        """Replace the subscription list.  A spec is ``{sid, id, extended, signal}``;
        a series that stays subscribed keeps its samples."""
        if len(specs) > MAX_SERIES:
            raise ValueError(f"最多同时记录 {MAX_SERIES} 条信号曲线")
        with self.lock:
            wanted = {str(spec["sid"]): spec for spec in specs}
            for sid in list(self.rings):
                if sid not in wanted:
                    del self.rings[sid]
            for sid in wanted:
                self.rings.setdefault(sid, Ring())
            self.specs = wanted
            self.dirty = True
            return list(wanted)

    def invalidate(self) -> None:
        """Definitions changed (edit, reload, rebinding): rebuild routes lazily."""
        self.dirty = True

    def clear(self) -> None:
        with self.lock:
            for ring in self.rings.values():
                ring.clear()

    def _rebuild(self) -> None:
        routes: dict[tuple[int, bool], list[Route]] = {}
        decoders: dict[tuple[int, bool], tuple[Any, Any]] = {}
        for sid, spec in self.specs.items():
            key = (int(spec["id"]), bool(spec["extended"]))
            resolved = self.resolver(*key)
            if resolved is None:
                continue
            decoder, message = resolved
            if not any(signal.name == spec["signal"] for signal in message.signals):
                continue
            decoders[key] = (decoder, message)
            routes.setdefault(key, []).append(Route(sid, spec["signal"], self.rings[sid]))
        self._routes, self._decoders = routes, decoders
        self.dirty = False

    def on_frame(self, arbitration_id: int, extended: bool, data: bytes, timestamp: float) -> None:
        """Called for every accepted frame under the service lock; must stay cheap."""
        if not self.specs:
            return
        if self.dirty:
            with self.lock:
                if self.dirty:
                    self._rebuild()
        key = (arbitration_id, extended)
        routes = self._routes.get(key)
        if not routes:
            return
        decoder, message = self._decoders[key]
        result = decoder.decode(message, data)
        values = result["values"]
        for route in routes:
            entry = values.get(route.signal)
            if entry is None:
                continue
            value = entry["value"]
            if isinstance(value, (int, float)) and math.isfinite(value):
                route.ring.append(timestamp, float(value))

    def fetch(self, cursors: dict[str, int], limit: int = 20_000) -> dict[str, Any]:
        with self.lock:
            out: dict[str, Any] = {}
            for sid, ring in self.rings.items():
                cursor = int(cursors.get(sid, 0))
                reset = cursor > ring.total
                if reset:
                    cursor = 0
                end, times, values, lost = ring.since(cursor, limit)
                out[sid] = {"cursor": end, "t": times, "v": values, "lost": lost, "reset": reset}
            return out
