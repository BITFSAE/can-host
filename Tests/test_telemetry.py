"""MQTT telemetry payload and fault-monitor state tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from canhost.telemetry import fsae_telemetry_pb2 as telemetry_pb
from canhost.telemetry.protocol import decode_telemetry_payload
from canhost.telemetry.service import TelemetryService


def telemetry_payload(
    code: int, *, legacy_code: int | None = None, include_bms_state: bool = True,
) -> bytes:
    frame = telemetry_pb.TelemetryFrame(
        timestamp_ms=123456,
        frame_id=42,
        hv_voltage=548.5,
        hv_current=-12.25,
        battery_soc=78,
        fault_code=code if legacy_code is None else legacy_code,
        battery_fault_code=code,
    )
    frame.header.timestamp_ms = 123456
    frame.header.seq = 77
    frame.header.source_id = 1
    if include_bms_state:
        frame.bms_telemetry.battery_state = 7
        frame.bms_telemetry.battery_alarm_level = 1
    alarm = frame.alarms.add()
    alarm.alarm_id = 0x186050F4
    alarm.severity = telemetry_pb.ALARM_SEVERITY_ERROR
    alarm.message = "BMS summary alarm level 1"
    return frame.SerializeToString()


class TelemetryProtocolTest(unittest.TestCase):
    def test_fault_word_and_alarm_are_decoded(self) -> None:
        decoded = decode_telemetry_payload(telemetry_payload((1 << 0) | (1 << 22) | (1 << 31)))
        self.assertEqual(decoded["fault"]["code_hex"], "0x80400001")
        self.assertEqual(
            decoded["fault"]["active"],
            [
                {"bit": 0, "name": "单体过压"},
                {"bit": 22, "name": "外部安全回路中断事件"},
                {"bit": 31, "name": "IVT 包电压通道失联"},
            ],
        )
        self.assertEqual(decoded["bms"]["state_name"], "故障保持")
        self.assertEqual(decoded["bms"]["alarm_level_name"], "一级故障")
        self.assertTrue(decoded["fault"]["valid"])
        self.assertEqual(decoded["header"]["sequence"], 77)
        self.assertEqual(decoded["alarms"][0]["severity_name"], "错误")
        self.assertEqual(decoded["summary"]["soc_pct"], 78)

    def test_dedicated_battery_fault_word_wins_and_reports_mismatch(self) -> None:
        decoded = decode_telemetry_payload(telemetry_payload(2, legacy_code=1))
        self.assertEqual(decoded["fault"]["code"], 2)
        self.assertEqual(decoded["fault"]["source"], "battery_fault_code")
        self.assertTrue(decoded["fault"]["sources_mismatch"])

    def test_legacy_fault_word_is_accepted_when_battery_field_is_zero(self) -> None:
        decoded = decode_telemetry_payload(telemetry_payload(0, legacy_code=4))
        self.assertEqual(decoded["fault"]["code"], 4)
        self.assertEqual(decoded["fault"]["source"], "fault_code（兼容）")
        self.assertFalse(decoded["fault"]["sources_mismatch"])

    def test_zero_fault_without_fresh_bms_presence_is_unknown(self) -> None:
        decoded = decode_telemetry_payload(telemetry_payload(0, include_bms_state=False))
        self.assertFalse(decoded["fault"]["valid"])
        self.assertFalse(decoded["bms"]["valid"])

    def test_corrupt_protobuf_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Protobuf 解码失败"):
            decode_telemetry_payload(b"\xff")
        with self.assertRaisesRegex(ValueError, "为空"):
            decode_telemetry_payload(b"")


class TelemetryServiceTest(unittest.TestCase):
    @patch("paho.mqtt.client.Client")
    def test_connect_subscribes_read_only_and_does_not_expose_password(self, client_factory: MagicMock) -> None:
        client = client_factory.return_value
        client.subscribe.return_value = (0, 9)
        service = TelemetryService()
        result = service.connect({
            "host": "broker.example",
            "port": 1883,
            "topic": "fsae/telemetry",
            "username": "viewer",
            "password": "secret-value",
        })
        self.assertTrue(result["ok"])
        client.username_pw_set.assert_called_once_with("viewer", "secret-value")
        client.connect_async.assert_called_once_with("broker.example", 1883, keepalive=30)
        client.loop_start.assert_called_once_with()
        self.assertFalse(hasattr(client, "publish") and client.publish.called)

        client.on_connect(client, None, None, 0, None)
        client.subscribe.assert_called_once_with("fsae/telemetry", qos=0)
        snapshot = service.snapshot()
        self.assertTrue(snapshot["connection"]["connected"])
        self.assertEqual(snapshot["connection"]["username"], "viewer")
        self.assertNotIn("secret-value", repr(snapshot))

        service.disconnect()
        client.disconnect.assert_called_once_with()
        client.loop_stop.assert_called_once_with()

    def test_fault_changes_build_bounded_session_history(self) -> None:
        monotonic = [10.0]
        wall = [1_800_000_000.0]
        service = TelemetryService(clock=lambda: monotonic[0], wall_clock=lambda: wall[0])
        service._generation = 1

        service._on_message(1, "fsae/telemetry", telemetry_payload(1))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["rx_count"], 1)
        self.assertEqual(snapshot["fault_history"][0]["added"], ["单体过压"])
        self.assertEqual(snapshot["last_message_age"], 0.0)

        monotonic[0] = 11.5
        wall[0] += 1.5
        service._on_message(1, "fsae/telemetry", telemetry_payload(4))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["fault_history"][0]["added"], ["单体过温"])
        self.assertEqual(snapshot["fault_history"][0]["cleared"], ["单体过压"])
        self.assertEqual(snapshot["rx_count"], 2)

        monotonic[0] = 15.0
        self.assertEqual(service.snapshot()["last_message_age"], 3.5)

    def test_parse_errors_do_not_replace_last_valid_frame(self) -> None:
        service = TelemetryService()
        service._generation = 1
        service._on_message(1, "fsae/telemetry", telemetry_payload(1))
        service._on_message(1, "fsae/telemetry", b"\xff")
        snapshot = service.snapshot()
        self.assertEqual(snapshot["rx_count"], 1)
        self.assertEqual(snapshot["parse_error_count"], 1)
        self.assertEqual(snapshot["latest"]["fault"]["code"], 1)

    def test_invalid_zero_does_not_create_a_false_fault_clear(self) -> None:
        service = TelemetryService()
        service._generation = 1
        service._on_message(1, "fsae/telemetry", telemetry_payload(1))
        service._on_message(
            1, "fsae/telemetry", telemetry_payload(0, include_bms_state=False))
        snapshot = service.snapshot()
        self.assertFalse(snapshot["latest"]["fault"]["valid"])
        self.assertEqual(len(snapshot["fault_history"]), 1)
        self.assertEqual(snapshot["fault_history"][0]["code"], "0x00000001")

        service._on_message(1, "fsae/telemetry", telemetry_payload(0))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["fault_history"][0]["cleared"], ["单体过压"])

    def test_connection_configuration_rejects_wildcards_and_partial_credentials(self) -> None:
        service = TelemetryService()
        self.assertFalse(service.connect({"topic": "fsae/#"})["ok"])
        result = service.connect({"username": "telegraf", "password": ""})
        self.assertFalse(result["ok"])
        self.assertIn("同时填写", result["error"])


if __name__ == "__main__":
    unittest.main()
