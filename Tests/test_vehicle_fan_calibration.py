"""FanController protocol tests against the sibling repo's Doc/风扇控制.md."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from canhost.app import Api
from canhost.decoders import (CanFrame, build_fan_command, fan_ack_matches,
                              decode_fan_power_status, decode_fan_calib_status,
                              build_bms_fan_command, decode_bms_fan_detail)
from canhost.transport import BATTERY_FAN_ACK_TIMEOUT_S, CanService
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.calibration import FanCalibrationSession, BatteryFanCalibrationSession



class RestoredFanControllerToolTest(unittest.TestCase):

    def test_two_automatic_fan_calibrations_are_mutually_exclusive(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            service.battery_fan_calib_session.status = "running"
            blocked = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(blocked["ok"])
            self.assertIn("PDM", blocked["error"])
            blocked_start = service.start_fan_calibration()
            self.assertFalse(blocked_start["ok"])
            self.assertIn("不能同时", blocked_start["error"])

            service.battery_fan_calib_session.status = "idle"
            service.fan_calib_session.status = "running"
            blocked = service.send_battery_fan_command(
                "battery_fan_control", {"mode": 0, "duty_pct": 0, "lease_s": 0}, True)
            self.assertFalse(blocked["ok"])
            self.assertIn("PDM", blocked["error"])
            blocked_start = service.start_battery_fan_calibration()
            self.assertFalse(blocked_start["ok"])
            self.assertIn("不能同时", blocked_start["error"])
        finally:
            service.disconnect()

    def test_fan_calibration_session_preconditions_and_export(self) -> None:
        sent_commands = []
        def fake_send(name, vals, ack):
            sent_commands.append((name, vals))
            return {"ok": True}

        fake_snap = _calib_snap()
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        self.assertTrue(session.check_preconditions()["ok"])

        # Fail when PDM is offline
        fake_snap["pdm"]["bus"]["offline"] = True
        self.assertFalse(session.check_preconditions()["ok"])
        fake_snap["pdm"]["bus"]["offline"] = False

        # Fail when power limited / unknown source before explicit DCDC confirmation
        fake_snap["fan"]["power_status"]["power_supply_state"] = 4
        self.assertFalse(session.check_preconditions()["ok"])
        fake_snap["fan"]["power_status"]["power_supply_state"] = 3

        # Fail when over-temperature
        fake_snap["fan"]["diagnostic"]["motor_temp_c"] = 71.0
        self.assertFalse(session.check_preconditions()["ok"])
        fake_snap["fan"]["diagnostic"]["motor_temp_c"] = 45.0

        # 温度失联时必须拒绝：固件看门狗只在温度“新鲜且超温”时中止，
        # 失联时标定会在没有温度保护的情况下运行。
        fake_snap["fan"]["diagnostic"]["faults"] = 0x18
        result = session.check_preconditions()
        self.assertFalse(result["ok"])
        self.assertIn("温度输入失联", result["error"])
        fake_snap["fan"]["diagnostic"]["faults"] = 0

        # 温度为 None（0x5A3 上报 0x7FFF）时同样必须拒绝
        fake_snap["fan"]["diagnostic"]["motor_temp_c"] = None
        fake_snap["fan"]["diagnostic"]["controller_temp_c"] = None
        result = session.check_preconditions()
        self.assertFalse(result["ok"])
        self.assertIn("温度无效", result["error"])
        fake_snap["fan"]["diagnostic"]["motor_temp_c"] = 45.0
        fake_snap["fan"]["diagnostic"]["controller_temp_c"] = 40.0

        # Export test
        session.records = [{
            "step": 1, "channel": 1, "direction": "up", "baseline_id": 2,
            "duty1_pct": 30, "duty2_pct": 0,
            "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
            "voltage_v": 24.0, "current_a": 4.5, "power_w": 108.0,
            "delta_current_a": 2.5, "delta_power_w": 60.0,
            "motor_temp_c": 45.0, "controller_temp_c": 40.0, "timestamp": 123456.789
        }]
        session.baseline_history = [{"baseline_id": 2, "step": 0, "direction": "initial",
                                     "current_a": 2.0, "power_w": 48.0,
                                     "sample_count": 30, "captured_at": 123456.0}]
        session.baseline_raw_samples = [
            {"baseline_id": 1, "step": 0, "direction": "initial",
             "duty1_pct": 0, "duty2_pct": 0, "t": 123456.0, "v": 24.0, "i": 2.0, "p": 48.0},
            {"baseline_id": 2, "step": 0, "direction": "initial",
             "duty1_pct": 0, "duty2_pct": 0, "t": 123456.1, "v": 24.0, "i": 2.0, "p": 48.0},
        ]
        csv_data = session.export_csv()
        self.assertIn("Delta_Current_A", csv_data)
        self.assertIn("2.5", csv_data)
        self.assertIn("Baseline_ID", csv_data)
        json_data = session.export_json()
        self.assertIn("delta_power_w", json_data)
        # 基线原始数据必须进入 JSON 导出，否则无法复核每条记录关联的基线。
        self.assertIn("baseline_raw_samples", json_data)
        self.assertIn("baseline_history", json_data)

    def test_aborted_fan_session_remains_exportable_without_completed_points(self) -> None:
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        session.status = "aborted"
        session.abort_reason = "首个基线有效样本不足"
        session.run_params = {"channel": 1, "tier": "dcdc"}
        snapshot = session.get_snapshot()
        self.assertTrue(snapshot["export_available"])
        csv_data = session.export_csv()
        self.assertIn("Session_Status", csv_data)
        self.assertIn("首个基线有效样本不足", csv_data)
        json_data = json.loads(session.export_json())
        self.assertEqual(json_data["status"], "aborted")
        self.assertEqual(json_data["abort_reason"], "首个基线有效样本不足")

    def test_unstable_baseline_is_kept_as_warning_instead_of_aborting(self) -> None:
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        session._calib_generation = lambda: 1
        session._wait_for_calib_state = lambda *args, **kwargs: None
        noisy = [
            {"t": float(index), "v": 24.0,
             "i": 2.0 if index % 2 else 2.4,
             "p": 48.0 if index % 2 else 57.6}
            for index in range(12)
        ]
        session._sample_until = lambda *args, **kwargs: (list(noisy), None)
        baseline, error = session._measure_baseline(0, "initial", 18.0, 3)
        self.assertIsNone(error)
        self.assertIsNotNone(baseline)
        self.assertFalse(baseline["quality_ok"])
        self.assertEqual(len(session.quality_warnings), 1)
        self.assertEqual(len(session.baseline_raw_samples), 36)


    def test_calibration_rejects_nonfinite_dcdc_and_temperature_values(self) -> None:
        snap = _calib_snap()
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        snap["pdm"]["battery"]["current_a"] = float("nan")
        ready, error = session._dcdc_ready_by_measurement(snap)
        self.assertFalse(ready)
        self.assertIn("无效", error)
        snap["pdm"]["battery"]["current_a"] = 0.1
        snap["fan"]["diagnostic"]["motor_temp_c"] = float("nan")
        self.assertIn("温度无效", session.check_preconditions()["error"])
        snap["fan"]["diagnostic"]["motor_temp_c"] = 45.0
        snap["fan"]["status_age"] = float("nan")
        self.assertIn("0x5A2", session.check_preconditions()["error"])

    def test_start_current_must_also_fit_user_protection_limit(self) -> None:
        snap = _calib_snap(state=1, bus_current=6.0, bat_current=3.0,
                           bus_v=23.5, bat_v=23.5)
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        result = session.start_sweep(channel=1, steps=[0], hold_s=3.0,
                                     max_current_a=5.0, tier="battery")
        self.assertFalse(result["ok"])
        self.assertIn("5.0 A", result["error"])

    def test_calibration_preconditions_require_real_pcan_and_fresh_fan_frames(self) -> None:
        sent_commands = []
        def fake_send(name, vals, ack):
            sent_commands.append((name, vals, ack))
            return {"ok": True}

        fake_snap = {
            "connection": {"mode": "simulation", "connected": True},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                            "age": 0.1, "offline": False}},
            "fan": {"status": {}, "diagnostic": {}, "power_status": {},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1},
        }
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        result = session.check_preconditions()
        self.assertFalse(result["ok"])
        self.assertIn("真实 PCAN", result["error"])

        fake_snap["connection"] = {"mode": "pcan", "connected": True,
                                   "bus_profile": "canb", "bitrate": 500000}
        fake_snap["fan"] = {
            "status": {"rpm": [3000, 3000, 0]},
            "diagnostic": {"faults": 0, "motor_temp_c": 45.0, "controller_temp_c": 40.0},
            "power_status": {"power_supply_state": 3, "power_supply_name": "DCDC就绪"},
            "status_age": None, "diagnostic_age": 0.1, "power_status_age": 0.1,
        }
        result = session.check_preconditions()
        self.assertFalse(result["ok"])
        self.assertIn("0x5A2", result["error"])

    def test_calibration_start_does_not_deadlock_with_rlock(self) -> None:
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        fake_snap = _calib_snap()
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        # 把 3 秒稳定验证缩短，避免单元测试真的等待 3 秒。
        session.DCDC_STABLE_REQUIRED_S = 0.2
        try:
            result = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
            self.assertTrue(result["ok"], result)
            # 不等待后台线程完成，只验证调用没有持锁卡死。
            self.assertEqual(session.status, "running")
        finally:
            session._stop_event.set()

    def test_dcdc_channel_two_uses_stability_interval(self) -> None:
        snap = _calib_snap()
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.05
        try:
            result = session.start_sweep(channel=2, steps=[0], hold_s=3.0,
                                         max_current_a=18.0, tier="dcdc")
            self.assertTrue(result["ok"], result)
            self.assertEqual(session.channel, 2)
        finally:
            session._stop_event.set()

    def test_calibration_accepts_matching_battery_tier(self) -> None:
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        fake_snap = _calib_snap(state=1, bus_current=3.0, bat_current=3.0,
                                bus_v=23.5, bat_v=23.5)
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        try:
            result = session.start_sweep(channel=1, steps=[0], hold_s=3.0,
                                         max_current_a=8.0, tier="battery")
            self.assertTrue(result["ok"], result)
            self.assertEqual(session.tier, "battery")
            self.assertEqual(session.run_params["tier"], "battery")
        finally:
            session._stop_event.set()

    def test_calibration_abort_uses_acknowledged_stop_and_auto(self) -> None:
        sent = []
        post_ack_status = {"stop_sent": False, "polls_until_frame": 0}
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            if name == "fan_calib" and vals.get("action") == 3:
                post_ack_status["stop_sent"] = True
                post_ack_status["polls_until_frame"] = 2
            return {"ok": True}
        def snapshot():
            if post_ack_status["polls_until_frame"] > 0:
                post_ack_status["polls_until_frame"] -= 1
            if (post_ack_status["stop_sent"]
                    and post_ack_status["polls_until_frame"] == 0
                    and "calib_status" not in fake_snap["fan"]):
                fake_snap["fan"]["calib_status"] = {
                    "calib_state": 3, "step": 0, "calib_target_pct": [0, 0],
                    "lease_remaining_s": 0,
                }
                fake_snap["fan"]["calib_status_age"] = 0.1
                fake_snap["fan"]["calib_status_generation"] += 1
            return fake_snap
        fake_snap = {
            "connection": {"mode": "pcan", "connected": True,
                           "bus_profile": "canb", "bitrate": 500000},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                            "age": 0.1, "offline": False}},
            "fan": {"status": {}, "diagnostic": {}, "power_status": {},
                    "calib_status_generation": 0,
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1},
        }
        session = FanCalibrationSession(fake_send, snapshot)
        session.status = "running"
        result = session.abort("测试中止")
        self.assertTrue(result["ok"], result)
        self.assertEqual(session.status, "aborted")
        self.assertEqual([item[2] for item in sent], [True, True])
        self.assertEqual(sent[0][0], "fan_calib")
        self.assertEqual(sent[0][1]["action"], 3)
        self.assertEqual(sent[1][0], "fan_control")
        self.assertEqual(sent[1][1]["mode"], 0)

    def test_calibration_abort_accepts_firmware_aborted_zero_target(self) -> None:
        """安全看门狗已中止时，STOP ACK 后不应等待不存在的 COMPLETED 帧。"""
        sent = []
        snap = _calib_snap()
        snap["fan"].update({
            "calib_status": {"param_version": 4,
                "calib_state": 2,
                "calib_state_name": "已中止",
                "calib_abort_reason": 2,
                "calib_abort_name": "PDM遥测超时",
                "step": 2,
                "calib_target_pct": [0, 0],
                "lease_remaining_s": 0,
            },
            "calib_status_age": 0.1,
            "calib_status_generation": 184,
        })

        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}

        session = FanCalibrationSession(fake_send, lambda: snap)
        session.status = "running"
        result = session.abort("FanController 标定会话已不活动（已中止）")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("固件恢复失败", result["reason"])
        self.assertEqual([item[0] for item in sent], ["fan_calib", "fan_control"])

    def test_firmware_abort_reason_keeps_code_step_and_target(self) -> None:
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        reason = session._watchdog({
            **_calib_snap(),
            "fan": {
                **_calib_snap()["fan"],
                "calib_status": {"param_version": 4,
                    "calib_state": 2, "calib_state_name": "已中止",
                    "calib_abort_reason": 5, "calib_abort_name": "风扇停转",
                    "step": 7, "calib_target_pct": [30, 0],
                    "lease_remaining_s": 0,
                },
                "calib_status_age": 0.1,
            },
        }, 18.0)
        self.assertIn("风扇停转", reason)
        self.assertIn("原因码 5", reason)
        self.assertIn("步骤 7", reason)
        self.assertIn("[30, 0]", reason)

    def test_recoverable_timeout_pauses_instead_of_aborting(self) -> None:
        snap = _calib_snap()
        snap["pdm"]["bus"]["age"] = 1.6
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        session.status = "running"
        self.assertIsNone(session._watchdog(snap, 18.0))
        self.assertIn("暂停", session._communication_pause_reason(snap))

    def test_automatic_calibration_uses_long_lease(self) -> None:
        sent = []
        session = FanCalibrationSession(
            lambda name, values, ack: sent.append((name, values, ack)) or {"ok": True},
            lambda: {"fan": {"calib_status_generation": 3}},
        )
        result, generation = session._send_calib_command(2, 4, 30, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(generation, 3)
        self.assertEqual(sent[0][1]["lease_s"], 60)

    def test_terminal_abort_diagnostic_is_written_atomically(self) -> None:
        snap = _calib_snap()
        with tempfile.TemporaryDirectory() as directory:
            session = FanCalibrationSession(
                lambda *_: {"ok": True}, lambda: snap, Path(directory))
            session.status = "aborted"
            session.current_step = 7
            session.current_duty = [30, 0]
            session._persist_terminal_diagnostic("风扇停转（原因码 5）", snap)
            files = list(Path(directory).glob("fan_calibration_abort_*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "风扇停转（原因码 5）")
            self.assertEqual(payload["session"]["current_step"], 7)
            self.assertEqual(session.last_diagnostic_path, str(files[0]))
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_communication_pause_zeros_then_repeats_current_point(self) -> None:
        snap = _calib_snap()
        snap["fan"].update({
            "calib_status_generation": 0,
            "calib_status_age": 0.1,
            "calib_status": {"param_version": 4,
                "calib_state": 1, "calib_state_name": "标定中",
                "calib_abort_reason": 0, "calib_abort_name": "无",
                "step": 4, "calib_target_pct": [30, 0],
                "lease_remaining_s": 60, "output_paused": False,
            },
        })
        sent = []
        pending = {"polls": 0, "step": 4, "duties": [30, 0]}

        def fake_send(name, values, acknowledged):
            sent.append((name, dict(values), acknowledged))
            pending.update({"polls": 2, "step": values["step"],
                            "duties": [values["duty1_pct"], values["duty2_pct"]]})
            return {"ok": True}

        def snapshot():
            if pending["polls"] > 0:
                pending["polls"] -= 1
                if pending["polls"] == 0:
                    snap["fan"]["calib_status_generation"] += 1
                    snap["fan"]["calib_status"].update({
                        "step": pending["step"],
                        "calib_target_pct": list(pending["duties"]),
                        "lease_remaining_s": 60,
                    })
            return snap

        session = FanCalibrationSession(fake_send, snapshot)
        session.status = "running"
        with patch("canhost.vehicle.calibration.CALIB_RECOVERY_STABLE_S", 0.0):
            error = session._wait_for_communication_recovery(
                "测试短时断流", 18.0, 3, 4, (30, 0))
        self.assertIsNone(error)
        # 恢复确认后，续租必须沿用重新确认的 30% 目标，不能把暂停期间
        # 缓存的 0% 安全目标补发回来覆盖当前测点。
        renew_result = session._renew_lease_once()
        self.assertTrue(renew_result["ok"])
        self.assertEqual([item[1]["duty1_pct"] for item in sent], [0, 30, 30])
        self.assertEqual(session.recovery_count, 1)
        self.assertEqual(session.pause_reason, "")

    def test_calibration_requires_measured_dcdc_ready(self) -> None:
        """自动扫频必须由 PDM 实测判据证明 DCDC 已接管，不能只看固件状态。"""
        sent: list[tuple[str, dict, bool]] = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}

        # 固件上报 DCDC_READY（可能是 Action=4 手动覆盖），但电池仍在放电：必须拒绝。
        snap = _calib_snap(state=3, bat_current=3.0)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(rejected["ok"], "电池仍在放电时不得开始扫频")
        self.assertIn("电池支路仍在放电", rejected["error"])

        # 电压差不足：同样拒绝。
        snap = _calib_snap(state=3, bus_v=23.6, bat_v=23.5)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(rejected["ok"])
        self.assertIn("电压差", rejected["error"])

        # 电池支路离线：无法证明 DCDC 接管，拒绝。
        snap = _calib_snap(state=3, bat_offline=True)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(rejected["ok"])
        self.assertIn("PDM", rejected["error"])

        # 实测判据满足，但固件状态与实测不一致：拒绝。
        snap = _calib_snap(state=1)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(rejected["ok"])
        self.assertIn("不一致", rejected["error"])

        # 整车基础负载过高：拒绝，否则扫到高占空比必然触发电流保护。
        snap = _calib_snap(state=3, bus_current=12.0)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(rejected["ok"])
        self.assertIn("开始标定门槛", rejected["error"])

        # 全部条件满足才允许开始。
        snap = _calib_snap(state=3)
        session = FanCalibrationSession(fake_send, lambda: snap)
        session.DCDC_STABLE_REQUIRED_S = 0.2
        accepted = session.start_sweep(channel=1, steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertTrue(accepted["ok"], accepted)
        self.assertFalse(any(
            name == "fan_calib" and vals.get("action") == 4
            for name, vals, _ in sent
        ), "自动阶梯扫频不得自动发送 Action=4")
        session._stop_event.set()

    def test_baseline_samples_append_and_are_exported(self) -> None:
        """每 4 个点重新测量基线时必须追加保存，而不是覆盖上一组。"""
        sent: list[tuple[str, dict, bool]] = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        snap = _calib_snap()
        session = FanCalibrationSession(fake_send, lambda: snap)
        # 直接构造两组样本，验证追加与 baseline_id 递增。
        session.baseline_raw_samples = []
        session.baseline_history = []
        session.baseline_id = 0
        for _ in range(2):
            with session.lock:
                session.baseline_id += 1
                session.baseline_raw_samples.extend([
                    {"baseline_id": session.baseline_id, "step": 0, "direction": "up",
                     "duty1_pct": 0, "duty2_pct": 0, "t": 1.0, "v": 24.0, "i": 2.0, "p": 48.0},
                    {"baseline_id": session.baseline_id, "step": 0, "direction": "up",
                     "duty1_pct": 0, "duty2_pct": 0, "t": 1.1, "v": 24.0, "i": 2.0, "p": 48.0},
                ])
                session.baseline_history.append({"baseline_id": session.baseline_id,
                                                 "step": 0, "direction": "up",
                                                 "current_a": 2.0, "power_w": 48.0})
        self.assertEqual(len(session.baseline_raw_samples), 4)
        self.assertEqual([s["baseline_id"] for s in session.baseline_raw_samples], [1, 1, 2, 2])
        exported = json.loads(session.export_json())
        self.assertEqual(len(exported["baseline_raw_samples"]), 4)
        self.assertEqual(len(exported["baseline_history"]), 2)

def _calib_snap(*, bus_current: float = 2.0, bus_offline: bool = False,
                bat_current: float = 0.1, bat_offline: bool = False,
                bus_v: float = 24.0, bat_v: float = 23.5,
                state: int = 3, faults: int = 0,
                motor_temp: float | None = 45.0,
                ctrl_temp: float | None = 40.0) -> dict:
    """构造标定测试用的整车快照；PDM 双路都给出，满足 DCDC 实测判据。"""
    return {
        "connection": {"mode": "pcan", "connected": True,
                       "bus_profile": "canb", "bitrate": 500000},
        "pdm": {
            "bus": {"voltage_v": bus_v, "current_a": bus_current, "power_w": 48.0,
                    "age": 0.1, "offline": bus_offline},
            "battery": {"voltage_v": bat_v, "current_a": bat_current, "power_w": 2.0,
                        "age": 0.1, "offline": bat_offline},
        },
        "fan": {
            "status": {"rpm": [3000, 3000, 0]},
            "diagnostic": {"faults": faults, "motor_temp_c": motor_temp,
                           "controller_temp_c": ctrl_temp},
            "power_status": {"power_supply_state": state, "power_supply_name": "DCDC就绪"},
            "calib_status": {"param_version": 4},
            "calib_status_age": 0.1,
            "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
            "calib_limits_age": 0.1,
        },
    }

class RestoredFanCalibrationWatchdogTest(unittest.TestCase):

    def test_watchdog_stops_on_pdm_loss_and_dcdc_loss(self) -> None:
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        def snapshot(offline=False, state=3, current=2.0, motor=45.0, ctrl=40.0, faults=0):
            return {
                "connection": {"mode": "pcan", "connected": True,
                               "bus_profile": "canb", "bitrate": 500000},
                "pdm": {"bus": {"voltage_v": 24.0, "current_a": current, "power_w": 48.0,
                                "age": 0.1, "offline": offline}},
                "fan": {
                    "status": {"rpm": [3000, 3000, 0]},
                    "diagnostic": {"faults": faults, "motor_temp_c": motor, "controller_temp_c": ctrl},
                    "power_status": {"power_supply_state": state, "power_supply_name": "DCDC就绪"},
                    "calib_status": {"param_version": 4, "calib_state": 1, "calib_state_name": "标定中",
                                     "step": 2, "calib_target_pct": [20, 0],
                                     "lease_remaining_s": 10},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                    "calib_status_age": 0.1,
                },
            }
        session = FanCalibrationSession(fake_send, snapshot)
        self.assertIsNone(session._watchdog(snapshot(motor=71.8), 18.0))
        self.assertIsNone(session._watchdog(snapshot(offline=True), 18.0))
        self.assertIn("PDM", session._communication_pause_reason(snapshot(offline=True)))
        self.assertIn("供电", session._watchdog(snapshot(state=1), 18.0))
        self.assertIn("总线电流", session._watchdog(snapshot(current=18.1), 18.0))
        self.assertIn("电机温度", session._watchdog(snapshot(motor=72.0), 18.0))
        # 低占空比扫描的目的就是找起转点；0x5A3 TACH 位只记录当前点未起转，
        # 不能抢在固件 0x5A9 的标定状态之前中止整轮扫描。
        self.assertIsNone(session._watchdog(snapshot(faults=0x01), 18.0))
        firmware_aborted = snapshot(faults=0x01)
        firmware_aborted["fan"]["calib_status"].update({
            "calib_state": 2,
            "calib_state_name": "已中止",
            "calib_abort_reason": 5,
            "calib_abort_name": "风扇停转",
        })
        terminal_reason = session._watchdog(firmware_aborted, 18.0)
        self.assertIn("风扇停转", terminal_reason)
        self.assertIn("原因码 5", terminal_reason)
        # 温度失联或温度无效时必须中止，否则标定在没有温度保护的情况下继续。
        self.assertIsNone(session._watchdog(snapshot(faults=0x18), 18.0))
        self.assertIn("温度输入失联", session._communication_pause_reason(snapshot(faults=0x18)))
        self.assertIsNone(session._watchdog(snapshot(motor=None, ctrl=None), 18.0))
        self.assertIn("温度输入无效", session._communication_pause_reason(
            snapshot(motor=None, ctrl=None)))
        self.assertIn("外部改写", session._watchdog(
            snapshot(), 18.0, expected_step=3, expected_duties=(20, 0)))

    def test_watchdog_keeps_selected_battery_tier(self) -> None:
        """重复采样路径也必须使用所选档位，不能把电池档误当成 DCDC 放行。"""
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        snap = {
            "connection": {"mode": "pcan", "connected": True,
                           "bus_profile": "canb", "bitrate": 500000},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                            "age": 0.1, "offline": False}},
            "fan": {
                "status": {"rpm": [3000, 3000, 0]},
                "diagnostic": {"faults": 0, "motor_temp_c": 45.0, "controller_temp_c": 40.0},
                "power_status": {"power_supply_state": 3, "power_supply_name": "DCDC就绪"},
                "calib_status": {"param_version": 4, "calib_state": 1, "step": 2,
                                 "calib_target_pct": [20, 0], "lease_remaining_s": 10},
                "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                "calib_status_age": 0.1,
            },
        }
        self.assertIn("档位", session._watchdog(snap, 8.0, expected_state=1))


class CalibrationRenewalTest(unittest.TestCase):
    def test_external_target_prevents_renewal(self):
        snap = _calib_snap()
        snap['fan']['calib_status'].update(calib_state=1,step=9,calib_target_pct=[40,0])
        send = MagicMock(return_value={'ok':True})
        session = FanCalibrationSession(send, lambda:snap)
        session.status='running'
        session._command_step=2
        session._command_duties=[10,0]
        session._firmware_started.set()
        self.assertFalse(session._renew_lease_once()['ok'])
        send.assert_not_called()

    def test_disconnect_keeps_partial_measurements(self):
        session=FanCalibrationSession(lambda *_:{'ok':True},lambda:{})
        session.status='completed'
        session.raw_samples=[{'actual_duty1_pct':10,'actual_duty2_pct':0}]
        session.run_params={'channel':1}
        session.cancel_for_disconnect()
        self.assertEqual(session.status,'stale')
        self.assertIn('actual_duty1_pct',session.export_json())


class VehicleCalibrationExportTest(unittest.TestCase):
    def test_native_csv_json_export_and_cancel(self):
        api=Api.__new__(Api)
        api._window=MagicMock()
        api._vehicle_service=MagicMock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                'sys.modules', {'webview':SimpleNamespace(SAVE_DIALOG='save')}):
            for fmt in ('csv','json'):
                destination=Path(directory)/('fan.'+fmt)
                api._window.create_file_dialog.return_value=str(destination)
                api._vehicle_service.export_fan_calibration.return_value={'ok':True,'data':'partial samples'}
                result=api.choose_export_fan_calibration(fmt)
                self.assertTrue(result['ok'])
                self.assertEqual(destination.read_text(encoding='utf-8-sig'),'partial samples')
            api._window.create_file_dialog.return_value=None
            api._vehicle_service.export_fan_calibration.reset_mock()
            self.assertTrue(api.choose_export_fan_calibration()['cancelled'])
            api._vehicle_service.export_fan_calibration.assert_not_called()


class CalibrationShutdownTest(unittest.TestCase):
    def test_cancelled_worker_cannot_restart_after_stop(self):
        sender=MagicMock(return_value={'ok':True})
        session=FanCalibrationSession(sender,lambda:{})
        session._stop_event.set()
        for action in (1,2):
            result,_=session._send_calib_command(action,1,30,0)
            self.assertFalse(result['ok'])
        sender.assert_not_called()
        result,_=session._send_calib_command(3,0,0,0)
        self.assertTrue(result['ok'])

    def test_shutdown_holds_cross_session_gate(self):
        session=FanCalibrationSession(lambda *_:{'ok':True},lambda:{})
        session.status='running'
        observed=[]
        def finish():
            observed.append(session.is_running())
            return {'ok':True,'errors':[]}
        with patch.object(session,'_stop_and_restore_auto',side_effect=finish):
            self.assertTrue(session.abort()['ok'])
        self.assertEqual(observed,[True])
        self.assertFalse(session.is_running())
        session._thread=MagicMock()
        session._thread.is_alive.return_value=True
        self.assertTrue(session.is_running())
