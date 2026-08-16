"""Threaded python-can transport with an offline simulator."""

from __future__ import annotations

import csv
from datetime import datetime
import math
from pathlib import Path
import sqlite3
import threading
import time
from collections import deque
from typing import Any

from .ivt import (BMS_CANB_CMD_ID, BMS_CANB_RSP_ID, BITRATE_PRESETS, DEFAULT_CMD_ID,
                  DEFAULT_RSP_ID, IVT_RESPONSE_MUXES, IvtClient, IvtFrame,
                  compare_readback, expected_bms_canb_config)
from .protocol import (CAN1_CELL_TEMP_BASE, CAN1_CELL_VOLT_BASE, CAN1_IDS, CAN1_TOOL_IDS,
                       BmsProtocol, CanFrame, build_command, command_ack_matches)


class CanService:
    def __init__(self, allow_simulation: bool = True) -> None:
        self.lock = threading.RLock()
        self.allow_simulation = allow_simulation
        self.protocol = BmsProtocol()
        self.bus: Any = None
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.simulator: Any = None
        self.connection: dict[str, Any] = {
            "connected": False, "mode": None, "channel": None, "bitrate": None,
            "bus_profile": "can1", "status": "未连接", "error": None,
        }
        self.record_file: Any = None
        self.record_writer: csv.writer | None = None
        self.record_db: sqlite3.Connection | None = None
        self.record_kind: str | None = None
        self.record_path: str | None = None
        self.record_pending = 0
        self.record_last_commit = 0.0
        self.replay_frames: list[CanFrame] = []
        self.replay_relative: list[float] = []
        self.replay_index = 0
        self.replay_position = 0.0
        self.replay_speed = 1.0
        self.replay_paused = False
        self.replay_db: sqlite3.Connection | None = None
        self.replay_first_timestamp = 0.0
        self.replay_duration = 0.0
        self.replay_total = 0
        self.replay_next_seq = 1
        self.replay_db_buffer: deque[tuple[Any, ...]] = deque()
        self.command_sequence = 0
        self.ivt_operation_lock = threading.Lock()
        self.ivt_rx_condition = threading.Condition(self.lock)
        self.ivt_rx_frames: deque[IvtFrame] = deque(maxlen=512)
        self.ivt_config: dict[str, Any] | None = None
        self.bench_module: Any = None
        self.bench_model: Any = None
        self.bench_command_processor: Any = None
        self.bench_thread: threading.Thread | None = None
        self.bench_stop_event = threading.Event()
        self.bench_lock = threading.RLock()
        self.bench_last_tx_error = ""
        self.bench_last_tx_error_time = 0.0

    def connect(self, config: dict[str, Any]) -> dict[str, Any]:
        self.disconnect()
        mode = config.get("mode", "pcan")
        profile = config.get("bus_profile", "can1")
        bitrate = int(config.get("bitrate") or (250000 if profile == "canb_legacy" else 500000))
        channel = str(config.get("channel") or "PCAN_USBBUS1")
        if mode == "bench" and profile != "can1":
            return {"ok": False, "error": "主上位机台架只发送 CAN1 从控帧；CANB 请使用真实 PCAN 和真实 IVT"}
        with self.lock:
            self.protocol = BmsProtocol()
            self.ivt_rx_frames.clear()
            self.ivt_config = None
            self.connection.update({"connected": False, "mode": mode, "channel": channel,
                                    "bitrate": bitrate, "bus_profile": profile, "status": "正在连接", "error": None})
        try:
            if mode == "simulation":
                if not self.allow_simulation:
                    raise RuntimeError("当前发布版仅支持真实 PCAN 设备")
                from .simulator import BmsSimulator
                self.simulator = BmsSimulator(self._ingest, bus_profile=profile)
                self.simulator.start()
            else:
                try:
                    import can
                except ImportError as exc:
                    raise RuntimeError("未安装 python-can，请先执行 pip install -r requirements.txt") from exc
                self.bus = can.Bus(interface="pcan", channel=channel, bitrate=bitrate, receive_own_messages=False)
                self.stop_event.clear()
                self.worker = threading.Thread(target=self._receive_loop, name="pcan-receiver", daemon=True)
                self.worker.start()
                if mode == "bench":
                    self._start_bench(profile)
            with self.lock:
                status = "模拟数据" if mode == "simulation" else "台架发送" if mode == "bench" else "已连接"
                self.connection.update({"connected": True, "status": status, "error": None})
            return {"ok": True, "connection": dict(self.connection)}
        except Exception as exc:
            self.disconnect()
            with self.lock:
                self.connection.update({"status": "连接失败", "error": str(exc), "mode": mode, "channel": channel,
                                        "bitrate": bitrate, "bus_profile": profile})
            return {"ok": False, "error": str(exc), "connection": dict(self.connection)}

    def disconnect(self) -> dict[str, Any]:
        self.stop_event.set()
        self.bench_stop_event.set()
        if self.bench_thread and self.bench_thread.is_alive():
            self.bench_thread.join(timeout=1.2)
        self.bench_thread = None
        self.bench_module = None
        self.bench_model = None
        self.bench_command_processor = None
        if self.simulator:
            self.simulator.stop()
            self.simulator = None
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.2)
        self.worker = None
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None
        if self.replay_db is not None:
            try:
                self.replay_db.close()
            except Exception:
                pass
            self.replay_db = None
        self.stop_recording()
        self.replay_frames = []
        self.replay_relative = []
        self.replay_index = 0
        self.replay_position = 0.0
        self.replay_first_timestamp = 0.0
        self.replay_duration = 0.0
        self.replay_total = 0
        self.replay_next_seq = 1
        self.replay_db_buffer.clear()
        with self.lock:
            # A disconnect ends the identity of the attached BMS. Do not let
            # RTC replies, fault history, or measurements from the previous
            # device survive into the next connection.
            self.protocol = BmsProtocol()
            self.ivt_rx_frames.clear()
            self.ivt_config = None
            self.ivt_rx_condition.notify_all()
            self.connection.pop("bench_target", None)
            self.connection.update({"connected": False, "status": "未连接"})
        return {"ok": True}

    def send_command(self, name: str, values: dict[str, Any], acknowledged: bool) -> dict[str, Any]:
        if not acknowledged:
            return {"ok": False, "error": "发送前必须确认本次写操作"}
        with self.lock:
            if not self.connection.get("connected"):
                return {"ok": False, "error": "CAN 尚未连接"}
            if self.connection.get("mode") == "replay":
                return {"ok": False, "error": "历史回放为只读，不能发送 CAN 命令"}
            profile = self.connection.get("bus_profile")
            state = self.protocol.overview.get("state")
            charge_mode = bool(self.protocol.fault.get("flags", {}).get("charge_mode"))
            summary_seen = self.protocol.last_summary_monotonic
            summary_age = None if summary_seen is None else max(0.0, self.protocol.clock() - summary_seen)
        if profile != "can1":
            return {"ok": False, "error": "F405 工具命令只在 CAN1 接收；当前连接不是 CAN1"}
        if summary_age is None or summary_age > 1.5:
            return {"ok": False, "error": "主控总状态帧未收到或已经超时，暂不发送命令"}
        if name in {"charge_config", "alarm_thresholds", "alarm_switches", "current_direction",
                    "charger_type", "log_info", "log_read", "log_clear"} and state not in {2, 3, 7}:
            return {"ok": False, "error": "主控仅在自检、待机或故障保持状态接受此命令"}
        if name == "fault_reset" and state != 7:
            return {"ok": False, "error": "故障复位命令仅在故障保持状态处理"}
        if name == "charger_type" and charge_mode:
            return {"ok": False, "error": "必须先释放实体充电按钮并退出充电模式，才能切换充电机类型"}
        try:
            command_values = dict(values)
            expects_unified_ack = name != "rtc"
            with self.lock:
                self.command_sequence = (self.command_sequence + 1) & 0xFF
                sequence = self.command_sequence
                if expects_unified_ack:
                    self.protocol.command_acks.pop(sequence, None)
                    if name == "log_read":
                        self.protocol._flash_record_parts.pop(sequence, None)
                else:
                    self.protocol.rtc_replies.pop(sequence, None)
            command_values["_sequence"] = sequence
            frame = build_command(name, command_values)
            if self.simulator:
                self.simulator.on_command(frame)
            else:
                import can
                message = can.Message(arbitration_id=frame.arbitration_id, data=frame.data,
                                      is_extended_id=frame.is_extended_id, is_fd=False)
                self.bus.send(message, timeout=0.2)
            with self.lock:
                self.protocol.ingest(frame)
                self._record(frame)
            if not expects_unified_ack:
                # RTC replies echo the request sequence. Wait for this exact
                # sequence instead of treating whichever old reply is visible
                # as the result of the current button press.
                deadline = time.monotonic() + 1.0
                reply = None
                while time.monotonic() < deadline:
                    with self.lock:
                        reply = self.protocol.rtc_replies.get(sequence)
                    if reply is not None:
                        break
                    time.sleep(0.01)
                if reply is None:
                    return {"ok": False, "sequence": sequence,
                            "error": f"RTC 请求在 1.0s 内没有收到序列 {sequence} 的专用应答"}
                if int(reply.get("status", -1)) != 0:
                    return {"ok": False, "sequence": sequence, "rtc_reply": reply,
                            "error": f"主控拒绝 RTC 校时：状态 {reply.get('status')}"}
                return {"ok": True, "sequence": sequence, "rtc_reply": reply,
                        "message": "RTC 校时成功"}

            deadline = time.monotonic() + 1.0
            ack = None
            while time.monotonic() < deadline:
                with self.lock:
                    candidate = self.protocol.command_acks.get(sequence)
                    ack = candidate if candidate is not None and command_ack_matches(name, candidate) else None
                if ack is not None:
                    break
                time.sleep(0.01)
            if ack is None:
                return {"ok": False, "error": f"命令 {frame.arbitration_id:#010x} 在 1.0s 内没有收到统一应答"}
            if not ack.get("accepted"):
                return {"ok": False, "error": f"主控拒绝：{ack.get('result_name')}（detail={ack.get('detail')}）", "ack": ack}
            return {"ok": True, "message": ack.get("result_name", "主控已接受"), "ack": ack}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def read_flash_fault_logs(self, limit: int = 50) -> dict[str, Any]:
        with self.lock:
            if self.protocol.fault.get("flags", {}).get("log_clear_pending"):
                return {"ok": False, "error": "主控正在分阶段清除 Flash 故障日志，请等待清除完成"}
            self.protocol.flash_log_info.clear()
        info_result = self.send_command("log_info", {}, True)
        if not info_result.get("ok"):
            return info_result
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with self.lock:
                info = dict(self.protocol.flash_log_info)
            if info.get("sequence") == info_result.get("ack", {}).get("sequence"):
                break
            time.sleep(0.01)
        else:
            return {"ok": False, "error": "已收到日志信息应答，但没有收到日志数量数据帧"}

        count = int(info.get("count", 0))
        start = max(0, count - max(1, min(int(limit), 200)))
        with self.lock:
            self.protocol.flash_log_records.clear()
        for index in range(start, count):
            result = self.send_command("log_read", {"index": index}, True)
            if not result.get("ok"):
                return {"ok": False, "error": f"读取日志 {index} 失败：{result.get('error')}", "read": index - start}
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with self.lock:
                    complete = index in self.protocol.flash_log_records
                if complete:
                    break
                time.sleep(0.01)
            else:
                return {"ok": False, "error": f"日志 {index} 的四个数据分片未收齐", "read": index - start}
        with self.lock:
            records = [self.protocol.flash_log_records[key] for key in sorted(self.protocol.flash_log_records)]
        return {"ok": True, "count": count, "dropped": int(info.get("dropped", 0)), "records": records}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            connection = dict(self.connection)
            if connection.get("mode") == "replay":
                connection["replay"] = {
                    "position": round(self.replay_position, 3), "duration": round(self.replay_duration, 3),
                    "speed": self.replay_speed, "paused": self.replay_paused,
                    "index": self.replay_index, "total": self.replay_total,
                }
            if self.record_kind:
                connection["recording"] = {"format": self.record_kind, "path": self.record_path}
            snapshot = self.protocol.snapshot(connection)
            snapshot["ivt_config"] = dict(self.ivt_config) if self.ivt_config else None
            snapshot["bench"] = self._bench_snapshot_locked()
            return snapshot

    def bench_snapshot(self) -> dict[str, Any]:
        """Return only the state needed by the independent bench page."""
        with self.lock:
            return {"connection": dict(self.connection), "bench": self._bench_snapshot_locked()}

    def ivt_snapshot(self) -> dict[str, Any]:
        """Return only the state needed by the independent IVT page."""
        with self.lock:
            return {"connection": dict(self.connection),
                    "ivt_config": dict(self.ivt_config) if self.ivt_config else None}

    def _start_bench(self, profile: str) -> None:
        if profile != "can1":
            raise RuntimeError("主上位机台架只支持 CAN1 从控帧")
        try:
            from .. import pcan_bms_bench as bench_module
        except ImportError as exc:
            raise RuntimeError("台架模式需要 python-can") from exc
        if bench_module.can is None:
            raise RuntimeError("台架模式需要 python-can")
        with self.lock:
            self.connection["bench_target"] = "CAN1 从控帧"
        self.bench_module = bench_module
        with self.bench_lock:
            self.bench_model = bench_module.BenchModel()
            self.bench_command_processor = bench_module.CommandProcessor(
                self.bench_model, bench_module.BmsMonitorState())
        self.bench_stop_event.clear()
        self.bench_thread = threading.Thread(target=self._bench_loop, name="pcan-bench-sender", daemon=True)
        self.bench_thread.start()

    def _bench_snapshot_locked(self) -> dict[str, Any] | None:
        if self.connection.get("mode") != "bench" or self.bench_model is None:
            return None
        with self.bench_lock:
            model = self.bench_model
            return {
                "active": True,
                "target": self.connection.get("bench_target") or "CAN1 从控帧",
                "ivt_source": "CANB 真实 IVT-S",
                "slaves": [{"id": slave.slave_id, "online": bool(slave.online),
                            "base_cell_mv": slave.base_cell_mv, "base_temp_c": slave.base_temp_c}
                           for slave in model.slaves],
                "open_wire_cells": len(model.open_wire_cells),
                "open_wire_temps": len(model.open_wire_temps),
            }

    def bench_command(self, command: str) -> dict[str, Any]:
        with self.bench_lock:
            if self.connection.get("mode") != "bench" or self.bench_command_processor is None:
                return {"ok": False, "error": "当前没有运行 CAN1 从控台架"}
            try:
                result = self.bench_command_processor.handle(str(command))
                return {"ok": True, "message": result}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def _bench_send(self, message: Any, bus: Any, label: str = "台架 TX") -> None:
        if bus is None:
            return
        try:
            bus.send(message, timeout=0.2)
            frame = CanFrame(int(message.arbitration_id), bytes(message.data), bool(message.is_extended_id),
                             time.time(), "tx")
            with self.lock:
                self.protocol.ingest(frame)
                self._record(frame)
        except Exception as exc:
            now = time.monotonic()
            if str(exc) != self.bench_last_tx_error or now - self.bench_last_tx_error_time >= 1.0:
                with self.lock:
                    self.connection.update({"error": f"{label}失败：{exc}"})
                self.bench_last_tx_error = str(exc)
                self.bench_last_tx_error_time = now

    def _bench_loop(self) -> None:
        next_voltage = time.monotonic()
        next_temperature = next_voltage
        while not self.bench_stop_event.wait(0.01):
            with self.bench_lock:
                model = self.bench_model
                module = self.bench_module
            if model is None or module is None:
                return
            now = time.monotonic()
            if now >= next_voltage:
                with self.bench_lock:
                    frames = [frame for slave in model.slaves
                              for frame in module.build_slave_voltage_frames(model, slave)]
                for frame in frames:
                    self._bench_send(frame, self.bus)
                next_voltage += module.TX_VOLT_PERIOD_S
            if now >= next_temperature:
                with self.bench_lock:
                    frames = [frame for slave in model.slaves
                              for frame in [module.build_slave_temp_frame(model, slave)] if frame is not None]
                for frame in frames:
                    self._bench_send(frame, self.bus)
                next_temperature += module.TX_TEMP_PERIOD_S

    @staticmethod
    def _parse_can_id(value: Any, default: int) -> int:
        if value is None or value == "":
            value = default
        parsed = int(value, 0) if isinstance(value, str) else int(value)
        if not 0 <= parsed <= 0x7FF:
            raise ValueError("IVT CAN ID 必须在 0x000..0x7FF 范围内")
        return parsed

    @staticmethod
    def _parse_u32(value: Any) -> int | None:
        if value is None or value == "":
            return None
        parsed = int(value, 0) if isinstance(value, str) else int(value)
        if not 0 <= parsed <= 0xFFFFFFFF:
            raise ValueError("IVT 序列号必须在 0..0xFFFFFFFF 范围内")
        return parsed

    @staticmethod
    def _parse_threshold(value: Any, default: int = 0) -> int:
        if value is None or value == "":
            return default
        parsed = int(value, 0) if isinstance(value, str) else int(value)
        if not 0 <= parsed <= 0xFFFF:
            raise ValueError("IVT 过流阈值必须在 0..65535 A 范围内")
        return parsed

    def _check_ivt_access(self) -> dict[str, Any]:
        with self.lock:
            connection = dict(self.connection)
        if not connection.get("connected"):
            raise RuntimeError("CAN 尚未连接")
        if connection.get("mode") != "pcan":
            raise RuntimeError("IVT 配置只允许使用真实 PCAN，模拟数据和历史回放为只读")
        if connection.get("bus_profile") not in {"canb", "canb_legacy"}:
            raise RuntimeError("IVT 命令只允许从 CANB 发送；CAN1 只发送 F405 工具命令")
        if connection.get("bitrate") not in BITRATE_PRESETS:
            raise RuntimeError("IVT 操作只支持 CANB 250 或 500 kbit/s 预设")
        return connection

    def _clear_ivt_rx(self) -> None:
        with self.lock:
            self.ivt_rx_frames.clear()

    def _ivt_receive(self, timeout: float) -> IvtFrame:
        deadline = time.monotonic() + timeout
        with self.ivt_rx_condition:
            while not self.ivt_rx_frames:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 IVT 应答超时")
                self.ivt_rx_condition.wait(timeout=remaining)
            return self.ivt_rx_frames.popleft()

    def _ivt_send(self, arbitration_id: int, data: Any) -> None:
        with self.lock:
            if self.bus is None or not self.connection.get("connected"):
                raise RuntimeError("CAN 已断开，无法发送 IVT 命令")
            import can
            payload = bytes(data)
            message = can.Message(arbitration_id=arbitration_id, data=payload,
                                  is_extended_id=False, is_fd=False)
            self.bus.send(message, timeout=0.2)
            frame = CanFrame(arbitration_id, payload, False, time.time(), "tx")
            self.protocol.ingest(frame)
            self._record(frame)

    def _reopen_pcan(self, bitrate: int) -> None:
        """Reopen CANB at a preset after IVT has restarted itself."""
        if bitrate not in BITRATE_PRESETS:
            raise ValueError("CANB 位率预设只支持 250 或 500 kbit/s")
        with self.lock:
            if self.bus is None or self.connection.get("mode") != "pcan":
                raise RuntimeError("PCAN 已断开，无法重新打开 CANB")
            old_bus = self.bus
            worker = self.worker
            channel = str(self.connection.get("channel") or "PCAN_USBBUS1")
            self.stop_event.set()
        if worker and worker.is_alive():
            worker.join(timeout=1.2)
        try:
            old_bus.shutdown()
        except Exception:
            pass
        with self.lock:
            self.worker = None
            self.bus = None
        try:
            import can
            new_bus = can.Bus(interface="pcan", channel=channel, bitrate=bitrate, receive_own_messages=False)
        except Exception as exc:
            with self.lock:
                self.connection.update({"status": "位率切换后无法重连", "error": str(exc), "bitrate": bitrate})
            raise RuntimeError(f"CANB 已切换到 {bitrate // 1000} kbit/s，但 PCAN 重开失败：{exc}") from exc
        with self.lock:
            self.bus = new_bus
            self.connection.update({"bitrate": bitrate, "status": "已连接", "error": None})
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._receive_loop, name="pcan-receiver", daemon=True)
            self.worker.start()

    def _ivt_expected(self, options: dict[str, Any], bitrate: int) -> dict[str, Any]:
        return expected_bms_canb_config(
            startup=str(options.get("startup") or "run"), bitrate=bitrate,
            positive_threshold_a=self._parse_threshold(options.get("positive_threshold_a")),
            positive_reset_threshold_a=self._parse_threshold(options.get("positive_reset_threshold_a")),
            negative_threshold_a=self._parse_threshold(options.get("negative_threshold_a")),
            negative_reset_threshold_a=self._parse_threshold(options.get("negative_reset_threshold_a")),
        )

    def _ivt_id_candidates(self, options: dict[str, Any]) -> list[tuple[int, int]]:
        """Return likely request/response IDs, with factory probing by default."""
        has_cmd = options.get("cmd_id") not in (None, "")
        has_rsp = options.get("rsp_id") not in (None, "")
        if has_cmd or has_rsp:
            return [(
                self._parse_can_id(options.get("cmd_id"), DEFAULT_CMD_ID),
                self._parse_can_id(options.get("rsp_id"), DEFAULT_RSP_ID),
            )]
        # A first-time device answers on the factory pair.  A device already
        # prepared for F405 answers on the BMS pair.  The mixed pairs also
        # cover an interrupted ID update without making the user guess first.
        return [
            (DEFAULT_CMD_ID, DEFAULT_RSP_ID),
            (BMS_CANB_CMD_ID, BMS_CANB_RSP_ID),
            (DEFAULT_CMD_ID, BMS_CANB_RSP_ID),
            (BMS_CANB_CMD_ID, DEFAULT_RSP_ID),
        ]

    def _probe_ivt_client(self, options: dict[str, Any]) -> IvtClient:
        last_error: Exception | None = None
        for cmd_id, rsp_id in self._ivt_id_candidates(options):
            self._clear_ivt_rx()
            client = IvtClient(self._ivt_send, self._ivt_receive, cmd_id=cmd_id, rsp_id=rsp_id)
            try:
                # Serial number is the shortest request that proves both IDs
                # and leaves the full readback to the selected client.
                client.read_serial_number()
                return client
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"未找到 IVT 应答，请检查位率和当前 Command/Response ID：{last_error}")

    def read_ivt_config(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        try:
            connection = self._check_ivt_access()
            with self.ivt_operation_lock:
                with self.lock:
                    self.ivt_config = None
                client = self._probe_ivt_client(options)
                readback = client.readback(bitrate=int(connection["bitrate"]))
                expected = self._ivt_expected(options, int(connection["bitrate"]))
                readback["comparison"] = compare_readback(readback, expected)
                readback["expected"] = expected
                with self.lock:
                    self.ivt_config = readback
                return {"ok": True, "readback": readback}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def configure_ivt_bms_canb(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        try:
            connection = self._check_ivt_access()
            startup = str(options.get("startup") or "run")
            if startup not in {"stop", "run"}:
                raise ValueError("IVT 上电模式必须是 stop 或 run")
            with self.ivt_operation_lock:
                with self.lock:
                    self.ivt_config = None
                client = self._probe_ivt_client(options)
                serial = self._parse_u32(options.get("serial_number"))
                thresholds = {
                    "positive": {
                        "threshold_a": self._parse_threshold(options.get("positive_threshold_a")),
                        "reset_threshold_a": self._parse_threshold(options.get("positive_reset_threshold_a")),
                    },
                    "negative": {
                        "threshold_a": self._parse_threshold(options.get("negative_threshold_a")),
                        "reset_threshold_a": self._parse_threshold(options.get("negative_reset_threshold_a")),
                    },
                }
                readback = client.setup_bms_canb(startup=startup, serial_number=serial,
                                                  reopen=self._reopen_pcan, bitrate=int(connection["bitrate"]),
                                                  thresholds=thresholds)
                expected = self._ivt_expected(options, int(connection["bitrate"]))
                readback["comparison"] = compare_readback(readback, expected)
                readback["expected"] = expected
                with self.lock:
                    self.ivt_config = readback
                return {"ok": True, "readback": readback, "message": "IVT 已按 BMS CANB 配置并完成重启读回核对"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def switch_ivt_bitrate(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        try:
            connection = self._check_ivt_access()
            target_bitrate = int(options.get("target_bitrate"))
            if target_bitrate not in BITRATE_PRESETS:
                raise ValueError("IVT 位率预设只支持 250 或 500 kbit/s")
            if target_bitrate == int(connection["bitrate"]):
                raise ValueError("目标位率与当前 PCAN 位率相同")
            with self.ivt_operation_lock:
                with self.lock:
                    self.ivt_config = None
                client = self._probe_ivt_client(options)
                client.set_mode("stop", str(options.get("startup") or "run"))
                response, alive = client.restart_to_bitrate(target_bitrate, self._reopen_pcan)
                self._clear_ivt_rx()
                readback = client.readback(bitrate=target_bitrate)
                expected = self._ivt_expected({**options, "bitrate": target_bitrate}, target_bitrate)
                readback["comparison"] = compare_readback(readback, expected)
                readback["expected"] = expected
                readback["bitrate_switch"] = {"target_bitrate": target_bitrate,
                                                "response": list(response.data), "alive": list(alive.data)}
                with self.lock:
                    self.ivt_config = readback
                return {"ok": True, "readback": readback,
                        "message": f"IVT 已重启到 {target_bitrate // 1000} kbit/s，并完成读回核对"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def load_replay(self, path: str) -> dict[str, Any]:
        """Load a native BMS log or CSV and start read-only replay."""
        database: sqlite3.Connection | None = None
        try:
            frames: list[CanFrame] = []
            relative: list[float] = []
            metadata: dict[str, str] = {}
            source = Path(path).expanduser()

            def is_can1(frame: CanFrame) -> bool:
                can_id = frame.arbitration_id
                if not frame.is_extended_id:
                    return False
                if can_id in CAN1_IDS or can_id in CAN1_TOOL_IDS:
                    return True
                volt_delta = can_id - CAN1_CELL_VOLT_BASE
                temp_delta = can_id - CAN1_CELL_TEMP_BASE
                return ((0 <= volt_delta <= (35 << 16) and volt_delta & 0xFFFF == 0)
                        or (0 <= temp_delta <= (5 << 16) and temp_delta & 0xFFFF == 0))

            if source.suffix.lower() == ".bmslog":
                database = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
                metadata = dict(database.execute("SELECT key, value FROM meta"))
                if metadata.get("format") != "BITFSAE_BMS_LOG" or metadata.get("schema_version") != "1":
                    raise ValueError("不是受支持的 BMS 数据记录文件")
                total, first, last, sample = self._validate_bmslog_database(database)
                inferred_profile = "can1" if any(is_can1(frame) for frame in sample) else "canb"
                duration = last - first
            else:
                with source.open("r", newline="", encoding="utf-8-sig") as handle:
                    for row in csv.DictReader(handle):
                        raw_id = (row.get("ID") or "").strip()
                        raw_data = (row.get("数据") or "").strip()
                        raw_time = (row.get("本地时间") or "").strip()
                        if not raw_id or not raw_time:
                            continue
                        frames.append(CanFrame(
                            int(raw_id, 16), bytes.fromhex(raw_data), (row.get("帧类型") or "") == "扩展",
                            datetime.fromisoformat(raw_time).timestamp(), (row.get("方向") or "rx").lower(),
                        ))
                if not frames:
                    raise ValueError("文件中没有可回放的 CAN 帧")
                if len(frames) > 2_000_000:
                    raise ValueError("CSV超过200万帧，请改用.bmslog记录长时间数据")
                for frame in frames:
                    self._validate_replay_frame(frame)
                frames.sort(key=lambda frame: frame.timestamp)
                first = frames[0].timestamp
                relative = [max(0.0, frame.timestamp - first) for frame in frames]
                total = len(frames)
                duration = relative[-1]
                inferred_profile = "can1" if any(is_can1(frame) for frame in frames) else "canb"

            profile = metadata.get("bus_profile") or inferred_profile
            if profile not in {"can1", "canb", "canb_legacy"}:
                profile = inferred_profile
            bitrate = int(metadata.get("bitrate") or (250000 if profile == "canb_legacy" else 500000))
            if bitrate not in {250000, 500000}:
                bitrate = 250000 if profile == "canb_legacy" else 500000
            self.disconnect()
            with self.lock:
                self.protocol = BmsProtocol(clock=lambda: self.replay_position)
                self.replay_frames = frames
                self.replay_relative = relative
                self.replay_db = database
                database = None
                self.replay_index = 0
                self.replay_position = 0.0
                self.replay_speed = 1.0
                self.replay_paused = False
                self.replay_first_timestamp = first
                self.replay_duration = duration
                self.replay_total = total
                self.replay_next_seq = 1
                self.replay_db_buffer.clear()
                self.connection.update({
                    "connected": True, "mode": "replay", "channel": source.name,
                    "bitrate": bitrate, "bus_profile": profile,
                    "status": "历史回放", "error": None,
                })
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._replay_loop, name="bms-log-replay", daemon=True)
            self.worker.start()
            return {"ok": True, "frames": total, "duration": duration, "path": str(path)}
        except Exception as exc:
            if database is not None:
                try:
                    database.close()
                except Exception:
                    pass
            return {"ok": False, "error": str(exc)}

    def replay_control(self, action: str, value: float | None = None) -> dict[str, Any]:
        with self.lock:
            if self.connection.get("mode") != "replay":
                return {"ok": False, "error": "当前没有历史回放"}
            if action == "pause":
                self.replay_paused = True
            elif action == "play":
                if self.replay_index >= self.replay_total:
                    self._rebuild_replay(0.0)
                self.replay_paused = False
                self.connection["status"] = "历史回放"
            elif action == "speed":
                speed = float(value or 1.0)
                if speed not in {0.25, 0.5, 1.0, 2.0, 5.0, 10.0}:
                    return {"ok": False, "error": "不支持的回放速度"}
                self.replay_speed = speed
            elif action == "seek":
                self._rebuild_replay(max(0.0, min(self.replay_duration, float(value or 0.0))))
            else:
                return {"ok": False, "error": "未知回放操作"}
            return {"ok": True}

    def start_recording(self, path: str) -> dict[str, Any]:
        with self.lock:
            if not self.connection.get("connected"):
                return {"ok": False, "error": "连接 CAN 或启动模拟数据后才能记录"}
            if self.connection.get("mode") == "replay":
                return {"ok": False, "error": "历史回放期间不能开始新的数据记录"}
            return self._start_recording_locked(path)

    def _start_recording_locked(self, path: str) -> dict[str, Any]:
        try:
            target = Path(path).expanduser()
            if target.suffix.lower() not in {".csv", ".bmslog"}:
                target = target.with_suffix(".bmslog")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.stop_recording()
            self.record_path = str(target)
            if target.suffix.lower() == ".csv":
                self.record_kind = "csv"
                self.record_file = target.open("w", newline="", encoding="utf-8-sig")
                self.record_writer = csv.writer(self.record_file)
                self.record_writer.writerow(["本地时间", "方向", "ID", "帧类型", "DLC", "数据", "说明"])
            else:
                self.record_kind = "bmslog"
                self.record_db = sqlite3.connect(target, check_same_thread=False)
                self.record_db.executescript("""
                    PRAGMA synchronous=NORMAL;
                    DROP TABLE IF EXISTS meta;
                    DROP TABLE IF EXISTS frames;
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE frames (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        direction TEXT NOT NULL,
                        arbitration_id INTEGER NOT NULL,
                        extended INTEGER NOT NULL,
                        data BLOB NOT NULL
                    );
                    CREATE INDEX frames_timestamp_idx ON frames(timestamp);
                    CREATE INDEX frames_id_timestamp_idx ON frames(arbitration_id, timestamp);
                """)
                metadata = {
                    "format": "BITFSAE_BMS_LOG", "schema_version": "1",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "bus_profile": str(self.connection.get("bus_profile") or ""),
                    "channel": str(self.connection.get("channel") or ""),
                    "bitrate": str(self.connection.get("bitrate") or ""),
                }
                self.record_db.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", metadata.items())
                self.record_db.commit()
                self.record_last_commit = time.monotonic()
                self.record_pending = 0
            return {"ok": True, "path": str(target), "format": self.record_kind}
        except Exception as exc:
            self.stop_recording()
            return {"ok": False, "error": str(exc)}

    def stop_recording(self) -> dict[str, Any]:
        with self.lock:
            return self._stop_recording_locked()

    def _stop_recording_locked(self) -> dict[str, Any]:
        if self.record_file:
            try:
                self.record_file.close()
            except Exception:
                pass
        if self.record_db:
            try:
                self.record_db.commit()
                self.record_db.close()
            except Exception:
                pass
        self.record_file = None
        self.record_writer = None
        self.record_db = None
        self.record_kind = None
        self.record_path = None
        self.record_pending = 0
        return {"ok": True}

    def _receive_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.bus.recv(timeout=0.1)
                if message is None:
                    continue
                frame = CanFrame(message.arbitration_id, bytes(message.data), message.is_extended_id,
                                 float(message.timestamp or time.time()), "rx")
                self._ingest(frame)
                if (not frame.is_extended_id and frame.data
                        and frame.data[0] in IVT_RESPONSE_MUXES):
                    with self.ivt_rx_condition:
                        self.ivt_rx_frames.append(IvtFrame(frame.arbitration_id, frame.data, frame.is_extended_id))
                        self.ivt_rx_condition.notify_all()
            except Exception as exc:
                with self.lock:
                    self.connection.update({"status": "接收异常", "error": str(exc)})
                if self.stop_event.wait(0.2):
                    break

    def _replay_loop(self) -> None:
        previous_clock = time.monotonic()
        while not self.stop_event.wait(0.01):
            try:
                with self.lock:
                    now = time.monotonic()
                    elapsed = now - previous_clock
                    previous_clock = now
                    if self.replay_paused:
                        continue
                    self.replay_position = min(self.replay_duration, self.replay_position + elapsed * self.replay_speed)
                    if self.replay_db is not None:
                        self._advance_database_replay()
                    else:
                        while (self.replay_index < len(self.replay_frames)
                               and self.replay_relative[self.replay_index] <= self.replay_position):
                            self.protocol.ingest(self.replay_frames[self.replay_index])
                            self.replay_index += 1
                    if self.replay_index >= self.replay_total:
                        self.replay_paused = True
                        self.connection["status"] = "回放结束"
            except Exception as exc:
                with self.lock:
                    self.replay_paused = True
                    self.connection.update({"status": "回放异常", "error": str(exc)})
                return

    def _rebuild_replay(self, position: float) -> None:
        if self.replay_db is not None:
            self._rebuild_database_replay(position)
            return
        self.replay_position = 0.0
        self.protocol = BmsProtocol(clock=lambda: self.replay_position)
        self.replay_index = 0
        while (self.replay_index < len(self.replay_frames)
               and self.replay_relative[self.replay_index] <= position):
            self.replay_position = self.replay_relative[self.replay_index]
            self.protocol.ingest(self.replay_frames[self.replay_index])
            self.replay_index += 1
        self.replay_position = position

    @staticmethod
    def _validate_replay_frame(frame: CanFrame) -> None:
        limit = 0x1FFFFFFF if frame.is_extended_id else 0x7FF
        if not 0 <= frame.arbitration_id <= limit:
            raise ValueError("记录中存在超出范围的 CAN ID")
        if len(frame.data) > 8:
            raise ValueError("记录包含 CAN FD 或损坏帧；当前程序只支持经典 CAN DLC 0..8")
        if frame.direction not in {"rx", "tx"}:
            raise ValueError("记录中存在未知收发方向")
        if not math.isfinite(frame.timestamp):
            raise ValueError("记录中存在无效时间")

    @classmethod
    def _validate_bmslog_database(cls, database: sqlite3.Connection) -> tuple[int, float, float, list[CanFrame]]:
        """Validate every native-log row before declaring the file loadable."""
        total = 0
        first: float | None = None
        last: float | None = None
        previous_seq = 0
        previous_timestamp: float | None = None
        sample: list[CanFrame] = []
        sample_keys: set[tuple[int, bool]] = set()
        try:
            rows = database.execute(
                "SELECT seq, timestamp, direction, arbitration_id, extended, data "
                "FROM frames ORDER BY seq"
            )
            for row in rows:
                seq, timestamp, direction, arbitration_id, extended, data = row
                if not isinstance(seq, int) or seq != previous_seq + 1:
                    raise ValueError("BMSLOG 帧序号不连续或无效")
                if extended not in (0, 1):
                    raise ValueError("BMSLOG 帧类型字段无效")
                try:
                    frame = CanFrame(int(arbitration_id), bytes(data), bool(extended),
                                     float(timestamp), str(direction))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("BMSLOG 帧记录字段损坏") from exc
                cls._validate_replay_frame(frame)
                if previous_timestamp is not None and frame.timestamp < previous_timestamp:
                    raise ValueError("BMSLOG 时间戳未按记录顺序递增")
                previous_seq = seq
                previous_timestamp = frame.timestamp
                if first is None:
                    first = frame.timestamp
                last = frame.timestamp
                total += 1
                key = (frame.arbitration_id, frame.is_extended_id)
                if key not in sample_keys and len(sample) < 512:
                    sample_keys.add(key)
                    sample.append(CanFrame(frame.arbitration_id, b"", frame.is_extended_id))
        except sqlite3.Error as exc:
            raise ValueError("BMSLOG 帧表无法读取") from exc
        if total == 0 or first is None or last is None:
            raise ValueError("文件中没有可回放的 CAN 帧")
        if not math.isfinite(first) or not math.isfinite(last) or last < first:
            raise ValueError("数据记录时间字段无效")
        return total, first, last, sample

    @staticmethod
    def _frame_from_db_row(row: tuple[Any, ...]) -> tuple[int, CanFrame]:
        seq, timestamp, direction, arbitration_id, extended, data = row
        frame = CanFrame(int(arbitration_id), bytes(data), bool(extended), float(timestamp), str(direction))
        CanService._validate_replay_frame(frame)
        return int(seq), frame

    def _fill_database_buffer(self) -> None:
        if self.replay_db is None or self.replay_db_buffer:
            return
        rows = self.replay_db.execute(
            "SELECT seq, timestamp, direction, arbitration_id, extended, data "
            "FROM frames WHERE seq >= ? ORDER BY seq LIMIT 2000",
            (self.replay_next_seq,),
        ).fetchall()
        self.replay_db_buffer.extend(rows)

    def _advance_database_replay(self) -> None:
        self._fill_database_buffer()
        target = self.replay_first_timestamp + self.replay_position
        while self.replay_db_buffer and float(self.replay_db_buffer[0][1]) <= target:
            seq, frame = self._frame_from_db_row(self.replay_db_buffer.popleft())
            self.protocol.ingest(frame)
            self.replay_next_seq = seq + 1
            self.replay_index += 1
            if not self.replay_db_buffer:
                self._fill_database_buffer()

    def _rebuild_database_replay(self, position: float) -> None:
        if self.replay_db is None:
            return
        target = self.replay_first_timestamp + position
        recent_start = max(self.replay_first_timestamp, target - 130.0)
        self.replay_position = 0.0
        self.protocol = BmsProtocol(clock=lambda: self.replay_position)

        fault_rows = self.replay_db.execute(
            "SELECT seq, timestamp, direction, arbitration_id, extended, data FROM frames "
            "WHERE timestamp < ? AND direction = 'rx' AND arbitration_id IN (?, ?) ORDER BY seq",
            (recent_start, 0x187650F4, 0x4B1),
        )
        for row in fault_rows:
            _, frame = self._frame_from_db_row(row)
            self.replay_position = max(0.0, frame.timestamp - self.replay_first_timestamp)
            self.protocol.ingest(frame)

        for can_id in (0x186B50F4, 0x186C50F4, 0x186C51F4, 0x186D50F4, 0x18A450F4):
            prior = self.replay_db.execute(
                "SELECT seq, timestamp, direction, arbitration_id, extended, data FROM frames "
                "WHERE timestamp < ? AND direction = 'rx' AND arbitration_id = ? ORDER BY seq DESC LIMIT 1",
                (recent_start, can_id),
            ).fetchone()
            if prior is not None:
                _, frame = self._frame_from_db_row(prior)
                self.replay_position = max(0.0, frame.timestamp - self.replay_first_timestamp)
                self.protocol.ingest(frame)

        recent_rows = self.replay_db.execute(
            "SELECT seq, timestamp, direction, arbitration_id, extended, data FROM frames "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY seq",
            (recent_start, target),
        )
        for row in recent_rows:
            _, frame = self._frame_from_db_row(row)
            self.replay_position = max(0.0, frame.timestamp - self.replay_first_timestamp)
            self.protocol.ingest(frame)

        self.replay_index = int(self.replay_db.execute(
            "SELECT COUNT(*) FROM frames WHERE timestamp <= ?", (target,)
        ).fetchone()[0])
        next_row = self.replay_db.execute(
            "SELECT MIN(seq) FROM frames WHERE timestamp > ?", (target,)
        ).fetchone()[0]
        self.replay_next_seq = int(next_row) if next_row is not None else self.replay_total + 1
        counts = dict(self.replay_db.execute(
            "SELECT direction, COUNT(*) FROM frames WHERE timestamp <= ? GROUP BY direction", (target,)
        ))
        self.protocol.rx_count = int(counts.get("rx", 0))
        self.protocol.tx_count = int(counts.get("tx", 0))
        self.replay_db_buffer.clear()
        self.replay_position = position

    def _ingest(self, frame: CanFrame) -> None:
        with self.lock:
            self.protocol.ingest(frame)
            self._record(frame)

    def _record(self, frame: CanFrame) -> None:
        if self.record_db:
            self.record_db.execute(
                "INSERT INTO frames(timestamp, direction, arbitration_id, extended, data) VALUES (?, ?, ?, ?, ?)",
                (frame.timestamp, frame.direction, frame.arbitration_id, int(frame.is_extended_id), frame.data),
            )
            self.record_pending += 1
            now = time.monotonic()
            if self.record_pending >= 100 or now - self.record_last_commit >= 0.5:
                self.record_db.commit()
                self.record_pending = 0
                self.record_last_commit = now
            return
        if not self.record_writer:
            return
        from .protocol import frame_name
        self.record_writer.writerow([
            datetime.fromtimestamp(frame.timestamp).isoformat(timespec="milliseconds"), frame.direction,
            f"0x{frame.arbitration_id:08X}" if frame.is_extended_id else f"0x{frame.arbitration_id:03X}",
            "扩展" if frame.is_extended_id else "标准", len(frame.data),
            " ".join(f"{byte:02X}" for byte in frame.data), frame_name(frame.arbitration_id, frame.is_extended_id),
        ])
        self.record_file.flush()
