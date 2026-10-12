"""Lifetime, scheduling and safety of the driver-display bench.

One PCAN handle on the display's CANB side carries the simulated nodes; an
optional second handle feeds the display's steering-wheel input (its CAN2).
The bench is an engineering tool for a bench with the display on it, so three
rules protect a real vehicle bus:

* connecting needs an explicit confirmation that the bus is a test bus;
* before the first transmission the bus is listened to, and a frame carrying an
  ID the bench is about to send aborts the connection;
* while running, a foreign frame with an ID the bench is sending pauses all
  transmission and reports the conflict.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime
import threading
import time
from typing import Any

from .. import decoders
from .cycle import DriveCycle
from .wheel_check import WheelObserver
from .model import (
    DEFECT_NAMES, DISPLAY_TIMEOUT_MS, ECU_PERIODS_MS, FRAME_BY_ID, FRAMES, FRAMES_BY_NODE, NODES,
    NODE_LABELS, SCENARIOS, build_frame, cycle_drives, default_controls, default_values, derive,
    frame_catalog, frame_period_ms, node_catalog, normalize_value, scenario_catalog, value_catalog,
)

try:
    import can
except ImportError:  # pragma: no cover - python-can ships with every supported install
    can = None  # type: ignore[assignment]

BITRATE = 500_000
TX_TICK_S = 0.002
CYCLE_TICK_S = 0.05
PROBE_S = 1.0
ONLINE_WINDOW_S = 1.0
ERROR_WINDOW_S = 2.0
RATE_WINDOW_S = 1.0
EVENT_LIMIT = 40

MODE_NAMES = {0: "S 直线", 1: "Q 避障", 2: "C 八字", 3: "E 耐久"}
WHEEL_FRAME_NAMES = {decoders.DRIVE_MODE_REQUEST_ID: "驾驶模式请求", decoders.SLIP_LEVEL_ID: "滑级",
                     decoders.LAP_TOGGLE_ID: "圈速计时键", decoders.ERROR_CLEAR_ID: "消错键"}
# Display-originated frames: id -> (snapshot key, decoder, label)
_DISPLAY_RX = {
    decoders.DISPLAY_SPEED_ID: ("speed", decoders.decode_display_speed),
    decoders.DISPLAY_POSITION_ID: ("position", decoders.decode_display_position),
    decoders.DISPLAY_MOTION_ID: ("motion", decoders.decode_display_motion),
    decoders.DISPLAY_STATUS_ID: ("status", decoders.decode_display_status),
    decoders.DISPLAY_LAP_ID: ("lap", decoders.decode_display_lap),
    decoders.STEERING_COMMAND_ID: ("steering", decoders.decode_steering_command),
    decoders.DRIVE_MODE_REQUEST_ID: ("mode_request", decoders.decode_drive_mode_request),
}
# The five frames the display always sends on its own prove that it is alive.
_ALIVE_IDS = frozenset((decoders.DISPLAY_SPEED_ID, decoders.DISPLAY_POSITION_ID, decoders.DISPLAY_MOTION_ID,
                        decoders.DISPLAY_STATUS_ID, decoders.DISPLAY_LAP_ID))


class _PreviewBus:
    """Stands in for PCAN in source-run previews: accepts frames, receives nothing."""

    def send(self, message: Any, timeout: float | None = None) -> None:
        return None

    def recv(self, timeout: float | None = None) -> None:
        time.sleep(min(timeout if timeout is not None else 0.1, 0.1))

    def shutdown(self) -> None:
        return None


class _FrameStats:
    __slots__ = ("count", "payload", "sent_at", "hz", "window_start", "window_count")

    def __init__(self) -> None:
        self.count = 0
        self.payload = b""
        self.sent_at: float | None = None
        self.hz = 0.0
        self.window_start: float | None = None
        self.window_count = 0


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


class DisplayBenchService:
    def __init__(self, *, allow_preview: bool = False,
                 bus_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 probe_s: float = PROBE_S) -> None:
        self.allow_preview = allow_preview
        self._bus_factory = bus_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._probe_s = probe_s
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()
        self._values = default_values()
        self._controls = default_controls()
        self._connect_nodes: list[str] = list(NODES)
        self._state = "disconnected"
        self._mode = "pcan"
        self._error: str | None = None
        self._channel = ""
        self._wheel_channel = ""
        self._bus: Any = None
        self._wheel_bus: Any = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._generation = 0
        self._cycle: DriveCycle | None = None
        self._started_at: float | None = None
        self._reset_counters()

    # -- counters ---------------------------------------------------------

    def _reset_counters(self) -> None:
        self._stats = {spec.frame_id: _FrameStats() for spec in FRAMES}
        self._tx_total = 0
        self._tx_errors = 0
        self._tx_error_times: deque[float] = deque(maxlen=64)
        self._tx_last_error: str | None = None
        self._tx_window: tuple[float, int] | None = None
        self._tx_hz = 0.0
        self._late = 0
        self._rx_total = 0
        self._rx_other = 0
        self._wheel_tx = 0
        self._display: dict[str, dict[str, Any]] = {}
        self._display_seen: float | None = None
        self._events: deque[dict[str, str]] = deque(maxlen=EVENT_LIMIT)
        self._conflict: dict[str, Any] | None = None
        self._wheel_observer = WheelObserver(wall_clock=self._wall_clock)

    def _log(self, kind: str, text: str) -> None:
        stamp = datetime.fromtimestamp(self._wall_clock()).strftime("%H:%M:%S.%f")[:-3]
        with self._lock:
            self._events.appendleft({"time": stamp, "kind": kind, "text": text})

    # -- connection -------------------------------------------------------

    def _open_bus(self, channel: str) -> Any:
        if self._bus_factory is not None:
            return self._bus_factory(channel)
        if can is None:
            raise RuntimeError("缺少 python-can，请重新安装上位机依赖")
        return can.Bus(interface="pcan", channel=channel, bitrate=BITRATE, receive_own_messages=False)

    def _parse_connect(self, config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("连接参数必须是对象")
        preview = bool(config.get("preview", False))
        if preview and not self.allow_preview:
            raise ValueError("发布版本只支持连接真实 PCAN，不提供无硬件预览")
        if not preview and not config.get("confirmed"):
            raise ValueError("请先确认所选通道只接车手屏测试台，没有接入实车 CANB")
        channel = str(config.get("channel") or "").strip()
        wheel_channel = str(config.get("wheel_channel") or "").strip()
        if not preview and not channel:
            raise ValueError("请选择车手屏所在的 PCAN 通道")
        if wheel_channel and wheel_channel == channel:
            raise ValueError("方向盘通道不能与车手屏 CANB 通道相同")
        nodes = config.get("nodes")
        nodes = list(NODES) if nodes is None else [str(node) for node in nodes]
        unknown = [node for node in nodes if node not in NODES]
        if unknown or not nodes:
            raise ValueError("请至少选择一个要模拟的节点")
        return {"preview": preview, "channel": channel, "wheel_channel": wheel_channel, "nodes": nodes}

    def _probe(self, bus: Any, ids: set[int]) -> list[int]:
        """Listen for ``self._probe_s`` and return IDs from ``ids`` that other nodes already send."""
        seen: set[int] = set()
        deadline = self._clock() + self._probe_s
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            message = bus.recv(timeout=min(0.05, remaining))
            if (message is not None and not message.is_extended_id
                    and getattr(message, "is_rx", True) and message.arbitration_id in ids):
                seen.add(int(message.arbitration_id))
        return sorted(seen)

    def connect(self, config: dict[str, Any]) -> dict[str, Any]:
        with self._op_lock:
            self._teardown()
            try:
                settings = self._parse_connect(config)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            with self._lock:
                self._state = "connecting"
                self._error = None
            bus = wheel_bus = None
            try:
                if settings["preview"]:
                    bus = _PreviewBus()
                else:
                    try:
                        bus = self._open_bus(settings["channel"])
                    except Exception as exc:
                        raise RuntimeError(f"无法打开 {settings['channel']}：{exc}") from exc
                    ids = {spec.frame_id for node in settings["nodes"] for spec in FRAMES_BY_NODE[node]}
                    found = self._probe(bus, ids)
                    if found:
                        names = "、".join(f"0x{value:03X}" for value in found[:6])
                        raise RuntimeError(
                            f"总线上已有其他节点发送 {names}，疑似接入实车或仍有真实节点。"
                            "已取消连接；若要与真实节点混用，请在连接设置里取消勾选对应节点")
                if settings["preview"]:
                    wheel_bus = _PreviewBus()
                elif settings["wheel_channel"]:
                    try:
                        wheel_bus = self._open_bus(settings["wheel_channel"])
                    except Exception as exc:
                        raise RuntimeError(f"无法打开方向盘通道 {settings['wheel_channel']}：{exc}") from exc
            except Exception as exc:
                for opened in (bus, wheel_bus):
                    self._shutdown_bus(opened)
                with self._lock:
                    self._state = "disconnected"
                    self._error = str(exc)
                return {"ok": False, "error": str(exc)}

            with self._lock:
                self._reset_counters()
                self._generation += 1
                generation = self._generation
                self._stop = threading.Event()
                stop = self._stop
                self._bus, self._wheel_bus = bus, wheel_bus
                self._mode = "preview" if settings["preview"] else "pcan"
                self._channel = settings["channel"]
                self._wheel_channel = settings["wheel_channel"]
                self._connect_nodes = settings["nodes"]
                self._controls["nodes"] = {node: node in settings["nodes"] for node in NODES}
                self._controls["paused"] = False
                self._controls["run"] = "manual"
                self._cycle = None
                self._started_at = self._clock()
                self._state = "running"
                self._threads = [
                    threading.Thread(target=self._tx_loop, args=(generation, stop, bus),
                                     name="display-bench-tx", daemon=True),
                    threading.Thread(target=self._rx_loop, args=(generation, stop, bus),
                                     name="display-bench-rx", daemon=True),
                ]
                for thread in self._threads:
                    thread.start()
            return {"ok": True, "message": "车手屏联调已连接" + ("（预览，不发送）" if settings["preview"] else "")}

    @staticmethod
    def _shutdown_bus(bus: Any) -> None:
        if bus is None:
            return
        try:
            bus.shutdown()
        except Exception:
            pass

    def _teardown(self) -> None:
        with self._lock:
            threads = self._threads
            self._threads = []
            self._stop.set()
            self._generation += 1
            bus, wheel_bus = self._bus, self._wheel_bus
            self._bus = self._wheel_bus = None
            self._state = "disconnected"
            self._cycle = None
            self._controls["run"] = "manual"
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._shutdown_bus(bus)
        self._shutdown_bus(wheel_bus)

    def disconnect(self) -> dict[str, Any]:
        with self._op_lock:
            self._teardown()
            with self._lock:
                self._error = None
        return {"ok": True}

    close = disconnect

    def _fail(self, generation: int, message: str) -> None:
        """Stop sending after a bus failure; resources are released by disconnect()."""
        with self._lock:
            if generation != self._generation or self._state != "running":
                return
            self._state = "error"
            self._error = message
            self._stop.set()

    # -- transmit ---------------------------------------------------------

    def _tx_loop(self, generation: int, stop: threading.Event, bus: Any) -> None:
        try:
            self._tx_run(generation, stop, bus)
        except Exception as exc:  # a builder or model bug must not leave the page showing "发送中"
            self._fail(generation, f"发送线程异常停止：{exc}")

    def _tx_run(self, generation: int, stop: threading.Event, bus: Any) -> None:
        clock = self._clock
        due: dict[int, float] = {}
        cycle_at = cycle_prev = clock()
        # time.sleep rather than Event.wait: since Python 3.11 it uses a high-resolution timer on
        # Windows, where Event.wait would round the 2 ms tick up to the 15.6 ms system tick and
        # cut the 10 ms ECU frames to about 64 Hz.
        while not stop.is_set():
            time.sleep(TX_TICK_S)
            now = clock()
            plan: list[tuple[Any, bytes]] = []
            with self._lock:
                if generation != self._generation:
                    return
                if self._cycle is not None and now >= cycle_at:
                    step = min(now - cycle_prev, 0.25)
                    cycle_prev, cycle_at = now, now + CYCLE_TICK_S
                    self._values.update(self._cycle.step(step, self._values))
                controls = self._controls
                if controls["paused"]:
                    due.clear()
                    continue
                for spec in FRAMES:
                    if not (controls["nodes"][spec.node] and controls["frames"][spec.key]):
                        due.pop(spec.frame_id, None)
                        continue
                    scheduled = due.get(spec.frame_id, now)
                    if now < scheduled:
                        continue
                    period = frame_period_ms(spec, controls) / 1000.0
                    following = scheduled + period
                    if following <= now:
                        if now - scheduled > max(0.1, 3 * period):
                            self._late += 1
                        following = now + period
                    due[spec.frame_id] = following
                    plan.append((spec, build_frame(spec, self._values, self._stats[spec.frame_id].count,
                                                   controls["defects"][spec.key])))
            for spec, payload in plan:
                self._transmit(bus, spec, payload)

    def _transmit(self, bus: Any, spec: Any, payload: bytes) -> None:
        try:
            bus.send(can.Message(arbitration_id=spec.frame_id, data=payload, is_extended_id=False),
                     timeout=0.05)
        except Exception as exc:
            now = self._clock()
            with self._lock:
                self._tx_errors += 1
                self._tx_error_times.append(now)
                self._tx_last_error = str(exc)
            return
        with self._lock:
            stats = self._stats[spec.frame_id]
            stats.count += 1
            stats.payload = payload
            stats.sent_at = self._clock()
            self._tx_total += 1

    # -- receive ----------------------------------------------------------

    def _rx_loop(self, generation: int, stop: threading.Event, bus: Any) -> None:
        while not stop.is_set():
            try:
                message = bus.recv(timeout=0.1)
            except Exception as exc:
                self._fail(generation, f"PCAN 接收失败：{exc}")
                return
            if message is None or generation != self._generation:
                continue
            if (message.is_extended_id or getattr(message, "is_error_frame", False)
                    or not getattr(message, "is_rx", True)):
                continue
            try:
                self._handle_rx(int(message.arbitration_id), bytes(message.data))
            except Exception as exc:
                self._fail(generation, f"接收处理异常：{exc}")
                return

    def _handle_rx(self, frame_id: int, data: bytes) -> None:
        now = self._clock()
        entry = _DISPLAY_RX.get(frame_id)
        with self._lock:
            self._rx_total += 1
            spec = FRAME_BY_ID.get(frame_id)
            controls = self._controls
            if (spec is not None and not controls["paused"] and controls["nodes"][spec.node]
                    and controls["frames"][spec.key] and self._conflict is None):
                controls["paused"] = True
                self._conflict = {"id": spec.key, "name": spec.name, "node": NODE_LABELS[spec.node],
                                  "time": self._wall_clock()}
                text = f"检测到其他节点也在发送 {spec.key}，已暂停全部发送"
            else:
                text = ""
            if entry is None:
                if spec is None:
                    self._rx_other += 1
        if text:
            self._log("conflict", text)
        if entry is None:
            return
        key, decoder = entry
        if frame_id in (decoders.STEERING_COMMAND_ID, decoders.DRIVE_MODE_REQUEST_ID):
            with self._lock:
                self._wheel_observer.display_frame(now, frame_id, data)
        decoded = decoder(data)
        if decoded is None:
            with self._lock:
                self._rx_other += 1
            return
        with self._lock:
            previous = self._display.get(key)
            self._display[key] = {"data": decoded, "time": now, "count": (previous["count"] + 1) if previous else 1}
            if frame_id in _ALIVE_IDS:
                self._display_seen = now
        if key == "steering":
            parts = [f"滑级 {decoded['slip_level']}"]
            parts += ["圈速计时键"] if decoded["lap_toggle"] else []
            parts += ["清错键"] if decoded["error_clear"] else []
            self._log("display", "车手屏发给 ECU 的方向盘指令（0x703）：" + " · ".join(parts))
        elif key == "mode_request":
            mode = decoded["mode"]
            self._log("display", "车手屏转发的驾驶模式请求（0x310）：" + (MODE_NAMES[mode] if mode is not None
                                                          else f"无效码 0x{decoded['code']:02X}"))
            if mode is not None:
                with self._lock:
                    follow = self._controls["ecu_follow_mode"] and self._controls["nodes"]["ecu"]
                    if follow:
                        self._values["ecu.mode"] = mode
                if follow:
                    self._log("ecu", f"模拟 ECU 收到转发的驾驶模式，0x509 回报改为 {MODE_NAMES[mode]}")

    # -- values and commands ---------------------------------------------

    def set_values(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            return {"ok": False, "error": "没有可写入的参数"}
        try:
            normalized = {key: normalize_value(key, raw) for key, raw in patch.items()}
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._cycle is not None and any(cycle_drives(key) for key in normalized):
                return {"ok": False, "error": "行驶循环运行中，该参数由循环控制；切换到手动后再修改"}
            self._values.update(normalized)
            return {"ok": True, "values": {key: self._values[key] for key in normalized}}

    def command(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        handler = self._COMMANDS.get(str(name))
        if handler is None:
            return {"ok": False, "error": f"未知命令：{name}"}
        try:
            return handler(self, args or {})
        except (KeyError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def _cmd_pause(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._controls["paused"] = bool(args.get("paused"))
        return {"ok": True}

    def _cmd_resume(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._conflict = None
            self._controls["paused"] = False
        return {"ok": True}

    def _cmd_node(self, args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node"))
        if node not in NODES:
            raise ValueError(f"未知节点：{node}")
        with self._lock:
            self._controls["nodes"][node] = bool(args.get("enabled"))
        return {"ok": True}

    def _frame_key(self, args: dict[str, Any]) -> str:
        raw = args.get("frame_id")
        key = f"0x{int(str(raw), 0):03X}"
        if int(key, 16) not in FRAME_BY_ID:
            raise ValueError(f"未知报文：{raw}")
        return key

    def _cmd_frame(self, args: dict[str, Any]) -> dict[str, Any]:
        key = self._frame_key(args)
        with self._lock:
            self._controls["frames"][key] = bool(args.get("enabled"))
        return {"ok": True}

    def _cmd_defect(self, args: dict[str, Any]) -> dict[str, Any]:
        key = self._frame_key(args)
        defect = str(args.get("defect", "none"))
        spec = FRAME_BY_ID[int(key, 16)]
        if defect != "none" and defect not in spec.defects:
            raise ValueError(f"{key} 不支持异常类型 {defect}")
        with self._lock:
            self._controls["defects"][key] = defect
        return {"ok": True}

    def _cmd_run(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = str(args.get("mode"))
        if mode not in {"manual", "cycle"}:
            raise ValueError("运行方式只能是手动或行驶循环")
        with self._lock:
            self._controls["run"] = mode
            self._cycle = DriveCycle(self._values) if mode == "cycle" else None
        return {"ok": True}

    def _cmd_ecu_period(self, args: dict[str, Any]) -> dict[str, Any]:
        period = int(args.get("ms"))
        if period not in ECU_PERIODS_MS:
            raise ValueError("ECU 周期只能是 " + "/".join(str(value) for value in ECU_PERIODS_MS) + " ms")
        with self._lock:
            self._controls["ecu_period_ms"] = period
        return {"ok": True}

    def _cmd_ecu_follow(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._controls["ecu_follow_mode"] = bool(args.get("enabled"))
        return {"ok": True}

    def _cmd_scenario(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name"))
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise ValueError(f"未知场景：{name}")
        with self._lock:
            self._values = default_values()
            self._values.update({key: normalize_value(key, raw) for key, raw in scenario.values.items()})
            self._cycle = None
            self._controls["run"] = "manual"
            self._controls["paused"] = scenario.paused
            self._conflict = None
        return {"ok": True, "message": f"已应用场景：{scenario.label}"}

    def _cmd_wheel_log_clear(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._wheel_observer.reset()
        return {"ok": True}

    def _cmd_reset(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._values = default_values()
            self._controls = default_controls()
            self._controls["nodes"] = {node: node in self._connect_nodes for node in NODES}
            self._cycle = None
            self._conflict = None
        return {"ok": True, "message": "已恢复默认值与全部开关"}

    # -- steering wheel (display CAN2) -----------------------------------

    def _send_wheel(self, frame_id: int, payload: bytes, text: str) -> dict[str, Any]:
        with self._lock:
            running = self._state == "running"
            preview = self._mode == "preview"
            bus = self._wheel_bus
        if not running:
            return {"ok": False, "error": "车手屏联调未连接"}
        if bus is None:
            return {"ok": False, "error": "未连接方向盘 CAN2 通道：断开后在连接设置里选择第二个 PCAN 通道"}
        if not preview:
            try:
                bus.send(can.Message(arbitration_id=frame_id, data=payload, is_extended_id=False), timeout=0.2)
            except Exception as exc:
                return {"ok": False, "error": f"方向盘帧 0x{frame_id:03X} 发送失败：{exc}"}
        with self._lock:
            self._wheel_tx += 1
        self._log("wheel", f"方向盘 → 车手屏 {WHEEL_FRAME_NAMES[frame_id]}（0x{frame_id:03X}）：{text}"
                  + ("（预览，未发送）" if preview else ""))
        return {"ok": True}

    def _wheel_mode(self, mode: int) -> dict[str, Any]:
        if not 0 <= mode <= 3:
            raise ValueError("驾驶模式只能是 0..3")
        result = self._send_wheel(decoders.DRIVE_MODE_REQUEST_ID,
                                  decoders.build_drive_mode_request(decoders.DRIVE_MODE_ASCII_ZERO + mode),
                                  f"驾驶模式 {MODE_NAMES[mode]}")
        if result["ok"]:
            with self._lock:
                self._values["wheelsw.mode"] = mode
        return result

    def _wheel_slip(self, level: int) -> dict[str, Any]:
        if not 0 <= level <= 7:
            raise ValueError("滑级只能是 0..7")
        result = self._send_wheel(decoders.SLIP_LEVEL_ID, decoders.build_slip_level(level), f"滑级 {level}")
        if result["ok"]:
            with self._lock:
                self._values["wheelsw.slip"] = level
        return result

    def _cmd_wheel_mode(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._wheel_mode(int(args.get("mode")))

    def _cmd_wheel_slip(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._wheel_slip(int(args.get("level")))

    def _cmd_wheel_lap(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._send_wheel(decoders.LAP_TOGGLE_ID, decoders.build_button_press(), "圈速计时键")

    def _cmd_wheel_clear(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._send_wheel(decoders.ERROR_CLEAR_ID, decoders.build_button_press(), "消错键")

    def _cmd_wheel_lap_double(self, args: dict[str, Any]) -> dict[str, Any]:
        """Two presses 100 ms apart: the display counts them as one (300 ms debounce)."""
        first = self._send_wheel(decoders.LAP_TOGGLE_ID, decoders.build_button_press(), "圈速计时键（连按 1/2）")
        if not first["ok"]:
            return first
        time.sleep(0.1)
        return self._send_wheel(decoders.LAP_TOGGLE_ID, decoders.build_button_press(), "圈速计时键（连按 2/2）")

    def _cmd_wheel_invalid(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind"))
        cases = {
            "mode_ff": (decoders.DRIVE_MODE_REQUEST_ID, decoders.build_drive_mode_request(0xFF),
                        "无效驾驶模式码 0xFF"),
            "mode_4": (decoders.DRIVE_MODE_REQUEST_ID, decoders.build_drive_mode_request(52),
                       "越界驾驶模式码 '4'"),
            "slip_8": (decoders.SLIP_LEVEL_ID, decoders.build_slip_level(8), "越界滑级 8"),
        }
        if kind not in cases:
            raise ValueError(f"未知异常输入：{kind}")
        return self._send_wheel(*cases[kind])

    def _cmd_wheel_powerup(self, args: dict[str, Any]) -> dict[str, Any]:
        """What the wheel board does at power-up: current slip level, then current mode."""
        with self._lock:
            slip, mode = self._values["wheelsw.slip"], self._values["wheelsw.mode"]
        if slip < 0 and mode < 0:
            return {"ok": False, "error": "还没有设置过方向盘滑级或驾驶模式，请先选择一项"}
        result = self._wheel_slip(slip) if slip >= 0 else {"ok": True}
        return result if not result["ok"] or mode < 0 else self._wheel_mode(mode)

    _COMMANDS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
        "pause": _cmd_pause, "resume": _cmd_resume, "node": _cmd_node, "frame": _cmd_frame,
        "defect": _cmd_defect, "run": _cmd_run, "ecu_period": _cmd_ecu_period,
        "ecu_follow": _cmd_ecu_follow, "scenario": _cmd_scenario, "reset": _cmd_reset,
        "wheel_mode": _cmd_wheel_mode, "wheel_slip": _cmd_wheel_slip, "wheel_lap": _cmd_wheel_lap,
        "wheel_clear": _cmd_wheel_clear, "wheel_lap_double": _cmd_wheel_lap_double,
        "wheel_invalid": _cmd_wheel_invalid, "wheel_powerup": _cmd_wheel_powerup,
        "wheel_log_clear": _cmd_wheel_log_clear,
    }

    # -- snapshot ---------------------------------------------------------

    def _roll_rates(self, now: float) -> None:
        for stats in self._stats.values():
            if stats.window_start is None:
                stats.window_start, stats.window_count = now, stats.count
            elif now - stats.window_start >= RATE_WINDOW_S:
                stats.hz = (stats.count - stats.window_count) / (now - stats.window_start)
                stats.window_start, stats.window_count = now, stats.count
        if self._tx_window is None:
            self._tx_window = (now, self._tx_total)
        elif now - self._tx_window[0] >= RATE_WINDOW_S:
            self._tx_hz = (self._tx_total - self._tx_window[1]) / (now - self._tx_window[0])
            self._tx_window = (now, self._tx_total)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            running = self._state == "running"
            self._roll_rates(now)
            controls = self._controls
            frames = []
            for spec in FRAMES:
                stats = self._stats[spec.frame_id]
                enabled = controls["nodes"][spec.node] and controls["frames"][spec.key]
                frames.append({
                    "id": spec.key, "name": spec.name, "node": spec.node,
                    "period_ms": frame_period_ms(spec, controls), "timeout_ms": DISPLAY_TIMEOUT_MS.get(spec.frame_id),
                    "enabled": enabled, "sending": running and enabled and not controls["paused"],
                    "count": stats.count, "hz": round(stats.hz, 1), "data": _hex(stats.payload),
                    "age": None if stats.sent_at is None else round(now - stats.sent_at, 2),
                    "defect": controls["defects"][spec.key],
                })
            display = {key: {"data": item["data"], "age": round(now - item["time"], 2), "count": item["count"]}
                       for key, item in self._display.items()}
            seen_age = None if self._display_seen is None else now - self._display_seen
            recent_errors = sum(1 for stamp in self._tx_error_times if now - stamp <= ERROR_WINDOW_S)
            conflict = dict(self._conflict) if self._conflict else None
            return {
                "connection": {
                    "state": self._state, "connected": running,
                    "active": self._state in {"running", "error"},
                    "mode": self._mode, "channel": self._channel, "wheel_channel": self._wheel_channel,
                    "wheel_connected": self._wheel_bus is not None, "error": self._error,
                    "age": None if self._started_at is None or not running else round(now - self._started_at, 1),
                },
                "values": dict(self._values),
                "derived": derive(self._values),
                "controls": {
                    "run": controls["run"], "paused": controls["paused"],
                    "ecu_period_ms": controls["ecu_period_ms"], "ecu_follow_mode": controls["ecu_follow_mode"],
                    "nodes": dict(controls["nodes"]), "frames": dict(controls["frames"]),
                    "defects": dict(controls["defects"]),
                },
                "frames": frames,
                "display": {
                    "online": seen_age is not None and seen_age <= ONLINE_WINDOW_S,
                    "age": None if seen_age is None else round(seen_age, 2), **display},
                "events": list(self._events),
                "wheel_observer": self._wheel_observer.snapshot(now),
                "stats": {
                    "tx_total": self._tx_total, "tx_hz": round(self._tx_hz, 1), "tx_errors": self._tx_errors,
                    "tx_recent_errors": recent_errors, "tx_last_error": self._tx_last_error,
                    "late": self._late, "rx_total": self._rx_total, "rx_other": self._rx_other,
                    "wheel_tx": self._wheel_tx,
                },
                "conflict": conflict,
                "cycle": None if self._cycle is None else {
                    "phase": self._cycle.phase, "t": round(self._cycle.t, 1),
                    "speed_kmh": round(self._cycle.speed_kmh, 1)},
            }


def bench_catalog() -> dict[str, Any]:
    """Static lists the page renders from (sent once in bootstrap)."""
    return {
        "scenarios": scenario_catalog(), "nodes": node_catalog(), "frames": frame_catalog(),
        "ecu_periods_ms": list(ECU_PERIODS_MS), "values": value_catalog(),
        "defects": dict(DEFECT_NAMES), "lap_diag": dict(decoders.LAP_DIAG_NAMES),
    }


__all__ = ["DisplayBenchService", "bench_catalog"]
