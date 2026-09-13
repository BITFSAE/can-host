"""Local telemetry simulator generation, validation and lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from canhost.telemetry.protocol import decode_telemetry_payload
from canhost.telemetry.simulator import (
    TelemetryFrameGenerator,
    TelemetrySimulatorService,
    build_serial_packet,
    normalize_simulator_config,
)


class TelemetryFrameGeneratorTest(unittest.TestCase):
    def test_generates_full_vehicle_frame_and_bms_detail_at_two_hz(self) -> None:
        now = [100.0]
        generator = TelemetryFrameGenerator(clock=lambda: now[0])
        frames = []
        for _ in range(5):
            now[0] += 0.1
            frames.append(generator.generate_frame())

        frame = frames[-1]
        self.assertEqual(frame.header.seq, 5)
        self.assertEqual(frame.header.timestamp_ms, 499)
        self.assertEqual(len(frame.vehicle_state.motors), 4)
        self.assertEqual(len(frame.thermal_summary.sensors), 4)
        self.assertEqual(len(frame.modules), 6)
        self.assertEqual(len(frames[0].modules), 0)
        self.assertAlmostEqual(frame.pdm_telemetry.bus_voltage_v, 13.8, places=1)
        self.assertEqual(frame.fan_telemetry.fan3_rpm, 5300)

        decoded = decode_telemetry_payload(frame.SerializeToString())
        self.assertEqual(decoded["header"]["sequence"], 5)
        self.assertTrue(decoded["fault"]["valid"])

    def test_serial_packet_preserves_payload_and_optional_suffix(self) -> None:
        frame = TelemetryFrameGenerator().generate_frame()
        payload = frame.SerializeToString()
        self.assertEqual(build_serial_packet(frame), payload)
        self.assertEqual(build_serial_packet(frame, "0D0A"), payload + b"\r\n")


class TelemetrySimulatorConfigTest(unittest.TestCase):
    def test_requires_output_and_validates_output_specific_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少选择"):
            normalize_simulator_config({})
        with self.assertRaisesRegex(ValueError, "用户名和密码"):
            normalize_simulator_config({"mqtt": True, "mqtt_username": "vehicle_gateway"})
        with self.assertRaisesRegex(ValueError, "选择串口"):
            normalize_simulator_config({"serial": True})
        with self.assertRaisesRegex(ValueError, "包尾"):
            normalize_simulator_config({"serial": True, "serial_port": "loop", "serial_suffix_hex": "0"})

        config = normalize_simulator_config({
            "mqtt": True, "serial": True, "pcan": True,
            "mqtt_username": "vehicle_gateway", "mqtt_password": "secret",
            "serial_port": "/dev/cu.test", "serial_suffix_hex": "0D 0A",
        })
        self.assertEqual(config["mqtt_topic"], "fsae/telemetry")
        self.assertEqual(config["serial_suffix_hex"], "0D0A")
        self.assertEqual(config["pcan_bitrate"], 500000)

    def test_serial_service_runs_and_never_exposes_password(self) -> None:
        class FakeSerial:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.writes = []
                self.closed = False
                self.instances.append(self)

            def write(self, payload):
                self.writes.append(bytes(payload))
                return len(payload)

            def flush(self):
                pass

            def close(self):
                self.closed = True

        fake_module = SimpleNamespace(Serial=FakeSerial)
        service = TelemetrySimulatorService()
        with patch.dict(sys.modules, {"serial": fake_module}):
            result = service.start({
                "serial": True, "serial_port": "/dev/cu.fake",
                "serial_suffix_hex": "0A", "mqtt_password": "must-not-leak",
            })
            self.assertTrue(result["ok"])
            deadline = time.monotonic() + 1.0
            while service.snapshot()["serial_frames"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot = service.snapshot()
            self.assertGreaterEqual(snapshot["serial_frames"], 2)
            self.assertEqual(snapshot["serial_frames"], snapshot["protobuf_frames"])
            self.assertNotIn("must-not-leak", repr(snapshot))
            self.assertTrue(service.stop()["ok"])
            stopped_age = service.snapshot()["run_age"]
            time.sleep(0.02)
            self.assertEqual(service.snapshot()["run_age"], stopped_age)

        self.assertTrue(FakeSerial.instances[0].closed)
        self.assertTrue(all(payload.endswith(b"\n") for payload in FakeSerial.instances[0].writes))

    @patch("can.Bus")
    @patch("paho.mqtt.client.Client")
    def test_mqtt_and_pcan_outputs_run_together(self, mqtt_factory: MagicMock,
                                               bus_factory: MagicMock) -> None:
        mqtt_client = mqtt_factory.return_value
        mqtt_client.publish.return_value = SimpleNamespace(rc=0)
        mqtt_client.loop_start.side_effect = lambda: mqtt_client.on_connect(
            mqtt_client, None, None, 0, None)
        pcan_bus = bus_factory.return_value
        service = TelemetrySimulatorService()
        result = service.start({
            "mqtt": True, "pcan": True,
            "mqtt_host": "broker.example", "mqtt_topic": "fsae/telemetry",
            "pcan_channel": "PCAN_USBBUS2", "pcan_bitrate": 500000,
        })
        self.assertTrue(result["ok"])
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            snapshot = service.snapshot()
            if snapshot["mqtt_frames"] >= 2 and snapshot["pcan_frames"] > 0:
                break
            time.sleep(0.01)
        snapshot = service.snapshot()
        self.assertGreaterEqual(snapshot["mqtt_frames"], 2)
        self.assertGreater(snapshot["pcan_frames"], 0)
        self.assertTrue(service.stop()["ok"])

        mqtt_client.connect.assert_called_once_with("broker.example", 1883, 30)
        mqtt_client.publish.assert_called()
        self.assertEqual(mqtt_client.publish.call_args.args[0], "fsae/telemetry")
        mqtt_client.disconnect.assert_called_once_with()
        pcan_bus.send.assert_called()
        pcan_bus.shutdown.assert_called_once_with()

    @patch("canhost.telemetry.simulator.trust.https_ssl_context")
    @patch("paho.mqtt.client.Client")
    def test_mqtt_tls_uses_bundled_roots_and_rejection_stops_run(
        self, mqtt_factory: MagicMock, context_factory: MagicMock,
    ) -> None:
        mqtt_client = mqtt_factory.return_value
        mqtt_client.loop_start.side_effect = lambda: mqtt_client.on_connect(
            mqtt_client, None, None, 5, None)
        service = TelemetrySimulatorService()

        result = service.start({"mqtt": True, "mqtt_host": "broker.example", "mqtt_tls": True})
        self.assertTrue(result["ok"])
        deadline = time.monotonic() + 1.0
        while service.snapshot()["state"] != "error" and time.monotonic() < deadline:
            time.sleep(0.01)

        snapshot = service.snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("连接被拒绝", snapshot["error"])
        mqtt_client.tls_set_context.assert_called_once_with(context_factory.return_value)
        self.assertFalse(mqtt_client.publish.called)


if __name__ == "__main__":
    unittest.main()
