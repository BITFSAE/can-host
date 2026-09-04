"""CAN monitor aggregation and raw-transmit safety tests."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from canhost.app import Api
from canhost.decoders import CanFrame
from canhost.monitor import CanMonitor, normalize_message_spec
from canhost.transport import CanService


class MonitorAggregationTest(unittest.TestCase):
    def test_same_id_is_grouped_with_cycle_count_and_payload_variants(self) -> None:
        monitor = CanMonitor(lambda can_id, extended: "测试帧")
        monitor.ingest(CanFrame(0x290, bytes.fromhex("01 02"), False, 100.000, "rx"))
        monitor.ingest(CanFrame(0x290, bytes.fromhex("01 03"), False, 100.200, "rx"))
        monitor.ingest(CanFrame(0x290, bytes.fromhex("01 03"), False, 100.400, "rx"))
        group = monitor.snapshot()["groups"][0]
        self.assertEqual(group["id"], "0x290")
        self.assertEqual(group["count"], 3)
        self.assertEqual(group["rx_count"], 3)
        self.assertEqual(group["cycle_ms"], 200.0)
        self.assertEqual(group["variant_count"], 2)
        self.assertEqual(group["changed"], [])
        self.assertEqual(group["data"], "01 03")
        self.assertEqual(group["name"], "测试帧")

    def test_rx_and_tx_share_one_id_group_but_keep_direction_counts(self) -> None:
        monitor = CanMonitor(lambda can_id, extended: "共享 ID")
        monitor.ingest(CanFrame(0x321, b"\x10", False, 200.0, "rx"))
        monitor.ingest(CanFrame(0x321, b"\x20", False, 200.1, "tx"))
        group = monitor.snapshot()["groups"][0]
        self.assertEqual(group["direction"], "both")
        self.assertEqual((group["rx_count"], group["tx_count"]), (1, 1))


class MonitorFrameValidationTest(unittest.TestCase):
    def test_frame_validation_accepts_compact_data_and_rejects_can_fd(self) -> None:
        row = normalize_message_spec({"id": "0x123", "data": "0102A0", "cycle_ms": 20})
        self.assertEqual(row["data_bytes"], [1, 2, 160])
        with self.assertRaisesRegex(ValueError, "8 字节"):
            normalize_message_spec({"id": "0x123", "data": "00 " * 9, "cycle_ms": 20})

    def test_monitor_page_keeps_connection_roles_and_python_out_of_its_copy(self) -> None:
        html = (Path(__file__).parents[1] / "canhost" / "web" / "index.html").read_text(encoding="utf-8")
        section = html.split('id="page-frames"', 1)[1].split('id="page-telemetry"', 1)[0]
        self.assertNotIn("CAN1", section)
        self.assertNotIn("CANB", section)
        self.assertNotIn("PY", section)
        self.assertNotIn("monitor-status", section)


class MonitorTransportTest(unittest.TestCase):
    def make_writable_service(self) -> CanService:
        service = CanService()
        service.bus = MagicMock()
        service.connection.update({
            "connected": True, "mode": "pcan", "bus_profile": "can1",
            "channel": "PCAN_USBBUS1", "bitrate": 500000,
        })
        return service

    def test_raw_send_uses_selected_pcan_and_blocks_project_command_ids(self) -> None:
        service = self.make_writable_service()
        try:
            protected = (
                {"id": "0x18A050F5", "extended": True},
                {"id": "0x410"}, {"id": "0x411"},
                {"id": "0x5A4"}, {"id": "0x5AB"},
            )
            for spec in protected:
                with self.subTest(spec=spec):
                    denied = service.send_monitor_frame(
                        {**spec, "data": "01", "cycle_ms": 200}, True)
                    self.assertFalse(denied["ok"])
                    self.assertIn("受保护", denied["error"])
            self.assertFalse(service.send_monitor_frame(
                {"id": "0x290", "data": "01", "cycle_ms": 200}, False)["ok"])

            sent = service.send_monitor_frame(
                {"id": "0x290", "data": "01 02", "cycle_ms": 200}, True)
            self.assertTrue(sent["ok"])
            service.bus.send.assert_called_once()
            group = service.snapshot()["monitor"]["groups"][0]
            self.assertEqual(group["tx_count"], 1)

            service.connection["bus_profile"] = "canb"
            on_other_bus = service.send_monitor_frame(
                {"id": "0x290", "data": "01", "cycle_ms": 200}, True)
            self.assertTrue(on_other_bus["ok"])
            protected_fan = service.send_monitor_frame(
                {"id": "0x5A4", "data": "01", "cycle_ms": 200}, True)
            self.assertFalse(protected_fan["ok"])
            self.assertIn("受保护", protected_fan["error"])
        finally:
            # Prevent shutdown() assertions from depending on MagicMock state.
            service.disconnect()

    def test_periodic_send_starts_and_stops_by_stable_row_id(self) -> None:
        service = self.make_writable_service()
        try:
            spec = {"id": "0x290", "data": "AA", "cycle_ms": 20}
            result = service.configure_monitor_periodic("saved-row", spec, True, True)
            self.assertTrue(result["ok"])
            deadline = time.monotonic() + 0.3
            while service.bus.send.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(service.bus.send.call_count, 0)
            self.assertTrue(service.configure_monitor_periodic("saved-row", spec, False, True)["ok"])
        finally:
            service.disconnect()

    def test_active_native_trace_exports_to_csv_without_stopping_capture(self) -> None:
        service = CanService()
        service.connection.update({"connected": True, "mode": "pcan", "bus_profile": "can1"})
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "auto.bmslog"
            csv_path = Path(directory) / "export.csv"
            try:
                self.assertTrue(service.start_recording(str(log_path), auto=True)["ok"])
                service._ingest(CanFrame(0x290, b"\x01\x02", False, time.time(), "rx"))
                result = service.export_recording_csv(str(csv_path))
                self.assertTrue(result["ok"])
                self.assertTrue(service.record_auto)
                self.assertIn("0x290", csv_path.read_text(encoding="utf-8-sig"))
            finally:
                service.disconnect()


class MonitorApiTest(unittest.TestCase):
    def test_successful_real_connection_starts_separate_native_auto_trace(self) -> None:
        api = Api.__new__(Api)
        service = MagicMock()
        service.connection = {
            "bus_profile": "canb", "channel": "PCAN_USBBUS2", "bitrate": 500000,
        }
        service.start_recording.return_value = {
            "ok": True, "path": "record.bmslog", "format": "bmslog", "auto": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            with patch("canhost.app.settings_path", return_value=settings):
                result = api._start_auto_trace(
                    service, {"mode": "pcan", "auto_record": True}, {"ok": True}, "CONN2")
        self.assertTrue(result["recording"]["ok"])
        path = Path(service.start_recording.call_args.args[0])
        self.assertEqual(path.parent.name, "traces")
        self.assertTrue(path.name.startswith("CONN2_CANB_PCAN_USBBUS2_"))
        self.assertEqual(path.suffix, ".bmslog")
        self.assertTrue(service.start_recording.call_args.kwargs["auto"])

    def test_simulation_or_disabled_preference_does_not_auto_record(self) -> None:
        api = Api.__new__(Api)
        service = MagicMock()
        result = {"ok": True}
        self.assertIs(api._start_auto_trace(
            service, {"mode": "simulation"}, result, "CONN1"), result)
        self.assertIs(api._start_auto_trace(
            service, {"mode": "pcan", "auto_record": False}, result, "CONN1"), result)
        service.start_recording.assert_not_called()


if __name__ == "__main__":
    unittest.main()
