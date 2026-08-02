"""PyWebView application shell and JavaScript API."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import json
import sys
from typing import Any

from . import __version__
from .can_service import CanService
from .protocol import switch_catalog


WEB_DIR = Path(__file__).parent / "web"


def build_project_document(project: dict[str, Any]) -> dict[str, Any]:
    """Create the versioned, portable subset stored in a .bmsproj file."""
    return {
        "format": "BITFSAE_BMS_PROJECT", "schema_version": 1,
        "app_version": __version__, "exported_at": datetime.now().isoformat(timespec="seconds"),
        "name": str(project.get("name") or "未命名工程").strip()[:80],
        "notes": str(project.get("notes") or "")[:4000],
        "connection": project.get("connection") if isinstance(project.get("connection"), dict) else {},
        "parameters": project.get("parameters") if isinstance(project.get("parameters"), dict) else {},
        "view": project.get("view") if isinstance(project.get("view"), dict) else {},
    }


def validate_project_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("工程文件根字段必须是对象")
    if document.get("format") != "BITFSAE_BMS_PROJECT" or document.get("schema_version") != 1:
        raise ValueError("不是受支持的 BMS 工程文件")
    for key in ("connection", "parameters", "view"):
        if not isinstance(document.get(key, {}), dict):
            raise ValueError(f"工程文件字段 {key} 格式错误")
    return document


class Api:
    def __init__(self) -> None:
        self.service = CanService()
        self.window: Any = None

    def bootstrap(self) -> dict[str, Any]:
        return {
            "version": __version__, "switch_catalog": switch_catalog(),
            "channels": [f"PCAN_USBBUS{i}" for i in range(1, 9)],
            "profiles": [
                {"key": "can1", "name": "CAN1 · 主控 / 从控 / 工具", "bitrate": 500000, "writable": True},
                {"key": "canb", "name": "CANB · IVT / ECU / Chroma", "bitrate": 500000, "writable": False},
                {"key": "canb_legacy", "name": "CANB · Legacy 充电", "bitrate": 250000, "writable": False},
            ],
        }

    def connect_can(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.service.connect(config)

    def disconnect_can(self) -> dict[str, Any]:
        return self.service.disconnect()

    def get_snapshot(self) -> dict[str, Any]:
        return self.service.snapshot()

    def send_command(self, name: str, values: dict[str, Any], acknowledged: bool = False) -> dict[str, Any]:
        return self.service.send_command(name, values, acknowledged)

    def read_flash_fault_logs(self, limit: int = 50) -> dict[str, Any]:
        return self.service.read_flash_fault_logs(limit)

    def choose_record_file(self) -> dict[str, Any]:
        if not self.window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            default = f"BMS_CAN_{datetime.now():%Y%m%d_%H%M%S}.bmslog"
            selected = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("BMS 数据记录 (*.bmslog)", "CSV 文件 (*.csv)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self.service.start_recording(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_recording(self) -> dict[str, Any]:
        return self.service.stop_recording()

    def choose_replay_file(self) -> dict[str, Any]:
        if not self.window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("BMS 数据记录 (*.bmslog;*.csv)", "BMS 原生记录 (*.bmslog)", "CSV 文件 (*.csv)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected if isinstance(selected, str) else selected[0]
            return self.service.load_replay(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replay_control(self, action: str, value: float | None = None) -> dict[str, Any]:
        return self.service.replay_control(action, value)

    def export_project(self, project: dict[str, Any]) -> dict[str, Any]:
        if not self.window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            name = str(project.get("name") or "BMS_Project").strip()[:80]
            safe_name = "".join(char if char not in '<>:"/\\|?*' else "_" for char in name) or "BMS_Project"
            selected = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"{safe_name}.bmsproj",
                file_types=("BMS 工程文件 (*.bmsproj)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            target = Path(selected if isinstance(selected, str) else selected[0])
            if target.suffix.lower() != ".bmsproj":
                target = target.with_suffix(".bmsproj")
            document = build_project_document(project)
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_project(self) -> dict[str, Any]:
        if not self.window:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview
            selected = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("BMS 工程文件 (*.bmsproj)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            source = Path(selected if isinstance(selected, str) else selected[0])
            if source.stat().st_size > 1_000_000:
                raise ValueError("工程文件超过 1 MB，拒绝载入")
            document = validate_project_document(json.loads(source.read_text(encoding="utf-8")))
            return {"ok": True, "path": str(source), "project": document}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        self.service.disconnect()


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit("缺少 pywebview。请先执行：pip install -r TOOLS/bms_host/requirements.txt")
    api = Api()
    window = webview.create_window(
        "BITFSAE · BMS Control Desk", url=(WEB_DIR / "index.html").as_uri(), js_api=api,
        width=1460, height=920, min_size=(1120, 720), background_color="#0C1519",
    )
    api.window = window
    debug = "--debug" in sys.argv
    try:
        webview.start(debug=debug)
    finally:
        api.close()


if __name__ == "__main__":
    main()
