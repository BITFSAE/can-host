"""Reading a physical steering wheel from the display's CANB output only (0x703 relayed verbatim)."""

from __future__ import annotations

import time
import unittest

from canhost import decoders as d
from canhost.display.wheel_check import WheelObserver


def observer() -> WheelObserver:
    return WheelObserver(wall_clock=lambda: 1_000_000.0)


def command(slip: int, lap: bool = False, clear: bool = False, disable: bool = False) -> bytes:
    # Vehicle_CanB.dbc BO_1795 byte1: bit0 DisableRequest, bit1 ErrorClearActive, bit2 LapTimerToggle.
    return bytes([slip, int(disable) | int(clear) << 1 | int(lap) << 2])


def mode(code: int, length: int = 8) -> bytes:
    return bytes([code]) + bytes(length - 1)


class WheelObserverTest(unittest.TestCase):
    def rows(self, view: WheelObserver, now: float = 100.0) -> list[dict]:
        return view.snapshot(now)["rows"][::-1]          # oldest first

    def test_knob_positions_are_translated_and_forwarded_frames_pass(self) -> None:
        view = observer()
        for index, name in enumerate(("S 直线", "Q 避障", "C 八字", "E 耐久")):
            view.display_frame(float(index), d.DRIVE_MODE_REQUEST_ID, mode(48 + index))
        rows = self.rows(view)
        self.assertEqual([row["status"] for row in rows], ["ok"] * 4)
        self.assertIn("E 耐久", rows[-1]["action"])
        self.assertEqual(view.snapshot(5.0)["knob"], {"mode": 3, "name": "E 耐久", "age": 2.0})
        self.assertEqual(rows[0]["frame"], "0x310 · 30 00 00 00 00 00 00 00")

    def test_default_mode_47_is_a_valid_forwarded_request(self) -> None:
        view = observer()
        view.display_frame(1.0, d.DRIVE_MODE_REQUEST_ID, mode(47))
        snapshot = view.snapshot(1.0)
        self.assertEqual(snapshot["knob"]["mode"], -1)
        self.assertEqual(snapshot["knob"]["name"], "默认")
        self.assertEqual(snapshot["rows"][0]["status"], "ok")

    def test_invalid_mode_code_is_flagged_because_the_display_must_not_relay_it(self) -> None:
        for code in (0xFF, 52, 46):
            view = observer()
            view.display_frame(1.0, d.DRIVE_MODE_REQUEST_ID, mode(code))
            row = view.snapshot(2.0)["rows"][0]
            self.assertEqual(row["status"], "bad", code)
            self.assertIn("本应丢弃", row["notes"][0])
            self.assertIsNone(view.snapshot(2.0)["knob"])

    def test_mode_request_length_and_padding_are_warnings_not_errors(self) -> None:
        view = observer()
        view.display_frame(1.0, d.DRIVE_MODE_REQUEST_ID, mode(49, 1))
        view.display_frame(2.0, d.DRIVE_MODE_REQUEST_ID, bytes([49, 0, 0, 0, 0, 0, 0, 9]))
        rows = self.rows(view)
        self.assertEqual([row["status"] for row in rows], ["warn", "warn"])
        self.assertIn("DLC 为 1", rows[0]["notes"][0])
        self.assertIn("Byte1..7", rows[1]["notes"][0])
        self.assertEqual(view.snapshot(3.0)["summary"], {"ok": 2, "bad": 0})

    def test_slip_and_single_keys_are_told_apart(self) -> None:
        view = observer()
        view.display_frame(1.0, d.STEERING_COMMAND_ID, command(0))
        view.display_frame(2.0, d.STEERING_COMMAND_ID, command(5))
        view.display_frame(3.0, d.STEERING_COMMAND_ID, command(5))
        view.display_frame(4.0, d.STEERING_COMMAND_ID, command(5, lap=True))
        view.display_frame(5.0, d.STEERING_COMMAND_ID, command(5, clear=True))
        actions = [row["action"] for row in self.rows(view)]
        self.assertIn("电位器转动", actions[0])
        self.assertEqual(actions[1], "电位器 → 滑级 5（原为 0）")
        self.assertIn("未变", actions[2])
        self.assertEqual(actions[3], "圈速计时键 按下（当前滑级 5）")
        self.assertEqual(actions[4], "消错键 按下（当前滑级 5）")
        snapshot = view.snapshot(6.0)
        self.assertEqual((snapshot["lap"]["count"], snapshot["clear"]["count"], snapshot["slip"]["level"]),
                         (1, 1, 5))
        self.assertEqual(snapshot["summary"], {"ok": 5, "bad": 0})

    def test_bit0_is_the_exit_enable_request_not_the_lap_timer(self) -> None:
        view = observer()
        view.display_frame(1.0, d.STEERING_COMMAND_ID, command(4, disable=True))
        snapshot = view.snapshot(1.01)
        self.assertEqual((snapshot["lap"]["count"], snapshot["disable"]["count"]), (0, 1))
        self.assertTrue(snapshot["disable"]["active"])
        self.assertIn("退使组合键 按住", snapshot["rows"][0]["action"])

    def test_second_press_inside_the_debounce_window_is_a_warning(self) -> None:
        # The display relays every frame verbatim and only its own screen counts them as one press.
        view = observer()
        view.display_frame(10.0, d.STEERING_COMMAND_ID, command(2, lap=True))
        view.display_frame(10.1, d.STEERING_COMMAND_ID, command(2, lap=True))
        view.display_frame(10.5, d.STEERING_COMMAND_ID, command(2, lap=True))   # > 300 ms later: fine
        statuses = [row["status"] for row in self.rows(view)]
        self.assertEqual(statuses, ["ok", "warn", "ok"])
        self.assertIn("300 ms 去重", self.rows(view)[1]["notes"][0])
        # The two keys debounce independently.
        view = observer()
        view.display_frame(10.0, d.STEERING_COMMAND_ID, command(2, lap=True))
        view.display_frame(10.1, d.STEERING_COMMAND_ID, command(2, clear=True))
        self.assertEqual([row["status"] for row in self.rows(view)], ["ok", "ok"])

    def test_malformed_commands_are_reported_with_the_reason(self) -> None:
        cases = {
            "长度": bytes([3]),
            "大于 7": bytes([8, 0]),
            "保留位": bytes([3, 8]),
            "同时为 1": bytes([3, 6]),
        }
        for reason, payload in cases.items():
            view = observer()
            view.display_frame(1.0, d.STEERING_COMMAND_ID, payload)
            row = view.snapshot(2.0)["rows"][0]
            self.assertEqual(row["status"], "bad", reason)
            self.assertIn(reason if reason != "长度" else "DLC 应为 2", row["notes"][0])
        for flags in (3, 5, 7):   # the exit-enable request never shares a frame with a key bit either
            view = observer()
            view.display_frame(1.0, d.STEERING_COMMAND_ID, bytes([3, flags]))
            self.assertIn("同时为 1", view.snapshot(1.01)["rows"][0]["notes"][0])

    def test_longer_command_is_a_warning_because_the_display_relays_it_verbatim(self) -> None:
        view = observer()
        view.display_frame(1.0, d.STEERING_COMMAND_ID, bytes([3, 4, 0xFF]))
        row = view.snapshot(2.0)["rows"][0]
        self.assertEqual(row["status"], "warn")
        self.assertIn("DLC 为 3", row["notes"][0])
        self.assertEqual(view.snapshot(2.0)["lap"]["count"], 1)

    def chord(self, view: WheelObserver, start: float, frames: int, slip: int = 4,
              period: float = 0.02, end: bytes | None = None) -> float:
        for index in range(frames):
            view.display_frame(start + index * period, d.STEERING_COMMAND_ID, command(slip, disable=True))
        finish = start + frames * period
        view.display_frame(finish, d.STEERING_COMMAND_ID, command(slip) if end is None else end)
        return finish

    def test_exit_enable_frames_fold_into_one_row(self) -> None:
        view = observer()
        view.display_frame(0.5, d.STEERING_COMMAND_ID, command(4))
        finish = self.chord(view, 1.0, 6)
        rows = self.rows(view, finish)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["status"], "ok")
        self.assertIn("已松开", rows[1]["action"])
        self.assertIn("共 6 帧", rows[1]["action"])
        self.assertEqual(rows[1]["frame"], "0x703 · 04 00")
        snapshot = view.snapshot(finish)
        self.assertEqual(snapshot["disable"], {"count": 1, "age": 0.0, "active": False})
        self.assertEqual((snapshot["lap"]["count"], snapshot["clear"]["count"]), (0, 0))
        self.assertEqual(snapshot["summary"], {"ok": 2, "bad": 0})
        # A second chord is a second request with its own row.
        self.chord(view, 3.0, 3)
        self.assertEqual(view.snapshot(4.0)["disable"]["count"], 2)
        self.assertEqual(len(view.snapshot(4.0)["rows"]), 3)

    def test_row_revision_changes_while_the_request_is_held(self) -> None:
        view = observer()
        view.display_frame(1.0, d.STEERING_COMMAND_ID, command(4, disable=True))
        first = view.snapshot(1.0)["rows"][0]
        view.display_frame(1.02, d.STEERING_COMMAND_ID, command(4, disable=True))
        second = view.snapshot(1.02)["rows"][0]
        self.assertEqual(first["id"], second["id"])
        self.assertGreater(second["rev"], first["rev"])
        self.assertIn("已 2 帧", second["action"])

    def test_too_few_exit_enable_frames_are_an_error(self) -> None:
        view = observer()
        finish = self.chord(view, 1.0, 2)
        row = view.snapshot(finish)["rows"][0]
        self.assertEqual(row["status"], "bad")
        self.assertIn("至少发 3 帧", row["notes"][0])
        self.assertEqual(view.snapshot(finish)["summary"], {"ok": 0, "bad": 1})

    def test_chord_must_not_end_with_a_single_key_action(self) -> None:
        view = observer()
        finish = self.chord(view, 1.0, 4, end=command(4, lap=True))
        row = view.snapshot(finish)["rows"][0]
        self.assertEqual(row["status"], "bad")
        self.assertIn("不应带计时或消错位", row["notes"][0])

    def test_missing_end_frame_and_lost_frames_are_reported(self) -> None:
        view = observer()
        for index in range(4):
            view.display_frame(1.0 + index * 0.02, d.STEERING_COMMAND_ID, command(4, disable=True))
        self.assertTrue(view.snapshot(1.5)["disable"]["active"])
        snapshot = view.snapshot(3.0)          # more than 1 s without any frame
        self.assertFalse(snapshot["disable"]["active"])
        self.assertEqual(snapshot["rows"][0]["status"], "bad")
        self.assertIn("结束帧", snapshot["rows"][0]["notes"][0])
        view = observer()
        view.display_frame(1.0, d.STEERING_COMMAND_ID, command(4, disable=True))
        view.display_frame(1.02, d.STEERING_COMMAND_ID, command(4, disable=True))
        view.display_frame(1.3, d.STEERING_COMMAND_ID, command(4, disable=True))
        view.display_frame(1.32, d.STEERING_COMMAND_ID, command(4))
        row = view.snapshot(2.0)["rows"][0]
        self.assertEqual(row["status"], "warn")
        self.assertIn("可能丢帧", row["notes"][0])

    def test_unrelated_ids_and_clear_reset_the_log(self) -> None:
        view = observer()
        view.display_frame(1.0, 0x301, bytes(2))
        self.assertEqual(view.snapshot(2.0)["rows"], [])
        view.display_frame(1.0, d.STEERING_COMMAND_ID, command(1))
        view.reset()
        snapshot = view.snapshot(2.0)
        self.assertEqual((snapshot["rows"], snapshot["slip"], snapshot["summary"]), ([], None, {"ok": 0, "bad": 0}))

    def test_rows_are_capped_and_newest_first(self) -> None:
        view = observer()
        for index in range(60):
            view.display_frame(float(index), d.STEERING_COMMAND_ID, command(index % 8))
        rows = view.snapshot(100.0)["rows"]
        self.assertEqual(len(rows), 20)
        self.assertGreater(rows[0]["id"], rows[-1]["id"])


class ServiceWheelObserverTest(unittest.TestCase):
    def test_canb_frames_from_the_display_reach_the_observer(self) -> None:
        import can
        from canhost.display.service import DisplayBenchService

        service = DisplayBenchService(
            bus_factory=lambda channel: can.Bus(interface="virtual", channel=channel), probe_s=0.02)
        self.addCleanup(service.disconnect)
        peer = can.Bus(interface="virtual", channel="wheel-observer")
        self.addCleanup(peer.shutdown)
        self.assertTrue(service.connect({"channel": "wheel-observer", "confirmed": True})["ok"])

        def send(frame_id: int, data: bytes) -> None:
            peer.send(can.Message(arbitration_id=frame_id, data=data, is_extended_id=False))

        send(0x310, mode(50))
        send(0x703, command(4))
        send(0x703, command(4, lap=True))
        for _ in range(3):
            send(0x703, command(4, disable=True))
        send(0x703, command(4))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (len(service.snapshot()["wheel_observer"]["rows"]) < 4
                                               or service.snapshot()["wheel_observer"]["disable"]["active"]):
            time.sleep(0.02)
        observed = service.snapshot()["wheel_observer"]
        self.assertEqual(observed["knob"]["name"], "C 八字")
        self.assertEqual((observed["slip"]["level"], observed["lap"]["count"], observed["disable"]["count"]),
                         (4, 1, 1))
        self.assertEqual(observed["summary"], {"ok": 4, "bad": 0})
        events = [event["text"] for event in service.snapshot()["events"] if "0x703" in event["text"]]
        self.assertEqual(sum("退使请求（" in text for text in events), 1)   # repeats are not logged
        self.assertEqual(sum("退使请求结束" in text for text in events), 1)
        self.assertTrue(service.command("wheel_log_clear")["ok"])
        self.assertEqual(service.snapshot()["wheel_observer"]["rows"], [])


if __name__ == "__main__":
    unittest.main()
