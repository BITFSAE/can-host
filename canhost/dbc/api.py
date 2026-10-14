"""JavaScript-facing API of the DBC page (mixed into ``canhost.app.Api``).

Every public ``dbc_*`` method is callable from the page, so each one validates
its input and returns ``{"ok": ...}`` instead of raising.  The page only ever
learns about CAN data through the monitor facts of an existing connection and
only ever sends through the guarded raw-frame API; this module opens no bus.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..updater import _read_settings
from .document import DbcError
from .workspace import BUS_LABELS, DbcWorkspace, SourceTap

SOURCES = ("main", "vehicle")
MAX_KEYS = 64
MAX_SPECS = 24
SETTINGS_KEY = "dbc_state"
VIEW_KEY = "dbc_view"
MAX_WATCH = 64


def _is_text(value: Any, limit: int = 256) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


class DbcApi:
    """Mixin.  The host provides ``_service``, ``_vehicle_service``, ``_window`` and
    ``_save_workbench_preference``; ``_init_dbc`` must run once from ``__init__``."""

    def _init_dbc(self) -> None:
        self._dbc = DbcWorkspace()
        self._dbc_restored = False
        self._dbc_problems: list[str] = []
        self._dbc_taps = {
            "main": SourceTap(self._dbc, self._service.dbc_bus),
            "vehicle": SourceTap(self._dbc, self._vehicle_service.dbc_bus),
        }
        self._service.frame_observers.append(self._dbc_taps["main"])
        self._vehicle_service.frame_observers.append(self._dbc_taps["vehicle"])

    # -- helpers --------------------------------------------------------------

    def _dbc_source(self, source: Any) -> tuple[str, Any] | None:
        if source == "main":
            return "main", self._service
        if source == "vehicle":
            return "vehicle", self._vehicle_service
        return None

    def _dbc_persist(self) -> None:
        self._save_workbench_preference(SETTINGS_KEY, self._dbc.persisted())

    def _dbc_restore_once(self) -> None:
        """Reopen last session's files on first use (the monitor page needs their names too)."""
        if self._dbc_restored:
            return
        self._dbc_restored = True
        try:
            saved = _read_settings().get(SETTINGS_KEY)
        except Exception:
            saved = None
        self._dbc_problems = self._dbc.restore(saved)

    @staticmethod
    def _central_candidates() -> list[dict[str, str]]:
        folder = Path(__file__).resolve().parents[3] / "vehicle-interfaces" / "can"
        if not folder.is_dir():
            return []
        return [{"path": str(path), "name": path.name} for path in sorted(folder.glob("*.dbc"))
                if "_candbpp" not in path.stem]

    def _dbc_reply(self, call: Any, *args: Any, persist: bool = False) -> dict[str, Any]:
        try:
            result = call(*args)
        except DbcError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # keep the bridge alive; the message is shown to the operator
            return {"ok": False, "error": f"DBC 操作失败：{exc}"}
        if persist and result.get("ok"):
            self._dbc_persist()
        return result

    # -- files ------------------------------------------------------------------

    def dbc_state(self) -> dict[str, Any]:
        self._dbc_restore_once()
        problems, self._dbc_problems = self._dbc_problems, []
        state = self._dbc.state()
        try:
            view = _read_settings().get(VIEW_KEY)
        except Exception:
            view = None
        state.update(ok=True, problems=problems, suggestions=self._central_candidates(),
                     view=view if isinstance(view, dict) else {},
                     buses=[{"key": key, "label": label} for key, label in BUS_LABELS.items()])
        return state

    def dbc_set_view(self, view: dict[str, Any]) -> dict[str, Any]:
        """Remember the watch list and the plotted signals between sessions."""
        if not isinstance(view, dict):
            return {"ok": False, "error": "视图格式无效"}
        saved: dict[str, Any] = {}
        for name, limit in (("watch", MAX_WATCH), ("plot", MAX_SPECS)):
            entries = view.get(name, [])
            if not isinstance(entries, list) or len(entries) > limit:
                return {"ok": False, "error": "观察表或曲线列表过长"}
            clean = []
            for entry in entries:
                if (not isinstance(entry, dict) or entry.get("source") not in SOURCES
                        or not _is_text(entry.get("key"), 24) or not _is_text(entry.get("signal"), 160)):
                    return {"ok": False, "error": "观察项格式无效"}
                clean.append({"source": entry["source"], "key": entry["key"], "signal": entry["signal"]})
            saved[name] = clean
        if view.get("source") in SOURCES:
            saved["source"] = view["source"]
        return self._save_workbench_preference(VIEW_KEY, saved)

    def dbc_choose_open(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("DBC 数据库 (*.dbc)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not selected:
            return {"ok": False, "cancelled": True}
        return self._dbc_open_many([str(path) for path in selected])

    def _dbc_open_many(self, paths: list[str]) -> dict[str, Any]:
        if not isinstance(paths, list) or not paths or len(paths) > 12:
            return {"ok": False, "error": "文件列表无效"}
        opened, errors = [], []
        for path in paths:
            if not _is_text(path, 1024):
                errors.append("文件路径无效")
                continue
            result = self.dbc_open(path)
            (opened if result.get("ok") else errors).append(result.get("file") or result.get("error"))
        return {"ok": bool(opened), "files": opened, "error": "；".join(errors) if errors else None}

    def dbc_open(self, path: str, bus: str | None = None) -> dict[str, Any]:
        if not _is_text(path, 1024) or (bus is not None and bus not in BUS_LABELS):
            return {"ok": False, "error": "文件路径或总线无效"}
        self._dbc_restore_once()
        return self._dbc_reply(self._dbc.open_path, path, bus, persist=True)

    def dbc_new(self, name: str = "未命名.dbc") -> dict[str, Any]:
        return self._dbc_reply(self._dbc.new_file, name if _is_text(name, 80) else "未命名.dbc")

    def dbc_close(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.close, file_id, persist=True)

    def dbc_reload(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.reload, file_id)

    def dbc_set_bus(self, file_id: str, bus: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.set_bus, file_id, bus, persist=True)

    def dbc_detail(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.detail, file_id)

    # -- editing ----------------------------------------------------------------

    def dbc_edit(self, file_id: str, ops: Any) -> dict[str, Any]:
        if isinstance(ops, dict):
            ops = [ops]
        if not isinstance(ops, list) or not 0 < len(ops) <= 200:
            return {"ok": False, "error": "编辑操作无效"}
        return self._dbc_reply(self._dbc.edit_many, file_id, ops)

    def dbc_undo(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.undo, file_id)

    def dbc_redo(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.redo, file_id)

    def dbc_compare(self, first_id: str, second_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.compare, first_id, second_id)

    def dbc_save_review(self, file_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.save_review, file_id)

    def dbc_save(self, file_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        path = options.get("path")
        if path is not None and (not _is_text(path, 1024) or not str(path).lower().endswith(".dbc")):
            return {"ok": False, "error": "保存路径必须是 .dbc 文件"}
        encoding = options.get("encoding")
        if encoding is not None and encoding not in ("utf-8", "utf-8-sig", "gbk"):
            return {"ok": False, "error": "编码只能是 UTF-8、UTF-8 带 BOM 或 GBK"}
        return self._dbc_reply(self._dbc.save, file_id, path, encoding, bool(options.get("candbpp")),
                               persist=True)

    def dbc_choose_save(self, file_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask for a destination, then save there (save as)."""
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        state = self._dbc.state()
        current = next((f for f in state["files"] if f["id"] == file_id), None)
        if current is None:
            return {"ok": False, "error": "该 DBC 已关闭"}
        try:
            import webview
            default = current["name"] if current["name"].lower().endswith(".dbc") else current["name"] + ".dbc"
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default, file_types=("DBC 数据库 (*.dbc)",))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not selected:
            return {"ok": False, "cancelled": True}
        target = selected if isinstance(selected, str) else selected[0]
        if not str(target).lower().endswith(".dbc"):
            target = f"{target}.dbc"
        merged = dict(options) if isinstance(options, dict) else {}
        merged["path"] = str(target)
        return self.dbc_save(file_id, merged)

    # -- live data ---------------------------------------------------------------

    def dbc_live(self, source: str = "main", options: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None:
            return {"ok": False, "error": "数据源只能是 main 或 vehicle"}
        options = options if isinstance(options, dict) else {}
        keys = options.get("keys", [])
        if not isinstance(keys, list) or len(keys) > MAX_KEYS or any(not _is_text(k, 24) for k in keys):
            return {"ok": False, "error": "报文列表无效"}
        self._dbc_restore_once()
        _, service = resolved
        facts = service.monitor_latest()
        bus = service.dbc_bus()
        try:
            result = self._dbc.live(bus, facts["frames"], facts["clock"], keys,
                                    include_messages=options.get("messages", True) is not False,
                                    include_unknown=options.get("unknown", True) is not False)
        except Exception as exc:
            return {"ok": False, "error": f"实时解码失败：{exc}"}
        result.update(ok=True, source=source, connection=facts["connection"], clock=facts["clock"])
        return result

    def dbc_series_set(self, source: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None or not isinstance(specs, list) or len(specs) > MAX_SPECS:
            return {"ok": False, "error": "曲线订阅无效"}
        clean = []
        for spec in specs:
            try:
                clean.append({"sid": str(spec["sid"])[:160], "id": int(spec["id"]),
                              "extended": bool(spec["extended"]), "signal": str(spec["signal"])[:160]})
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": "曲线订阅项无效"}
        try:
            sids = self._dbc_taps[resolved[0]].recorder.set_series(clean)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "series": sids}

    def dbc_series_fetch(self, source: str, cursors: dict[str, int] | None = None) -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None:
            return {"ok": False, "error": "数据源无效"}
        cursors = cursors if isinstance(cursors, dict) else {}
        try:
            safe = {str(k): int(v) for k, v in cursors.items()}
        except (TypeError, ValueError):
            return {"ok": False, "error": "游标无效"}
        service = resolved[1]
        return {"ok": True, "series": self._dbc_taps[resolved[0]].recorder.fetch(safe),
                "clock": service.monitor_latest()["clock"]}

    def dbc_series_clear(self, source: str) -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None:
            return {"ok": False, "error": "数据源无效"}
        self._dbc_taps[resolved[0]].recorder.clear()
        return {"ok": True}

    # -- audit ---------------------------------------------------------------------

    def dbc_audit(self, source: str, action: str = "report") -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None or action not in ("start", "stop", "reset", "report"):
            return {"ok": False, "error": "对账操作无效"}
        tap = self._dbc_taps[resolved[0]]
        if action == "start":
            tap.audit.reset()
            tap.audit_running = True
        elif action == "stop":
            tap.audit_running = False
        elif action == "reset":
            tap.audit.reset()
        bus = resolved[1].dbc_bus()
        report = tap.audit.report(self._dbc.expected_messages(bus))
        return {"ok": True, "running": tap.audit_running, "bus": bus, "report": report}

    # -- signal-level send -------------------------------------------------------

    def dbc_form(self, source: str, key: str) -> dict[str, Any]:
        resolved = self._dbc_source(source)
        if resolved is None or not _is_text(key, 24):
            return {"ok": False, "error": "报文无效"}
        return self._dbc_reply(self._dbc.message_form, resolved[1].dbc_bus(), key)

    def dbc_encode(self, source: str, key: str, values: dict[str, Any]) -> dict[str, Any]:
        """Turn signal values into payload bytes.  Sending stays with
        ``send_monitor_frame`` / ``configure_monitor_periodic`` and their guards."""
        resolved = self._dbc_source(source)
        if resolved is None or not _is_text(key, 24) or not isinstance(values, dict) or len(values) > 256:
            return {"ok": False, "error": "编码参数无效"}
        return self._dbc_reply(self._dbc.encode, resolved[1].dbc_bus(), key, values)

    # -- traces --------------------------------------------------------------------

    def dbc_trace_choose(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("CAN 数据记录 (*.bmslog;*.csv)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not selected:
            return {"ok": False, "cancelled": True}
        return self._dbc.open_trace(selected if isinstance(selected, str) else selected[0])

    def dbc_trace_close(self, trace_id: str) -> dict[str, Any]:
        return self._dbc_reply(self._dbc.close_trace, trace_id)

    def dbc_trace_series(self, trace_id: str, bus: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
        if bus not in ("can1", "canb") or not isinstance(specs, list) or len(specs) > MAX_SPECS:
            return {"ok": False, "error": "曲线参数无效"}
        try:
            clean = [{"sid": str(s["sid"])[:160], "id": int(s["id"]), "extended": bool(s["extended"]),
                      "signal": str(s["signal"])[:160]} for s in specs]
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "曲线参数无效"}
        return self._dbc_reply(self._dbc.trace_series, trace_id, bus, clean)

    def dbc_trace_audit(self, trace_id: str, bus: str) -> dict[str, Any]:
        if bus not in ("can1", "canb"):
            return {"ok": False, "error": "总线无效"}
        return self._dbc_reply(self._dbc.trace_audit, trace_id, bus)

    def dbc_trace_export(self, trace_id: str, bus: str) -> dict[str, Any]:
        if bus not in ("can1", "canb") or not self._window:
            return {"ok": False, "error": "总线无效或窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"DBC信号_{datetime.now():%Y%m%d_%H%M%S}.csv",
                file_types=("CSV 文件 (*.csv)",))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not selected:
            return {"ok": False, "cancelled": True}
        return self._dbc_reply(self._dbc.trace_export_csv, trace_id, bus,
                               selected if isinstance(selected, str) else selected[0])

    def dbc_save_text(self, suggested_name: str, text: str) -> dict[str, Any]:
        """Save text produced by the page (for example the plotted samples as CSV)."""
        if not self._window or not isinstance(text, str) or len(text) > 64 * 1024 * 1024:
            return {"ok": False, "error": "没有可保存的内容或窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=str(suggested_name or "dbc.csv")[:120],
                file_types=("CSV 文件 (*.csv)",))
            if not selected:
                return {"ok": False, "cancelled": True}
            target = Path(selected if isinstance(selected, str) else selected[0])
            if target.suffix.lower() != ".csv":
                target = target.with_suffix(".csv")
            target.write_text(text, encoding="utf-8-sig", newline="")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}

    def _dbc_close(self) -> None:
        self._dbc.close_all()

    def _handle_file_drop(self, event: Any) -> list[str]:
        """Open ``.dbc`` files dropped on the window; returns the paths that were opened.

        pywebview adds ``pywebviewFullPath`` to dropped files only because this handler
        is registered.  Anything that is not a DBC is ignored, never opened or run."""
        try:
            files = (event or {}).get("dataTransfer", {}).get("files") or []
        except AttributeError:
            return []
        paths = [str(item.get("pywebviewFullPath")) for item in files
                 if isinstance(item, dict) and str(item.get("pywebviewFullPath", "")).lower().endswith(".dbc")]
        if paths:
            self._dbc_open_many(paths[:12])
        return paths

    def _register_file_drop(self) -> None:
        """Called once the page has loaded; failure only means drag-and-drop is unavailable."""
        try:
            from webview.dom import DOMEventHandler
            self._window.dom.document.events.drop += DOMEventHandler(self._handle_file_drop, True, True)
        except Exception:
            pass
