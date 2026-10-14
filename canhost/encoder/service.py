"""Lifetime, polling, commands and curves of the BRT encoder page.

One PCAN handle talks to one encoder.  The encoder only answers requests (query mode, the factory
behaviour) or streams its position (auto-report), so connecting sends nothing; frames leave only when
the operator asks for data, polls, or changes a setting.  Three tiers protect the device and the bus:

* reads and the poll/auto-report switches need no confirmation;
* anything that rewrites the encoder's stored settings (zero, midpoint, direction, period, address,
  bitrate, sample time, position count) is refused unless the caller passes ``acknowledged=True``;
* every setting write waits for the device's ``04 ADDR FUNC 00`` answer, and position-changing writes
  are read back and compared, so the page never reports a calibration that the device did not take.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime
import math
import threading
import time
from typing import Any

from .. import decoders as d
from .model import (
    DEG_PER_COUNT, RESOLUTION, MotionTracker, SampleSeries, Timebase, circular_distance, max_trackable_rpm,
    motion_label,
)
from .simulator import SimulatedEncoderBus

try:
    import can
except ImportError:  # pragma: no cover - python-can ships with every supported install
    can = None  # type: ignore[assignment]

POLL_RATES_HZ = (5, 10, 20, 50, 100, 200)
BITRATES = tuple(sorted(d.BRT_BITRATE_CODES))
ACK_TIMEOUT_S = 0.6
SETTLE_S = 0.03
ZERO_TOLERANCE = 3
STABILITY_WINDOW_S = 5.0
RATE_WINDOW_S = 1.0
FRESH_MAX_S = 1.5
EVENT_LIMIT = 60
MARKER_LIMIT = 30
CENSUS_LIMIT = 24
POLL_FAILURE_LIMIT = 10


class EncoderError(RuntimeError):
    """A request that cannot be carried out; the message is shown to the operator."""


class _Rx:
    __slots__ = ("can_id", "decoded", "arrival")

    def __init__(self, can_id: int, decoded: dict[str, Any], arrival: float) -> None:
        self.can_id = can_id
        self.decoded = decoded
        self.arrival = arrival


class _Waiter:
    __slots__ = ("accept", "event", "result")

    def __init__(self, accept: Callable[[_Rx], bool]) -> None:
        self.accept = accept
        self.event = threading.Event()
        self.result: _Rx | None = None


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def _as_int(args: dict[str, Any], key: str, label: str) -> int:
    value = args.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{label}格式无效")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            pass
    raise ValueError(f"{label}必须是整数")


class EncoderService:
    def __init__(self, *, allow_preview: bool = False, bus_factory: Callable[[str, int], Any] | None = None,
                 clock: Callable[[], float] = time.perf_counter, wall_clock: Callable[[], float] = time.time,
                 ack_timeout_s: float = ACK_TIMEOUT_S) -> None:
        # perf_counter, not monotonic: on Windows monotonic() ticks every 15.6 ms, which would cap the
        # poll scheduler near 64 Hz and quantise arrival times.
        self.allow_preview = allow_preview
        self._bus_factory = bus_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._ack_timeout_s = ack_timeout_s
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._state = "disconnected"
        self._mode = "pcan"
        self._error: str | None = None
        self._channel = ""
        self._bitrate = d.BRT_DEFAULT_BITRATE
        self._address = d.BRT_DEFAULT_ADDRESS
        self._bus: Any = None
        self._rx_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._stop = threading.Event()
        self._generation = 0
        self._started_at: float | None = None
        self._series = SampleSeries()
        self._reset_session()

    # -- session state ----------------------------------------------------

    def _reset_session(self) -> None:
        self._series.clear()
        self._tracker = MotionTracker()
        self._timebase = Timebase()
        self._last: dict[str, Any] | None = None
        self._last_direction: tuple[str, float] | None = None
        self._alt_ids: set[int] = set()
        self._waiters: list[_Waiter] = []
        self._census: dict[tuple[int, bool, int, int | None], dict[str, Any]] = {}
        self._events: deque[dict[str, str]] = deque(maxlen=EVENT_LIMIT)
        self._markers: deque[dict[str, Any]] = deque(maxlen=MARKER_LIMIT)
        self._applied: dict[str, dict[str, Any]] = {}
        self._device_values: dict[str, dict[str, Any]] = {}
        self._report_on = False
        self._poll_hz = 0
        self._pending_bitrate: int | None = None
        self._rx_total = 0
        self._rx_other = 0
        self._rejected = 0
        self._last_reject: str | None = None
        self._tx_total = 0
        self._tx_errors = 0
        self._last_match: float | None = None
        self._pos_count = 0
        self._win_start: float | None = None
        self._win_count = 0
        self._win_sum = 0.0
        self._win_n = 0
        self._win_max = 0.0
        self._prev_t: float | None = None
        self._hz = 0.0
        self._interval_ms: float | None = None
        self._max_gap_ms: float | None = None

    def _log(self, kind: str, text: str) -> None:
        stamp = datetime.fromtimestamp(self._wall_clock()).strftime("%H:%M:%S.%f")[:-3]
        with self._lock:
            self._events.appendleft({"time": stamp, "kind": kind, "text": text})

    def _mark(self, text: str) -> None:
        with self._lock:
            if self._last is not None:
                self._markers.append({"t": self._last["t"], "text": text})

    # -- connection -------------------------------------------------------

    def _open_bus(self, channel: str, bitrate: int) -> Any:
        if self._bus_factory is not None:
            return self._bus_factory(channel, bitrate)
        if can is None:
            raise RuntimeError("缺少 python-can，请重新安装上位机依赖")
        return can.Bus(interface="pcan", channel=channel, bitrate=bitrate, receive_own_messages=False)

    def _parse_connect(self, config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("连接参数必须是对象")
        preview = bool(config.get("preview", False))
        if preview and not self.allow_preview:
            raise ValueError("发布版本只支持连接真实 PCAN，不提供无硬件预览")
        channel = str(config.get("channel") or "").strip()
        if not preview and not channel:
            raise ValueError("请选择编码器所在的 PCAN 通道")
        bitrate = _as_int({"v": config.get("bitrate", d.BRT_DEFAULT_BITRATE)}, "v", "位率")
        if bitrate not in d.BRT_BITRATE_CODES:
            raise ValueError("编码器位率只支持 100 / 125 / 250 / 500 kbit/s 和 1 Mbit/s")
        address = _as_int({"v": config.get("address", d.BRT_DEFAULT_ADDRESS)}, "v", "节点地址")
        if not 1 <= address <= 0xFF:
            raise ValueError("节点地址必须是 0x01..0xFF")
        return {"preview": preview, "channel": channel, "bitrate": bitrate, "address": address}

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
            try:
                if settings["preview"]:
                    bus = SimulatedEncoderBus(self._preview_device(settings["address"]))
                else:
                    try:
                        bus = self._open_bus(settings["channel"], settings["bitrate"])
                    except Exception as exc:
                        raise RuntimeError(f"无法打开 {settings['channel']}：{exc}") from exc
            except Exception as exc:
                with self._lock:
                    self._state = "disconnected"
                    self._error = str(exc)
                return {"ok": False, "error": str(exc)}
            with self._lock:
                self._reset_session()
                self._generation += 1
                generation = self._generation
                self._stop = threading.Event()
                stop = self._stop
                self._bus = bus
                self._mode = "preview" if settings["preview"] else "pcan"
                self._channel = settings["channel"]
                self._bitrate = settings["bitrate"]
                self._address = settings["address"]
                self._started_at = self._clock()
                self._state = "running"
                self._rx_thread = threading.Thread(target=self._rx_loop, args=(generation, stop, bus),
                                                   name="encoder-rx", daemon=True)
                self._rx_thread.start()
            return {"ok": True, "message": "编码器连接已建立" + ("（模拟编码器，不发送到总线）" if settings["preview"] else "")}

    @staticmethod
    def _preview_device(address: int) -> Any:
        from .simulator import SimulatedEncoder
        return SimulatedEncoder(address)

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
            rx, poll = self._rx_thread, self._poll_thread
            self._rx_thread = self._poll_thread = None
            self._stop.set()
            self._poll_stop.set()
            self._generation += 1
            bus, self._bus = self._bus, None
            self._state = "disconnected"
            waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.event.set()
        for thread in (rx, poll):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._shutdown_bus(bus)

    def disconnect(self) -> dict[str, Any]:
        with self._op_lock:
            self._teardown()
            with self._lock:
                self._error = None
                self._report_on = False
                self._poll_hz = 0
        return {"ok": True}

    close = disconnect

    def _fail(self, generation: int, message: str) -> None:
        with self._lock:
            if generation != self._generation or self._state != "running":
                return
            self._state = "error"
            self._error = message
            self._stop.set()
            self._poll_stop.set()
            self._poll_hz = 0
        self._log("error", message)

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
            if getattr(message, "is_error_frame", False) or not getattr(message, "is_rx", True):
                continue
            try:
                self._handle_message(message)
            except Exception as exc:
                self._fail(generation, f"接收处理异常：{exc}")
                return

    def _census_add(self, can_id: int, extended: bool, data: bytes, decoded: dict[str, Any] | None,
                    arrival: float, mine: bool) -> None:
        func = decoded["func"] if decoded else None
        key = (can_id, extended, len(data), func)
        entry = self._census.get(key)
        if entry is None:
            if len(self._census) >= CENSUS_LIMIT:
                return
            entry = {"id": can_id, "extended": extended, "dlc": len(data), "func": func, "count": 0,
                     "mine": mine, "kind": decoded["kind"] if decoded else ("other" if not mine else "invalid")}
            self._census[key] = entry
        entry["count"] += 1
        entry["data"] = _hex(data)
        entry["time"] = arrival

    def _reject(self, reason: str) -> None:
        self._rejected += 1
        self._last_reject = reason

    def _handle_message(self, message: Any) -> None:
        arrival = self._clock()
        can_id = int(message.arbitration_id)
        data = bytes(message.data)
        extended = bool(getattr(message, "is_extended_id", False))
        waiters: list[_Waiter] = []
        info: _Rx | None = None
        with self._lock:
            self._rx_total += 1
            mine = not extended and (can_id == self._address or can_id in self._alt_ids)
            decoded = d.decode_brt_frame(data, None) if mine else None
            self._census_add(can_id, extended, data, decoded, arrival, mine)
            if not mine:
                self._rx_other += 1
                return
            if decoded is None:
                self._reject(f"0x{can_id:03X} 帧头无效（LEN 与 DLC 不符或不足 4 字节）：{_hex(data)}")
                return
            if decoded["address"] != can_id and not self._alt_ids:
                self._reject(f"ADDR 字节 0x{decoded['address']:02X} 与 CAN ID 0x{can_id:03X} 不一致")
                return
            kind = decoded["kind"]
            if kind == "unknown":
                self._reject(f"未识别的帧布局：FUNC 0x{decoded['func']:02X} · DLC {len(data)} · {_hex(data)}")
            else:
                self._last_match = arrival
            if kind == "position":
                self._record_position(message, decoded, arrival)
            elif kind == "value":
                name = {d.BRT_FUNC_MULTITURN: "multiturn", d.BRT_FUNC_TURNS: "turns",
                        d.BRT_FUNC_SPEED: "speed"}.get(decoded["func"])
                if name:
                    self._device_values[name] = {"unsigned": decoded["unsigned"], "signed": decoded["signed"],
                                                 "time": arrival}
            info = _Rx(can_id, decoded, arrival)
            waiters = [waiter for waiter in self._waiters if waiter.accept(info)]
        for waiter in waiters:
            waiter.result = info
            waiter.event.set()

    def _record_position(self, message: Any, decoded: dict[str, Any], arrival: float) -> None:
        raw = decoded["raw"]
        if not decoded["in_range"]:
            self._reject(f"位置值 {raw} 超出 0..{RESOLUTION - 1}")
            return
        t = self._timebase.map(getattr(message, "timestamp", None), arrival)
        position, speed = self._tracker.push(t, raw)
        self._series.append(t, raw, position, speed)
        self._last = {"raw": raw, "t": t, "arrival": arrival, "position": position, "speed": speed}
        label = motion_label(speed)
        if label in ("increasing", "decreasing"):
            self._last_direction = (label, arrival)
        self._pos_count += 1
        if self._prev_t is not None and t > self._prev_t:
            gap = t - self._prev_t
            self._win_sum += gap
            self._win_n += 1
            self._win_max = max(self._win_max, gap)
        self._prev_t = t

    # -- transmit ---------------------------------------------------------

    def _require_bus(self) -> tuple[Any, int]:
        with self._lock:
            if self._state != "running" or self._bus is None:
                raise EncoderError("编码器未连接")
            return self._bus, self._address

    def _send(self, bus: Any, address: int, frame: bytes, label: str | None) -> None:
        try:
            bus.send(can.Message(arbitration_id=address, data=frame, is_extended_id=False), timeout=0.2)
        except Exception as exc:
            with self._lock:
                self._tx_errors += 1
            raise EncoderError(f"发送失败：{exc}") from exc
        with self._lock:
            self._tx_total += 1
        if label:
            self._log("tx", f"→ 0x{address:03X}  {_hex(frame)}  {label}")

    def _transact(self, frame: bytes, accept: Callable[[_Rx], bool], label: str, *,
                  timeout: float | None = None) -> _Rx:
        bus, address = self._require_bus()
        waiter = _Waiter(accept)
        with self._lock:
            self._waiters.append(waiter)
        try:
            self._send(bus, address, frame, label)
            if not waiter.event.wait(timeout if timeout is not None else self._ack_timeout_s):
                raise EncoderError(f"编码器无应答（{label}）：核对位率、节点地址和接线")
            if waiter.result is None:
                raise EncoderError("连接已断开")
            return waiter.result
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def _expect_ack(self, frame: bytes, func: int, label: str, *, extra_ids: tuple[int, ...] = ()) -> _Rx:
        _, address = self._require_bus()
        ids = {address, *extra_ids}
        reply = self._transact(
            frame, lambda rx: rx.can_id in ids and rx.decoded["kind"] == "ack" and rx.decoded["func"] == func, label)
        status = reply.decoded["status"]
        if status != 0:
            self._log("error", f"← {label}：设备返回状态 0x{status:02X}")
            raise EncoderError(f"编码器拒绝了{label}（状态 0x{status:02X}，说明书未定义该值）")
        self._log("ack", f"← {label}：编码器已应答")
        return reply

    def _read_value(self, func: int, label: str) -> _Rx:
        _, address = self._require_bus()
        kind = "position" if func == d.BRT_FUNC_POSITION else "value"
        return self._transact(
            d.build_brt_read(address, func),
            lambda rx: rx.can_id == address and rx.decoded["kind"] == kind and rx.decoded["func"] == func, label)

    def _verify_position(self, target: int | None, tolerance: int = ZERO_TOLERANCE) -> dict[str, Any]:
        time.sleep(SETTLE_S)
        try:
            raw = self._read_value(d.BRT_FUNC_POSITION, "读回位置").decoded["raw"]
        except EncoderError as exc:
            return {"ok": False, "actual": None, "expected": target, "error": str(exc)}
        ok = target is None or circular_distance(raw, target) <= tolerance
        return {"ok": ok, "actual": raw, "expected": target, "tolerance": tolerance}

    def _record_applied(self, key: str, label: str, value: str, verified: bool | None, detail: str = "") -> None:
        with self._lock:
            self._applied[key] = {"key": key, "label": label, "value": value, "verified": verified, "detail": detail,
                                  "time": datetime.fromtimestamp(self._wall_clock()).strftime("%H:%M:%S")}

    def _reference_changed(self, text: str) -> None:
        """The position origin moved: restart unwrapping and put a marker on the curves."""
        with self._lock:
            self._mark(text)
            self._tracker.reset()

    # -- poll thread ------------------------------------------------------

    def _start_poll(self, hz: int) -> None:
        self._stop_poll()
        bus, address = self._require_bus()
        with self._lock:
            generation = self._generation
            self._poll_stop = threading.Event()
            stop = self._poll_stop
            self._poll_hz = hz
            self._poll_thread = threading.Thread(target=self._poll_loop,
                                                 args=(generation, stop, bus, hz),
                                                 name="encoder-poll", daemon=True)
            self._poll_thread.start()

    def _stop_poll(self) -> None:
        with self._lock:
            thread, self._poll_thread = self._poll_thread, None
            self._poll_stop.set()
            self._poll_hz = 0
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _poll_loop(self, generation: int, stop: threading.Event, bus: Any, hz: int) -> None:
        period = 1.0 / hz
        failures = 0
        next_at = self._clock()
        while not stop.is_set():
            while not stop.is_set():
                remaining = next_at - self._clock()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.002))
            if stop.is_set() or generation != self._generation:
                return
            with self._lock:
                address = self._address
            try:
                self._send(bus, address, d.build_brt_read(address), None)
                failures = 0
            except EncoderError:
                failures += 1
                if failures >= POLL_FAILURE_LIMIT:
                    with self._lock:
                        if self._poll_stop is stop:
                            self._poll_hz = 0
                    self._log("error", f"连续 {POLL_FAILURE_LIMIT} 次发送失败，已停止轮询；核对位率与接线后重新开始")
                    return
            next_at += period
            if next_at < self._clock() - 0.25:
                next_at = self._clock()

    # -- commands ---------------------------------------------------------

    def command(self, name: str, args: dict[str, Any] | None = None, acknowledged: bool = False) -> dict[str, Any]:
        entry = self._COMMANDS.get(str(name))
        if entry is None:
            return {"ok": False, "error": f"未知命令：{name}"}
        tier, handler = entry
        if tier == "write" and acknowledged is not True:
            return {"ok": False, "error": "该操作会改写编码器保存的设置，需要在确认窗口里确认后执行"}
        try:
            with self._cmd_lock:
                return handler(self, args or {})
        except (EncoderError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def _cmd_read_position(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = self._read_value(d.BRT_FUNC_POSITION, "读取单圈位置").decoded["raw"]
        if raw >= RESOLUTION:
            raise EncoderError(f"编码器返回的位置值 {raw} 超出 0..{RESOLUTION - 1}")
        return {"ok": True, "raw": raw, "angle_deg": round(raw * DEG_PER_COUNT, 3)}

    def _cmd_read_multiturn(self, args: dict[str, Any]) -> dict[str, Any]:
        value = self._read_value(d.BRT_FUNC_MULTITURN, "读取虚拟多圈位置").decoded
        return {"ok": True, "unsigned": value["unsigned"], "signed": value["signed"]}

    def _cmd_read_turns(self, args: dict[str, Any]) -> dict[str, Any]:
        value = self._read_value(d.BRT_FUNC_TURNS, "读取虚拟圈数").decoded
        return {"ok": True, "unsigned": value["unsigned"], "signed": value["signed"]}

    def _cmd_read_speed(self, args: dict[str, Any]) -> dict[str, Any]:
        value = self._read_value(d.BRT_FUNC_SPEED, "读取角速度计数").decoded
        result: dict[str, Any] = {"ok": True, "count": value["signed"]}
        if args.get("sample_ms") is not None:
            sample_ms = _as_int(args, "sample_ms", "角速度采样时间")
            result["sample_ms"] = sample_ms
            result["rpm"] = round(d.brt_speed_rpm(value["signed"], sample_ms), 3)
        with self._lock:
            self._device_values.setdefault("speed", {}).update({k: result[k] for k in ("sample_ms", "rpm") if k in result})
        return result

    def _cmd_report_start(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        self._stop_poll()
        self._expect_ack(d.build_brt_report_mode(address, True), d.BRT_FUNC_REPORT, "开启自动上报")
        with self._lock:
            self._report_on = True
        self._record_applied("report", "自动上报", "开启", True)
        return {"ok": True, "message": "已开启自动上报；用“到达速率”核对间隔，低于 5 ms 立即关闭；迟迟没有位置数据时到“收到的帧”核对帧布局"}

    def _cmd_report_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        self._expect_ack(d.build_brt_report_mode(address, False), d.BRT_FUNC_REPORT, "关闭自动上报")
        with self._lock:
            self._report_on = False
        self._record_applied("report", "自动上报", "关闭（查询模式）", True)
        return {"ok": True, "message": "已回到查询模式"}

    def _cmd_poll_start(self, args: dict[str, Any]) -> dict[str, Any]:
        hz = _as_int(args, "hz", "轮询频率")
        if hz not in POLL_RATES_HZ:
            raise ValueError("轮询频率只能是 " + " / ".join(str(rate) for rate in POLL_RATES_HZ) + " Hz")
        self._require_bus()
        with self._lock:
            streaming = self._report_on
        if streaming:
            self._cmd_report_stop({})
        self._start_poll(hz)
        return {"ok": True, "message": f"开始轮询 {hz} Hz"}

    def _cmd_poll_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        self._stop_poll()
        return {"ok": True}

    def _cmd_clear_curves(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._series.clear()
            self._tracker.reset()
            self._markers.clear()
            self._prev_t = None
        return {"ok": True}

    def _cmd_clear_log(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._events.clear()
            self._census.clear()
            self._rejected = 0
            self._last_reject = None
        return {"ok": True}

    def _cmd_set_period(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        period_us = _as_int(args, "period_us", "上报周期")
        frame = d.build_brt_period(address, period_us)
        self._expect_ack(frame, d.BRT_FUNC_PERIOD, f"上报周期 {period_us} µs")
        self._record_applied("period", "上报周期", f"{period_us} µs", True,
                             "掉电记忆；是否生效以“实测周期”为准")
        return {"ok": True, "period_us": period_us, "message": f"上报周期已写入 {period_us} µs"}

    def _cmd_set_zero(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        self._expect_ack(d.build_brt_zero(address), d.BRT_FUNC_ZERO, "当前位置置零")
        verify = self._verify_position(0)
        self._reference_changed("置零")
        self._record_applied("zero", "零位", "当前位置 → 0°", verify["ok"], self._verify_text(verify))
        return {"ok": True, "verify": verify, "message": self._verify_message("置零", verify)}

    def _cmd_set_midpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        self._expect_ack(d.build_brt_midpoint(address), d.BRT_FUNC_MIDPOINT, "当前位置设为中点")
        verify = self._verify_position(RESOLUTION // 2)
        self._reference_changed("设为中点")
        self._record_applied("midpoint", "中点", "当前位置 → 180°", verify["ok"], self._verify_text(verify))
        return {"ok": True, "verify": verify, "message": self._verify_message("设为中点", verify)}

    def _cmd_set_count(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        count = _as_int(args, "count", "当前位置计数")
        frame = d.build_brt_set_count(address, count)
        self._expect_ack(frame, d.BRT_FUNC_SET_COUNT, f"设置当前计数 {count}")
        verify = self._verify_position(count)
        self._reference_changed(f"设为 {count}")
        self._record_applied("count", "位置计数", f"当前位置 → {count}", verify["ok"], self._verify_text(verify))
        return {"ok": True, "verify": verify, "message": self._verify_message(f"设置计数 {count}", verify)}

    def _cmd_set_direction(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        counter_clockwise = bool(args.get("counter_clockwise"))
        with self._lock:
            last = self._last
            before = last["raw"] if last and self._clock() - last["arrival"] <= FRESH_MAX_S else None
        frame = d.build_brt_direction(address, counter_clockwise)
        text = "逆时针增大" if counter_clockwise else "顺时针增大"
        self._expect_ack(frame, d.BRT_FUNC_DIRECTION, f"递增方向 {text}")
        verify = self._verify_position(None)
        self._reference_changed(f"方向 {text}")
        after = verify.get("actual")
        detail = f"写入前后读数 {before if before is not None else '—'} → {after if after is not None else '—'}"
        self._record_applied("direction", "递增方向", text, None, detail)
        return {"ok": True, "before": before, "after": after, "message": f"递增方向已写入：{text}（{detail}）"}

    def _cmd_set_speed_time(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        sample_ms = _as_int(args, "sample_ms", "角速度采样时间")
        frame = d.build_brt_speed_time(address, sample_ms)
        self._expect_ack(frame, d.BRT_FUNC_SPEED_TIME, f"角速度采样时间 {sample_ms} ms")
        self._record_applied("speed_time", "角速度采样时间", f"{sample_ms} ms", None,
                             "说明书示例字节序不一致，写入后用“读取角速度计数”核对")
        return {"ok": True, "sample_ms": sample_ms, "message": f"角速度采样时间已写入 {sample_ms} ms"}

    def _cmd_set_address(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        new_address = _as_int(args, "address", "新节点地址")
        if new_address == address:
            raise ValueError("新节点地址与当前地址相同")
        frame = d.build_brt_set_address(address, new_address)
        with self._lock:
            self._alt_ids = {new_address}
        try:
            self._expect_ack(frame, d.BRT_FUNC_ADDRESS, f"节点地址 0x{address:02X} → 0x{new_address:02X}",
                             extra_ids=(new_address,))
        except EncoderError:
            with self._lock:
                self._alt_ids = set()
            raise
        with self._lock:
            self._address = new_address
            self._alt_ids = set()
            self._mark("地址已改")
        verify = self._verify_position(None)
        note = "" if verify["ok"] else "；写入已应答，但新地址下读取位置无应答"
        self._record_applied("address", "节点地址", f"0x{new_address:02X}", verify["ok"],
                             "新地址立即生效，掉电记忆" + note)
        return {"ok": True, "address": new_address,
                "message": f"节点地址已改为 0x{new_address:02X}，连接已跟随新地址{note}"}

    def _cmd_set_bitrate(self, args: dict[str, Any]) -> dict[str, Any]:
        _, address = self._require_bus()
        bitrate = _as_int(args, "bitrate", "位率")
        if bitrate == self._bitrate:
            raise ValueError("新位率与当前连接位率相同")
        frame = d.build_brt_set_bitrate(address, bitrate)
        self._expect_ack(frame, d.BRT_FUNC_BITRATE, f"位率 {bitrate // 1000} kbit/s")
        self._stop_poll()
        with self._lock:
            self._pending_bitrate = bitrate
        self._record_applied("bitrate", "CAN 位率", f"{bitrate // 1000} kbit/s", None,
                             "说明书未写明何时生效；以新位率重连，仍无应答时给编码器重新上电")
        return {"ok": True, "reconnect_bitrate": bitrate,
                "message": f"位率 {bitrate // 1000} kbit/s 已写入；请以新位率重新连接（必要时给编码器重新上电）"}

    @staticmethod
    def _verify_text(verify: dict[str, Any]) -> str:
        if verify.get("actual") is None:
            return "读回失败：" + str(verify.get("error", "无应答"))
        actual = verify["actual"]
        return f"读回 {actual}（{actual * DEG_PER_COUNT:.2f}°）" + ("" if verify["ok"] else f"，偏离目标 {verify['expected']}")

    @classmethod
    def _verify_message(cls, action: str, verify: dict[str, Any]) -> str:
        if verify["ok"]:
            return f"{action}成功：{cls._verify_text(verify)}"
        return f"{action}已应答，但{cls._verify_text(verify)}；转轴可能仍在转动，请固定后重试"

    _COMMANDS: dict[str, tuple[str, Callable[[Any, dict[str, Any]], dict[str, Any]]]] = {
        "read_position": ("read", _cmd_read_position), "read_multiturn": ("read", _cmd_read_multiturn),
        "read_turns": ("read", _cmd_read_turns), "read_speed": ("read", _cmd_read_speed),
        "report_start": ("stream", _cmd_report_start), "report_stop": ("stream", _cmd_report_stop),
        "poll_start": ("stream", _cmd_poll_start), "poll_stop": ("stream", _cmd_poll_stop),
        "clear_curves": ("local", _cmd_clear_curves), "clear_log": ("local", _cmd_clear_log),
        "set_period": ("write", _cmd_set_period), "set_zero": ("write", _cmd_set_zero),
        "set_midpoint": ("write", _cmd_set_midpoint), "set_count": ("write", _cmd_set_count),
        "set_direction": ("write", _cmd_set_direction), "set_speed_time": ("write", _cmd_set_speed_time),
        "set_address": ("write", _cmd_set_address), "set_bitrate": ("write", _cmd_set_bitrate),
    }

    # -- snapshot and curves ----------------------------------------------

    def _roll_rates(self, now: float) -> None:
        if self._win_start is None:
            self._win_start, self._win_count = now, self._pos_count
            return
        elapsed = now - self._win_start
        if elapsed < RATE_WINDOW_S:
            return
        self._hz = (self._pos_count - self._win_count) / elapsed
        self._interval_ms = 1000.0 * self._win_sum / self._win_n if self._win_n else None
        self._max_gap_ms = 1000.0 * self._win_max if self._win_n else None
        self._win_start, self._win_count = now, self._pos_count
        self._win_sum, self._win_n, self._win_max = 0.0, 0, 0.0

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            running = self._state == "running"
            self._roll_rates(now)
            last = self._last
            reading = None
            if last is not None:
                speed = last["speed"]
                reading = {
                    "raw": last["raw"], "angle_deg": round(last["raw"] * DEG_PER_COUNT, 3),
                    "accumulated_deg": round(last["position"] * DEG_PER_COUNT, 3),
                    "turns": round(last["position"] / RESOLUTION, 4),
                    "speed_dps": None if speed is None else round(speed, 2),
                    "speed_rpm": None if speed is None else round(speed / 6.0, 3),
                    "motion": motion_label(speed), "age": round(now - last["arrival"], 2),
                }
            stability = self._series.stability(last["t"], STABILITY_WINDOW_S) if last else None
            interval_ms = self._interval_ms
            direction = None
            if self._last_direction is not None:
                direction = {"label": self._last_direction[0], "age": round(now - self._last_direction[1], 1)}
            # "mine" follows the current address, so entries from before an address change turn into
            # "other node" rows instead of staying listed as the encoder's frames.
            census = sorted(
                ({**item, "mine": item["id"] == self._address and not item["extended"]}
                 for item in self._census.values()),
                key=lambda item: (not item["mine"], item["id"], item["dlc"]))
            return {
                "connection": {
                    "state": self._state, "connected": running, "mode": self._mode, "channel": self._channel,
                    "bitrate": self._bitrate, "address": self._address, "error": self._error,
                    "pending_bitrate": self._pending_bitrate,
                    "age": None if self._started_at is None or not running else round(now - self._started_at, 1),
                },
                "reading": reading,
                "direction": direction,
                "stream": {
                    "hz": round(self._hz, 1), "interval_ms": None if interval_ms is None else round(interval_ms, 3),
                    "max_gap_ms": None if self._max_gap_ms is None else round(self._max_gap_ms, 3),
                    "polling": self._poll_hz > 0, "poll_hz": self._poll_hz, "report_on": self._report_on,
                    "silent_s": None if self._last_match is None else round(now - self._last_match, 2),
                    "samples": self._series.total, "rejected": self._rejected, "last_reject": self._last_reject,
                    "max_rpm": None if not interval_ms else round(max_trackable_rpm(interval_ms / 1000.0), 1),
                },
                "stability": stability,
                "device": {name: {**{k: v for k, v in item.items() if k != "time"},
                                  "age": None if "time" not in item else round(now - item["time"], 1)}
                           for name, item in self._device_values.items()},
                "applied": list(self._applied.values()),
                "events": list(self._events),
                "markers": list(self._markers),
                "frames": [{**{k: v for k, v in item.items() if k != "time"}, "age": round(now - item["time"], 2)}
                           for item in census],
                "stats": {"rx_total": self._rx_total, "rx_other": self._rx_other, "tx_total": self._tx_total,
                          "tx_errors": self._tx_errors},
            }

    def series(self, cursor: int = 0, epoch: int = -1) -> dict[str, Any]:
        with self._lock:
            reply = self._series.fetch(int(cursor), int(epoch))
        reply["ok"] = True
        return reply

    def export_csv(self) -> dict[str, Any]:
        with self._lock:
            samples = len(self._series)
            data = self._series.csv()
        if not samples:
            return {"ok": False, "error": "曲线里还没有数据可导出"}
        return {"ok": True, "data": data, "samples": samples}


def encoder_catalog() -> dict[str, Any]:
    """Static choices the page renders from (sent once in bootstrap)."""
    return {
        "resolution": RESOLUTION, "default_address": d.BRT_DEFAULT_ADDRESS,
        "default_bitrate": d.BRT_DEFAULT_BITRATE, "bitrates": list(BITRATES),
        "poll_rates_hz": list(POLL_RATES_HZ), "period_us_range": list(d.BRT_PERIOD_US_RANGE),
        "zero_tolerance": ZERO_TOLERANCE, "stability_window_s": STABILITY_WINDOW_S,
        "fresh_max_s": FRESH_MAX_S,
    }


__all__ = ["EncoderError", "EncoderService", "encoder_catalog"]
