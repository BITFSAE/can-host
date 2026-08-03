"""PyWebView application shell and JavaScript API."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import sys
from typing import Any

from . import __version__, __version_date__
from .can_service import CanService
from .protocol import switch_catalog


WEB_DIR = Path(__file__).parent / "web"


class Api:
    def __init__(self) -> None:
        # PyWebView exposes every public member of js_api to JavaScript. Native
        # Window/WinForms objects must remain private; walking AccessibilityObject
        # recursively raises TYPE_E_CANTLOADLIBRARY on affected Windows systems.
        # The source build keeps the simulator for UI/protocol development.
        # A frozen Windows release is a field tool and only exposes real PCAN.
        self._service = CanService(allow_simulation=not getattr(sys, "frozen", False))
        self._window: Any = None

    def bootstrap(self) -> dict[str, Any]:
        return {
            "version": __version__, "version_date": __version_date__, "switch_catalog": switch_catalog(),
            "simulation_enabled": self._service.allow_simulation,
            "channels": [f"PCAN_USBBUS{i}" for i in range(1, 9)],
            "profiles": [
                {"key": "can1", "name": "CAN1 · 主控 / 从控 / 工具", "bitrate": 500000, "writable": True},
                {"key": "canb", "name": "CANB · IVT / ECU / Chroma", "bitrate": 500000, "writable": False},
                {"key": "canb_legacy", "name": "CANB · Legacy 充电", "bitrate": 250000, "writable": False},
            ],
        }

    def connect_can(self, config: dict[str, Any]) -> dict[str, Any]:
        return self._service.connect(config)

    def disconnect_can(self) -> dict[str, Any]:
        return self._service.disconnect()

    def get_snapshot(self) -> dict[str, Any]:
        return self._service.snapshot()

    def send_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self._service.send_command(name, values, acknowledged)

    def read_flash_fault_logs(self, limit: int = 50) -> dict[str, Any]:
        return self._service.read_flash_fault_logs(limit)

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


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit("缺少 pywebview。请先执行：pip install -r TOOLS/bms_host/requirements.txt")
    api = Api()
    window = webview.create_window(
        "BITFSAE · BMS Control Desk", url=(WEB_DIR / "index.html").as_uri(), js_api=api,
        width=1460, height=920, min_size=(1120, 720), background_color="#0B0F12",
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
