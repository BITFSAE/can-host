"""Host-side tests for the Windows BMS CAN application."""

from __future__ import annotations

import time
import csv
from datetime import datetime, timedelta
import sqlite3
import tempfile
from pathlib import Path
import unittest

from TOOLS.bms_host.can_service import CanService
from TOOLS.bms_host.app import Api
from TOOLS.bms_host.protocol import (BmsProtocol, CanFrame, build_command, build_fan_command,
                                     command_ack_matches, fan_ack_matches, switch_catalog)
from TOOLS.bms_host.simulator import BmsSimulator


class BmsProtocolTest(unittest.TestCase):
    def test_cell_voltage_and_temperature_mapping(self) -> None:
        protocol = BmsProtocol()
        # Slave 2, frame 0 -> global cells 24..26; frame 5 -> 43..46.
        protocol.ingest(CanFrame(0x180650F3, b"\x00\x00" + (3701).to_bytes(2, "little")
                                 + (3702).to_bytes(2, "little") + (3703).to_bytes(2, "little"), True))
        protocol.ingest(CanFrame(0x180B50F3, b"".join(value.to_bytes(2, "little") for value in (3720, 3721, 3722, 3723)), True))
        protocol.ingest(CanFrame(0x184150F3, bytes((58, 59, 60, 61, 62, 63, 64, 65)), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["cells"][23]["value"], 3701)
        self.assertEqual(snapshot["cells"][42]["value"], 3720)
        self.assertEqual(snapshot["cells"][45]["value"], 3723)
        self.assertEqual(snapshot["temps"][8]["value"], 28)
        self.assertEqual(snapshot["temps"][15]["value"], 35)

    def test_summary_fault_alarm_and_config_decode(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 0A 4E 1F 72"), True))
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("72 80 00 00 01 C6 21 02"), True))
        alarm_bytes = bytearray(8)
        alarm_bytes[0] = 0b00001001  # index 0=1, index 1=2
        protocol.ingest(CanFrame(0x187850F4, bytes(alarm_bytes), True))
        protocol.ingest(CanFrame(0x187750F4, bytes.fromhex("10 5E 0C 1C 5A 1E"), True))
        protocol.ingest(CanFrame(0x187F50F4, bytes.fromhex("F3 3F E0 04"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["overview"]["voltage_v"], 570.0)
        self.assertEqual(snapshot["overview"]["current_a"], 1.0)
        self.assertEqual(snapshot["overview"]["state"], 7)
        self.assertEqual(snapshot["overview"]["state_name"], "FAULT")
        self.assertEqual(snapshot["fault"]["code_hex"], "0x80000001")
        self.assertTrue(snapshot["fault"]["slave_offline"][0])
        self.assertTrue(snapshot["fault"]["slave_offline"][5])
        self.assertEqual(snapshot["alarms"][0]["level"], 1)
        self.assertEqual(snapshot["alarms"][1]["level"], 2)
        self.assertEqual(snapshot["config"]["thresholds"]["ov_mv"], 4190)
        self.assertEqual(snapshot["config"]["thresholds"]["ot_c"], 60)
        self.assertEqual(snapshot["config"]["switch_version"], 4)
        self.assertTrue(snapshot["config"]["switches"]["ivt_voltage_loss"])

    def test_fault_frames_do_not_keep_electrical_summary_fresh(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 0A 4E 1F 32"), True))
        clock[0] = 2.0
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("32 00 00 00 00 00 00 02"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["fault"]["age"], 0.0)
        self.assertEqual(snapshot["connection"]["summary_age"], 2.0)

    def test_low_rate_measurements_have_independent_ages(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 0A 4E 1F 30"), True))
        protocol.ingest(CanFrame(0x186750F4, bytes.fromhex("16 44"), True))
        protocol.ingest(CanFrame(0x186150F4, bytes.fromhex("0F A0 0B B8 00 01"), True))

        clock[0] = 2.0
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 0A 4E 1F 30"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["connection"]["summary_age"], 0.0)
        self.assertEqual(snapshot["overview"]["cell_sum_age"], 2.0)
        self.assertEqual(snapshot["overview"]["cell_extremes_age"], 2.0)

        clock[0] = 3.0
        protocol.ingest(CanFrame(0x186750F4, bytes.fromhex("16 44"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["overview"]["cell_sum_age"], 0.0)
        self.assertEqual(snapshot["overview"]["cell_extremes_age"], 3.0)

    def test_trend_does_not_append_stale_electrical_values(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        status = bytes.fromhex("16 44 00 0A 4E 1F 30")
        protocol.ingest(CanFrame(0x186050F4, status, True))
        clock[0] = 0.5
        protocol.ingest(CanFrame(0x186050F4, status, True))
        trend_count = len(protocol.snapshot({"connected": True})["trends"])
        clock[0] = 2.1
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 00 00 02"), True))
        self.assertEqual(len(protocol.snapshot({"connected": True})["trends"]), trend_count)

    def test_configuration_reports_expire_independently(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x187750F4, bytes.fromhex("10 5E 0C 1C 5A 1E"), True))
        protocol.ingest(CanFrame(0x187F50F4, bytes.fromhex("F3 3F E0 04"), True))
        protocol.ingest(CanFrame(0x186B50F4, bytes.fromhex("04 D3 16 30 00 1E 04 01"), True))

        clock[0] = 2.0
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["config"]["thresholds_age"], 2.0)
        self.assertEqual(snapshot["config"]["switches_age"], 2.0)
        self.assertEqual(snapshot["config"]["runtime_age"], 2.0)

        clock[0] = 2.5
        protocol.ingest(CanFrame(0x187750F4, bytes.fromhex("10 5E 0C 1C 5A 1E"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["config"]["thresholds_age"], 0.0)
        self.assertEqual(snapshot["config"]["switches_age"], 2.5)
        self.assertEqual(snapshot["config"]["runtime_age"], 2.5)

    def test_identity_selects_debug_bringup_slave_timeout(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        cell_frame = CanFrame(0x180050F3, b"\x00\x00\x74\x0E\x75\x0E\x76\x0E", True)
        protocol.ingest(CanFrame(0x186C50F4, bytes.fromhex("04 01 00 00 00 00 00 00"), True))
        protocol.ingest(cell_frame)
        clock[0] = 0.4
        self.assertIsNone(protocol.snapshot({"connected": True})["cells"][0]["value"])

        protocol.ingest(CanFrame(0x186C50F4, bytes.fromhex("04 02 00 00 00 00 00 00"), True))
        protocol.ingest(cell_frame)
        clock[0] = 0.5
        self.assertEqual(protocol.slave_sample_timeout_s, 0.75)
        self.assertEqual(protocol.snapshot({"connected": True})["cells"][0]["value"], 3700)

    def test_canb_snapshot_does_not_invent_slave_data(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 30"), False))
        snapshot = protocol.snapshot({"connected": True, "bus_profile": "canb"})
        self.assertFalse(snapshot["raw_cell_data_available"])
        self.assertEqual(snapshot["cells"], [])
        self.assertEqual(snapshot["temps"], [])
        self.assertEqual(snapshot["modules"], [])

    def test_rtc_reply_has_sequence_and_expires(self) -> None:
        clock = [0.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x18A450F4, bytes.fromhex("00 2A 1A 08 03 0C 22 38"), True))
        self.assertEqual(protocol.snapshot({"connected": True})["rtc_reply"]["sequence"], 0x2A)
        clock[0] = 5.1
        self.assertEqual(protocol.snapshot({"connected": True})["rtc_reply"]["age"], 5.1)

    def test_disconnect_clears_previous_rtc_reply(self) -> None:
        service = CanService()
        service.protocol.ingest(CanFrame(0x18A450F4, bytes.fromhex("00 2A 1A 08 03 0C 22 38"), True))
        service.disconnect()
        self.assertNotIn("status", service.snapshot()["rtc_reply"])

    def test_simulator_rejects_nonzero_reserved_command_bytes(self) -> None:
        frames: list[CanFrame] = []
        simulator = BmsSimulator(frames.append)
        simulator.on_command(CanFrame(0x18A050F5, bytes.fromhex("41 01 16 44 00 1E 00 01"), True))
        ack = next(frame for frame in frames if frame.arbitration_id == 0x18A650F4)
        self.assertEqual(ack.data[3], 5)

    def test_canb_simulator_does_not_emit_can1_frames(self) -> None:
        frames: list[CanFrame] = []
        simulator = BmsSimulator(frames.append, bus_profile="canb")
        simulator._emit_summary()
        ids = {frame.arbitration_id for frame in frames}
        self.assertIn(0x4B0, ids)
        self.assertIn(0x512, ids)
        self.assertNotIn(0x186050F4, ids)
        self.assertFalse(any(frame.arbitration_id == 0x180050F3 for frame in frames))

    def test_bmslog_rejects_corrupt_frame_during_load(self) -> None:
        service = CanService()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.bmslog"
            database = sqlite3.connect(path)
            database.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE frames (
                    seq INTEGER PRIMARY KEY, timestamp REAL NOT NULL, direction TEXT NOT NULL,
                    arbitration_id INTEGER NOT NULL, extended INTEGER NOT NULL, data BLOB NOT NULL
                );
            """)
            database.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [
                ("format", "BITFSAE_BMS_LOG"), ("schema_version", "1")])
            database.execute("INSERT INTO frames VALUES (1, 1.0, 'rx', ?, 1, ?)",
                             (0x186050F4, bytes(9)))
            database.commit()
            database.close()
            result = service.load_replay(str(path))
            self.assertFalse(result["ok"])
            self.assertIn("CAN FD", result["error"])

    def test_charge_mode_summary_uses_direct_current_without_offset(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 04 00 02"), True))
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 1E 50 1F 30"), True))
        self.assertEqual(protocol.snapshot({"connected": True})["overview"]["current_a"], 3.0)

    def test_pack_status_uses_signed_current_and_validity_bits_on_canb(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x4B0, bytes.fromhex("16 44 FF E2 50 02 30"), False))
        overview = protocol.snapshot({"connected": True})["overview"]
        self.assertIsNone(overview["voltage_v"])
        self.assertEqual(overview["current_a"], -3.0)
        self.assertFalse(overview["soc_valid"])
        self.assertFalse(overview["cell_voltage_complete"])

    def test_extreme_measurements_wait_for_complete_status(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x186150F4, bytes.fromhex("0F A0 0B B8 00 01"), True))
        protocol.ingest(CanFrame(0x186250F4, bytes.fromhex("FF FF 00 01 00"), True))
        overview = protocol.snapshot({"connected": True})["overview"]
        self.assertIsNone(overview["max_cell_mv"])
        self.assertIsNone(overview["max_temp_c"])

        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 0A 50 1F 30"), True))
        protocol.ingest(CanFrame(0x186150F4, bytes.fromhex("0F A0 0B B8 00 01"), True))
        protocol.ingest(CanFrame(0x186250F4, bytes.fromhex("50 3C 00 01 00"), True))
        overview = protocol.snapshot({"connected": True})["overview"]
        self.assertEqual(overview["max_cell_mv"], 4000)
        self.assertEqual(overview["min_temp_c"], 30)

    def test_chroma_ids_are_not_decoded_as_legacy_bms_status(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x401, bytes.fromhex("16 44 27 10 50 00 30 3A"), False))
        overview = protocol.snapshot({"connected": True})["overview"]
        self.assertIsNone(overview["voltage_v"])
        self.assertIsNone(overview["current_a"])

    def test_hv_request_external_event_and_bms_fault_signal_are_separate(self) -> None:
        protocol = BmsProtocol()
        # HV_ACC request released, charge button released, Q0/Q1/Q2 off.
        protocol.ingest(CanFrame(0x186950F4, bytes.fromhex("00 00 00 00 00"), True))
        # fault_code bit22 is the external safety-circuit interruption event;
        # Byte5 bit6 is the independent BMS fault signal Q3 state.
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 40 00 00 40 00 02"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertFalse(snapshot["hv"]["hv_acc"])
        self.assertTrue(snapshot["fault"]["received"])
        self.assertTrue(snapshot["fault"]["flags"]["bms_output_latched"])
        self.assertTrue(snapshot["alarms"][22]["in_fault_code"])
        self.assertEqual(snapshot["alarms"][22]["name"], "外部安全回路中断事件")
        self.assertIsNotNone(snapshot["hv"]["age"])

    def test_imd_flags_gate_measurements_and_decode_digital_state(self) -> None:
        protocol = BmsProtocol()
        # No PB8/PWM/insulation-valid flags: zero payload bytes are placeholders,
        # not real zero measurements.
        protocol.ingest(CanFrame(0x186850F4, bytes.fromhex("20 00 00 00 00 00 00 00"), True))
        imd = protocol.snapshot({"connected": True})["imd"]
        self.assertFalse(imd["digital_ok"])
        self.assertFalse(imd["pwm_signal_ok"])
        self.assertFalse(imd["insulation_valid"])
        self.assertIsNone(imd["duty_pct"])
        self.assertIsNone(imd["frequency_hz"])
        self.assertIsNone(imd["resistance_kohm"])

        # Valid PWM and insulation flags expose the engineering values. 0xFFFF
        # is the saturated resistance sentinel and remains distinguishable.
        protocol.ingest(CanFrame(0x186850F4, bytes.fromhex("01 F0 01 F4 FF FF 03 E8"), True))
        imd = protocol.snapshot({"connected": True})["imd"]
        self.assertTrue(imd["digital_ok"])
        self.assertTrue(imd["pwm_signal_ok"])
        self.assertTrue(imd["insulation_valid"])
        self.assertTrue(imd["insulation_pass"])
        self.assertEqual(imd["duty_pct"], 50.0)
        self.assertEqual(imd["frequency_hz"], 10.0)
        self.assertIsNone(imd["resistance_kohm"])
        self.assertTrue(imd["resistance_saturated"])

    def test_canb_sop_uses_little_endian_and_validates_pair_crc(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x4A0, bytes.fromhex("08 07 50 00 E4 02 50 00"), False))
        protocol.ingest(CanFrame(0x4A3, bytes.fromhex("15 27 05 00 00 FF 60 9C"), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["sop"]["discharge_current_a"], 180.0)
        self.assertEqual(snapshot["sop"]["discharge_power_kw"], 74.0)
        self.assertTrue(snapshot["sop"]["status"]["crc_valid"])
        self.assertTrue(snapshot["sop"]["status"]["ack_fresh"])

    def test_out_of_range_slave_frame_invalidates_complete_frame(self) -> None:
        protocol = BmsProtocol()
        payload = b"\x00\x00" + (3700).to_bytes(2, "little") + (6000).to_bytes(2, "little") + (3702).to_bytes(2, "little")
        protocol.ingest(CanFrame(0x180050F3, payload, True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual([item["value"] for item in snapshot["cells"][:3]], [None, None, None])
        self.assertEqual([item["status"] for item in snapshot["cells"][:3]], ["范围错误"] * 3)
        self.assertEqual(snapshot["modules"][0]["voltage_frames"], 0)

    def test_command_validation_and_encoding(self) -> None:
        frame = build_command("charge_config", {"voltage_v": 570.0, "current_a": 3.0})
        self.assertEqual(frame.arbitration_id, 0x18A050F5)
        self.assertEqual(frame.data, bytes.fromhex("41 00 16 44 00 1E 00 00"))
        frame = build_command("alarm_thresholds", {"ov_mv": 4190, "uv_mv": 3100, "ot_c": 60, "ut_c": 0})
        self.assertEqual(frame.data, bytes.fromhex("42 00 10 5E 0C 1C 5A 1E"))
        frame = build_command("fault_reset")
        self.assertEqual(frame.data, bytes.fromhex("44 00 A5 5A 3C 00 00 00"))
        frame = build_command("log_clear", {"_sequence": 7})
        self.assertEqual(frame.data, bytes.fromhex("4F 07 03 C3 3C A5 00 00"))
        frame = build_command("rtc", {"_sequence": 8, "datetime": "2026-08-03T12:34:56"})
        self.assertEqual(frame.arbitration_id, 0x18A050F5)
        self.assertEqual(frame.data, bytes.fromhex("46 08 1A 08 03 0C 22 38"))
        with self.assertRaises(ValueError):
            build_command("charge_config", {"voltage_v": 600, "current_a": 3})
        with self.assertRaises(ValueError):
            build_command("alarm_thresholds", {"ov_mv": 3100, "uv_mv": 3200, "ot_c": 60, "ut_c": 0})

    def test_simulation_transport_produces_complete_pack(self) -> None:
        service = CanService()
        try:
            result = service.connect({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})
            self.assertTrue(result["ok"])
            deadline = time.monotonic() + 1.2
            snapshot = service.snapshot()
            while time.monotonic() < deadline and snapshot["overview"]["voltage_v"] is None:
                time.sleep(0.03)
                snapshot = service.snapshot()
            self.assertEqual(len(snapshot["cells"]), 138)
            self.assertEqual(len(snapshot["temps"]), 48)
            self.assertTrue(all(module["online"] for module in snapshot["modules"]))
            self.assertEqual(snapshot["overview"]["state"], 3)
            response = service.send_command("alarm_thresholds", {"ov_mv": 4180, "uv_mv": 3110, "ot_c": 59, "ut_c": 1}, True)
            self.assertTrue(response["ok"])
            self.assertEqual(response["ack"]["result"], 1)
            log_result = service.read_flash_fault_logs(50)
            self.assertTrue(log_result["ok"])
            self.assertEqual(log_result["count"], 0)
        finally:
            service.disconnect()

    def test_simulator_charge_phase_sets_charge_mode_and_direct_current(self) -> None:
        frames: list[CanFrame] = []
        simulator = BmsSimulator(frames.append)
        simulator.tick = 120  # Third 30-second phase: high-voltage charging.
        simulator._emit_summary()
        summary = next(frame for frame in frames if frame.arbitration_id == 0x186050F4)
        fault = next(frame for frame in frames if frame.arbitration_id == 0x187650F4)
        self.assertGreater(int.from_bytes(summary.data[2:4], "big", signed=True), 0)
        self.assertEqual(fault.data[5] & 0x04, 0x04)

    def test_fault_code_change_history_is_decoded(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("31 00 00 00 01 00 00 02"), True))
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("32 00 00 00 02 00 00 02"), True))
        history = protocol.snapshot({"connected": True})["fault_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["added"], ["单体欠压"])
        self.assertEqual(history[0]["cleared"], ["单体过压"])

    def test_runtime_identity_sensor_and_command_ack_decode(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x186B50F4, bytes.fromhex("04 D3 16 30 00 1E 04 01"), True))
        protocol.ingest(CanFrame(0x186C50F4, bytes.fromhex("04 81 12 34 56 78 9A BC"), True))
        protocol.ingest(CanFrame(0x186C51F4, bytes.fromhex("04 1A 08 03 00 00 00 00"), True))
        protocol.ingest(CanFrame(0x186D50F4, bytes.fromhex("04 F6 01 00 FF FF FF 9C"), True))
        protocol.ingest(CanFrame(0x18A650F4, bytes.fromhex("04 2A 01 01 03 0F 00 1E"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertTrue(snapshot["config"]["current_direction_inverted"])
        self.assertEqual(snapshot["relay"]["charger_feedback_voltage_v"], 568.0)
        self.assertEqual(snapshot["firmware"]["variant"], "Release")
        self.assertEqual(snapshot["firmware"]["charger_variant"], "Runtime")
        self.assertTrue(snapshot["firmware"]["dirty"])
        self.assertEqual(snapshot["firmware"]["build_date"], "2026-08-03")
        self.assertEqual(snapshot["sensor_diag"]["soc_zero_bias_ma"], -100)
        self.assertIsNotNone(snapshot["runtime_diag"]["age"])
        self.assertTrue(protocol.command_acks[42]["accepted"])
        self.assertTrue(protocol.fault["flags"]["log_clear_pending"])

    def test_identity_decodes_fixed_legacy_charger_variant(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x186C50F4, bytes.fromhex("04 05 12 34 56 78 9A BC"), True))
        firmware = protocol.snapshot({"connected": True})["firmware"]
        self.assertEqual(firmware["variant"], "Release")
        self.assertEqual(firmware["charger_variant"], "Legacy-fixed")

    def test_build_date_rejects_non_leap_year_february_29(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x186C51F4, bytes.fromhex("04 1A 02 1D 00 00 00 00"), True))
        self.assertIsNone(protocol.snapshot({"connected": True})["firmware"]["build_date"])

        protocol.ingest(CanFrame(0x186C51F4, bytes.fromhex("04 1C 02 1D 00 00 00 00"), True))
        self.assertEqual(protocol.snapshot({"connected": True})["firmware"]["build_date"], "2028-02-29")

    def test_switch_catalog_contains_documented_short_names(self) -> None:
        catalog = switch_catalog()
        ov = next(item for item in catalog if item["key"] == "cell_ov")
        self.assertEqual(ov["code"], "ov")
        self.assertEqual(ov["variable"], "ALM_CELL_OV_SWITCH")

    def test_simulator_reports_flash_save_pending_then_clear(self) -> None:
        frames: list[CanFrame] = []
        simulator = BmsSimulator(frames.append)
        simulator.on_command(CanFrame(0x18A050F5, bytes.fromhex("42 01 10 5E 0C 1C 5A 1E"), True))
        ack = next(frame for frame in frames if frame.arbitration_id == 0x18A650F4)
        self.assertTrue(ack.data[5] & 0x02)
        for _ in range(2):
            frames.clear()
            simulator._emit_summary()
            runtime = next(frame for frame in frames if frame.arbitration_id == 0x186B50F4)
            self.assertTrue(runtime.data[1] & 0x20)
        frames.clear()
        simulator._emit_summary()
        runtime = next(frame for frame in frames if frame.arbitration_id == 0x186B50F4)
        self.assertFalse(runtime.data[1] & 0x20)

    def test_command_ack_requires_matching_command_code(self) -> None:
        self.assertFalse(command_ack_matches("charge_config", {"command": 0x02}))
        self.assertTrue(command_ack_matches("charge_config", {"command": 0x01}))

    def test_log_read_is_blocked_while_clear_is_pending(self) -> None:
        service = CanService()
        service.protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 08 00 02"), True))
        result = service.read_flash_fault_logs(50)
        self.assertFalse(result["ok"])
        self.assertIn("正在分阶段清除", result["error"])

    def test_commands_are_blocked_when_total_status_is_stale(self) -> None:
        service = CanService()
        service.connection.update({"connected": True, "mode": "simulation", "bus_profile": "can1"})
        service.protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 00 00 02"), True))
        result = service.send_command("charge_config", {"voltage_v": 570.0, "current_a": 3.0}, True)
        self.assertFalse(result["ok"])
        self.assertIn("总状态帧", result["error"])

    def test_canb_command_rejection_names_bus_before_freshness(self) -> None:
        service = CanService()
        service.connection.update({"connected": True, "mode": "pcan", "bus_profile": "canb"})
        result = service.send_command("charge_config", {"voltage_v": 570.0, "current_a": 3.0}, True)
        self.assertFalse(result["ok"])
        self.assertIn("只在 CAN1", result["error"])
        self.assertNotIn("总状态帧", result["error"])

    def test_flash_log_record_fragments_are_reassembled(self) -> None:
        protocol = BmsProtocol()
        raw = bytes.fromhex("1A 08 02 11 16 21 80 00 00 01 01 04 01 00 93 97")
        protocol.ingest(CanFrame(0x18A650F4, bytes.fromhex("04 09 82 00 03 01 00 03"), True))
        for part in range(4):
            protocol.ingest(CanFrame(0x18A750F4, bytes([4, 9, 2, part])
                                     + raw[part * 4:(part + 1) * 4], True))
        records = protocol.snapshot({"connected": True})["flash_log_records"]
        self.assertEqual(records[0]["index"], 3)
        self.assertEqual(records[0]["timestamp"], "2026-08-02 17:22:33")
        self.assertEqual(records[0]["fault_code"], "0x80000001")
        self.assertEqual(records[0]["event_type"], 1)

    def test_csv_history_replay_and_seek(self) -> None:
        service = CanService()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.csv"
            start = datetime.now()
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["本地时间", "方向", "ID", "帧类型", "DLC", "数据", "说明"])
                writer.writerow([start.isoformat(timespec="milliseconds"), "rx", "0x186050F4", "扩展", 7,
                                 "16 44 00 0A 50 1F 30", "电池总状态"])
                writer.writerow([(start + timedelta(milliseconds=40)).isoformat(timespec="milliseconds"), "rx",
                                 "0x187650F4", "扩展", 8, "30 00 00 00 00 00 00 02", "统一故障状态"])
            try:
                result = service.load_replay(str(path))
                self.assertTrue(result["ok"])
                time.sleep(0.09)
                snapshot = service.snapshot()
                self.assertEqual(snapshot["connection"]["mode"], "replay")
                self.assertEqual(snapshot["overview"]["voltage_v"], 570.0)
                self.assertTrue(snapshot["connection"]["replay"]["paused"])
                self.assertTrue(service.replay_control("seek", 0.0)["ok"])
                self.assertEqual(service.snapshot()["overview"]["state"], 3)
            finally:
                service.disconnect()

    def test_native_bmslog_record_and_replay(self) -> None:
        service = CanService()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charge_session.bmslog"
            try:
                self.assertFalse(service.start_recording(str(path))["ok"])
                self.assertTrue(service.connect({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})["ok"])
                self.assertEqual(service.start_recording(str(path))["format"], "bmslog")
                time.sleep(0.08)
                service.stop_recording()
                self.assertGreater(path.stat().st_size, 0)
                result = service.load_replay(str(path))
                self.assertTrue(result["ok"])
                self.assertGreater(result["frames"], 40)
                self.assertEqual(service.snapshot()["connection"]["bus_profile"], "can1")
                self.assertTrue(service.replay_control("seek", result["duration"])["ok"])
                snapshot = service.snapshot()
                self.assertIsNotNone(snapshot["overview"]["voltage_v"])
                self.assertEqual(snapshot["firmware"]["build_date"], "2026-08-03")
            finally:
                service.disconnect()

    def test_pywebview_api_does_not_expose_native_objects(self) -> None:
        api = Api()
        try:
            public_state = [name for name in vars(api) if not name.startswith("_")]
            self.assertEqual(public_state, [])
        finally:
            api.close()

    def test_source_bootstrap_exposes_simulation_profile(self) -> None:
        api = Api()
        try:
            bootstrap = api.bootstrap()
            self.assertTrue(bootstrap["simulation_enabled"])
            simulation = next(item for item in bootstrap["profiles"] if item["key"] == "simulation")
            self.assertEqual(simulation["mode"], "simulation")
            self.assertIn("开发测试", simulation["name"])
        finally:
            api.close()

    def test_release_transport_rejects_simulation(self) -> None:
        service = CanService(allow_simulation=False)
        try:
            result = service.connect({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})
            self.assertFalse(result["ok"])
            self.assertIn("真实 PCAN", result["error"])
        finally:
            service.disconnect()

    def test_overview_markup_matches_information_hierarchy(self) -> None:
        html = (Path(__file__).parents[1] / "TOOLS" / "bms_host" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="relay-tiles"', html)
        self.assertIn('class="condition-stack"', html)
        self.assertIn('class="data-section thermal-section"', html)
        self.assertIn('class="data-section imd-section"', html)
        self.assertIn('>OK_HS<', html)
        self.assertIn('id="imdFrequency"', html)
        self.assertNotIn('imd-flag-grid', html)
        self.assertIn('id="saveStatus"', html)
        self.assertIn('id="chargeRequestEcho"', html)
        self.assertIn('id="chargeFeedbackEcho"', html)
        self.assertNotIn('id="chargeEcho"', html)
        self.assertIn('class="panel span-6 danger-panel"', html)
        self.assertNotIn('不是 IVT 测量值', html)
        self.assertIn('id="chargeElapsed"', html)
        self.assertIn('id="chargeRemaining"', html)
        self.assertIn('id="overviewFaultCode">等待数据', html)
        self.assertIn('id="faultCode">等待数据', html)
        self.assertIn('id="chargeTimingState">等待数据', html)
        self.assertIn('id="page-bench"', html)
        self.assertIn('id="page-ivt"', html)
        self.assertIn('id="page-fan"', html)
        self.assertIn("台架模拟从控", html)
        self.assertIn("IVT 能量计配置", html)
        self.assertIn("整车风扇", html)
        self.assertNotIn("benchIvtMode", html)
        self.assertNotIn("模拟 IVT", html)
        self.assertNotIn("真实 IVT-S", html)
        for obsolete in ("chargeExpectedAt", "chargeAverageCurrent", "chargeEstimateNote"):
            self.assertNotIn(obsolete, html)

    def test_alarm_detail_reports_whether_level_frame_was_received(self) -> None:
        protocol = BmsProtocol()
        self.assertFalse(protocol.snapshot({"connected": False})["alarms"][0]["received"])
        protocol.ingest(CanFrame(0x187850F4, bytes(8), True))
        self.assertTrue(protocol.snapshot({"connected": True})["alarms"][0]["received"])


class FanControllerToolTest(unittest.TestCase):
    """FanController tool protocol against the sibling repo's Doc/风扇控制.md."""

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
        ]
        for name, values in cases:
            with self.assertRaises(ValueError, msg=f"{name} {values}"):
                build_fan_command(name, values)
        with self.assertRaises(ValueError):
            build_fan_command("fan_unknown")

    def test_status_and_diagnostic_decode(self) -> None:
        protocol = BmsProtocol()
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
        fan_state = protocol.fan_state()
        self.assertLessEqual(fan_state["fan"]["status_age"], 1.0)
        self.assertLessEqual(fan_state["fan"]["diagnostic_age"], 1.0)

    def test_ack_curve_and_failsafe_decode(self) -> None:
        protocol = BmsProtocol()
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
        protocol.ingest(CanFrame(0x5A6, bytes([35, 40, 60, 30, 20, 0, 0, 0]), False))
        self.assertEqual(protocol.fan["curve"], {"temp_off_c": 35, "temp_on_c": 40, "temp_full_c": 60,
                                                 "min_duty_pct": 30, "ramp_up_pct_per_s": 20})
        protocol.ingest(CanFrame(0x5A7, bytes([1, 50, 50, 5, 50, 2, 7, 0]), False))
        self.assertEqual(protocol.fan["failsafe"]["failsafe_name"], "固定保底")
        self.assertEqual(protocol.fan["failsafe"]["fallback1_duty_pct"], 50)
        self.assertEqual(protocol.fan["failsafe"]["stale_hold_s"], 5)
        self.assertEqual(protocol.fan["failsafe"]["mode"], 2)
        self.assertEqual(protocol.fan["failsafe"]["lease_remaining_s"], 7)

    def test_extended_frames_do_not_update_fan_state(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x5A2, bytes(8), True))
        protocol.ingest(CanFrame(0x5A3, bytes(8), True))
        protocol.ingest(CanFrame(0x5A5, bytes(8), True))
        self.assertEqual(protocol.fan["status"], {})
        self.assertEqual(protocol.fan["diagnostic"], {})
        self.assertEqual(protocol.fan_acks, {})
        self.assertIsNone(protocol.last_fan_status_monotonic)

    def test_fan_state_stays_out_of_main_snapshot(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x5A2, bytes([0x0B, 0xB8, 0x0D, 0x48, 0x00, 0x00, 50, 60]), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertNotIn("fan", snapshot)
        self.assertNotIn("fan_ack_history", snapshot)

    def test_send_fan_command_preconditions(self) -> None:
        service = CanService()
        try:
            result = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(result["ok"])
            self.assertIn("尚未连接", result["error"])
            service.connect({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})
            result = service.send_fan_command("fan_query", {}, False)
            self.assertFalse(result["ok"])
            self.assertIn("必须确认", result["error"])
            result = service.send_fan_command("fan_query", {}, True)
            self.assertFalse(result["ok"])
            self.assertIn("只允许使用真实 PCAN", result["error"])
        finally:
            service.disconnect()


if __name__ == "__main__":
    unittest.main()
