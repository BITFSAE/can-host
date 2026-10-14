"""Offline decoding of recorded traces (``.bmslog`` and CSV).

A recording can be decoded without playing it back: the native log has an
``(arbitration_id, timestamp)`` index, so one message's samples are a single
indexed query.  Plots, the bus audit and the CSV export all read frames through
:class:`TraceFile`.
"""

from __future__ import annotations

import csv
from datetime import datetime
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator

CSV_FRAME_LIMIT = 2_000_000
Frame = tuple[float, int, bool, str, bytes]    # timestamp, id, extended, direction, data


class TraceError(ValueError):
    pass


class TraceFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.kind = "bmslog" if self.path.suffix.lower() == ".bmslog" else "csv"
        self.metadata: dict[str, str] = {}
        self._database: sqlite3.Connection | None = None
        self._frames: list[Frame] = []
        self.count = 0
        self.first = 0.0
        self.last = 0.0
        self.ids: dict[tuple[int, bool], int] = {}
        if self.kind == "bmslog":
            self._open_database()
        else:
            self._load_csv()

    # -- opening ----------------------------------------------------------

    def _open_database(self) -> None:
        database: sqlite3.Connection | None = None
        try:
            database = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                       check_same_thread=False)
            self.metadata = dict(database.execute("SELECT key, value FROM meta"))
            if self.metadata.get("format") != "BITFSAE_BMS_LOG":
                raise TraceError("不是受支持的 CAN 数据记录文件")
            row = database.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM frames").fetchone()
            self.count, first, last = int(row[0]), row[1], row[2]
            if not self.count:
                raise TraceError("记录中没有 CAN 帧")
            self.first, self.last = float(first), float(last)
            for identifier, extended, count in database.execute(
                    "SELECT arbitration_id, extended, COUNT(*) FROM frames GROUP BY arbitration_id, extended"):
                self.ids[(int(identifier), bool(extended))] = int(count)
        except (sqlite3.Error, TraceError) as exc:
            if database is not None:
                database.close()
            if isinstance(exc, TraceError):
                raise
            raise TraceError(f"记录文件无法读取：{exc}") from exc
        self._database = database

    def _load_csv(self) -> None:
        frames: list[Frame] = []
        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    raw_id = (row.get("ID") or "").strip()
                    raw_time = (row.get("本地时间") or "").strip()
                    if not raw_id or not raw_time:
                        continue
                    frames.append((
                        datetime.fromisoformat(raw_time).timestamp(), int(raw_id, 16),
                        (row.get("帧类型") or "") == "扩展", (row.get("方向") or "rx").lower(),
                        bytes.fromhex((row.get("数据") or "").strip().replace(" ", "")),
                    ))
                    if len(frames) > CSV_FRAME_LIMIT:
                        raise TraceError("CSV 超过 200 万帧，请改用 .bmslog 记录长时间数据")
        except (OSError, ValueError) as exc:
            if isinstance(exc, TraceError):
                raise
            raise TraceError(f"CSV 记录无法读取：{exc}") from exc
        if not frames:
            raise TraceError("文件中没有 CAN 帧")
        frames.sort(key=lambda frame: frame[0])
        self._frames = frames
        self.count = len(frames)
        self.first, self.last = frames[0][0], frames[-1][0]
        for frame in frames:
            self.ids[(frame[1], frame[2])] = self.ids.get((frame[1], frame[2]), 0) + 1

    def close(self) -> None:
        if self._database is not None:
            try:
                self._database.close()
            except sqlite3.Error:
                pass
            self._database = None
        self._frames = []

    # -- reading ----------------------------------------------------------

    @property
    def duration(self) -> float:
        return max(0.0, self.last - self.first)

    @property
    def bus_profile(self) -> str | None:
        profile = self.metadata.get("bus_profile")
        return profile if profile in ("can1", "canb") else None

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "name": self.path.name, "kind": self.kind, "frames": self.count,
            "duration": round(self.duration, 3), "first": self.first, "bus_profile": self.bus_profile,
            "bitrate": self.metadata.get("bitrate"), "created_at": self.metadata.get("created_at"),
            "ids": [{"id": identifier, "extended": extended, "count": count}
                    for (identifier, extended), count in sorted(self.ids.items())],
        }

    def frames(self, keys: set[tuple[int, bool]] | None = None) -> Iterator[Frame]:
        """Frames in recorded order, optionally only the given ``(id, extended)`` keys."""
        if self._database is not None:
            if keys is not None and len(keys) <= 16:
                for identifier, extended in sorted(keys):
                    cursor = self._database.execute(
                        "SELECT timestamp, arbitration_id, extended, direction, data FROM frames "
                        "WHERE arbitration_id = ? AND extended = ? ORDER BY timestamp", (identifier, int(extended)))
                    for row in cursor:
                        yield float(row[0]), int(row[1]), bool(row[2]), str(row[3]), bytes(row[4])
                return
            cursor = self._database.execute(
                "SELECT timestamp, arbitration_id, extended, direction, data FROM frames ORDER BY seq")
            for row in cursor:
                key = (int(row[1]), bool(row[2]))
                if keys is None or key in keys:
                    yield float(row[0]), key[0], key[1], str(row[3]), bytes(row[4])
            return
        for frame in self._frames:
            if keys is None or (frame[1], frame[2]) in keys:
                yield frame


def decimate(times: list[float], values: list[float], limit: int) -> tuple[list[float], list[float]]:
    """Keep the extremes of each bucket so a spike survives down-sampling."""
    count = len(times)
    if count <= limit or limit < 4:
        return times, values
    buckets = limit // 2
    size = count / buckets
    out_t: list[float] = []
    out_v: list[float] = []
    for bucket in range(buckets):
        lo, hi = int(bucket * size), int((bucket + 1) * size)
        if hi <= lo:
            continue
        segment = values[lo:hi]
        low_at = lo + segment.index(min(segment))
        high_at = lo + segment.index(max(segment))
        for index in sorted({low_at, high_at}):
            out_t.append(times[index])
            out_v.append(values[index])
    return out_t, out_v


def extract_series(trace: TraceFile, resolver: Callable[[int, bool], tuple[Any, Any] | None],
                   specs: list[dict[str, Any]], max_points: int = 6000) -> dict[str, Any]:
    """Decode the requested signals over the whole trace."""
    wanted: dict[tuple[int, bool], list[dict[str, Any]]] = {}
    for spec in specs:
        wanted.setdefault((int(spec["id"]), bool(spec["extended"])), []).append(spec)
    data: dict[str, tuple[list[float], list[float]]] = {str(spec["sid"]): ([], []) for spec in specs}
    for key, group in wanted.items():
        resolved = resolver(*key)
        if resolved is None:
            continue
        decoder, message = resolved
        for timestamp, identifier, extended, direction, payload in trace.frames({key}):
            values = decoder.decode(message, payload)["values"]
            for spec in group:
                entry = values.get(spec["signal"])
                if entry is None:
                    continue
                value = entry["value"]
                if isinstance(value, (int, float)) and math.isfinite(value):
                    times, series = data[str(spec["sid"])]
                    times.append(timestamp - trace.first)
                    series.append(float(value))
    result: dict[str, Any] = {}
    for sid, (times, series) in data.items():
        total = len(times)
        times, series = decimate(times, series, max_points)
        result[sid] = {"t": times, "v": series, "samples": total}
    return result
