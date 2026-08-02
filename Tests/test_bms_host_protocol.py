"""Host-side tests for the Windows BMS CAN application."""

from __future__ import annotations

import time
import csv
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest

from TOOLS.bms_host.can_service import CanService
from TOOLS.bms_host.app import build_project_document, validate_project_document
from TOOLS.bms_host.protocol import BmsProtocol, CanFrame, build_command, command_ack_matches


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
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 27 1A 4E 00 32"), True))
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("72 80 00 00 01 C6 21 02"), True))
        alarm_bytes = bytearray(8)
        alarm_bytes[0] = 0b00001001  # index 0=1, index 1=2
        protocol.ingest(CanFrame(0x187850F4, bytes(alarm_bytes), True))
        protocol.ingest(CanFrame(0x187750F4, bytes.fromhex("10 5E 0C 1C 5A 1E"), True))
        protocol.ingest(CanFrame(0x187F50F4, bytes.fromhex("F3 3F E0 03"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["overview"]["voltage_v"], 570.0)
        self.assertEqual(snapshot["overview"]["current_a"], 1.0)
        self.assertEqual(snapshot["overview"]["state"], 7)
        self.assertEqual(snapshot["fault"]["code_hex"], "0x80000001")
        self.assertTrue(snapshot["fault"]["slave_offline"][0])
        self.assertTrue(snapshot["fault"]["slave_offline"][5])
        self.assertEqual(snapshot["alarms"][0]["level"], 1)
        self.assertEqual(snapshot["alarms"][1]["level"], 2)
        self.assertEqual(snapshot["config"]["thresholds"]["ov_mv"], 4190)
        self.assertEqual(snapshot["config"]["thresholds"]["ot_c"], 60)
        self.assertEqual(snapshot["config"]["switch_version"], 3)
        self.assertTrue(snapshot["config"]["switches"]["ivt_voltage_loss"])

    def test_charge_mode_summary_uses_direct_current_without_offset(self) -> None:
        protocol = BmsProtocol()
        protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 04 00 02"), True))
        protocol.ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 1E 50 00 30"), True))
        self.assertEqual(protocol.snapshot({"connected": True})["overview"]["current_a"], 3.0)

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
        self.assertEqual(frame.arbitration_id, 0x188050F5)
        self.assertEqual(frame.data, bytes.fromhex("03 00 16 44 00 1E"))
        frame = build_command("alarm_thresholds", {"ov_mv": 4190, "uv_mv": 3100, "ot_c": 60, "ut_c": 0})
        self.assertEqual(frame.data, bytes.fromhex("03 00 10 5E 0C 1C 5A 1E"))
        frame = build_command("fault_reset")
        self.assertEqual(frame.data, bytes.fromhex("03 00 A5 5A 3C C3"))
        frame = build_command("log_clear", {"_sequence": 7})
        self.assertEqual(frame.data, bytes.fromhex("03 07 03 C3 3C A5"))
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
        protocol.ingest(CanFrame(0x186B50F4, bytes.fromhex("03 D3 16 30 00 1E 04 01"), True))
        protocol.ingest(CanFrame(0x186C50F4, bytes.fromhex("03 81 12 34 56 78 9A BC"), True))
        protocol.ingest(CanFrame(0x186D50F4, bytes.fromhex("03 F6 01 00 FF FF FF 9C"), True))
        protocol.ingest(CanFrame(0x18A650F4, bytes.fromhex("03 2A 01 01 03 0F 00 1E"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertTrue(snapshot["config"]["current_direction_inverted"])
        self.assertEqual(snapshot["relay"]["charger_feedback_voltage_v"], 568.0)
        self.assertEqual(snapshot["firmware"]["variant"], "Release")
        self.assertTrue(snapshot["firmware"]["dirty"])
        self.assertEqual(snapshot["sensor_diag"]["soc_zero_bias_ma"], -100)
        self.assertTrue(protocol.command_acks[42]["accepted"])
        self.assertTrue(protocol.fault["flags"]["log_clear_pending"])

    def test_command_ack_requires_matching_command_code(self) -> None:
        self.assertFalse(command_ack_matches("charge_config", {"command": 0x02}))
        self.assertTrue(command_ack_matches("charge_config", {"command": 0x01}))

    def test_log_read_is_blocked_while_clear_is_pending(self) -> None:
        service = CanService()
        service.protocol.ingest(CanFrame(0x187650F4, bytes.fromhex("30 00 00 00 00 08 00 02"), True))
        result = service.read_flash_fault_logs(50)
        self.assertFalse(result["ok"])
        self.assertIn("正在分阶段清除", result["error"])

    def test_flash_log_record_fragments_are_reassembled(self) -> None:
        protocol = BmsProtocol()
        raw = bytes.fromhex("1A 08 02 11 16 21 80 00 00 01 01 04 01 00 93 97")
        protocol.ingest(CanFrame(0x18A650F4, bytes.fromhex("03 09 82 00 03 01 00 03"), True))
        for part in range(4):
            protocol.ingest(CanFrame(0x18A750F4, bytes([3, 9, 2, part])
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
                                 "16 44 27 10 50 00 30", "电池总状态"])
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
                self.assertIsNotNone(service.snapshot()["overview"]["voltage_v"])
            finally:
                service.disconnect()

    def test_project_document_round_trip_validation(self) -> None:
        document = build_project_document({
            "name": "26E charge", "notes": "bench",
            "connection": {"bus_profile": "can1", "channel": "PCAN_USBBUS1"},
            "parameters": {"thresholds": {"ov_mv": 4190}},
            "view": {"cell_mode": "voltage"},
        })
        self.assertIs(validate_project_document(document), document)
        self.assertEqual(document["schema_version"], 1)
        with self.assertRaises(ValueError):
            validate_project_document({"format": "BITFSAE_BMS_PROJECT", "schema_version": 2})


if __name__ == "__main__":
    unittest.main()
