"""The DBC workspace behind the DBC page.

It owns the open DBC files (document, undo history, bus binding), turns the
latest monitor frames into decoded rows, records signals for the plot, audits a
bus against its DBC and decodes saved traces.  It never touches a CAN bus
itself: frames come in through ``live`` / the source taps, and a payload built
by ``encode`` is sent through the existing guarded raw-frame sender.
"""

from __future__ import annotations

import itertools
import re
import threading
from pathlib import Path
from typing import Any, Callable

from . import codec
from .audit import AuditAccumulator
from .catalog import build_catalog
from .decode import DbcEncodeError, DbcLoadError, DecodeDb
from .diff import count_ops, semantic_diff, text_diff
from .document import (
    DbcDocument, DbcEditError, DbcError, EXTENDED_FLAG, ID_MASK, NO_NODE, message_key, parse_message_key,
)
from .lint import ERROR, lint_catalog, summarize
from .series import SeriesRecorder
from .trace import TraceError, TraceFile, extract_series

BUSES = ("can1", "canb", "all", "none")
BUS_LABELS = {"can1": "CAN1", "canb": "CANB", "all": "全部总线", "none": "不解码"}
CENTRAL_DIRECTORY = "vehicle-interfaces"
UNDO_LIMIT = 100
MAX_FILES = 12
MAX_TRACES = 3
STALE_FLOOR_MS = 2000.0
STALE_CYCLES = 3.0
MAX_UNKNOWN = 300
RECENT_LIMIT = 15


def guess_bus(name: str) -> str:
    lowered = name.lower()
    if "canb" in lowered:
        return "canb"
    if "can1" in lowered:
        return "can1"
    return "none"


def is_central(path: Path | None) -> bool:
    return path is not None and CENTRAL_DIRECTORY in (part.lower() for part in path.resolve().parts)


def _squash_text(value: Any, key: str | None = None) -> Any:
    """Collapse whitespace in comments: CANdb++ saving joins multi-line comments."""
    if isinstance(value, dict):
        return {name: _squash_text(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_squash_text(item, key) for item in value]
    if isinstance(value, str) and key in ("comment", "label"):
        return re.sub(r"\s+", " ", value).strip()
    return value


class DbcFile:
    def __init__(self, file_id: str, path: Path | None, text: str, encoding: str, bus: str,
                 name: str | None = None) -> None:
        self.id = file_id
        self.path = path
        self.name = name or (path.name if path else "未命名.dbc")
        self.encoding = encoding
        self.bus = bus
        self.document = DbcDocument.parse(text)
        self.saved_text = self.document.to_text()
        self.undo: list[str] = []
        self.redo: list[str] = []
        self.revision = 1
        self.readonly = is_central(path)
        self.decode: DecodeDb | None = None
        self.decode_error: str | None = None
        self.decode_stale = False
        self.disk_stamp = self._stamp()
        self._catalog: tuple[int, dict[str, Any], list[dict[str, Any]]] | None = None
        self._dirty: tuple[int, bool] | None = None
        self.rebuild_decode()

    def _stamp(self) -> tuple[float, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def rebuild_decode(self) -> None:
        try:
            decode = DecodeDb(self.document.to_text())
        except DbcLoadError as exc:
            self.decode_error = str(exc)
            self.decode_stale = self.decode is not None
            return
        self.decode, self.decode_error, self.decode_stale = decode, decode.strict_error, False

    def analysis(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self._catalog is None or self._catalog[0] != self.revision:
            catalog = build_catalog(self.document)
            self._catalog = (self.revision, catalog, lint_catalog(catalog))
        return self._catalog[1], self._catalog[2]

    @property
    def dirty(self) -> bool:
        if self._dirty is None or self._dirty[0] != self.revision:
            self._dirty = (self.revision, self.document.to_text() != self.saved_text)
        return self._dirty[1]

    def disk_changed(self) -> bool:
        return self.path is not None and self.disk_stamp is not None and self._stamp() != self.disk_stamp


class DbcWorkspace:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.files: list[DbcFile] = []
        self.revision = 0
        self.recent: list[str] = []
        self._ids = itertools.count(1)
        self._index_revision = -1
        self._indexes: dict[str, dict[tuple[int, bool], tuple[DbcFile, DecodeDb, Any]]] = {}
        self._conflicts: dict[str, list[dict[str, Any]]] = {}
        self.listeners: list[Callable[[], None]] = []
        self.traces: dict[str, TraceFile] = {}

    # -- file management ----------------------------------------------------

    def _changed(self) -> None:
        self.revision += 1
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                pass

    def _get(self, file_id: str) -> DbcFile:
        for file in self.files:
            if file.id == file_id:
                return file
        raise DbcEditError("该 DBC 已关闭")

    def add_text(self, text: str, path: Path | None = None, name: str | None = None, bus: str | None = None,
                 encoding: str = "utf-8") -> DbcFile:
        with self.lock:
            if len(self.files) >= MAX_FILES:
                raise DbcEditError(f"最多同时打开 {MAX_FILES} 个 DBC")
            file = DbcFile(f"f{next(self._ids)}", path, text, encoding,
                           bus if bus in BUSES else guess_bus(name or (path.name if path else "")), name)
            self.files.append(file)
            self._changed()
            return file

    def open_path(self, path: str | Path, bus: str | None = None) -> dict[str, Any]:
        target = Path(path).expanduser()
        with self.lock:
            for file in self.files:
                if file.path is not None and file.path.resolve() == target.resolve():
                    return {"ok": True, "file": self.describe(file), "already_open": True}
            try:
                text, encoding = codec.read_file(target)
            except (OSError, UnicodeDecodeError) as exc:
                return {"ok": False, "error": f"无法读取 {target.name}：{exc}"}
            try:
                file = self.add_text(text, target, bus=bus, encoding=encoding)
            except DbcError as exc:
                return {"ok": False, "error": str(exc)}
            self._remember(str(target))
            return {"ok": True, "file": self.describe(file)}

    def new_file(self, name: str = "未命名.dbc", bus: str = "none") -> dict[str, Any]:
        with self.lock:
            try:
                file = self.add_text(DbcDocument.empty().to_text(), None, name=name, bus=bus)
            except DbcError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "file": self.describe(file)}

    def close(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            self.files.remove(file)
            self._changed()
            return {"ok": True}

    def reload(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            if file.path is None:
                return {"ok": False, "error": "未保存的新文件没有磁盘版本可重新载入"}
            try:
                text, encoding = codec.read_file(file.path)
            except OSError as exc:
                return {"ok": False, "error": f"无法读取 {file.path.name}：{exc}"}
            file.document = DbcDocument.parse(text)
            file.saved_text = file.document.to_text()
            file.encoding = encoding
            file.undo.clear()
            file.redo.clear()
            file.revision += 1
            file.disk_stamp = file._stamp()
            file.rebuild_decode()
            self._changed()
            return {"ok": True, "file": self.describe(file)}

    def set_bus(self, file_id: str, bus: str) -> dict[str, Any]:
        if bus not in BUSES:
            return {"ok": False, "error": "总线绑定只能是 CAN1、CANB、全部或不解码"}
        with self.lock:
            file = self._get(file_id)
            file.bus = bus
            self._changed()
            return {"ok": True, "file": self.describe(file)}

    def _remember(self, path: str) -> None:
        self.recent = [path, *[p for p in self.recent if p != path]][:RECENT_LIMIT]

    def describe(self, file: DbcFile) -> dict[str, Any]:
        catalog, issues = file.analysis()
        counts = summarize(issues)
        return {
            "id": file.id, "name": file.name, "path": str(file.path) if file.path else None,
            "bus": file.bus, "dirty": file.dirty, "readonly": file.readonly,
            "readonly_reason": "中央库 DBC 默认只读，另存为后才能编辑" if file.readonly else None,
            "encoding": file.encoding, "encoding_label": codec.LABELS.get(file.encoding, file.encoding),
            "revision": file.revision, "can_undo": bool(file.undo), "can_redo": bool(file.redo),
            "messages": sum(1 for m in catalog["messages"] if not m["virtual"]),
            "signals": sum(len(m["signals"]) for m in catalog["messages"] if not m["virtual"]),
            "errors": counts["errors"], "warnings": counts["warnings"], "notes": counts["notes"],
            "decode_error": file.decode_error, "decode_stale": file.decode_stale,
            "disk_changed": file.disk_changed(),
        }

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {"revision": self.revision, "files": [self.describe(f) for f in self.files],
                    "recent": list(self.recent), "buses": BUS_LABELS}

    def persisted(self) -> dict[str, Any]:
        with self.lock:
            return {"files": [{"path": str(f.path), "bus": f.bus} for f in self.files if f.path is not None],
                    "recent": list(self.recent)}

    def restore(self, saved: Any) -> list[str]:
        """Reopen the files of the last session; returns messages for files that are gone."""
        problems: list[str] = []
        if not isinstance(saved, dict):
            return problems
        recent = saved.get("recent")
        if isinstance(recent, list):
            self.recent = [str(p) for p in recent if isinstance(p, str)][:RECENT_LIMIT]
        for entry in saved.get("files", []) if isinstance(saved.get("files"), list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            result = self.open_path(entry["path"], entry.get("bus") if entry.get("bus") in BUSES else None)
            if not result.get("ok"):
                problems.append(result.get("error", entry["path"]))
        return problems

    # -- catalog / editing ---------------------------------------------------

    def detail(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            catalog, issues = file.analysis()
            return {"ok": True, "file": self.describe(file), "catalog": catalog, "issues": issues}

    def _commit(self, file: DbcFile, before: str) -> None:
        file.undo.append(before)
        del file.undo[:-UNDO_LIMIT]
        file.redo.clear()
        file.revision += 1
        file.rebuild_decode()
        self._changed()

    def edit(self, file_id: str, op: dict[str, Any]) -> dict[str, Any]:
        return self.edit_many(file_id, [op])

    def edit_many(self, file_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            if file.readonly:
                return {"ok": False, "error": "中央库 DBC 是只读的，请先“另存为”再编辑"}
            if not isinstance(ops, list) or not ops:
                return {"ok": False, "error": "没有编辑操作"}
            before = file.document.to_text()
            try:
                for op in ops:
                    file.document.apply(op)
            except DbcError as exc:
                file.document = DbcDocument.parse(before)
                return {"ok": False, "error": str(exc)}
            if file.document.to_text() == before:
                return {"ok": True, "unchanged": True, "file": self.describe(file)}
            self._commit(file, before)
            return {"ok": True, "file": self.describe(file)}

    def undo(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            if not file.undo:
                return {"ok": False, "error": "没有可撤销的操作"}
            file.redo.append(file.document.to_text())
            file.document = DbcDocument.parse(file.undo.pop())
            file.revision += 1
            file.rebuild_decode()
            self._changed()
            return {"ok": True, "file": self.describe(file)}

    def redo(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            if not file.redo:
                return {"ok": False, "error": "没有可重做的操作"}
            file.undo.append(file.document.to_text())
            file.document = DbcDocument.parse(file.redo.pop())
            file.revision += 1
            file.rebuild_decode()
            self._changed()
            return {"ok": True, "file": self.describe(file)}

    # -- saving ---------------------------------------------------------------

    def save_review(self, file_id: str) -> dict[str, Any]:
        """Everything the "review before saving" dialog shows."""
        with self.lock:
            file = self._get(file_id)
            catalog, issues = file.analysis()
            original = build_catalog(DbcDocument.parse(file.saved_text))
            changes = semantic_diff(original, catalog)
            return {
                "ok": True, "file": self.describe(file), "changes": changes, "counts": count_ops(changes),
                "text": text_diff(file.saved_text, file.document.to_text()),
                "blockers": [i for i in issues if i["level"] == ERROR],
                "non_ascii": not file.document.to_text().isascii(),
            }

    def compare(self, first_id: str, second_id: str) -> dict[str, Any]:
        """Semantic and text differences from the first open file to the second
        (for example the central library against a working copy)."""
        with self.lock:
            first, second = self._get(first_id), self._get(second_id)
            changes = semantic_diff(first.analysis()[0], second.analysis()[0])
            return {"ok": True, "first": first.name, "second": second.name, "changes": changes,
                    "counts": count_ops(changes),
                    "text": text_diff(first.document.to_text(), second.document.to_text(), labels=(first.name, second.name))}

    def save(self, file_id: str, path: str | None = None, encoding: str | None = None,
             candbpp: bool = False) -> dict[str, Any]:
        with self.lock:
            file = self._get(file_id)
            catalog, issues = file.analysis()
            errors = [i for i in issues if i["level"] == ERROR]
            if errors:
                return {"ok": False, "error": f"仍有 {len(errors)} 个错误，修正后才能保存", "blockers": errors[:20]}
            target = Path(path).expanduser() if path else file.path
            if target is None:
                return {"ok": False, "error": "新文件需要先选择保存位置"}
            if file.readonly and (path is None or (file.path and target.resolve() == file.path.resolve())):
                return {"ok": False, "error": "中央库 DBC 是只读的，请另存为新文件"}
            try:
                text = file.document.candbpp_text() if candbpp else file.document.to_text()
                chosen = encoding or ("gbk" if candbpp and not text.isascii() else file.encoding)
                probe = DecodeDb(text)
                if probe.strict_error:
                    return {"ok": False, "error": f"保存前严格解析失败：{probe.strict_error}"}
                warnings: list[str] = []
                if candbpp:
                    reparsed = build_catalog(DbcDocument.parse(text))
                    left, right = _squash_text(catalog), _squash_text(reparsed)
                    # CANdb++ wants the placeholder node listed once it is used; not a change of meaning.
                    differences = [r for r in semantic_diff(left, right)
                                   if not (r["scope"] == "node" and r["target"] == NO_NODE)]
                    if differences:
                        warnings.append("CANdb++ 规整后语义与当前内容存在差异，请在评审里核对")
                codec.write_file(target, text, chosen)
            except (codec.DbcCodecError, DbcLoadError, DbcError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            before = file.document.to_text()
            if candbpp:
                file.document = DbcDocument.parse(text)
                if file.document.to_text() != before:
                    file.undo.append(before)
                    del file.undo[:-UNDO_LIMIT]
                    file.redo.clear()
            file.path, file.name, file.encoding = target, target.name, chosen
            file.saved_text = file.document.to_text()
            file.revision += 1
            file.disk_stamp = file._stamp()
            file.rebuild_decode()
            self._remember(str(target))
            self._changed()
            backup = target.with_name(target.name + ".bak")
            return {"ok": True, "path": str(target), "encoding": chosen, "warnings": warnings,
                    "backup": str(backup) if backup.exists() else None, "file": self.describe(file)}

    def export_text(self, file_id: str, candbpp: bool = False) -> str:
        with self.lock:
            file = self._get(file_id)
            return file.document.candbpp_text() if candbpp else file.document.to_text()

    # -- bus index --------------------------------------------------------------

    def _table(self, bus: str) -> dict[tuple[int, bool], tuple[DbcFile, DecodeDb, Any]]:
        if self._index_revision != self.revision:
            with self.lock:
                if self._index_revision != self.revision:
                    self._rebuild_index()
        return self._indexes.get(bus, {})

    def _rebuild_index(self) -> None:
        indexes: dict[str, dict[tuple[int, bool], tuple[DbcFile, DecodeDb, Any]]] = {}
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for bus in ("can1", "canb"):
            table: dict[tuple[int, bool], tuple[DbcFile, DecodeDb, Any]] = {}
            clashes: dict[tuple[int, bool], list[str]] = {}
            for file in self.files:
                if file.bus not in (bus, "all") or file.decode is None:
                    continue
                for key, message in file.decode.messages.items():
                    if key in table:
                        clashes.setdefault(key, [table[key][0].name]).append(file.name)
                    else:
                        table[key] = (file, file.decode, message)
            indexes[bus] = table
            conflicts[bus] = [{"key": message_key(key[0] | (EXTENDED_FLAG if key[1] else 0)), "files": names}
                              for key, names in sorted(clashes.items())]
        self._indexes, self._conflicts, self._index_revision = indexes, conflicts, self.revision

    def resolve(self, bus: str, frame_id: int, extended: bool) -> tuple[DecodeDb, Any] | None:
        entry = self._table(bus).get((frame_id, extended))
        return (entry[1], entry[2]) if entry else None

    def resolver(self, bus_getter: Callable[[], str]) -> Callable[[int, bool], tuple[DecodeDb, Any] | None]:
        return lambda frame_id, extended: self.resolve(bus_getter(), frame_id, extended)

    def message_name(self, bus: str, frame_id: int, extended: bool) -> str | None:
        entry = self._table(bus).get((frame_id, extended))
        return entry[2].name if entry else None

    def expected_messages(self, bus: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.lock:
            for (frame_id, extended), (file, decode, message) in sorted(self._table(bus).items()):
                limits = {s.name: [s.minimum, s.maximum] for s in message.signals}
                out.append({"id": frame_id, "extended": extended, "name": message.name, "dlc": message.length,
                            "cycle_ms": message.cycle_time or None, "limits": limits,
                            "id_text": f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"})
        return out

    # -- live decoding ------------------------------------------------------------

    @staticmethod
    def _catalog_entry(file: DbcFile, frame_id: int, extended: bool) -> dict[str, Any] | None:
        key = message_key(frame_id | (EXTENDED_FLAG if extended else 0))
        catalog, _ = file.analysis()
        return next((m for m in catalog["messages"] if m["key"] == key), None)

    def live(self, bus: str, frames: list[dict[str, Any]], clock: float, keys: list[str] | None = None,
             include_messages: bool = True, include_unknown: bool = True) -> dict[str, Any]:
        """Decode the latest frame of each ID against the DBCs bound to ``bus``.

        ``frames`` are the monitor's per-ID facts (``id``, ``extended``, ``data``,
        ``last_ts``, ``count``, ``cycle_ms``, ``direction``); ``clock`` is the
        time those timestamps are compared with (wall time, or the replay position
        during a replay).  Signals are decoded only for the requested ``keys``."""
        wanted = set(keys or [])
        seen = {(f["id"], f["extended"]): f for f in frames}
        with self.lock:
            table = self._table(bus)
            names: dict[str, str] = {}
            messages: list[dict[str, Any]] = []
            decoded: dict[str, Any] = {}
            by_file: dict[str, dict[str, Any]] = {}
            for (frame_id, extended), (file, decode, message) in sorted(table.items()):
                key = message_key(frame_id | (EXTENDED_FLAG if extended else 0))
                names[key] = message.name
                if not include_messages and key not in wanted:
                    continue
                if file.id not in by_file:
                    by_file[file.id] = {m["key"]: m for m in file.analysis()[0]["messages"]}
                entry = by_file[file.id].get(key)
                frame = seen.get((frame_id, extended))
                summary = self._summarize(key, file, message, entry, frame, clock)
                if include_messages:
                    messages.append(summary)
                if key in wanted:
                    summary = dict(summary)
                    summary["signals"] = self._signal_rows(decode, message, entry, frame, summary)
                    decoded[key] = summary
            unknown: list[dict[str, Any]] = []
            if include_unknown:
                for (frame_id, extended), frame in sorted(seen.items()):
                    if (frame_id, extended) in table:
                        continue
                    unknown.append({
                        "id": frame_id, "extended": extended,
                        "id_text": f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}",
                        "count": frame["count"], "dlc": len(frame["data"]),
                        "data": " ".join(f"{b:02X}" for b in frame["data"]),
                        "cycle_ms": frame.get("cycle_ms"),
                        "age_ms": round(max(0.0, clock - frame["last_ts"]) * 1000.0, 1),
                    })
                    if len(unknown) >= MAX_UNKNOWN:
                        break
            return {
                "revision": self.revision, "bus": bus, "names": names, "messages": messages,
                "decoded": decoded, "unknown": unknown, "conflicts": self._conflicts.get(bus, []),
                "files": [{"id": f.id, "name": f.name, "bus": f.bus} for f in self.files
                          if f.bus in (bus, "all") and f.decode is not None],
            }

    @staticmethod
    def _summarize(key: str, file: DbcFile, message: Any, entry: dict[str, Any] | None,
                   frame: dict[str, Any] | None, clock: float) -> dict[str, Any]:
        expected = message.cycle_time or None
        summary: dict[str, Any] = {
            "key": key, "id": message.frame_id, "extended": message.is_extended_frame,
            "id_text": entry["id_text"] if entry else key, "name": message.name, "file": file.id,
            "dlc": message.length, "signal_count": len(message.signals), "expected_cycle_ms": expected,
            "comment": entry["comment"] if entry else "",
        }
        if frame is None:
            summary.update(state="waiting", age_ms=None, count=0, cycle_ms=None, data="", seen_dlc=None,
                           issue=None, direction=None)
            return summary
        age_ms = round(max(0.0, clock - frame["last_ts"]) * 1000.0, 1)
        cycle = frame.get("cycle_ms") or expected
        limit = max(STALE_FLOOR_MS, STALE_CYCLES * (cycle or 0.0))
        issue = None
        if len(frame["data"]) < message.length:
            issue = "dlc_short"
        elif len(frame["data"]) > message.length:
            issue = "dlc_long"
        summary.update(
            state="stale" if age_ms > limit else "live", age_ms=age_ms, count=frame["count"],
            cycle_ms=frame.get("cycle_ms"), data=" ".join(f"{b:02X}" for b in frame["data"]),
            seen_dlc=len(frame["data"]), issue=issue, direction=frame.get("direction"))
        return summary

    @staticmethod
    def _signal_rows(decode: DecodeDb, message: Any, entry: dict[str, Any] | None,
                     frame: dict[str, Any] | None, summary: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        if frame is not None:
            result = decode.decode(message, frame["data"])
            values = result["values"]
            if result["status"] == "error":
                summary["issue"] = "error"
                summary["error"] = result.get("error")
        catalog_signals = {s["name"]: s for s in entry["signals"]} if entry else {}
        for signal in message.signals:
            info = catalog_signals.get(signal.name, {})
            row = {
                "name": signal.name, "unit": signal.unit or "", "mux": info.get("mux", {}).get("role", "none"),
                "mux_value": info.get("mux", {}).get("value"), "comment": info.get("comment", ""),
                "length": signal.length, "has_label": bool(signal.choices),
            }
            if frame is None:
                row["state"] = "waiting"
            elif signal.name in values:
                value = values[signal.name]
                row.update(state="ok", value=value["value"], raw=value["raw"], raw_text=value["raw_text"],
                           label=value["label"], in_range=value["in_range"])
            else:
                row["state"] = "missing" if summary.get("issue") == "dlc_short" else "inactive"
            rows.append(row)
        return rows

    # -- encode ------------------------------------------------------------------------

    def encode(self, bus: str, key: str, values: dict[str, Any]) -> dict[str, Any]:
        try:
            raw_id = parse_message_key(key)
        except DbcEditError as exc:
            return {"ok": False, "error": str(exc)}
        frame_id, extended = raw_id & ID_MASK, bool(raw_id & EXTENDED_FLAG)
        with self.lock:
            resolved = self.resolve(bus, frame_id, extended)
            if resolved is None:
                return {"ok": False, "error": "当前总线上没有绑定包含该报文的 DBC"}
            decode, message = resolved
            try:
                data = decode.encode(message, values or {})
            except DbcEncodeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "id": frame_id, "extended": extended, "name": message.name,
                    "dlc": len(data), "data": " ".join(f"{b:02X}" for b in data)}

    def message_form(self, bus: str, key: str) -> dict[str, Any]:
        """Initial values for a signal-level sender: each signal's start value, or 0."""
        try:
            raw_id = parse_message_key(key)
        except DbcEditError as exc:
            return {"ok": False, "error": str(exc)}
        with self.lock:
            entry = self._table(bus).get((raw_id & ID_MASK, bool(raw_id & EXTENDED_FLAG)))
            if entry is None:
                return {"ok": False, "error": "当前总线上没有绑定包含该报文的 DBC"}
            file, decode, message = entry
            catalog_entry = self._catalog_entry(file, message.frame_id, message.is_extended_frame)
            values = {}
            for signal in message.signals:
                initial = signal.initial
                values[signal.name] = initial if initial is not None else (0 if signal.offset == 0 else signal.offset)
            return {"ok": True, "message": catalog_entry, "values": values}

    # -- traces ---------------------------------------------------------------------------

    def open_trace(self, path: str) -> dict[str, Any]:
        with self.lock:
            if len(self.traces) >= MAX_TRACES:
                oldest = next(iter(self.traces))
                self.traces.pop(oldest).close()
        try:
            trace = TraceFile(path)
        except (TraceError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        trace_id = f"t{next(self._ids)}"
        with self.lock:
            self.traces[trace_id] = trace
        summary = trace.summary()
        summary["id"] = trace_id
        return {"ok": True, "trace": summary}

    def close_trace(self, trace_id: str) -> dict[str, Any]:
        with self.lock:
            trace = self.traces.pop(trace_id, None)
        if trace is not None:
            trace.close()
        return {"ok": True}

    def _trace(self, trace_id: str) -> TraceFile:
        trace = self.traces.get(trace_id)
        if trace is None:
            raise TraceError("记录文件已关闭，请重新打开")
        return trace

    def trace_series(self, trace_id: str, bus: str, specs: list[dict[str, Any]],
                     max_points: int = 6000) -> dict[str, Any]:
        try:
            trace = self._trace(trace_id)
            data = extract_series(trace, lambda i, e: self.resolve(bus, i, e), specs, max_points)
        except (TraceError, KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "series": data, "duration": round(trace.duration, 3)}

    def trace_audit(self, trace_id: str, bus: str) -> dict[str, Any]:
        try:
            trace = self._trace(trace_id)
        except TraceError as exc:
            return {"ok": False, "error": str(exc)}
        audit = AuditAccumulator(lambda i, e: self.resolve(bus, i, e))
        for timestamp, frame_id, extended, direction, data in trace.frames():
            audit.ingest(timestamp, frame_id, extended, data, direction)
        report = audit.report(self.expected_messages(bus))
        return {"ok": True, "report": report}

    def trace_export_csv(self, trace_id: str, bus: str, path: str) -> dict[str, Any]:
        import csv
        try:
            trace = self._trace(trace_id)
        except TraceError as exc:
            return {"ok": False, "error": str(exc)}
        target = Path(path).expanduser()
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        rows = 0
        try:
            with target.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["时间 s", "报文", "ID", "信号", "原始值", "物理值", "单位", "值表"])
                for timestamp, frame_id, extended, direction, data in trace.frames():
                    resolved = self.resolve(bus, frame_id, extended)
                    if resolved is None:
                        continue
                    decode, message = resolved
                    id_text = f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"
                    for name, value in decode.decode(message, data)["values"].items():
                        signal = next((s for s in message.signals if s.name == name), None)
                        writer.writerow([f"{timestamp - trace.first:.6f}", message.name, id_text, name,
                                         value["raw"], value["value"], getattr(signal, "unit", "") or "",
                                         value["label"] or ""])
                        rows += 1
        except OSError as exc:
            return {"ok": False, "error": f"导出失败：{exc}"}
        return {"ok": True, "path": str(target), "rows": rows}

    def close_all(self) -> None:
        with self.lock:
            for trace in self.traces.values():
                trace.close()
            self.traces.clear()


class SourceTap:
    """Receive-path observer for one data source (``main`` or ``vehicle``)."""

    def __init__(self, workspace: DbcWorkspace, bus_getter: Callable[[], str]) -> None:
        self.workspace = workspace
        self.bus_getter = bus_getter
        resolver = workspace.resolver(bus_getter)
        self.recorder = SeriesRecorder(resolver)
        self.audit = AuditAccumulator(resolver)
        self.audit_running = False
        self._bus = bus_getter()
        workspace.listeners.append(self.recorder.invalidate)

    def __call__(self, frame: Any) -> None:
        if not (self.recorder.specs or self.audit_running):
            return
        bus = self.bus_getter()
        if bus != self._bus:
            self._bus = bus
            self.recorder.invalidate()
        if self.recorder.specs:
            self.recorder.on_frame(frame.arbitration_id, frame.is_extended_id, frame.data, frame.timestamp)
        if self.audit_running:
            self.audit.ingest(frame.timestamp, frame.arbitration_id, frame.is_extended_id, frame.data,
                              frame.direction)
