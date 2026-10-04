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
from canhost.vehicle.calibration import BatteryFanCalibrationSession


class FanControllerToolTest(unittest.TestCase):
    def test_calibration_exports_use_native_save_dialog_after_abort(self) -> None:
        api = Api.__new__(Api)
        api._window = MagicMock()
        api._vehicle_service = MagicMock()
        api._vehicle_service.export_battery_fan_calibration.side_effect = lambda format_type: {
            "ok": True,
            "data": (json.dumps({"status": "aborted", "abort_reason": "保护中止",
                                 "baseline_raw_samples": [{"t": 123.0, "p": 48.0}]},
                                ensure_ascii=False)
                     if format_type == "json" else
                     "session_status,abort_reason\r\naborted,保护中止\r\n"),
        }

        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "sys.modules", {"webview": SimpleNamespace(SAVE_DIALOG="save")}):
            root = Path(directory)
            battery_csv = root / "battery-aborted.csv"
            battery_json = root / "battery-aborted.json"
            for path, action in (
                    (battery_csv, api.choose_export_battery_fan_calibration),
                    (battery_json, lambda: api.choose_export_battery_fan_calibration("json"))):
                api._window.create_file_dialog.return_value = str(path)
                result = action()
                self.assertTrue(result["ok"])
                self.assertEqual(Path(result["path"]), path)
                self.assertTrue(path.exists())

            self.assertIn("保护中止", battery_csv.read_text(encoding="utf-8-sig"))
            battery_data = json.loads(battery_json.read_text(encoding="utf-8"))
            self.assertEqual(battery_data["abort_reason"], "保护中止")
            self.assertEqual(battery_data["baseline_raw_samples"][0]["p"], 48.0)
            self.assertEqual(api._vehicle_service.export_battery_fan_calibration.call_args_list[-2:][0].args,
                             ("csv",))
            self.assertEqual(api._vehicle_service.export_battery_fan_calibration.call_args.args,
                             ("json",))

    def test_calibration_export_cancel_does_not_generate_data(self) -> None:
        api = Api.__new__(Api)
        api._window = MagicMock()
        api._window.create_file_dialog.return_value = None
        api._vehicle_service = MagicMock()
        with patch.dict("sys.modules", {"webview": SimpleNamespace(SAVE_DIALOG="save")}):
            result = api.choose_export_battery_fan_calibration("csv")
        self.assertTrue(result["cancelled"])
        api._vehicle_service.export_battery_fan_calibration.assert_not_called()

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
        snap["battery_fan"]["status"]["power_source"] = 0
        self.assertIn("充电状态", session._safety_error(snap, 8.0))
        snap["fault"] = {"age": 0.1, "flags": {"charge_mode": False}}
        self.assertIsNone(session._safety_error(snap, 8.0))
        snap["fault"]["flags"]["charge_mode"] = True
        self.assertIn("正在充电", session._safety_error(snap, 8.0))
        snap["fault"]["flags"]["charge_mode"] = False
        self.assertIn("8A", session._safety_error(snap, 18.0))
        snap["battery_fan"]["status"]["power_source"] = 1
        self.assertIn("Chroma", session._safety_error(snap, 8.0))
        snap["battery_fan"]["status"]["power_source"] = 0
        session.status = "running"
        session._expected_power_source = 2
        session._expected_pack_state = 5
        self.assertIn("供电或BMS状态已变化", session._safety_error(snap, 8.0))
        session._expected_power_source = 0
        snap["pack"]["state"] = 5
        snap["battery_fan"]["status"]["power_source"] = 2
        self.assertIn("供电或BMS状态已变化", session._safety_error(snap, 8.0))
        session.status = "idle"
        snap["pack"]["state"] = 4
        self.assertIn("待机或高压接通", session._safety_error(snap, 8.0))
        snap["pack"]["state"] = 5
        snap["battery_fan"]["status"]["power_source"] = 2
        snap["battery_fan"]["status"]["flags"]["stall_confirmed"] = True
        self.assertIn("停转", session._safety_error(snap, 18.0))
        snap["battery_fan"]["status"]["flags"]["stall_confirmed"] = False
        snap["pack"]["temperature_complete"] = False
        self.assertIn("温度", session._safety_error(snap, 18.0))
        summary = session._median([{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 2000.0}] * 10)
        self.assertEqual({key: summary[key] for key in ("v", "i", "p", "rpm")},
                         {"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 2000.0})
        self.assertEqual(summary["std_i"], 0.0)

        snap["pack"]["state"] = 3
        snap["pack"]["temperature_complete"] = True
        snap["battery_fan"]["status"]["power_source"] = 0
        snap["pdm"]["bus"]["current_a"] = 9.0
        service = CanService(protocol_kind="vehicle")
        try:
            service.vehicle_snapshot = MagicMock(return_value=snap)  # type: ignore[method-assign]
            for action in (1, "1", None):
                values = {"step": 0, "duty_pct": 30, "lease_s": 10}
                if action is not None:
                    values["action"] = action
                result = service.send_battery_fan_command(
                    "battery_fan_calib", values, True)
                self.assertFalse(result["ok"])
                self.assertIn("8.0A保护值", result["error"])
        finally:
            service.disconnect()

    def test_battery_fan_commands(self) -> None:
        vectors = [
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
            (build_fan_command("fan_restore_defaults", {"_sequence": 6}),
             bytes.fromhex("04 06 A5 00 00 00 00 D7")),
            (build_fan_command("fan_query", {"_sequence": 7}),
             bytes.fromhex("05 07 00 00 00 00 00 F1")),
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

    def test_calibration_pause_flag_is_decoded(self) -> None:
        paused = decode_fan_calib_status(bytes.fromhex("01 04 28 00 3C 02 01 00"))
        self.assertTrue(paused["output_paused"])
        self.assertEqual(paused["calib_state_name"], "标定中")
        self.assertEqual(paused["lease_remaining_s"], 60)

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

    def test_vehicle_service_rejects_non_500k_profile(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            result = service.connect({"mode": "simulation", "bus_profile": "canb", "bitrate": 250000})
            self.assertFalse(result["ok"])
            self.assertIn("内置模拟只支持 500 kbit/s", result["error"])
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
        service.battery_fan_calib_session = FakeSession("battery")  # type: ignore[assignment]
        service.disconnect()
        self.assertEqual(events, [
            "battery:abort:整车 CANB 正在断开", "battery:invalidate",
        ])

    def test_battery_calibration_blocks_vehicle_fan_commands(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            service.battery_fan_calib_session.status = "running"
            blocked = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(blocked["ok"])
            self.assertIn("PDM", blocked["error"])
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


    def test_fan_cap_requires_both_loops_and_battery_fan_has_no_fake_default(self) -> None:
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

    def test_battery_fan_frame_gaps_pause_sampling_then_stop_on_loss(self) -> None:
        snap = {
            "connection": {"connected": True, "mode": "pcan",
                           "bus_profile": "canb", "bitrate": 500000},
            "pack": {"age": 0.1, "state": 5, "temperature_complete": True},
            "pdm": {"bus": {"offline": False, "age": 0.1, "voltage_v": 24.0,
                            "current_a": 3.0, "power_w": 72.0}},
            "battery_fan": {"status_age": 0.1, "calibration_age": 1.5,
                            "calibration": {"calib_state": 1, "step": 1,
                                            "target_duty_pct": 20, "chroma_budget_w": 35,
                                            "hv_budget_w": 70},
                            "status": {"power_source": 2, "protocol_version": 1,
                                       "lease_remaining_s": 10,
                                       "flags": {"hardware_ready": True,
                                                 "stall_confirmed": False,
                                                 "calibration_active": True}}},
        }
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: snap)
        self.assertIsNone(session._safety_error(snap, 18.0, True))
        self.assertTrue(session._sample_frames_fresh(snap))
        for frame, age_key, temporary_age, expired_age in (
                ("pack", "age", 1.7, 2.1),
                ("pdm", "age", 1.2, 1.6),
                ("battery_status", "status_age", 1.2, 1.6),
                ("battery_calibration", "calibration_age", 2.1, 3.1)):
            with self.subTest(frame=frame):
                target = (snap["pack"] if frame == "pack" else
                          snap["pdm"]["bus"] if frame == "pdm" else
                          snap["battery_fan"])
                original_age = target[age_key]
                target[age_key] = temporary_age
                self.assertIsNone(session._safety_error(snap, 18.0, True))
                self.assertFalse(session._sample_frames_fresh(snap))
                target[age_key] = expired_age
                self.assertIsNotNone(session._safety_error(snap, 18.0, True))
                target[age_key] = original_age
        calls = 0

        def recovering_snapshot():
            nonlocal calls
            calls += 1
            snap["pdm"]["bus"]["age"] = 1.2 if calls <= 2 else 0.1
            snap["pdm"]["bus"]["current_a"] = 6.0 if calls <= 2 else 3.0
            return snap

        session.snapshot_fn = recovering_snapshot
        session._last_command_monotonic = time.monotonic()
        samples, error = session._samples(0.12, 18.0, 1, 20)
        self.assertIsNone(error)
        self.assertGreaterEqual(calls, 3)
        self.assertTrue(samples)
        self.assertTrue(all(sample["i"] == 3.0 for sample in samples))
        snap["battery_fan"]["status"]["flags"]["calibration_active"] = False
        self.assertIn("未处于活动", session._safety_error(snap, 18.0, True))
        snap["battery_fan"]["status"]["flags"]["calibration_active"] = True
        snap["battery_fan"]["calibration_age"] = 3.1
        self.assertIn("0x5AD 标定状态已断流 3.1s", session._safety_error(snap, 18.0, True))
        snap["battery_fan"]["calibration_age"] = None
        self.assertIn("尚未收到 CANB 0x5AD", session._safety_error(snap, 18.0, True))

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

    def test_battery_fan_command_confirmation_uses_pre_send_generations(self) -> None:
        """应答前已到达的状态帧也属于本次命令，不能再等不存在的下一代。"""
        generations = {"status_generation": 4, "calibration_generation": 7}
        sent = []

        def snapshot():
            return {"battery_fan": generations}

        def send(name, values, acknowledged):
            sent.append((name, values["action"]))
            generations["status_generation"] += 1
            generations["calibration_generation"] += 1
            return {"ok": True}

        session = BatteryFanCalibrationSession(send, snapshot)
        session.status = "running"
        observed = []
        session._wait_for_active = lambda step, duty, **kwargs: observed.append(
            ("active", step, duty, kwargs["after_status_generation"],
             kwargs["after_calibration_generation"]))
        session._wait_for_completed = lambda **kwargs: observed.append(
            ("completed", kwargs["after_status_generation"],
             kwargs["after_calibration_generation"]))
        session._samples = lambda seconds, current, step, duty: (
            [{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 0.0 if duty == 0 else 1500.0}] * 12,
            None)
        session._run(steps=[20], hold_s=3.0, max_current_a=18.0)
        self.assertEqual(session.status, "completed")
        self.assertEqual(sent, [("battery_fan_calib", 1),
                                ("battery_fan_calib", 2),
                                ("battery_fan_calib", 3)])
        self.assertEqual(observed, [("active", 0, 0, 4, 7),
                                    ("active", 1, 20, 5, 8),
                                    ("completed", 6, 9)])
        exported = json.loads(session.export_json())
        self.assertEqual(len(exported["baseline_raw_samples"]), 24)
        self.assertEqual(len(exported["raw_samples"]), 24)
        self.assertEqual(exported["baseline_raw_samples"][0]["phase"], "baseline_settle")
        self.assertEqual(exported["raw_samples"][12]["phase"], "step_measure")
        self.assertEqual(exported["raw_samples"][12]["baseline_id"], 1)

    def test_battery_fan_lost_ack_uses_new_firmware_state(self) -> None:
        snap = {
            "connection": {"connected": True, "mode": "pcan",
                           "bus_profile": "canb", "bitrate": 500000},
            "pack": {"age": 0.1, "state": 5, "temperature_complete": True},
            "pdm": {"bus": {"offline": False, "age": 0.1, "voltage_v": 24.0,
                            "current_a": 3.0, "power_w": 72.0}},
            "battery_fan": {"status_age": 0.1, "calibration_age": 0.1,
                            "status_generation": 4, "calibration_generation": 7,
                            "calibration": {"calib_state": 0, "step": 0,
                                            "target_duty_pct": 0,
                                            "chroma_budget_w": 35, "hv_budget_w": 70},
                            "status": {"power_source": 2, "protocol_version": 1,
                                       "lease_remaining_s": 0,
                                       "flags": {"hardware_ready": True,
                                                 "stall_confirmed": False,
                                                 "calibration_active": False}}},
        }

        def send(_name, values, _acknowledged):
            battery = snap["battery_fan"]
            battery["status_generation"] += 1
            battery["calibration_generation"] += 1
            battery["status"]["lease_remaining_s"] = 15 if values["action"] != 3 else 0
            battery["status"]["flags"]["calibration_active"] = values["action"] != 3
            battery["calibration"]["calib_state"] = 3 if values["action"] == 3 else 1
            battery["calibration"]["step"] = values["step"]
            battery["calibration"]["target_duty_pct"] = values["duty_pct"]
            return {"ok": False, "ack_timeout": True, "error": "0x5AC 未收到"}

        session = BatteryFanCalibrationSession(send, lambda: snap)
        session.status = "running"
        session.run_params = {"max_current_a": 18.0}
        session._expected_pack_state = 5
        session._expected_power_source = 2
        started = session._send(1, 0, 0)
        self.assertTrue(started["ok"])
        self.assertTrue(started["status_confirmed"])
        self.assertTrue(started["ack_timeout"])

        snap["battery_fan"]["status"]["lease_remaining_s"] = 10
        renewed = session._send(2, 0, 0)
        self.assertFalse(renewed["ok"])
        self.assertTrue(renewed["ack_timeout"])
        session._last_command_monotonic = time.monotonic() - 6.0
        self.assertIsNone(session._renew_if_due(0, 0))
        self.assertLess(session._last_command_monotonic, time.monotonic() - 3.0)

        stopped = session._send(3, 0, 0)
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["status_confirmed"])

    def test_battery_fan_missing_ack_does_not_hide_rejection_or_old_lease(self) -> None:
        session = BatteryFanCalibrationSession(
            lambda *_: {"ok": False, "error": "BMS 拒绝：安全条件拒绝"}, lambda: {})
        self.assertIn("安全条件拒绝", session._send(1, 0, 0)["error"])
        session.send_fn = lambda *_: {"ok": False, "ack_timeout": True, "error": "0x5AC 未收到"}
        with patch.object(session, "_wait_for_active", return_value="未收到新状态"):
            self.assertIn("未收到新状态", session._send(1, 0, 0)["error"])

        snap = {"battery_fan": {
            "status_generation": 4, "calibration_generation": 7,
            "status_age": 0.1, "calibration_age": 0.1,
            "status": {"flags": {"calibration_active": True},
                       "lease_remaining_s": 10},
            "calibration": {"calib_state": 1, "step": 1,
                            "target_duty_pct": 20},
        }}
        session = BatteryFanCalibrationSession(
            lambda *_: {"ok": False, "ack_timeout": True, "error": "0x5AC 未收到"},
            lambda: snap)
        session._safety_error = lambda *_args, **_kwargs: None
        with patch.object(session, "_wait_for_active") as verify:
            result = session._send(2, 1, 20)
        self.assertFalse(result["ok"])
        self.assertTrue(result["ack_timeout"])
        verify.assert_not_called()
        session._last_command_monotonic = time.monotonic() - 6.0
        snap["battery_fan"]["status"]["lease_remaining_s"] = 4
        self.assertIn("续发失败", session._renew_if_due(1, 20))

    def test_battery_fan_ack_after_two_seconds_is_accepted(self) -> None:
        self.assertGreaterEqual(BATTERY_FAN_ACK_TIMEOUT_S, 2.5)
        service = CanService(protocol_kind="vehicle")
        service.connection.update({"connected": True, "mode": "pcan",
                                   "bus_profile": "canb", "bitrate": 500000})
        service.bus = MagicMock()
        timer = threading.Timer(2.15, lambda: service._ingest(
            CanFrame(0x5AC, bytes([3, 1, 0, 0, 0, 55, 1, 2]), False)))
        timer.start()
        try:
            result = service.send_battery_fan_command(
                "battery_fan_calib", {"action": 1, "step": 0,
                                      "duty_pct": 0, "lease_s": 15}, True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sequence"], 1)
        finally:
            timer.join(timeout=3.5)
            service.disconnect()

    def test_battery_fan_json_keeps_partial_samples_after_abort(self) -> None:
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        session.status = "aborted"
        session.abort_reason = "PDM 遥测超时"
        session.run_params = {"steps": [5, 10], "hold_s": 5.0}
        session._samples = lambda *args: (
            [{"t": 123.0, "v": 24.0, "i": 2.0, "p": 48.0, "rpm": 0.0}],
            "PDM 遥测超时")
        _, error = session._capture_samples(2.0, 8.0, 0, 0, 1, "baseline_settle", 0)
        self.assertIn("PDM", error)
        data = json.loads(session.export_json())
        self.assertEqual(data["status"], "aborted")
        self.assertEqual(data["abort_reason"], "PDM 遥测超时")
        self.assertEqual(data["records"], [])
        self.assertEqual(data["baseline_raw_samples"][0]["phase"], "baseline_settle")
        self.assertEqual(data["baseline_raw_samples"][0]["t"], 123.0)

        service = CanService(protocol_kind="vehicle")
        try:
            service.battery_fan_calib_session = session
            result = service.export_battery_fan_calibration("json")
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(result["data"])["abort_reason"], "PDM 遥测超时")
            self.assertFalse(service.export_battery_fan_calibration("xml")["ok"])
        finally:
            service.disconnect()

    def test_battery_fan_baseline_reports_firmware_rejection(self) -> None:
        session = BatteryFanCalibrationSession(
            lambda *_: {"ok": False, "error": "BMS 拒绝：安全条件拒绝"},
            lambda: {"battery_fan": {"status_generation": 1,
                                     "calibration_generation": 1}})
        baseline, error = session._measure_baseline(0, 8.0, True)
        self.assertIsNone(baseline)
        self.assertIn("安全条件拒绝", error)

    def test_battery_fan_sampling_renews_15_second_lease(self) -> None:
        sent = []

        def send(name, values, acknowledged):
            sent.append(dict(values))
            return {"ok": len(sent) < 3, "error": "BMS 拒绝：安全条件拒绝"}

        session = BatteryFanCalibrationSession(send, lambda: {})
        with patch("canhost.vehicle.calibration.time.monotonic", return_value=100.0):
            self.assertTrue(session._send(1, 0, 0)["ok"])
        with patch("canhost.vehicle.calibration.time.monotonic", return_value=104.9):
            self.assertIsNone(session._renew_if_due(2, 20))
        self.assertEqual(len(sent), 1)
        with patch("canhost.vehicle.calibration.time.monotonic", return_value=105.0):
            self.assertIsNone(session._renew_if_due(2, 20))
        self.assertEqual(sent[1]["action"], 2)
        self.assertEqual((sent[1]["step"], sent[1]["duty_pct"], sent[1]["lease_s"]),
                         (2, 20, 15))
        with patch("canhost.vehicle.calibration.time.monotonic", return_value=110.0):
            error = session._renew_if_due(2, 20)
        self.assertIn("安全条件拒绝", error)

    def test_battery_fan_external_target_is_not_overwritten_by_renewal(self) -> None:
        sent = []
        session = BatteryFanCalibrationSession(
            lambda name, values, acknowledged: sent.append(dict(values)) or {"ok": True},
            lambda: {"battery_fan": {"calibration": {
                "step": 9, "target_duty_pct": 90}}})
        session._safety_error = lambda *args, **kwargs: None
        session._last_command_monotonic = 0.0
        samples, error = session._samples(0.1, 18.0, 2, 20)
        self.assertEqual(samples, [])
        self.assertIn("外部改写", error)
        self.assertEqual(sent, [])

    def test_battery_fan_full_duty_waits_for_startup_verdict(self) -> None:
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        session.status = "running"
        session._measure_baseline = lambda *args: (
            {"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 0.0,
             "std_i": 0.0, "std_p": 0.0, "baseline_id": 1, "quality_ok": True}, None)
        session._wait_for_active = lambda *args, **kwargs: None
        session._wait_for_completed = lambda **kwargs: None
        windows = []

        def samples(seconds, current, step, duty):
            windows.append(seconds)
            return ([{"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 1500.0}] * 12, None)

        session._samples = samples
        session._run([100], hold_s=3.0, max_current_a=18.0)
        self.assertEqual(session.status, "completed")
        self.assertEqual(windows, [2.0, 5.0])

    def test_battery_fan_power_variation_is_warning_not_abort(self) -> None:
        session = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        baseline = {"v": 24.0, "i": 2.0, "p": 48.0, "rpm": 0.0,
                    "std_i": 0.0, "std_p": 0.0, "baseline_id": 1,
                    "step": 0, "quality_ok": True}
        session._measure_baseline = lambda step, current, start: (dict(baseline), None)
        session._wait_for_active = lambda *args, **kwargs: None
        session._wait_for_completed = lambda **kwargs: None

        def noisy_samples(seconds, current, expected_step, expected_duty):
            return ([{"v": 24.0,
                      "i": 2.0 if index % 2 else 2.4,
                      "p": 48.0 if index % 2 else 57.6,
                      "rpm": 1500.0} for index in range(12)], None)

        session._samples = noisy_samples
        session.status = "running"
        session._run(steps=[50], hold_s=3.0, max_current_a=18.0)
        self.assertEqual(session.status, "completed")
        self.assertEqual(len(session.records), 1)
        self.assertFalse(session.records[0]["quality_ok"])
        self.assertEqual(len(session.quality_warnings), 1)
        self.assertIsNone(session.suggested_caps["hv_cap_pct"])


    def test_status_confirmation_rejects_pre_command_generations(self) -> None:
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

        battery = BatteryFanCalibrationSession(lambda *_: {"ok": True}, lambda: {})
        battery_stuck = StuckWorker()
        battery._thread = battery_stuck  # type: ignore[assignment]
        battery.status = "aborted"
        battery._stop_event.set()
        blocked = battery.start(steps=[0], hold_s=3.0, max_current_a=18.0)
        self.assertFalse(blocked["ok"])
        self.assertIn("尚未安全退出", blocked["error"])
        self.assertTrue(battery._stop_event.is_set())


    def test_battery_calibration_abort_accepts_firmware_aborted_zero_target(self) -> None:
        """F405 已安全中止时，STOP ACK 后不应再要求 COMPLETED。"""
        sent = []
        snap = {
            "battery_fan": {
                "status_age": 0.1,
                "calibration_age": 0.1,
                "status_generation": 10,
                "calibration_generation": 20,
                "status": {"flags": {"calibration_active": False}},
                "calibration": {
                    "calib_state": 2,
                    "calib_state_name": "已中止",
                    "abort_reason": 1,
                    "abort_reason_name": "状态变化",
                    "step": 2,
                    "target_duty_pct": 0,
                },
            },
        }

        def fake_send(name, vals, ack):
            sent.append((name, vals, ack))
            return {"ok": True}

        session = BatteryFanCalibrationSession(fake_send, lambda: snap)
        session.status = "running"
        session._wait_for_completed = lambda **kwargs: self.fail(
            "固件已上报 ABORTED/0%，不应再等待 COMPLETED")
        result = session.abort("F405 标定会话已安全中止")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("停止命令失败", result["reason"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "battery_fan_calib")
        self.assertEqual(sent[0][1]["action"], 3)

        # 兼容旧固件：STOP 后新状态可能是 INACTIVE/0%。中止流程接受，
        # 正常扫频完成流程仍必须要求 COMPLETED，不能生成可提交建议。
        snap["battery_fan"]["status_generation"] = 11
        snap["battery_fan"]["calibration_generation"] = 21
        snap["battery_fan"]["calibration"]["calib_state"] = 0
        terminal_session = BatteryFanCalibrationSession(fake_send, lambda: snap)
        self.assertIsNone(terminal_session._wait_for_completed(
            after_status_generation=10,
            after_calibration_generation=20,
            allow_safe_terminal=True,
            timeout_s=0.1))
        strict_error = terminal_session._wait_for_completed(
            after_status_generation=10,
            after_calibration_generation=20,
            timeout_s=0.1)
        self.assertIn("未进入可提交", strict_error)



if __name__ == "__main__":
    unittest.main()
