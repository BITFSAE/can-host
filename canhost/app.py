"""PyWebView application shell and JavaScript API."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import sys
import threading
from typing import Any

from . import __version__, __version_date__
from .transport import CanService
from .bms.protocol import switch_catalog
from .telemetry import TelemetryService
from .updater import DEFAULT_REPO, HostUpdater, install_ready
from .updater import _read_settings, settings_path


WEB_DIR = Path(__file__).parent / "web"


class Api:
    def __init__(self) -> None:
        # PyWebView exposes every public member of js_api to JavaScript. Native
        # Window/WinForms objects must remain private; walking AccessibilityObject
        # recursively raises TYPE_E_CANTLOADLIBRARY on affected Windows systems.
        # The source build keeps the simulator for UI/protocol development.
        # A frozen Windows release is a field tool and only exposes real PCAN.
        self._service = CanService(allow_simulation=not getattr(sys, "frozen", False))
        # The engineering tools have their own transport lifetime.  This
        # lets the operator keep the BMS monitor on CAN1 while the bench
        # sender, the IVT configurator, or the fan tool uses its own PCAN handle.
        self._bench_service = CanService(allow_simulation=False)
        self._ivt_service = CanService(allow_simulation=False)
        # The vehicle connection is a second independent CANB channel.  It
        # feeds the vehicle pages and the quick-value strip while the main
        # BMS connection stays on CAN1 for parameter work.
        self._vehicle_service = CanService(protocol_kind="vehicle",
                                           allow_simulation=not getattr(sys, "frozen", False))
        # MQTT telemetry is a fifth independent receive-only connection.  It
        # never changes a CAN mode and has no publish/command API.
        self._telemetry_service = TelemetryService()
        self._updater = HostUpdater(current_version=__version__, token_provider=self._read_update_token)
        self._updater_auto_checked = False
        self._window: Any = None

    def _read_update_token(self) -> str | None:
        try:
            return _read_settings().get("github_token") or None
        except Exception:
            return None

    def bootstrap(self) -> dict[str, Any]:
        profiles = [
            {"key": "can1", "name": "CAN1 · F405 / 从控 / IVT / 工具", "bitrate": 500000,
             "writable": True, "ivt_writable": True},
            {"key": "canb", "name": "CANB · ECU / Chroma · 500 kbit/s", "bitrate": 500000,
             "writable": False},
            {"key": "canb_legacy", "name": "CANB · Legacy · 250 kbit/s", "bitrate": 250000,
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
            "updater_enabled": install_ready(),
            "updater_repo": DEFAULT_REPO,
            "updater_has_token": self._updater.has_token(),
            "updater_settings_path": str(settings_path()),
            "channels": [f"PCAN_USBBUS{i}" for i in range(1, 9)],
            "profiles": profiles,
        }

    def get_updater_status(self) -> dict[str, Any]:
        status = self._updater.status()
        status["has_token"] = self._updater.has_token()
        status["install_supported"] = install_ready()
        status["auto_checked"] = self._updater_auto_checked
        return status

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
        app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
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

    def save_update_token(self, token: str) -> dict[str, Any]:
        return self._updater.set_token(token)

    def clear_update_token(self) -> dict[str, Any]:
        return self._updater.clear_token()

    def connect_can(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._service.connect(config)

    def disconnect_can(self) -> dict[str, Any]:
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
        profile = "can1"
        bitrate = int(config.get("bitrate") or 500000)
        return self._ivt_service.connect({
            "mode": "pcan", "bus_profile": profile,
            "channel": config.get("channel"), "bitrate": bitrate,
        })

    def disconnect_ivt(self) -> dict[str, Any]:
        return self._ivt_service.disconnect()

    def get_ivt_snapshot(self) -> dict[str, Any]:
        return self._ivt_service.ivt_snapshot()

    def connect_vehicle(self, config: dict[str, Any]) -> dict[str, Any]:
        mode = "simulation" if config.get("mode") == "simulation" else "pcan"
        profile = str(config.get("bus_profile") or "canb")
        bitrate = int(config.get("bitrate") or (250000 if profile == "canb_legacy" else 500000))
        return self._vehicle_service.connect({
            "mode": mode, "bus_profile": profile,
            "channel": config.get("channel"), "bitrate": bitrate,
        })

    def disconnect_vehicle(self) -> dict[str, Any]:
        return self._vehicle_service.disconnect()

    def get_vehicle_snapshot(self) -> dict[str, Any]:
        return self._vehicle_service.vehicle_snapshot()

    def get_quick_snapshot(self) -> dict[str, Any]:
        return {"vehicle": self._vehicle_service.quick_snapshot()}

    def connect_telemetry(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._telemetry_service.connect(config)

    def disconnect_telemetry(self) -> dict[str, Any]:
        return self._telemetry_service.disconnect()

    def get_telemetry_snapshot(self) -> dict[str, Any]:
        return self._telemetry_service.snapshot()

    def send_fan_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._vehicle_service.send_fan_command(name, values, acknowledged)

    def send_battery_fan_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._vehicle_service.send_battery_fan_command(name, values, acknowledged)

    def start_fan_calibration(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        opts = options or {}
        channel = int(opts.get("channel", 1))
        steps = opts.get("steps")
        hold_s = float(opts.get("hold_s", 6.0))
        max_current_a = float(opts.get("max_current_a", 18.0))
        tier = str(opts.get("tier", "dcdc"))
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

    def start_battery_fan_calibration(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        opts = options or {}
        return self._vehicle_service.start_battery_fan_calibration(
            opts.get("steps"), float(opts.get("hold_s", 5.0)),
            float(opts.get("max_current_a", 18.0)))

    def stop_battery_fan_calibration(self) -> dict[str, Any]:
        return self._vehicle_service.stop_battery_fan_calibration()

    def export_battery_fan_calibration(self) -> dict[str, Any]:
        return self._vehicle_service.export_battery_fan_calibration()

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

    def switch_ivt_bitrate(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ivt_service.switch_ivt_bitrate(options)

    def bench_command(self, command: str) -> dict[str, Any]:
        return self._bench_service.bench_command(command)

    def choose_record_file(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            default = f"BMS_CAN_{datetime.now():%Y%m%d_%H%M%S}.bmslog"
            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("BMS 数据记录 (*.bmslog)", "CSV 文件 (*.csv)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self._service.start_recording(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_recording(self) -> dict[str, Any]:
        return self._service.stop_recording()

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


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit("缺少 pywebview。请先执行：pip install -r requirements.txt")
    api = Api()
    window = webview.create_window(
        "BITFSAE · CAN HOST", url=(WEB_DIR / "index.html").as_uri(), js_api=api,
        width=1460, height=920, min_size=(1120, 720), background_color="#0D0E0F",
        zoomable=True,
    )
    api._window = window
    debug = "--debug" in sys.argv
    try:
        webview.start(debug=debug)
    finally:
        api.close()


if __name__ == "__main__":
    main()
