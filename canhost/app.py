"""PyWebView application shell and JavaScript API."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import threading
from typing import Any

from . import __version__, __version_date__
from .transport import CanService
from .pcan_channels import discover_pcan_channels
from .bms.protocol import switch_catalog
from .telemetry import TelemetryService
from .updater import (
    DEFAULT_CNB_REPO,
    DEFAULT_REPO,
    HostUpdater,
    consume_update_result,
    installed_app_dir,
    installed_update_state,
    install_ready,
    record_installed_version,
    startup_cleanup,
    update_log_dir,
    changelog_page_url,
    release_page_url,
)
from .updater import _read_settings, _write_settings, settings_path


WEB_DIR = Path(__file__).parent / "web"
THEME_PREFERENCE_KEY = "theme_mode"
CONNECTION_PREFERENCE_KEY = "connection_preferences"
MONITOR_TX_ROWS_KEY = "monitor_tx_rows"
THEME_MODES = {"light", "dark"}
LIGHT_WINDOW_BACKGROUND = "#E9EDEF"
DARK_WINDOW_BACKGROUND = "#0D0E0F"


def _update_health_path_from_argv(argv: list[str] | None = None) -> Path | None:
    """Return the helper-provided health path without exposing it to JS."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        index = args.index("--update-health-file")
        value = args[index + 1]
    except (ValueError, IndexError):
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else None


def _write_update_health(path: Path, version: str) -> None:
    """Atomically tell the installer helper that backend and UI both loaded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{os.getpid()}.tmp")
    payload = {
        "pid": os.getpid(),
        "version": version,
        "ready_at": datetime.now().astimezone().isoformat(),
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _simulation_available() -> bool:
    """Return whether this is a local source run rather than a frozen release.

    ``python -m canhost`` is the documented launcher, but developer scripts and
    equivalent source entry points keep the same debug capability.  Windows and
    macOS release packages are both hardware-only.  The local telemetry
    publisher is a separate engineering tool and remains available everywhere.
    """
    return not getattr(sys, "frozen", False)


class Api:
    def __init__(self, update_health_path: Path | None = None) -> None:
        # PyWebView exposes every public member of js_api to JavaScript. Native
        # Window/WinForms objects must remain private; walking AccessibilityObject
        # recursively raises TYPE_E_CANTLOADLIBRARY on affected Windows systems.
        # Source runs expose the simulator for UI/protocol development.  Both
        # frozen releases remain hardware-only.
        simulation_available = _simulation_available()
        self._service = CanService(allow_simulation=simulation_available)
        # The engineering tools have their own transport lifetime.  This
        # lets the operator keep the BMS monitor on CAN1 while the bench
        # sender, the IVT configurator, or the fan tool uses its own PCAN handle.
        self._bench_service = CanService(allow_simulation=False)
        self._ivt_service = CanService(allow_simulation=False)
        # CANB is one physical connection.  Its receive stream feeds both the
        # vehicle protocol and a BMS mirror projection while the independent
        # CAN1 connection remains available for detailed data and commands.
        self._vehicle_service = CanService(protocol_kind="vehicle",
                                           allow_simulation=simulation_available,
                                           calibration_diagnostic_dir=(
                                               settings_path().parent / "calibration-diagnostics"))
        # MQTT telemetry is a fifth independent receive-only connection.  It
        # never changes a CAN mode and has no publish/command API.
        self._telemetry_service = TelemetryService()
        # The local telemetry publisher is an engineering tool for gateway
        # commissioning, so both release packages ship it: the specs bundle
        # canhost.telemetry.simulator and pyserial.  Its PCAN output opens its
        # own test channel and never enables the CAN simulation mode, which
        # keeps it usable on the hardware-only Windows build.  The import stays
        # inside the constructor so merely importing canhost.app (tests, build
        # tooling) does not pull in pyserial and protobuf.
        from .telemetry.simulator import TelemetrySimulatorService, available_serial_ports
        self._telemetry_simulator: Any = TelemetrySimulatorService()
        self._serial_port_provider: Any = available_serial_ports
        self._updater = HostUpdater(current_version=__version__, token_provider=self._read_update_token,
                                    cnb_repo=DEFAULT_CNB_REPO)
        self._updater_auto_checked = False
        self._startup_update_result = consume_update_result()
        # 每次启动判定一次“这次启动是否发生了升级”，安装包升级和软件内更新
        # 走同一条路径；结果只在本次进程内有效。
        self._startup_update_state = installed_update_state(__version__, __version_date__)
        self._update_health_path = update_health_path
        self._shutdown_finished = threading.Event()
        self._window: Any = None
        self._can_connection_lock = threading.Lock()
        self._preference_lock = threading.Lock()

    def _read_update_token(self) -> str | None:
        try:
            return _read_settings().get("github_token") or None
        except Exception:
            return None

    def theme_preference(self) -> str:
        """Return the persisted binary appearance preference."""
        try:
            mode = str(_read_settings().get(THEME_PREFERENCE_KEY) or "dark")
        except Exception:
            mode = "dark"
        return mode if mode in THEME_MODES else "dark"

    def set_theme_preference(self, mode: str) -> dict[str, Any]:
        """Persist the appearance without exposing the rest of settings.json."""
        value = str(mode)
        if value not in THEME_MODES:
            return {"ok": False, "error": "外观模式只能是 light 或 dark"}
        try:
            with self._preference_lock:
                payload = _read_settings()
                payload[THEME_PREFERENCE_KEY] = value
                _write_settings(payload)
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "mode": value}

    def workbench_preferences(self) -> dict[str, Any]:
        """Read durable operator choices without exposing updater credentials."""
        settings = _read_settings()
        connection = settings.get(CONNECTION_PREFERENCE_KEY)
        rows = settings.get(MONITOR_TX_ROWS_KEY)
        return {
            "connection": connection if isinstance(connection, dict) else None,
            "monitor_tx_rows": rows if isinstance(rows, list) else None,
        }

    def set_connection_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(preferences, dict):
            return {"ok": False, "error": "通道分配格式无效"}
        channels = (preferences.get("can1Channel"), preferences.get("canbChannel"))
        if any(not isinstance(channel, str) or len(channel) > 64 for channel in channels):
            return {"ok": False, "error": "PCAN 通道名称无效"}
        saved = {"version": 3, "can1Channel": channels[0], "canbChannel": channels[1]}
        return self._save_workbench_preference(CONNECTION_PREFERENCE_KEY, saved)

    def set_monitor_tx_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(rows, list) or len(rows) > 256:
            return {"ok": False, "error": "发送项列表无效或超过 256 项"}
        fields = {"uid": 96, "name": 80, "id": 32, "data": 64}
        saved = []
        for row in rows:
            if (not isinstance(row, dict)
                    or any(not isinstance(row.get(key), str) or len(row[key]) > limit
                           for key, limit in fields.items())
                    or not isinstance(row.get("extended"), bool)
                    or isinstance(row.get("cycle_ms"), bool)
                    or not isinstance(row.get("cycle_ms"), (int, float))):
                return {"ok": False, "error": "发送项格式无效"}
            if not math.isfinite(row["cycle_ms"]):
                return {"ok": False, "error": "发送项周期无效"}
            saved.append({key: row[key] for key in (*fields, "extended", "cycle_ms")})
        return self._save_workbench_preference(MONITOR_TX_ROWS_KEY, saved)

    def _save_workbench_preference(self, key: str, value: Any) -> dict[str, Any]:
        try:
            with self._preference_lock:
                payload = _read_settings()
                payload[key] = value
                _write_settings(payload)
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def bootstrap(self) -> dict[str, Any]:
        pcan_scan = discover_pcan_channels()
        profiles = [
            {"key": "can1", "name": "CAN1 · F405 / 从控 / IVT / 工具", "bitrate": 500000,
             "writable": True, "ivt_writable": True},
            {"key": "canb", "name": "CANB · ECU / Chroma · 500 kbit/s", "bitrate": 500000,
             "writable": False},
        ]
        if self._service.allow_simulation:
            profiles.insert(0, {
                "key": "simulation", "mode": "simulation",
                "name": "内置模拟数据 · CAN1 / 开发测试", "bitrate": 500000,
                "writable": True,
            })
        return {
            "version": __version__, "version_date": __version_date__, "switch_catalog": switch_catalog(),
            "simulation_enabled": self._service.allow_simulation,
            "bench_enabled": True,
            "ivt_enabled": True,
            "vehicle_enabled": True,
            "vehicle_simulation_enabled": self._vehicle_service.allow_simulation,
            "telemetry_enabled": True,
            "telemetry_simulator_enabled": self._telemetry_simulator is not None,
            "serial_ports": self._serial_port_provider() if self._serial_port_provider else [],
            "updater_enabled": install_ready(),
            "updater_check_enabled": True,
            "updater_repo": DEFAULT_REPO,
            "updater_cnb_repo": DEFAULT_CNB_REPO,
            "updater_has_token": self._updater.has_token(),
            "updater_settings_path": str(settings_path()),
            "updater_log_dir": str(update_log_dir()),
            "startup_update_result": self._startup_update_result,
            "runtime_platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
            "channels": pcan_scan["channels"],
            "channel_details": pcan_scan["channel_details"],
            "pcan_scan": pcan_scan,
            "profiles": profiles,
        }

    def refresh_pcan_channels(self) -> dict[str, Any]:
        """Rescan attached hardware after a USB hot-plug event."""
        return discover_pcan_channels()

    def mark_frontend_ready(self) -> dict[str, Any]:
        """Complete the updater health handshake after the first UI poll."""
        # 界面已经跑起来，这时才确认“本次启动的新版本可用”，供下一次升级对照。
        record_installed_version(__version__)
        if self._update_health_path is None:
            return {"ok": True, "required": False}
        try:
            _write_update_health(self._update_health_path, __version__)
        except OSError as exc:
            return {"ok": False, "required": True, "error": str(exc)}
        return {"ok": True, "required": True}

    def get_updater_status(self) -> dict[str, Any]:
        status = self._updater.status()
        status["has_token"] = self._updater.has_token()
        status["install_supported"] = install_ready()
        status["auto_checked"] = self._updater_auto_checked
        return status

    def startup_update_state(self) -> dict[str, Any]:
        """本次启动的版本信息与更新说明，供启动弹窗和侧栏使用。

        只读取启动时记录的版本标记和检查阶段缓存下来的 Release 记录，不再联网；
        说明缺失时界面按 ``release_url`` 引导到对应版本的 Release 页面。
        """
        state = dict(self._startup_update_state)
        state["latest"] = self._latest_release_summary()
        return state

    def _latest_release_summary(self) -> dict[str, Any]:
        """检查阶段留下的最新 Release 摘要；未检查过时字段为空。"""
        latest = self._updater.status().get("latest") or {}
        return {
            "tag_name": str(latest.get("tag_name") or ""),
            "name": str(latest.get("name") or ""),
            "html_url": str(latest.get("html_url") or ""),
            "published_at": str(latest.get("published_at") or ""),
            "body": str(latest.get("body") or ""),
            "changes": [str(item) for item in (latest.get("changes") or [])],
        }

    def release_history(self, online: bool = False) -> dict[str, Any]:
        """版本说明历史：默认读随包数据，``online`` 时用最近一次检查的发布列表。

        离线数据来自构建时写进程序的 ``RELEASE_HISTORY``，因此全新安装的机器
        或断网的车间笔记本也能回看每个版本改了什么。
        """
        from . import release_notes

        entries: list[dict[str, Any]] = [
            {
                "version": str(item["version"]),
                "date": str(item["date"]),
                "notes": [str(note) for note in item["notes"]],
                "source": "embedded",
            }
            for item in release_notes.history()
        ]
        online_entries: list[dict[str, Any]] = []
        if online:
            for release in self._updater.status().get("history") or []:
                tag = str(release.get("tag_name") or "")
                if not tag:
                    continue
                online_entries.append({
                    "version": release_notes.base_version(tag),
                    "date": str(release.get("published_at") or "")[:10],
                    "notes": [str(note) for note in (release.get("changes") or [])],
                    "url": str(release.get("html_url") or ""),
                    "prerelease": bool(release.get("prerelease")),
                    "source": "release",
                })
        return {
            "current_version": __version__,
            "entries": self._merge_history(entries, online_entries),
            "online": bool(online_entries),
            "release_url": release_page_url(__version__),
            "changelog_url": changelog_page_url(),
        }

    @staticmethod
    def _merge_history(
        offline: list[dict[str, Any]], online: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """同名版本以在线内容为准，其余按版本号从新到旧排列。"""
        from . import release_notes

        merged: dict[str, dict[str, Any]] = {str(item["version"]): item for item in offline}
        for item in online:
            merged[str(item["version"])] = {**merged.get(str(item["version"]), {}), **item}
        # 非 vX.Y.Z 的标签（历史发布或手工预发布）排在可解析版本之后。
        ordered = sorted(
            merged.values(),
            key=lambda item: release_notes.version_sort_key(str(item["version"])),
            reverse=True,
        )
        return ordered

    def check_for_updates(self, include_prerelease: bool = False) -> dict[str, Any]:
        result = self._updater.check(bool(include_prerelease))
        if result.get("ok", False):
            self._updater_auto_checked = True
        return result

    def auto_check_for_updates(self) -> dict[str, Any]:
        if self._updater_auto_checked:
            return {"ok": True, "state": self._updater.status()["state"], "skipped": True}
        result = self._updater.check(False)
        if result.get("ok", False):
            self._updater_auto_checked = True
        return result

    def download_update(self, tag: str | None = None) -> dict[str, Any]:
        return self._updater.start_download(tag)

    def install_update(self) -> dict[str, Any]:
        app_dir = installed_app_dir() if getattr(sys, "frozen", False) else Path.cwd()
        result = self._updater.start_install(app_dir)
        if result.get("ok"):
            self._schedule_update_exit()
        return result

    def _schedule_update_exit(self) -> None:
        """Let the JS call deliver its result before closing the window.

        PyWebView runs js_api methods on a background thread and then tries to
        deliver the JSON result back through the WebView.  Destroying the window
        from inside the same call can break that delivery, so the installer is
        started first and the exit is scheduled shortly afterwards.
        """
        def close_window() -> None:
            try:
                if self._window is not None:
                    self._window.destroy()
            except Exception:
                pass

        timer = threading.Timer(1.5, close_window)
        timer.name = "canhost-update-exit"
        timer.daemon = True
        timer.start()

        # A WebView2/native backend shutdown can occasionally stall after its
        # window is gone. The helper cannot replace loaded files until this PID
        # exits, so allow normal cleanup first and then guarantee the handoff.
        def force_exit_if_stuck() -> None:
            if not self._shutdown_finished.is_set():
                os._exit(0)

        watchdog = threading.Timer(20.0, force_exit_if_stuck)
        watchdog.name = "canhost-update-exit-watchdog"
        watchdog.daemon = True
        watchdog.start()

    def save_update_token(self, token: str) -> dict[str, Any]:
        return self._updater.set_token(token)

    def clear_update_token(self) -> dict[str, Any]:
        return self._updater.clear_token()

    def open_release_page(self, url: str) -> dict[str, Any]:
        """Open a release/changelog page or published asset in the system browser.

        Only the two published project hosts are accepted: the UI must not be
        able to hand an arbitrary string to the shell.
        """
        import webbrowser
        from urllib.parse import urlsplit

        allowed = ("github.com", "cnb.cool")
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not any(
            host == item or host.endswith("." + item) for item in allowed
        ):
            return {"ok": False, "error": "只能打开项目发布页"}
        try:
            webbrowser.open(parts.geturl())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def connect_can(self, config: dict[str, Any]) -> dict[str, Any]:
        mode = str(config.get("mode") or "pcan")
        profile = str(config.get("bus_profile") or "can1")
        if mode == "pcan" and profile != "can1":
            return {"ok": False, "error": "实体 CANB 已统一由 CANB 连接管理；此入口只连接 CAN1"}
        with getattr(self, "_can_connection_lock", nullcontext()):
            handover_error, handover_note, handover = self._physical_channel_handover(
                config, self._vehicle_service, "CANB", "CONN2")
            if handover_error:
                return {"ok": False, "error": handover_error}
            result = self._service.connect(config)
            result = self._start_auto_trace(self._service, config, result, "CONN1")
            if handover and not result.get("ok"):
                self._report_handover_rollback(result, handover)
            if handover_note and result.get("ok"):
                result["notice"] = handover_note
            return result

    def disconnect_can(self) -> dict[str, Any]:
        with getattr(self, "_can_connection_lock", nullcontext()):
            return self._service.disconnect()

    def connect_bench(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._bench_service.connect({
            "mode": "bench", "bus_profile": "can1",
            "channel": config.get("channel"), "bitrate": 500000,
        })

    def disconnect_bench(self) -> dict[str, Any]:
        return self._bench_service.disconnect()

    def get_bench_snapshot(self) -> dict[str, Any]:
        return self._bench_service.bench_snapshot()

    def connect_ivt(self, config: dict[str, Any]) -> dict[str, Any]:
        bitrate = int(config.get("bitrate") or 500000)
        if bitrate != 500000:
            return {"ok": False, "error": "IVT 配置连接固定使用 CAN1 500 kbit/s"}
        return self._ivt_service.connect({
            "mode": "pcan", "bus_profile": "can1",
            "channel": config.get("channel"), "bitrate": 500000,
        })

    def disconnect_ivt(self) -> dict[str, Any]:
        return self._ivt_service.disconnect()

    def get_ivt_snapshot(self) -> dict[str, Any]:
        return self._ivt_service.ivt_snapshot()

    def connect_vehicle(self, config: dict[str, Any]) -> dict[str, Any]:
        mode = "simulation" if config.get("mode") == "simulation" else "pcan"
        profile = str(config.get("bus_profile") or "canb")
        bitrate = int(config.get("bitrate") or 500000)
        if profile != "canb" or bitrate != 500000:
            return {"ok": False, "error": "统一 CANB 连接固定使用 CANB 500 kbit/s"}
        with getattr(self, "_can_connection_lock", nullcontext()):
            handover_error, handover_note, handover = self._physical_channel_handover(
                config, self._service, "CAN1", "CONN1")
            if handover_error:
                return {"ok": False, "error": handover_error}
            result = self._vehicle_service.connect({
                "mode": mode, "bus_profile": profile,
                "channel": config.get("channel"), "bitrate": bitrate,
            })
            result = self._start_auto_trace(self._vehicle_service, config, result, "CONN2")
            if handover and not result.get("ok"):
                self._report_handover_rollback(result, handover)
            if handover_note and result.get("ok"):
                result["notice"] = handover_note
            return result

    def disconnect_vehicle(self) -> dict[str, Any]:
        with getattr(self, "_can_connection_lock", nullcontext()):
            return self._vehicle_service.disconnect()

    def get_vehicle_snapshot(self) -> dict[str, Any]:
        return self._vehicle_service.vehicle_snapshot()

    def get_canb_bms_snapshot(self) -> dict[str, Any]:
        return self._vehicle_service.canb_bms_snapshot()

    def get_quick_snapshot(self) -> dict[str, Any]:
        return {"vehicle": self._vehicle_service.quick_snapshot()}

    @staticmethod
    def _connection_restore_context(service: CanService, name: str,
                                    prefix: str) -> dict[str, Any]:
        connection = dict(service.connection)
        return {
            "service": service,
            "name": name,
            "prefix": prefix,
            "config": {
                "mode": connection.get("mode", "pcan"),
                "bus_profile": connection.get("bus_profile"),
                "channel": connection.get("channel"),
                "bitrate": connection.get("bitrate", 500000),
            },
            "auto_record": bool(service.record_auto),
            "manual_recording": bool(service.record_kind and not service.record_auto),
        }

    def _restore_physical_connection(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context["service"]
        config = dict(context["config"])
        result = service.connect(config)
        if result.get("ok") and context.get("auto_record"):
            config["auto_record"] = True
            result = self._start_auto_trace(service, config, result, context["prefix"])
        return result

    def _report_handover_rollback(self, result: dict[str, Any],
                                  context: dict[str, Any]) -> None:
        original_error = str(result.get("error") or "新连接失败")
        restored = self._restore_physical_connection(context)
        if restored.get("ok"):
            suffix = f"原 {context['name']} 连接已恢复"
            if context.get("manual_recording"):
                suffix += "，但原手动留档已结束"
            if restored.get("warning"):
                suffix += f"；{restored['warning']}"
        else:
            suffix = (f"原 {context['name']} 连接恢复失败："
                      f"{restored.get('error', '未知错误')}")
        result["error"] = f"{original_error}；{suffix}"

    @staticmethod
    def _physical_channel_handover(config: dict[str, Any], other: CanService,
                                   other_name: str, other_prefix: str
                                   ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        """Resolve two live PCAN sessions trying to own the same adapter handle.

        两条总线配置了同一条通道（单通道轮换）时，连接另一条总线只有一种含义：
        把通道切换过去。在同一把连接锁内先断开对侧再继续，返回提示文案。
        对侧风扇标定进行中时拒绝切换，不让一次按钮点击隐式中止安全相关会话。
        """
        if str(config.get("mode") or "pcan") != "pcan":
            return None, None, None
        channel = str(config.get("channel") or "")
        connection = other.connection
        if not (channel and connection.get("connected") is True
                and connection.get("mode") == "pcan"
                and str(connection.get("channel") or "") == channel):
            return None, None, None
        for session in (other.fan_calib_session, other.battery_fan_calib_session):
            if session is not None and session.is_running():
                return (f"{channel} 正由 {other_name} 使用且风扇标定进行中；"
                        "请先停止标定再切换", None, None)
        restore_context = Api._connection_restore_context(
            other, other_name, other_prefix)
        other.disconnect()
        return (None, f"{channel} 已从 {other_name} 切换给本次连接",
                restore_context)

    @staticmethod
    def _physical_bus_mismatch(service: CanService) -> bool:
        return bool(service.snapshot().get("connection", {}).get("bus_mismatch"))

    def _restore_connection_pair(self, contexts: list[dict[str, Any]]) -> str:
        restored: list[str] = []
        failed: list[str] = []
        manual_recordings = [str(context["name"]) for context in contexts
                             if context.get("manual_recording")]
        for context in contexts:
            result = self._restore_physical_connection(context)
            if result.get("ok"):
                restored.append(str(context["name"]))
            else:
                failed.append(f"{context['name']}：{result.get('error', '未知错误')}")
        if failed:
            prefix = f"已恢复 {'、'.join(restored)}；" if restored else ""
            summary = f"{prefix}原连接恢复失败（{'；'.join(failed)}）"
        else:
            summary = "原 CAN1/CANB 连接已恢复"
        if manual_recordings:
            summary += f"；原 {'、'.join(manual_recordings)} 手动留档已结束"
        return summary

    def swap_mismatched_bus_channels(self,
                                     options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically exchange two live physical buses after reciprocal mismatch evidence."""
        if options is not None and not isinstance(options, dict):
            return {"ok": False, "error": "交换参数必须是对象"}
        auto_record = (options or {}).get("auto_record", True) is not False
        with getattr(self, "_can_connection_lock", nullcontext()):
            main = dict(self._service.connection)
            vehicle = dict(self._vehicle_service.connection)
            main_channel = str(main.get("channel") or "")
            vehicle_channel = str(vehicle.get("channel") or "")
            live_physical = (
                main.get("connected") is True and main.get("mode") == "pcan"
                and vehicle.get("connected") is True and vehicle.get("mode") == "pcan"
            )
            if not live_physical or not main_channel or not vehicle_channel:
                return {"ok": False, "error": "CAN1 与 CANB 必须同时保持实体连接"}
            if main_channel == vehicle_channel:
                return {"ok": False, "error": "两条总线当前占用同一通道，不能执行交换"}
            if not (self._physical_bus_mismatch(self._service)
                    and self._physical_bus_mismatch(self._vehicle_service)):
                return {"ok": False, "error": "双向接反证据已消失，请重新核对接线"}
            for session in (self._vehicle_service.fan_calib_session,
                            self._vehicle_service.battery_fan_calib_session):
                if session is not None and session.is_running():
                    return {"ok": False, "error": "风扇标定进行中；请先停止标定再交换通道"}

            originals = [
                self._connection_restore_context(self._service, "CAN1", "CONN1"),
                self._connection_restore_context(self._vehicle_service, "CANB", "CONN2"),
            ]
            self._service.disconnect()
            self._vehicle_service.disconnect()

            can1_config = {
                "mode": "pcan", "bus_profile": "can1",
                "channel": vehicle_channel, "bitrate": 500000,
                "auto_record": auto_record,
            }
            canb_config = {
                "mode": "pcan", "bus_profile": "canb",
                "channel": main_channel, "bitrate": 500000,
                "auto_record": auto_record,
            }
            can1_result = self._service.connect(can1_config)
            can1_result = self._start_auto_trace(
                self._service, can1_config, can1_result, "CONN1")
            if not can1_result.get("ok"):
                rollback = self._restore_connection_pair(originals)
                return {"ok": False,
                        "error": f"CAN1 重连失败：{can1_result.get('error', '未知错误')}；{rollback}"}

            canb_result = self._vehicle_service.connect(canb_config)
            canb_result = self._start_auto_trace(
                self._vehicle_service, canb_config, canb_result, "CONN2")
            if not canb_result.get("ok"):
                self._service.disconnect()
                self._vehicle_service.disconnect()
                rollback = self._restore_connection_pair(originals)
                return {"ok": False,
                        "error": f"CANB 重连失败：{canb_result.get('error', '未知错误')}；{rollback}"}

            warnings = [str(item["warning"]) for item in (can1_result, canb_result)
                        if item.get("warning")]
            result = {
                "ok": True,
                "can1_channel": vehicle_channel,
                "canb_channel": main_channel,
                "notice": (f"通道已交换并重连：CAN1 → {vehicle_channel}，"
                           f"CANB → {main_channel}"),
            }
            if warnings:
                result["warning"] = "；".join(warnings)
            return result

    def connect_telemetry(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._telemetry_service.connect(config)

    def disconnect_telemetry(self) -> dict[str, Any]:
        return self._telemetry_service.disconnect()

    def get_telemetry_snapshot(self) -> dict[str, Any]:
        return self._telemetry_service.snapshot()

    def start_telemetry_simulator(self, config: dict[str, Any]) -> dict[str, Any]:
        if self._telemetry_simulator is None:
            return {"ok": False, "error": "当前发布版本未包含本地遥测模拟器"}
        return self._telemetry_simulator.start(config)

    def stop_telemetry_simulator(self) -> dict[str, Any]:
        if self._telemetry_simulator is None:
            return {"ok": True, "unchanged": True}
        return self._telemetry_simulator.stop()

    def get_telemetry_simulator_snapshot(self) -> dict[str, Any]:
        if self._telemetry_simulator is None:
            return {"state": "unavailable", "running": False,
                    "error": "当前发布版本未包含本地遥测模拟器"}
        return self._telemetry_simulator.snapshot()

    def send_fan_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._vehicle_service.send_fan_command(name, values, acknowledged)

    def send_battery_fan_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._vehicle_service.send_battery_fan_command(name, values, acknowledged)

    def start_fan_calibration(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if options is not None and not isinstance(options, dict):
            return {"ok": False, "error": "标定参数必须是对象"}
        opts = options or {}
        try:
            channel = int(opts.get("channel", 1))
            hold_s = float(opts.get("hold_s", 6.0))
            max_current_a = float(opts.get("max_current_a", 18.0))
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "标定通道、保持时间或电流保护参数无效"}
        steps = opts.get("steps")
        tier = str(opts.get("tier", "dcdc"))
        with getattr(self, "_can_connection_lock", nullcontext()):
            return self._vehicle_service.start_fan_calibration(
                channel, steps, hold_s, max_current_a, tier)

    def confirm_dcdc_ready(self) -> dict[str, Any]:
        """操作者独立确认 DCDC 已实际供电。

        只用于手动逐点调试时的短租约覆盖；自动阶梯扫频不依赖也不接受该覆盖，
        它要求 PDM 实测判据连续稳定 3 秒。
        """
        confirmed = self._vehicle_service.send_fan_command(
            "fan_calib",
            {"action": 4, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 60},
            True,
        )
        if not confirmed.get("ok"):
            return {"ok": False, "error": f"DCDC 就绪确认失败：{confirmed.get('error', '未知错误')}"}
        return {"ok": True, "message": "DCDC 就绪已确认，可在 60 秒内进行手动标定；自动扫频仍需 PDM 实测判据"}

    def stop_fan_calibration(self) -> dict[str, Any]:
        return self._vehicle_service.stop_fan_calibration()

    def export_fan_calibration(self, format_type: str = "csv") -> dict[str, Any]:
        return self._vehicle_service.export_fan_calibration(format_type)

    def _choose_calibration_export(self, format_type: str, *, battery_fan: bool) -> dict[str, Any]:
        """Save calibration records through the native file dialog."""
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        export_format = str(format_type).lower()
        if battery_fan:
            if export_format != "csv":
                return {"ok": False, "error": "电池箱风扇标定仅支持 CSV 导出"}
        elif export_format not in {"csv", "json"}:
            return {"ok": False, "error": "整车风扇标定仅支持 CSV 或 JSON 导出"}

        try:
            import webview
            extension = ".json" if export_format == "json" else ".csv"
            prefix = "battery_fan_calibration" if battery_fan else "fan_calibration"
            file_types = (("JSON 文件 (*.json)",) if export_format == "json"
                          else ("CSV 文件 (*.csv)",))
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}{extension}",
                file_types=file_types,
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            selected_path = selected if isinstance(selected, str) else selected[0]
            path = Path(selected_path).expanduser()
            if path.suffix.lower() != extension:
                path = path.with_suffix(extension)

            exported = (self._vehicle_service.export_battery_fan_calibration()
                        if battery_fan else
                        self._vehicle_service.export_fan_calibration(export_format))
            if not exported.get("ok"):
                return exported
            data = exported.get("data")
            if not isinstance(data, str):
                return {"ok": False, "error": "标定导出内容无效"}
            encoding = "utf-8" if export_format == "json" else "utf-8-sig"
            path.write_text(data, encoding=encoding, newline="")
            return {"ok": True, "path": str(path), "format": export_format}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_export_fan_calibration(self, format_type: str = "csv") -> dict[str, Any]:
        """Choose a destination and persist FanController calibration data."""
        return self._choose_calibration_export(format_type, battery_fan=False)

    def start_battery_fan_calibration(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if options is not None and not isinstance(options, dict):
            return {"ok": False, "error": "标定参数必须是对象"}
        opts = options or {}
        try:
            hold_s = float(opts.get("hold_s", 5.0))
            max_current_a = float(opts.get("max_current_a", 18.0))
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "保持时间或电流保护参数无效"}
        with getattr(self, "_can_connection_lock", nullcontext()):
            return self._vehicle_service.start_battery_fan_calibration(
                opts.get("steps"), hold_s, max_current_a)

    def stop_battery_fan_calibration(self) -> dict[str, Any]:
        return self._vehicle_service.stop_battery_fan_calibration()

    def export_battery_fan_calibration(self) -> dict[str, Any]:
        return self._vehicle_service.export_battery_fan_calibration()

    def choose_export_battery_fan_calibration(self) -> dict[str, Any]:
        """Choose a destination and persist F405 fan calibration data."""
        return self._choose_calibration_export("csv", battery_fan=True)

    def get_snapshot(self) -> dict[str, Any]:
        return self._service.snapshot()

    def send_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._service.send_command(name, values, acknowledged)

    def read_flash_fault_logs(self, limit: int = 50) -> dict[str, Any]:
        return self._service.read_flash_fault_logs(limit)

    def read_ivt_config(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ivt_service.read_ivt_config(options)

    def configure_ivt_bms_can1(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ivt_service.configure_ivt_bms_can1(options)

    def bench_command(self, command: str) -> dict[str, Any]:
        return self._bench_service.bench_command(command)

    def _monitor_service(self, source: str = "main") -> CanService:
        return self._vehicle_service if source == "vehicle" else self._service

    def _auto_trace_path(self, service: CanService, prefix: str) -> Path:
        trace_dir = settings_path().parent / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        connection = service.connection
        profile = str(connection.get("bus_profile") or "CAN").upper()
        channel = str(connection.get("channel") or "PCAN").replace("/", "_").replace("\\", "_")
        return trace_dir / f"{prefix}_{profile}_{channel}_{datetime.now():%Y%m%d_%H%M%S_%f}.bmslog"

    def _start_auto_trace(self, service: CanService, config: dict[str, Any],
                          result: dict[str, Any], prefix: str) -> dict[str, Any]:
        if not result.get("ok") or config.get("mode", "pcan") != "pcan" or config.get("auto_record", True) is False:
            return result
        recording = service.start_recording(str(self._auto_trace_path(service, prefix)), auto=True)
        result["recording"] = recording
        if not recording.get("ok"):
            result["warning"] = f"PCAN 已连接，但自动留档未启动：{recording.get('error', '未知错误')}"
        return result

    def choose_record_file(self, source: str = "main") -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            source_label = "1" if source == "main" else "2"
            default = f"CAN_{source_label}_{datetime.now():%Y%m%d_%H%M%S}.bmslog"
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("CAN 数据记录 (*.bmslog)", "CSV 文件 (*.csv)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self._monitor_service(source).start_recording(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_recording(self, source: str = "main") -> dict[str, Any]:
        return self._monitor_service(source).stop_recording()

    def set_monitor_auto_record(self, source: str, enabled: bool) -> dict[str, Any]:
        service = self._monitor_service(source)
        if not enabled:
            if service.record_auto:
                return service.stop_recording()
            return {"ok": True, "unchanged": True}
        if service.record_kind:
            return {"ok": True, "unchanged": True, "path": service.record_path}
        if not service.connection.get("connected") or service.connection.get("mode") != "pcan":
            return {"ok": False, "error": "连接真实 PCAN 后才能自动留档"}
        prefix = "CONN2" if source == "vehicle" else "CONN1"
        return service.start_recording(str(self._auto_trace_path(service, prefix)), auto=True)

    def choose_export_monitor_csv(self, source: str = "main") -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"CAN_{'2' if source == 'vehicle' else '1'}_{datetime.now():%Y%m%d_%H%M%S}.csv",
                file_types=("CSV 文件 (*.csv)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self._monitor_service(source).export_recording_csv(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def send_monitor_frame(self, source: str, spec: dict[str, Any],
                           acknowledged: bool = False) -> dict[str, Any]:
        return self._monitor_service(source).send_monitor_frame(spec, acknowledged)

    def configure_monitor_periodic(self, source: str, task_id: str, spec: dict[str, Any],
                                   active: bool, acknowledged: bool = False) -> dict[str, Any]:
        return self._monitor_service(source).configure_monitor_periodic(
            task_id, spec, active, acknowledged)

    def choose_replay_file(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("BMS 数据记录 (*.bmslog;*.csv)", "BMS 原生记录 (*.bmslog)", "CSV 文件 (*.csv)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self._service.load_replay(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replay_control(self, action: str, value: float | None = None) -> dict[str, Any]:
        return self._service.replay_control(action, value)

    def close(self) -> None:
        self._service.disconnect()
        self._bench_service.disconnect()
        self._ivt_service.disconnect()
        self._vehicle_service.disconnect()
        self._telemetry_service.disconnect()
        if self._telemetry_simulator is not None:
            self._telemetry_simulator.stop()


def _run_startup_update_cleanup(keep_temp_dir: Path | None = None) -> None:
    """Delete old-version backups and update temp dirs once the app runs.

    The updated build itself proves the update worked by reaching this point,
    so backups past the rollback window are no longer needed.  Runs in a daemon
    thread; removal is best effort and never blocks or breaks startup.
    """
    try:
        keep = {keep_temp_dir} if keep_temp_dir is not None else None
        startup_cleanup(installed_app_dir(), keep_temp_dirs=keep)
    except Exception:
        pass


def _initial_window_background(api: Api) -> str:
    """Match the native surface to the saved theme before the web view paints."""
    return LIGHT_WINDOW_BACKGROUND if api.theme_preference() == "light" else DARK_WINDOW_BACKGROUND


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit("缺少 pywebview。请先执行：pip install -r requirements.txt")
    if "--packaging-smoke-test" in sys.argv or "--pcan-driver-smoke-test" in sys.argv:
        # Exercise the real-PCAN backend first.  CI cannot attach a USB adapter
        # or install the third-party macOS driver, but it must still prove that
        # PyInstaller included every Python module needed to load that driver.
        import can  # noqa: F401
        from can.interfaces.pcan.basic import PCANBasic
        from can.interfaces.pcan.pcan import PcanBus  # noqa: F401

        if "--pcan-driver-smoke-test" in sys.argv:
            # The build script invokes this extra check when libPCBUSB is
            # installed on the build Mac.  Constructing PCANBasic proves that
            # the frozen app can find and load the external arm64 driver; no
            # adapter is opened and no CAN frame is sent.
            PCANBasic()
            return
        import paho.mqtt.client  # noqa: F401
        import time
        from .bms.simulator import BmsSimulator  # noqa: F401
        from .telemetry import fsae_telemetry_pb2  # noqa: F401
        # The local telemetry publisher ships in both release packages, so its
        # frame generator and pyserial must be importable everywhere.  Serial is
        # imported and enumerated directly: the port list goes through
        # available_serial_ports(), which swallows a missing pyserial into an
        # empty list and would hide the packaging mistake this check exists for.
        from .telemetry.simulator import TelemetryFrameGenerator
        import serial  # noqa: F401
        from serial.tools import list_ports

        list_ports.comports()
        # A local source smoke run may load the debug CAN simulation.  Both
        # frozen release packages must reject that mode.
        simulation_channels = _simulation_available()
        if simulation_channels:
            from .vehicle.simulator import VehicleSimulator  # noqa: F401
        # The updater must prove the bundle really ships usable CA certs.
        from . import trust
        if not trust.ca_bundle_path():
            raise SystemExit("打包自检失败：certifi CA 证书包缺失")
        if not trust.certifi_roots_loaded(trust.https_ssl_context()):
            raise SystemExit("打包自检失败：certifi CA 证书未完整加载")
        if not (WEB_DIR / "index.html").is_file():
            raise SystemExit("打包自检失败：缺少 canhost/web/index.html")
        # 更新完成弹窗和“版本历史”读随包说明；标签构建写了版本号却没有条目，
        # 说明 CHANGELOG 与构建标签脱节，必须在发布前拦下。
        from . import release_notes
        embedded = release_notes.embedded_release()
        if embedded["version"] == __version__ and not embedded["notes"]:
            raise SystemExit(
                f"打包自检失败：随包更新说明为空（版本 {__version__}）；"
                f"请检查 CHANGELOG.md 是否有该版本小节"
            )
        api = Api()
        try:
            bootstrap = api.bootstrap()
            if not isinstance(bootstrap.get("pcan_scan"), dict):
                raise SystemExit("打包自检失败：缺少 PCAN 通道枚举接口")
            if not bootstrap["telemetry_simulator_enabled"]:
                raise SystemExit("打包自检失败：发布包未包含本地遥测模拟器")
            simulator_frame = TelemetryFrameGenerator().generate_frame()
            if not simulator_frame.SerializeToString() or len(simulator_frame.vehicle_state.motors) != 4:
                raise SystemExit("打包自检失败：本地遥测模拟器未生成完整 TelemetryFrame")
            if (getattr(sys, "frozen", False) and sys.platform == "darwin"
                    and not bootstrap["updater_enabled"]):
                raise SystemExit("打包自检失败：macOS 应用包未启用两步软件更新")
            if not simulation_channels:
                # 发布包及其他启动方式不带调试模拟通道：既不开放标志，也要真的拒绝连接，
                # 界面上的“调试模拟”开关因此根本不会出现。
                if bootstrap["simulation_enabled"] or bootstrap["vehicle_simulation_enabled"]:
                    raise SystemExit("打包自检失败：硬件专用发布包不应提供调试模拟通道")
                rejected = api.connect_can({"mode": "simulation", "bus_profile": "can1",
                                            "bitrate": 500000})
                if rejected.get("ok") or "真实 PCAN" not in str(rejected.get("error", "")):
                    raise SystemExit("打包自检失败：硬件专用发布包未拒绝调试模拟通道")
                return
            if not bootstrap["simulation_enabled"] or not bootstrap["vehicle_simulation_enabled"]:
                raise SystemExit("源码自检失败：源码运行未启用调试模拟通道")
            bms_result = api.connect_can({
                "mode": "simulation", "bus_profile": "can1", "bitrate": 500000,
            })
            vehicle_result = api.connect_vehicle({
                "mode": "simulation", "bus_profile": "canb", "bitrate": 500000,
            })
            if not bms_result.get("ok") or not vehicle_result.get("ok"):
                raise SystemExit("源码自检失败：调试模拟通道无法启动")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                bms_ready = api.get_snapshot()["overview"].get("voltage_v") is not None
                vehicle_ready = api.get_vehicle_snapshot()["pack"].get("voltage_v") is not None
                if bms_ready and vehicle_ready:
                    break
                time.sleep(0.05)
            else:
                raise SystemExit("源码自检失败：调试模拟通道未产出完整数据")
        finally:
            api.close()
        return
    update_health_path = _update_health_path_from_argv()
    api = Api(update_health_path=update_health_path)
    if install_ready():
        keep_temp_dir = update_health_path.parent if update_health_path is not None else None
        cleanup_thread = threading.Thread(target=_run_startup_update_cleanup,
                                          args=(keep_temp_dir,),
                                          name="canhost-startup-cleanup", daemon=True)
        cleanup_thread.start()
    window = webview.create_window(
        "BITFSAE · CAN HOST", url=(WEB_DIR / "index.html").as_uri(), js_api=api,
        width=1460, height=920, min_size=(1120, 720),
        background_color=_initial_window_background(api),
        zoomable=True,
    )
    api._window = window
    debug = "--debug" in sys.argv
    try:
        webview.start(debug=debug)
    finally:
        try:
            api.close()
        finally:
            api._shutdown_finished.set()


if __name__ == "__main__":
    main()
