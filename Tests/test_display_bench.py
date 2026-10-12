"""Driver-display bench: value model, scenarios, drive cycle and the PCAN service."""

from __future__ import annotations

import collections
import itertools
import json
import threading
import time
import unittest
from unittest.mock import patch

import can

from canhost import decoders as d
from canhost.display import model
from canhost.display.cycle import PERIOD_S, DriveCycle
from canhost.display.service import DisplayBenchService, bench_catalog

_channels = itertools.count()


def wait_until(condition, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ModelTest(unittest.TestCase):
    def test_defaults_build_every_frame_within_classic_can_limits(self) -> None:
        values = model.default_values()
        controls = model.default_controls()
        self.assertEqual(len(model.FRAMES), 19)
        for spec in model.FRAMES:
            payload = model.build_frame(spec, values, 0)
            self.assertGreaterEqual(len(payload), spec.min_dlc, spec.key)
            self.assertLessEqual(len(payload), 8, spec.key)
            self.assertGreater(model.frame_period_ms(spec, controls), 0)

    def test_frame_ids_match_the_display_filter_banks(self) -> None:
        # HMI_Display can.c CAN1 filter banks 0..5 (the 0x4A0/0x4A3 bank lists each twice).
        accepted = {0x4B0, 0x305, 0x502, 0x505, 0x506, 0x509, 0x508, 0x507, 0x503, 0x504, 0x5A0, 0x5A1,
                    0x4A0, 0x4A3, 0x5A2, 0x071, 0x072, 0x073, 0x074}
        self.assertEqual(set(model.FRAME_BY_ID), accepted)

    def test_values_are_clamped_and_typed(self) -> None:
        self.assertEqual(model.normalize_value("bms.soc_pct", 300), 255)
        self.assertEqual(model.normalize_value("bms.soc_pct", 76.6), 77)
        self.assertEqual(model.normalize_value("pedal.throttle_pct", -5), 0.0)
        self.assertEqual(model.normalize_value("wheel.fl.fault", "0x0419"), 0x419)
        self.assertEqual(model.normalize_value("wheel.fl.fault", "ffffffffff"), 0xFFFFFFFF)
        self.assertIs(model.normalize_value("pdm.offline", "on"), True)
        self.assertEqual(model.normalize_value("ecu.mode", -20), -8)
        for key, raw in (("nope.key", 1), ("bms.voltage_v", "abc"), ("bms.voltage_v", float("nan")),
                         ("wheel.fl.fault", "zz"), ("bms.soc_pct", True)):
            with self.assertRaises((KeyError, ValueError), msg=f"{key}={raw!r}"):
                model.normalize_value(key, raw)

    def test_brake_bar_matches_the_firmware_oil_pressure_table(self) -> None:
        # firmware_regression.c: raw oil pressure -> brake percent.
        for raw, percent in ((0, 0), (2099, 0), (2100, 0), (3500, 45), (4900, 90), (5200, 99),
                             (5300, 100), (65535, 100)):
            self.assertEqual(model.brake_percent(raw / 1000.0), percent, raw)

    def test_derived_readouts_follow_the_display_rules(self) -> None:
        values = model.default_values()
        values.update({"bms.voltage_v": 549.1, "bms.current_a": -85.7, "pedal.throttle_pct": 50.0,
                       "wheel.fl.torque_nm": 20, "wheel.fl.rpm": 9549, "wheel.fl.motor_c": 72.9,
                       "pdm.bus_v": 24.0, "pdm.bus_a": 1.5})
        derived = model.derive(values)
        self.assertEqual(derived["hv_power_kw"], 47.1)
        self.assertEqual(derived["throttle_pct"], 50)
        self.assertEqual(derived["wheel_power_kw"]["fl"], 20.0)
        self.assertEqual(derived["tmax_c"], 72)          # whole degrees, truncated like the firmware
        self.assertEqual(derived["pdm_power_w"], 36.0)
        values["bms.current_a"] = 12.0                    # charging is not drive power
        self.assertEqual(model.derive(values)["hv_power_kw"], 0.0)

    def test_pdm_power_follows_auto_or_manual(self) -> None:
        values = model.default_values()
        values.update({"pdm.bus_v": 24.0, "pdm.bus_a": 2.0, "pdm.bus_w": 50.0})
        self.assertEqual(model.pdm_power_w(values), 48.0)
        values["pdm.power_auto"] = False
        self.assertEqual(model.pdm_power_w(values), 50.0)

    def test_tyre_frames_follow_the_display_wheel_assignment(self) -> None:
        values = model.default_values()
        for wheel, base in zip(("fr", "fl", "rl", "rr"), (10.0, 20.0, 30.0, 40.0)):
            for point in range(1, 5):
                values[f"tire.{wheel}.t{point}"] = base + point
        for frame_id, base in ((0x071, 10.0), (0x072, 20.0), (0x073, 30.0), (0x074, 40.0)):
            payload = model.build_frame(model.FRAME_BY_ID[frame_id], values, 0)
            self.assertEqual(d.decode_tire_temp_frame(payload), [base + 1, base + 2, base + 3, base + 4])

    def test_defects_break_exactly_what_the_display_checks(self) -> None:
        values = model.default_values()
        sop = model.FRAME_BY_ID[0x4A0]
        self.assertTrue(d.decode_sop_limits(model.build_frame(sop, values, 3), True)["frame_valid"])
        for defect in ("crc", "version", "reserved"):
            self.assertFalse(d.decode_sop_limits(model.build_frame(sop, values, 3, defect), True)["frame_valid"],
                             defect)
        self.assertFalse(d.decode_sop_status(model.build_frame(model.FRAME_BY_ID[0x4A3], values, 0, "version"))
                         ["frame_valid"])
        for spec in model.FRAMES:
            short = model.build_frame(spec, values, 0, "short")
            self.assertEqual(len(short), spec.min_dlc - 1, spec.key)
        # A defect a frame does not support is ignored instead of corrupting it.
        self.assertEqual(model.build_frame(model.FRAME_BY_ID[0x4B0], values, 0, "crc"),
                         model.build_frame(model.FRAME_BY_ID[0x4B0], values, 0))

    def test_every_scenario_applies_cleanly(self) -> None:
        for key, scenario in model.SCENARIOS.items():
            values = model.default_values()
            values.update({name: model.normalize_value(name, raw) for name, raw in scenario.values.items()})
            for spec in model.FRAMES:
                self.assertGreaterEqual(len(model.build_frame(spec, values, 0)), spec.min_dlc, (key, spec.key))

    def test_scenarios_produce_the_conditions_they_describe(self) -> None:
        def values_for(key: str) -> dict:
            values = model.default_values()
            values.update(model.SCENARIOS[key].values)
            return values

        self.assertEqual(model.brake_percent(values_for("brake")["pedal.oil_kpa"]), 90)
        self.assertTrue(d.decode_pdm_side(model.build_frame(model.FRAME_BY_ID[0x5A0], values_for("pdm_offline"), 0))
                        ["offline"])
        self.assertFalse(d.decode_pack_status(model.build_frame(model.FRAME_BY_ID[0x4B0], values_for("no_soc"), 0))
                         ["soc_valid"])
        limits = d.decode_sop_limits(model.build_frame(model.FRAME_BY_ID[0x4A0], values_for("sop_invalid"), 0), True)
        self.assertFalse(limits["limits_valid"])
        self.assertEqual(model.derive(values_for("overheat"))["tmax_c"], 95)
        self.assertTrue(model.SCENARIOS["silence"].paused)

    def test_catalog_is_json_and_covers_every_value(self) -> None:
        catalog = bench_catalog()
        json.dumps(catalog)
        self.assertEqual(set(catalog["values"]), set(model.VALUE_SPECS))
        self.assertEqual([frame["id"] for frame in catalog["frames"]], [spec.key for spec in model.FRAMES])
        self.assertFalse(catalog["values"]["wheel.fl.fault"]["driven"])
        self.assertTrue(catalog["values"]["bms.voltage_v"]["driven"])


class DriveCycleTest(unittest.TestCase):
    def test_two_loops_stay_inside_every_field_range(self) -> None:
        values = model.default_values()
        cycle = DriveCycle(values)
        peak_speed = peak_current = 0.0
        regen_seen = False
        for _ in range(int(2 * PERIOD_S / 0.05)):
            updates = cycle.step(0.05, values)
            for key, raw in updates.items():
                self.assertIn(key, model.VALUE_SPECS)
                self.assertTrue(model.cycle_drives(key), key)
                self.assertAlmostEqual(model.normalize_value(key, raw), raw, places=6, msg=key)
            values.update(updates)
            peak_speed = max(peak_speed, cycle.speed_kmh)
            peak_current = min(peak_current, values["bms.current_a"])
            regen_seen |= values["bms.current_a"] > 5
        self.assertGreater(peak_speed, 60)
        self.assertLess(peak_current, -100)
        self.assertTrue(regen_seen)
        self.assertLess(values["bms.soc_pct"], 76)

    def test_it_never_writes_the_manual_only_fields(self) -> None:
        updates = DriveCycle(model.default_values()).step(0.05, model.default_values())
        self.assertFalse([key for key in updates if not model.cycle_drives(key)])

    def test_torque_follows_the_published_sop(self) -> None:
        def peak_power(sop_kw: float, valid: bool) -> float:
            values = model.default_values()
            values.update({"sop.discharge_kw": sop_kw, "sop.limits_valid": valid, "pedal.throttle_pct": 100.0})
            cycle = DriveCycle(values)
            cycle.t = 5.0
            cycle.speed_mps = 20.0
            updates = cycle.step(0.05, values)
            return sum(updates[f"wheel.{w}.torque_nm"] * updates[f"wheel.{w}.rpm"] for w in model.WHEELS) / 9549.0

        self.assertGreater(peak_power(80.0, True), 40)
        self.assertLess(peak_power(10.0, True), peak_power(80.0, True) / 2)
        self.assertEqual(peak_power(80.0, False), 0.0)

    def test_disabled_wheel_gets_no_torque_and_low_soc_derates_sop(self) -> None:
        values = model.default_values()
        values.update({"wheel.fr.enabled": False, "bms.soc_pct": 10})
        cycle = DriveCycle(values)
        cycle.t = 5.0
        cycle.speed_mps = 15.0
        updates = cycle.step(0.05, values)
        self.assertEqual(updates["wheel.fr.torque_nm"], 0)
        self.assertGreater(updates["wheel.fl.torque_nm"], 0)
        self.assertLess(updates["sop.discharge_kw"], 40.0)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        index = next(_channels)
        self.canb = f"bench-canb-{index}"
        self.wheel = f"bench-wheel-{index}"
        self.buses: list[can.BusABC] = []
        self.service = DisplayBenchService(allow_preview=False, bus_factory=self._open, probe_s=0.05)
        self.addCleanup(self.service.disconnect)
        self.addCleanup(self._close_buses)

    def _open(self, channel: str) -> can.BusABC:
        return can.Bus(interface="virtual", channel=channel, receive_own_messages=False)

    def _peer(self, channel: str) -> can.BusABC:
        bus = self._open(channel)
        self.buses.append(bus)
        return bus

    def _close_buses(self) -> None:
        for bus in self.buses:
            bus.shutdown()

    def _connect(self, **extra) -> dict:
        config = {"channel": self.canb, "confirmed": True}
        config.update(extra)
        result = self.service.connect(config)
        self.assertTrue(result["ok"], result)
        return result

    def _capture(self, peer: can.BusABC, seconds: float) -> collections.Counter:
        counts: collections.Counter = collections.Counter()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            message = peer.recv(timeout=0.02)
            if message is not None:
                counts[message.arbitration_id] += 1
        return counts

    # -- connection rules ---------------------------------------------------

    def test_hardware_connection_needs_confirmation_and_a_channel(self) -> None:
        self.assertFalse(self.service.connect({"channel": self.canb})["ok"])
        self.assertIn("确认", self.service.connect({"channel": self.canb})["error"])
        self.assertFalse(self.service.connect({"confirmed": True})["ok"])
        self.assertFalse(self.service.connect({"channel": "a", "wheel_channel": "a", "confirmed": True})["ok"])
        self.assertFalse(self.service.connect({"channel": self.canb, "confirmed": True, "nodes": []})["ok"])
        self.assertFalse(self.service.connect({"channel": self.canb, "confirmed": True, "nodes": ["xx"]})["ok"])
        self.assertEqual(self.service.snapshot()["connection"]["state"], "disconnected")

    def test_preview_is_refused_unless_allowed_and_never_opens_a_bus(self) -> None:
        refused = self.service.connect({"preview": True})
        self.assertFalse(refused["ok"])
        self.assertIn("发布版本", refused["error"])
        opened: list[str] = []
        preview = DisplayBenchService(allow_preview=True, bus_factory=lambda channel: opened.append(channel))
        self.addCleanup(preview.disconnect)
        self.assertTrue(preview.connect({"preview": True})["ok"])
        self.assertTrue(wait_until(lambda: preview.snapshot()["stats"]["tx_total"] > 20))
        self.assertEqual(opened, [])
        self.assertEqual(preview.snapshot()["connection"]["mode"], "preview")
        self.assertTrue(preview.command("wheel_lap")["ok"])

    def test_open_failure_is_reported_and_leaves_the_service_clean(self) -> None:
        def failing(channel: str):
            raise OSError("PCAN_ERROR_ILLHW")

        service = DisplayBenchService(bus_factory=failing)
        result = service.connect({"channel": "PCAN_USBBUS9", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertIn("PCAN_USBBUS9", result["error"])
        snapshot = service.snapshot()
        self.assertEqual(snapshot["connection"]["state"], "disconnected")
        self.assertEqual(snapshot["connection"]["error"], result["error"])

    def test_connect_aborts_when_a_real_node_is_already_on_the_bus(self) -> None:
        peer = self._peer(self.canb)
        stop = threading.Event()

        def real_bms() -> None:
            while not stop.is_set():
                peer.send(can.Message(arbitration_id=0x4B0, data=bytes(7), is_extended_id=False))
                time.sleep(0.01)

        thread = threading.Thread(target=real_bms, daemon=True)
        thread.start()
        try:
            result = self.service.connect({"channel": self.canb, "confirmed": True})
        finally:
            stop.set()
            thread.join()
        self.assertFalse(result["ok"])
        self.assertIn("0x4B0", result["error"])
        self.assertEqual(self.service.snapshot()["stats"]["tx_total"], 0)

    def test_unchecked_nodes_may_coexist_with_real_ones(self) -> None:
        peer = self._peer(self.canb)
        stop = threading.Event()
        threading.Thread(target=lambda: [peer.send(can.Message(arbitration_id=0x4B0, data=bytes(7),
                                                                is_extended_id=False)) or time.sleep(0.01)
                                         for _ in iter(stop.is_set, True)], daemon=True).start()
        try:
            self._connect(nodes=["ecu", "pdm"])
            counts = self._capture(peer, 0.4)
        finally:
            stop.set()
        self.assertIn(0x5A0, counts)
        self.assertNotIn(0x4A0, counts)
        self.assertFalse(self.service.snapshot()["conflict"])

    # -- transmission -------------------------------------------------------

    def test_every_frame_is_sent_at_its_nominal_rate(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self._capture(peer, 0.2)
        counts = self._capture(peer, 1.0)
        for spec in model.FRAMES:
            nominal = 1000 / model.frame_period_ms(spec, model.default_controls())
            self.assertGreaterEqual(counts[spec.frame_id], max(1, int(nominal * 0.5)), spec.key)
            self.assertLessEqual(counts[spec.frame_id], nominal * 1.5 + 2, spec.key)
        self.assertEqual(set(counts), set(model.FRAME_BY_ID))

    def test_sop_counter_increments_and_payload_follows_values(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertTrue(self.service.set_values({"sop.discharge_kw": 74.0, "sop.regen_kw": 4.0})["ok"])
        sequences = []
        deadline = time.monotonic() + 1.0
        while len(sequences) < 6 and time.monotonic() < deadline:
            message = peer.recv(timeout=0.05)
            if message is not None and message.arbitration_id == 0x4A0:
                decoded = d.decode_sop_limits(bytes(message.data), True)
                self.assertTrue(decoded["frame_valid"])
                if decoded["discharge_power_kw"] == 74.0:
                    sequences.append(decoded["sequence"])
        self.assertGreaterEqual(len(sequences), 3)
        self.assertEqual([(b - a) & 0xFF for a, b in zip(sequences, sequences[1:])], [1] * (len(sequences) - 1))

    def test_pause_node_and_frame_switches_silence_exactly_their_frames(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertTrue(self.service.command("pause", {"paused": True})["ok"])
        self._capture(peer, 0.1)
        self.assertEqual(sum(self._capture(peer, 0.3).values()), 0)
        self.assertEqual(self.service.snapshot()["connection"]["connected"], True)
        self.service.command("pause", {"paused": False})
        self.service.command("node", {"node": "tire", "enabled": False})
        self.service.command("frame", {"frame_id": "0x5A2", "enabled": False})
        self._capture(peer, 0.1)
        counts = self._capture(peer, 0.4)
        self.assertFalse({0x071, 0x072, 0x073, 0x074, 0x5A2} & set(counts))
        self.assertIn(0x5A0, counts)
        sending = {frame["id"] for frame in self.service.snapshot()["frames"] if frame["sending"]}
        self.assertNotIn("0x5A2", sending)
        self.assertIn("0x5A0", sending)

    def test_ecu_period_changes_the_rate(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertTrue(self.service.command("ecu_period", {"ms": 100})["ok"])
        self.assertFalse(self.service.command("ecu_period", {"ms": 7})["ok"])
        self._capture(peer, 0.2)
        counts = self._capture(peer, 1.0)
        self.assertLessEqual(counts[0x502], 14)
        self.assertGreaterEqual(counts[0x502], 5)

    def test_defect_injection_sends_short_frames(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertTrue(self.service.command("defect", {"frame_id": "0x4B0", "defect": "short"})["ok"])
        self.assertFalse(self.service.command("defect", {"frame_id": "0x4B0", "defect": "crc"})["ok"])
        lengths = set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(lengths) < 1:
            message = peer.recv(timeout=0.1)
            if message is not None and message.arbitration_id == 0x4B0 and len(message.data) == 6:
                lengths.add(len(message.data))
        self.assertEqual(lengths, {6})

    def test_scenarios_and_reset_change_what_is_sent(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertTrue(self.service.command("scenario", {"name": "pdm_offline"})["ok"])
        self.assertFalse(self.service.command("scenario", {"name": "nope"})["ok"])
        offline = None
        deadline = time.monotonic() + 2.0
        while offline is None and time.monotonic() < deadline:
            message = peer.recv(timeout=0.1)
            if message is not None and message.arbitration_id == 0x5A0 and bytes(message.data).hex() == "7fff7fffffffffff":
                offline = message
        self.assertIsNotNone(offline)
        self.service.command("reset")
        self.assertFalse(self.service.snapshot()["values"]["pdm.offline"])
        self.service.command("scenario", {"name": "silence"})
        self.assertTrue(self.service.snapshot()["controls"]["paused"])

    # -- values ---------------------------------------------------------------

    def test_set_values_validates_atomically(self) -> None:
        self.assertFalse(self.service.set_values({"bms.voltage_v": 500, "bogus": 1})["ok"])
        self.assertEqual(self.service.snapshot()["values"]["bms.voltage_v"], 548.0)
        result = self.service.set_values({"bms.voltage_v": 9999, "wheel.rr.fault": "1F"})
        self.assertEqual(result["values"], {"bms.voltage_v": 6553.5, "wheel.rr.fault": 0x1F})
        self.assertFalse(self.service.set_values({})["ok"])
        self.assertFalse(self.service.set_values("x")["ok"])

    def test_drive_cycle_owns_dynamic_values_but_not_flags(self) -> None:
        self._connect()
        self.assertTrue(self.service.command("run", {"mode": "cycle"})["ok"])
        blocked = self.service.set_values({"bms.voltage_v": 500})
        self.assertFalse(blocked["ok"])
        self.assertIn("行驶循环", blocked["error"])
        self.assertTrue(self.service.set_values({"wheel.fl.enabled": False, "ecu.mode": 2})["ok"])
        self.assertTrue(wait_until(lambda: self.service.snapshot()["cycle"] is not None
                                   and self.service.snapshot()["cycle"]["t"] > 0.2))
        self.service.command("run", {"mode": "manual"})
        self.assertIsNone(self.service.snapshot()["cycle"])
        self.assertTrue(self.service.set_values({"bms.voltage_v": 500})["ok"])
        self.assertFalse(self.service.command("run", {"mode": "warp"})["ok"])

    # -- display -> bench (receive path) ---------------------------------

    def test_display_frames_are_decoded_and_prove_the_display_is_alive(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.assertFalse(self.service.snapshot()["display"]["online"])
        send = lambda frame_id, data: peer.send(can.Message(arbitration_id=frame_id, data=data,
                                                            is_extended_id=False))
        send(0x301, bytes([0xE8, 0x03]))
        send(0x7A1, (5231).to_bytes(2, "little") + bytes(2) + (5280).to_bytes(2, "little") + bytes(2))
        send(0x703, bytes([3, 1]))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["display"].get("steering") is not None))
        display = self.service.snapshot()["display"]
        self.assertTrue(display["online"])
        self.assertEqual(display["speed"]["data"]["speed_kmh"], 100.0)
        self.assertEqual(display["lap"]["data"]["current_s"], 52.31)
        self.assertEqual(display["steering"]["data"], {"slip_level": 3, "lap_toggle": True, "error_clear": False})
        self.assertTrue(any("0x703" in event["text"] and "圈速计时键" in event["text"]
                            for event in self.service.snapshot()["events"]))

    def test_malformed_display_frames_are_counted_not_shown(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        peer.send(can.Message(arbitration_id=0x301, data=b"\x01", is_extended_id=False))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["stats"]["rx_other"] == 1))
        self.assertNotIn("speed", self.service.snapshot()["display"])

    def test_ecu_follows_the_mode_relayed_by_the_display(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        peer.send(can.Message(arbitration_id=0x310, data=d.build_drive_mode_request(51), is_extended_id=False))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["values"]["ecu.mode"] == 3))
        self.service.command("ecu_follow", {"enabled": False})
        peer.send(can.Message(arbitration_id=0x310, data=d.build_drive_mode_request(48), is_extended_id=False))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["display"]["mode_request"]["data"]["mode"] == 0))
        self.assertEqual(self.service.snapshot()["values"]["ecu.mode"], 3)
        peer.send(can.Message(arbitration_id=0x310, data=d.build_drive_mode_request(0xFF), is_extended_id=False))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["display"]["mode_request"]["data"]["mode"] is None))

    def test_foreign_frame_with_a_sent_id_pauses_everything_until_resumed(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        peer.send(can.Message(arbitration_id=0x5A0, data=bytes(8), is_extended_id=False))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["conflict"] is not None))
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["conflict"]["id"], "0x5A0")
        self.assertTrue(snapshot["controls"]["paused"])
        self.assertTrue(any(event["kind"] == "conflict" for event in snapshot["events"]))
        self._capture(peer, 0.1)
        self.assertEqual(sum(self._capture(peer, 0.2).values()), 0)
        self.assertTrue(self.service.command("resume")["ok"])
        self.assertIsNone(self.service.snapshot()["conflict"])
        self.assertTrue(wait_until(lambda: self._capture(peer, 0.1)[0x5A2] > 0))

    def test_foreign_frame_of_a_disabled_node_is_not_a_conflict(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self.service.command("node", {"node": "bms", "enabled": False})
        peer.send(can.Message(arbitration_id=0x4B0, data=bytes(7), is_extended_id=False))
        time.sleep(0.2)
        self.assertIsNone(self.service.snapshot()["conflict"])

    # -- steering wheel (CAN2) ---------------------------------------------

    def test_wheel_commands_use_the_second_channel_with_wheel_frame_layouts(self) -> None:
        wheel_peer = self._peer(self.wheel)
        self._connect(wheel_channel=self.wheel)
        self.assertTrue(self.service.snapshot()["connection"]["wheel_connected"])
        frames = []

        def expect(command: str, args: dict | None, frame_id: int, data: bytes) -> None:
            self.assertTrue(self.service.command(command, args)["ok"], command)
            message = wheel_peer.recv(timeout=1.0)
            self.assertIsNotNone(message, command)
            frames.append((message.arbitration_id, bytes(message.data)))
            self.assertEqual((message.arbitration_id, bytes(message.data)), (frame_id, data), command)

        expect("wheel_mode", {"mode": 2}, 0x310, b"2" + bytes(7))
        expect("wheel_slip", {"level": 5}, 0x700, b"\x05")
        expect("wheel_lap", None, 0x701, b"\x01")
        expect("wheel_clear", None, 0x702, b"\x01")
        expect("wheel_invalid", {"kind": "mode_ff"}, 0x310, b"\xff" + bytes(7))
        expect("wheel_invalid", {"kind": "mode_4"}, 0x310, b"4" + bytes(7))
        expect("wheel_invalid", {"kind": "slip_8"}, 0x700, b"\x08")
        self.assertTrue(self.service.command("wheel_powerup")["ok"])
        self.assertEqual([wheel_peer.recv(timeout=1.0).arbitration_id for _ in range(2)], [0x700, 0x310])
        self.assertTrue(self.service.command("wheel_lap_double")["ok"])
        first, second = wheel_peer.recv(timeout=1.0), wheel_peer.recv(timeout=1.0)
        self.assertLess(0.08, second.timestamp - first.timestamp)
        self.assertLess(second.timestamp - first.timestamp, 0.3)
        values = self.service.snapshot()["values"]
        self.assertEqual((values["wheelsw.mode"], values["wheelsw.slip"]), (2, 5))

    def test_wheel_commands_report_why_they_cannot_run(self) -> None:
        self.assertIn("未连接", self.service.command("wheel_lap")["error"])
        self._connect()
        self.assertIn("方向盘", self.service.command("wheel_lap")["error"])
        self.assertFalse(self.service.command("wheel_mode", {"mode": 4})["ok"])
        self.assertFalse(self.service.command("wheel_slip", {"level": 8})["ok"])
        self.assertFalse(self.service.command("wheel_invalid", {"kind": "x"})["ok"])
        self.assertFalse(self.service.command("nonexistent")["ok"])

    def test_powerup_needs_something_to_replay(self) -> None:
        self._peer(self.wheel)
        self._connect(wheel_channel=self.wheel)
        self.assertIn("还没有", self.service.command("wheel_powerup")["error"])

    # -- failures and snapshot ---------------------------------------------

    def test_snapshot_is_json_and_reports_rates_and_errors(self) -> None:
        self._connect()
        self.assertTrue(wait_until(lambda: self.service.snapshot()["stats"]["tx_total"] > 100))
        snapshot = self.service.snapshot()
        json.dumps(snapshot)
        self.assertEqual(snapshot["connection"]["state"], "running")
        self.assertEqual(len(snapshot["frames"]), 19)
        sent = [frame for frame in snapshot["frames"] if frame["count"]]
        self.assertTrue(sent and all(frame["data"] for frame in sent))
        self.assertTrue(wait_until(lambda: self.service.snapshot()["stats"]["tx_hz"] > 100, 3.0))

    def test_send_failures_are_counted_and_surface_the_reason(self) -> None:
        self._connect()
        with patch.object(can.Message, "__init__", side_effect=OSError("PCAN_ERROR_QXMTFULL")):
            self.assertTrue(wait_until(lambda: self.service.snapshot()["stats"]["tx_recent_errors"] > 0))
        snapshot = self.service.snapshot()
        self.assertIn("QXMTFULL", snapshot["stats"]["tx_last_error"])
        self.assertEqual(snapshot["connection"]["state"], "running")

    def test_receive_failure_stops_the_session_and_disconnect_cleans_up(self) -> None:
        self._connect()
        bus = self.service._bus
        with patch.object(bus, "recv", side_effect=OSError("device removed")):
            self.assertTrue(wait_until(lambda: self.service.snapshot()["connection"]["state"] == "error"))
        connection = self.service.snapshot()["connection"]
        self.assertFalse(connection["connected"])
        self.assertTrue(connection["active"])
        self.assertIn("device removed", connection["error"])
        self.service.disconnect()
        self.assertEqual(self.service.snapshot()["connection"]["state"], "disconnected")
        self._connect()           # reconnecting after a failure works
        self.service.disconnect()
        self.service.disconnect()  # idempotent

    def test_disconnect_stops_transmission(self) -> None:
        peer = self._peer(self.canb)
        self._connect()
        self._capture(peer, 0.1)
        self.service.disconnect()
        self._capture(peer, 0.1)
        self.assertEqual(sum(self._capture(peer, 0.3).values()), 0)


if __name__ == "__main__":
    unittest.main()
