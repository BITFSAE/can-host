"""FanController protocol tests against the sibling repo's Doc/风扇控制.md."""

from __future__ import annotations

import unittest

from canhost.decoders import (CanFrame, build_fan_command, fan_ack_matches,
                              decode_fan_power_status, decode_fan_calib_status)
from canhost.transport import CanService
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.calibration import FanCalibrationSession


class FanControllerToolTest(unittest.TestCase):
    def test_command_frames_match_fancontroller_doc_examples(self) -> None:
        frames = [
            (build_fan_command("fan_control", {"_sequence": 1, "mode": 0, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 0}),
             bytes.fromhex("01 01 00 00 00 00 00 11")),
            (build_fan_command("fan_control", {"_sequence": 2, "mode": 1, "duty1_pct": 40, "duty2_pct": 50, "lease_s": 10}),
             bytes.fromhex("01 02 01 28 32 0A 00 8E")),
            (build_fan_command("fan_control", {"_sequence": 3, "mode": 2, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 5}),
             bytes.fromhex("01 03 02 00 00 05 00 28")),
            (build_fan_command("fan_curve", {"_sequence": 4, "temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60,
                                             "min_duty_pct": 30, "ramp_up_pct_per_s": 20}),
             bytes.fromhex("02 04 23 28 3C 1E 14 DC")),
            (build_fan_command("fan_failsafe", {"_sequence": 5, "strategy": 1, "fallback1_duty_pct": 50,
                                                "fallback2_duty_pct": 50, "stale_hold_s": 5, "ramp_down_pct_per_s": 50}),
             bytes.fromhex("03 05 01 32 32 05 32 B6")),
            (build_fan_command("fan_restore_defaults", {"_sequence": 6}),
             bytes.fromhex("04 06 A5 00 00 00 00 D7")),
            (build_fan_command("fan_query", {"_sequence": 7}),
             bytes.fromhex("05 07 00 00 00 00 00 F1")),
            (build_fan_command("fan_curve_ch2", {"_sequence": 8, "temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60,
                                                 "min_duty_pct": 30, "ramp_up_pct_per_s": 20}),
             bytes.fromhex("06 08 23 28 3C 1E 14 BA")),
            (build_fan_command("fan_calib", {"_sequence": 9, "action": 1, "step": 0, "duty1_pct": 0,
                                             "duty2_pct": 0, "lease_s": 15}),
             bytes.fromhex("08 09 01 00 00 00 0F 45")),
            (build_fan_command("fan_calib", {"_sequence": 10, "action": 4, "step": 0, "duty1_pct": 0,
                                             "duty2_pct": 0, "lease_s": 60}),
             bytes.fromhex("08 0A 04 00 00 00 3C 3D")),
        ]
        for frame, expected in frames:
            self.assertEqual(frame.arbitration_id, 0x5A4)
            self.assertFalse(frame.is_extended_id)
            self.assertEqual(frame.data, expected)

    def test_fan_command_rejects_out_of_range_values(self) -> None:
        cases = [
            ("fan_control", {"mode": 3, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 5}),
            ("fan_control", {"mode": -1, "duty1_pct": 0, "duty2_pct": 0, "lease_s": 5}),
            ("fan_control", {"mode": 1, "duty1_pct": 101, "duty2_pct": 0, "lease_s": 5}),
            ("fan_control", {"mode": 1, "duty1_pct": 0, "duty2_pct": -1, "lease_s": 5}),
            ("fan_control", {"mode": 1, "duty1_pct": 40, "duty2_pct": 50, "lease_s": 0}),
            ("fan_control", {"mode": 1, "duty1_pct": 40, "duty2_pct": 50, "lease_s": 61}),
            ("fan_control", {"mode": 2, "duty1_pct": 10, "duty2_pct": 0, "lease_s": 5}),
            ("fan_curve", {"temp_off_c": 40, "temp_on_c": 40, "temp_full_c": 60, "min_duty_pct": 30, "ramp_up_pct_per_s": 20}),
            ("fan_curve", {"temp_off_c": 35, "temp_on_c": 60, "temp_full_c": 60, "min_duty_pct": 30, "ramp_up_pct_per_s": 20}),
            ("fan_curve", {"temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 151, "min_duty_pct": 30, "ramp_up_pct_per_s": 20}),
            ("fan_curve", {"temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60, "min_duty_pct": 9, "ramp_up_pct_per_s": 20}),
            ("fan_curve", {"temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60, "min_duty_pct": 30, "ramp_up_pct_per_s": 9}),
            ("fan_failsafe", {"strategy": 3, "fallback1_duty_pct": 50, "fallback2_duty_pct": 50, "stale_hold_s": 5, "ramp_down_pct_per_s": 50}),
            ("fan_failsafe", {"strategy": 1, "fallback1_duty_pct": 101, "fallback2_duty_pct": 50, "stale_hold_s": 5, "ramp_down_pct_per_s": 50}),
            ("fan_failsafe", {"strategy": 1, "fallback1_duty_pct": 50, "fallback2_duty_pct": 50, "stale_hold_s": 31, "ramp_down_pct_per_s": 50}),
            ("fan_failsafe", {"strategy": 1, "fallback1_duty_pct": 50, "fallback2_duty_pct": 50, "stale_hold_s": 5, "ramp_down_pct_per_s": 101}),
            ("fan_calib", {"action": 1, "step": 1, "duty1_pct": 101, "duty2_pct": 50, "lease_s": 15}),
            ("fan_calib", {"action": 1, "step": 1, "duty1_pct": 50, "duty2_pct": 50, "lease_s": 65}),
            ("fan_calib", {"action": 4, "step": 1, "duty1_pct": 50, "duty2_pct": 50, "lease_s": 0}),
            ("fan_calib", {"action": 5, "step": 1, "duty1_pct": 50, "duty2_pct": 50, "lease_s": 15}),
        ]
        for name, values in cases:
            with self.assertRaises(ValueError, msg=f"{name} {values}"):
                build_fan_command(name, values)
        with self.assertRaises(ValueError):
            build_fan_command("fan_unknown")

    def test_status_and_diagnostic_decode(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A2, bytes([0x0B, 0xB8, 0x0D, 0x48, 0x00, 0x00, 50, 60]), False))
        protocol.ingest(CanFrame(0x5A3, bytes([0x09, 0x29, 0x01, 0x90, 0x7F, 0xFF, 30, 40]), False))
        self.assertEqual(protocol.fan["status"], {"rpm": [3000, 3400, 0], "duty_pct": [50, 60]})
        diag = protocol.fan["diagnostic"]
        self.assertEqual(diag["faults"], 0x09)
        self.assertEqual(diag["fault_names"], ["风扇 1 无转速", "电机温度超时"])
        self.assertTrue(diag["motor_temp_valid"])
        self.assertFalse(diag["inverter_temp_valid"])
        self.assertFalse(diag["igbt_temp_valid"])
        self.assertTrue(diag["group1_running"])
        self.assertFalse(diag["group2_running"])
        self.assertEqual(diag["mode"], 1)
        self.assertEqual(diag["mode_name"], "手动")
        self.assertEqual(diag["motor_temp_c"], 40.0)
        self.assertIsNone(diag["controller_temp_c"])
        self.assertEqual(diag["target_pct"], [30, 40])
        fan = protocol.snapshot({})["fan"]
        self.assertLessEqual(fan["status_age"], 1.0)
        self.assertLessEqual(fan["diagnostic_age"], 1.0)

    def test_ack_curve_and_failsafe_decode(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A5, bytes([0x02, 9, 0, 0x11, 40, 50, 45, 55]), False))
        ack = protocol.fan_acks[9]
        self.assertTrue(ack["accepted"])
        self.assertEqual(ack["opcode"], 2)
        self.assertEqual(ack["sequence"], 9)
        self.assertEqual(ack["mode_name"], "手动")
        self.assertEqual(ack["failsafe_name"], "固定保底")
        self.assertEqual(ack["duty_pct"], [40, 50])
        self.assertEqual(ack["target_pct"], [45, 55])
        self.assertTrue(fan_ack_matches("fan_curve", ack))
        self.assertFalse(fan_ack_matches("fan_control", ack))
        protocol.ingest(CanFrame(0x5A5, bytes([0x01, 10, 3, 0x10, 0, 0, 0, 0]), False))
        rejected = protocol.fan_acks[10]
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["result_name"], "参数错误")
        self.assertEqual(protocol.fan_ack_history[0]["sequence"], 10)
        self.assertEqual(protocol.fan_ack_history[1]["result_name"], "成功")
        protocol.ingest(CanFrame(0x5A6, bytes([35, 40, 60, 30, 20, 75, 30, 1]), False))
        self.assertEqual(protocol.fan["curve"], {
            "temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60,
            "min_duty_pct": 30, "ramp_up_pct_per_s": 20,
            "critical_temp_c": 75, "start_duty_pct": 30, "channel": 1,
        })
        protocol.ingest(CanFrame(0x5A7, bytes([1, 50, 50, 5, 50, 2, 7, 2]), False))
        self.assertEqual(protocol.fan["failsafe"]["failsafe_name"], "固定保底")
        self.assertEqual(protocol.fan["failsafe"]["fallback1_duty_pct"], 50)
        self.assertEqual(protocol.fan["failsafe"]["stale_hold_s"], 5)
        self.assertEqual(protocol.fan["failsafe"]["mode"], 2)
        self.assertEqual(protocol.fan["failsafe"]["lease_remaining_s"], 7)
        self.assertEqual(protocol.fan["failsafe"]["protocol_version"], 2)

    def test_power_status_and_calib_status_decode(self) -> None:
        protocol = VehicleProtocol()
        # 0x5A8: DCDC_READY (state=3, limit=0), req 50/60, tgt 50/60, budget 18.0A (180), pred 12.5A (125 -> 0x007D little endian)
        protocol.ingest(CanFrame(0x5A8, bytes([0x03, 50, 60, 50, 60, 180, 0x7D, 0x00]), False))
        pwr = protocol.fan["power_status"]
        self.assertEqual(pwr["power_supply_state"], 3)
        self.assertEqual(pwr["power_supply_name"], "DCDC就绪")
        self.assertEqual(pwr["power_limit_reason"], 0)
        self.assertEqual(pwr["power_limit_name"], "无限制")
        self.assertEqual(pwr["thermal_req_pct"], [50, 60])
        self.assertEqual(pwr["power_limited_target_pct"], [50, 60])
        self.assertEqual(pwr["current_budget_a"], 18.0)
        self.assertEqual(pwr["predicted_current_a"], 12.5)

        # 0x5A9: Calib Running (state=1, abort=0), step 2, tgt 40/0, lease 12, ver 1, flags 0
        protocol.ingest(CanFrame(0x5A9, bytes([0x01, 2, 40, 0, 12, 1, 0, 0]), False))
        calib = protocol.fan["calib_status"]
        self.assertEqual(calib["calib_state"], 1)
        self.assertEqual(calib["calib_state_name"], "标定中")
        self.assertEqual(calib["step"], 2)
        self.assertEqual(calib["calib_target_pct"], [40, 0])
        self.assertEqual(calib["lease_remaining_s"], 12)
        self.assertEqual(calib["param_version"], 1)

    def test_extended_frames_do_not_update_fan_state(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A2, bytes(8), True))
        protocol.ingest(CanFrame(0x5A3, bytes(8), True))
        protocol.ingest(CanFrame(0x5A5, bytes(8), True))
        protocol.ingest(CanFrame(0x5A8, bytes(8), True))
        protocol.ingest(CanFrame(0x5A9, bytes(8), True))
        self.assertEqual(protocol.fan["status"], {})
        self.assertEqual(protocol.fan["diagnostic"], {})
        self.assertEqual(protocol.fan["power_status"], {})
        self.assertEqual(protocol.fan["calib_status"], {})
        self.assertEqual(protocol.fan_acks, {})
        self.assertIsNone(protocol.last_fan_status_monotonic)

    def test_vehicle_snapshot_contains_fan_and_ack_history(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A2, bytes([0x0B, 0xB8, 0x0D, 0x48, 0x00, 0x00, 50, 60]), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["fan"]["status"]["rpm"], [3000, 3400, 0])
        self.assertIn("ack_history", snapshot["fan"])

    def test_send_fan_command_preconditions(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            result = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(result["ok"])
            self.assertIn("尚未连接", result["error"])
            service.connect({"mode": "simulation", "bus_profile": "canb", "bitrate": 500000})
            result = service.send_fan_command("fan_query", {}, False)
            self.assertFalse(result["ok"])
            self.assertIn("必须确认", result["error"])
            result = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(result["ok"])
            self.assertIn("只允许使用真实 PCAN", result["error"])
        finally:
            service.disconnect()

    def test_bms_service_rejects_fan_commands(self) -> None:
        service = CanService()
        try:
            service.connection.update({"connected": True, "mode": "pcan", "bus_profile": "canb"})
            result = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(result["ok"])
            self.assertIn("整车连接", result["error"])
        finally:
            service.disconnect()

    def test_fan_calibration_session_preconditions_and_export(self) -> None:
        sent_commands = []
        def fake_send(name, vals, ack):
            sent_commands.append((name, vals))
            return {"ok": True}

        fake_snap = {
            "connection": {"mode": "pcan", "connected": True},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0, "age": 0.1, "offline": False}},
            "fan": {
                "status": {"rpm": [3000, 3000, 0]},
                "diagnostic": {"faults": 0, "motor_temp_c": 45.0, "controller_temp_c": 40.0},
                "power_status": {"power_supply_state": 3, "power_supply_name": "DCDC就绪"},
                "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
            }
        }
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

        # Export test
        session.records = [{
            "step": 1, "channel": 1, "duty1_pct": 30, "duty2_pct": 0,
            "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
            "voltage_v": 24.0, "current_a": 4.5, "power_w": 108.0,
            "delta_current_a": 2.5, "delta_power_w": 60.0,
            "motor_temp_c": 45.0, "controller_temp_c": 40.0, "timestamp": 123456.789
        }]
        csv_data = session.export_csv()
        self.assertIn("Delta_Current_A", csv_data)
        self.assertIn("2.5", csv_data)
        json_data = session.export_json()
        self.assertIn("delta_power_w", json_data)

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

        fake_snap["connection"] = {"mode": "pcan", "connected": True}
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
        fake_snap = {
            "connection": {"mode": "pcan", "connected": True},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                            "age": 0.1, "offline": False}},
            "fan": {
                "status": {"rpm": [3000, 3000, 0]},
                "diagnostic": {"faults": 0, "motor_temp_c": 45.0, "controller_temp_c": 40.0},
                "power_status": {"power_supply_state": 3, "power_supply_name": "DCDC就绪"},
                "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
            },
        }
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        try:
            result = session.start_sweep(channel=1, steps=[0], hold_s=0.2, max_current_a=18.0)
            self.assertTrue(result["ok"], result)
            # 不等待后台线程完成，只验证调用没有持锁卡死。
            self.assertEqual(session.status, "running")
        finally:
            session._stop_event.set()

    def test_calibration_abort_uses_acknowledged_stop_and_auto(self) -> None:
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        fake_snap = {
            "connection": {"mode": "pcan", "connected": True},
            "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                            "age": 0.1, "offline": False}},
            "fan": {"status": {}, "diagnostic": {}, "power_status": {},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1},
        }
        session = FanCalibrationSession(fake_send, lambda: fake_snap)
        session.status = "running"
        result = session.abort("测试中止")
        self.assertTrue(result["ok"], result)
        self.assertEqual(session.status, "aborted")
        self.assertEqual([item[2] for item in sent], [True, True])
        self.assertEqual(sent[0][0], "fan_calib")
        self.assertEqual(sent[0][1]["action"], 3)
        self.assertEqual(sent[1][0], "fan_control")
        self.assertEqual(sent[1][1]["mode"], 0)

    def test_calibration_requires_explicit_dcdc_confirmation(self) -> None:
        state = {"value": 1}
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            if name == "fan_calib" and vals.get("action") == 4:
                state["value"] = 3
            return {"ok": True}
        def snapshot():
            return {
                "connection": {"mode": "pcan", "connected": True},
                "pdm": {"bus": {"voltage_v": 24.0, "current_a": 2.0, "power_w": 48.0,
                                "age": 0.1, "offline": False}},
                "fan": {
                    "status": {"rpm": [3000, 3000, 0]},
                    "diagnostic": {"faults": 0, "motor_temp_c": 45.0, "controller_temp_c": 40.0},
                    "power_status": {"power_supply_state": state["value"], "power_supply_name": "待确认"},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                },
            }
        session = FanCalibrationSession(fake_send, snapshot)
        rejected = session.start_sweep(channel=1, steps=[0], hold_s=0.2,
                                      max_current_a=18.0, confirm_dcdc=False)
        self.assertFalse(rejected["ok"])
        self.assertIn("确认 DCDC", rejected["error"])
        # 明确确认后才允许启动，并会在后台发送 Action=4。
        accepted = session.start_sweep(channel=1, steps=[0], hold_s=0.2,
                                       max_current_a=18.0, confirm_dcdc=True)
        self.assertTrue(accepted["ok"], accepted)
        self.assertTrue(any(vals.get("action") == 4 for name, vals, _ in sent))
        session._stop_event.set()


class FanCalibrationWatchdogTest(unittest.TestCase):
    def test_watchdog_stops_on_pdm_loss_and_dcdc_loss(self) -> None:
        sent = []
        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}
        def snapshot(offline=False, state=3, current=2.0, motor=45.0, ctrl=40.0, faults=0):
            return {
                "connection": {"mode": "pcan", "connected": True},
                "pdm": {"bus": {"voltage_v": 24.0, "current_a": current, "power_w": 48.0,
                                "age": 0.1, "offline": offline}},
                "fan": {
                    "status": {"rpm": [3000, 3000, 0]},
                    "diagnostic": {"faults": faults, "motor_temp_c": motor, "controller_temp_c": ctrl},
                    "power_status": {"power_supply_state": state, "power_supply_name": "DCDC就绪"},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                },
            }
        session = FanCalibrationSession(fake_send, snapshot)
        self.assertIsNone(session._watchdog(snapshot(motor=71.8), 18.0))
        self.assertIn("PDM", session._watchdog(snapshot(offline=True), 18.0))
        self.assertIn("供电", session._watchdog(snapshot(state=1), 18.0))
        self.assertIn("总线电流", session._watchdog(snapshot(current=18.1), 18.0))
        self.assertIn("电机温度", session._watchdog(snapshot(motor=72.0), 18.0))
        self.assertIn("停转", session._watchdog(snapshot(faults=0x01), 18.0))


if __name__ == "__main__":
    unittest.main()
