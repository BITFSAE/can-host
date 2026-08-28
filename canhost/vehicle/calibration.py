"""FanController calibration session manager and automated sweep runner.

Runs controlled calibration sweeps over PWM1 (dual 2H4PU) and PWM2 (single 2H6P),
measures baseline PDM bus power/current, samples steady-state RPM and delta I/P,
and strictly enforces safety gating (DCDC_READY, temperature limits, stall guards).
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import threading
import time
from typing import Any, Callable


class FanCalibrationSession:
    """Manages automated or step-by-step fan calibration sweeps."""

    def __init__(self, send_fn: Callable[[str, dict[str, Any], bool], dict[str, Any]],
                 snapshot_fn: Callable[[], dict[str, Any]]) -> None:
        self.send_fn = send_fn
        self.snapshot_fn = snapshot_fn
        self.lock = threading.Lock()
        self.status: str = "idle"  # "idle", "running", "aborted", "completed"
        self.abort_reason: str = ""
        self.channel: int = 1  # 1 (PWM1), 2 (PWM2), or 3 (both)
        self.current_step: int = 0
        self.total_steps: int = 0
        self.current_duty: list[int] = [0, 0]
        self.baseline: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def check_preconditions(self) -> dict[str, Any]:
        """Verify bus and vehicle safety conditions before calibration."""
        snap = self.snapshot_fn()
        conn = snap.get("connection", {})
        if conn.get("mode") not in ("pcan", "simulation"):
            return {"ok": False, "error": "请先连接 CANB（真实 PCAN 或仿真）"}

        fan = snap.get("fan", {})
        fan_status = fan.get("status", {})
        fan_diag = fan.get("diagnostic", {})
        pdm = snap.get("pdm", {})
        bus = pdm.get("bus", {})
        power_status = fan.get("power_status", {})

        pdm_age = bus.get("age")
        if pdm_age is None or pdm_age > 1.0 or bus.get("offline", True):
            return {"ok": False, "error": "PDM 低压总线遥测离线或超时（>1.0s），无法进行标定"}

        power_supply_state = power_status.get("power_supply_state")
        # In simulation mode, allow calibration if simulated state is DcdcReady (3) or not yet sent
        if conn.get("mode") != "simulation" and power_supply_state != 3:
            state_name = power_status.get("power_supply_name", str(power_supply_state))
            return {"ok": False, "error": f"供电必须为 DCDC_READY 稳态供电（当前：{state_name}），禁止电池模式标定"}

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
                    hold_s: float = 4.0, max_current_a: float = 18.0) -> dict[str, Any]:
        """Start a background automated calibration sweep."""
        with self.lock:
            if self.status == "running":
                return {"ok": False, "error": "标定会话已在进行中"}

            pre_check = self.check_preconditions()
            if not pre_check["ok"]:
                return pre_check

            if steps is None:
                steps = [0, 20, 30, 40, 50, 60, 70, 80, 90, 100]

            self.status = "running"
            self.abort_reason = ""
            self.channel = channel
            self.current_step = 0
            self.total_steps = len(steps)
            self.current_duty = [0, 0]
            self.baseline.clear()
            self.records.clear()
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

        # Send stop command to fan controller
        try:
            self.send_fn("fan_calib", {"action": 3, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0}, False)
        except Exception:
            pass

        return {"ok": True, "status": self.status, "reason": self.abort_reason}

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
                "Motor_Temp_C", "Controller_Temp_C", "Timestamp"
            ])
            for r in self.records:
                writer.writerow([
                    r.get("step"), r.get("channel"),
                    r.get("duty1_pct"), r.get("duty2_pct"),
                    r.get("rpm1"), r.get("rpm2"), r.get("rpm3"),
                    r.get("voltage_v"), r.get("current_a"), r.get("power_w"),
                    r.get("delta_current_a"), r.get("delta_power_w"),
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
                "baseline": self.baseline,
                "records": self.records,
                "exported_at": time.time(),
            }
            return json.dumps(data, ensure_ascii=False, indent=2)

    def _run_sweep(self, channel: int, steps: list[int], hold_s: float, max_current_a: float) -> None:
        try:
            # Step 1: Establish baseline with 0% duty
            baseline_samples = []
            self.send_fn("fan_calib", {"action": 1, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 15}, True)
            t_end = time.monotonic() + 3.0
            while time.monotonic() < t_end:
                if self._stop_event.is_set():
                    return
                snap = self.snapshot_fn()
                bus = snap.get("pdm", {}).get("bus", {})
                if not bus.get("offline", True):
                    baseline_samples.append({
                        "v": bus.get("voltage_v", 24.0),
                        "i": bus.get("current_a", 0.0),
                        "p": bus.get("power_w", 0.0),
                    })
                time.sleep(0.1)

            if baseline_samples:
                base_v = sum(s["v"] for s in baseline_samples) / len(baseline_samples)
                base_i = sum(s["i"] for s in baseline_samples) / len(baseline_samples)
                base_p = sum(s["p"] for s in baseline_samples) / len(baseline_samples)
            else:
                base_v, base_i, base_p = 24.0, 0.0, 0.0

            with self.lock:
                self.baseline = {
                    "voltage_v": round(base_v, 3),
                    "current_a": round(base_i, 3),
                    "power_w": round(base_p, 2),
                }

            # Step 2: Sweep each target step
            for idx, target_duty in enumerate(steps, start=1):
                if self._stop_event.is_set():
                    return

                d1 = target_duty if channel in (1, 3) else 0
                d2 = target_duty if channel in (2, 3) else 0

                with self.lock:
                    self.current_step = idx
                    self.current_duty = [d1, d2]

                cmd_res = self.send_fn("fan_calib", {
                    "action": 2, "step": idx, "duty1_pct": d1, "duty2_pct": d2, "lease_s": 15
                }, True)
                if not cmd_res.get("ok"):
                    with self.lock:
                        self.status = "aborted"
                        self.abort_reason = f"下发标定步骤命令失败：{cmd_res.get('error')}"
                    return

                # Wait for steady state while actively monitoring safety
                step_samples = []
                step_end = time.monotonic() + hold_s
                while time.monotonic() < step_end:
                    if self._stop_event.is_set():
                        return

                    snap = self.snapshot_fn()
                    conn = snap.get("connection", {})
                    fan = snap.get("fan", {})
                    fan_status = fan.get("status", {})
                    fan_diag = fan.get("diagnostic", {})
                    pdm = snap.get("pdm", {})
                    bus = pdm.get("bus", {})
                    power_status = fan.get("power_status", {})

                    # Safety Watchdog 1: PDM offline or stale
                    pdm_age = bus.get("age")
                    if pdm_age is not None and pdm_age > 1.0:
                        self.abort("PDM 遥测超时 (>1.0s)，触发安全中止")
                        return

                    # Safety Watchdog 2: DCDC lost
                    if conn.get("mode") != "simulation" and power_status.get("power_supply_state") not in (None, 3):
                        self.abort(f"供电脱离 DCDC_READY 状态，触发安全中止")
                        return

                    # Safety Watchdog 3: Over-current
                    i_curr = bus.get("current_a", 0.0) or 0.0
                    if i_curr > max_current_a:
                        self.abort(f"总线电流 ({i_curr:.1f} A) 超过安全限制 ({max_current_a:.1f} A)")
                        return

                    # Safety Watchdog 4: Temperature
                    mt = fan_diag.get("motor_temp_c")
                    ct = fan_diag.get("controller_temp_c")
                    if mt is not None and mt >= 72.0:
                        self.abort(f"电机温度超限 ({mt:.1f} ℃ >= 72 ℃)")
                        return
                    if ct is not None and ct >= 68.0:
                        self.abort(f"控制器温度超限 ({ct:.1f} ℃ >= 68 ℃)")
                        return

                    # Collect samples during the last 1.5s
                    if step_end - time.monotonic() <= 1.5:
                        rpm = fan_status.get("rpm", [0, 0, 0]) or [0, 0, 0]
                        step_samples.append({
                            "v": bus.get("voltage_v", 24.0) or 24.0,
                            "i": i_curr,
                            "p": bus.get("power_w", 0.0) or 0.0,
                            "rpm1": rpm[0] if len(rpm) > 0 else 0,
                            "rpm2": rpm[1] if len(rpm) > 1 else 0,
                            "rpm3": rpm[2] if len(rpm) > 2 else 0,
                            "mt": mt,
                            "ct": ct,
                        })

                    time.sleep(0.1)

                # Process averages for this step
                if step_samples:
                    avg_v = sum(s["v"] for s in step_samples) / len(step_samples)
                    avg_i = sum(s["i"] for s in step_samples) / len(step_samples)
                    avg_p = sum(s["p"] for s in step_samples) / len(step_samples)
                    avg_r1 = int(sum(s["rpm1"] for s in step_samples) / len(step_samples))
                    avg_r2 = int(sum(s["rpm2"] for s in step_samples) / len(step_samples))
                    avg_r3 = int(sum(s["rpm3"] for s in step_samples) / len(step_samples))
                    last_mt = step_samples[-1]["mt"]
                    last_ct = step_samples[-1]["ct"]
                else:
                    avg_v, avg_i, avg_p = 24.0, 0.0, 0.0
                    avg_r1 = avg_r2 = avg_r3 = 0
                    last_mt = last_ct = None

                delta_i = max(0.0, avg_i - base_i)
                delta_p = max(0.0, avg_p - base_p)

                with self.lock:
                    self.records.append({
                        "step": idx,
                        "channel": channel,
                        "duty1_pct": d1,
                        "duty2_pct": d2,
                        "rpm1": avg_r1,
                        "rpm2": avg_r2,
                        "rpm3": avg_r3,
                        "voltage_v": round(avg_v, 3),
                        "current_a": round(avg_i, 3),
                        "power_w": round(avg_p, 2),
                        "delta_current_a": round(delta_i, 3),
                        "delta_power_w": round(delta_p, 2),
                        "motor_temp_c": last_mt,
                        "controller_temp_c": last_ct,
                        "timestamp": round(time.time(), 3),
                    })

            # Clean completion
            self.send_fn("fan_calib", {"action": 3, "step": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0}, False)
            with self.lock:
                self.status = "completed"

        except Exception as exc:
            self.abort(f"标定执行发生异常：{exc}")
