import unittest
from collections import Counter

from canhost.decoders import CanFrame, crc8_sae_j1850, decode_sop_limits, sop_sequence_is_new
from canhost.bms.protocol import BmsProtocol
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.simulator import VehicleSimulator
from cli.pcan_bms_bench import build_sop_ack


def frame(body: bytes) -> CanFrame:
    return CanFrame(0x4A0, body + bytes([crc8_sae_j1850(b"\x04\xa0" + body)]), False)


class SopV2Test(unittest.TestCase):
    def test_zero_invalid_and_reserved_limits(self):
        decoded = decode_sop_limits(frame(bytes.fromhex("E4 02 28 00 FF 02 00")).data, True)
        self.assertTrue(decoded["frame_valid"])
        self.assertFalse(decoded["limits_valid"])
        self.assertEqual(decoded["discharge_power_kw"], 0)
        self.assertEqual(decoded["charge_power_kw"], 0)
        for body in (bytes.fromhex("E4 02 28 00 FF 01 01"), bytes.fromhex("E4 02 28 00 FF 02 81")):
            self.assertFalse(decode_sop_limits(frame(body).data, True)["frame_valid"])

    def test_wrap_old_sequence_and_recovery(self):
        self.assertTrue(sop_sequence_is_new(0, 255, 0.01))
        self.assertFalse(sop_sequence_is_new(5, 5, 1.0))
        self.assertFalse(sop_sequence_is_new(4, 5, 0.01))
        self.assertTrue(sop_sequence_is_new(0, 5, 0.4))
        for protocol in (BmsProtocol(), VehicleProtocol()):
            protocol.ingest(frame(bytes.fromhex("E4 02 28 00 FF 02 01")))
            protocol.ingest(frame(bytes.fromhex("E4 02 00 00 00 02 01")))
            limits = protocol.sop if isinstance(protocol, BmsProtocol) else protocol.sop["limits"]
            self.assertEqual(limits["sequence"], 0)
            self.assertFalse(limits["regen_allowed"])

    def test_cli_ack_matches_documented_vector(self):
        ack = build_sop_ack(bytes.fromhex("E4 02 28 00 05 02 01 29"))
        self.assertEqual(ack.arbitration_id, 0x4A4)
        self.assertFalse(ack.is_extended_id)
        self.assertEqual(bytes(ack.data), bytes.fromhex("05 02 01 98"))

    def test_bms_mirror_preserves_canb_control_and_diagnostics(self):
        clock = [1.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        limits = frame(bytes.fromhex("E4 02 28 00 05 02 01"))
        protocol.ingest(limits)
        protocol.ingest(CanFrame(0x4A3, bytes.fromhex("08 07 50 00 00 00 FF 24"), False))
        protocol.ingest(CanFrame(0x4A4, bytes.fromhex("05 02 01 98"), False))
        clock[0] = 1.1
        mirror = CanFrame(0x186A50F4, bytes.fromhex("07 08 00 50 02 BC 00 1E"), True)
        protocol.ingest(mirror)
        self.assertEqual(protocol.sop["discharge_power_kw"], 74.0)
        self.assertEqual(protocol.sop["discharge_current_a"], 180.0)
        self.assertEqual(protocol.sop["sequence"], 5)
        self.assertTrue(protocol.sop["status"]["ack_fresh"])
        self.assertEqual(protocol.sop["ecu_ack"]["sequence"], 5)
        protocol.ingest(limits)
        self.assertEqual(protocol._sop_limits_received_at, 1.0)
        clock[0] = 1.4
        protocol.ingest(mirror)
        self.assertEqual(protocol.sop["discharge_power_kw"], 70.0)
        self.assertEqual(protocol.sop["source"], "CAN1")
        protocol.ingest(limits)
        self.assertEqual(protocol._sop_limits_received_at, 1.0)

    def test_bms_recovery_uses_protocol_clock(self):
        clock = [1.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        protocol.ingest(frame(bytes.fromhex("E4 02 28 00 05 02 01")))
        clock[0] = 1.1
        restarted = frame(bytes.fromhex("E4 02 28 00 00 02 01"))
        protocol.ingest(restarted)
        self.assertEqual(protocol.sop["sequence"], 5)
        clock[0] = 1.4
        protocol.ingest(restarted)
        self.assertEqual(protocol.sop["sequence"], 0)
        self.assertEqual(protocol._sop_limits_received_at, 1.4)

    def test_simulator_periods_survive_counter_wrap(self):
        frames = []
        simulator = VehicleSimulator(frames.append)
        for _ in range(300):
            simulator._emit_sop()
        counts = Counter(f.arbitration_id for f in frames)
        self.assertEqual(counts, {0x4A0: 300, 0x4A3: 6, 0x4A4: 60})
        self.assertEqual([f.data[4] for f in frames if f.arbitration_id == 0x4A0][255:258], [255, 0, 1])


if __name__ == "__main__":
    unittest.main()
