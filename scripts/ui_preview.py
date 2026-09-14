"""在没有 PCAN 硬件时，用真实协议数据在浏览器里审阅界面排版。

脚本把 canhost 包内确定性模拟器产出的快照写成 JSON，复制一份 `canhost/web`
并在 `core.js` 之前注入 `window.pywebview` 桩，然后用本地 HTTP 服务打开页面。
数据字段、单位和新鲜度判定与桌面版完全一致，因此页面代码原样运行，可以直接
用来核对间距、字号、换行、过期配色和控件宽度。

用法：

    .venv-canhost/bin/python scripts/ui_preview.py [--port 8801]

浏览器打开 http://127.0.0.1:8801/index.html?mock=live，`mock` 可取：

    live   已连接实体 CANB 的赛场状态（默认，数据新鲜）
    stale  风扇/PDM 数据超出新鲜窗口，用于核对过期配色
    calib  整车标定进行中、电池箱风扇标定已完成
    sim    内置模拟通道（只能读、不能下发），用于核对写入锁定与横幅

只用于界面审阅：数据是模拟值，不代表任何真实测量。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canhost.app import Api  # noqa: E402

VARIANTS = ("live", "stale", "calib", "sim")

# 只桩接 pywebview：未列出的方法一律返回 {ok: True}，界面新增调用不会让预览报错。
STUB_JS = """/* 预览桩：代替 pywebview 的 Python 桥。 */
(function () {
  // 页面脚本的错误不会进控制台之外的日志，集中收在 __previewErrors 里便于核对。
  window.__previewErrors = [];
  window.addEventListener("error", event => window.__previewErrors.push(String(event.message)));
  window.addEventListener("unhandledrejection",
    event => window.__previewErrors.push("rejection: " + String(event.reason)));
  const variant = new URLSearchParams(location.search).get("mock") || "live";
  let data = null;
  const clone = value => JSON.parse(JSON.stringify(value));
  async function load() {
    if (!data) data = await (await fetch(`mock-${variant}.json`)).json();
    return data;
  }
  const ok = async () => ({ ok: true });
  const base = {
    bootstrap: async () => clone((await load()).bootstrap),
    refresh_pcan_channels: async () => clone((await load()).bootstrap.pcan_scan),
    get_snapshot: async () => clone((await load()).snapshot),
    get_vehicle_snapshot: async () => clone((await load()).vehicle),
    get_quick_snapshot: async () => clone((await load()).quick),
    get_bench_snapshot: ok,
    get_ivt_snapshot: ok,
    get_telemetry_snapshot: ok,
    get_telemetry_simulator_snapshot: async () => ({ running: false }),
    mark_frontend_ready: async () => ({ required: false, ok: true }),
    startup_update_state: async () => ({ upgraded: false }),
    get_updater_status: async () => ({ enabled: false }),
    auto_check_for_updates: async () => ({ ok: true, skipped: true }),
  };
  window.pywebview = { api: new Proxy(base, {
    // then/catch/finally 必须返回 undefined：否则 api 会被当成 thenable，
    // async 函数返回它时永远不会落定，页面会停在“等待后端”而不报错。
    get: (target, key) => {
      if (typeof key !== "string") return undefined;
      if (key in target) return target[key];
      return (key === "then" || key === "catch" || key === "finally") ? undefined : ok;
    },
  }) };
})();
"""


def collect(api: Api) -> dict:
    """等模拟器同时产出 BMS 与整车数据，再抓一份完整快照。"""
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        vehicle = api.get_vehicle_snapshot()
        if (api.get_snapshot()["overview"].get("voltage_v") is not None
                and vehicle["pack"].get("voltage_v") is not None
                and vehicle["fan"].get("status") and vehicle["fan"].get("calib_limits")):
            break
        time.sleep(0.05)
    else:
        raise SystemExit("模拟数据未就绪：请确认源码运行环境完整")
    time.sleep(0.7)  # 让最后一个慢周期帧落地
    return {
        "bootstrap": api.bootstrap(),
        "snapshot": api.get_snapshot(),
        "vehicle": api.get_vehicle_snapshot(),
        "quick": api.get_quick_snapshot(),
    }


def freeze_ages(data: dict) -> None:
    """把所有数据年龄钉在新鲜窗口内，渲染成“有数据且新鲜”的常态。"""
    vehicle = data["vehicle"]
    vehicle["pack"]["age"] = 0.2
    vehicle["fault"]["age"] = 0.2
    vehicle["tires"]["age"] = 0.3
    for side in vehicle["pdm"]:
        vehicle["pdm"][side]["age"] = 0.3
    for channel in vehicle["meter"].values():
        if isinstance(channel, dict):
            channel["age"] = 0.3
    for key in list(vehicle["sop"]):
        if key.endswith("_age"):
            vehicle["sop"][key] = 0.2
    for key in vehicle["ecu"].get("age", {}):
        vehicle["ecu"]["age"][key] = 0.2
    for key in ("status_age", "diagnostic_age", "curve_age", "failsafe_age",
                "power_status_age", "calib_status_age", "calib_limits_age"):
        vehicle["fan"][key] = 0.3
    vehicle["battery_fan"]["status_age"] = 0.4
    vehicle["battery_fan"]["calibration_age"] = 0.4
    quick = data["quick"]["vehicle"]
    quick["fan_age"] = 0.3
    quick["pack"]["age"] = 0.2
    quick["sop"]["age"] = 0.2
    quick["pdm"]["age"] = 0.3


def as_field_connection(data: dict) -> None:
    """把模拟数据描述成已连接的实体 CANB，这是维修区的真实状态。"""
    data["vehicle"]["connection"].update({
        "connected": True, "mode": "pcan", "bus_profile": "canb", "bitrate": 500000,
        "channel": "PCAN_USBBUS2", "status": "已连接", "error": None, "last_rx_age": 0.1,
    })
    data["snapshot"]["connection"].update({
        "connected": True, "mode": "pcan", "bus_profile": "can1", "bitrate": 500000,
        "channel": "PCAN_USBBUS1", "status": "已连接", "last_rx_age": 0.1,
    })
    data["quick"]["vehicle"]["connection"] = data["vehicle"]["connection"]


def add_ack_history(data: dict) -> None:
    """0x5A5 回执不在模拟器里产生，补两条与解码结果同构的记录。"""
    data["vehicle"]["fan"]["ack_history"] = [
        {"time": "11:42:07.318", "opcode_name": "模式命令", "sequence": 12, "result": 0,
         "result_name": "已接受", "mode_name": "手动", "failsafe_name": "固定保底",
         "accepted": True, "duty_pct": [42, 38], "target_pct": [42, 38]},
        {"time": "11:41:36.902", "opcode_name": "温控曲线", "sequence": 9, "result": 3,
         "result_name": "标定进行中", "mode_name": "自动", "failsafe_name": "保持最后",
         "accepted": False, "duty_pct": [30, 30], "target_pct": [30, 30]},
    ]


def apply_calib(data: dict) -> None:
    """整车标定进行中：页面进入记录最多、按钮状态最复杂的分支。"""
    fan = data["vehicle"]["fan"]
    fan["calib_session"] = {
        "status": "running", "abort_reason": "", "channel": 1, "tier": "dcdc",
        "current_step": 7, "total_steps": 12, "current_duty": [62, 0],
        "suggested_caps": {"battery_cap_pct": None, "dcdc_cap_pct": 62},
        "channel_caps": {"battery": {"1": 58, "2": 61}, "dcdc": {"1": 62, "2": None}},
        "baseline": {"voltage_v": 23.9, "current_a": 8.4, "power_w": 201},
        "baseline_id": "dcdc-3", "run_params": {"hold_s": 6.0, "max_current_a": 18.0},
        "records": [
            {"step": 1, "channel": 1, "tier": "dcdc", "duty1_pct": 20, "duty2_pct": 0,
             "rpm1": 1620, "rpm2": 1688, "rpm3": 0, "voltage_v": 23.9, "current_a": 9.1,
             "power_w": 218, "delta_current_a": 0.7, "delta_power_w": 17},
            {"step": 2, "channel": 1, "tier": "dcdc", "duty1_pct": 32, "duty2_pct": 0,
             "rpm1": 2180, "rpm2": 2244, "rpm3": 0, "voltage_v": 23.8, "current_a": 10.4,
             "power_w": 248, "delta_current_a": 2.0, "delta_power_w": 47},
            {"step": 3, "channel": 1, "tier": "dcdc", "duty1_pct": 44, "duty2_pct": 0,
             "rpm1": 2610, "rpm2": 2688, "rpm3": 0, "voltage_v": 23.8, "current_a": 11.8,
             "power_w": 281, "delta_current_a": 3.4, "delta_power_w": 80},
        ],
    }
    fan["calib_status"] = {"calib_state": 1, "calib_state_name": "运行中",
                           "calib_abort_reason": 0, "calib_abort_name": "无", "step": 7,
                           "calib_target_pct": [62, 0], "lease_remaining_s": 4,
                           "param_version": 3, "flags": 0}
    battery = data["vehicle"]["battery_fan"]
    battery["calib_session"] = {
        "status": "completed", "current_step": 12, "total_steps": 12,
        "records": [{"step": step} for step in range(1, 13)],
        "suggested_caps": {"chroma_cap_pct": 48, "hv_cap_pct": 52},
    }


def apply_stale(data: dict) -> None:
    """风扇与 PDM 数据超出新鲜窗口：核对过期配色与“上次值”表述。"""
    fan = data["vehicle"]["fan"]
    for key in ("status_age", "diagnostic_age", "curve_age", "failsafe_age", "power_status_age"):
        fan[key] = 6.0
    fan["calib_limits_age"] = 9.0
    fan["calib_status_age"] = 9.0
    data["vehicle"]["battery_fan"]["status_age"] = 6.0
    data["vehicle"]["battery_fan"]["calibration_age"] = 6.0


def write_variants(out_dir: Path) -> None:
    api = Api()
    try:
        api.connect_can({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})
        api.connect_vehicle({"mode": "simulation", "bus_profile": "canb", "bitrate": 500000})
        data = collect(api)
    finally:
        api.close()

    freeze_ages(data)
    add_ack_history(data)
    # 先落 sim：保留内置模拟连接，页面进入“只能读、不能下发”的锁定分支。
    (out_dir / "mock-sim.json").write_text(json.dumps(data, ensure_ascii=False))

    as_field_connection(data)
    (out_dir / "mock-live.json").write_text(json.dumps(data, ensure_ascii=False))

    stale = json.loads(json.dumps(data))
    apply_stale(stale)
    (out_dir / "mock-stale.json").write_text(json.dumps(stale, ensure_ascii=False))

    calib = json.loads(json.dumps(data))
    apply_calib(calib)
    (out_dir / "mock-calib.json").write_text(json.dumps(calib, ensure_ascii=False))


def build_page(out_dir: Path) -> None:
    web = ROOT / "canhost" / "web"
    for name in ("js", "assets"):
        shutil.copytree(web / name, out_dir / name, dirs_exist_ok=True)
    shutil.copy2(web / "styles.css", out_dir / "styles.css")
    html = (web / "index.html").read_text(encoding="utf-8")
    marker = '  <script src="js/core.js"></script>'
    if marker not in html:
        raise SystemExit("index.html 缺少 core.js 引入，预览桩无法注入")
    (out_dir / "index.html").write_text(html.replace(marker, "  <script src=\"__stub.js\"></script>\n" + marker, 1),
                                       encoding="utf-8")
    (out_dir / "__stub.js").write_text(STUB_JS, encoding="utf-8")


class PreviewHandler(SimpleHTTPRequestHandler):
    """预览用的静态服务：禁用缓存，改完前端刷新即生效；不打印每条请求。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="用模拟数据在浏览器里预览界面")
    parser.add_argument("--port", type=int, default=8812)
    parser.add_argument("--dir", type=Path, default=None, help="输出目录（默认临时目录）")
    args = parser.parse_args()

    out_dir = args.dir or Path(tempfile.mkdtemp(prefix="canhost-ui-preview-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    build_page(out_dir)
    write_variants(out_dir)
    for variant in VARIANTS:
        assert (out_dir / f"mock-{variant}.json").is_file()

    handler = partial(PreviewHandler, directory=str(out_dir))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as error:
        raise SystemExit(f"端口 {args.port} 不可用（{error}）；用 --port 换一个端口")
    with server:
        print(f"预览已就绪：http://127.0.0.1:{args.port}/index.html?mock=live")
        print("变体：" + " / ".join(f"{name}（?mock={name}）" for name in VARIANTS))
        print(f"页面来自 {out_dir}；数据为模拟值；Ctrl+C 结束。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")


if __name__ == "__main__":
    main()
