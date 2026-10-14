"""BRT encoder: frame formats, motion model, simulator and the PCAN service."""

from __future__ import annotations

from collections import deque
import json
import threading
import time
import unittest
from unittest.mock import patch

import can

from canhost import decoders as d
from canhost.app import Api
from canhost.encoder import model
from canhost.encoder.service import EncoderService, encoder_catalog
from canhost.encoder.simulator import SimulatedEncoder, SimulatedEncoderBus, default_motion


def wait_until(condition, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def still(raw: float = 1000.0):
    return lambda now: raw


class FrameFormatTest(unittest.TestCase):
    """Frames are anchored to the examples in the vendor manual V2.7 pages 12-17."""

    def test_requests_match_the_manual_examples(self) -> None:
        self.assertEqual(d.build_brt_read(1).hex(" "), "04 01 01 00")
        self.assertEqual(d.build_brt_report_mode(1, True).hex(" "), "04 01 04 aa")
        self.assertEqual(d.build_brt_report_mode(1, False).hex(" "), "04 01 04 00")
        self.assertEqual(d.build_brt_period(1, 5000).hex(" "), "05 01 05 88 13")
        self.assertEqual(d.build_brt_zero(1).hex(" "), "04 01 06 00")
        self.assertEqual(d.build_brt_direction(1, False).hex(" "), "04 01 07 00")
        self.assertEqual(d.build_brt_direction(1, True).hex(" "), "04 01 07 01")
        self.assertEqual(d.build_brt_midpoint(1).hex(" "), "04 01 0c 01")
        self.assertEqual(d.build_brt_set_address(1, 5).hex(" "), "04 01 02 05")

    def test_other_read_commands_and_little_endian_payloads(self) -> None:
        for func in (d.BRT_FUNC_MULTITURN, d.BRT_FUNC_TURNS, d.BRT_FUNC_SPEED):
            self.assertEqual(d.build_brt_read(1, func).hex(" "), f"04 01 {func:02x} 00")
        self.assertEqual(d.build_brt_set_count(1, 2048).hex(" "), "07 01 0d 00 08 00 00")
        self.assertEqual(d.build_brt_speed_time(1, 100).hex(" "), "05 01 0b 64 00")
        self.assertEqual(d.build_brt_speed_time(1, 0x1234).hex(" "), "05 01 0b 34 12")
        self.assertEqual(d.build_brt_period(0x2A, 0xFFFF).hex(" "), "05 2a 05 ff ff")
        self.assertEqual(d.build_brt_period(1, 6000).hex(" "), "05 01 05 70 17")

    def test_bitrate_codes_follow_the_manual(self) -> None:
        codes = {500_000: "00", 1_000_000: "01", 250_000: "02", 125_000: "03", 100_000: "04"}
        for bitrate, code in codes.items():
            self.assertEqual(d.build_brt_set_bitrate(1, bitrate).hex(" "), f"04 01 03 {code}")

    def test_len_counts_every_byte_including_itself(self) -> None:
        for frame in (d.build_brt_read(1), d.build_brt_period(1, 5000), d.build_brt_set_count(1, 7)):
            self.assertEqual(frame[0], len(frame))

    def test_builders_reject_out_of_range_values(self) -> None:
        for call in (lambda: d.build_brt_period(1, 4999), lambda: d.build_brt_period(1, 1000),
                     lambda: d.build_brt_period(1, 50), lambda: d.build_brt_period(1, 65536),
                     lambda: d.build_brt_period(1, True), lambda: d.build_brt_set_count(1, 4096),
                     lambda: d.build_brt_set_count(1, -1), lambda: d.build_brt_speed_time(1, 0),
                     lambda: d.build_brt_speed_time(1, 65536), lambda: d.build_brt_set_bitrate(1, 123_456),
                     lambda: d.build_brt_read(0), lambda: d.build_brt_read(256), lambda: d.build_brt_read(True),
                     lambda: d.build_brt_set_address(1, 0), lambda: d.build_brt_read(1, d.BRT_FUNC_ZERO)):
            with self.assertRaises(ValueError):
                call()

    def test_position_example_is_90_degrees(self) -> None:
        decoded = d.decode_brt_frame(bytes.fromhex("07 01 01 00 04 00 00"), 1)
        self.assertEqual(decoded["kind"], "position")
        self.assertEqual(decoded["raw"], 1024)
        self.assertTrue(decoded["in_range"])
        self.assertEqual(d.brt_angle_deg(decoded["raw"]), 90.0)
        self.assertAlmostEqual(d.brt_angle_deg(4095), 359.912109375, places=9)

    def test_position_bytes_are_little_endian(self) -> None:
        decoded = d.decode_brt_frame(bytes([7, 1, 1, 0x34, 0x02, 0, 0]), 1)
        self.assertEqual(decoded["raw"], 0x0234)

    def test_out_of_range_position_is_flagged_not_clamped(self) -> None:
        decoded = d.decode_brt_frame(bytes([7, 1, 1, 0x00, 0x10, 0, 0]), 1)   # 4096
        self.assertFalse(decoded["in_range"])
        self.assertEqual(decoded["raw"], 4096)

    def test_acknowledgement_status_zero_is_success(self) -> None:
        ok = d.decode_brt_frame(bytes([4, 1, 6, 0]), 1)
        self.assertEqual((ok["kind"], ok["func"], ok["ok"]), ("ack", 6, True))
        failed = d.decode_brt_frame(bytes([4, 1, 6, 1]), 1)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], 1)

    def test_value_answers_carry_unsigned_and_signed_readings(self) -> None:
        speed = d.decode_brt_frame(bytes([7, 1, d.BRT_FUNC_SPEED]) + (-5 & 0xFFFFFFFF).to_bytes(4, "little"), 1)
        self.assertEqual((speed["kind"], speed["signed"], speed["unsigned"]), ("value", -5, 0xFFFFFFFB))
        turns = d.decode_brt_frame(bytes([7, 1, d.BRT_FUNC_TURNS, 3, 0, 0, 0]), 1)
        self.assertEqual((turns["unsigned"], turns["signed"]), (3, 3))

    def test_malformed_frames_are_not_decoded(self) -> None:
        for data in (b"", bytes([3, 1, 1]), bytes([7, 1, 1, 0, 4, 0]),            # shorter than LEN
                     bytes([6, 1, 1, 0, 4, 0, 0]),                                    # LEN disagrees with DLC
                     bytes([7, 2, 1, 0, 4, 0, 0])):                                   # other node address
            self.assertIsNone(d.decode_brt_frame(data, 1), data.hex())
        self.assertIsNotNone(d.decode_brt_frame(bytes([7, 2, 1, 0, 4, 0, 0]), None))

    def test_unusual_but_consistent_layouts_are_classified_unknown(self) -> None:
        self.assertEqual(d.decode_brt_frame(bytes([6, 1, 1, 0, 0, 0]), 1)["kind"], "unknown")
        self.assertEqual(d.decode_brt_frame(bytes([7, 1, 0x55, 0, 0, 0, 0]), 1)["kind"], "unknown")

    def test_speed_conversion_uses_the_manual_formula(self) -> None:
        self.assertAlmostEqual(d.brt_speed_rpm(4096, 60000), 1.0)
        self.assertAlmostEqual(d.brt_speed_rpm(-2048, 100), -2048 * 60000 / (4096 * 100))
        with self.assertRaises(ValueError):
            d.brt_speed_rpm(1, 0)


class ModelTest(unittest.TestCase):
    def test_circular_distance_wraps(self) -> None:
        self.assertEqual(model.circular_distance(4095, 0), 1)
        self.assertEqual(model.circular_distance(0, 2048), 2048)
        self.assertEqual(model.circular_distance(10, 10), 0)
        self.assertEqual(model.circular_distance(100, 4000), 196)

    def test_unwrapping_follows_both_directions_across_the_wrap(self) -> None:
        tracker = model.MotionTracker()
        values = [4090, 4094, 3, 10]
        accumulated = [tracker.push(i * 0.01, raw)[0] for i, raw in enumerate(values)]
        self.assertEqual(accumulated, [4090, 4094, 4099, 4106])
        tracker.reset()
        accumulated = [tracker.push(i * 0.01, raw)[0] for i, raw in enumerate([5, 1, 4094, 4090])]
        self.assertEqual(accumulated, [5, 1, -2, -6])

    def test_half_turn_step_is_taken_as_the_shorter_way(self) -> None:
        tracker = model.MotionTracker()
        tracker.push(0.0, 0)
        self.assertEqual(tracker.push(0.01, 2047)[0], 2047)
        self.assertEqual(tracker.push(0.02, 4094)[0], 4094)       # 2047 more
        tracker.reset()
        tracker.push(0.0, 0)
        self.assertEqual(tracker.push(0.01, 2049)[0], -2047)      # past half a turn: the other way

    def test_speed_matches_a_constant_rotation(self) -> None:
        tracker = model.MotionTracker()
        speed = None
        for step in range(300):                                   # 1 kHz, 100 deg/s = 1.1378 count/ms
            raw = int(step * 100 / 360 * 4096 / 1000) % 4096
            _, speed = tracker.push(step * 0.001, raw)
        self.assertAlmostEqual(speed, 100.0, delta=2.5)           # one count over 50 ms is 1.76 deg/s

    def test_first_sample_and_zero_time_step_have_no_speed(self) -> None:
        tracker = model.MotionTracker()
        self.assertEqual(tracker.push(1.0, 10)[1], None)
        self.assertEqual(tracker.push(1.0, 11)[1], None)

    def test_slow_streams_use_the_previous_sample(self) -> None:
        tracker = model.MotionTracker(window_s=0.05)
        tracker.push(0.0, 0)
        _, speed = tracker.push(0.2, 100)                          # 200 ms apart, wider than the window
        self.assertAlmostEqual(speed, 100 * 360 / 4096 / 0.2, places=6)

    def test_motion_labels_split_still_from_moving(self) -> None:
        self.assertEqual(model.motion_label(None), None)
        self.assertEqual(model.motion_label(0.5), "still")
        self.assertEqual(model.motion_label(-1.9), "still")
        self.assertEqual(model.motion_label(2.0), "increasing")
        self.assertEqual(model.motion_label(-30), "decreasing")

    def test_max_trackable_rpm_is_half_a_turn_per_sample(self) -> None:
        self.assertAlmostEqual(model.max_trackable_rpm(0.001), 30000.0)
        self.assertAlmostEqual(model.max_trackable_rpm(0.065535), 30 / 0.065535)

    def test_timebase_uses_hardware_stamps_and_never_goes_backwards(self) -> None:
        base = model.Timebase()
        first = base.map(1_000_000.0, 50.0)                        # offset = -999950
        self.assertEqual(first, 50.0)
        self.assertAlmostEqual(base.map(1_000_000.001, 50.3), 50.001)   # batched arrival does not clump the stamps
        self.assertAlmostEqual(base.map(1_000_000.0005, 50.4), 50.001)  # older stamp is held, not moved back

    def test_timebase_resyncs_after_a_clock_jump_and_falls_back_to_arrival(self) -> None:
        base = model.Timebase(resync_s=1.0)
        base.map(100.0, 10.0)
        self.assertAlmostEqual(base.map(100.5, 10.5), 10.5)
        self.assertAlmostEqual(base.map(500.0, 11.0), 11.0)        # 400 s jump: offset fixed again
        for stamp in (None, 0, -1, float("nan"), "bad"):
            fallback = model.Timebase()
            self.assertEqual(fallback.map(stamp, 7.25), 7.25)

    def test_series_cursor_returns_only_new_samples(self) -> None:
        series = model.SampleSeries(capacity=100, slack=10)
        for index in range(5):
            series.append(float(index), index, index, None)
        first = series.fetch(0, series.epoch)
        self.assertEqual((first["cursor"], first["raw"], first["reset"]), (5, [0, 1, 2, 3, 4], False))
        series.append(5.0, 5, 5, 1.5)
        second = series.fetch(first["cursor"], first["epoch"])
        self.assertEqual((second["raw"], second["speed"], second["cursor"]), ([5], [1.5], 6))
        self.assertEqual(series.fetch(6, series.epoch)["t"], [])

    def test_series_trims_in_blocks_and_tells_a_stale_reader_to_reset(self) -> None:
        series = model.SampleSeries(capacity=10, slack=5)
        for index in range(16):
            series.append(float(index), index, index, None)
        self.assertEqual(len(series), 10)
        self.assertEqual(series.total, 16)
        stale = series.fetch(2, series.epoch)                      # cursor 2 was trimmed away
        self.assertTrue(stale["reset"])
        self.assertEqual(stale["raw"], list(range(6, 16)))
        self.assertFalse(series.fetch(16, series.epoch)["reset"])
        self.assertTrue(series.fetch(99, series.epoch)["reset"])   # cursor from the future: another session

    def test_series_clear_changes_the_epoch(self) -> None:
        series = model.SampleSeries()
        series.append(0.0, 1, 1, None)
        epoch = series.epoch
        series.clear()
        self.assertNotEqual(series.epoch, epoch)
        self.assertEqual(len(series), 0)
        self.assertTrue(series.fetch(1, epoch)["reset"])

    def test_stability_reports_spread_over_the_window(self) -> None:
        series = model.SampleSeries()
        for index, count in enumerate([1000, 1001, 999, 1000, 1002, 998]):
            series.append(index * 0.1, count % 4096, count, None)
        stats = series.stability(0.5, 5.0)
        self.assertEqual((stats["samples"], stats["ptp_counts"], stats["moving"]), (6, 4, False))
        self.assertAlmostEqual(stats["ptp_deg"], 4 * 360 / 4096, places=3)
        self.assertAlmostEqual(stats["std_counts"], 1.291, places=3)       # population deviation of 0,1,-1,0,2,-2
        windowed = series.stability(0.5, 0.25)                      # only the last three samples
        self.assertEqual(windowed["samples"], 3)
        series.append(0.6, 1200, 1200, None)
        self.assertTrue(series.stability(0.6, 5.0)["moving"])
        self.assertEqual(model.SampleSeries().stability(0.0, 5.0), {"window_s": 5.0, "samples": 0})

    def test_csv_has_a_header_and_one_row_per_sample(self) -> None:
        series = model.SampleSeries()
        self.assertEqual(series.csv().strip(), "time_s,raw_count,angle_deg,accumulated_deg,speed_rpm")
        series.append(10.0, 1024, 1024, None)
        series.append(10.5, 1034, 1034, 60.0)
        lines = series.csv().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "0.000000,1024,90.0000,90.0000,")
        self.assertEqual(lines[2], "0.500000,1034,90.8789,90.8789,10.000")


class SimulatorTest(unittest.TestCase):
    def make(self, **kwargs) -> SimulatedEncoder:
        return SimulatedEncoder(1, motion=still(1024.0), noise_counts=0, **kwargs)

    def handle(self, device: SimulatedEncoder, frame: bytes, now: float = 1.0):
        return device.handle(1, frame, now)

    def test_position_query_matches_the_manual_layout(self) -> None:
        reply = self.handle(self.make(), d.build_brt_read(1))
        self.assertEqual(reply, [(1, bytes.fromhex("07 01 01 00 04 00 00"))])

    def test_zero_midpoint_and_count_move_the_reference(self) -> None:
        device = self.make()
        raw = lambda: d.decode_brt_frame(self.handle(device, d.build_brt_read(1))[0][1], 1)["raw"]
        self.assertEqual(self.handle(device, d.build_brt_zero(1)), [(1, bytes([4, 1, 6, 0]))])
        self.assertEqual(raw(), 0)
        self.handle(device, d.build_brt_midpoint(1))
        self.assertEqual(raw(), 2048)
        self.handle(device, d.build_brt_set_count(1, 321))
        self.assertEqual(raw(), 321)

    def test_direction_flip_mirrors_the_reading(self) -> None:
        device = SimulatedEncoder(1, motion=lambda now: 100.0 * now, noise_counts=0)
        raw = lambda now: d.decode_brt_frame(device.handle(1, d.build_brt_read(1), now)[0][1], 1)["raw"]
        self.assertEqual(raw(2.0), 200)
        device.handle(1, d.build_brt_direction(1, True), 2.0)
        self.assertEqual(raw(2.0), 4096 - 200)
        self.assertEqual(raw(3.0), 4096 - 300)

    def test_other_addresses_and_malformed_frames_get_no_reply(self) -> None:
        device = self.make()
        self.assertEqual(device.handle(2, d.build_brt_read(2), 1.0), [])
        self.assertEqual(device.handle(1, bytes([7, 1, 1, 0]), 1.0), [])
        self.assertEqual(device.handle(1, d.build_brt_read(2), 1.0), [])

    def test_unknown_function_is_answered_with_a_failure_status(self) -> None:
        reply = self.handle(self.make(), bytes([4, 1, 0x7E, 0]))
        self.assertEqual(reply, [(1, bytes([4, 1, 0x7E, 0xFF]))])

    def test_address_change_answers_from_the_new_id(self) -> None:
        device = self.make()
        self.assertEqual(self.handle(device, d.build_brt_set_address(1, 9)), [(9, bytes([4, 9, 2, 0]))])
        self.assertEqual(device.handle(1, d.build_brt_read(1), 1.0), [])
        self.assertEqual(len(device.handle(9, d.build_brt_read(9), 1.0)), 1)

    def test_auto_report_streams_at_the_configured_period_and_stops(self) -> None:
        device = self.make()
        self.handle(device, d.build_brt_period(1, 5000), 0.0)
        self.handle(device, d.build_brt_report_mode(1, True), 0.0)
        frames = device.due_reports(0.0255)
        self.assertEqual(len(frames), 6)                            # t = 0, 5, 10, 15, 20, 25 ms
        self.assertEqual([round(stamp, 4) for stamp, _ in frames], [0.0, 0.005, 0.01, 0.015, 0.02, 0.025])
        self.handle(device, d.build_brt_report_mode(1, False), 0.02)
        self.assertEqual(device.due_reports(1.0), [])

    def test_other_queries_report_device_values(self) -> None:
        device = SimulatedEncoder(1, motion=lambda now: 4096.0 * 2.5, noise_counts=0)
        multi = d.decode_brt_frame(device.handle(1, d.build_brt_read(1, d.BRT_FUNC_MULTITURN), 1.0)[0][1], 1)
        turns = d.decode_brt_frame(device.handle(1, d.build_brt_read(1, d.BRT_FUNC_TURNS), 1.0)[0][1], 1)
        self.assertEqual((multi["unsigned"], turns["unsigned"]), (10240, 2))

    def test_default_motion_alternates_standing_and_turning(self) -> None:
        self.assertEqual(default_motion(1.0), default_motion(4.9))
        self.assertGreater(default_motion(7.5), default_motion(5.0))
        self.assertEqual(default_motion(10.0), 3072.0)

    def test_bus_adapter_delivers_replies_and_reports(self) -> None:
        bus = SimulatedEncoderBus(self.make())
        bus.send(can.Message(arbitration_id=1, data=d.build_brt_read(1), is_extended_id=False))
        message = bus.recv(timeout=0.5)
        self.assertEqual(bytes(message.data), bytes.fromhex("07 01 01 00 04 00 00"))
        self.assertIsNone(bus.recv(timeout=0.02))
        bus.send(can.Message(arbitration_id=1, data=d.build_brt_report_mode(1, True), is_extended_id=False))
        bus.recv(timeout=0.5)                                       # the acknowledgement
        self.assertIsNotNone(bus.recv(timeout=0.5))                 # then a position frame


class RecordingBus(SimulatedEncoderBus):
    def __init__(self, device: SimulatedEncoder | None = None) -> None:
        super().__init__(device or SimulatedEncoder(1, motion=still(1000.0), noise_counts=0))
        self.sent: list[bytes] = []

    def send(self, message, timeout=None) -> None:
        self.sent.append(bytes(message.data))
        super().send(message, timeout)


class ScriptedBus:
    """Feeds fixed frames and records what the service sends."""

    def __init__(self, frames=()) -> None:
        self.frames = deque(frames)
        self.sent: list[bytes] = []

    def send(self, message, timeout=None) -> None:
        self.sent.append(bytes(message.data))

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.popleft()
        time.sleep(0.005)
        return None

    def shutdown(self) -> None:
        return None


def message(frame_id: int, data: bytes, extended: bool = False, stamp: float | None = None) -> can.Message:
    return can.Message(arbitration_id=frame_id, data=data, is_extended_id=extended,
                       timestamp=time.time() if stamp is None else stamp)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = RecordingBus()
        self.service = EncoderService(bus_factory=lambda channel, bitrate: self.bus, ack_timeout_s=0.4)
        self.addCleanup(self.service.disconnect)

    def connect(self, **extra) -> dict:
        return self.service.connect({"channel": "TEST", "bitrate": 500000, "address": 1, **extra})

    def snapshot(self) -> dict:
        return self.service.snapshot()

    def samples(self) -> int:
        return self.snapshot()["stream"]["samples"]


class ConnectionTest(ServiceTestBase):
    def test_connecting_sends_nothing(self) -> None:
        result = self.connect()
        self.assertTrue(result["ok"], result)
        time.sleep(0.15)
        self.assertEqual(self.bus.sent, [])
        snapshot = self.snapshot()
        self.assertTrue(snapshot["connection"]["connected"])
        self.assertEqual((snapshot["connection"]["channel"], snapshot["connection"]["address"]), ("TEST", 1))
        self.assertIsNone(snapshot["reading"])

    def test_invalid_configurations_are_refused_with_a_reason(self) -> None:
        cases = [({"channel": "", "bitrate": 500000, "address": 1}, "PCAN 通道"),
                 ({"channel": "X", "bitrate": 123456, "address": 1}, "位率"),
                 ({"channel": "X", "bitrate": 500000, "address": 0}, "节点地址"),
                 ({"channel": "X", "bitrate": 500000, "address": 256}, "节点地址"),
                 ({"channel": "X", "bitrate": 500000, "address": True}, "节点地址"),
                 ({"channel": "X", "bitrate": "fast", "address": 1}, "位率")]
        for config, phrase in cases:
            result = self.service.connect(config)
            self.assertFalse(result["ok"], config)
            self.assertIn(phrase, result["error"])
        self.assertFalse(self.service.connect("nope")["ok"])
        self.assertFalse(self.snapshot()["connection"]["connected"])

    def test_release_builds_refuse_the_preview(self) -> None:
        result = self.service.connect({"preview": True})
        self.assertFalse(result["ok"])
        self.assertIn("发布版本", result["error"])

    def test_preview_runs_on_the_simulated_encoder_without_a_bus(self) -> None:
        service = EncoderService(allow_preview=True)
        self.addCleanup(service.disconnect)
        self.assertTrue(service.connect({"preview": True, "address": 3})["ok"])
        self.assertEqual(service.snapshot()["connection"]["mode"], "preview")
        self.assertTrue(service.command("read_position")["ok"])

    def test_bus_open_failure_is_reported_and_leaves_the_service_disconnected(self) -> None:
        def failing(channel, bitrate):
            raise OSError("adapter busy")

        service = EncoderService(bus_factory=failing)
        result = service.connect({"channel": "PCAN_USBBUS1", "bitrate": 500000, "address": 1})
        self.assertFalse(result["ok"])
        self.assertIn("adapter busy", result["error"])
        self.assertEqual(service.snapshot()["connection"]["state"], "disconnected")

    def test_the_configured_bitrate_reaches_the_bus_factory(self) -> None:
        seen = []
        service = EncoderService(bus_factory=lambda channel, bitrate: seen.append((channel, bitrate)) or self.bus)
        self.addCleanup(service.disconnect)
        service.connect({"channel": "PCAN_USBBUS2", "bitrate": 125000, "address": 7})
        self.assertEqual(seen, [("PCAN_USBBUS2", 125000)])

    def test_commands_need_a_connection(self) -> None:
        for name in ("read_position", "poll_start", "report_start"):
            result = self.service.command(name, {"hz": 10})
            self.assertFalse(result["ok"], name)
            self.assertIn("未连接", result["error"])

    def test_disconnect_stops_everything_and_commands_fail_afterwards(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 3))
        self.service.disconnect()
        count = len(self.bus.sent)
        time.sleep(0.1)
        self.assertEqual(len(self.bus.sent), count)
        self.assertFalse(self.service.command("read_position")["ok"])
        self.assertEqual(self.snapshot()["connection"]["state"], "disconnected")

    def test_reconnecting_starts_a_clean_session(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 3))
        epoch = self.service.series(0, -1)["epoch"]
        self.connect()
        self.assertEqual(self.samples(), 0)
        self.assertNotEqual(self.service.series(0, epoch)["epoch"], epoch)
        self.assertFalse(self.snapshot()["stream"]["polling"])

    def test_a_failing_receive_marks_the_connection_as_broken(self) -> None:
        class Broken(ScriptedBus):
            def recv(self, timeout=None):
                raise OSError("usb unplugged")

        service = EncoderService(bus_factory=lambda channel, bitrate: Broken())
        self.addCleanup(service.disconnect)
        service.connect({"channel": "X", "bitrate": 500000, "address": 1})
        self.assertTrue(wait_until(lambda: service.snapshot()["connection"]["state"] == "error"))
        self.assertIn("usb unplugged", service.snapshot()["connection"]["error"])
        self.assertFalse(service.snapshot()["connection"]["connected"])


class ReceiveTest(unittest.TestCase):
    def run_frames(self, frames) -> tuple[EncoderService, ScriptedBus]:
        bus = ScriptedBus(frames)
        service = EncoderService(bus_factory=lambda channel, bitrate: bus)
        self.addCleanup(service.disconnect)
        service.connect({"channel": "X", "bitrate": 500000, "address": 1})
        self.assertTrue(wait_until(lambda: not bus.frames))
        time.sleep(0.05)
        return service, bus

    def position(self, raw: int, stamp: float | None = None) -> can.Message:
        return message(1, bytes([7, 1, 1]) + raw.to_bytes(4, "little"), stamp=stamp)

    def test_valid_positions_become_samples_with_unwrapping_and_speed(self) -> None:
        base = 5000.0
        frames = [self.position(raw, base + index * 0.01) for index, raw in enumerate([4090, 4094, 3, 10])]
        service, _ = self.run_frames(frames)
        series = service.series(0, -1)
        self.assertEqual(series["raw"], [4090, 4094, 3, 10])
        self.assertEqual(series["position"], [4090, 4094, 4099, 4106])
        self.assertIsNone(series["speed"][0])
        self.assertAlmostEqual(series["speed"][3], (4106 - 4090) * 360 / 4096 / 0.03, places=2)
        reading = service.snapshot()["reading"]
        self.assertEqual(reading["raw"], 10)
        self.assertAlmostEqual(reading["angle_deg"], 10 * 360 / 4096, places=3)
        self.assertEqual(reading["motion"], "increasing")

    def test_rejected_frames_are_counted_and_never_become_samples(self) -> None:
        frames = [
            self.position(1024),
            message(1, bytes([7, 1, 1, 0x00, 0x10, 0, 0])),            # 4096: out of range
            message(1, bytes([6, 1, 1, 0, 4, 0, 0])),                 # LEN disagrees with DLC
            message(1, bytes([7, 2, 1, 0, 4, 0, 0])),                 # ADDR byte of another node
            message(1, bytes([6, 1, 1, 0, 4, 0])),                    # consistent but unknown layout
        ]
        service, _ = self.run_frames(frames)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["stream"]["samples"], 1)
        self.assertEqual(snapshot["stream"]["rejected"], 4)
        self.assertIn("未识别", snapshot["stream"]["last_reject"])
        self.assertEqual(snapshot["reading"]["raw"], 1024)

    def test_frames_from_other_ids_only_show_up_in_the_census(self) -> None:
        frames = [message(0x123, bytes(8)), message(0x123, bytes(8)), message(1, bytes([7, 1, 1, 0, 4, 0, 0]), extended=True),
                  self.position(100)]
        service, _ = self.run_frames(frames)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["stream"]["samples"], 1)
        self.assertEqual(snapshot["stats"]["rx_other"], 3)
        census = {(item["id"], item["extended"]): item for item in snapshot["frames"]}
        self.assertEqual(census[(0x123, False)]["count"], 2)
        self.assertFalse(census[(0x123, False)]["mine"])
        self.assertEqual(census[(1, False)]["kind"], "position")
        self.assertTrue(census[(1, False)]["mine"])
        self.assertEqual(snapshot["frames"][0]["mine"], True)       # the encoder's own frames are listed first

    def test_stale_reading_keeps_its_age_instead_of_inventing_data(self) -> None:
        service, _ = self.run_frames([self.position(500)])
        time.sleep(0.2)
        reading = service.snapshot()["reading"]
        self.assertEqual(reading["raw"], 500)
        self.assertGreaterEqual(reading["age"], 0.2)

    def test_a_device_value_answer_is_remembered_with_both_readings(self) -> None:
        negative = bytes([7, 1, d.BRT_FUNC_SPEED]) + (-40 & 0xFFFFFFFF).to_bytes(4, "little")
        service, _ = self.run_frames([message(1, negative)])
        speed = service.snapshot()["device"]["speed"]
        self.assertEqual((speed["signed"], speed["unsigned"]), (-40, 0xFFFFFFD8))

    def test_snapshot_is_json_serialisable(self) -> None:
        service, _ = self.run_frames([self.position(1), message(9, bytes(3)), message(1, bytes([6, 1, 1, 0, 4, 0]))])
        json.dumps(service.snapshot())
        json.dumps(service.series(0, -1))


class CollectionTest(ServiceTestBase):
    def test_polling_collects_samples_and_reports_its_rate(self) -> None:
        self.connect()
        self.assertTrue(self.service.command("poll_start", {"hz": 100})["ok"])
        self.assertTrue(wait_until(lambda: self.samples() > 40))
        snapshot = self.snapshot()
        self.assertTrue(snapshot["stream"]["polling"])
        self.assertEqual(snapshot["stream"]["poll_hz"], 100)
        self.assertEqual(snapshot["reading"]["raw"], int(1000 % 4096))
        self.assertEqual(snapshot["reading"]["motion"], "still")
        time.sleep(1.1)
        stream = self.snapshot()["stream"]
        self.assertGreater(stream["hz"], 50)
        self.assertLess(stream["hz"], 130)
        self.assertAlmostEqual(stream["interval_ms"], 10.0, delta=3.0)
        self.assertTrue(self.service.command("poll_stop")["ok"])
        sent = len(self.bus.sent)
        time.sleep(0.1)
        self.assertEqual(len(self.bus.sent), sent)
        self.assertFalse(self.snapshot()["stream"]["polling"])

    def test_poll_rates_are_limited_to_the_offered_choices(self) -> None:
        self.connect()
        for hz in (0, 7, 1000, "fast", True):
            self.assertFalse(self.service.command("poll_start", {"hz": hz})["ok"], hz)
        self.assertFalse(self.snapshot()["stream"]["polling"])
        self.assertEqual(self.bus.sent, [])

    def test_auto_report_streams_without_requests_and_is_acknowledged(self) -> None:
        self.connect()
        result = self.service.command("report_start")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.bus.sent, [bytes.fromhex("04 01 04 aa")])
        self.assertTrue(wait_until(lambda: self.samples() > 40))
        stream = self.snapshot()["stream"]
        self.assertTrue(stream["report_on"])
        self.assertIsNotNone(stream["silent_s"])
        self.assertTrue(self.service.command("report_stop")["ok"])
        self.assertEqual(self.bus.sent[-1], bytes.fromhex("04 01 04 00"))
        self.assertFalse(self.snapshot()["stream"]["report_on"])

    def test_starting_a_poll_turns_the_device_report_off_first(self) -> None:
        self.connect()
        self.service.command("report_start")
        self.assertTrue(self.service.command("poll_start", {"hz": 20})["ok"])
        self.assertEqual(self.bus.sent[:2], [bytes.fromhex("04 01 04 aa"), bytes.fromhex("04 01 04 00")])
        self.assertTrue(wait_until(lambda: self.bus.sent.count(bytes.fromhex("04 01 01 00")) >= 2))
        snapshot = self.snapshot()
        self.assertTrue(snapshot["stream"]["polling"])
        self.assertFalse(snapshot["stream"]["report_on"])

    def test_starting_the_report_stops_the_poll(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 50})
        self.assertTrue(wait_until(lambda: self.samples() > 2))
        self.service.command("report_start")
        self.assertFalse(self.snapshot()["stream"]["polling"])

    def test_poll_stops_after_repeated_send_failures(self) -> None:
        class Failing(RecordingBus):
            def send(self, message, timeout=None) -> None:
                if bytes(message.data) == bytes.fromhex("04 01 01 00"):
                    raise can.CanOperationError("tx queue full")
                super().send(message, timeout)

        self.bus = Failing()
        self.connect()
        self.service.command("poll_start", {"hz": 200})
        self.assertTrue(wait_until(lambda: not self.snapshot()["stream"]["polling"]))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["stats"]["tx_errors"], 10)
        self.assertTrue(any("已停止轮询" in event["text"] for event in snapshot["events"]))

    def test_curves_fetch_by_cursor_and_clear_starts_a_new_epoch(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 10))
        first = self.service.series(0, -1)
        self.assertTrue(first["reset"])
        self.assertTrue(wait_until(lambda: self.samples() > first["cursor"] + 5))
        second = self.service.series(first["cursor"], first["epoch"])
        self.assertFalse(second["reset"])
        self.assertEqual(second["cursor"], first["cursor"] + len(second["t"]))
        self.assertTrue(self.service.command("clear_curves")["ok"])
        cleared = self.service.series(second["cursor"], second["epoch"])
        self.assertTrue(cleared["reset"])
        self.assertNotEqual(cleared["epoch"], first["epoch"])

    def test_export_requires_samples_and_names_every_column(self) -> None:
        self.connect()
        self.assertFalse(self.service.export_csv()["ok"])
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 5))
        exported = self.service.export_csv()
        self.assertTrue(exported["ok"])
        lines = exported["data"].strip().splitlines()
        self.assertEqual(lines[0], "time_s,raw_count,angle_deg,accumulated_deg,speed_rpm")
        self.assertEqual(len(lines) - 1, exported["samples"])


class QueryTest(ServiceTestBase):
    def test_reads_return_the_device_values(self) -> None:
        self.bus = RecordingBus(SimulatedEncoder(1, motion=lambda now: 4096.0 * 2 + 1024.0, noise_counts=0))
        self.connect()
        position = self.service.command("read_position")
        self.assertEqual((position["raw"], position["angle_deg"]), (1024, 90.0))
        self.assertEqual(self.service.command("read_multiturn")["unsigned"], 4096 * 2 + 1024)
        self.assertEqual(self.service.command("read_turns")["unsigned"], 2)
        self.assertEqual(self.service.command("read_speed", {"sample_ms": 100})["count"], 0)
        speed = self.snapshot()["device"]
        self.assertEqual(speed["multiturn"]["unsigned"], 4096 * 2 + 1024)
        self.assertEqual(speed["speed"]["sample_ms"], 100)
        self.assertEqual(speed["speed"]["rpm"], 0.0)

    def test_speed_is_converted_with_the_given_sample_time(self) -> None:
        self.bus = RecordingBus(SimulatedEncoder(1, motion=lambda now: 40960.0 * now, noise_counts=0))
        self.connect()
        result = self.service.command("read_speed", {"sample_ms": 100})
        self.assertGreater(result["count"], 0)
        self.assertAlmostEqual(result["rpm"], d.brt_speed_rpm(result["count"], 100), places=3)
        self.assertFalse(self.service.command("read_speed", {"sample_ms": 0})["ok"])

    def test_a_silent_device_gives_a_clear_timeout(self) -> None:
        self.bus.device.stall_ack = True
        self.connect()
        started = time.monotonic()
        result = self.service.command("read_position")
        self.assertFalse(result["ok"])
        self.assertIn("无应答", result["error"])
        self.assertLess(time.monotonic() - started, 1.5)


class WriteTest(ServiceTestBase):
    def test_every_write_is_refused_without_acknowledgement(self) -> None:
        self.connect()
        writes = {"set_period": {"period_us": 5000}, "set_zero": {}, "set_midpoint": {}, "set_count": {"count": 5},
                  "set_direction": {"counter_clockwise": True}, "set_speed_time": {"sample_ms": 50},
                  "set_address": {"address": 2}, "set_bitrate": {"bitrate": 250000}}
        self.assertEqual(set(writes), {name for name, (tier, _) in EncoderService._COMMANDS.items() if tier == "write"})
        for name, args in writes.items():
            for flag in (False, None, 1, "yes"):
                result = self.service.command(name, args, flag)
                self.assertFalse(result["ok"], (name, flag))
                self.assertIn("确认", result["error"])
        self.assertEqual(self.bus.sent, [])
        self.assertEqual(self.snapshot()["applied"], [])

    def test_reads_and_stream_switches_do_not_need_acknowledgement(self) -> None:
        for name, (tier, _) in EncoderService._COMMANDS.items():
            if tier != "write":
                self.assertIn(tier, {"read", "stream", "local"}, name)

    def test_zero_is_acknowledged_then_read_back(self) -> None:
        self.connect()
        result = self.service.command("set_zero", acknowledged=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.bus.sent[0], bytes.fromhex("04 01 06 00"))
        self.assertEqual(self.bus.sent[1], bytes.fromhex("04 01 01 00"))        # the read-back query
        self.assertTrue(result["verify"]["ok"])
        self.assertEqual(result["verify"]["actual"], 0)
        record = self.snapshot()["applied"][0]
        self.assertEqual((record["key"], record["verified"]), ("zero", True))
        self.assertIn("读回", record["detail"])

    def test_midpoint_and_count_are_verified_against_their_targets(self) -> None:
        self.connect()
        mid = self.service.command("set_midpoint", acknowledged=True)
        self.assertEqual((mid["verify"]["ok"], mid["verify"]["actual"], mid["verify"]["expected"]), (True, 2048, 2048))
        count = self.service.command("set_count", {"count": 4000}, True)
        self.assertEqual((count["verify"]["ok"], count["verify"]["actual"]), (True, 4000))
        self.assertEqual(self.bus.sent[-2], bytes.fromhex("07 01 0d a0 0f 00 00"))
        self.assertFalse(self.service.command("set_count", {"count": 4096}, True)["ok"])

    def test_a_device_that_acknowledges_but_does_not_apply_is_caught_by_the_read_back(self) -> None:
        class Ignoring(SimulatedEncoder):
            def handle(self, can_id, data, now):
                if len(data) > 2 and data[2] == d.BRT_FUNC_ZERO:
                    return [(self.address, bytes([4, self.address, d.BRT_FUNC_ZERO, 0]))]
                return super().handle(can_id, data, now)

        self.bus = RecordingBus(Ignoring(1, motion=still(1000.0), noise_counts=0))
        self.connect()
        result = self.service.command("set_zero", acknowledged=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["verify"]["ok"])
        self.assertEqual(result["verify"]["actual"], 1000)
        self.assertIn("偏离", result["message"])
        self.assertIs(self.snapshot()["applied"][0]["verified"], False)

    def test_moving_shaft_is_reported_as_a_read_back_mismatch(self) -> None:
        self.bus = RecordingBus(SimulatedEncoder(1, motion=lambda now: 4000.0 * now, noise_counts=0))
        self.connect()
        result = self.service.command("set_zero", acknowledged=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["verify"]["ok"])
        self.assertIn("转动", result["message"])

    def test_reference_changes_restart_unwrapping_and_leave_a_curve_marker(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 5))
        self.service.command("set_zero", acknowledged=True)
        markers = self.snapshot()["markers"]
        self.assertEqual([marker["text"] for marker in markers], ["置零"])
        self.assertTrue(wait_until(lambda: self.service.series(0, -1)["raw"][-1] <= 3))

    def test_period_is_written_little_endian_and_recorded(self) -> None:
        self.connect()
        result = self.service.command("set_period", {"period_us": 6000}, True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.bus.sent[0], bytes.fromhex("05 01 05 70 17"))
        self.assertEqual(self.bus.device.period_us, 6000)
        record = self.snapshot()["applied"][0]
        self.assertEqual((record["key"], record["value"]), ("period", "6000 µs"))
        sent = len(self.bus.sent)
        for bad in (4999, 1000, 50, 65536, "x", None):
            refused = self.service.command("set_period", {"period_us": bad}, True)
            self.assertFalse(refused["ok"], bad)
        self.assertEqual(len(self.bus.sent), sent)                # nothing shorter than 5 ms ever leaves the host
        self.assertIn("5 ms", self.service.command("set_period", {"period_us": 1000}, True)["error"])

    def test_direction_records_the_reading_before_and_after(self) -> None:
        self.bus = RecordingBus(SimulatedEncoder(1, motion=still(300.0), noise_counts=0))
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 3))
        result = self.service.command("set_direction", {"counter_clockwise": True}, True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["before"], 300)
        self.assertEqual(result["after"], 4096 - 300)
        self.assertIn("逆时针", result["message"])
        self.assertIn(bytes.fromhex("04 01 07 01"), self.bus.sent)

    def test_speed_time_is_written_and_marked_unverified(self) -> None:
        self.connect()
        self.assertTrue(self.service.command("set_speed_time", {"sample_ms": 250}, True)["ok"])
        self.assertEqual(self.bus.sent[0], bytes.fromhex("05 01 0b fa 00"))
        self.assertEqual(self.bus.device.speed_time_ms, 250)
        record = self.snapshot()["applied"][0]
        self.assertIsNone(record["verified"])
        self.assertIn("字节序", record["detail"])

    def test_a_refusing_device_is_an_error_not_a_success(self) -> None:
        class Refusing(SimulatedEncoder):
            def handle(self, can_id, data, now):
                if len(data) > 2 and data[2] == d.BRT_FUNC_MIDPOINT:
                    return [(self.address, bytes([4, self.address, d.BRT_FUNC_MIDPOINT, 0x03]))]
                return super().handle(can_id, data, now)

        self.bus = RecordingBus(Refusing(1, motion=still(), noise_counts=0))
        self.connect()
        result = self.service.command("set_midpoint", acknowledged=True)
        self.assertFalse(result["ok"])
        self.assertIn("0x03", result["error"])
        self.assertEqual(self.snapshot()["applied"], [])

    def test_a_write_without_an_answer_records_nothing(self) -> None:
        self.bus.device.stall_ack = True
        self.connect()
        result = self.service.command("set_zero", acknowledged=True)
        self.assertFalse(result["ok"])
        self.assertIn("无应答", result["error"])
        self.assertEqual(self.snapshot()["applied"], [])
        self.assertEqual(self.snapshot()["markers"], [])

    def test_address_change_follows_the_new_id_and_ignores_the_old_one(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 3))
        self.service.command("poll_stop")
        result = self.service.command("set_address", {"address": "0x09"}, True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["address"], 9)
        write = self.bus.sent.index(bytes.fromhex("04 01 02 09"))
        self.assertEqual(self.snapshot()["connection"]["address"], 9)
        self.assertEqual(self.bus.sent[write + 1], bytes.fromhex("04 09 01 00"))  # read-back at the new address
        self.assertTrue(self.service.command("read_position")["ok"])
        self.assertEqual(self.snapshot()["applied"][0]["key"], "address")
        self.assertFalse(self.service.command("set_address", {"address": 9}, True)["ok"])   # unchanged
        self.assertFalse(self.service.command("set_address", {"address": 0}, True)["ok"])
        self.assertEqual(self.snapshot()["frames"][0]["id"], 9)

    def test_failed_address_change_keeps_the_old_address(self) -> None:
        self.bus.device.stall_ack = True
        self.connect()
        result = self.service.command("set_address", {"address": 7}, True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.snapshot()["connection"]["address"], 1)
        self.bus.device.stall_ack = False
        self.assertTrue(self.service.command("read_position")["ok"])

    def test_bitrate_change_stops_polling_and_asks_for_a_reconnect(self) -> None:
        self.connect()
        self.service.command("poll_start", {"hz": 100})
        self.assertTrue(wait_until(lambda: self.samples() > 3))
        result = self.service.command("set_bitrate", {"bitrate": 250000}, True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reconnect_bitrate"], 250000)
        self.assertEqual(self.bus.device.pending_bitrate, 250000)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["connection"]["pending_bitrate"], 250000)
        self.assertFalse(snapshot["stream"]["polling"])
        self.assertEqual(snapshot["applied"][0]["key"], "bitrate")
        self.assertFalse(self.service.command("set_bitrate", {"bitrate": 500000}, True)["ok"])   # same as connected
        self.assertFalse(self.service.command("set_bitrate", {"bitrate": 99999}, True)["ok"])

    def test_events_record_the_frames_that_were_sent(self) -> None:
        self.connect()
        self.service.command("set_zero", acknowledged=True)
        texts = [event["text"] for event in self.snapshot()["events"]]
        self.assertTrue(any("04 01 06 00" in text and "置零" in text for text in texts))
        self.assertTrue(any("已应答" in text for text in texts))
        self.assertTrue(self.service.command("clear_log")["ok"])
        self.assertEqual(self.snapshot()["events"], [])


class CommandSurfaceTest(ServiceTestBase):
    def test_unknown_commands_and_bad_arguments_return_errors_not_exceptions(self) -> None:
        self.connect()
        self.assertFalse(self.service.command("format_disk")["ok"])
        self.assertFalse(self.service.command("poll_start", None)["ok"])
        self.assertFalse(self.service.command("set_count", {"count": None}, True)["ok"])
        self.assertFalse(self.service.command("set_address", {"address": [1]}, True)["ok"])

    def test_numeric_arguments_accept_integral_floats_and_numeric_text(self) -> None:
        self.connect()
        self.assertTrue(self.service.command("poll_start", {"hz": 20.0})["ok"])
        self.assertTrue(self.service.command("set_count", {"count": "0x10"}, True)["ok"])
        self.assertFalse(self.service.command("poll_start", {"hz": 20.5})["ok"])

    def test_catalog_lists_what_the_page_renders_from(self) -> None:
        catalog = encoder_catalog()
        json.dumps(catalog)
        self.assertEqual(catalog["resolution"], 4096)
        self.assertEqual(catalog["bitrates"], [100000, 125000, 250000, 500000, 1000000])
        self.assertEqual(catalog["period_us_range"], [5000, 65535])
        self.assertIn(50, catalog["poll_rates_hz"])


class ApiTest(unittest.TestCase):
    def test_bootstrap_and_round_trip_in_a_source_run(self) -> None:
        api = Api()
        try:
            bootstrap = api.bootstrap()
            self.assertTrue(bootstrap["encoder_enabled"])
            self.assertTrue(bootstrap["encoder_preview"])
            self.assertEqual(bootstrap["encoder"]["resolution"], 4096)
            self.assertTrue(api.connect_encoder({"preview": True, "address": 1})["ok"])
            self.assertTrue(api.encoder_command("poll_start", {"hz": 50})["ok"])
            self.assertTrue(wait_until(lambda: api.get_encoder_snapshot()["stream"]["samples"] > 3))
            self.assertTrue(api.get_encoder_series(0, -1)["ok"])
            refused = api.encoder_command("set_zero", {})
            self.assertFalse(refused["ok"])
            self.assertTrue(api.disconnect_encoder()["ok"])
            self.assertEqual(api.get_encoder_snapshot()["connection"]["state"], "disconnected")
        finally:
            api.close()

    def test_release_build_has_no_preview_even_if_asked(self) -> None:
        with patch("canhost.app._simulation_available", return_value=False):
            api = Api()
        try:
            self.assertFalse(api.bootstrap()["encoder_preview"])
            result = api.connect_encoder({"preview": True})
            self.assertFalse(result["ok"])
            self.assertEqual(api.get_encoder_snapshot()["connection"]["state"], "disconnected")
        finally:
            api.close()

    def test_closing_the_api_releases_the_encoder_connection(self) -> None:
        api = Api()
        api.connect_encoder({"preview": True})
        api.close()
        self.assertEqual(api.get_encoder_snapshot()["connection"]["state"], "disconnected")

    def test_export_needs_a_window(self) -> None:
        api = Api()
        try:
            self.assertFalse(api.choose_export_encoder_csv()["ok"])
        finally:
            api.close()


class ConcurrencyTest(ServiceTestBase):
    def test_commands_from_several_threads_are_serialised(self) -> None:
        self.connect()
        results: list[dict] = []

        def worker() -> None:
            results.append(self.service.command("read_position"))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result["ok"] for result in results), results)


if __name__ == "__main__":
    unittest.main()
