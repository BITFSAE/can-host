"""FanController calibration session manager and automated sweep runner.

Runs controlled calibration sweeps over PWM1 (dual 2H4PU) and PWM2 (single 2H6P),
measures baseline PDM bus power/current, samples steady-state RPM and delta I/P,
and strictly enforces safety gating (DCDC_READY, temperature limits, stall guards).
"""

from __future__ import annotations

import csv
from io import StringIO
import json
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
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def check_preconditions(self) -> dict[str, Any]:
        """Verify bus and vehicle safety conditions before calibration."""
        snap = self.snapshot_fn()
        conn = snap.get("connection", {})
        if not conn.get("connected", False) or conn.get("mode") != "pcan":
            return {"ok": False, "error": "请先连接真实 PCAN 上的 CANB，禁止模拟连接发送标定命令"}

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
        if pdm_age is None or pdm_age > 1.0 or bus.get("offline", True):
            return {"ok": False, "error": "PDM 低压总线遥测离线或超时（>1.0s），无法进行标定"}
        if fan_status_age is None or fan_status_age > 1.0:
            return {"ok": False, "error": "FanController 0x5A2 状态超时（>1.0s），无法进行标定"}
        if fan_diag_age is None or fan_diag_age > 1.0:
            return {"ok": False, "error": "FanController 0x5A3 诊断超时（>1.0s），无法进行标定"}
        if power_status_age is None or power_status_age > 1.0:
            return {"ok": False, "error": "FanController 0x5A8 功率状态超时（>1.0s），无法进行标定"}

        power_supply_state = power_status.get("power_supply_state")
        # In simulation mode, allow calibration if simulated state is DcdcReady (3) or not yet sent
        if power_supply_state not in (None, 1, 2, 3):
            # 固件自动识别未启用时，允许在发送 Action=4 确认后再进入 DCDC_READY。
            state_name = power_status.get("power_supply_name", str(power_supply_state))
            return {"ok": False, "error": f"供电状态异常（当前：{state_name}），禁止开始标定"}

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
        if motor_temp is None or ctrl_temp is None:
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
        if bus_age is None or bat_age is None or bus_age > 1.0 or bat_age > 1.0:
            return False, "PDM 双路遥测超时（要求两路都 <= 1.0s）"
        v_bus = bus.get("voltage_v")
        v_bat = battery.get("voltage_v")
        i_bat = battery.get("current_a")
        if v_bus is None or v_bat is None or i_bat is None:
            return False, "PDM 双路遥测数据不完整"
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
                    max_current_a: float = 18.0) -> dict[str, Any]:
        """Start a background automated calibration sweep."""
        if channel not in (1, 2):
            return {"ok": False, "error": "计划要求先分别标定回路 1/2，暂不开放双回路联合扫频"}
        # 前置检查（内部会经 vehicle_snapshot() -> get_snapshot()）必须放在
        # 本锁外，不能持有 session 锁去获取 service 锁，否则 service 侧
        # 同步调用会反过来获取 session 锁，形成跨锁死锁。
        pre_check = self.check_preconditions()
        if not pre_check["ok"]:
            return pre_check

        # 用 PDM 原始测量值独立验证 DCDC 真的在供电，并连续稳定 3 秒。
        # 不采信固件上报的供电状态本身：Action=4 手动覆盖也会上报同一个状态，
        # 只检查 power_supply_state == 3 无法区分“自动识别”和“人工覆盖”。
        dcdc_check = self._verify_dcdc_ready(self.DCDC_STABLE_REQUIRED_S)
        if not dcdc_check["ok"]:
            return dcdc_check

        # 固件状态与实测不一致时同样拒绝：说明供电状态来自覆盖或已过期。
        current_state = self.snapshot_fn().get("fan", {}).get("power_status", {}).get("power_supply_state")
        if current_state != 3:
            state_name = self.snapshot_fn().get("fan", {}).get("power_status", {}).get(
                "power_supply_name", str(current_state))
            return {
                "ok": False,
                "error": f"PDM 实测判据满足，但固件上报供电为 {state_name}；"
                         "两侧不一致时禁止开始自动扫频，请检查 DCDC 与 PDM 接线。",
            }

        # 起始总线负载必须足够低，否则扫到高占空比时必然触发电流保护。
        start_current = self.snapshot_fn().get("pdm", {}).get("bus", {}).get("current_a")
        if start_current is None:
            return {"ok": False, "error": "无法读取总线电流，禁止开始标定"}
        if start_current > CALIB_MAX_START_BUS_CURRENT_A:
            return {"ok": False, "error":
                    f"整车基础总线电流 {start_current:.2f} A 超过开始标定门槛 "
                    f"{CALIB_MAX_START_BUS_CURRENT_A:.1f} A，请先关闭其他低压负载"}

        if steps is None:
            steps = list(self.DEFAULT_STEPS)
        if not steps or any(not (0 <= int(d) <= 100) for d in steps):
            return {"ok": False, "error": "扫描点必须在 0..100 % 且至少包含一个点"}
        if not 3.0 <= hold_s <= 10.0:
            return {"ok": False, "error": "稳态保持时间必须在 3..10 秒（含 3s 稳定 + 采样窗口）"}
        if not 5.0 <= max_current_a <= 20.0:
            return {"ok": False, "error": "总线电流保护必须在 5..20 A"}

        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "标定会话已在进行中"}

            self.status = "running"
            self.abort_reason = ""
            self.channel = channel
            self.current_step = 0
            # 双向扫描执行 len(steps) * 2 个点。
            self.total_steps = len(steps) * 2
            self.current_duty = [0, 0]
            self.baseline.clear()
            self.records.clear()
            self.run_params = {
                "channel": channel,
                "steps": list(steps),
                "hold_s": hold_s,
                "max_current_a": max_current_a,
            }
            self.raw_samples.clear()
            self.baseline_raw_samples.clear()
            self.baseline_history.clear()
            self.baseline_id = 0
            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._run_sweep,
                args=(channel, steps, hold_s, max_current_a),
                name="fan-calib-runner",
                daemon=True,
            )
            self._thread.start()

        return {"ok": True, "message": "标定会话已启动", "total_steps": len(steps)}

    def abort(self, reason: str = "用户手动停止") -> dict[str, Any]:
        """Abort any ongoing calibration immediately and restore AUTO mode."""
        self._stop_event.set()
        with self.lock:
            if self.status == "running":
                self.status = "aborted"
                self.abort_reason = reason

        send_result = self._stop_and_restore_auto()
        with self.lock:
            if not send_result["ok"]:
                self.status = "aborted"
                self.abort_reason = f"{reason}；固件恢复失败：{'；'.join(send_result['errors'])}"
        return {
            "ok": send_result["ok"],
            "status": self.status,
            "reason": self.abort_reason,
            "errors": send_result["errors"],
        }

    def _stop_and_restore_auto(self) -> dict[str, Any]:
        """停止标定并通过确认命令恢复自动模式。"""
        commands = [
            ("fan_calib", {"action": 3, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0}),
            ("fan_control", {"mode": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0}),
        ]
        errors: list[str] = []
        for name, values in commands:
            try:
                result = self.send_fn(name, values, True)
                if not result.get("ok"):
                    errors.append(f"{name}: {result.get('error', '发送失败')}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return {"ok": not errors, "errors": errors}

    def get_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "abort_reason": self.abort_reason,
                "channel": self.channel,
                "current_step": self.current_step,
                "total_steps": self.total_steps,
                "current_duty": list(self.current_duty),
                "baseline": dict(self.baseline),
                "records": list(self.records),
                "run_params": dict(self.run_params),
                "raw_samples": list(self.raw_samples),
                "baseline_raw_samples": list(self.baseline_raw_samples),
                "baseline_history": list(self.baseline_history),
                "baseline_id": self.baseline_id,
            }

    def export_csv(self) -> str:
        with self.lock:
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Step", "Channel", "Direction", "PWM1_Duty_Pct", "PWM2_Duty_Pct",
                "Fan1_RPM", "Fan2_RPM", "Fan3_RPM",
                "Bus_Voltage_V", "Bus_Current_A", "Bus_Power_W",
                "Delta_Current_A", "Delta_Power_W",
                "Baseline_ID", "Baseline_Current_A", "Baseline_Power_W",
                "Motor_Temp_C", "Controller_Temp_C", "Timestamp"
            ])
            for r in self.records:
                writer.writerow([
                    r.get("step"), r.get("channel"), r.get("direction", ""),
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

    def _sample_until(self, seconds: float) -> list[dict[str, float]]:
        """采集指定时长内的 PDM 快照样本（约 0.1s 一个）。"""
        samples: list[dict[str, float]] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_event.is_set():
                return samples
            snap = self.snapshot_fn()
            bus = snap.get("pdm", {}).get("bus", {})
            fan = snap.get("fan", {})
            fan_status_age = fan.get("status_age")
            fan_diag_age = fan.get("diagnostic_age")
            power_status_age = fan.get("power_status_age")
            fresh = (
                not bus.get("offline", True)
                and bus.get("age") is not None
                and bus.get("age") <= 1.0
                and fan_status_age is not None and fan_status_age <= 1.0
                and fan_diag_age is not None and fan_diag_age <= 1.0
                and power_status_age is not None and power_status_age <= 1.0
            )
            if fresh and bus.get("current_a") is not None:
                samples.append({
                    # 记录每个样本的真实采集时间，不能等采样结束后统一生成。
                    "t": round(time.time(), 3),
                    "v": bus.get("voltage_v") or 24.0,
                    "i": bus.get("current_a") or 0.0,
                    "p": bus.get("power_w") or 0.0,
                })
            time.sleep(0.1)
        return samples

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

    def _measure_baseline(self, step_label: int, direction: str = "") -> dict[str, float] | None:
        """先归零并等待 3s，再采集 3s 稳态基线。

        每次测量分配一个递增的 baseline_id，原始样本追加保存而不是覆盖上一组，
        这样导出的记录可以复核每个稳态点实际关联的基线。
        """
        cmd_res = self.send_fn("fan_calib", {
            "action": 1, "step": step_label, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 15,
        }, True)
        if not cmd_res.get("ok"):
            return None
        self._sample_until(self.SETTLE_S)
        if self._stop_event.is_set():
            return None
        samples = self._sample_until(self.SAMPLE_S)
        summary, stable = self._stable_summary(samples)
        if not samples or not stable:
            extra = self._sample_until(self.SAMPLE_S)
            samples.extend(extra)
            summary, stable = self._stable_summary(samples)
            if not samples or not stable:
                return None
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
        }

    def _abort_and_return(self, reason: str) -> None:
        self.abort(reason)

    def _wait_for_dcdc_confirm(self, timeout_s: float = 3.0) -> bool:
        """等待固件上报 DCDC_READY；超时或连接失败返回 False。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            snap = self.snapshot_fn()
            state = snap.get("fan", {}).get("power_status", {}).get("power_supply_state")
            if state == 3:
                return True
            time.sleep(0.1)
        return False

    def _watchdog(self, snap: dict[str, Any], max_current_a: float) -> str | None:
        """扫描期间持续安全检查，返回中止原因；None 表示通过。"""
        conn = snap.get("connection", {})
        fan = snap.get("fan", {})
        fan_diag = fan.get("diagnostic", {})
        fan_power = fan.get("power_status", {})
        bus = snap.get("pdm", {}).get("bus", {})

        if conn.get("mode") != "pcan":
            return "连接模式已变化，触发安全中止"
        if bus.get("offline", True) or (bus.get("age") is not None and bus.get("age") > 1.0):
            return "PDM 遥测超时 (>1.0s)，触发安全中止"
        fan_status_age = fan.get("status_age")
        fan_diag_age = fan.get("diagnostic_age")
        power_status_age = fan.get("power_status_age")
        if fan_status_age is None or fan_status_age > 1.0:
            return "FanController 0x5A2 状态超时 (>1.0s)，触发安全中止"
        if fan_diag_age is None or fan_diag_age > 1.0:
            return "FanController 0x5A3 诊断超时 (>1.0s)，触发安全中止"
        if power_status_age is None or power_status_age > 1.0:
            return "FanController 0x5A8 功率状态超时 (>1.0s)，触发安全中止"
        if fan_power.get("power_supply_state") not in (None, 3):
            state_name = fan_power.get("power_supply_name", str(fan_power.get("power_supply_state")))
            return f"供电脱离 DCDC_READY 状态（当前：{state_name}），触发安全中止"
        if fan_diag.get("faults", 0) & FAULT_TACH_MASK:
            return "检测到风扇停转故障，触发安全中止"
        # 温度失联或温度无效时继续标定等于没有温度保护，必须中止。
        if fan_diag.get("faults", 0) & (FAULT_MOTOR_TEMP_STALE | FAULT_CTRL_TEMP_STALE):
            return "温度输入失联，触发安全中止"

        i_curr = bus.get("current_a") or 0.0
        if i_curr > max_current_a:
            return f"总线电流 ({i_curr:.1f} A) 超过安全限制 ({max_current_a:.1f} A)"
        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        if motor_temp is None or ctrl_temp is None:
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
                    baseline_id: int = 0) -> dict[str, Any] | None:
        """下发一个目标点，等待稳定并采集后 3s 中位数，返回记录或 None。"""
        cmd_res = self.send_fn("fan_calib", {
            "action": 2, "step": step, "duty1_pct": duty1, "duty2_pct": duty2, "lease_s": 15,
        }, True)
        if not cmd_res.get("ok"):
            self._abort_and_return(f"下发标定步骤 {step} 命令失败：{cmd_res.get('error')}")
            return None

        # 前 3s 让占空比和转速稳定；同时持续执行安全看门狗。
        settle_end = time.monotonic() + self.SETTLE_S
        while time.monotonic() < settle_end:
            if self._stop_event.is_set():
                return None
            reason = self._watchdog(self.snapshot_fn(), max_current_a)
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
            reason = self._watchdog(snap, max_current_a)
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
                    "v": bus.get("voltage_v") or 24.0,
                    "i": bus.get("current_a") or 0.0,
                    "p": bus.get("power_w") or 0.0,
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
                reason = self._watchdog(extra_snap, max_current_a)
                if reason:
                    self._abort_and_return(reason)
                    return None
                extra_bus = extra_snap.get("pdm", {}).get("bus", {})
                extra_status = extra_snap.get("fan", {}).get("status", {})
                extra_diag = extra_snap.get("fan", {}).get("diagnostic", {})
                if (not extra_bus.get("offline", True)
                        and extra_bus.get("age") is not None
                        and extra_bus.get("age") <= 1.0
                        and extra_bus.get("current_a") is not None):
                    extra_rpm = extra_status.get("rpm", [0, 0, 0]) or [0, 0, 0]
                    extra.append({
                        "t": round(time.time(), 3),
                        "v": extra_bus.get("voltage_v") or 24.0,
                        "i": extra_bus.get("current_a") or 0.0,
                        "p": extra_bus.get("power_w") or 0.0,
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
                   hold_s: float, max_current_a: float) -> None:
        """执行计划中的上升+下降双向阶梯扫频。"""
        try:
            current_state = self.snapshot_fn().get("fan", {}).get("power_status", {}).get("power_supply_state")
            if current_state != 3:
                self._abort_and_return("启动后供电已离开 DCDC_READY，中止标定")
                return

            baseline = self._measure_baseline(0, "initial")
            if baseline is None:
                self._abort_and_return("无法采集 0% 静态基线，中止标定")
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
                        fresh = self._measure_baseline(index, direction_name)
                        if fresh is None:
                            self._abort_and_return(f"步骤 {index} 前方重新采集 0% 基线失败，中止标定")
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
                    )
                    if record is None:
                        return
                    record_index += 1
                    with self.lock:
                        self.current_step = record_index
                        self.records.append(record)

            result = self._stop_and_restore_auto()
            with self.lock:
                if result["ok"]:
                    self.status = "completed"
                    self.current_duty = [0, 0]
                else:
                    self.status = "aborted"
                    self.abort_reason = "扫描完成但固件恢复自动失败：" + "；".join(result["errors"])
        except Exception as exc:
            self._abort_and_return(f"标定执行发生异常：{exc}")
