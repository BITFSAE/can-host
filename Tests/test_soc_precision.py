import unittest
from canhost.decoders import decode_pack_status, build_pack_status

class SocPrecisionTest(unittest.TestCase):
    def test_old_and_new_frames(self):
        for soc in (0, 5, 10, 100):
            data = build_pack_status(500, -10, soc)
            self.assertEqual(len(data), 7)
            self.assertEqual(decode_pack_status(data)["soc_pct"], soc)
        for soc in (5.1, 10.5, 15.9, 99.9):
            data = build_pack_status(500, -10, soc)
            self.assertEqual(len(data), 8)
            self.assertEqual(data[4], int(soc))
            self.assertEqual(decode_pack_status(data)["soc_pct"], soc)
            self.assertEqual(decode_pack_status(data[:7])["soc_pct"], int(soc))
        data = bytearray(build_pack_status(500, -10, 15.9))
        data[5] &= ~4
        self.assertIsNone(decode_pack_status(data)["soc_pct"])
        data[5] |= 4
        data[4] = 100
        self.assertIsNone(decode_pack_status(data)["soc_pct"])

    def test_rounding_across_integer_boundary(self):
        for value, expected in ((15.96, 16.0), (99.96, 100.0), (15.94, 15.9)):
            self.assertEqual(decode_pack_status(build_pack_status(500, 0, value))["soc_pct"], expected)

    def test_mqtt_soc_precision_and_legacy(self):
        from canhost.telemetry.fsae_telemetry_pb2 import TelemetryFrame
        from canhost.telemetry.protocol import decode_telemetry_payload
        for soc in (0, 51, 159, 1000):
            frame = TelemetryFrame(frame_id=1, battery_soc=soc // 10, battery_soc_pct_x10=soc)
            self.assertEqual(decode_telemetry_payload(frame.SerializeToString())["summary"]["soc_pct"], soc / 10)
        old = TelemetryFrame(frame_id=1, battery_soc=75)
        self.assertEqual(decode_telemetry_payload(old.SerializeToString())["summary"]["soc_pct"], 75)

    def test_extension_marker(self):
        base = build_pack_status(500, 0, 15)
        for extension in (0x00, 0x99, 0xAA, 0xAF):
            self.assertEqual(decode_pack_status(base + bytes([extension]))["soc_pct"], 15)
