"""FanController protocol tests against the sibling repo's Doc/风扇控制.md."""

from __future__ import annotations

import json
import unittest

from canhost.decoders import (CanFrame, build_fan_command, fan_ack_matches,
                              decode_fan_power_status, decode_fan_calib_status,
                              build_bms_fan_command, decode_bms_fan_detail)
from canhost.transport import CanService
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.calibration import FanCalibrationSession, BatteryFanCalibrationSession


class FanControllerToolTest(unittest.TestCase):
    def test_battery_fan_calibration_safety_and_cap_calculation_helpers(self) -> None:
        snap = {
            "connection": {"connected": True, "mode": "pcan",
                           "bus_profile": "canb", "bitrate": 500000},
            "pack": {"age": 0.1, "state": 5, "temperature_complete": True},
            "pdm": {"bus": {"offline": False, "age": 0.1, "voltage_v": 24.0,
                            "current_a": 3.0, "power_w": 72.0}},
            "battery_fan": {"status_age": 0.1, "calibration_age": 0.1,
                            "calibration": {"calib_state": 1, "step": 1,
                                            "target_duty_pct": 20, "chroma_budget_w": 35,
                                            "hv_budget_w": 70}, "status": {
                "power_source": 2, "power_source_name": "高压/DCDC 70W",
                "protocol_version": 1,
                "flags": {"hardware_ready": True, "stall_confirmed": False,
                          "calibration_active": True},
            }},
        }
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        self.assertIsNone(session._safety_error(snap, 18.0))
        snap["pack"]["state"] = 3
        self.assertIn("高压接通", session._safety_error(snap, 18.0))
        snap["pack"]["state"] = 5
        snap["battery_fan"]["status"]["flags"]["stall_confirmed"] = True
        self.assertIn("停转", session._safety_error(snap, 18.0))
        snap["battery_fan"]["status"]["flags"]["stall_confirmed"] = False
        snap["pack"]["temperature_complete"] = False
        self.assertIn("温度", session._safety_error(snap, 18.0))
        summary = session._median([{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 2000.0}] * 10)
        self.assertEqual({key: summary[key] for key in ("v", "i", "p", "rpm")},
                         {"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 2000.0})
        self.assertEqual(summary["std_i"], 0.0)

    def test_v3_cap_commands_and_battery_fan_commands(self) -> None:
        self.assertEqual(build_fan_command("fan_calib", {
            "_sequence": 11, "action": 5, "battery_cap_pct": 35, "dcdc_cap_pct": 70,
        }).data, bytes.fromhex("08 0B 05 23 46 A5 00 8F"))
        self.assertEqual(build_fan_command("fan_calib", {
            "_sequence": 12, "action": 6,
        }).data, bytes.fromhex("08 0C 06 A5 5A 00 00 6D"))
        vectors = [
            ("battery_fan_query", {"_sequence": 1}, "02 01 00 00 00 00 00 F6"),
            ("battery_fan_control", {"_sequence": 2, "mode": 1, "duty_pct": 40, "lease_s": 10},
             "01 02 01 28 0A 00 00 68"),
            ("battery_fan_calib", {"_sequence": 3, "action": 1, "step": 0, "duty_pct": 0, "lease_s": 10},
             "03 03 01 00 00 0A 00 8F"),
            ("battery_fan_commit", {"_sequence": 4, "chroma_cap_pct": 35, "hv_cap_pct": 70},
             "04 04 23 46 23 46 A5 CF"),
            ("battery_fan_clear", {"_sequence": 5}, "05 05 A5 5A 00 00 00 4A"),
        ]
        for name, values, expected in vectors:
            frame = build_bms_fan_command(name, values)
            self.assertEqual(frame.arbitration_id, 0x5AB)
            self.assertEqual(frame.data, bytes.fromhex(expected))

    def test_battery_fan_status_calibration_and_can1_detail_decode(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5AA, bytes.fromhex("0B B8 28 37 09 E7 0A 01"), False))
        protocol.ingest(CanFrame(0x5AD, bytes.fromhex("03 23 46 23 46 03 10 50"), False))
        protocol.ingest(CanFrame(0x5AE, bytes.fromhex("01 0F 32 01 32 03 00 00"), False))
        status = protocol.battery_fan["status"]
        self.assertEqual(status["rpm"], 3000)
        self.assertEqual(status["mode_name"], "手动")
        self.assertEqual(status["power_source_name"], "高压/DCDC 70W")
        self.assertTrue(status["flags"]["calibrated"])
        self.assertTrue(protocol.battery_fan["calibration"]["save_pending"])
        self.assertEqual(protocol.fan["calib_limits"]["dcdc_cap_pct"], 50)
        self.assertEqual(protocol.fan["calib_limits"]["active_tier_name"], "DCDC")
        self.assertEqual(protocol.fan["calib_limits"]["protocol_version"], 3)
        detail = decode_bms_fan_detail(bytes.fromhex("0B B8 01 90 02 26 09 E0"))
        self.assertEqual(detail["actual_duty_pct"], 40.0)
        self.assertEqual(detail["active_limit_pct"], 55.0)
        self.assertTrue(detail["flags"]["calibration_active"])

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
            ("fan_calib", {"action": 5, "battery_cap_pct": 80, "dcdc_cap_pct": 20}),
            ("fan_calib", {"action": 5, "battery_cap_pct": 4, "dcdc_cap_pct": 100}),
            ("fan_calib", {"action": 5, "battery_cap_pct": 100, "dcdc_cap_pct": 101}),
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

    def test_calibration_status_generations_advance_only_on_received_frames(self) -> None:
        protocol = VehicleProtocol()
        initial = protocol.snapshot({"connected": True})
        self.assertEqual(initial["fan"]["calib_status_generation"], 0)
        self.assertEqual(initial["battery_fan"]["status_generation"], 0)
        self.assertEqual(initial["battery_fan"]["calibration_generation"], 0)
        protocol.ingest(CanFrame(0x5A9, bytes.fromhex("01 02 14 1E 0F 01 00 00"), False))
        protocol.ingest(CanFrame(0x5AA, bytes.fromhex("0B B8 28 37 09 E7 0A 01"), False))
        protocol.ingest(CanFrame(0x5AD, bytes.fromhex("03 23 46 23 46 01 02 28"), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["fan"]["calib_status_generation"], 1)
        self.assertEqual(snapshot["battery_fan"]["status_generation"], 1)
        self.assertEqual(snapshot["battery_fan"]["calibration_generation"], 1)

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

    def test_fan_writes_reject_legacy_250k_profile(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            service.connection.update({"connected": True, "mode": "pcan",
                                       "bus_profile": "canb_legacy", "bitrate": 250000})
            fan = service.send_fan_command("fan_query", {}, True)
            battery = service.send_battery_fan_command("battery_fan_query", {}, True)
            self.assertFalse(fan["ok"])
            self.assertFalse(battery["ok"])
            self.assertIn("500 kbit/s", fan["error"])
            self.assertIn("500 kbit/s", battery["error"])
        finally:
            service.disconnect()

    def test_disconnect_attempts_safe_stop_before_invalidating_sessions(self) -> None:
        events: list[str] = []

        class FakeSession:
            def __init__(self, name: str) -> None:
                self.name = name

            def is_running(self) -> bool:
                return True

            def abort(self, reason: str = "") -> dict:
                events.append(f"{self.name}:abort:{reason}")
                return {"ok": True}

            def cancel_for_disconnect(self) -> None:
                events.append(f"{self.name}:invalidate")

        service = CanService(protocol_kind="vehicle")
        service.fan_calib_session = FakeSession("fan")  # type: ignore[assignment]
        service.battery_fan_calib_session = FakeSession("battery")  # type: ignore[assignment]
        service.disconnect()
        self.assertEqual(events, [
            "fan:abort:整车 CANB 正在断开", "fan:invalidate",
            "battery:abort:整车 CANB 正在断开", "battery:invalidate",
        ])

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
            blocked = service.send_battery_fan_command("battery_fan_query", {}, True)
            self.assertFalse(blocked["ok"])
            self.assertIn("PDM", blocked["error"])
            blocked_start = service.start_battery_fan_calibration()
            self.assertFalse(blocked_start["ok"])
            self.assertIn("不能同时", blocked_start["error"])
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

    def test_fan_calibration_suggested_caps_use_bus_current_and_merge(self) -> None:
        """推荐上限应使用总线总电流，并按档位保守合并。"""
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})

        # 同一档位先扫 PWM1：PWM1 双风扇正常，1A、3A、9A 三点。
        records = [
            {
                "tier": "dcdc", "channel": 1, "duty1_pct": 20, "duty2_pct": 0,
                "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
                "current_a": 3.0,
            },
            {
                "tier": "dcdc", "channel": 1, "duty1_pct": 40, "duty2_pct": 0,
                "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
                "current_a": 6.0,
            },
            {
                "tier": "dcdc", "channel": 1, "duty1_pct": 60, "duty2_pct": 0,
                "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
                "current_a": 19.0,
            },
        ]
        self.assertEqual(session._max_safe_duty(records, "dcdc"), 40)

        # 第一组 PWM1 得到 40%；随后 PWM2 电流达 17A，仍有效，合并后保持 40%。
        session.tier = "dcdc"
        session.records = records
        with session.lock:
            old_cap = session._max_safe_duty(session.records, session.tier)
            session.suggested_caps["dcdc_cap_pct"] = old_cap
        second = [{
            "tier": "dcdc", "channel": 2, "duty1_pct": 0, "duty2_pct": 55,
            "rpm1": 0, "rpm2": 0, "rpm3": 2500, "current_a": 17.0,
        }]
        second_cap = session._max_safe_duty(second, "dcdc")
        self.assertEqual(second_cap, 55)
        # 实际两次扫频分别计算后，再取更保守值，防止后扫回路覆盖此前更严格上限。
        merged_cap = min(old_cap, second_cap)
        self.assertEqual(merged_cap, 40)

        # 电池档使用 8A 阈值；超过 8A 的有效点会被排除。
        battery_records = [{
            "tier": "battery", "channel": 1, "duty1_pct": 30, "duty2_pct": 0,
            "rpm1": 2500, "rpm2": 2500, "rpm3": 0, "current_a": 8.5,
        }]
        self.assertIsNone(session._max_safe_duty(battery_records, "battery"))

        # 即使净风扇电流很小，只要总线总电流超过预算也不能推荐更高占空比。
        bg_limited = [{
            "tier": "dcdc", "channel": 1, "duty1_pct": 80, "duty2_pct": 0,
            "rpm1": 2500, "rpm2": 2500, "rpm3": 0,
            "current_a": 18.5, "delta_current_a": 3.0,
        }]
        self.assertIsNone(session._max_safe_duty(bg_limited, "dcdc"))

        # 没有有效数据时返回 None，不应生成 15% 的假建议。
        self.assertIsNone(session._max_safe_duty([], "dcdc"))

        # 非单调结果不能跨过失败点：40% 超预算后，即使 60% 偶然回落也只能推荐 20%。
        non_monotonic = [
            {"tier": "dcdc", "channel": 2, "duty2_pct": 20,
             "rpm3": 2000, "current_a": 10.0},
            {"tier": "dcdc", "channel": 2, "duty2_pct": 40,
             "rpm3": 2000, "current_a": 18.5},
            {"tier": "dcdc", "channel": 2, "duty2_pct": 60,
             "rpm3": 2000, "current_a": 17.0},
        ]
        self.assertEqual(session._max_safe_duty(non_monotonic, "dcdc"), 20)
        non_monotonic[1]["current_a"] = float("nan")
        self.assertEqual(session._max_safe_duty(non_monotonic, "dcdc"), 20)
        startup_dead_zone = [
            {"tier": "dcdc", "channel": 2, "duty2_pct": 5,
             "rpm3": 0, "current_a": 3.0},
            {"tier": "dcdc", "channel": 2, "duty2_pct": 10,
             "rpm3": 1800, "current_a": 4.0},
            {"tier": "dcdc", "channel": 2, "duty2_pct": 20,
             "rpm3": 2200, "current_a": 5.0},
        ]
        self.assertEqual(session._max_safe_duty(startup_dead_zone, "dcdc"), 20)

    def test_fan_cap_requires_both_loops_and_battery_fan_has_no_fake_default(self) -> None:
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        session.channel_caps["dcdc"][1] = 40
        values = list(session.channel_caps["dcdc"].values())
        self.assertFalse(all(value is not None for value in values))
        session.channel_caps["dcdc"][2] = 55
        values = list(session.channel_caps["dcdc"].values())
        self.assertEqual(min(values), 40)

        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        self.assertIsNone(battery.suggested_caps["chroma_cap_pct"])
        self.assertIsNone(battery.suggested_caps["hv_cap_pct"])
        battery_records = [
            {"duty_pct": 20, "delta_power_w": 20.0, "rpm": 1800},
            {"duty_pct": 40, "delta_power_w": 40.0, "rpm": 2200},
            {"duty_pct": 60, "delta_power_w": 30.0, "rpm": 2500},
        ]
        self.assertEqual(battery._max_safe_duty(battery_records, 35.0), 20)

    def test_calibration_parameter_and_stop_validation(self) -> None:
        session = FanCalibrationSession(lambda *_: {"ok": True}, lambda: _calib_snap())
        self.assertFalse(session.start_sweep(steps=[0, 20, 10])["ok"])
        self.assertFalse(session.start_sweep(steps=[0, 10, 10])["ok"])
        self.assertFalse(session.start_sweep(hold_s=float("nan"))["ok"])
        self.assertFalse(session.abort()["ok"])

        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        self.assertFalse(battery.start(steps=[0, 20, 10])["ok"])
        self.assertFalse(battery.start(hold_s=float("nan"))["ok"])
        self.assertFalse(battery.abort()["ok"])

    def test_battery_fan_safety_requires_live_session_and_valid_measurements(self) -> None:
        snap = {
            "connection": {"connected": True, "mode": "pcan",
                           "bus_profile": "canb", "bitrate": 500000},
            "pack": {"age": 0.1, "state": 5, "temperature_complete": True},
            "pdm": {"bus": {"offline": False, "age": 0.1, "voltage_v": 24.0,
                            "current_a": 3.0, "power_w": 72.0}},
            "battery_fan": {"status_age": 0.1, "calibration_age": 0.1,
                            "calibration": {"calib_state": 0, "chroma_budget_w": 35,
                                            "hv_budget_w": 70}, "status": {
                "power_source": 2,
                "protocol_version": 1,
                "flags": {"hardware_ready": True, "stall_confirmed": False,
                          "calibration_active": False},
            }},
        }
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        self.assertIn("未处于活动", session._safety_error(snap, 18.0, True))
        snap["pdm"]["bus"]["power_w"] = float("nan")
        self.assertIn("无效", session._safety_error(snap, 18.0))

    def test_battery_fan_step_sampling_retries_once_before_aborting(self) -> None:
        """最小 hold 的 1s 采样窗样本不足时延长一轮，而不是直接中止整次扫频。"""
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        baseline = {"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 0.0, "std_i": 0.0,
                    "std_p": 0.0, "baseline_id": 1, "step": 0}
        session._measure_baseline = lambda step, current, start: (dict(baseline), None)
        session._wait_for_active = lambda *args, **kwargs: None
        session._wait_for_completed = lambda **kwargs: None
        windows: list[float] = []

        def fake_samples(seconds, current, expected_step, expected_duty):
            windows.append(seconds)
            # settle(2s) 结果被丢弃；正式的 1s 窗只给 5 个样本（低于 10 个门槛）。
            count = 5 if len(windows) == 2 else 12
            return [{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 1500.0}] * count, None

        session._samples = fake_samples
        session.status = "running"
        session._run(steps=[50], hold_s=3.0, max_current_a=18.0)
        self.assertEqual(session.status, "completed")
        self.assertEqual(windows, [2.0, 1.0, 2.0])
        self.assertEqual(len(session.records), 1)
        self.assertEqual(session.records[0]["duty_pct"], 50)

        # 延长一轮后仍然不足才允许中止，且中止文案保留手动恢复路径。
        always_short = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        always_short._measure_baseline = lambda step, current, start: (dict(baseline), None)
        always_short._wait_for_active = lambda *args, **kwargs: None
        always_short._wait_for_completed = lambda **kwargs: None
        always_short._samples = lambda seconds, current, step, duty: (
            [{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 1500.0}] * 4, None)
        always_short.status = "running"
        always_short._run(steps=[50], hold_s=3.0, max_current_a=18.0)
        self.assertEqual(always_short.status, "aborted")
        self.assertIn("样本不足", always_short.abort_reason)

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

    def test_status_confirmation_rejects_pre_command_generations(self) -> None:
        fan_snap = _calib_snap()
        fan_snap["fan"].update({
            "calib_status_generation": 4,
            "calib_status_age": 0.1,
            "calib_status": {"calib_state": 1, "step": 2,
                             "calib_target_pct": [20, 0], "lease_remaining_s": 10},
        })
        fan = FanCalibrationSession(lambda *_: {"ok": True}, lambda: fan_snap)
        stale = fan._wait_for_calib_state(
            1, 2, 20, 0, after_generation=4, timeout_s=0.06)
        self.assertIn("新0x5A9", stale)
        fan_snap["fan"]["calib_status_generation"] = 5
        self.assertIsNone(fan._wait_for_calib_state(
            1, 2, 20, 0, after_generation=4, timeout_s=0.06))

        battery_snap = {
            "connection": {"connected": True, "mode": "pcan",
                           "bus_profile": "canb", "bitrate": 500000},
            "pack": {"age": 0.1, "state": 5, "temperature_complete": True},
            "pdm": {"bus": {"offline": False, "age": 0.1, "voltage_v": 24.0,
                            "current_a": 3.0, "power_w": 72.0}},
            "battery_fan": {"status_age": 0.1, "calibration_age": 0.1,
                            "status_generation": 8, "calibration_generation": 9,
                            "calibration": {"calib_state": 1, "step": 3,
                                            "target_duty_pct": 30,
                                            "chroma_budget_w": 35, "hv_budget_w": 70},
                            "status": {"power_source": 2, "protocol_version": 1,
                                       "lease_remaining_s": 10,
                                       "flags": {"hardware_ready": True,
                                                 "stall_confirmed": False,
                                                 "calibration_active": True}}},
        }
        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: battery_snap)
        battery.run_params = {"max_current_a": 18.0}
        stale = battery._wait_for_active(
            3, 30, after_status_generation=8, after_calibration_generation=9,
            timeout_s=0.06)
        self.assertIn("确认标定目标", stale)
        battery_snap["battery_fan"]["status_generation"] = 9
        battery_snap["battery_fan"]["calibration_generation"] = 10
        self.assertIsNone(battery._wait_for_active(
            3, 30, after_status_generation=8, after_calibration_generation=9,
            timeout_s=0.06))

    def test_disconnect_invalidates_recommendations_but_keeps_export_records(self) -> None:
        fan = FanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        fan.status = "completed"
        fan.records = [{"step": 1}]
        fan.suggested_caps = {"battery_cap_pct": 30, "dcdc_cap_pct": 50}
        fan.channel_caps["battery"] = {1: 30, 2: 35}
        fan.cancel_for_disconnect()
        self.assertEqual(fan.status, "stale")
        self.assertEqual(fan.records, [{"step": 1}])
        self.assertIsNone(fan.suggested_caps["battery_cap_pct"])
        self.assertNotIn("raw_samples", fan.get_snapshot())

        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        battery.status = "completed"
        battery.records = [{"step": 1}]
        battery.suggested_caps = {"chroma_cap_pct": 30, "hv_cap_pct": 50}
        battery.cancel_for_disconnect()
        self.assertEqual(battery.status, "stale")
        self.assertEqual(battery.records, [{"step": 1}])
        self.assertIsNone(battery.suggested_caps["hv_cap_pct"])

    def test_new_sweep_waits_for_old_worker_and_invalidates_rerun_result(self) -> None:
        class StuckWorker:
            def __init__(self) -> None:
                self.joined = False

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float) -> None:
                self.joined = True

        fan = FanCalibrationSession(lambda *_: {"ok": True}, lambda: _calib_snap())
        stuck = StuckWorker()
        fan._thread = stuck  # type: ignore[assignment]
        fan.status = "aborted"
        fan._stop_event.set()
        blocked = fan.start_sweep(channel=1, steps=[0], hold_s=3.0,
                                  max_current_a=18.0, tier="dcdc")
        self.assertFalse(blocked["ok"])
        self.assertIn("尚未安全退出", blocked["error"])
        self.assertTrue(stuck.joined)
        self.assertTrue(fan._stop_event.is_set(), "旧线程未退出时不得清除停止信号")

        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        battery_stuck = StuckWorker()
        battery._thread = battery_stuck  # type: ignore[assignment]
        battery.status = "aborted"
        battery._stop_event.set()
        blocked = battery.start(steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(blocked["ok"])
        self.assertIn("尚未安全退出", blocked["error"])
        self.assertTrue(battery._stop_event.is_set())

        # Once the prior worker is gone, a rerun is allowed, but its loop's old
        # recommendation is invalidated before the new worker starts.
        snap = _calib_snap(state=1, bus_current=3.0, bat_current=3.0,
                           bus_v=23.5, bat_v=23.5)
        fan = FanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        fan.status = "aborted"
        fan._stop_event.set()
        fan.channel_caps["battery"] = {1: 30, 2: 35}
        fan.suggested_caps = {"battery_cap_pct": 30, "dcdc_cap_pct": 50}
        try:
            started = fan.start_sweep(channel=1, steps=[0], hold_s=3.0,
                                      max_current_a=8.0, tier="battery")
            self.assertTrue(started["ok"], started)
            self.assertIsNone(fan.channel_caps["battery"][1])
            self.assertIsNone(fan.suggested_caps["battery_cap_pct"])
            self.assertEqual(fan.suggested_caps["dcdc_cap_pct"], 50)
        finally:
            fan._stop_event.set()

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
            "calib_status": {
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
            "calib_limits": {"protocol_version": 3},
            "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
            "calib_limits_age": 0.1,
        },
    }


class FanCalibrationWatchdogTest(unittest.TestCase):
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
                    "calib_status": {"calib_state": 1, "calib_state_name": "标定中",
                                     "step": 2, "calib_target_pct": [20, 0],
                                     "lease_remaining_s": 10},
                    "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                    "calib_status_age": 0.1,
                },
            }
        session = FanCalibrationSession(fake_send, snapshot)
        self.assertIsNone(session._watchdog(snapshot(motor=71.8), 18.0))
        self.assertIn("PDM", session._watchdog(snapshot(offline=True), 18.0))
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
        self.assertIn("不活动", session._watchdog(firmware_aborted, 18.0))
        # 温度失联或温度无效时必须中止，否则标定在没有温度保护的情况下继续。
        self.assertIn("温度输入失联", session._watchdog(snapshot(faults=0x18), 18.0))
        self.assertIn("温度无效", session._watchdog(snapshot(motor=None, ctrl=None), 18.0))
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
                "calib_status": {"calib_state": 1, "step": 2,
                                 "calib_target_pct": [20, 0], "lease_remaining_s": 10},
                "status_age": 0.1, "diagnostic_age": 0.1, "power_status_age": 0.1,
                "calib_status_age": 0.1,
            },
        }
        self.assertIn("档位", session._watchdog(snap, 8.0, expected_state=1))


if __name__ == "__main__":
    unittest.main()
