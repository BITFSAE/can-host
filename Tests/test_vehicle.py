"""Vehicle CANB protocol state, simulator, and service-level tests."""

from __future__ import annotations

import time
import unittest

from canhost.decoders import CanFrame
from canhost.transport import CanService
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.simulator import VehicleSimulator


class VehicleProtocolTest(unittest.TestCase):
    def test_pack_and_fault_strip(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 50"), False))
        protocol.ingest(CanFrame(0x4B1, bytes.fromhex("50 00 00 00 00 00 00 04"), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["pack"]["voltage_v"], 570.0)
        self.assertEqual(snapshot["pack"]["current_a"], 1.0)
        self.assertEqual(snapshot["pack"]["soc_pct"], 78)
        self.assertEqual(snapshot["pack"]["state_name"], "HV_ON")
        self.assertTrue(snapshot["fault"]["received"])
        self.assertEqual(snapshot["fault"]["code_hex"], "0x00000000")

    def test_sop_trio_states(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x4A0, bytes.fromhex("08 07 50 00 E4 02 50 00"), False))
        protocol.ingest(CanFrame(0x4A3, bytes.fromhex("15 27 05 00 00 FF 60 9C"), False))
        protocol.ingest(CanFrame(0x4A4, bytes([0x11, 0x07, 0x46, 0xE8, 0x03, 0x82, 0x01, 0x00]), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["sop"]["limits"]["discharge_power_kw"], 74.0)
        self.assertTrue(snapshot["sop"]["status"]["crc_valid"])
        self.assertFalse(snapshot["sop"]["ecu_ack"]["crc_valid"])

    def test_ivt_channels_track_freshness_independently(self) -> None:
        clock = [0.0]
        protocol = VehicleProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x512, bytes([0x00, 0x01]) + (15000).to_bytes(4, "little", signed=True) + b"\x00\x00", False))
        clock[0] = 3.0
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["ivt"]["current_a"]["value"], 15.0)
        self.assertEqual(snapshot["ivt"]["current_a"]["age"], 3.0)
        self.assertIsNone(snapshot["ivt"]["u1_v"]["age"])

    def test_ivt_mux_mismatch_is_ignored(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x513, bytes([0x02, 0x00]) + (40000).to_bytes(4, "little", signed=True) + b"\x00\x00", False))
        self.assertIsNone(protocol.ivt["u1_v"]["value"])

    def test_meter_channels(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x521, bytes([0x00, 0x02]) + (12345).to_bytes(4, "big", signed=True) + b"\x00\x00", False))
        protocol.ingest(CanFrame(0x522, bytes([0x01, 0x02]) + (548000).to_bytes(4, "big", signed=True) + b"\x00\x00", False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["meter"]["current_a"]["value"], 12.345)
        self.assertEqual(snapshot["meter"]["u1_v"]["value"], 548.0)
        self.assertIsNone(snapshot["meter"]["power_w"]["value"])

    def test_pdm_sides_and_offline(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A0, bytes.fromhex("5D C0 00 96 01 68 00 7B"), False))
        protocol.ingest(CanFrame(0x5A1, bytes.fromhex("7F FF 7F FF FF FF FF FF"), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["pdm"]["bus"]["voltage_v"], 24.0)
        self.assertFalse(snapshot["pdm"]["bus"]["offline"])
        self.assertTrue(snapshot["pdm"]["battery"]["offline"])
        self.assertIsNone(snapshot["pdm"]["battery"]["current_a"])

    def test_ecu_block_and_tire_frames(self) -> None:
        protocol = VehicleProtocol()
        payload = b"".join(value.to_bytes(2, "little", signed=True) for value in (100, 200, 300, 400))
        protocol.ingest(CanFrame(0x502, payload, False))
        protocol.ingest(CanFrame(0x505, payload, False))
        protocol.ingest(CanFrame(0x506, payload, False))
        protocol.ingest(CanFrame(0x509, bytes([0x25, 0x0D, 0xF0, 0x44, 0x33]), False))
        protocol.ingest(CanFrame(0x071, bytes([30, 0x55, 40, 0x00, 48, 0x01, 60, 0x63]), False))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["ecu"]["torque_pct"], [30.0, 40.0, 10.0, 20.0])
        self.assertEqual(snapshot["ecu"]["velocity_rpm"], [300, 400, 100, 200])
        self.assertEqual(snapshot["ecu"]["motor_temp_c"], [30.0, 40.0, 10.0, 20.0])
        self.assertTrue(snapshot["ecu"]["status"]["enable"]["FR"])
        self.assertEqual(snapshot["tires"]["0x071"], [30.85, 40.0, 48.01, 60.99])
        self.assertEqual(snapshot["ecu"]["wheel_names"], ["FL", "FR", "RL", "RR"])

    def test_extended_frames_are_ignored_entirely(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 50"), True))
        snapshot = protocol.snapshot({"connected": True})
        self.assertEqual(snapshot["pack"], {"age": None})

    def test_quick_values(self) -> None:
        protocol = VehicleProtocol()
        protocol.ingest(CanFrame(0x5A0, bytes.fromhex("5D C0 00 96 01 68 00 7B"), False))
        protocol.ingest(CanFrame(0x4A0, bytes.fromhex("08 07 50 00 E4 02 50 00"), False))
        protocol.ingest(CanFrame(0x5A2, bytes([0x0B, 0xB8, 0x0D, 0x48, 0x00, 0x00, 50, 60]), False))
        quick = protocol.quick_values()
        self.assertEqual(quick["pdm"]["bus_voltage_v"], 24.0)
        self.assertEqual(quick["sop"]["discharge_power_kw"], 74.0)
        self.assertEqual(quick["fan_rpm_max"], 3400)
        self.assertFalse(quick["pack"]["voltage_valid"])

    def test_vehicle_trend_appends_when_fresh(self) -> None:
        clock = [0.0]
        protocol = VehicleProtocol(clock=lambda: clock[0])
        protocol.ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 50"), False))
        clock[0] = 0.5
        protocol.ingest(CanFrame(0x5A0, bytes.fromhex("5D C0 00 96 01 68 00 7B"), False))
        trends = protocol.snapshot({"connected": True})["trends"]
        self.assertEqual(trends[-1]["hv_voltage"], 570.0)
        self.assertEqual(trends[-1]["lv_voltage"], 24.0)


class VehicleSimulatorTest(unittest.TestCase):
    def test_emitters_produce_decodable_frames(self) -> None:
        frames: list[CanFrame] = []
        simulator = VehicleSimulator(frames.append)
        simulator._emit_pack_and_fault()
        simulator._emit_sop()
        simulator._emit_ivt_and_meter()
        simulator._emit_pdm()
        simulator._emit_fan()
        simulator._emit_ecu()
        simulator._emit_tires()
        ids = {frame.arbitration_id for frame in frames}
        for expected in (0x4B0, 0x4B1, 0x4A0, 0x4A3, 0x512, 0x513, 0x517,
                         0x521, 0x522, 0x5A0, 0x5A1, 0x5A2, 0x5A3, 0x5A6, 0x5A7,
                         0x502, 0x505, 0x506, 0x507, 0x508, 0x509,
                         0x071, 0x072, 0x073, 0x074):
            self.assertIn(expected, ids)
        # The power/energy meter frames are intentionally absent, mirroring
        # the real competition meter that may not send them.
        self.assertNotIn(0x526, ids)
        self.assertNotIn(0x528, ids)

        protocol = VehicleProtocol()
        for frame in frames:
            protocol.ingest(frame)
        snapshot = protocol.snapshot({"connected": True})
        self.assertTrue(snapshot["sop"]["status"]["crc_valid"])
        self.assertTrue(snapshot["pdm"]["bus"]["voltage_v"] > 20)
        self.assertIsNotNone(snapshot["pack"]["voltage_v"])
        self.assertTrue(snapshot["fan"]["status"]["rpm"][0] > 0)

    def test_simulation_transport_produces_vehicle_snapshot(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            result = service.connect({"mode": "simulation", "bus_profile": "canb", "bitrate": 500000})
            self.assertTrue(result["ok"])
            deadline = time.monotonic() + 2.0
            snapshot = service.vehicle_snapshot()
            while time.monotonic() < deadline and (snapshot["pack"].get("voltage_v") is None
                                                   or not snapshot["fan"]["status"]):
                time.sleep(0.05)
                snapshot = service.vehicle_snapshot()
            self.assertIsNotNone(snapshot["pack"]["voltage_v"])
            self.assertIsNotNone(snapshot["pdm"]["bus"]["voltage_v"])
            self.assertIsNotNone(snapshot["sop"]["limits"]["discharge_current_a"])
            self.assertTrue(snapshot["fan"]["status"])
            quick = service.quick_snapshot()
            self.assertEqual(quick["connection"]["bus_profile"], "canb")
            self.assertIsNotNone(quick["pdm"]["bus_voltage_v"])
        finally:
            service.disconnect()

    def test_vehicle_simulation_rejects_can1(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            result = service.connect({"mode": "simulation", "bus_profile": "can1", "bitrate": 500000})
            self.assertFalse(result["ok"])
            self.assertIn("CANB", result["error"])
        finally:
            service.disconnect()

    def test_vehicle_service_rejects_bms_commands_and_replay(self) -> None:
        service = CanService(protocol_kind="vehicle")
        try:
            result = service.send_command("charge_config", {"voltage_v": 570.0, "current_a": 3.0}, True)
            self.assertFalse(result["ok"])
            self.assertIn("F405 工具命令", result["error"])
            result = service.read_flash_fault_logs(50)
            self.assertFalse(result["ok"])
            result = service.load_replay("/nonexistent.csv")
            self.assertFalse(result["ok"])
            self.assertIn("主连接", result["error"])
        finally:
            service.disconnect()


if __name__ == "__main__":
    unittest.main()
