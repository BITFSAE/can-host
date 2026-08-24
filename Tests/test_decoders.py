"""Byte-level tests for the shared frame decoders.

Anchors: BMS-MASTER-F405 DOC/CAN通信协议.md, PDM Doc/CAN接口.md and the
vehicle-interfaces Vehicle_CanB.dbc.
"""

from __future__ import annotations

import unittest

from canhost.decoders import (decode_alarm_levels, decode_ecu_sop_ack, decode_ecu_status,
                              decode_ecu_wheels_i16, decode_fault_fields, decode_ivt_result,
                              decode_meter_result, decode_pack_status, decode_pdm_side,
                              decode_sop_limits, decode_sop_status, decode_tire_temp_frame)


class SharedDecoderTest(unittest.TestCase):
    def test_pack_status_signed_current_and_validity_bits(self) -> None:
        decoded = decode_pack_status(bytes.fromhex("16 44 FF E2 50 02 30"))
        self.assertEqual(decoded["voltage_v"], None)
        self.assertEqual(decoded["current_a"], -3.0)
        self.assertFalse(decoded["soc_valid"])
        self.assertFalse(decoded["cell_voltage_complete"])
        self.assertEqual(decoded["state"], 3)
        self.assertEqual(decoded["alarm_level"], 0)

    def test_fault_fields_decode(self) -> None:
        decoded = decode_fault_fields(bytes.fromhex("72 80 00 00 01 C6 21 03"))
        self.assertEqual(decoded["code_hex"], "0x80000001")
        self.assertEqual(decoded["state_name"], "FAULT")
        self.assertTrue(decoded["flags"]["latched"])
        self.assertTrue(decoded["flags"]["charge_mode"])
        self.assertEqual(decoded["flags"]["charger_type"], "Chroma")
        self.assertTrue(decoded["slave_offline"][0])
        self.assertTrue(decoded["slave_offline"][5])
        self.assertFalse(decoded["slave_offline"][1])

    def test_alarm_levels_two_bits_each(self) -> None:
        data = bytearray(8)
        data[0] = 0b00001001
        self.assertEqual(decode_alarm_levels(bytes(data))[:4], [1, 2, 0, 0])

    def test_sop_limits_little_and_big_endian(self) -> None:
        data = bytes.fromhex("08 07 50 00 E4 02 50 00")
        little = decode_sop_limits(data, little_endian=True)
        self.assertEqual(little["discharge_current_a"], 180.0)
        self.assertEqual(little["charge_current_a"], 8.0)
        self.assertEqual(little["discharge_power_kw"], 74.0)
        self.assertEqual(little["charge_power_kw"], 8.0)
        big = decode_sop_limits(data, little_endian=False)
        self.assertEqual(big["discharge_current_a"], 205.5)

    def test_sop_status_pair_crc(self) -> None:
        limits = bytes.fromhex("08 07 50 00 E4 02 50 00")
        status = decode_sop_status(bytes.fromhex("15 27 05 00 00 FF 60 9C"), limits)
        self.assertTrue(status["crc_valid"])
        self.assertTrue(status["ack_fresh"])
        self.assertEqual(status["sequence"], 5)
        self.assertEqual(status["protocol_version"], 1)
        # Missing limits frame must report crc_valid False, not raise.
        self.assertFalse(decode_sop_status(bytes.fromhex("15 27 05 00 00 FF 60 9C"), None)["crc_valid"])

    def test_ecu_sop_ack_crc(self) -> None:
        data = bytes([0x10 | 3, 0x07, 0x46, 0xE8, 0x03, 0x82, 0x01, 0x00])
        decoded = decode_ecu_sop_ack(data)
        self.assertEqual(decoded["sequence"], 3)
        self.assertFalse(decoded["crc_valid"])
        # Corrupt CRC must fail; rebuild with the real CRC appended.
        from canhost.decoders import crc8_sae_j1850
        good = data[:7] + bytes([crc8_sae_j1850(bytes.fromhex("04 A4") + data[:7])])
        self.assertTrue(decode_ecu_sop_ack(good)["crc_valid"])

    def test_ivt_result_is_little_endian(self) -> None:
        payload = bytes([0x00, 0x03]) + (-1234).to_bytes(4, "little", signed=True)
        result = decode_ivt_result(payload, expected_mux=0)
        self.assertEqual(result["value"], -1234)
        self.assertEqual(result["counter"], 3)
        self.assertIsNone(decode_ivt_result(payload, expected_mux=1))

    def test_meter_result_is_big_endian(self) -> None:
        payload = bytes([0x00, 0x03]) + (12345).to_bytes(4, "big", signed=True)
        result = decode_meter_result(payload, expected_mux=0)
        self.assertEqual(result["value"], 12345)
        # The same bytes read little-endian would differ; keep the orders apart.
        self.assertEqual(decode_ivt_result(payload, expected_mux=0)["value"], 0x39300000)
        self.assertIsNone(decode_meter_result(payload, expected_mux=5))

    def test_pdm_side_decode_and_offline_sentinel(self) -> None:
        decoded = decode_pdm_side(bytes.fromhex("5D C0 00 96 01 68 00 7B"))
        self.assertEqual(decoded["voltage_v"], 24.0)
        self.assertEqual(decoded["current_a"], 1.5)
        self.assertEqual(decoded["power_w"], 36.0)
        self.assertEqual(decoded["energy_wh"], 1.23)
        self.assertFalse(decoded["offline"])
        offline = decode_pdm_side(bytes.fromhex("7F FF 7F FF FF FF FF FF"))
        self.assertTrue(offline["offline"])
        self.assertIsNone(offline["voltage_v"])
        self.assertIsNone(offline["energy_wh"])

    def test_ecu_wheels_byte_order(self) -> None:
        # Frame order RL, RR, FL, FR -> returned display order FL, FR, RL, RR.
        payload = b"".join(value.to_bytes(2, "little", signed=True)
                           for value in (100, 200, 300, 400))
        self.assertEqual(decode_ecu_wheels_i16(payload, 0.1), [30.0, 40.0, 10.0, 20.0])

    def test_ecu_status_nibbles(self) -> None:
        data = bytes([0x25, 0x0D, 0xF0, 0x44, 0x33])
        status = decode_ecu_status(data)
        # byte0 low nibble: error FR/FL/RR/RL; high nibble: signed mode flag.
        self.assertTrue(status["error"]["FR"])
        self.assertTrue(status["error"]["RR"])
        self.assertFalse(status["error"]["FL"])
        self.assertEqual(status["mode_flag"], 2)
        # byte1 low nibble: system ready; high nibble: quit dc on.
        self.assertTrue(status["system_ready"]["FR"])
        self.assertTrue(status["system_ready"]["RR"])
        self.assertTrue(status["system_ready"]["RL"])
        self.assertFalse(status["quit_dc_on"]["FR"])
        # byte2 low nibble: quit inverter on; high nibble: enable.
        self.assertFalse(status["quit_inverter_on"]["RR"])
        self.assertTrue(status["enable"]["FR"])
        self.assertTrue(status["enable"]["RL"])
        # byte3/4: logic states RR, RL (byte3), FR, FL (byte4).
        self.assertEqual(status["logic_state"]["RR"], 4)
        self.assertEqual(status["logic_state"]["RL"], 4)
        self.assertEqual(status["logic_state"]["FR"], 3)
        self.assertEqual(status["logic_state"]["FL"], 3)

    def test_tire_temp_integer_plus_fraction(self) -> None:
        data = bytes([30, 0x55, 40, 0x00, 48, 0x01, 60, 0x63])
        self.assertEqual(decode_tire_temp_frame(data), [30.85, 40.0, 48.01, 60.99])
        self.assertEqual(decode_tire_temp_frame(b"\x01\x02"), None)


if __name__ == "__main__":
    unittest.main()
