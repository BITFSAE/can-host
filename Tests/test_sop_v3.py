import unittest
from collections import Counter

from canhost.decoders import CanFrame, crc8_sae_j1850, decode_sop_limits, sop_sequence_is_new, build_sop_limits
from canhost.bms.protocol import BmsProtocol
from canhost.vehicle.protocol import VehicleProtocol
from canhost.vehicle.simulator import VehicleSimulator
from cli.pcan_bms_bench import build_sop_ack


def frame(body: bytes) -> CanFrame:
    return CanFrame(0x4A0, body + bytes([crc8_sae_j1850(b"\x04\xa0" + body)]), False)


class SopV3Test(unittest.TestCase):
    def test_zero_invalid_and_old_version_limits(self):
        decoded = decode_sop_limits(frame(bytes.fromhex("E4 02 28 00 FF 00 C0")).data, True)
        self.assertTrue(decoded["frame_valid"])
        self.assertFalse(decoded["limits_valid"])
        self.assertEqual(decoded["discharge_power_kw"], 0)
        self.assertEqual(decoded["charge_power_kw"], 0)
        for body in (bytes.fromhex("E4 02 28 00 FF 00 90"), bytes.fromhex("E4 02 28 00 FF 02 01")):
            self.assertFalse(decode_sop_limits(frame(body).data, True)["frame_valid"])

    def test_actual_power_sign_range_and_independent_validity(self):
        self.assertEqual(build_sop_limits(74, 4, 5, actual_power_kw=-32.1),
                         bytes.fromhex("E4 02 28 00 05 BF FE DE"))
        for value, expected in ((-204.8, -204.8), (-32.199, -32.1), (-0.099, 0.0),
                                (0, 0.0), (32.199, 32.1), (204.7, 204.7),
                                (-204.801, None), (204.701, None), (None, None)):
            for limits_valid in (False, True):
                payload = build_sop_limits(74, 4, 5, limits_valid=limits_valid, actual_power_kw=value)
                decoded = decode_sop_limits(payload, True)
                self.assertTrue(decoded["frame_valid"])
                self.assertEqual(decoded["actual_power_kw"], expected)
                self.assertEqual(decoded["power_valid"], expected is not None)
                self.assertEqual(decoded["limits_valid"], limits_valid)
        payload = bytearray(build_sop_limits(74, 4, 5, actual_power_kw=-32.1))
        payload[5] ^= 1
        self.assertFalse(decode_sop_limits(payload, True)["frame_valid"])
        for protocol in (BmsProtocol(), VehicleProtocol()):
            protocol.ingest(CanFrame(0x4A0, build_sop_limits(74, 4, 5, actual_power_kw=-32.1), False))
            decoded = protocol.sop if isinstance(protocol, BmsProtocol) else protocol.sop["limits"]
            self.assertEqual(decoded["actual_power_kw"], -32.1)
            protocol.ingest(CanFrame(0x4A0, build_sop_limits(74, 4, 6), False))
            decoded = protocol.sop if isinstance(protocol, BmsProtocol) else protocol.sop["limits"]
            self.assertIsNone(decoded["actual_power_kw"])

    def test_wrap_old_sequence_and_recovery(self):
        self.assertTrue(sop_sequence_is_new(0, 255, 0.01))
        self.assertFalse(sop_sequence_is_new(5, 5, 1.0))
        self.assertFalse(sop_sequence_is_new(4, 5, 0.01))
        self.assertTrue(sop_sequence_is_new(0, 5, 0.4))
        for protocol in (BmsProtocol(), VehicleProtocol()):
            protocol.ingest(frame(bytes.fromhex("E4 02 28 00 FF 00 D0")))
            protocol.ingest(frame(bytes.fromhex("E4 02 00 00 00 00 D0")))
            limits = protocol.sop if isinstance(protocol, BmsProtocol) else protocol.sop["limits"]
            self.assertEqual(limits["sequence"], 0)
            self.assertFalse(limits["regen_allowed"])

    def test_cli_ack_matches_documented_vector(self):
        ack = build_sop_ack(bytes.fromhex("E4 02 28 00 05 00 D0 54"))
        self.assertEqual(ack.arbitration_id, 0x4A4)
        self.assertFalse(ack.is_extended_id)
        self.assertEqual(bytes(ack.data), bytes.fromhex("05 03 01 D4"))

    def test_bms_mirror_preserves_canb_control_and_diagnostics(self):
        clock = [1.0]
        protocol = BmsProtocol(clock=lambda: clock[0])
        limits = frame(bytes.fromhex("E4 02 28 00 05 00 D0"))
        protocol.ingest(limits)
        protocol.ingest(CanFrame(0x4A3, bytes.fromhex("08 07 50 00 00 00 FF 34"), False))
        protocol.ingest(CanFrame(0x4A4, bytes.fromhex("05 03 01 D4"), False))
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
        protocol.ingest(frame(bytes.fromhex("E4 02 28 00 05 00 D0")))
        clock[0] = 1.1
        restarted = frame(bytes.fromhex("E4 02 28 00 00 00 D0"))
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
