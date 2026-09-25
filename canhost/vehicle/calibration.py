"""FanController calibration session manager and automated sweep runner.

Runs controlled calibration sweeps over PWM1 (dual 2H4PU) and PWM2 (single 2H6P),
measures baseline PDM bus power/current, samples steady-state RPM and delta I/P,
and strictly enforces safety gating (DCDC_READY, temperature and electrical limits).
"""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
from typing import Any, Callable


# 故障位定义，与固件 fan_controller.c 的 FAN_FAULT_* 保持一致。
FAULT_TACH_MASK = 0x07          # bit0..2：TACH1/TACH2/TACH3 停转或无信号
FAULT_MOTOR_TEMP_STALE = 0x08   # bit3：0x506 电机温度失联
FAULT_CTRL_TEMP_STALE = 0x10    # bit4：0x507/0x508 逆变器或 IGBT 温度失联

# DCDC 识别判据，必须与固件 fan_controller.c 的 FAN_DCDC_DETECT_* 保持一致：
#   总线电压 - 电池电压 >= 300mV，且电池支路放电 <= 0.5A（正 = 放电）。
# 上位机用 PDM 原始测量值独立判定，不直接采信固件上报的供电状态，
# 这样 Action=4 手动覆盖也不会被误认为自动识别结果。
DCDC_DETECT_VDIFF_V = 0.30
DCDC_DETECT_IBAT_MAX_A = 0.50
# 计划 11.1：POWER_DCDC_READY 必须稳定至少 3 秒才允许标定。
DCDC_STABLE_REQUIRED_S = 3.0
# 开始标定前允许的整车基础总线电流：三台风扇满载合计约 9.4A，
# 18A 上限下必须先给风扇留出足够空间，否则扫到高占空比时必然触发保护。
CALIB_MAX_START_BUS_CURRENT_A = 8.0
# 温度上限：低于固件的临界温度，给标定过程留出余量。
CALIB_MAX_MOTOR_TEMP_C = 70.0
CALIB_MAX_CONTROLLER_TEMP_C = 65.0
CALIB_ABORT_MOTOR_TEMP_C = 72.0
CALIB_ABORT_CONTROLLER_TEMP_C = 68.0
# 推荐上限按固件“正常预算”目标计算，而不是按快速限/硬限计算：
# 预算给测量误差和背景负载留出余量，8A/18A 是正常控制目标。
FAN_CALIB_BATTERY_CAP_CURRENT_A = 8.0
FAN_CALIB_DCDC_CAP_CURRENT_A = 18.0

# PDM 读数稳定性只用于判断测点是否适合生成推荐上限，不是安全保护。
# 整车背景负载会有正常波动；超过该门槛时保留并标记测量结果、继续扫频，
# 但不让该点进入自动推荐。真正需要立即停止的条件仍由 _watchdog/
# _safety_error 中的电流、温度、供电和遥测新鲜度硬门槛负责。
CALIB_QUALITY_MIN_SAMPLES = 10
CALIB_QUALITY_MAX_STD_CURRENT_A = 0.10
CALIB_QUALITY_MAX_STD_POWER_W = 3.0

# 自动标定中的通信数据窗口。周期最慢的 0x5A3 为 500 ms；1.5 s 允许
# 连续丢失两帧而不立刻销毁整轮标定。过期后进入安全暂停，恢复后重做当前点。
CALIB_TELEMETRY_TIMEOUT_S = 1.5
CALIB_STATUS_CONFIRM_TIMEOUT_S = 1.5
CALIB_RECOVERY_STABLE_S = 1.0
CALIB_COMMAND_LEASE_S = 60
CALIB_LEASE_HEARTBEAT_S = 5.0
CALIB_SAMPLE_RETRY_LIMIT = 3
RECOVERABLE_FIRMWARE_ABORT_REASONS = {2, 6}  # PDM遥测超时、租约超时


def _fresh_age(value: Any, limit_s: float) -> bool:
    """Return True only for a finite, non-negative telemetry age."""
    return (isinstance(value, (int, float)) and math.isfinite(value)
            and 0.0 <= float(value) <= limit_s)


class FanCalibrationSession:
    """Manages automated or step-by-step fan calibration sweeps."""

    DEFAULT_STEPS = [0, 5, 8, 10, 12, 15, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
    DEFAULT_HOLD_S = 6.0
    SETTLE_S = 3.0
    SAMPLE_S = 3.0
    BASELINE_INTERVAL = 4
    DCDC_STABLE_REQUIRED_S = DCDC_STABLE_REQUIRED_S

    def __init__(self, send_fn: Callable[[str, dict[str, Any], bool], dict[str, Any]],
                 snapshot_fn: Callable[[], dict[str, Any]],
                 diagnostic_dir: Path | None = None) -> None:
        self.send_fn = send_fn
        self.snapshot_fn = snapshot_fn
        # CanService.vehicle_snapshot()会在持有本锁时再次调用get_snapshot()。
        # 使用可重入锁避免标定启动自死锁，同时保留worker/snapshot之间的互斥。
        self.lock = threading.RLock()
        self.status: str = "idle"  # "idle", "running", "aborted", "completed"
        self.abort_reason: str = ""
        self.channel: int = 1  # 1 (PWM1), 2 (PWM2), or 3 (both)
        self.tier: str = "dcdc"
        self.current_step: int = 0
        self.total_steps: int = 0
        self.current_duty: list[int] = [0, 0]
        self._command_step: int = 0
        self._command_duties: list[int] = [0, 0]
        self.baseline: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []
        self.quality_warnings: list[dict[str, Any]] = []
        self.pause_reason: str = ""
        self.pause_started_at: float | None = None
        self.recovery_count: int = 0
        self.last_diagnostic: dict[str, Any] = {}
        self.last_diagnostic_path: str = ""
        self.run_params: dict[str, Any] = {}
        self.raw_samples: list[dict[str, Any]] = []
        self.baseline_raw_samples: list[dict[str, Any]] = []
        # 每次 0% 基线的汇总（含 baseline_id/方向/步骤），用于复核每条记录关联的基线；
        # baseline_raw_samples 是追加保存的逐样本原始数据。
        self.baseline_history: list[dict[str, Any]] = []
        self.baseline_id: int = 0
        # None 表示该档位还没有完成过可用的扫频数据；完成后再填充。
        self.suggested_caps: dict[str, int | None] = {
            "battery_cap_pct": None,
            "dcdc_cap_pct": None,
        }
        # 两路输出共用同一个档位上限；必须分别测得两个物理回路后，才能给出
        # 完整推荐值，避免只测较轻负载回路就保存一个过高上限。
        self.channel_caps: dict[str, dict[int, int | None]] = {
            "battery": {1: None, 2: None},
            "dcdc": {1: None, 2: None},
        }
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lease_stop_event = threading.Event()
        self._firmware_started = threading.Event()
        self._command_lock = threading.RLock()
        self._lease_thread: threading.Thread | None = None
        self._diagnostic_dir = Path(diagnostic_dir) if diagnostic_dir is not None else None
        # Manual abort, watchdog abort and normal completion may converge at
        # nearly the same time.  Serialize their stop/restore sequence so the
        # controller never receives two competing terminal command pairs.
        self._stop_lock = threading.RLock()

    @staticmethod
    def _max_safe_duty(records: list[dict[str, Any]], tier: str) -> int | None:
        """根据当前档位扫频记录，返回不超过电流预算的最大安全占空比。

        固件预算限制的是 PDM 总线总电流（已包含整车背景负载），因此使用每条
        记录的 current_a 直接与 8.0A/18.0A 比较；同时按被扫回路的实际转速确认
        风扇已经正常运行。没有任何可用点时不生成推荐值，避免把无数据误当成
        15% 的安全建议。
        """
        if tier == "battery":
            threshold = FAN_CALIB_BATTERY_CAP_CURRENT_A
        elif tier == "dcdc":
            threshold = FAN_CALIB_DCDC_CAP_CURRENT_A
        else:
            return None

        observations: dict[int, list[tuple[bool, bool]]] = {}
        for rec in records:
            if rec.get("tier") != tier:
                continue
            try:
                channel = int(rec.get("channel") or 0)
                duty_value = float(rec.get(f"duty{channel}_pct") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if channel not in (1, 2):
                continue
            if not math.isfinite(duty_value) or not duty_value.is_integer():
                continue
            duty = int(duty_value)
            if not 0 < duty <= 100:
                continue
            current = rec.get("current_a")
            quality_ok = rec.get("quality_ok", True) is not False
            current_ok = (quality_ok and isinstance(current, (int, float))
                          and math.isfinite(current) and current <= threshold)
            if channel == 1:
                rpm_values = (rec.get("rpm1"), rec.get("rpm2"))
            else:
                rpm_values = (rec.get("rpm3"),)
            rpm_ok = all(isinstance(rpm, (int, float)) and math.isfinite(rpm) and rpm > 0
                         for rpm in rpm_values)
            observations.setdefault(duty, []).append((current_ok, rpm_ok))

        # A higher point is not allowed to "jump over" a failed lower point.
        # A non-rotating low-duty startup dead zone may be skipped before the
        # first rotating point; once rotation is established, every higher
        # tested point and both up/down observations must pass.
        max_safe: int | None = None
        for duty in sorted(observations):
            duty_observations = observations[duty]
            if not duty_observations or not all(item[0] for item in duty_observations):
                break
            if all(item[1] for item in duty_observations):
                max_safe = duty
            elif max_safe is not None:
                break
        return max_safe

    def check_preconditions(self) -> dict[str, Any]:
        """Verify bus and vehicle safety conditions before calibration."""
        snap = self.snapshot_fn()
        conn = snap.get("connection", {})
        if not conn.get("connected", False) or conn.get("mode") != "pcan":
            return {"ok": False, "error": "请先连接真实 PCAN 上的 CANB，禁止模拟连接发送标定命令"}
        if conn.get("bus_profile") != "canb" or conn.get("bitrate") != 500000:
            return {"ok": False, "error": "风扇标定只允许使用整车 CANB 500 kbit/s"}

        fan = snap.get("fan", {})
        fan_status = fan.get("status", {})
        fan_diag = fan.get("diagnostic", {})
        pdm = snap.get("pdm", {})
        bus = pdm.get("bus", {})
        power_status = fan.get("power_status", {})

        pdm_age = bus.get("age")
        fan_status_age = fan.get("status_age")
        fan_diag_age = fan.get("diagnostic_age")
        power_status_age = fan.get("power_status_age")
        limits_age = fan.get("calib_limits_age")
        if not _fresh_age(pdm_age, CALIB_TELEMETRY_TIMEOUT_S) or bus.get("offline", True):
            return {"ok": False, "error": "PDM 低压总线遥测离线或超时（>1.5s），无法进行标定"}
        if not _fresh_age(fan_status_age, CALIB_TELEMETRY_TIMEOUT_S):
            return {"ok": False, "error": "FanController 0x5A2 状态超时（>1.5s），无法进行标定"}
        if not _fresh_age(fan_diag_age, CALIB_TELEMETRY_TIMEOUT_S):
            return {"ok": False, "error": "FanController 0x5A3 诊断超时（>1.5s），无法进行标定"}
        if not _fresh_age(power_status_age, CALIB_TELEMETRY_TIMEOUT_S):
            return {"ok": False, "error": "FanController 0x5A8 功率状态超时（>1.5s），无法进行标定"}
        if not _fresh_age(limits_age, 1.5):
            return {"ok": False, "error": "FanController 0x5AE 标定上限状态超时（>1.5s），无法确认协议版本"}
        if fan.get("calib_limits", {}).get("protocol_version") != 3:
            return {"ok": False, "error": "FanController 标定协议版本不匹配（0x5AE 必须为版本 3）"}

        power_supply_state = power_status.get("power_supply_state")
        if power_supply_state not in (1, 2, 3):
            # 固件自动识别未启用时，允许在发送 Action=4 确认后再进入 DCDC_READY。
            state_name = power_status.get("power_supply_name", str(power_supply_state))
            return {"ok": False, "error": f"供电状态异常（当前：{state_name}），禁止开始标定"}

        firmware_calib = fan.get("calib_status", {})
        firmware_calib_age = fan.get("calib_status_age")
        if (_fresh_age(firmware_calib_age, CALIB_TELEMETRY_TIMEOUT_S)
                and firmware_calib.get("calib_state") == 1):
            return {"ok": False, "error": "FanController 已有活动标定会话，请先安全中止后再开始"}

        faults = fan_diag.get("faults", 0)
        # 温度失联期间不能标定：固件安全看门狗只在温度“新鲜且超温”时中止，
        # 失联时标定会在完全没有温度保护的情况下运行。
        if faults & (FAULT_MOTOR_TEMP_STALE | FAULT_CTRL_TEMP_STALE):
            stale_names = []
            if faults & FAULT_MOTOR_TEMP_STALE:
                stale_names.append("电机温度(0x506)")
            if faults & FAULT_CTRL_TEMP_STALE:
                stale_names.append("控制器温度(0x507/0x508)")
            return {"ok": False, "error": "温度输入失联（" + "、".join(stale_names) + "），禁止开启标定"}

        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        # 温度为 None 表示 0x5A3 上报了 0x7FFF，等同于该路温度不可用，必须拒绝。
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (motor_temp, ctrl_temp)):
            return {"ok": False, "error": "电机或控制器温度无效（0x5A3 上报 0x7FFF），禁止开启标定"}
        if motor_temp >= CALIB_MAX_MOTOR_TEMP_C:
            return {"ok": False, "error": f"电机温度过高 ({motor_temp:.1f} ℃ >= {CALIB_MAX_MOTOR_TEMP_C:.0f} ℃)，禁止开启标定"}
        if ctrl_temp >= CALIB_MAX_CONTROLLER_TEMP_C:
            return {"ok": False, "error": f"控制器温度过高 ({ctrl_temp:.1f} ℃ >= {CALIB_MAX_CONTROLLER_TEMP_C:.0f} ℃)，禁止开启标定"}

        if faults & FAULT_TACH_MASK:
            return {"ok": False, "error": "存在风扇停转故障，请先排除硬件问题"}

        return {"ok": True}

    @staticmethod
    def _dcdc_ready_by_measurement(snap: dict[str, Any]) -> tuple[bool, str]:
        """用 PDM 双路原始测量值判断 DCDC 是否真的在供电。

        与固件 FAN_DCDC_DETECT_* 判据一致，但不读取固件上报的供电状态，
        因此 Action=4 手动覆盖不会让这里误判为自动识别结果。
        """
        pdm = snap.get("pdm", {})
        bus = pdm.get("bus", {})
        battery = pdm.get("battery", {})
        if bus.get("offline", True) or battery.get("offline", True):
            return False, "PDM 总线或电池支路遥测离线"
        bus_age = bus.get("age")
        bat_age = battery.get("age")
        if (not _fresh_age(bus_age, CALIB_TELEMETRY_TIMEOUT_S)
                or not _fresh_age(bat_age, CALIB_TELEMETRY_TIMEOUT_S)):
            return False, "PDM 双路遥测超时（要求两路都 <= 1.5s）"
        v_bus = bus.get("voltage_v")
        v_bat = battery.get("voltage_v")
        i_bat = battery.get("current_a")
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (v_bus, v_bat, i_bat)):
            return False, "PDM 双路电压或电流无效"
        if (v_bus - v_bat) < DCDC_DETECT_VDIFF_V:
            return False, (f"总线与电池电压差仅 {(v_bus - v_bat) * 1000.0:.0f} mV"
                           f"（要求 >= {DCDC_DETECT_VDIFF_V * 1000.0:.0f} mV）")
        if i_bat > DCDC_DETECT_IBAT_MAX_A:
            return False, (f"电池支路仍在放电 {i_bat:.2f} A"
                           f"（要求 <= {DCDC_DETECT_IBAT_MAX_A:.2f} A）")
        return True, ""

    def _verify_dcdc_ready(self, stable_s: float = DCDC_STABLE_REQUIRED_S) -> dict[str, Any]:
        """连续 stable_s 秒用 PDM 实测判据确认 DCDC 已稳定接管。"""
        deadline = time.monotonic() + stable_s + 1.0
        stable_since: float | None = None
        last_error = "超时"
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return {"ok": False, "error": "标定已停止，DCDC 就绪验证中断"}
            ok, error = self._dcdc_ready_by_measurement(self.snapshot_fn())
            if not ok:
                stable_since = None
                last_error = error
            elif stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return {"ok": True}
            time.sleep(0.1)
        return {"ok": False, "error":
                f"DCDC 就绪判据未连续稳定 {stable_s:.0f} 秒（{last_error}），禁止开始自动扫频"}

    def _set_pause(self, reason: str, snapshot: dict[str, Any] | None = None) -> None:
        """Expose a recoverable communication pause without ending the session."""
        with self.lock:
            if not self.pause_reason:
                self.pause_started_at = time.time()
                self.recovery_count += 1
            self.pause_reason = reason
        if snapshot is not None:
            self._capture_diagnostic("paused", reason, snapshot)

    def _clear_pause(self) -> None:
        with self.lock:
            self.pause_reason = ""
            self.pause_started_at = None

    def _capture_diagnostic(self, event: str, reason: str,
                            snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snap = snapshot if snapshot is not None else self.snapshot_fn()
        fan = snap.get("fan", {})
        pdm = snap.get("pdm", {})
        with self.lock:
            payload = {
                "event": event,
                "reason": reason,
                "captured_at": datetime.now().astimezone().isoformat(),
                "session": {
                    "status": self.status,
                    "channel": self.channel,
                    "tier": self.tier,
                    "current_step": self.current_step,
                    "total_steps": self.total_steps,
                    "current_duty": list(self.current_duty),
                    "pause_reason": self.pause_reason,
                    "recovery_count": self.recovery_count,
                    "run_params": dict(self.run_params),
                    "record_count": len(self.records),
                },
                "connection": dict(snap.get("connection", {})),
                "fan": {
                    "status_age": fan.get("status_age"),
                    "diagnostic_age": fan.get("diagnostic_age"),
                    "power_status_age": fan.get("power_status_age"),
                    "calib_status_age": fan.get("calib_status_age"),
                    "status": dict(fan.get("status", {})),
                    "diagnostic": dict(fan.get("diagnostic", {})),
                    "power_status": dict(fan.get("power_status", {})),
                    "calib_status": dict(fan.get("calib_status", {})),
                },
                "pdm": {
                    "bus": dict(pdm.get("bus", {})),
                    "battery": dict(pdm.get("battery", {})),
                },
            }
            self.last_diagnostic = payload
        return payload

    def _persist_terminal_diagnostic(self, reason: str,
                                     snapshot: dict[str, Any] | None = None) -> None:
        """Atomically keep the exact terminal evidence even if the UI is closed."""
        payload = self._capture_diagnostic("aborted", reason, snapshot)
        if self._diagnostic_dir is None:
            return
        try:
            self._diagnostic_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
            target = self._diagnostic_dir / f"fan_calibration_abort_{stamp}.json"
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, target)
            with self.lock:
                self.last_diagnostic_path = str(target)
        except OSError as exc:
            with self.lock:
                self.last_diagnostic_path = f"诊断快照写入失败：{exc}"

    def _send_calib_command(self, action: int, step: int, duty1: int, duty2: int,
                            lease_s: int = CALIB_COMMAND_LEASE_S
                            ) -> tuple[dict[str, Any], int]:
        """Serialize scan/heartbeat commands and capture the post-ACK generation."""
        with self._command_lock:
            result = self.send_fn("fan_calib", {
                "action": action, "step": step,
                "duty1_pct": duty1, "duty2_pct": duty2,
                "lease_s": lease_s if action in {1, 2} else 0,
            }, True)
            generation = self._calib_generation()
            if result.get("ok") and action in {1, 2}:
                with self.lock:
                    self._command_step = step
                    self._command_duties = [duty1, duty2]
                self._firmware_started.set()
            elif action == 3:
                self._firmware_started.clear()
            return result, generation

    def _lease_heartbeat_loop(self) -> None:
        while not self._lease_stop_event.wait(CALIB_LEASE_HEARTBEAT_S):
            try:
                result = self._renew_lease_once()
                if result is not None and not result.get("ok"):
                    reason = self._communication_pause_reason(self.snapshot_fn())
                    if reason:
                        self._set_pause(reason)
            except Exception:
                reason = self._communication_pause_reason(self.snapshot_fn())
                if reason:
                    self._set_pause(reason)

    def _renew_lease_once(self) -> dict[str, Any] | None:
        """Renew the latest target without racing a point-recovery command.

        The command lock must be acquired before reading pause/target state.
        Otherwise a heartbeat can cache the paused zero target, wait behind the
        recovery UPDATE, and then overwrite the just-confirmed scan target.
        """
        with self._command_lock:
            if self._stop_event.is_set() or not self._firmware_started.is_set():
                return None
            with self.lock:
                if self.status != "running":
                    return None
                step = self._command_step
                duties = ([0, 0] if self.pause_reason else list(self._command_duties))
            result, _ = self._send_calib_command(
                2, step, duties[0], duties[1], CALIB_COMMAND_LEASE_S)
            return result

    def _start_lease_heartbeat(self) -> None:
        self._lease_stop_event.clear()
        self._firmware_started.clear()
        self._lease_thread = threading.Thread(
            target=self._lease_heartbeat_loop,
            name="fan-calib-lease",
            daemon=True,
        )
        self._lease_thread.start()

    def _stop_lease_heartbeat(self) -> None:
        self._lease_stop_event.set()
        self._firmware_started.clear()
        worker = self._lease_thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=0.5)

    @staticmethod
    def _firmware_terminal_reason(calib: dict[str, Any]) -> str:
        state_name = calib.get("calib_state_name", calib.get("calib_state", "未知"))
        reason_code = calib.get("calib_abort_reason")
        reason_name = calib.get("calib_abort_name", "未知")
        return (f"FanController 标定会话{state_name}：{reason_name}"
                f"（原因码 {reason_code}，步骤 {calib.get('step', '未知')}，"
                f"目标 {calib.get('calib_target_pct', '未知')}）")

    @staticmethod
    def _communication_pause_reason(snap: dict[str, Any],
                                    require_calibration_active: bool = True,
                                    include_firmware_terminal: bool = True) -> str | None:
        fan = snap.get("fan", {})
        bus = snap.get("pdm", {}).get("bus", {})
        if bus.get("offline", True) or not _fresh_age(
                bus.get("age"), CALIB_TELEMETRY_TIMEOUT_S):
            return "PDM 遥测超过 1.5 s，已暂停采样并等待恢复"
        if not all(isinstance(bus.get(key), (int, float)) and math.isfinite(bus[key])
                   for key in ("voltage_v", "current_a", "power_w")):
            return "PDM 总线测量值无效，已暂停采样并等待恢复"
        frame_ages = (
            ("0x5A2 状态", fan.get("status_age")),
            ("0x5A3 诊断", fan.get("diagnostic_age")),
            ("0x5A8 功率状态", fan.get("power_status_age")),
        )
        for name, age in frame_ages:
            if not _fresh_age(age, CALIB_TELEMETRY_TIMEOUT_S):
                return f"FanController {name}超过 1.5 s，已暂停采样并等待恢复"
        diagnostic = fan.get("diagnostic", {})
        if diagnostic.get("faults", 0) & (FAULT_MOTOR_TEMP_STALE | FAULT_CTRL_TEMP_STALE):
            return "温度输入失联，已暂停标定采样并等待恢复"
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (diagnostic.get("motor_temp_c"),
                                 diagnostic.get("controller_temp_c"))):
            return "温度输入无效，已暂停标定采样并等待恢复"
        if fan.get("power_status", {}).get("power_supply_state") in {0, 2, 5, None}:
            return "供电状态正在切换或暂不可判定，已暂停采样并等待稳定"
        if require_calibration_active:
            calib = fan.get("calib_status", {})
            if not _fresh_age(fan.get("calib_status_age"), CALIB_TELEMETRY_TIMEOUT_S):
                return "FanController 0x5A9 标定状态超过 1.5 s，已暂停采样并等待恢复"
            if calib.get("output_paused"):
                return "FanController 因 PDM 数据短时失联暂停标定输出"
            if (include_firmware_terminal and calib.get("calib_state") == 2
                    and calib.get("calib_abort_reason") in RECOVERABLE_FIRMWARE_ABORT_REASONS):
                return ("FanController 已安全终止当前输出，可恢复原因："
                        f"{calib.get('calib_abort_name', '未知')}")
            if (include_firmware_terminal and calib.get("calib_state") == 1
                    and (not isinstance(calib.get("lease_remaining_s"), (int, float))
                         or calib.get("lease_remaining_s") <= 0)):
                return "FanController 标定租约待续，已暂停采样"
        return None

    def _wait_for_communication_recovery(
            self, initial_reason: str, max_current_a: float, expected_state: int,
            expected_step: int, expected_duties: tuple[int, int]) -> str | None:
        """Wait indefinitely for stable telemetry, then restart the current point."""
        self._set_pause(initial_reason, self.snapshot_fn())
        # Best-effort safe target for host-detected frame/temperature loss.
        # New FanController firmware already ignores the scan target during a
        # PDM pause; this command also protects old firmware and other frame-loss
        # cases. Failure is expected when the bus itself is unavailable.
        try:
            self._send_calib_command(
                2, expected_step, 0, 0, CALIB_COMMAND_LEASE_S)
        except Exception:
            pass
        stable_since: float | None = None
        while not self._stop_event.is_set():
            snap = self.snapshot_fn()
            conn = snap.get("connection", {})
            if (not conn.get("connected") or conn.get("mode") != "pcan"
                    or conn.get("bus_profile") != "canb"
                    or conn.get("bitrate") != 500000):
                return "整车 CANB 连接已完全断开，终止标定"

            transient = self._communication_pause_reason(
                snap, require_calibration_active=False,
                include_firmware_terminal=False)
            if transient:
                self._set_pause(transient)
                stable_since = None
                time.sleep(0.1)
                continue

            hard_error = self._watchdog(
                snap, max_current_a, expected_state,
                require_calibration_active=False)
            if hard_error:
                return hard_error

            calib = snap.get("fan", {}).get("calib_status", {})
            state = calib.get("calib_state")
            abort_reason = calib.get("calib_abort_reason")
            if state == 2 and abort_reason not in RECOVERABLE_FIRMWARE_ABORT_REASONS:
                return self._firmware_terminal_reason(calib)

            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            if now - stable_since < CALIB_RECOVERY_STABLE_S:
                time.sleep(0.1)
                continue

            # New firmware keeps the session ACTIVE while PDM is paused. Old
            # firmware reports ABORTED; START recreates only the current point.
            # Keep the heartbeat behind this complete UPDATE + 0x5A9 confirm
            # transaction. It must observe the cleared pause and new target,
            # never replay a zero target captured before recovery completed.
            with self._command_lock:
                action = 2 if state == 1 else 1
                result, generation = self._send_calib_command(
                    action, expected_step, expected_duties[0], expected_duties[1])
                if not result.get("ok"):
                    self._set_pause(
                        f"恢复当前测点命令暂未确认：{result.get('error', '无应答')}")
                    stable_since = None
                    time.sleep(0.2)
                    continue
                confirm_error = self._wait_for_calib_state(
                    1, expected_step, expected_duties[0], expected_duties[1],
                    after_generation=generation,
                    timeout_s=CALIB_STATUS_CONFIRM_TIMEOUT_S,
                    max_current_a=max_current_a,
                    expected_supply_state=expected_state)
                if confirm_error:
                    self._set_pause(f"恢复当前测点待确认：{confirm_error}")
                    stable_since = None
                    time.sleep(0.2)
                    continue
                self._clear_pause()
            return None
        return "连接或用户操作已中止标定"

    def start_sweep(self, channel: int = 1, steps: list[int] | None = None,
                    hold_s: float = DEFAULT_HOLD_S,
                    max_current_a: float = 18.0, tier: str = "dcdc") -> dict[str, Any]:
        """Start a background automated calibration sweep."""
        if channel not in (1, 2):
            return {"ok": False, "error": "计划要求先分别标定回路 1/2，暂不开放双回路联合扫频"}
        if tier not in {"battery", "dcdc"}:
            return {"ok": False, "error": "供电档位必须是 battery 或 dcdc"}
        try:
            hold_s = float(hold_s)
            max_current_a = float(max_current_a)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "稳态保持时间和总线电流保护必须是有效数字"}
        if not math.isfinite(hold_s) or not 3.0 <= hold_s <= 10.0:
            return {"ok": False, "error": "稳态保持时间必须在 3..10 秒（含 3s 稳定 + 采样窗口）"}
        if not math.isfinite(max_current_a) or not 5.0 <= max_current_a <= 20.0:
            return {"ok": False, "error": "总线电流保护必须在 5..20 A"}
        if steps is None:
            normalized_steps = list(self.DEFAULT_STEPS)
        else:
            try:
                numeric_steps = [float(value) for value in steps]
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": "扫描点必须是 0..100 % 的整数"}
            if any(not math.isfinite(value) or not value.is_integer() for value in numeric_steps):
                return {"ok": False, "error": "扫描点必须是 0..100 % 的整数"}
            normalized_steps = [int(value) for value in numeric_steps]
        if (not normalized_steps or any(not 0 <= duty <= 100 for duty in normalized_steps)
                or normalized_steps != sorted(set(normalized_steps))):
            return {"ok": False, "error": "扫描点必须是 0..100 % 内严格递增且不重复的整数"}

        # 避免已有 worker 运行时再次执行耗时的 DCDC 稳定性检查。
        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "标定会话已在进行中"}
            previous_worker = self._thread
        if (previous_worker and previous_worker.is_alive()
                and previous_worker is not threading.current_thread()):
            previous_worker.join(timeout=1.5)
        if previous_worker and previous_worker.is_alive():
            return {"ok": False, "error": "上一标定后台线程尚未安全退出，请稍后重试"}
        self._stop_lease_heartbeat()
        # Clear only after the prior worker is gone.  Clearing earlier could
        # revive that worker after an abort; clearing later could erase a
        # disconnect signal raised during the preflight window.
        self._stop_event.clear()
        # 前置检查（内部会经 vehicle_snapshot() -> get_snapshot()）必须放在
        # 本锁外，不能持有 session 锁去获取 service 锁，否则 service 侧
        # 同步调用会反过来获取 session 锁，形成跨锁死锁。
        pre_check = self.check_preconditions()
        if not pre_check["ok"]:
            return pre_check

        # 用 PDM 原始测量值独立验证 DCDC 真的在供电，并连续稳定 3 秒。
        # 不采信固件上报的供电状态本身：Action=4 手动覆盖也会上报同一个状态，
        # 只检查 power_supply_state == 3 无法区分“自动识别”和“人工覆盖”。
        if tier == "dcdc":
            dcdc_check = self._verify_dcdc_ready(self.DCDC_STABLE_REQUIRED_S)
            if not dcdc_check["ok"]:
                return dcdc_check

        # The DCDC proof takes several seconds.  Re-run every freshness and
        # connection check so a disconnect/profile change during that window
        # cannot launch a worker on cached data.
        pre_check = self.check_preconditions()
        if not pre_check["ok"]:
            return pre_check

        # 固件状态与实测不一致时同样拒绝：说明供电状态来自覆盖或已过期。
        launch_snapshot = self.snapshot_fn()
        current_state = launch_snapshot.get("fan", {}).get("power_status", {}).get("power_supply_state")
        expected_state = 3 if tier == "dcdc" else 1
        if current_state != expected_state:
            state_name = launch_snapshot.get("fan", {}).get("power_status", {}).get(
                "power_supply_name", str(current_state))
            return {
                "ok": False,
                "error": f"固件上报供电为 {state_name}，与所选标定档位不一致；禁止开始自动扫频。",
            }

        # 起始总线负载必须足够低，否则扫到高占空比时必然触发电流保护。
        start_current = launch_snapshot.get("pdm", {}).get("bus", {}).get("current_a")
        if not isinstance(start_current, (int, float)) or not math.isfinite(start_current):
            return {"ok": False, "error": "无法读取总线电流，禁止开始标定"}
        start_limit = min(CALIB_MAX_START_BUS_CURRENT_A, max_current_a)
        if start_current > start_limit:
            return {"ok": False, "error":
                    f"整车基础总线电流 {start_current:.2f} A 超过开始标定门槛 "
                    f"{start_limit:.1f} A，请先关闭其他低压负载或提高保护值"}

        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "标定会话已在进行中"}
            if self._stop_event.is_set():
                return {"ok": False, "error": "启动检查期间连接或会话被中止，请重新确认后再试"}

            self.status = "running"
            self.abort_reason = ""
            self.channel = channel
            self.tier = tier
            self.current_step = 0
            # 双向扫描执行 len(steps) * 2 个点。
            self.total_steps = len(normalized_steps) * 2
            self.current_duty = [0, 0]
            self._command_step = 0
            self._command_duties = [0, 0]
            self.pause_reason = ""
            self.pause_started_at = None
            self.recovery_count = 0
            self.last_diagnostic = {}
            self.last_diagnostic_path = ""
            self.baseline.clear()
            self.records.clear()
            # A rerun invalidates the previous result for this physical loop
            # immediately.  If the new scan aborts, stale recommendations must
            # not remain available for commit.
            self.channel_caps[tier][channel] = None
            cap_key = "battery_cap_pct" if tier == "battery" else "dcdc_cap_pct"
            self.suggested_caps[cap_key] = None
            self.run_params = {
                "channel": channel,
                "steps": list(normalized_steps),
                "hold_s": hold_s,
                "max_current_a": max_current_a,
                "tier": tier,
            }
            self.raw_samples.clear()
            self.baseline_raw_samples.clear()
            self.baseline_history.clear()
            self.quality_warnings.clear()
            self.baseline_id = 0
            self._thread = threading.Thread(
                target=self._run_sweep,
                args=(channel, normalized_steps, hold_s, max_current_a, tier),
                name="fan-calib-runner",
                daemon=True,
            )
            self._start_lease_heartbeat()
            self._thread.start()

        return {"ok": True, "message": "标定会话已启动",
                "total_steps": len(normalized_steps) * 2}

    def abort(self, reason: str = "用户手动停止") -> dict[str, Any]:
        """Abort any ongoing calibration immediately and restore AUTO mode."""
        with self._stop_lock:
            terminal_snapshot = self.snapshot_fn()
            with self.lock:
                if self.status != "running":
                    return {"ok": False, "status": self.status, "reason": self.abort_reason,
                            "errors": ["当前没有正在运行的整车风扇自动标定"]}
                self._stop_event.set()
                self.status = "aborted"
                self.abort_reason = reason
                self.pause_reason = ""

            self._stop_lease_heartbeat()
            send_result = self._stop_and_restore_auto()
            with self.lock:
                if not send_result["ok"]:
                    self.abort_reason = f"{reason}；固件恢复失败：{'；'.join(send_result['errors'])}"
                status = self.status
                abort_reason = self.abort_reason
            self._persist_terminal_diagnostic(abort_reason, terminal_snapshot)
            return {
                "ok": send_result["ok"],
                "status": status,
                "reason": abort_reason,
                "errors": send_result["errors"],
            }

    def cancel_for_disconnect(self) -> None:
        """Stop the host worker and invalidate results before the bus closes."""
        self._stop_event.set()
        self._stop_lease_heartbeat()
        with self.lock:
            was_running = self.status == "running"
            had_session = self.status in {"running", "completed", "aborted", "stale"}
            if was_running:
                self.status = "aborted"
                self.abort_reason = "整车 CANB 已断开；固件输出由标定租约到期自动归零"
            elif self.status in {"completed", "aborted"}:
                self.status = "stale"
                self.abort_reason = "连接已更换；旧记录仅供导出，推荐上限已作废"
            self.suggested_caps = {"battery_cap_pct": None, "dcdc_cap_pct": None}
            self.channel_caps = {
                "battery": {1: None, 2: None},
                "dcdc": {1: None, 2: None},
            }
            worker = self._thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.2)
        # The worker may have been between its final stop ACK and status update.
        # Reassert disconnect invalidation after the bounded join so it cannot
        # publish a recommendation belonging to the previous controller.
        with self.lock:
            if had_session:
                self.status = "aborted" if was_running else "stale"
                self.abort_reason = ("整车 CANB 已断开；固件输出由标定租约到期自动归零"
                                     if was_running else
                                     "连接已更换；旧记录仅供导出，推荐上限已作废")
            self.suggested_caps = {"battery_cap_pct": None, "dcdc_cap_pct": None}
            self.channel_caps = {
                "battery": {1: None, 2: None},
                "dcdc": {1: None, 2: None},
            }

    def _stop_and_restore_auto(self) -> dict[str, Any]:
        """停止标定并通过确认命令恢复自动模式。"""
        errors: list[str] = []
        before_stop = self.snapshot_fn().get("fan", {})
        before_calib = before_stop.get("calib_status", {})
        already_aborted = (
            _fresh_age(before_stop.get("calib_status_age"), CALIB_TELEMETRY_TIMEOUT_S)
            and before_calib.get("calib_state") == 2
            and before_calib.get("calib_target_pct") == [0, 0]
        )
        try:
            result, generation = self._send_calib_command(3, 0, 0, 0, 0)
            if not result.get("ok"):
                errors.append(f"fan_calib: {result.get('error', '发送失败')}")
            else:
                # The generation baseline is captured after ACK.  A periodic
                # frame received while sending/waiting for ACK is not proof of
                # post-ACK controller state.
                # 固件安全看门狗已把会话置为 ABORTED 且目标归零时，STOP 会把
                # 状态收回 INACTIVE；它不会伪装成正常完成的 COMPLETED，也不会
                # 再周期发送 0x5A9。此时 STOP ACK 加此前的新鲜 ABORTED/零目标
                # 已足以证明安全收尾，不能继续等待一个协议上不会出现的状态帧。
                if not already_aborted:
                    confirm_error = self._wait_for_calib_state(
                        3, 0, 0, 0, after_generation=generation,
                        timeout_s=CALIB_STATUS_CONFIRM_TIMEOUT_S)
                    if confirm_error:
                        errors.append(f"fan_calib: {confirm_error}")
        except Exception as exc:
            errors.append(f"fan_calib: {exc}")

        # Even if STOP cannot be confirmed, explicitly request AUTO.  Firmware
        # may reject it while calibration is still active, but attempting both
        # leaves the safest recoverable state and reports every failure.
        try:
            result = self.send_fn("fan_control", {
                "mode": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0,
            }, True)
            if not result.get("ok"):
                errors.append(f"fan_control: {result.get('error', '发送失败')}")
        except Exception as exc:
            errors.append(f"fan_control: {exc}")
        return {"ok": not errors, "errors": errors}

    def get_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "abort_reason": self.abort_reason,
                "channel": self.channel,
                "tier": self.tier,
                "current_step": self.current_step,
                "total_steps": self.total_steps,
                "current_duty": list(self.current_duty),
                "suggested_caps": dict(self.suggested_caps),
                "channel_caps": {
                    tier: {str(channel): cap for channel, cap in channels.items()}
                    for tier, channels in self.channel_caps.items()
                },
                "baseline": dict(self.baseline),
                "records": list(self.records),
                "run_params": dict(self.run_params),
                "baseline_id": self.baseline_id,
                "quality_warnings": list(self.quality_warnings),
                "pause_reason": self.pause_reason,
                "pause_started_at": self.pause_started_at,
                "recovery_count": self.recovery_count,
                "last_diagnostic": dict(self.last_diagnostic),
                "last_diagnostic_path": self.last_diagnostic_path,
                "export_available": bool(self.run_params) and self.status != "idle",
                "raw_sample_count": len(self.raw_samples),
                "baseline_raw_sample_count": len(self.baseline_raw_samples),
            }

    def is_running(self) -> bool:
        with self.lock:
            return self.status == "running"

    def export_csv(self) -> str:
        with self.lock:
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Step", "Channel", "Power_Tier", "Direction", "PWM1_Duty_Pct", "PWM2_Duty_Pct",
                "Fan1_RPM", "Fan2_RPM", "Fan3_RPM",
                "Bus_Voltage_V", "Bus_Current_A", "Bus_Power_W",
                "Delta_Current_A", "Delta_Power_W",
                "Baseline_ID", "Baseline_Current_A", "Baseline_Power_W",
                "Motor_Temp_C", "Controller_Temp_C", "Timestamp",
                "Sample_Count", "Std_Current_A", "Std_Power_W",
                "Quality_OK", "Quality_Note", "Session_Status", "Abort_Reason"
            ])
            for r in self.records:
                writer.writerow([
                    r.get("step"), r.get("channel"), r.get("tier", self.tier), r.get("direction", ""),
                    r.get("duty1_pct"), r.get("duty2_pct"),
                    r.get("rpm1"), r.get("rpm2"), r.get("rpm3"),
                    r.get("voltage_v"), r.get("current_a"), r.get("power_w"),
                    r.get("delta_current_a"), r.get("delta_power_w"),
                    r.get("baseline_id"), r.get("baseline_current_a"), r.get("baseline_power_w"),
                    r.get("motor_temp_c"), r.get("controller_temp_c"),
                    r.get("timestamp"),
                    r.get("sample_count"), r.get("std_current_a"), r.get("std_power_w"),
                    r.get("quality_ok"), r.get("quality_note", ""),
                    self.status, self.abort_reason,
                ])
            if not self.records:
                # 即使在首个基线或首个测点就中止，也要给 CSV 留下一条可检索的
                # 会话诊断记录；逐样本详情由 JSON 导出提供。
                writer.writerow(["", self.channel, self.tier, "", "", "", "", "", "",
                                 "", "", "", "", "", self.baseline_id, "", "", "", "", "",
                                 0, "", "", False, "未形成完整测点",
                                 self.status, self.abort_reason])
            return output.getvalue()

    def export_json(self) -> str:
        with self.lock:
            data = {
                "channel": self.channel,
                "status": self.status,
                "abort_reason": self.abort_reason,
                "run_params": dict(self.run_params),
                "baseline": self.baseline,
                "baseline_history": list(self.baseline_history),
                "records": self.records,
                "raw_samples": list(self.raw_samples),
                # 基线的逐样本原始数据必须一起导出，否则无法复核每条记录关联的基线。
                "baseline_raw_samples": list(self.baseline_raw_samples),
                "quality_warnings": list(self.quality_warnings),
                "recovery_count": self.recovery_count,
                "last_diagnostic": dict(self.last_diagnostic),
                "last_diagnostic_path": self.last_diagnostic_path,
                "exported_at": time.time(),
            }
            return json.dumps(data, ensure_ascii=False, indent=2)

    def _sample_until(self, seconds: float, max_current_a: float,
                      expected_state: int, expected_step: int,
                      expected_duties: tuple[int, int]) -> tuple[list[dict[str, float]], str | None]:
        """采集指定时长内的 PDM 快照样本（约 0.1s 一个）。"""
        while True:
            samples: list[dict[str, float]] = []
            end = time.monotonic() + seconds
            recovered = False
            while time.monotonic() < end:
                if self._stop_event.is_set():
                    return samples, "标定已停止"
                snap = self.snapshot_fn()
                pause_reason = self._communication_pause_reason(snap)
                if pause_reason:
                    recovery_error = self._wait_for_communication_recovery(
                        pause_reason, max_current_a, expected_state,
                        expected_step, expected_duties)
                    if recovery_error:
                        return samples, recovery_error
                    recovered = True
                    break
                safety_error = self._watchdog(
                    snap, max_current_a, expected_state,
                    expected_step=expected_step, expected_duties=expected_duties)
                if safety_error:
                    return samples, safety_error
                bus = snap.get("pdm", {}).get("bus", {})
                values = (bus.get("voltage_v"), bus.get("current_a"), bus.get("power_w"))
                if all(isinstance(value, (int, float)) and math.isfinite(value)
                       for value in values):
                    samples.append({
                        # 记录每个样本的真实采集时间，不能等采样结束后统一生成。
                        "t": round(time.time(), 3),
                        "v": float(bus["voltage_v"]),
                        "i": float(bus["current_a"]),
                        "p": float(bus["power_w"]),
                    })
                time.sleep(0.1)
            if not recovered:
                return samples, None

    @staticmethod
    def _stable_summary(samples: list[dict[str, float]]) -> tuple[dict[str, float], bool]:
        """返回中位数/离散度；数量不足或波动过大时标记不稳定。"""
        if len(samples) < CALIB_QUALITY_MIN_SAMPLES:
            return {"median_v": 0.0, "median_i": 0.0, "median_p": 0.0,
                    "std_i": 0.0, "std_p": 0.0}, False
        median_v = statistics.median(s["v"] for s in samples)
        median_i = statistics.median(s["i"] for s in samples)
        median_p = statistics.median(s["p"] for s in samples)
        std_i = statistics.pstdev(s["i"] for s in samples)
        std_p = statistics.pstdev(s["p"] for s in samples)
        stable = (std_i <= CALIB_QUALITY_MAX_STD_CURRENT_A
                  and std_p <= CALIB_QUALITY_MAX_STD_POWER_W)
        return {"median_v": median_v, "median_i": median_i, "median_p": median_p,
                "std_i": std_i, "std_p": std_p}, stable

    def _measure_baseline(self, step_label: int, direction: str, max_current_a: float,
                          expected_state: int) -> tuple[dict[str, float] | None, str | None]:
        """先归零并等待 3s，再采集 3s 稳态基线。

        每次测量分配一个递增的 baseline_id，原始样本追加保存而不是覆盖上一组，
        这样导出的记录可以复核每个稳态点实际关联的基线。
        """
        cmd_res, generation = self._send_calib_command(
            1 if self.baseline_id == 0 else 2, step_label, 0, 0)
        if not cmd_res.get("ok"):
            recovery_error = self._wait_for_communication_recovery(
                f"0% 基线命令暂未确认：{cmd_res.get('error', '发送失败')}",
                max_current_a, expected_state, step_label, (0, 0))
            if recovery_error:
                return None, recovery_error
            generation = self._calib_generation()
        confirm_error = self._wait_for_calib_state(
            1, step_label, 0, 0, after_generation=generation,
            timeout_s=CALIB_STATUS_CONFIRM_TIMEOUT_S,
            max_current_a=max_current_a, expected_supply_state=expected_state)
        if confirm_error:
            recovery_error = self._wait_for_communication_recovery(
                confirm_error, max_current_a, expected_state, step_label, (0, 0))
            if recovery_error:
                return None, recovery_error
        _, error = self._sample_until(
            self.SETTLE_S, max_current_a, expected_state, step_label, (0, 0))
        if error:
            return None, error
        if self._stop_event.is_set():
            return None, "标定已停止"
        samples: list[dict[str, float]] = []
        all_attempt_samples: list[dict[str, Any]] = []
        summary, stable = self._stable_summary(samples)
        best_score = (False, -1, float("-inf"))
        for _attempt in range(CALIB_SAMPLE_RETRY_LIMIT):
            candidate, error = self._sample_until(
                self.SAMPLE_S, max_current_a, expected_state, step_label, (0, 0))
            if error:
                return None, error
            all_attempt_samples.extend({**sample, "sample_attempt": _attempt + 1}
                                       for sample in candidate)
            candidate_summary, candidate_stable = self._stable_summary(candidate)
            score = (candidate_stable, len(candidate),
                     -(candidate_summary["std_i"] + candidate_summary["std_p"]))
            if score > best_score:
                samples, summary, stable, best_score = (
                    candidate, candidate_summary, candidate_stable, score)
            if candidate_stable:
                break
        with self.lock:
            self.baseline_id += 1
            baseline_id = self.baseline_id
            # 追加保存：每 4 个点重新测量时保留之前所有基线的原始样本。
            self.baseline_raw_samples.extend([{
                "baseline_id": baseline_id,
                "step": step_label,
                "direction": direction,
                "duty1_pct": 0,
                "duty2_pct": 0,
                "t": s.get("t"),
                "v": s.get("v"),
                "i": s.get("i"),
                "p": s.get("p"),
                "sample_attempt": s.get("sample_attempt"),
            } for s in all_attempt_samples])
            self.baseline_history.append({
                "baseline_id": baseline_id,
                "step": step_label,
                "direction": direction,
                "voltage_v": round(summary["median_v"], 3),
                "current_a": round(summary["median_i"], 3),
                "power_w": round(summary["median_p"], 2),
                "std_current_a": round(summary["std_i"], 3),
                "std_power_w": round(summary["std_p"], 3),
                "sample_count": len(samples),
                "quality_ok": stable,
                "quality_note": ("" if stable else
                                 "背景负载波动较大；保留基线并继续扫频，相关测点不用于自动推荐"),
                "captured_at": round(time.time(), 3),
            })
            if not stable:
                self.quality_warnings.append({
                    "kind": "baseline", "baseline_id": baseline_id,
                    "step": step_label, "direction": direction,
                    "sample_count": len(samples),
                    "std_current_a": round(summary["std_i"], 3),
                    "std_power_w": round(summary["std_p"], 3),
                    "message": "0% 基线波动较大",
                })
        if len(samples) < CALIB_QUALITY_MIN_SAMPLES:
            return None, (f"0% 基线有效样本不足（{len(samples)}/"
                          f"{CALIB_QUALITY_MIN_SAMPLES}）；诊断数据可导出")
        return {
            "baseline_id": baseline_id,
            "voltage_v": round(summary["median_v"], 3),
            "current_a": round(summary["median_i"], 3),
            "power_w": round(summary["median_p"], 2),
            "sample_count": len(samples),
            "quality_ok": stable,
        }, None

    def _abort_and_return(self, reason: str) -> None:
        self.abort(reason)

    def _calib_generation(self) -> int:
        value = self.snapshot_fn().get("fan", {}).get("calib_status_generation", 0)
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    def _wait_for_calib_state(self, state: int, step: int, duty1: int, duty2: int,
                              *, after_generation: int,
                              timeout_s: float = CALIB_STATUS_CONFIRM_TIMEOUT_S,
                              max_current_a: float | None = None,
                              expected_supply_state: int = 3) -> str | None:
        """Require a newly received 0x5A9 matching the acknowledged command."""
        deadline = time.monotonic() + timeout_s
        last_state = "等待0x5A9"
        while time.monotonic() < deadline:
            if state == 1 and self._stop_event.is_set():
                return "标定已停止"
            snap = self.snapshot_fn()
            if state == 1 and max_current_a is not None:
                safety_error = self._watchdog(
                    snap, max_current_a, expected_supply_state,
                    require_calibration_active=False)
                if safety_error:
                    return safety_error
            fan = snap.get("fan", {})
            calib = fan.get("calib_status", {})
            age = fan.get("calib_status_age")
            try:
                generation = int(fan.get("calib_status_generation", 0))
            except (TypeError, ValueError, OverflowError):
                generation = 0
            fresh_new_frame = (generation > after_generation
                               and _fresh_age(age, CALIB_TELEMETRY_TIMEOUT_S))
            targets = calib.get("calib_target_pct")
            matches = (calib.get("calib_state") == state
                       and isinstance(targets, list) and len(targets) >= 2
                       and targets[0] == duty1 and targets[1] == duty2)
            if state == 1:
                matches = (matches and calib.get("step") == step
                           and isinstance(calib.get("lease_remaining_s"), (int, float))
                           and calib.get("lease_remaining_s") > 0)
            if fresh_new_frame and matches:
                return None
            if fresh_new_frame and calib.get("calib_state") in {0, 2}:
                return self._firmware_terminal_reason(calib)
            last_state = (f"代次={generation}/{after_generation + 1}+，"
                          f"状态={calib.get('calib_state', '等待')}，"
                          f"步骤={calib.get('step', '等待')}，"
                          f"目标={targets if targets is not None else '等待'}")
            time.sleep(0.05)
        return f"FanController未在{timeout_s:.1f}s内用新0x5A9确认标定目标（{last_state}）"

    def _watchdog(self, snap: dict[str, Any], max_current_a: float,
                  expected_state: int = 3, *, require_calibration_active: bool = True,
                  expected_step: int | None = None,
                  expected_duties: tuple[int, int] | None = None) -> str | None:
        """扫描期间持续安全检查，返回中止原因；None 表示通过。"""
        conn = snap.get("connection", {})
        fan = snap.get("fan", {})
        fan_diag = fan.get("diagnostic", {})
        fan_power = fan.get("power_status", {})
        bus = snap.get("pdm", {}).get("bus", {})

        if not conn.get("connected") or conn.get("mode") != "pcan":
            return "连接模式已变化，触发安全中止"
        if conn.get("bus_profile") != "canb" or conn.get("bitrate") != 500000:
            return "整车连接已离开 CANB 500 kbit/s，触发安全中止"
        if self._communication_pause_reason(
                snap, require_calibration_active=require_calibration_active,
                include_firmware_terminal=False):
            return None
        if require_calibration_active:
            calib = fan.get("calib_status", {})
            targets = calib.get("calib_target_pct")
            if calib.get("calib_state") != 1:
                return self._firmware_terminal_reason(calib)
            if (not isinstance(calib.get("lease_remaining_s"), (int, float))
                    or calib.get("lease_remaining_s") <= 0):
                return None
            if expected_step is not None and calib.get("step") != expected_step:
                return f"FanController 标定步骤被外部改写（期望 {expected_step}，当前 {calib.get('step')}）"
            if (expected_duties is not None
                    and (not isinstance(targets, list) or len(targets) < 2
                         or tuple(targets[:2]) != expected_duties)):
                return ("FanController 标定目标被外部改写"
                        f"（期望 {list(expected_duties)}，当前 {targets}）")
        # 状态 4 表示功率仲裁正在夹紧，不代表实际供电档位已经变化。
        # 该测点会标记为待复核并排除出推荐，但不能因此销毁整轮扫描。
        if fan_power.get("power_supply_state") not in {expected_state, 4}:
            state_name = fan_power.get("power_supply_name", str(fan_power.get("power_supply_state")))
            return f"供电脱离所选标定档位（当前：{state_name}），触发安全中止"
        # 扫频本来就要测出最低起转占空比。低占空比 START_KICK 结束时，固件会把
        # 尚未起转写进 0x5A3 TACH 位；这只是当前测点的结果，不能由上位机抢先
        # 当成安全中止。真实运行停转仍由固件安全看门狗确认并把 0x5A9 切到
        # ABORTED，本函数上面的会话活动检查会立即中止；记录中的 0 RPM 也不会
        # 被 _max_safe_duty() 选为推荐上限。
        i_curr = bus.get("current_a")
        if i_curr > max_current_a:
            return f"总线电流 ({i_curr:.1f} A) 超过安全限制 ({max_current_a:.1f} A)"
        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        if motor_temp >= CALIB_ABORT_MOTOR_TEMP_C:
            return f"电机温度超限 ({motor_temp:.1f} ℃ >= {CALIB_ABORT_MOTOR_TEMP_C:.0f} ℃)"
        if ctrl_temp >= CALIB_ABORT_CONTROLLER_TEMP_C:
            return f"控制器温度超限 ({ctrl_temp:.1f} ℃ >= {CALIB_ABORT_CONTROLLER_TEMP_C:.0f} ℃)"
        return None

    def _collect_step_samples(
            self, seconds: float, step: int, duty1: int, duty2: int,
            max_current_a: float, expected_state: int,
            direction: str, baseline: dict[str, float], baseline_id: int,
            attempt: int) -> tuple[list[dict[str, float]], str | None]:
        while True:
            samples: list[dict[str, float]] = []
            end = time.monotonic() + seconds
            recovered = False
            while time.monotonic() < end:
                if self._stop_event.is_set():
                    return samples, "标定已停止"
                snap = self.snapshot_fn()
                pause_reason = self._communication_pause_reason(snap)
                if pause_reason:
                    recovery_error = self._wait_for_communication_recovery(
                        pause_reason, max_current_a, expected_state,
                        step, (duty1, duty2))
                    if recovery_error:
                        return samples, recovery_error
                    recovered = True
                    break
                reason = self._watchdog(
                    snap, max_current_a, expected_state,
                    expected_step=step, expected_duties=(duty1, duty2))
                if reason:
                    return samples, reason
                fan = snap.get("fan", {})
                fan_status = fan.get("status", {})
                fan_diag = fan.get("diagnostic", {})
                fan_power = fan.get("power_status", {})
                bus = snap.get("pdm", {}).get("bus", {})
                rpm = fan_status.get("rpm", [0, 0, 0]) or [0, 0, 0]
                sample = {
                    "t": round(time.time(), 3),
                    "v": float(bus["voltage_v"]),
                    "i": float(bus["current_a"]),
                    "p": float(bus["power_w"]),
                    "rpm1": rpm[0] if len(rpm) > 0 else 0,
                    "rpm2": rpm[1] if len(rpm) > 1 else 0,
                    "rpm3": rpm[2] if len(rpm) > 2 else 0,
                    "mt": fan_diag.get("motor_temp_c"),
                    "ct": fan_diag.get("controller_temp_c"),
                    "power_limited": fan_power.get("power_supply_state") == 4,
                    "power_limit_name": fan_power.get("power_limit_name", ""),
                }
                samples.append(sample)
                with self.lock:
                    self.raw_samples.append({
                        "step": step, "direction": direction,
                        "duty1_pct": duty1, "duty2_pct": duty2,
                        **sample,
                        "baseline_id": baseline_id,
                        "baseline_current_a": baseline.get("current_a"),
                        "baseline_power_w": baseline.get("power_w"),
                        "sample_attempt": attempt,
                        "timestamp": sample["t"],
                    })
                time.sleep(0.1)
            if not recovered:
                return samples, None

    def _apply_step(self, step: int, duty1: int, duty2: int,
                    hold_s: float, max_current_a: float,
                    baseline: dict[str, float],
                    direction: str = "",
                    baseline_id: int = 0, expected_state: int = 3) -> dict[str, Any] | None:
        """下发一个目标点；通信暂停后重做，质量不足最多完整重采三次。"""
        cmd_res, generation = self._send_calib_command(2, step, duty1, duty2)
        if not cmd_res.get("ok"):
            recovery_error = self._wait_for_communication_recovery(
                f"步骤 {step} 命令暂未确认：{cmd_res.get('error', '无应答')}",
                max_current_a, expected_state, step, (duty1, duty2))
            if recovery_error:
                self._abort_and_return(recovery_error)
                return None
            generation = self._calib_generation()
        confirm_error = self._wait_for_calib_state(
            1, step, duty1, duty2, after_generation=generation,
            timeout_s=CALIB_STATUS_CONFIRM_TIMEOUT_S,
            max_current_a=max_current_a, expected_supply_state=expected_state)
        if confirm_error:
            recovery_error = self._wait_for_communication_recovery(
                confirm_error, max_current_a, expected_state, step, (duty1, duty2))
            if recovery_error:
                self._abort_and_return(recovery_error)
                return None

        # 通信恢复会重发当前目标；稳定窗口从恢复后的目标确认重新计时。
        _, settle_error = self._sample_until(
            self.SETTLE_S, max_current_a, expected_state, step, (duty1, duty2))
        if settle_error:
            self._abort_and_return(settle_error)
            return None

        with self.lock:
            self.current_step = step
            self.current_duty = [duty1, duty2]

        samples: list[dict[str, float]] = []
        summary, stable = self._stable_summary(samples)
        best_score = (False, False, -1, float("-inf"))
        for attempt in range(1, CALIB_SAMPLE_RETRY_LIMIT + 1):
            candidate, error = self._collect_step_samples(
                max(self.SAMPLE_S, hold_s - self.SETTLE_S),
                step, duty1, duty2, max_current_a, expected_state,
                direction, baseline, baseline_id, attempt)
            if error:
                self._abort_and_return(error)
                return None
            candidate_summary, candidate_stable = self._stable_summary(candidate)
            candidate_limited = any(bool(item.get("power_limited")) for item in candidate)
            score = (candidate_stable, not candidate_limited, len(candidate),
                     -(candidate_summary["std_i"] + candidate_summary["std_p"]))
            if score > best_score:
                samples, summary, stable, best_score = (
                    candidate, candidate_summary, candidate_stable, score)
            if candidate_stable and not candidate_limited:
                break

        if len(samples) < CALIB_QUALITY_MIN_SAMPLES:
            self._abort_and_return(
                f"步骤 {step} 连续 {CALIB_SAMPLE_RETRY_LIMIT} 次有效样本不足"
                f"（最佳 {len(samples)}/{CALIB_QUALITY_MIN_SAMPLES}）；诊断数据已保留")
            return None

        last = samples[-1]
        baseline_quality_ok = baseline.get("quality_ok", True) is not False
        power_limited = any(bool(item.get("power_limited")) for item in samples)
        quality_ok = stable and baseline_quality_ok and not power_limited
        quality_notes: list[str] = []
        if not stable:
            quality_notes.append("测点功率波动较大")
        if not baseline_quality_ok:
            quality_notes.append("关联的0%基线波动较大")
        if power_limited:
            limit_names = sorted({str(item.get("power_limit_name") or "功率仲裁")
                                  for item in samples if item.get("power_limited")})
            quality_notes.append("固件限功率：" + "/".join(limit_names))
        quality_note = "；".join(quality_notes)
        if quality_notes:
            with self.lock:
                self.quality_warnings.append({
                    "kind": "step", "step": step, "direction": direction,
                    "duty1_pct": duty1, "duty2_pct": duty2,
                    "baseline_id": baseline_id, "sample_count": len(samples),
                    "std_current_a": round(summary["std_i"], 3),
                    "std_power_w": round(summary["std_p"], 3),
                    "message": quality_note,
                })
        return {
            "step": step,
            "channel": self.channel,
            "tier": self.tier,
            "direction": direction,
            "baseline_id": baseline_id,
            "duty1_pct": duty1,
            "duty2_pct": duty2,
            "rpm1": int(statistics.median(s["rpm1"] for s in samples)),
            "rpm2": int(statistics.median(s["rpm2"] for s in samples)),
            "rpm3": int(statistics.median(s["rpm3"] for s in samples)),
            "voltage_v": round(summary["median_v"], 3),
            "current_a": round(summary["median_i"], 3),
            "power_w": round(summary["median_p"], 2),
            "delta_current_a": round(max(0.0, summary["median_i"] - baseline.get("current_a", 0.0)), 3),
            "delta_power_w": round(max(0.0, summary["median_p"] - baseline.get("power_w", 0.0)), 2),
            "std_current_a": round(summary["std_i"], 3),
            "std_power_w": round(summary["std_p"], 3),
            "sample_count": len(samples),
            "quality_ok": quality_ok,
            "quality_note": quality_note,
            "baseline_voltage_v": baseline.get("voltage_v"),
            "baseline_current_a": baseline.get("current_a"),
            "baseline_power_w": baseline.get("power_w"),
            "motor_temp_c": last.get("mt"),
            "controller_temp_c": last.get("ct"),
            "timestamp": round(time.time(), 3),
        }

    def _run_sweep(self, channel: int, steps: list[int],
                   hold_s: float, max_current_a: float, tier: str = "dcdc") -> None:
        """执行计划中的上升+下降双向阶梯扫频。"""
        try:
            current_state = self.snapshot_fn().get("fan", {}).get("power_status", {}).get("power_supply_state")
            expected_state = 3 if tier == "dcdc" else 1
            if current_state != expected_state:
                self._abort_and_return("启动后供电已离开所选档位，中止标定")
                return

            baseline, error = self._measure_baseline(
                0, "initial", max_current_a, expected_state)
            if baseline is None:
                self._abort_and_return(error or "无法采集 0% 静态基线，中止标定")
                return
            with self.lock:
                self.baseline = {
                    "baseline_id": baseline.get("baseline_id"),
                    "voltage_v": baseline["voltage_v"],
                    "current_a": baseline["current_a"],
                    "power_w": baseline["power_w"],
                    "sample_count": baseline.get("sample_count", 0),
                    "quality_ok": baseline.get("quality_ok", True),
                }

            directions: list[tuple[str, list[int]]] = [("up", steps), ("down", list(reversed(steps)))]
            record_index = 0
            for direction_name, direction in directions:
                for index, target_duty in enumerate(direction, start=1):
                    if self._stop_event.is_set():
                        return
                    if index > 1 and (index - 1) % self.BASELINE_INTERVAL == 0:
                        fresh, error = self._measure_baseline(
                            index, direction_name, max_current_a, expected_state)
                        if fresh is None:
                            self._abort_and_return(
                                error or f"步骤 {index} 前重新采集 0% 基线失败，中止标定")
                            return
                        with self.lock:
                            self.baseline = {
                                "baseline_id": fresh.get("baseline_id"),
                                "voltage_v": fresh["voltage_v"],
                                "current_a": fresh["current_a"],
                                "power_w": fresh["power_w"],
                                "sample_count": fresh.get("sample_count", 0),
                                "quality_ok": fresh.get("quality_ok", True),
                            }

                    d1 = target_duty if channel == 1 else 0
                    d2 = target_duty if channel == 2 else 0
                    record = self._apply_step(
                        record_index + 1, d1, d2, hold_s, max_current_a,
                        self.baseline, direction_name,
                        int(self.baseline.get("baseline_id") or 0),
                        expected_state,
                    )
                    if record is None:
                        return
                    record_index += 1
                    with self.lock:
                        self.current_step = record_index
                        self.records.append(record)

            with self._stop_lock:
                with self.lock:
                    if self.status != "running" or self._stop_event.is_set():
                        return
                self._stop_lease_heartbeat()
                result = self._stop_and_restore_auto()
                terminal_reason: str | None = None
                with self.lock:
                    self.current_duty = [0, 0]
                    if self._stop_event.is_set():
                        self.status = "aborted"
                        if not self.abort_reason:
                            self.abort_reason = "连接或用户操作已中止标定"
                    elif result["ok"]:
                        # Only publish recommendations after both STOP and the
                        # new COMPLETED 0x5A9 have been confirmed and AUTO was
                        # accepted.  A partially terminated scan is not saveable.
                        key = "battery_cap_pct" if tier == "battery" else "dcdc_cap_pct"
                        new_cap = self._max_safe_duty(self.records, tier)
                        self.channel_caps[tier][channel] = new_cap
                        channel_values = list(self.channel_caps[tier].values())
                        self.suggested_caps[key] = (min(channel_values)
                                                    if all(value is not None for value in channel_values)
                                                    else None)
                        self.status = "completed"
                    else:
                        self.status = "aborted"
                        self.abort_reason = ("扫描完成但固件恢复自动失败："
                                             + "；".join(result["errors"])
                                             + "；记录仍可导出，恢复自动后可手动填入两档上限保存")
                        terminal_reason = self.abort_reason
                if terminal_reason:
                    self._persist_terminal_diagnostic(terminal_reason)
        except Exception as exc:
            self._abort_and_return(f"标定执行发生异常：{exc}")


class BatteryFanCalibrationSession:
    """F405 battery-box fan sweep using PDM power with a nearby 0% baseline."""

    DEFAULT_STEPS = [0, 5, 10, 15, 20, 30, 40, 50, 55, 60, 70, 80, 90, 100]
    BASELINE_INTERVAL = 4
    # 步骤采样不足或波动过大时固定延长一轮的窗口；hold_s=10 时全程仍为
    # 2+8+2=12s，加上命令确认也在 15s 租约之内。
    RETRY_SAMPLE_S = 2.0
    LOW_VOLTAGE_MAX_CURRENT_A = 8.0

    def __init__(self, send_fn: Callable[[str, dict[str, Any], bool], dict[str, Any]],
                 snapshot_fn: Callable[[], dict[str, Any]]) -> None:
        self.send_fn = send_fn
        self.snapshot_fn = snapshot_fn
        self.lock = threading.RLock()
        self.status = "idle"
        self.abort_reason = ""
        self.current_step = 0
        self.total_steps = 0
        self.baseline: dict[str, float] = {}
        self.baseline_history: list[dict[str, float]] = []
        self.baseline_id = 0
        self.records: list[dict[str, Any]] = []
        self.quality_warnings: list[dict[str, Any]] = []
        # None means that the sweep did not produce a safe, rotating point for
        # that budget.  Never turn absence of evidence into a 5% recommendation.
        self.suggested_caps: dict[str, int | None] = {
            "chroma_cap_pct": None, "hv_cap_pct": None,
        }
        self.run_params: dict[str, Any] = {}
        self._expected_power_source: int | None = None
        self._expected_pack_state: int | None = None
        self._stop_event = threading.Event()
        self._stop_lock = threading.RLock()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _max_safe_duty(records: list[dict[str, Any]], budget_w: float) -> int | None:
        """Return the contiguous tested duty envelope that stays within budget."""
        observations: dict[int, list[tuple[bool, bool]]] = {}
        for record in records:
            duty = record.get("duty_pct")
            power = record.get("delta_power_w")
            rpm = record.get("rpm")
            if not (isinstance(duty, (int, float)) and math.isfinite(duty)
                    and float(duty).is_integer() and 0 < duty <= 100):
                continue
            power_ok = (record.get("quality_ok", True) is not False
                        and isinstance(power, (int, float)) and math.isfinite(power)
                        and power <= budget_w)
            rpm_ok = (isinstance(rpm, (int, float)) and math.isfinite(rpm) and rpm > 0)
            observations.setdefault(int(duty), []).append((power_ok, rpm_ok))
        max_safe: int | None = None
        for duty in sorted(observations):
            duty_observations = observations[duty]
            if not duty_observations or not all(item[0] for item in duty_observations):
                break
            if all(item[1] for item in duty_observations):
                max_safe = duty
            elif max_safe is not None:
                break
        return max_safe

    def _safety_error(self, snap: dict[str, Any], max_current_a: float,
                      require_calibration_active: bool = False) -> str | None:
        conn = snap.get("connection", {})
        pack = snap.get("pack", {})
        pdm = snap.get("pdm", {}).get("bus", {})
        battery = snap.get("battery_fan", {})
        status = battery.get("status", {})
        if not conn.get("connected") or conn.get("mode") != "pcan":
            return "必须连接真实PCAN上的CANB"
        if conn.get("bus_profile") != "canb" or conn.get("bitrate") != 500000:
            return "电池箱风扇标定只允许使用整车CANB 500 kbit/s"
        if not _fresh_age(pack.get("age"), 1.5) or pack.get("state") not in (3, 5):
            return "BMS必须处于新鲜的待机或高压接通状态"
        fault = snap.get("fault", {})
        if pack.get("state") == 3:
            if not _fresh_age(fault.get("age"), 1.5):
                return "缺少新鲜 CANB BMS 充电状态；等待故障状态帧"
            if fault.get("flags", {}).get("charge_mode"):
                return "BMS正在充电，待机低压状态下禁止标定"
        if not pack.get("temperature_complete", False):
            return "BMS温度采样不完整，禁止在缺少完整温度保护时标定"
        if pdm.get("offline", True) or not _fresh_age(pdm.get("age"), 1.0):
            return "PDM总线遥测离线或超时"
        pdm_values = (pdm.get("voltage_v"), pdm.get("current_a"), pdm.get("power_w"))
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in pdm_values):
            return "PDM总线电压、电流或功率无效"
        if not _fresh_age(battery.get("status_age"), 1.0):
            return "缺少新鲜 CANB 0x5AA 风扇状态；检查 F405 周期上报"
        calibration = battery.get("calibration", {})
        if not _fresh_age(battery.get("calibration_age"), 1.0):
            return "缺少新鲜 CANB 0x5AD 标定状态；检查 F405 周期上报"
        if status.get("protocol_version") != 1:
            return "电池箱风扇协议版本不匹配（0x5AA必须为版本1）"
        if calibration.get("chroma_budget_w") != 35 or calibration.get("hv_budget_w") != 70:
            return "电池箱风扇功率预算版本不匹配（0x5AD必须为35W/70W）"
        source = status.get("power_source")
        expected_source = 0 if pack.get("state") == 3 else 2
        if source != expected_source:
            return f"电池箱风扇供电与BMS状态不一致，或正在Chroma充电（{status.get('power_source_name', '未知')}）"
        if (self.status == "running"
                and (source != self._expected_power_source
                     or pack.get("state") != self._expected_pack_state)):
            return "电池箱风扇供电或BMS状态已变化，标定已中止"
        if source == 0 and max_current_a > self.LOW_VOLTAGE_MAX_CURRENT_A:
            return f"低压待机标定的总线电流保护不得超过{self.LOW_VOLTAGE_MAX_CURRENT_A:.0f}A"
        flags = status.get("flags", {})
        if not flags.get("hardware_ready", False):
            return "电池箱风扇PWM/TACH硬件尚未就绪"
        if flags.get("stall_confirmed"):
            return "电池箱风扇已确认停转"
        if require_calibration_active:
            calibration_age = battery.get("calibration_age")
            if (not flags.get("calibration_active")
                    or not _fresh_age(calibration_age, 1.0)
                    or calibration.get("calib_state") != 1
                    or not isinstance(status.get("lease_remaining_s"), (int, float))
                    or status.get("lease_remaining_s") <= 0):
                return "F405标定会话未处于活动状态"
        if float(pdm["current_a"]) > max_current_a:
            return f"总线电流超过{max_current_a:.1f}A保护值"
        return None

    def start(self, steps: list[int] | None = None, hold_s: float = 5.0,
              max_current_a: float = 18.0) -> dict[str, Any]:
        try:
            hold_s = float(hold_s)
            max_current_a = float(max_current_a)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "保持时间和总线保护必须是有效数字"}
        try:
            numeric_steps = [float(value) for value in
                             (self.DEFAULT_STEPS if steps is None else steps)]
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "扫描点必须是0..100%的整数"}
        if any(not math.isfinite(value) or not value.is_integer() for value in numeric_steps):
            return {"ok": False, "error": "扫描点必须是0..100%的整数"}
        steps = [int(value) for value in numeric_steps]
        if (not steps or any(not 0 <= value <= 100 for value in steps)
                or steps != sorted(set(steps))):
            return {"ok": False, "error": "扫描点必须是0..100%内严格递增且不重复的整数"}
        if (not math.isfinite(hold_s) or not 3.0 <= hold_s <= 10.0
                or not math.isfinite(max_current_a) or not 5.0 <= max_current_a <= 20.0):
            return {"ok": False, "error": "保持时间需3..10秒，总线保护需5..20A"}
        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "电池箱风扇标定正在进行"}
            previous_worker = self._thread
        if (previous_worker and previous_worker.is_alive()
                and previous_worker is not threading.current_thread()):
            previous_worker.join(timeout=1.5)
        if previous_worker and previous_worker.is_alive():
            return {"ok": False, "error": "上一电池箱风扇标定线程尚未安全退出，请稍后重试"}
        self._stop_event.clear()
        error = self._safety_error(self.snapshot_fn(), max_current_a)
        if error:
            return {"ok": False, "error": error}
        snap = self.snapshot_fn()
        error = self._safety_error(snap, max_current_a)
        if error:
            return {"ok": False, "error": error}
        firmware_calib = snap.get("battery_fan", {}).get("calibration", {})
        firmware_calib_age = snap.get("battery_fan", {}).get("calibration_age")
        if (_fresh_age(firmware_calib_age, 1.0)
                and firmware_calib.get("calib_state") == 1):
            return {"ok": False, "error": "F405 已有活动标定会话，请先安全中止后再开始"}
        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "电池箱风扇标定正在进行"}
            if self._stop_event.is_set():
                return {"ok": False, "error": "启动检查期间连接或会话被中止，请重新确认后再试"}
            self.status, self.abort_reason, self.current_step = "running", "", 0
            self.total_steps = len(steps)
            self.baseline.clear()
            self.baseline_history.clear()
            self.baseline_id = 0
            self.records.clear()
            self.quality_warnings.clear()
            self.suggested_caps = {"chroma_cap_pct": None, "hv_cap_pct": None}
            self.run_params = {"steps": steps, "hold_s": hold_s,
                               "max_current_a": max_current_a,
                               "power_source": snap["battery_fan"]["status"]["power_source"]}
            self._expected_power_source = snap["battery_fan"]["status"]["power_source"]
            self._expected_pack_state = snap["pack"]["state"]
            self._thread = threading.Thread(target=self._run, args=(steps, hold_s, max_current_a),
                                            name="battery-fan-calib", daemon=True)
            self._thread.start()
        return {"ok": True, "message": "电池箱风扇自动扫频已启动", "total_steps": len(steps)}

    def _samples(self, seconds: float, max_current_a: float,
                 expected_step: int, expected_duty: int) -> tuple[list[dict[str, float]], str | None]:
        result: list[dict[str, float]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return result, "用户停止"
            snap = self.snapshot_fn()
            error = self._safety_error(snap, max_current_a, require_calibration_active=True)
            if error:
                return result, error
            calibration = snap.get("battery_fan", {}).get("calibration", {})
            if (calibration.get("step") != expected_step
                    or calibration.get("target_duty_pct") != expected_duty):
                return result, ("F405标定目标被外部改写"
                                f"（期望步骤{expected_step}/{expected_duty}%，"
                                f"当前步骤{calibration.get('step')}/"
                                f"{calibration.get('target_duty_pct')}%）")
            bus = snap["pdm"]["bus"]
            fan = snap["battery_fan"]["status"]
            result.append({"v": float(bus.get("voltage_v") or 0),
                           "i": float(bus.get("current_a") or 0),
                           "p": float(bus.get("power_w") or 0),
                           "rpm": float(fan.get("rpm") or 0)})
            time.sleep(0.1)
        return result, None

    @staticmethod
    def _median(samples: list[dict[str, float]]) -> dict[str, float] | None:
        if len(samples) < CALIB_QUALITY_MIN_SAMPLES:
            return None
        result = {key: statistics.median(sample[key] for sample in samples) for key in ("v", "i", "p", "rpm")}
        result["std_i"] = statistics.pstdev(sample["i"] for sample in samples)
        result["std_p"] = statistics.pstdev(sample["p"] for sample in samples)
        return result

    def _send(self, action: int, step: int, duty: int, lease: int = 15) -> dict[str, Any]:
        return self.send_fn("battery_fan_calib", {
            "action": action, "step": step, "duty_pct": duty,
            "lease_s": 0 if action == 3 else lease,
        }, True)

    def _generations(self) -> tuple[int, int]:
        battery = self.snapshot_fn().get("battery_fan", {})
        try:
            status_generation = int(battery.get("status_generation", 0))
            calibration_generation = int(battery.get("calibration_generation", 0))
        except (TypeError, ValueError, OverflowError):
            return 0, 0
        return status_generation, calibration_generation

    def _wait_for_active(self, step: int, duty: int, *, after_status_generation: int,
                         after_calibration_generation: int,
                         timeout_s: float = 1.2) -> str | None:
        """Confirm 0x5AA/0x5AD reflect the just-acknowledged calibration target."""
        deadline = time.monotonic() + timeout_s
        last_state = "等待0x5AA/0x5AD"
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return "用户停止"
            snap = self.snapshot_fn()
            error = self._safety_error(snap, float(self.run_params.get("max_current_a", 18.0)))
            if error:
                return error
            battery = snap.get("battery_fan", {})
            status = battery.get("status", {})
            calibration = battery.get("calibration", {})
            calib_age = battery.get("calibration_age")
            active = status.get("flags", {}).get("calibration_active", False)
            try:
                status_generation = int(battery.get("status_generation", 0))
                calibration_generation = int(battery.get("calibration_generation", 0))
            except (TypeError, ValueError, OverflowError):
                status_generation, calibration_generation = 0, 0
            if (status_generation > after_status_generation
                    and calibration_generation > after_calibration_generation
                    and active and _fresh_age(battery.get("status_age"), 1.0)
                    and _fresh_age(calib_age, 1.0)
                    and isinstance(status.get("lease_remaining_s"), (int, float))
                    and status.get("lease_remaining_s") > 0
                    and calibration.get("calib_state") == 1
                    and calibration.get("step") == step
                    and calibration.get("target_duty_pct") == duty):
                return None
            if (calibration_generation > after_calibration_generation
                    and _fresh_age(calib_age, 1.0)
                    and calibration.get("calib_state") in {0, 2}):
                return ("F405已拒绝或中止标定状态"
                        f"（{calibration.get('calib_state_name', calibration.get('calib_state'))}，"
                        f"原因：{calibration.get('abort_reason_name', '未知')}）")
            last_state = (f"代次={status_generation}/{after_status_generation + 1}+、"
                          f"{calibration_generation}/{after_calibration_generation + 1}+，"
                          f"状态={calibration.get('calib_state', '等待')}，"
                          f"步骤={calibration.get('step', '等待')}，"
                          f"目标={calibration.get('target_duty_pct', '等待')}%")
            time.sleep(0.05)
        return f"F405未在{timeout_s:.1f}s内确认标定目标（{last_state}）"

    def _wait_for_completed(self, *, after_status_generation: int,
                            after_calibration_generation: int,
                            timeout_s: float = 1.2,
                            allow_safe_terminal: bool = False) -> str | None:
        """Require new 0x5AA/0x5AD frames proving STOP reached a safe terminal state."""
        deadline = time.monotonic() + timeout_s
        last_state = "等待0x5AA/0x5AD"
        while time.monotonic() < deadline:
            battery = self.snapshot_fn().get("battery_fan", {})
            status = battery.get("status", {})
            calibration = battery.get("calibration", {})
            try:
                status_generation = int(battery.get("status_generation", 0))
                calibration_generation = int(battery.get("calibration_generation", 0))
            except (TypeError, ValueError, OverflowError):
                status_generation, calibration_generation = 0, 0
            active = status.get("flags", {}).get("calibration_active", False)
            fresh_terminal = (status_generation > after_status_generation
                    and calibration_generation > after_calibration_generation
                    and _fresh_age(battery.get("status_age"), 1.0)
                    and _fresh_age(battery.get("calibration_age"), 1.0)
                    and not active and calibration.get("target_duty_pct") == 0)
            calib_state = calibration.get("calib_state")
            if (fresh_terminal and (calib_state == 3
                                    or (allow_safe_terminal and calib_state in {0, 2}))):
                return None
            if (calibration_generation > after_calibration_generation
                    and _fresh_age(battery.get("calibration_age"), 1.0)
                    and calibration.get("calib_state") in {0, 2}):
                return ("F405停止后未进入可提交的完成状态"
                        f"（{calibration.get('calib_state_name', calibration.get('calib_state'))}）")
            last_state = (f"代次={status_generation}/{after_status_generation + 1}+、"
                          f"{calibration_generation}/{after_calibration_generation + 1}+，"
                          f"状态={calibration.get('calib_state', '等待')}，"
                          f"活动={active}，目标={calibration.get('target_duty_pct', '等待')}%")
            time.sleep(0.05)
        return f"F405未在{timeout_s:.1f}s内用新状态帧确认停止完成（{last_state}）"

    def _measure_baseline(self, step: int, max_current_a: float, start: bool) -> tuple[dict[str, float] | None, str | None]:
        if not self._send(1 if start else 2, step, 0).get("ok"):
            return None, "0%基线命令失败"
        status_generation, calibration_generation = self._generations()
        active_error = self._wait_for_active(
            step, 0, after_status_generation=status_generation,
            after_calibration_generation=calibration_generation)
        if active_error:
            return None, active_error
        _, error = self._samples(2.0, max_current_a, step, 0)
        if error:
            return None, error
        samples, error = self._samples(2.0, max_current_a, step, 0)
        summary = self._median(samples)
        if not error and (summary is None
                          or summary["std_i"] > CALIB_QUALITY_MAX_STD_CURRENT_A
                          or summary["std_p"] > CALIB_QUALITY_MAX_STD_POWER_W):
            extra, extra_error = self._samples(
                self.RETRY_SAMPLE_S, max_current_a, step, 0)
            samples.extend(extra)
            summary = self._median(samples)
            error = error or extra_error
        if error or summary is None:
            return None, error or (f"0%基线有效样本不足（{len(samples)}/"
                                   f"{CALIB_QUALITY_MIN_SAMPLES}）；诊断记录可导出")
        quality_ok = (summary["std_i"] <= CALIB_QUALITY_MAX_STD_CURRENT_A
                      and summary["std_p"] <= CALIB_QUALITY_MAX_STD_POWER_W)
        with self.lock:
            self.baseline_id += 1
            measured = {key: round(summary[key], 3) for key in ("v", "i", "p", "rpm", "std_i", "std_p")}
            measured["baseline_id"] = self.baseline_id
            measured["step"] = step
            measured["sample_count"] = len(samples)
            measured["quality_ok"] = quality_ok
            self.baseline = dict(measured)
            self.baseline_history.append(dict(measured))
            if not quality_ok:
                self.quality_warnings.append({
                    "kind": "baseline", "baseline_id": self.baseline_id,
                    "step": step, "sample_count": len(samples),
                    "std_current_a": round(summary["std_i"], 3),
                    "std_power_w": round(summary["std_p"], 3),
                    "message": "0%基线波动较大",
                })
        return measured, None

    def _finish_abort(self, reason: str) -> dict[str, Any]:
        with self._stop_lock:
            with self.lock:
                if self.status != "running":
                    return {"ok": True, "status": self.status, "reason": self.abort_reason,
                            "errors": []}
                self._stop_event.set()
            before_stop = self.snapshot_fn().get("battery_fan", {})
            before_calibration = before_stop.get("calibration", {})
            already_aborted = (
                _fresh_age(before_stop.get("calibration_age"), 1.0)
                and before_calibration.get("calib_state") == 2
                and before_calibration.get("target_duty_pct") == 0
            )
            try:
                stop_result = self._send(3, self.current_step, 0, 0)
            except Exception as exc:
                stop_result = {"ok": False, "error": str(exc)}
            if stop_result.get("ok") and not already_aborted:
                status_generation, calibration_generation = self._generations()
                confirm_error = self._wait_for_completed(
                    after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation,
                    allow_safe_terminal=True)
                if confirm_error:
                    stop_result = {"ok": False, "error": confirm_error}
            with self.lock:
                if not stop_result.get("ok"):
                    reason = f"{reason}；停止命令失败：{stop_result.get('error', '未收到成功应答')}"
                self.status, self.abort_reason = "aborted", reason
            return {"ok": bool(stop_result.get("ok")), "status": self.status,
                    "reason": self.abort_reason,
                    "errors": ([] if stop_result.get("ok") else
                               [stop_result.get("error", "停止命令失败")])}

    def _run(self, steps: list[int], hold_s: float, max_current_a: float) -> None:
        try:
            baseline, error = self._measure_baseline(0, max_current_a, True)
            if error or baseline is None:
                self._finish_abort(error or "0%基线测量失败")
                return
            for index, duty in enumerate(steps, start=1):
                if index > 1 and (index - 1) % self.BASELINE_INTERVAL == 0:
                    baseline, error = self._measure_baseline(index, max_current_a, False)
                    if error or baseline is None:
                        self._finish_abort(error or f"步骤{index}前重新测量基线失败")
                        return
                if not self._send(2, index, int(duty)).get("ok"):
                    self._finish_abort(f"步骤{index}命令失败")
                    return
                status_generation, calibration_generation = self._generations()
                active_error = self._wait_for_active(
                    index, int(duty), after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation)
                if active_error:
                    self._finish_abort(active_error)
                    return
                settle, error = self._samples(2.0, max_current_a, index, int(duty))
                if error:
                    self._finish_abort(error)
                    return
                samples, error = self._samples(
                    max(1.0, hold_s - 2.0), max_current_a, index, int(duty))
                summary = self._median(samples)
                if not error and (summary is None
                                  or summary["std_i"] > CALIB_QUALITY_MAX_STD_CURRENT_A
                                  or summary["std_p"] > CALIB_QUALITY_MAX_STD_POWER_W):
                    # 与整车风扇会话对齐：最小 hold 时采样窗口只有 1s，
                    # 样本数或波动卡在门槛上时延长一轮再判，不把瞬态当成稳态。
                    extra, extra_error = self._samples(
                        self.RETRY_SAMPLE_S, max_current_a, index, int(duty))
                    samples.extend(extra)
                    summary = self._median(samples)
                    error = error or extra_error
                if error or summary is None:
                    self._finish_abort(error or
                                       f"步骤{index}有效样本不足（{len(samples)}/"
                                       f"{CALIB_QUALITY_MIN_SAMPLES}）；诊断记录可导出")
                    return
                step_quality_ok = (
                    summary["std_i"] <= CALIB_QUALITY_MAX_STD_CURRENT_A
                    and summary["std_p"] <= CALIB_QUALITY_MAX_STD_POWER_W)
                baseline_quality_ok = baseline.get("quality_ok", True) is not False
                quality_ok = step_quality_ok and baseline_quality_ok
                quality_notes: list[str] = []
                if not step_quality_ok:
                    quality_notes.append("测点功率波动较大")
                if not baseline_quality_ok:
                    quality_notes.append("关联的0%基线波动较大")
                quality_note = "；".join(quality_notes)
                record = {
                    "step": index, "duty_pct": int(duty), "rpm": round(summary["rpm"]),
                    "voltage_v": round(summary["v"], 3), "current_a": round(summary["i"], 3),
                    "power_w": round(summary["p"], 2),
                    "delta_current_a": round(max(0.0, summary["i"] - baseline["i"]), 3),
                    "delta_power_w": round(max(0.0, summary["p"] - baseline["p"]), 2),
                    "baseline_current_a": round(baseline["i"], 3),
                    "baseline_power_w": round(baseline["p"], 2),
                    "baseline_id": int(baseline["baseline_id"]),
                    "std_current_a": round(summary["std_i"], 3),
                    "std_power_w": round(summary["std_p"], 3),
                    "sample_count": len(samples),
                    "quality_ok": quality_ok,
                    "quality_note": quality_note,
                }
                with self.lock:
                    self.current_step = index
                    self.records.append(record)
                    if quality_notes:
                        self.quality_warnings.append({
                            "kind": "step", "step": index,
                            "duty_pct": int(duty), "baseline_id": int(baseline["baseline_id"]),
                            "sample_count": len(samples),
                            "std_current_a": round(summary["std_i"], 3),
                            "std_power_w": round(summary["std_p"], 3),
                            "message": quality_note,
                        })
            with self._stop_lock:
                with self.lock:
                    if self.status != "running" or self._stop_event.is_set():
                        return
                stop = self._send(3, len(steps), 0, 0)
                if not stop.get("ok"):
                    with self.lock:
                        self.status = "aborted"
                        self.abort_reason = "扫描完成但停止命令失败；记录仍可导出，确认停止后可手动填入上限提交"
                    return
                status_generation, calibration_generation = self._generations()
                confirm_error = self._wait_for_completed(
                    after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation)
                with self.lock:
                    if self._stop_event.is_set():
                        self.status = "aborted"
                        if not self.abort_reason:
                            self.abort_reason = "连接或用户操作已中止标定"
                        return
                    if confirm_error:
                        self.status = "aborted"
                        self.abort_reason = (f"{confirm_error}；"
                                             "记录仍可导出，确认停止完成后可手动填入上限提交")
                        return
                    self.suggested_caps["chroma_cap_pct"] = self._max_safe_duty(
                        self.records, 35.0)
                    self.suggested_caps["hv_cap_pct"] = self._max_safe_duty(
                        self.records, 70.0)
                    if (self.suggested_caps["chroma_cap_pct"] is not None
                            and self.suggested_caps["hv_cap_pct"] is not None
                            and self.suggested_caps["chroma_cap_pct"] > self.suggested_caps["hv_cap_pct"]):
                        self.suggested_caps["chroma_cap_pct"] = self.suggested_caps["hv_cap_pct"]
                    self.status = "completed"
        except Exception as exc:
            self._finish_abort(f"标定异常：{exc}")

    def abort(self, reason: str = "用户手动停止") -> dict[str, Any]:
        with self.lock:
            if self.status != "running":
                return {"ok": False, "status": self.status, "reason": self.abort_reason,
                        "error": "当前没有正在运行的电池箱风扇自动标定"}
        return self._finish_abort(reason)

    def cancel_for_disconnect(self) -> None:
        """Stop the host worker and invalidate results before the bus closes."""
        self._stop_event.set()
        with self.lock:
            was_running = self.status == "running"
            had_session = self.status in {"running", "completed", "aborted", "stale"}
            if was_running:
                self.status = "aborted"
                self.abort_reason = "整车 CANB 已断开；F405 输出由标定租约到期自动归零"
            elif self.status in {"completed", "aborted"}:
                self.status = "stale"
                self.abort_reason = "连接已更换；旧记录仅供导出，推荐上限已作废"
            self.suggested_caps = {"chroma_cap_pct": None, "hv_cap_pct": None}
            worker = self._thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.2)
        with self.lock:
            if had_session:
                self.status = "aborted" if was_running else "stale"
                self.abort_reason = ("整车 CANB 已断开；F405 输出由标定租约到期自动归零"
                                     if was_running else
                                     "连接已更换；旧记录仅供导出，推荐上限已作废")
            self.suggested_caps = {"chroma_cap_pct": None, "hv_cap_pct": None}

    def get_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"status": self.status, "abort_reason": self.abort_reason,
                    "current_step": self.current_step, "total_steps": self.total_steps,
                    "records": list(self.records),
                    "suggested_caps": dict(self.suggested_caps), "baseline": dict(self.baseline),
                    "baseline_history": list(self.baseline_history),
                    "quality_warnings": list(self.quality_warnings),
                    "export_available": bool(self.run_params) and self.status != "idle",
                    "run_params": dict(self.run_params)}

    def is_running(self) -> bool:
        with self.lock:
            return self.status == "running"

    def export_csv(self) -> str:
        with self.lock:
            output = StringIO()
            writer = csv.DictWriter(output, fieldnames=[
                "step", "duty_pct", "rpm", "voltage_v", "current_a", "power_w",
                "delta_current_a", "delta_power_w", "baseline_current_a", "baseline_power_w",
                "baseline_id", "std_current_a", "std_power_w", "sample_count",
                "quality_ok", "quality_note", "session_status", "abort_reason"])
            writer.writeheader()
            for record in self.records:
                writer.writerow({**record, "session_status": self.status,
                                 "abort_reason": self.abort_reason})
            if not self.records:
                writer.writerow({"quality_ok": False, "quality_note": "未形成完整测点",
                                 "session_status": self.status,
                                 "abort_reason": self.abort_reason})
            return output.getvalue()
