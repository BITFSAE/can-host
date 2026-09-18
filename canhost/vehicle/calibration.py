"""FanController calibration session manager and automated sweep runner.

Runs controlled calibration sweeps over PWM1 (dual 2H4PU) and PWM2 (single 2H6P),
measures baseline PDM bus power/current, samples steady-state RPM and delta I/P,
and strictly enforces safety gating (DCDC_READY, temperature and electrical limits).
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import math
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

    def __init__(self, send_fn: Callable[[str, dict[str, Any], bool], dict[str, Any]],
                 snapshot_fn: Callable[[], dict[str, Any]]) -> None:
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
        self.baseline: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []
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
            current_ok = (isinstance(current, (int, float))
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
            return {"ok": False, "error": "风扇标定只允许使用整车 CANB 500 kbit/s；禁止使用 Legacy 250 kbit/s"}

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
        if not _fresh_age(pdm_age, 1.0) or bus.get("offline", True):
            return {"ok": False, "error": "PDM 低压总线遥测离线或超时（>1.0s），无法进行标定"}
        if not _fresh_age(fan_status_age, 1.0):
            return {"ok": False, "error": "FanController 0x5A2 状态超时（>1.0s），无法进行标定"}
        if not _fresh_age(fan_diag_age, 1.0):
            return {"ok": False, "error": "FanController 0x5A3 诊断超时（>1.0s），无法进行标定"}
        if not _fresh_age(power_status_age, 1.0):
            return {"ok": False, "error": "FanController 0x5A8 功率状态超时（>1.0s），无法进行标定"}
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
        if (_fresh_age(firmware_calib_age, 1.0)
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
        if not _fresh_age(bus_age, 1.0) or not _fresh_age(bat_age, 1.0):
            return False, "PDM 双路遥测超时（要求两路都 <= 1.0s）"
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
            self.baseline_id = 0
            self._thread = threading.Thread(
                target=self._run_sweep,
                args=(channel, normalized_steps, hold_s, max_current_a, tier),
                name="fan-calib-runner",
                daemon=True,
            )
            self._thread.start()

        return {"ok": True, "message": "标定会话已启动",
                "total_steps": len(normalized_steps) * 2}

    def abort(self, reason: str = "用户手动停止") -> dict[str, Any]:
        """Abort any ongoing calibration immediately and restore AUTO mode."""
        with self._stop_lock:
            with self.lock:
                if self.status != "running":
                    return {"ok": False, "status": self.status, "reason": self.abort_reason,
                            "errors": ["当前没有正在运行的整车风扇自动标定"]}
                self._stop_event.set()
                self.status = "aborted"
                self.abort_reason = reason

            send_result = self._stop_and_restore_auto()
            with self.lock:
                if not send_result["ok"]:
                    self.abort_reason = f"{reason}；固件恢复失败：{'；'.join(send_result['errors'])}"
                status = self.status
                abort_reason = self.abort_reason
            return {
                "ok": send_result["ok"],
                "status": status,
                "reason": abort_reason,
                "errors": send_result["errors"],
            }

    def cancel_for_disconnect(self) -> None:
        """Stop the host worker and invalidate results before the bus closes."""
        self._stop_event.set()
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
        try:
            result = self.send_fn("fan_calib", {
                "action": 3, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0,
            }, True)
            if not result.get("ok"):
                errors.append(f"fan_calib: {result.get('error', '发送失败')}")
            else:
                # The generation baseline is captured after ACK.  A periodic
                # frame received while sending/waiting for ACK is not proof of
                # post-ACK controller state.
                generation = self._calib_generation()
                confirm_error = self._wait_for_calib_state(
                    3, 0, 0, 0, after_generation=generation, timeout_s=1.2)
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
                "Motor_Temp_C", "Controller_Temp_C", "Timestamp"
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
                ])
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
                "exported_at": time.time(),
            }
            return json.dumps(data, ensure_ascii=False, indent=2)

    def _sample_until(self, seconds: float, max_current_a: float,
                      expected_state: int, expected_step: int,
                      expected_duties: tuple[int, int]) -> tuple[list[dict[str, float]], str | None]:
        """采集指定时长内的 PDM 快照样本（约 0.1s 一个）。"""
        samples: list[dict[str, float]] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_event.is_set():
                return samples, "标定已停止"
            snap = self.snapshot_fn()
            safety_error = self._watchdog(
                snap, max_current_a, expected_state,
                expected_step=expected_step, expected_duties=expected_duties)
            if safety_error:
                return samples, safety_error
            bus = snap.get("pdm", {}).get("bus", {})
            fan = snap.get("fan", {})
            fan_status_age = fan.get("status_age")
            fan_diag_age = fan.get("diagnostic_age")
            power_status_age = fan.get("power_status_age")
            fresh = (
                not bus.get("offline", True)
                and _fresh_age(bus.get("age"), 1.0)
                and _fresh_age(fan_status_age, 1.0)
                and _fresh_age(fan_diag_age, 1.0)
                and _fresh_age(power_status_age, 1.0)
            )
            values = (bus.get("voltage_v"), bus.get("current_a"), bus.get("power_w"))
            if fresh and all(isinstance(value, (int, float)) and math.isfinite(value)
                             for value in values):
                samples.append({
                    # 记录每个样本的真实采集时间，不能等采样结束后统一生成。
                    "t": round(time.time(), 3),
                    "v": float(bus["voltage_v"]),
                    "i": float(bus["current_a"]),
                    "p": float(bus["power_w"]),
                })
            time.sleep(0.1)
        return samples, None

    @staticmethod
    def _stable_summary(samples: list[dict[str, float]]) -> tuple[dict[str, float], bool]:
        """返回中位数/离散度；数量不足或波动过大时标记不稳定。"""
        if len(samples) < 10:
            return {"median_v": 0.0, "median_i": 0.0, "median_p": 0.0,
                    "std_i": 0.0, "std_p": 0.0}, False
        median_v = statistics.median(s["v"] for s in samples)
        median_i = statistics.median(s["i"] for s in samples)
        median_p = statistics.median(s["p"] for s in samples)
        std_i = statistics.pstdev(s["i"] for s in samples)
        std_p = statistics.pstdev(s["p"] for s in samples)
        stable = std_i <= 0.05 and std_p <= 2.0
        return {"median_v": median_v, "median_i": median_i, "median_p": median_p,
                "std_i": std_i, "std_p": std_p}, stable

    def _measure_baseline(self, step_label: int, direction: str, max_current_a: float,
                          expected_state: int) -> tuple[dict[str, float] | None, str | None]:
        """先归零并等待 3s，再采集 3s 稳态基线。

        每次测量分配一个递增的 baseline_id，原始样本追加保存而不是覆盖上一组，
        这样导出的记录可以复核每个稳态点实际关联的基线。
        """
        cmd_res = self.send_fn("fan_calib", {
            "action": 1 if self.baseline_id == 0 else 2,
            "step": step_label, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 15,
        }, True)
        if not cmd_res.get("ok"):
            return None, f"0% 基线命令失败：{cmd_res.get('error', '发送失败')}"
        generation = self._calib_generation()
        confirm_error = self._wait_for_calib_state(
            1, step_label, 0, 0, after_generation=generation,
            max_current_a=max_current_a, expected_supply_state=expected_state)
        if confirm_error:
            return None, confirm_error
        _, error = self._sample_until(
            self.SETTLE_S, max_current_a, expected_state, step_label, (0, 0))
        if error:
            return None, error
        if self._stop_event.is_set():
            return None, "标定已停止"
        samples, error = self._sample_until(
            self.SAMPLE_S, max_current_a, expected_state, step_label, (0, 0))
        if error:
            return None, error
        summary, stable = self._stable_summary(samples)
        if not samples or not stable:
            extra, error = self._sample_until(
                self.SAMPLE_S, max_current_a, expected_state, step_label, (0, 0))
            if error:
                return None, error
            samples.extend(extra)
            summary, stable = self._stable_summary(samples)
            if not samples or not stable:
                return None, "0% 基线样本不足或功率未稳定"
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
            } for s in samples])
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
                "captured_at": round(time.time(), 3),
            })
        return {
            "baseline_id": baseline_id,
            "voltage_v": round(summary["median_v"], 3),
            "current_a": round(summary["median_i"], 3),
            "power_w": round(summary["median_p"], 2),
            "sample_count": len(samples),
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
                              *, after_generation: int, timeout_s: float = 1.2,
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
            fresh_new_frame = generation > after_generation and _fresh_age(age, 1.0)
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
                return ("FanController已拒绝或中止标定状态"
                        f"（{calib.get('calib_state_name', calib.get('calib_state'))}，"
                        f"原因：{calib.get('calib_abort_name', '未知')}）")
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
        if bus.get("offline", True) or not _fresh_age(bus.get("age"), 1.0):
            return "PDM 遥测超时 (>1.0s)，触发安全中止"
        fan_status_age = fan.get("status_age")
        fan_diag_age = fan.get("diagnostic_age")
        power_status_age = fan.get("power_status_age")
        if not _fresh_age(fan_status_age, 1.0):
            return "FanController 0x5A2 状态超时 (>1.0s)，触发安全中止"
        if not _fresh_age(fan_diag_age, 1.0):
            return "FanController 0x5A3 诊断超时 (>1.0s)，触发安全中止"
        if not _fresh_age(power_status_age, 1.0):
            return "FanController 0x5A8 功率状态超时 (>1.0s)，触发安全中止"
        if require_calibration_active:
            calib = fan.get("calib_status", {})
            if not _fresh_age(fan.get("calib_status_age"), 1.0):
                return "FanController 0x5A9 标定状态超时 (>1.0s)，触发安全中止"
            targets = calib.get("calib_target_pct")
            if (calib.get("calib_state") != 1
                    or not isinstance(calib.get("lease_remaining_s"), (int, float))
                    or calib.get("lease_remaining_s") <= 0):
                return f"FanController 标定会话已不活动（{calib.get('calib_state_name', '未知')}）"
            if expected_step is not None and calib.get("step") != expected_step:
                return f"FanController 标定步骤被外部改写（期望 {expected_step}，当前 {calib.get('step')}）"
            if (expected_duties is not None
                    and (not isinstance(targets, list) or len(targets) < 2
                         or tuple(targets[:2]) != expected_duties)):
                return ("FanController 标定目标被外部改写"
                        f"（期望 {list(expected_duties)}，当前 {targets}）")
        if fan_power.get("power_supply_state") != expected_state:
            state_name = fan_power.get("power_supply_name", str(fan_power.get("power_supply_state")))
            return f"供电脱离所选标定档位（当前：{state_name}），触发安全中止"
        # 扫频本来就要测出最低起转占空比。低占空比 START_KICK 结束时，固件会把
        # 尚未起转写进 0x5A3 TACH 位；这只是当前测点的结果，不能由上位机抢先
        # 当成安全中止。真实运行停转仍由固件安全看门狗确认并把 0x5A9 切到
        # ABORTED，本函数上面的会话活动检查会立即中止；记录中的 0 RPM 也不会
        # 被 _max_safe_duty() 选为推荐上限。
        # 温度失联或温度无效时继续标定等于没有温度保护，必须中止。
        if fan_diag.get("faults", 0) & (FAULT_MOTOR_TEMP_STALE | FAULT_CTRL_TEMP_STALE):
            return "温度输入失联，触发安全中止"

        i_curr = bus.get("current_a")
        if not isinstance(i_curr, (int, float)) or not math.isfinite(i_curr):
            return "PDM 总线电流无效，触发安全中止"
        if not all(isinstance(bus.get(key), (int, float)) and math.isfinite(bus[key])
                   for key in ("voltage_v", "power_w")):
            return "PDM 总线电压或功率无效，触发安全中止"
        if i_curr > max_current_a:
            return f"总线电流 ({i_curr:.1f} A) 超过安全限制 ({max_current_a:.1f} A)"
        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (motor_temp, ctrl_temp)):
            return "电机或控制器温度无效（0x5A3 上报 0x7FFF），触发安全中止"
        if motor_temp >= CALIB_ABORT_MOTOR_TEMP_C:
            return f"电机温度超限 ({motor_temp:.1f} ℃ >= {CALIB_ABORT_MOTOR_TEMP_C:.0f} ℃)"
        if ctrl_temp >= CALIB_ABORT_CONTROLLER_TEMP_C:
            return f"控制器温度超限 ({ctrl_temp:.1f} ℃ >= {CALIB_ABORT_CONTROLLER_TEMP_C:.0f} ℃)"
        return None

    def _apply_step(self, step: int, duty1: int, duty2: int,
                    hold_s: float, max_current_a: float,
                    baseline: dict[str, float],
                    direction: str = "",
                    baseline_id: int = 0, expected_state: int = 3) -> dict[str, Any] | None:
        """下发一个目标点，等待稳定并采集后 3s 中位数，返回记录或 None。"""
        cmd_res = self.send_fn("fan_calib", {
            "action": 2, "step": step, "duty1_pct": duty1, "duty2_pct": duty2, "lease_s": 15,
        }, True)
        if not cmd_res.get("ok"):
            self._abort_and_return(f"下发标定步骤 {step} 命令失败：{cmd_res.get('error')}")
            return None
        generation = self._calib_generation()
        confirm_error = self._wait_for_calib_state(
            1, step, duty1, duty2, after_generation=generation,
            max_current_a=max_current_a, expected_supply_state=expected_state)
        if confirm_error:
            self._abort_and_return(confirm_error)
            return None

        # 前 3s 让占空比和转速稳定；同时持续执行安全看门狗。
        settle_end = time.monotonic() + self.SETTLE_S
        while time.monotonic() < settle_end:
            if self._stop_event.is_set():
                return None
            reason = self._watchdog(
                self.snapshot_fn(), max_current_a, expected_state,
                expected_step=step, expected_duties=(duty1, duty2))
            if reason:
                self._abort_and_return(reason)
                return None
            time.sleep(0.1)

        with self.lock:
            self.current_step = step
            self.current_duty = [duty1, duty2]

        samples: list[dict[str, float]] = []
        sample_end = time.monotonic() + (hold_s - self.SETTLE_S)
        while time.monotonic() < sample_end:
            if self._stop_event.is_set():
                return None
            snap = self.snapshot_fn()
            reason = self._watchdog(
                snap, max_current_a, expected_state,
                expected_step=step, expected_duties=(duty1, duty2))
            if reason:
                self._abort_and_return(reason)
                return None
            fan_status = snap.get("fan", {}).get("status", {})
            fan_diag = snap.get("fan", {}).get("diagnostic", {})
            bus = snap.get("pdm", {}).get("bus", {})
            if not bus.get("offline", True) and bus.get("current_a") is not None:
                rpm = fan_status.get("rpm", [0, 0, 0]) or [0, 0, 0]
                samples.append({
                    "t": round(time.time(), 3),
                    "v": float(bus["voltage_v"]),
                    "i": float(bus["current_a"]),
                    "p": float(bus["power_w"]),
                    "rpm1": rpm[0] if len(rpm) > 0 else 0,
                    "rpm2": rpm[1] if len(rpm) > 1 else 0,
                    "rpm3": rpm[2] if len(rpm) > 2 else 0,
                    "mt": fan_diag.get("motor_temp_c"),
                    "ct": fan_diag.get("controller_temp_c"),
                })
                with self.lock:
                    self.raw_samples.append({
                        "step": step,
                        "direction": direction,
                        "duty1_pct": duty1,
                        "duty2_pct": duty2,
                        **samples[-1],
                        "baseline_id": baseline_id,
                        "baseline_current_a": baseline.get("current_a"),
                        "baseline_power_w": baseline.get("power_w"),
                        "timestamp": samples[-1]["t"],
                    })
            time.sleep(0.1)

        summary, stable = self._stable_summary(samples)
        if not samples or not stable:
            # 波动过大或样本不足：延长一轮再采集一次，避免把瞬态当成稳态。
            extra: list[dict[str, float]] = []
            extra_end = time.monotonic() + self.SAMPLE_S
            while time.monotonic() < extra_end:
                if self._stop_event.is_set():
                    return None
                extra_snap = self.snapshot_fn()
                reason = self._watchdog(
                    extra_snap, max_current_a, expected_state,
                    expected_step=step, expected_duties=(duty1, duty2))
                if reason:
                    self._abort_and_return(reason)
                    return None
                extra_bus = extra_snap.get("pdm", {}).get("bus", {})
                extra_status = extra_snap.get("fan", {}).get("status", {})
                extra_diag = extra_snap.get("fan", {}).get("diagnostic", {})
                if (not extra_bus.get("offline", True)
                        and _fresh_age(extra_bus.get("age"), 1.0)
                        and extra_bus.get("current_a") is not None):
                    extra_rpm = extra_status.get("rpm", [0, 0, 0]) or [0, 0, 0]
                    extra.append({
                        "t": round(time.time(), 3),
                        "v": float(extra_bus["voltage_v"]),
                        "i": float(extra_bus["current_a"]),
                        "p": float(extra_bus["power_w"]),
                        "rpm1": extra_rpm[0] if len(extra_rpm) > 0 else 0,
                        "rpm2": extra_rpm[1] if len(extra_rpm) > 1 else 0,
                        "rpm3": extra_rpm[2] if len(extra_rpm) > 2 else 0,
                        "mt": extra_diag.get("motor_temp_c"),
                        "ct": extra_diag.get("controller_temp_c"),
                    })
                    with self.lock:
                        self.raw_samples.append({
                            "step": step,
                            "direction": direction,
                            "duty1_pct": duty1,
                            "duty2_pct": duty2,
                            **extra[-1],
                            "baseline_id": baseline_id,
                            "baseline_current_a": baseline.get("current_a"),
                            "baseline_power_w": baseline.get("power_w"),
                            "retry_sample": True,
                            "timestamp": extra[-1]["t"],
                        })
                time.sleep(0.1)
            samples.extend(extra)
            summary, stable = self._stable_summary(samples)
            if not samples or not stable:
                self._abort_and_return(f"步骤 {step} 数据波动过大或样本不足，中止标定")
                return None

        last = samples[-1]
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
                result = self._stop_and_restore_auto()
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
        except Exception as exc:
            self._abort_and_return(f"标定执行发生异常：{exc}")


class BatteryFanCalibrationSession:
    """F405 battery-box fan sweep using PDM power with a nearby 0% baseline."""

    DEFAULT_STEPS = [0, 5, 10, 15, 20, 30, 40, 50, 55, 60, 70, 80, 90, 100]
    BASELINE_INTERVAL = 4
    # 步骤采样不足或波动过大时固定延长一轮的窗口；hold_s=10 时全程仍为
    # 2+8+2=12s，加上命令确认也在 15s 租约之内。
    RETRY_SAMPLE_S = 2.0

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
        # None means that the sweep did not produce a safe, rotating point for
        # that budget.  Never turn absence of evidence into a 5% recommendation.
        self.suggested_caps: dict[str, int | None] = {
            "chroma_cap_pct": None, "hv_cap_pct": None,
        }
        self.run_params: dict[str, Any] = {}
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
            power_ok = (isinstance(power, (int, float)) and math.isfinite(power)
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
        if not _fresh_age(pack.get("age"), 1.5) or pack.get("state") != 5:
            return "BMS必须处于新鲜的高压接通状态"
        if not pack.get("temperature_complete", False):
            return "BMS温度采样不完整，禁止在缺少完整温度保护时标定"
        if pdm.get("offline", True) or not _fresh_age(pdm.get("age"), 1.0):
            return "PDM总线遥测离线或超时"
        pdm_values = (pdm.get("voltage_v"), pdm.get("current_a"), pdm.get("power_w"))
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in pdm_values):
            return "PDM总线电压、电流或功率无效"
        if not _fresh_age(battery.get("status_age"), 1.0):
            return "电池箱风扇0x5AA状态超时；请先查询"
        calibration = battery.get("calibration", {})
        if not _fresh_age(battery.get("calibration_age"), 1.0):
            return "电池箱风扇0x5AD标定状态超时；请先查询"
        if status.get("protocol_version") != 1:
            return "电池箱风扇协议版本不匹配（0x5AA必须为版本1）"
        if calibration.get("chroma_budget_w") != 35 or calibration.get("hv_budget_w") != 70:
            return "电池箱风扇功率预算版本不匹配（0x5AD必须为35W/70W）"
        if status.get("power_source") != 2:
            return f"电池箱风扇当前供电不是高压/DCDC（{status.get('power_source_name', '未知')}）"
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
            self.suggested_caps = {"chroma_cap_pct": None, "hv_cap_pct": None}
            self.run_params = {"steps": steps, "hold_s": hold_s, "max_current_a": max_current_a}
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
        if len(samples) < 10:
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
                            timeout_s: float = 1.2) -> str | None:
        """Require new 0x5AA/0x5AD frames proving STOP reached COMPLETED."""
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
            if (status_generation > after_status_generation
                    and calibration_generation > after_calibration_generation
                    and _fresh_age(battery.get("status_age"), 1.0)
                    and _fresh_age(battery.get("calibration_age"), 1.0)
                    and not active and calibration.get("calib_state") == 3
                    and calibration.get("target_duty_pct") == 0):
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
        if error or summary is None or summary["std_i"] > 0.05 or summary["std_p"] > 2.0:
            return None, error or "0%基线样本不足或功率未稳定"
        with self.lock:
            self.baseline_id += 1
            measured = {key: round(summary[key], 3) for key in ("v", "i", "p", "rpm", "std_i", "std_p")}
            measured["baseline_id"] = self.baseline_id
            measured["step"] = step
            self.baseline = dict(measured)
            self.baseline_history.append(dict(measured))
        return measured, None

    def _finish_abort(self, reason: str) -> dict[str, Any]:
        with self._stop_lock:
            with self.lock:
                if self.status != "running":
                    return {"ok": True, "status": self.status, "reason": self.abort_reason,
                            "errors": []}
                self._stop_event.set()
            try:
                stop_result = self._send(3, self.current_step, 0, 0)
            except Exception as exc:
                stop_result = {"ok": False, "error": str(exc)}
            if stop_result.get("ok"):
                status_generation, calibration_generation = self._generations()
                confirm_error = self._wait_for_completed(
                    after_status_generation=status_generation,
                    after_calibration_generation=calibration_generation)
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
                if not error and (summary is None or summary["std_i"] > 0.05
                                  or summary["std_p"] > 2.0):
                    # 与整车风扇会话对齐：最小 hold 时采样窗口只有 1s，
                    # 样本数或波动卡在门槛上时延长一轮再判，不把瞬态当成稳态。
                    extra, extra_error = self._samples(
                        self.RETRY_SAMPLE_S, max_current_a, index, int(duty))
                    samples.extend(extra)
                    summary = self._median(samples)
                    error = error or extra_error
                if error or summary is None or summary["std_i"] > 0.05 or summary["std_p"] > 2.0:
                    self._finish_abort(error or f"步骤{index}样本不足或功率未稳定")
                    return
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
                }
                with self.lock:
                    self.current_step = index
                    self.records.append(record)
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
                "baseline_id", "std_current_a", "std_power_w"])
            writer.writeheader()
            writer.writerows(self.records)
            return output.getvalue()
