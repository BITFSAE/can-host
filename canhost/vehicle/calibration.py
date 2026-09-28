"""F405 电池箱风扇功率标定与自动扫频。"""

from __future__ import annotations

import csv
from io import StringIO
import json
import math
import statistics
import threading
import time
from typing import Any, Callable


# PDM 读数稳定性只用于判断测点是否适合生成推荐上限，不是安全保护。
# 整车背景负载会有正常波动；超过该门槛时保留并标记测量结果、继续扫频，
# 但不让该点进入自动推荐。真正需要立即停止的条件仍由
# _safety_error 中的电流、温度、供电和遥测新鲜度硬门槛负责。
CALIB_QUALITY_MIN_SAMPLES = 10
CALIB_QUALITY_MAX_STD_CURRENT_A = 0.10
CALIB_QUALITY_MAX_STD_POWER_W = 3.0

def _fresh_age(value: Any, limit_s: float) -> bool:
    """Return True only for a finite, non-negative telemetry age."""
    return (isinstance(value, (int, float)) and math.isfinite(value)
            and 0.0 <= float(value) <= limit_s)


class BatteryFanCalibrationSession:
    """F405 battery-box fan sweep using PDM power with a nearby 0% baseline."""

    DEFAULT_STEPS = [0, 5, 10, 15, 20, 25, 30, 40, 50, 55, 60, 70, 80, 90, 100]
    BASELINE_INTERVAL = 4
    # 步骤采样不足或波动过大时固定延长一轮的窗口；采样期间续发当前
    # 目标，保留固件 15s 硬失联租约，避免较长保持时间耗尽租约。
    RETRY_SAMPLE_S = 2.0
    LEASE_HEARTBEAT_S = 5.0
    LOW_VOLTAGE_MAX_CURRENT_A = 8.0
    PACK_MAX_AGE_S = 2.0
    PDM_MAX_AGE_S = 1.5
    STATUS_MAX_AGE_S = 1.5
    CALIBRATION_MAX_AGE_S = 3.0
    MAX_SAMPLE_PAUSE_S = 5.0

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
        self.raw_samples: list[dict[str, Any]] = []
        self.baseline_raw_samples: list[dict[str, Any]] = []
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
        self._last_command_monotonic = 0.0

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
        if not _fresh_age(pack.get("age"), self.PACK_MAX_AGE_S):
            return "缺少新鲜 CANB 0x4B0 BMS 状态；检查总线上的周期帧"
        if pack.get("state") not in (3, 5):
            return "BMS必须处于新鲜的待机或高压接通状态"
        fault = snap.get("fault", {})
        if pack.get("state") == 3:
            if not _fresh_age(fault.get("age"), self.PACK_MAX_AGE_S):
                return "缺少新鲜 CANB BMS 充电状态；等待故障状态帧"
            if fault.get("flags", {}).get("charge_mode"):
                return "BMS正在充电，待机低压状态下禁止标定"
        if not pack.get("temperature_complete", False):
            return "BMS温度采样不完整，禁止在缺少完整温度保护时标定"
        if pdm.get("offline", True) or not _fresh_age(pdm.get("age"), self.PDM_MAX_AGE_S):
            return "PDM总线遥测离线或超时"
        pdm_values = (pdm.get("voltage_v"), pdm.get("current_a"), pdm.get("power_w"))
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in pdm_values):
            return "PDM总线电压、电流或功率无效"
        if not _fresh_age(battery.get("status_age"), self.STATUS_MAX_AGE_S):
            return "缺少新鲜 CANB 0x5AA 风扇状态；检查 F405 周期上报"
        calibration = battery.get("calibration", {})
        calibration_age = battery.get("calibration_age")
        if not _fresh_age(calibration_age, self.CALIBRATION_MAX_AGE_S):
            if isinstance(calibration_age, (int, float)) and math.isfinite(calibration_age):
                return (f"CANB 0x5AD 标定状态已断流 {calibration_age:.1f}s；"
                        "检查 CAN 监视器中该帧的接收间隔")
            return "尚未收到 CANB 0x5AD 标定状态；检查 F405 固件和 CANB 接线"
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
                    or not _fresh_age(calibration_age, self.CALIBRATION_MAX_AGE_S)
                    or calibration.get("calib_state") != 1
                    or not isinstance(status.get("lease_remaining_s"), (int, float))
                    or status.get("lease_remaining_s") <= 0):
                return "F405标定会话未处于活动状态"
        if float(pdm["current_a"]) > max_current_a:
            return f"总线电流超过{max_current_a:.1f}A保护值"
        return None

    @staticmethod
    def _sample_frames_fresh(snap: dict[str, Any]) -> bool:
        """Pause measurements during short frame gaps instead of reusing old readings."""
        pack = snap.get("pack", {})
        battery = snap.get("battery_fan", {})
        return (bool(_fresh_age(pack.get("age"), 1.5))
                and (pack.get("state") != 3
                     or _fresh_age(snap.get("fault", {}).get("age"), 1.5))
                and _fresh_age(snap.get("pdm", {}).get("bus", {}).get("age"), 1.0)
                and _fresh_age(battery.get("status_age"), 1.0)
                and _fresh_age(battery.get("calibration_age"), 2.0))

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
        if (_fresh_age(firmware_calib_age, self.CALIBRATION_MAX_AGE_S)
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
            self.raw_samples.clear()
            self.baseline_raw_samples.clear()
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
        paused_s = 0.0
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
            if not self._sample_frames_fresh(snap):
                if paused_s >= self.MAX_SAMPLE_PAUSE_S:
                    return result, "标定采样因周期帧间歇缺失暂停过久"
                before_sleep = time.monotonic()
                time.sleep(0.1)
                pause = time.monotonic() - before_sleep
                paused_s += pause
                deadline += pause
                continue
            error = self._renew_if_due(expected_step, expected_duty)
            if error:
                return result, error
            bus = snap["pdm"]["bus"]
            fan = snap["battery_fan"]["status"]
            result.append({"t": round(time.time(), 3),
                           "v": float(bus.get("voltage_v") or 0),
                           "i": float(bus.get("current_a") or 0),
                           "p": float(bus.get("power_w") or 0),
                           "rpm": float(fan.get("rpm") or 0)})
            time.sleep(0.1)
        return result, None

    def _capture_samples(self, seconds: float, max_current_a: float,
                         step: int, duty: int, baseline_id: int,
                         phase: str, attempt: int) -> tuple[list[dict[str, float]], str | None]:
        samples, error = self._samples(seconds, max_current_a, step, duty)
        with self.lock:
            target = self.baseline_raw_samples if phase.startswith("baseline") else self.raw_samples
            target.extend({**sample, "step": step, "duty_pct": duty,
                           "baseline_id": baseline_id, "phase": phase,
                           "sample_attempt": attempt}
                          for sample in samples)
        return samples, error

    @staticmethod
    def _median(samples: list[dict[str, float]]) -> dict[str, float] | None:
        if len(samples) < CALIB_QUALITY_MIN_SAMPLES:
            return None
        result = {key: statistics.median(sample[key] for sample in samples) for key in ("v", "i", "p", "rpm")}
        result["std_i"] = statistics.pstdev(sample["i"] for sample in samples)
        result["std_p"] = statistics.pstdev(sample["p"] for sample in samples)
        return result

    def _send(self, action: int, step: int, duty: int, lease: int = 15) -> dict[str, Any]:
        before = self.snapshot_fn().get("battery_fan", {})
        try:
            status_generation = int(before.get("status_generation", 0))
            calibration_generation = int(before.get("calibration_generation", 0))
        except (TypeError, ValueError, OverflowError):
            status_generation, calibration_generation = 0, 0
        previous_calibration = before.get("calibration", {})
        same_target = (action == 2
                       and previous_calibration.get("calib_state") == 1
                       and previous_calibration.get("step") == step
                       and previous_calibration.get("target_duty_pct") == duty)
        result = self.send_fn("battery_fan_calib", {
            "action": action, "step": step, "duty_pct": duty,
            "lease_s": 0 if action == 3 else lease,
        }, True)
        if not result.get("ok") and result.get("ack_timeout"):
            if action in (1, 2):
                if same_target:
                    # A periodic status frame cannot prove that an identical
                    # heartbeat reached F405. Retry soon while its lease holds.
                    return result
                error = self._wait_for_active(
                    step, duty, after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation)
            else:
                error = self._wait_for_completed(
                    after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation,
                    allow_safe_terminal=True)
            if error is None:
                result = {"ok": True, "ack_timeout": True, "status_confirmed": True,
                          "message": "0x5AC 应答缺失，已由新 0x5AA/0x5AD 确认"}
            else:
                return {**result, "error": f"{result.get('error', '0x5AC 应答缺失')}；状态核对：{error}"}
        if result.get("ok") and action in (1, 2):
            self._last_command_monotonic = time.monotonic()
        return result

    def _renew_if_due(self, step: int, duty: int) -> str | None:
        if time.monotonic() - self._last_command_monotonic < self.LEASE_HEARTBEAT_S:
            return None
        result = self._send(2, step, duty)
        if not result.get("ok"):
            if result.get("ack_timeout"):
                snap = self.snapshot_fn()
                battery = snap.get("battery_fan", {})
                status = battery.get("status", {})
                calibration = battery.get("calibration", {})
                lease = status.get("lease_remaining_s")
                if (self._safety_error(snap, float(self.run_params.get("max_current_a", 18.0)), True) is None
                        and calibration.get("step") == step
                        and calibration.get("target_duty_pct") == duty
                        and isinstance(lease, (int, float)) and math.isfinite(lease)
                        and lease > self.LEASE_HEARTBEAT_S):
                    self._last_command_monotonic = time.monotonic() - self.LEASE_HEARTBEAT_S + 1.0
                    return None
            return f"标定租约续发失败：{result.get('error', '未收到成功应答')}"
        return None

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
                         timeout_s: float = 3.0) -> str | None:
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
                    and active and _fresh_age(battery.get("status_age"), self.STATUS_MAX_AGE_S)
                    and _fresh_age(calib_age, self.CALIBRATION_MAX_AGE_S)
                    and isinstance(status.get("lease_remaining_s"), (int, float))
                    and status.get("lease_remaining_s") > 0
                    and calibration.get("calib_state") == 1
                    and calibration.get("step") == step
                    and calibration.get("target_duty_pct") == duty):
                return None
            if (calibration_generation > after_calibration_generation
                    and _fresh_age(calib_age, self.CALIBRATION_MAX_AGE_S)
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
                            timeout_s: float = 3.0,
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
                    and _fresh_age(battery.get("status_age"), self.STATUS_MAX_AGE_S)
                    and _fresh_age(battery.get("calibration_age"), self.CALIBRATION_MAX_AGE_S)
                    and not active and calibration.get("target_duty_pct") == 0)
            calib_state = calibration.get("calib_state")
            if (fresh_terminal and (calib_state == 3
                                    or (allow_safe_terminal and calib_state in {0, 2}))):
                return None
            if (calibration_generation > after_calibration_generation
                    and _fresh_age(battery.get("calibration_age"), self.CALIBRATION_MAX_AGE_S)
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
        status_generation, calibration_generation = self._generations()
        sent = self._send(1 if start else 2, step, 0)
        if not sent.get("ok"):
            return None, f"0%基线命令失败：{sent.get('error', '未收到成功应答')}"
        active_error = self._wait_for_active(
            step, 0, after_status_generation=status_generation,
            after_calibration_generation=calibration_generation)
        if active_error:
            return None, active_error
        next_baseline_id = self.baseline_id + 1
        _, error = self._capture_samples(
            2.0, max_current_a, step, 0, next_baseline_id, "baseline_settle", 0)
        if error:
            return None, error
        samples, error = self._capture_samples(
            2.0, max_current_a, step, 0, next_baseline_id, "baseline_measure", 1)
        summary = self._median(samples)
        if not error and (summary is None
                          or summary["std_i"] > CALIB_QUALITY_MAX_STD_CURRENT_A
                          or summary["std_p"] > CALIB_QUALITY_MAX_STD_POWER_W):
            extra, extra_error = self._capture_samples(
                self.RETRY_SAMPLE_S, max_current_a, step, 0,
                next_baseline_id, "baseline_retry", 2)
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
                _fresh_age(before_stop.get("calibration_age"), self.CALIBRATION_MAX_AGE_S)
                and before_calibration.get("calib_state") == 2
                and before_calibration.get("target_duty_pct") == 0
            )
            status_generation, calibration_generation = self._generations()
            try:
                stop_result = self._send(3, self.current_step, 0, 0)
            except Exception as exc:
                stop_result = {"ok": False, "error": str(exc)}
            if stop_result.get("ok") and not already_aborted:
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
                status_generation, calibration_generation = self._generations()
                sent = self._send(2, index, int(duty))
                if not sent.get("ok"):
                    self._finish_abort(f"步骤{index}命令失败：{sent.get('error', '未收到成功应答')}")
                    return
                active_error = self._wait_for_active(
                    index, int(duty), after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation)
                if active_error:
                    self._finish_abort(active_error)
                    return
                settle, error = self._capture_samples(
                    2.0, max_current_a, index, int(duty),
                    int(baseline["baseline_id"]), "step_settle", 0)
                if error:
                    self._finish_abort(error)
                    return
                # 满占空比若仍未起转，F405 最多用 5s 确认；最短保持时间
                # 不能抢先 STOP 并把这个硬中止误记成扫描完成。
                effective_hold_s = max(hold_s, 7.0) if duty == 100 else hold_s
                samples, error = self._capture_samples(
                    max(1.0, effective_hold_s - 2.0), max_current_a, index, int(duty),
                    int(baseline["baseline_id"]), "step_measure", 1)
                summary = self._median(samples)
                if not error and (summary is None
                                  or summary["std_i"] > CALIB_QUALITY_MAX_STD_CURRENT_A
                                  or summary["std_p"] > CALIB_QUALITY_MAX_STD_POWER_W):
                    # 与整车风扇会话对齐：最小 hold 时采样窗口只有 1s，
                    # 样本数或波动卡在门槛上时延长一轮再判，不把瞬态当成稳态。
                    extra, extra_error = self._capture_samples(
                        self.RETRY_SAMPLE_S, max_current_a, index, int(duty),
                        int(baseline["baseline_id"]), "step_retry", 2)
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
                status_generation, calibration_generation = self._generations()
                stop = self._send(3, len(steps), 0, 0)
                if not stop.get("ok"):
                    with self.lock:
                        self.status = "aborted"
                        self.abort_reason = "扫描完成但停止命令失败；记录仍可导出，确认停止后可手动填入上限提交"
                    return
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
                    "run_params": dict(self.run_params),
                    "raw_sample_count": len(self.raw_samples),
                    "baseline_raw_sample_count": len(self.baseline_raw_samples)}

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

    def export_json(self) -> str:
        with self.lock:
            data = {
                "status": self.status,
                "abort_reason": self.abort_reason,
                "current_step": self.current_step,
                "total_steps": self.total_steps,
                "run_params": dict(self.run_params),
                "baseline": dict(self.baseline),
                "baseline_history": list(self.baseline_history),
                "baseline_raw_samples": list(self.baseline_raw_samples),
                "records": list(self.records),
                "raw_samples": list(self.raw_samples),
                "quality_warnings": list(self.quality_warnings),
                "suggested_caps": dict(self.suggested_caps),
                "exported_at": time.time(),
            }
            return json.dumps(data, ensure_ascii=False, indent=2)
