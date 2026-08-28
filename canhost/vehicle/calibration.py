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

        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        if motor_temp is not None and motor_temp >= 70.0:
            return {"ok": False, "error": f"电机温度过高 ({motor_temp:.1f} ℃ >= 70 ℃)，禁止开启标定"}
        if ctrl_temp is not None and ctrl_temp >= 65.0:
            return {"ok": False, "error": f"控制器温度过高 ({ctrl_temp:.1f} ℃ >= 65 ℃)，禁止开启标定"}

        faults = fan_diag.get("faults", 0)
        if faults & 0x07:
            return {"ok": False, "error": "存在风扇停转故障，请先排除硬件问题"}

        return {"ok": True}

    def start_sweep(self, channel: int = 1, steps: list[int] | None = None,
                    hold_s: float = DEFAULT_HOLD_S,
                    max_current_a: float = 18.0,
                    confirm_dcdc: bool = False) -> dict[str, Any]:
        """Start a background automated calibration sweep."""
        if channel not in (1, 2):
            return {"ok": False, "error": "计划要求先分别标定回路 1/2，暂不开放双回路联合扫频"}
        # 前置检查（内部会经 vehicle_snapshot() -> get_snapshot()）必须放在
        # 本锁外，不能持有 session 锁去获取 service 锁，否则 service 侧
        # 同步调用会反过来获取 session 锁，形成跨锁死锁。
        pre_check = self.check_preconditions()
        if not pre_check["ok"]:
            return pre_check

        current_state = self.snapshot_fn().get("fan", {}).get("power_status", {}).get("power_supply_state")
        if current_state != 3 and not confirm_dcdc:
            state_name = self.snapshot_fn().get("fan", {}).get("power_status", {}).get(
                "power_supply_name", str(current_state))
            return {
                "ok": False,
                "error": f"当前供电为 {state_name}，不是 DCDC_READY；"
                         "请先确认实物 DCDC 正常供电并点击“确认 DCDC 就绪”。",
            }

        if steps is None:
            steps = list(self.DEFAULT_STEPS)
        if not steps or any(not (0 <= int(d) <= 100) for d in steps):
            return {"ok": False, "error": "扫描点必须在 0..100 % 且至少包含一个点"}

        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "标定会话已在进行中"}

            self.status = "running"
            self.abort_reason = ""
            self.channel = channel
            self.current_step = 0
            self.total_steps = len(steps)
            self.current_duty = [0, 0]
            self.baseline.clear()
            self.records.clear()
            self.run_params = {
                "channel": channel,
                "steps": list(steps),
                "hold_s": hold_s,
                "max_current_a": max_current_a,
                "confirm_dcdc": confirm_dcdc,
            }
            self.raw_samples.clear()
            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._run_sweep,
                args=(channel, steps, hold_s, max_current_a, confirm_dcdc),
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
            }

    def export_csv(self) -> str:
        with self.lock:
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Step", "Channel", "PWM1_Duty_Pct", "PWM2_Duty_Pct",
                "Fan1_RPM", "Fan2_RPM", "Fan3_RPM",
                "Bus_Voltage_V", "Bus_Current_A", "Bus_Power_W",
                "Delta_Current_A", "Delta_Power_W",
                "Baseline_Current_A", "Baseline_Power_W",
                "Motor_Temp_C", "Controller_Temp_C", "Timestamp"
            ])
            for r in self.records:
                writer.writerow([
                    r.get("step"), r.get("channel"),
                    r.get("duty1_pct"), r.get("duty2_pct"),
                    r.get("rpm1"), r.get("rpm2"), r.get("rpm3"),
                    r.get("voltage_v"), r.get("current_a"), r.get("power_w"),
                    r.get("delta_current_a"), r.get("delta_power_w"),
                    r.get("baseline_current_a"), r.get("baseline_power_w"),
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
                "records": self.records,
                "raw_samples": list(self.raw_samples),
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

    def _measure_baseline(self, step_label: int) -> dict[str, float] | None:
        """先归零并等待 3s，再采集 3s 稳态基线。"""
        cmd_res = self.send_fn("fan_calib", {
            "action": 1, "step": step_label, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 15,
        }, True)
        if not cmd_res.get("ok"):
            return None
        self._sample_until(self.SETTLE_S)
        if self._stop_event.is_set():
            return None
        samples = self._sample_until(self.SAMPLE_S)
        summary, _ = self._stable_summary(samples)
        if not samples:
            return None
        return {
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
        if fan_diag.get("faults", 0) & 0x07:
            return "检测到风扇停转故障，触发安全中止"

        i_curr = bus.get("current_a") or 0.0
        if i_curr > max_current_a:
            return f"总线电流 ({i_curr:.1f} A) 超过安全限制 ({max_current_a:.1f} A)"
        motor_temp = fan_diag.get("motor_temp_c")
        ctrl_temp = fan_diag.get("controller_temp_c")
        if motor_temp is not None and motor_temp >= 72.0:
            return f"电机温度超限 ({motor_temp:.1f} ℃ >= 72 ℃)"
        if ctrl_temp is not None and ctrl_temp >= 68.0:
            return f"控制器温度超限 ({ctrl_temp:.1f} ℃ >= 68 ℃)"
        return None

    def _apply_step(self, step: int, duty1: int, duty2: int,
                    hold_s: float, max_current_a: float,
                    baseline: dict[str, float],
                    confirm_dcdc: bool = False) -> dict[str, Any] | None:
        """下发一个目标点，等待稳定并采集后 3s 中位数，返回记录或 None。"""
        if confirm_dcdc:
            # 独立确认的 Action=4 只有 60s 租约；每个步骤开始时续一次，
            # 避免 170s 双向扫描中途因租约到期被固件降回 BATTERY。
            renew = self.send_fn("fan_calib", {
                "action": 4, "step": 0, "duty1_pct": 0, "duty2_pct": 0,
                "lease_s": 60,
            }, True)
            if not renew.get("ok"):
                self._abort_and_return(f"续订 DCDC 就绪确认失败：{renew.get('error', '未知错误')}")
                return None
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
                        "duty1_pct": duty1,
                        "duty2_pct": duty2,
                        **samples[-1],
                        "baseline_current_a": baseline.get("current_a"),
                        "baseline_power_w": baseline.get("power_w"),
                        "timestamp": round(time.time(), 3),
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
                        "v": extra_bus.get("voltage_v") or 24.0,
                        "i": extra_bus.get("current_a") or 0.0,
                        "p": extra_bus.get("power_w") or 0.0,
                        "rpm1": extra_rpm[0] if len(extra_rpm) > 0 else 0,
                        "rpm2": extra_rpm[1] if len(extra_rpm) > 1 else 0,
                        "rpm3": extra_rpm[2] if len(extra_rpm) > 2 else 0,
                        "mt": extra_diag.get("motor_temp_c"),
                        "ct": extra_diag.get("controller_temp_c"),
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
                   hold_s: float, max_current_a: float,
                   confirm_dcdc: bool = False) -> None:
        """执行计划中的上升+下降双向阶梯扫频。"""
        try:
            current_state = self.snapshot_fn().get("fan", {}).get("power_status", {}).get("power_supply_state")
            if confirm_dcdc and current_state != 3:
                # 仅当操作者通过独立按钮确认过，才允许 Action=4 覆盖。绝不自动提升预算。
                confirm = self.send_fn("fan_calib", {
                    "action": 4, "step": 0, "duty1_pct": 0, "duty2_pct": 0,
                    "lease_s": 120,
                }, True)
                if not confirm.get("ok"):
                    self._abort_and_return(
                        f"DCDC 就绪确认命令失败：{confirm.get('error', '未知错误')}"
                    )
                    return
                if not self._wait_for_dcdc_confirm():
                    self._abort_and_return("确认后未在 3s 内进入 DCDC_READY，中止标定")
                    return
            elif current_state != 3:
                self._abort_and_return("启动后供电已离开 DCDC_READY，中止标定")
                return

            baseline = self._measure_baseline(0)
            if baseline is None:
                self._abort_and_return("无法采集 0% 静态基线，中止标定")
                return
            with self.lock:
                self.baseline = {
                    "voltage_v": baseline["voltage_v"],
                    "current_a": baseline["current_a"],
                    "power_w": baseline["power_w"],
                    "sample_count": baseline.get("sample_count", 0),
                }

            directions: list[list[int]] = [steps, list(reversed(steps))]
            record_index = 0
            for direction in directions:
                for index, target_duty in enumerate(direction, start=1):
                    if self._stop_event.is_set():
                        return
                    if index > 1 and (index - 1) % self.BASELINE_INTERVAL == 0:
                        fresh = self._measure_baseline(index)
                        if fresh is None:
                            self._abort_and_return(f"步骤 {index} 前方重新采集 0% 基线失败，中止标定")
                            return
                        with self.lock:
                            self.baseline = {
                                "voltage_v": fresh["voltage_v"],
                                "current_a": fresh["current_a"],
                                "power_w": fresh["power_w"],
                                "sample_count": fresh.get("sample_count", 0),
                            }

                    d1 = target_duty if channel == 1 else 0
                    d2 = target_duty if channel == 2 else 0
                    record = self._apply_step(
                        record_index + 1, d1, d2, hold_s, max_current_a,
                        self.baseline, confirm_dcdc,
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
