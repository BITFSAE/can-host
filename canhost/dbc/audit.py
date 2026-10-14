"""Reconcile a DBC with what is actually on the bus (or in a trace).

The firmware is the authority when it and the DBC disagree.  This turns "the
DBC looks wrong" into evidence: IDs on the bus that the DBC does not know,
DBC messages that never appear, a DLC or cycle that differs, values outside
the declared range and codes missing from the value table.
"""

from __future__ import annotations

from collections import Counter
import threading
from typing import Any, Callable

MAX_KEYS = 4096
MIN_WINDOW_S = 3.0
CYCLE_TOLERANCE = 0.2
CYCLE_MIN_DELTA_MS = 5.0
MIN_INTERVALS = 5


class _SignalStats:
    __slots__ = ("low", "high", "samples", "outside", "unlabeled", "has_label", "unit")

    def __init__(self, has_label: bool, unit: str) -> None:
        self.low = float("inf")
        self.high = float("-inf")
        self.samples = 0
        self.outside = 0
        self.unlabeled = 0
        self.has_label = has_label
        self.unit = unit


class _MessageStats:
    __slots__ = ("count", "first", "last", "dlcs", "gap_sum", "gap_count", "gap_max", "signals", "name")

    def __init__(self, name: str | None) -> None:
        self.count = 0
        self.first = 0.0
        self.last = 0.0
        self.dlcs: Counter[int] = Counter()
        self.gap_sum = 0.0
        self.gap_count = 0
        self.gap_max = 0.0
        self.signals: dict[str, _SignalStats] = {}
        self.name = name


class AuditAccumulator:
    """``resolver(id, extended)`` -> ``(decoder, message)`` or ``None``."""

    def __init__(self, resolver: Callable[[int, bool], tuple[Any, Any] | None]) -> None:
        self.resolver = resolver
        # The receive thread ingests while the page asks for reports.
        self.lock = threading.Lock()
        self.stats: dict[tuple[int, bool], _MessageStats] = {}
        self.first = 0.0
        self.last = 0.0
        self.frames = 0
        self.overflow = False

    def reset(self) -> None:
        with self.lock:
            self.stats.clear()
            self.first = self.last = 0.0
            self.frames = 0
            self.overflow = False

    def ingest(self, timestamp: float, arbitration_id: int, extended: bool, data: bytes,
               direction: str = "rx") -> None:
        with self.lock:
            self._ingest(timestamp, arbitration_id, extended, data, direction)

    def _ingest(self, timestamp: float, arbitration_id: int, extended: bool, data: bytes,
                direction: str) -> None:
        key = (arbitration_id, extended)
        stats = self.stats.get(key)
        if stats is None:
            if len(self.stats) >= MAX_KEYS:
                self.overflow = True
                return
            resolved = self.resolver(arbitration_id, extended)
            stats = self.stats[key] = _MessageStats(resolved[1].name if resolved else None)
        if not self.frames:
            self.first = timestamp
        self.last = timestamp
        self.frames += 1
        stats.dlcs[len(data)] += 1
        if direction == "tx":
            return
        if stats.count:
            gap = timestamp - stats.last
            if gap >= 0:
                stats.gap_sum += gap
                stats.gap_count += 1
                stats.gap_max = max(stats.gap_max, gap)
        else:
            stats.first = timestamp
        stats.last = timestamp
        stats.count += 1
        if stats.name is None:
            return
        resolved = self.resolver(arbitration_id, extended)
        if resolved is None:
            return
        decoder, message = resolved
        decoded = decoder.decode(message, data)
        for name, entry in decoded["values"].items():
            signal = stats.signals.get(name)
            if signal is None:
                definition = next((s for s in message.signals if s.name == name), None)
                signal = stats.signals[name] = _SignalStats(entry["has_label"], getattr(definition, "unit", "") or "")
            value = entry["value"]
            if isinstance(value, str):
                continue
            signal.samples += 1
            if value < signal.low:
                signal.low = value
            if value > signal.high:
                signal.high = value
            if not entry["in_range"]:
                signal.outside += 1
            if entry["has_label"] and entry["label"] is None:
                signal.unlabeled += 1

    def report(self, expected: list[dict[str, Any]]) -> dict[str, Any]:
        """``expected`` lists the DBC messages for this bus: ``id``, ``extended``,
        ``name``, ``dlc``, ``cycle_ms``, ``id_text`` and per-signal limits."""
        with self.lock:
            return self._report(expected)

    def _report(self, expected: list[dict[str, Any]]) -> dict[str, Any]:
        window = max(0.0, self.last - self.first)
        known = {(e["id"], e["extended"]): e for e in expected}
        unknown, dlc_issues, cycle_issues, range_issues, choice_issues, missing = [], [], [], [], [], []
        clean = 0
        for key, stats in sorted(self.stats.items()):
            entry = known.get(key)
            id_text = f"0x{key[0]:08X}" if key[1] else f"0x{key[0]:03X}"
            rx_gap = stats.gap_sum / stats.gap_count * 1000.0 if stats.gap_count else None
            if entry is None:
                unknown.append({"id_text": id_text, "id": key[0], "extended": key[1], "count": stats.count,
                                "cycle_ms": round(rx_gap, 2) if rx_gap else None,
                                "dlc": sorted(stats.dlcs)})
                continue
            problems = 0
            observed = sorted(stats.dlcs)
            if observed != [entry["dlc"]]:
                dlc_issues.append({"name": entry["name"], "id_text": id_text, "expected": entry["dlc"],
                                   "seen": observed})
                problems += 1
            expected_ms = entry.get("cycle_ms")
            if expected_ms and rx_gap is not None and stats.gap_count >= MIN_INTERVALS:
                delta = abs(rx_gap - expected_ms)
                if delta > CYCLE_MIN_DELTA_MS and delta > expected_ms * CYCLE_TOLERANCE:
                    cycle_issues.append({"name": entry["name"], "id_text": id_text, "expected_ms": expected_ms,
                                         "measured_ms": round(rx_gap, 2), "worst_ms": round(stats.gap_max * 1000, 1)})
                    problems += 1
            for name, signal in stats.signals.items():
                if signal.outside:
                    limits = entry["limits"].get(name, [None, None])
                    range_issues.append({"message": entry["name"], "signal": name, "unit": signal.unit,
                                         "low": signal.low, "high": signal.high,
                                         "limit_low": limits[0], "limit_high": limits[1],
                                         "count": signal.outside, "samples": signal.samples})
                    problems += 1
                if signal.unlabeled:
                    choice_issues.append({"message": entry["name"], "signal": name, "count": signal.unlabeled,
                                          "samples": signal.samples})
                    problems += 1
            clean += problems == 0
        if window >= MIN_WINDOW_S:
            for key, entry in known.items():
                if key not in self.stats:
                    missing.append({"name": entry["name"], "id_text": entry["id_text"]})
        return {
            "frames": self.frames, "window_s": round(window, 2), "ids": len(self.stats),
            "known_messages": len(known), "clean_messages": clean, "overflow": self.overflow,
            "unknown": unknown, "missing": sorted(missing, key=lambda m: m["id_text"]),
            "dlc": dlc_issues, "cycle": cycle_issues, "range": range_issues, "choice": choice_issues,
            "window_ok": window >= MIN_WINDOW_S,
        }
