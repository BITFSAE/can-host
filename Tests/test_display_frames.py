"""Frame builders for the driver-display bench, anchored to firmware and interface samples.

The golden vectors come from HMI_Display ``tools/tests/firmware_regression.c``
(the same bytes the display firmware is regression-tested with) and from the
sample frames in vehicle-interfaces ``docs/CANB接口.md`` and PDM ``Doc/CAN接口.md``.
"""

from __future__ import annotations

import unittest

from canhost import decoders as d


class NodeFrameGoldenTest(unittest.TestCase):
    def test_pack_status_matches_firmware_regression_frame(self) -> None:
        # 549.1 V, -85.7 A (charge positive), SOC 77, all validity bits, state 5.
        self.assertEqual(d.build_pack_status(549.1, -85.7, 77).hex(), "1573fca74d1f50")

    def test_pack_status_validity_bits_and_round_trip(self) -> None:
        frame = d.build_pack_status(548.0, 12.3, 80, voltage_valid=False, current_valid=True,
                                    soc_valid=False, cells_complete=False, temperatures_complete=True,
                                    state=4, alarm_level=2)
        decoded = d.decode_pack_status(frame)
        self.assertEqual(frame[5], 0b10010)
        self.assertIsNone(decoded["voltage_v"])
        self.assertAlmostEqual(decoded["current_a"], 12.3)
        self.assertIsNone(decoded["soc_pct"])
        self.assertEqual((decoded["state"], decoded["alarm_level"]), (4, 2))

    def test_sop_frames_match_interface_samples(self) -> None:
        self.assertEqual(d.build_sop_limits(74.0, 4.0, 5).hex(" "), "e4 02 28 00 05 02 01 29")
        self.assertEqual(d.build_sop_status(180.0, 8.0).hex(" "), "08 07 50 00 00 00 ff 24")

    def test_sop_limits_invalid_variants_are_rejected_by_the_existing_decoder(self) -> None:
        good = d.decode_sop_limits(d.build_sop_limits(74.0, 4.0, 5), True)
        self.assertTrue(good["frame_valid"] and good["limits_valid"])
        self.assertEqual((good["discharge_power_kw"], good["charge_power_kw"], good["sequence"]), (74.0, 4.0, 5))
        bad_crc = d.decode_sop_limits(d.build_sop_limits(74.0, 4.0, 5, crc_error=True), True)
        self.assertFalse(bad_crc["crc_valid"] or bad_crc["frame_valid"])
        wrong_version = d.decode_sop_limits(d.build_sop_limits(74.0, 4.0, 5, version=3), True)
        self.assertTrue(wrong_version["crc_valid"])
        self.assertFalse(wrong_version["frame_valid"])
        reserved = d.decode_sop_limits(d.build_sop_limits(74.0, 4.0, 5, reserved_bits=0x02), True)
        self.assertTrue(reserved["crc_valid"])
        self.assertFalse(reserved["frame_valid"])

    def test_sop_limits_send_zero_power_when_limits_are_not_valid(self) -> None:
        frame = d.build_sop_limits(74.0, 4.0, 9, limits_valid=False)
        self.assertEqual(frame[:4], bytes(4))
        self.assertEqual(frame[6], 0)
        self.assertTrue(d.decode_sop_limits(frame, True)["frame_valid"])

    def test_sop_status_input_health_and_version(self) -> None:
        valid = d.decode_sop_status(d.build_sop_status(150.0, 8.0))
        self.assertTrue(valid["frame_valid"] and valid["input_health"] & 0x80)
        invalid_inputs = d.build_sop_status(150.0, 8.0, inputs_valid=False)
        self.assertEqual(invalid_inputs[6] & 0x80, 0)
        self.assertFalse(d.decode_sop_status(d.build_sop_status(150.0, 8.0, version=3))["frame_valid"])

    def test_sop_sequence_wraps_to_one_byte(self) -> None:
        self.assertEqual(d.build_sop_limits(1.0, 1.0, 256)[4], 0)
        self.assertEqual(d.build_sop_limits(1.0, 1.0, 255)[4], 255)

    def test_pdm_matches_firmware_and_pdm_document_samples(self) -> None:
        self.assertEqual(d.build_pdm_side(24.0, 1.5, 36.0, 1.23).hex(" "), "5d c0 00 96 01 68 00 7b")
        self.assertEqual(d.build_pdm_side(13.82, 12.40, 170.7, 0.16).hex(" "), "35 fc 04 d8 06 ab 00 10")
        offline = d.build_pdm_side(24.0, 1.5, 36.0, 1.23, offline=True)
        self.assertEqual(offline.hex(), "7fff7fffffffffff")
        self.assertTrue(d.decode_pdm_side(offline)["offline"])

    def test_pdm_negative_current_and_single_field_saturation(self) -> None:
        self.assertEqual(d.decode_pdm_side(d.build_pdm_side(24.0, -2.5, 0.0, 0.0))["current_a"], -2.5)
        # One saturated field is not an offline report (only all four placeholders together are).
        voltage_only = d.build_pdm_side(99.0, 1.0, 10.0, 1.0)
        self.assertEqual(voltage_only[:2].hex(), "7fff")
        self.assertFalse(d.decode_pdm_side(voltage_only)["offline"])

    def test_data_logger_fields(self) -> None:
        frame = d.build_data_logger(-12.5, 50.0, 3.5)
        self.assertEqual(len(frame), 6)
        self.assertEqual(int.from_bytes(frame[0:2], "little", signed=True), -125)
        self.assertEqual(int.from_bytes(frame[2:4], "little"), 500)
        self.assertEqual(int.from_bytes(frame[4:6], "little"), 3500)

    def test_wheel_frames_use_the_ecu_byte_order(self) -> None:
        # Display order FL, FR, RL, RR -> wire order RL, RR, FL, FR.
        frame = d.build_ecu_wheels_i16([1, 2, 3, 4], 1.0)
        self.assertEqual(list(frame[::2]), [3, 4, 1, 2])
        self.assertEqual(d.decode_ecu_wheels_i16(frame, 1.0), [1.0, 2.0, 3.0, 4.0])

    def test_temperature_and_signed_values_round_trip_through_the_decoder(self) -> None:
        values = [61.1, -12.5, 0.0, 150.0]
        self.assertEqual(d.decode_ecu_wheels_i16(d.build_ecu_wheels_i16(values, 0.1), 0.1), values)
        torque = [-99, 21, 0, 32767]
        self.assertEqual(d.decode_ecu_wheels_i16(d.build_ecu_wheels_i16(torque, 1.0), 1.0),
                         [float(value) for value in torque])

    def test_firmware_regression_temperature_frame(self) -> None:
        # RL 30, RR 40, FL 50, FR 60 degC as 0.1 degC little-endian.
        frame = d.build_ecu_wheels_i16([50, 60, 30, 40], 0.1)
        self.assertEqual(frame.hex(" "), "2c 01 90 01 f4 01 58 02")

    def test_amk_diagnostic_numbers_are_little_endian_u32(self) -> None:
        self.assertEqual(d.build_amk_diagnostic(0x231, 0x22).hex(" "), "31 02 00 00 22 00 00 00")
        self.assertEqual(d.build_amk_diagnostic(2 ** 40, -5).hex(" "), "ff ff ff ff 00 00 00 00")

    def test_ecu_status_mode_and_enable_bits(self) -> None:
        frame = d.build_ecu_status(1, [True, False, False, True], [0x419, 0, 0, 0])
        decoded = d.decode_ecu_status(frame)
        self.assertEqual(len(frame), 5)
        self.assertEqual(decoded["mode_flag"], 1)
        self.assertEqual(decoded["enable"], {"FL": True, "FR": False, "RL": False, "RR": True})
        self.assertEqual(decoded["error"], {"FL": True, "FR": False, "RL": False, "RR": False})
        self.assertEqual(d.decode_ecu_status(d.build_ecu_status(-1, [True] * 4, [0] * 4))["mode_flag"], -1)
        # Firmware: byte2 bit4 FR, bit5 FL, bit6 RR, bit7 RL.
        enable_bits = d.build_ecu_status(0, [True, False, False, False], [0] * 4)[2] >> 4
        self.assertEqual(enable_bits, 0b0010)

    def test_fan_status_round_trip(self) -> None:
        frame = d.build_fan_status([2520, 2640, 1800], [42, 38])
        self.assertEqual(d.decode_fan_status(frame), {"rpm": [2520, 2640, 1800], "duty_pct": [42, 38]})

    def test_tire_frame_integer_and_hundredths(self) -> None:
        frame = d.build_tire_frame([48.37, 0.0, 255.99, 7.05])
        self.assertEqual(frame.hex(" "), "30 25 00 00 ff 63 07 05")
        self.assertEqual(d.decode_tire_temp_frame(frame), [48.37, 0.0, 255.99, 7.05])
        self.assertEqual(d.build_tire_frame([-3.0, 1000.0, 1.0, 1.0])[:4].hex(), "0000ff63")


class SteeringWheelFrameTest(unittest.TestCase):
    def test_drive_mode_request_is_ascii_with_dlc_eight(self) -> None:
        frame = d.build_drive_mode_request(d.DRIVE_MODE_ASCII_ZERO + 2)
        self.assertEqual(frame, b"2" + bytes(7))
        self.assertEqual(d.decode_drive_mode_request(frame), {"code": 50, "mode": 2})
        self.assertIsNone(d.decode_drive_mode_request(d.build_drive_mode_request(0xFF))["mode"])
        self.assertIsNone(d.decode_drive_mode_request(d.build_drive_mode_request(52))["mode"])

    def test_slip_and_buttons_are_single_byte_frames(self) -> None:
        self.assertEqual(d.build_slip_level(7), b"\x07")
        self.assertEqual(d.build_button_press(), b"\x01")
        self.assertEqual(d.build_button_press(False), b"\x00")


class DisplayOriginatedFrameTest(unittest.TestCase):
    """Frames the display sends; layouts follow can.c CAN_SendGPSSpeed / CAN_SendGPSTelemetry."""

    def test_speed_is_tenths_kmh_little_endian(self) -> None:
        self.assertEqual(d.decode_display_speed(bytes([0xE8, 0x03])), {"speed_kmh": 100.0})
        self.assertIsNone(d.decode_display_speed(b"\x00"))

    def test_position_uses_signed_1e7_degrees(self) -> None:
        latitude, longitude = round(39.9042 * 1e7), round(-116.4074 * 1e7)
        frame = latitude.to_bytes(4, "little", signed=True) + longitude.to_bytes(4, "little", signed=True)
        decoded = d.decode_display_position(frame)
        self.assertAlmostEqual(decoded["latitude_deg"], 39.9042, places=6)
        self.assertAlmostEqual(decoded["longitude_deg"], -116.4074, places=6)

    def test_motion_word_matches_the_dbc_bit_positions(self) -> None:
        # flags 0..3, lap diag 4..6, heading quality 7..9, fix quality 10..12, signal level 13..15.
        word = 0b1 | 0b0 << 1 | 0b1 << 2 | 0b1 << 3 | 6 << 4 | 5 << 7 | 4 << 10 | 3 << 13
        frame = (12345).to_bytes(2, "little") + (35999).to_bytes(2, "little") \
            + (-25).to_bytes(2, "little", signed=True) + word.to_bytes(2, "little")
        decoded = d.decode_display_motion(frame)
        self.assertEqual((decoded["track_deg"], decoded["heading_deg"], decoded["altitude_m"]),
                         (123.45, 359.99, -2.5))
        self.assertEqual((decoded["position_valid"], decoded["heading_valid"],
                          decoded["lap_active"], decoded["rmc_valid"]), (True, False, True, True))
        self.assertEqual((decoded["lap_diag_state"], decoded["heading_quality"],
                          decoded["fix_quality"], decoded["signal_level"]), (6, 5, 4, 3))
        self.assertEqual(d.LAP_DIAG_NAMES[decoded["lap_diag_state"]], "已过线")

    def test_status_and_lap_frames(self) -> None:
        status = (123).to_bytes(3, "little") + bytes([0xF6, 0x2A, 21, 18, 4])
        decoded = d.decode_display_status(status)
        self.assertEqual((decoded["odometer_km"], decoded["max_snr"], decoded["avg_snr"]), (12.3, -10, 42))
        self.assertEqual((decoded["satellites"], decoded["gsv_tracked_satellites"], decoded["lap_count"]), (21, 18, 4))
        lap = (5231).to_bytes(2, "little") + (5310).to_bytes(2, "little") + (5280).to_bytes(2, "little") \
            + (-49).to_bytes(2, "little", signed=True)
        self.assertEqual(d.decode_display_lap(lap),
                         {"current_s": 52.31, "last_s": 53.1, "best_s": 52.8, "delta_s": -0.49})
        self.assertIsNone(d.decode_display_status(bytes(7)))
        self.assertIsNone(d.decode_display_lap(bytes(9)))

    def test_merged_steering_command(self) -> None:
        # Firmware: byte0 slip level, byte1 bit0 lap toggle, bit1 error clear.
        self.assertEqual(d.decode_steering_command(bytes([5, 1])),
                         {"slip_level": 5, "lap_toggle": True, "error_clear": False})
        self.assertEqual(d.decode_steering_command(bytes([7, 2])),
                         {"slip_level": 7, "lap_toggle": False, "error_clear": True})
        self.assertIsNone(d.decode_steering_command(b"\x05"))


if __name__ == "__main__":
    unittest.main()
