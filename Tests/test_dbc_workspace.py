"""The DBC workspace: files, history, saving, live decoding, recording, audit, traces."""

from __future__ import annotations

import csv
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest

import cantools

from canhost.dbc import codec
from canhost.dbc.series import Ring, SeriesRecorder
from canhost.dbc.trace import TraceFile, decimate
from canhost.dbc.workspace import DbcWorkspace, SourceTap, guess_bus, is_central
from canhost.decoders import CanFrame

FIXTURE = Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc"
ENGINE_OK = bytes([0x88, 0x13, 0x50, 0x03, 0x64, 0, 0, 0])


def fact(identifier: int, data: bytes, last_ts: float, count: int = 10, cycle: float | None = 10.0,
         extended: bool = False, direction: str = "rx") -> dict:
    return {"id": identifier, "extended": extended, "data": data, "last_ts": last_ts, "count": count,
            "rx_count": count, "tx_count": 0, "cycle_ms": cycle, "direction": direction}


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.path = self.folder / "Vehicle_Can1.dbc"
        shutil.copy(FIXTURE, self.path)
        self.workspace = DbcWorkspace()

    def tearDown(self) -> None:
        self.workspace.close_all()
        self._folder.cleanup()

    def open(self, bus: str | None = None) -> str:
        result = self.workspace.open_path(self.path, bus)
        self.assertTrue(result["ok"], result)
        return result["file"]["id"]


class FileTests(WorkspaceCase):
    def test_open_describes_the_file_and_guesses_the_bus_from_its_name(self) -> None:
        file_id = self.open()
        info = self.workspace.state()["files"][0]
        self.assertEqual(info["id"], file_id)
        self.assertEqual(info["bus"], "can1")
        self.assertFalse(info["dirty"])
        self.assertFalse(info["readonly"])
        self.assertEqual(info["messages"], 6)
        self.assertEqual(info["signals"], 15)
        self.assertEqual((guess_bus("Vehicle_CanB.dbc"), guess_bus("x.dbc")), ("canb", "none"))

    def test_opening_the_same_file_twice_returns_the_open_one(self) -> None:
        first = self.open()
        again = self.workspace.open_path(self.path)
        self.assertTrue(again["already_open"])
        self.assertEqual(again["file"]["id"], first)
        self.assertEqual(len(self.workspace.files), 1)

    def test_cana_and_canc_open_without_binding_and_decode_after_explicit_binding(self) -> None:
        for suffix in ("A", "C"):
            with self.subTest(bus=suffix):
                path = self.folder / f"Vehicle_Can{suffix}.dbc"
                shutil.copy(FIXTURE, path)
                result = self.workspace.open_path(path)
                self.assertTrue(result["ok"], result)
                file_id = result["file"]["id"]
                self.assertEqual(result["file"]["bus"], "none")
                catalog = self.workspace.detail(file_id)["catalog"]
                self.assertTrue(any(message["virtual"] for message in catalog["messages"]))
                frames = [fact(291, ENGINE_OK, 99.99)]
                self.assertNotIn("s:291", self.workspace.live("can1", frames, 100, ["s:291"])["decoded"])
                self.workspace.set_bus(file_id, "can1")
                live = self.workspace.live("can1", frames, 100, ["s:291"])
                self.assertEqual(live["decoded"]["s:291"]["file"], file_id)
                speed = next(row for row in live["decoded"]["s:291"]["signals"] if row["name"] == "Speed")
                self.assertEqual(speed["value"], 50)
                self.assertNotIn("v", live["decoded"])
                self.assertFalse(any(message["key"] == "v" for message in live["messages"]))
                self.workspace.set_bus(file_id, "none")

    def test_missing_and_unreadable_files_are_reported(self) -> None:
        result = self.workspace.open_path(self.folder / "missing.dbc")
        self.assertFalse(result["ok"])
        self.assertIn("missing.dbc", result["error"])

    def test_central_library_files_are_read_only_until_saved_elsewhere(self) -> None:
        central = self.folder / "vehicle-interfaces" / "can"
        central.mkdir(parents=True)
        path = central / "Vehicle_CanB.dbc"
        shutil.copy(FIXTURE, path)
        self.assertTrue(is_central(path))
        result = self.workspace.open_path(path)
        file_id = result["file"]["id"]
        self.assertTrue(result["file"]["readonly"])
        edit = self.workspace.edit(file_id, {"op": "set_message", "message": "s:291", "fields": {"dlc": 6}})
        self.assertFalse(edit["ok"])
        self.assertIn("只读", edit["error"])
        self.assertFalse(self.workspace.save(file_id)["ok"])
        copy = self.folder / "mine.dbc"
        saved = self.workspace.save(file_id, path=str(copy))
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(copy.read_bytes(), path.read_bytes())

    def test_new_file_edit_and_save_as(self) -> None:
        file_id = self.workspace.new_file("scratch.dbc", "can1")["file"]["id"]
        self.assertFalse(self.workspace.save(file_id)["ok"])
        for op in ({"op": "add_node", "name": "ECU"},
                   {"op": "add_message", "name": "Status", "id": 256, "extended": False, "dlc": 8, "sender": "ECU"},
                   {"op": "add_signal", "message": "s:256", "fields": {"name": "Rpm", "length": 16}}):
            self.assertTrue(self.workspace.edit(file_id, op)["ok"])
        target = self.folder / "scratch.dbc"
        result = self.workspace.save(file_id, path=str(target))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.workspace.state()["files"][0]["path"], str(target))
        cantools.database.load_file(str(target), strict=True)

    def test_state_round_trips_through_persistence(self) -> None:
        self.open("canb")
        saved = self.workspace.persisted()
        other = DbcWorkspace()
        self.assertEqual(other.restore(saved), [])
        self.assertEqual(other.state()["files"][0]["bus"], "canb")
        self.path.unlink()
        self.assertEqual(len(DbcWorkspace().restore(saved)), 1)       # reported, not raised

    def test_closing_and_changing_the_bus(self) -> None:
        file_id = self.open()
        self.assertFalse(self.workspace.set_bus(file_id, "elsewhere")["ok"])
        self.assertEqual(self.workspace.set_bus(file_id, "all")["file"]["bus"], "all")
        self.workspace.close(file_id)
        self.assertEqual(self.workspace.state()["files"], [])
        with self.assertRaises(Exception):
            self.workspace.detail(file_id)


class EditHistoryTests(WorkspaceCase):
    def test_edit_marks_dirty_and_undo_redo_restore_the_text(self) -> None:
        file_id = self.open()
        original = self.workspace.export_text(file_id)
        result = self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Speed",
                                               "fields": {"unit": "m/s"}})
        self.assertTrue(result["file"]["dirty"])
        self.assertTrue(result["file"]["can_undo"])
        self.assertNotEqual(self.workspace.export_text(file_id), original)
        self.assertEqual(self.workspace.undo(file_id)["file"]["dirty"], False)
        self.assertEqual(self.workspace.export_text(file_id), original)
        self.assertTrue(self.workspace.redo(file_id)["file"]["dirty"])
        self.assertFalse(self.workspace.redo(file_id)["ok"])

    def test_a_new_edit_clears_the_redo_history(self) -> None:
        file_id = self.open()
        edit = {"op": "set_message", "message": "s:291", "fields": {"dlc": 6}}
        self.workspace.edit(file_id, edit)
        self.workspace.undo(file_id)
        self.workspace.edit(file_id, {"op": "set_message", "message": "s:291", "fields": {"dlc": 7}})
        self.assertFalse(self.workspace.redo(file_id)["ok"])

    def test_a_batch_is_all_or_nothing(self) -> None:
        file_id = self.open()
        before = self.workspace.export_text(file_id)
        result = self.workspace.edit_many(file_id, [
            {"op": "set_message", "message": "s:291", "fields": {"dlc": 6}},
            {"op": "set_signal", "message": "s:291", "signal": "Nope", "fields": {"unit": "x"}},
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(self.workspace.export_text(file_id), before)
        self.assertFalse(self.workspace.state()["files"][0]["can_undo"])

    def test_edits_update_the_decoder_immediately(self) -> None:
        file_id = self.open()
        self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Speed",
                                      "fields": {"factor": 0.1}})
        decode, message = self.workspace.resolve("can1", 291, False)
        self.assertAlmostEqual(decode.decode(message, ENGINE_OK)["values"]["Speed"]["value"], 500.0)

    def test_text_cantools_cannot_parse_keeps_the_last_working_decoder(self) -> None:
        from canhost.dbc.document import RawItem
        self.open()
        file = self.workspace.files[0]
        working = file.decode
        file.document.items.append(RawItem(text="BO_ this is not a message definition\n"))
        file.revision += 1
        file.rebuild_decode()
        self.assertTrue(file.decode_stale)
        self.assertIsNotNone(file.decode_error)
        self.assertIs(file.decode, working)
        self.assertIsNotNone(self.workspace.resolve("can1", 291, False) or file.decode.message(291, False))

    def test_lint_findings_are_part_of_the_detail(self) -> None:
        file_id = self.open()
        self.workspace.edit(file_id, {"op": "duplicate_signal", "message": "s:291", "signal": "Gear",
                                      "name": "Gear2"})
        detail = self.workspace.detail(file_id)
        self.assertEqual(detail["file"]["errors"], 1)
        errors = [i for i in detail["issues"] if i["level"] == "error"]
        self.assertEqual([i["code"] for i in errors], ["sig.overlap"])


class SaveTests(WorkspaceCase):
    def test_saving_an_unedited_file_changes_no_byte(self) -> None:
        file_id = self.open()
        original = self.path.read_bytes()
        result = self.workspace.save(file_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.path.read_bytes(), original)

    def test_an_edit_changes_only_its_own_line_on_disk(self) -> None:
        file_id = self.open()
        before = self.path.read_text(encoding="utf-8").splitlines()
        self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Temp",
                                      "fields": {"unit": "°C"}})
        self.assertTrue(self.workspace.save(file_id)["ok"])
        after = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(sum(1 for a, b in zip(before, after) if a != b), 1)
        self.assertTrue(Path(str(self.path) + ".bak").exists())
        self.assertFalse(self.workspace.state()["files"][0]["dirty"])

    def test_the_original_encoding_is_kept_and_unencodable_text_is_refused(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").replace("\n", "\r\n")
        self.path.write_bytes(codec.encode_text(text, "gbk"))
        file_id = self.open()
        self.assertEqual(self.workspace.state()["files"][0]["encoding"], "gbk")
        self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Gear",
                                      "fields": {"comment": "档位"}})
        self.assertTrue(self.workspace.save(file_id)["ok"])
        self.assertEqual(codec.read_file(self.path)[1], "gbk")
        self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Gear",
                                      "fields": {"comment": "☃"}})
        refused = self.workspace.save(file_id)
        self.assertFalse(refused["ok"])
        self.assertIn("无法用 GBK", refused["error"])

    def test_saving_as_utf8_is_possible_when_gbk_cannot_hold_the_text(self) -> None:
        self.path.write_bytes(codec.encode_text(FIXTURE.read_text(encoding="utf-8"), "gbk"))
        file_id = self.open()
        self.workspace.edit(file_id, {"op": "set_signal", "message": "s:291", "signal": "Gear",
                                      "fields": {"comment": "☃"}})
        self.assertTrue(self.workspace.save(file_id, encoding="utf-8")["ok"])
        self.assertEqual(codec.read_file(self.path)[1], "utf-8")

    def test_errors_block_saving_and_are_listed(self) -> None:
        file_id = self.open()
        self.workspace.edit(file_id, {"op": "duplicate_signal", "message": "s:291", "signal": "Gear",
                                      "name": "Gear2"})
        result = self.workspace.save(file_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"][0]["code"], "sig.overlap")
        self.assertTrue(self.workspace.save_review(file_id)["blockers"])

    def test_candbpp_save_reorders_sections_in_gbk_and_replaces_the_document(self) -> None:
        file_id = self.open()
        result = self.workspace.save(file_id, candbpp=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["encoding"], "gbk")
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith(b"\r\n"))
        text = raw.decode("gbk")
        self.assertLess(text.index("BO_ 2550588916"), text.index("CM_ BU_"))
        self.assertEqual(result["warnings"], [])
        self.assertFalse(self.workspace.state()["files"][0]["dirty"])
        self.assertTrue(self.workspace.state()["files"][0]["can_undo"])

    def test_review_lists_semantic_and_text_changes(self) -> None:
        file_id = self.open()
        for op in ({"op": "set_signal", "message": "s:291", "signal": "Speed", "fields": {"unit": "m/s", "factor": 0.1}},
                   {"op": "delete_signal", "message": "s:291", "signal": "Throttle"},
                   {"op": "add_message", "name": "Extra", "id": 999, "extended": False, "dlc": 2},
                   {"op": "set_signal", "message": "s:291", "signal": "Gear", "fields": {"choices": {"0": "Off"}}}):
            self.assertTrue(self.workspace.edit(file_id, op)["ok"])
        review = self.workspace.save_review(file_id)
        self.assertEqual(review["counts"], {"add": 1, "delete": 1, "change": 2})
        changed = next(r for r in review["changes"] if r["scope"] == "signal" and r["op"] == "change"
                       and r["target"] == "Speed")
        self.assertEqual({c["field"] for c in changed["changes"]}, {"单位", "因子"})
        self.assertTrue(any(line.startswith("+") and "Extra" in line for line in review["text"]["lines"]))
        self.assertEqual(review["blockers"], [])

    def test_two_open_files_can_be_compared(self) -> None:
        first = self.open()
        copy = self.folder / "Copy_Can1.dbc"
        shutil.copy(self.path, copy)
        second = self.workspace.open_path(copy)["file"]["id"]
        edited = self.workspace.edit_many(second, [
            {"op": "set_signal", "message": "s:291", "signal": "Speed", "fields": {"unit": "m/s"}},
            {"op": "add_message", "name": "Extra", "id": 999, "extended": False, "dlc": 2}])
        self.assertTrue(edited["ok"], edited)
        result = self.workspace.compare(first, second)
        self.assertEqual(result["counts"], {"add": 1, "delete": 0, "change": 1})
        self.assertEqual((result["first"], result["second"]), ("Vehicle_Can1.dbc", "Copy_Can1.dbc"))
        self.assertTrue(any("Extra" in line for line in result["text"]["lines"]))
        self.assertEqual(self.workspace.compare(first, first)["changes"], [])

    def test_review_reports_an_id_change_as_one_change(self) -> None:
        file_id = self.open()
        self.workspace.edit(file_id, {"op": "set_message", "message": "s:291", "fields": {"id": 600}})
        review = self.workspace.save_review(file_id)
        self.assertEqual(review["counts"], {"add": 0, "delete": 0, "change": 1})
        self.assertEqual(review["changes"][0]["changes"][0]["field"], "ID")


class LiveTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.file_id = self.open("can1")

    def live(self, frames, clock=100.0, keys=None, **kwargs):
        return self.workspace.live("can1", frames, clock, keys or [], **kwargs)

    def test_unseen_messages_wait_instead_of_showing_zero(self) -> None:
        result = self.live([], keys=["s:291"])
        engine = result["decoded"]["s:291"]
        self.assertEqual(engine["state"], "waiting")
        self.assertTrue(all(row["state"] == "waiting" and "value" not in row for row in engine["signals"]))
        self.assertEqual(result["names"]["s:291"], "EngineData")

    def test_fresh_frames_are_live_and_old_ones_stale(self) -> None:
        live = self.live([fact(291, ENGINE_OK, last_ts=99.99)], keys=["s:291"])["decoded"]["s:291"]
        self.assertEqual(live["state"], "live")
        row = next(r for r in live["signals"] if r["name"] == "Gear")
        self.assertEqual((row["state"], row["label"], row["raw"]), ("ok", "D", 3))
        stale = self.live([fact(291, ENGINE_OK, last_ts=90.0)], keys=["s:291"])["decoded"]["s:291"]
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["signals"][0]["state"], "ok")                  # last sample stays, flagged
        self.assertEqual(stale["expected_cycle_ms"], 10)

    def test_stale_limit_follows_the_cycle_time_but_never_drops_below_two_seconds(self) -> None:
        slow = self.live([fact(512, bytes(8), last_ts=95.0, cycle=3000.0)])["messages"]
        self.assertEqual(next(m for m in slow if m["key"] == "s:512")["state"], "live")   # 5 s < 3 * 3 s
        quick = self.live([fact(512, bytes(8), last_ts=98.5, cycle=10.0)])["messages"]
        self.assertEqual(next(m for m in quick if m["key"] == "s:512")["state"], "live")  # 1.5 s < 2 s floor

    def test_dlc_mismatch_marks_the_message_and_missing_signals(self) -> None:
        short = self.live([fact(291, ENGINE_OK[:3], last_ts=99.9)], keys=["s:291"])["decoded"]["s:291"]
        self.assertEqual(short["issue"], "dlc_short")
        states = {r["name"]: r["state"] for r in short["signals"]}
        self.assertEqual(states["Speed"], "ok")
        self.assertEqual(states["Throttle"], "missing")

    def test_inactive_multiplexer_branch_is_not_shown_as_a_value(self) -> None:
        rows = self.live([fact(768, bytes([0, 0x34, 0x12, 9, 0, 0, 0, 0]), 99.9)],
                         keys=["s:768"])["decoded"]["s:768"]["signals"]
        states = {r["name"]: r["state"] for r in rows}
        self.assertEqual((states["Ana"], states["Cur"]), ("ok", "inactive"))

    def test_unknown_ids_are_listed_with_their_data(self) -> None:
        result = self.live([fact(0x7FF, bytes([1, 2, 3]), 99.9, count=4)])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["unknown"][0]["id_text"], "0x7FF")
        self.assertEqual(result["unknown"][0]["data"], "01 02 03")

    def test_replay_age_is_measured_on_the_replay_clock(self) -> None:
        recorded = 1_700_000_000.0
        result = self.live([fact(291, ENGINE_OK, last_ts=recorded)], clock=recorded + 0.05)["messages"]
        self.assertEqual(next(m for m in result if m["key"] == "s:291")["state"], "live")

    def test_only_requested_messages_carry_signal_rows(self) -> None:
        result = self.live([fact(291, ENGINE_OK, 99.9)], keys=["s:291"])
        self.assertEqual(list(result["decoded"]), ["s:291"])
        light = self.live([fact(291, ENGINE_OK, 99.9)], include_messages=False, include_unknown=False)
        self.assertEqual((light["messages"], light["unknown"], list(light["names"])[:1]), ([], [], ["s:291"]))

    def test_bus_binding_decides_which_messages_apply(self) -> None:
        self.assertIsNotNone(self.workspace.resolve("can1", 291, False))
        self.assertIsNone(self.workspace.resolve("canb", 291, False))
        self.workspace.set_bus(self.file_id, "all")
        self.assertIsNotNone(self.workspace.resolve("canb", 291, False))
        self.workspace.set_bus(self.file_id, "none")
        self.assertIsNone(self.workspace.resolve("can1", 291, False))

    def test_the_first_file_wins_and_the_clash_is_reported(self) -> None:
        second = self.folder / "Other_Can1.dbc"
        second.write_text(FIXTURE.read_text(encoding="utf-8").replace("EngineData", "EngineClone"), encoding="utf-8")
        self.workspace.open_path(second)
        result = self.live([])
        self.assertEqual(result["names"]["s:291"], "EngineData")
        self.assertTrue(any(c["key"] == "s:291" and len(c["files"]) == 2 for c in result["conflicts"]))

    def test_encode_and_form(self) -> None:
        form = self.workspace.message_form("can1", "s:291")
        self.assertTrue(form["ok"])
        self.assertEqual(form["message"]["name"], "EngineData")
        result = self.workspace.encode("can1", "s:291", {"Speed": 50, "Temp": 40, "Gear": "D", "Throttle": 40})
        self.assertEqual(result["data"], "88 13 50 03 64 00 00 00")
        self.assertFalse(self.workspace.encode("canb", "s:291", {})["ok"])
        self.assertFalse(self.workspace.encode("can1", "s:291", {"Speed": 1e9})["ok"])
        self.assertFalse(self.workspace.encode("can1", "garbage", {})["ok"])


class SeriesTests(unittest.TestCase):
    def test_ring_returns_new_samples_by_cursor_and_notices_overwrites(self) -> None:
        ring = Ring(capacity=4)
        for index in range(3):
            ring.append(float(index), index * 10.0)
        cursor, times, values, lost = ring.since(0)
        self.assertEqual((cursor, times, values, lost), (3, [0.0, 1.0, 2.0], [0.0, 10.0, 20.0], False))
        for index in range(3, 9):
            ring.append(float(index), index * 10.0)
        cursor, times, _values, lost = ring.since(3)
        self.assertTrue(lost)
        self.assertEqual(times, [5.0, 6.0, 7.0, 8.0])
        self.assertEqual(ring.since(9)[1:3], ([], []))

    def test_recorder_stores_only_subscribed_signals_and_every_sample(self) -> None:
        workspace = DbcWorkspace()
        workspace.add_text(FIXTURE.read_text(encoding="utf-8"), name="Vehicle_Can1.dbc")
        recorder = SeriesRecorder(workspace.resolver(lambda: "can1"))
        recorder.set_series([{"sid": "a", "id": 291, "extended": False, "signal": "Speed"}])
        for index in range(5):
            data = struct.pack("<H", 1000 * (index + 1)) + bytes(6)
            recorder.on_frame(291, False, data, 10.0 + index)
        recorder.on_frame(512, False, bytes(8), 20.0)                       # not subscribed
        reply = recorder.fetch({})["a"]
        self.assertEqual(reply["t"], [10.0, 11.0, 12.0, 13.0, 14.0])
        self.assertEqual([round(v) for v in reply["v"]], [10, 20, 30, 40, 50])
        again = recorder.fetch({"a": reply["cursor"]})["a"]
        self.assertEqual(again["t"], [])
        self.assertTrue(recorder.fetch({"a": 999})["a"]["reset"])
        recorder.set_series([])
        self.assertFalse(recorder.active)

    def test_editing_the_dbc_rebuilds_the_routes(self) -> None:
        workspace = DbcWorkspace()
        file = workspace.add_text(FIXTURE.read_text(encoding="utf-8"), name="Vehicle_Can1.dbc")
        tap = SourceTap(workspace, lambda: "can1")
        tap.recorder.set_series([{"sid": "a", "id": 291, "extended": False, "signal": "Speed"}])
        frame = CanFrame(291, struct.pack("<H", 1000) + bytes(6), False, 1.0, "rx")
        tap(frame)
        workspace.edit(file.id, {"op": "set_signal", "message": "s:291", "signal": "Speed",
                                 "fields": {"factor": 1}})
        tap(CanFrame(291, struct.pack("<H", 1000) + bytes(6), False, 2.0, "rx"))
        self.assertEqual(tap.recorder.fetch({})["a"]["v"], [10.0, 1000.0])

    def test_too_many_series_are_refused(self) -> None:
        recorder = SeriesRecorder(lambda i, e: None)
        specs = [{"sid": str(n), "id": 1, "extended": False, "signal": "s"} for n in range(30)]
        with self.assertRaises(ValueError):
            recorder.set_series(specs)

    def test_decimation_keeps_the_extremes(self) -> None:
        times = [float(i) for i in range(10000)]
        values = [0.0] * 10000
        values[4321] = 99.0
        values[7777] = -50.0
        out_t, out_v = decimate(times, values, 200)
        self.assertLessEqual(len(out_t), 200)
        self.assertIn(99.0, out_v)
        self.assertIn(-50.0, out_v)
        self.assertEqual(out_t, sorted(out_t))


class AuditTests(WorkspaceCase):
    def test_audit_reports_what_differs_from_the_dbc(self) -> None:
        self.open("can1")
        tap = SourceTap(self.workspace, lambda: "can1")
        tap.audit_running = True
        for index in range(20):
            timestamp = index * 0.05                                         # 50 ms, the DBC says 10 ms
            tap(CanFrame(291, ENGINE_OK[:7] if index % 5 == 0 else ENGINE_OK, False, timestamp, "rx"))
            tap(CanFrame(512, bytes([0, 0, 0xFF, 0, 0, 0, 0, 0]), False, index * 0.02, "rx"))   # Flags
            tap(CanFrame(0x7FE, bytes(8), False, index * 0.05, "rx"))
        tap(CanFrame(291, bytes([0, 0, 0, 0, 0xFF, 0, 0, 0]), False, 1.0, "rx"))                # 102 % throttle
        tap(CanFrame(291, bytes([0, 0, 0, 7, 0, 0, 0, 0]), False, 1.05, "rx"))                  # Gear 7: no label
        tap(CanFrame(0x7FE, bytes(8), False, 4.0, "rx"))                                        # window > 3 s
        report = tap.audit.report(self.workspace.expected_messages("can1"))
        self.assertEqual([u["id_text"] for u in report["unknown"]], ["0x7FE"])
        self.assertIn("EngineData", [d["name"] for d in report["dlc"]])
        self.assertIn("EngineData", [c["name"] for c in report["cycle"]])
        self.assertEqual(report["cycle"][0]["expected_ms"], 10)
        self.assertEqual([r["signal"] for r in report["range"]], ["Throttle"])
        self.assertEqual([c["signal"] for c in report["choice"]], ["Gear"])
        self.assertIn("MuxMsg", [m["name"] for m in report["missing"]])
        self.assertTrue(report["window_ok"])

    def test_missing_messages_are_not_reported_before_the_window_is_long_enough(self) -> None:
        self.open("can1")
        tap = SourceTap(self.workspace, lambda: "can1")
        tap.audit_running = True
        tap(CanFrame(291, ENGINE_OK, False, 1.0, "rx"))
        tap(CanFrame(291, ENGINE_OK, False, 1.5, "rx"))
        report = tap.audit.report(self.workspace.expected_messages("can1"))
        self.assertFalse(report["window_ok"])
        self.assertEqual(report["missing"], [])


class TraceTests(WorkspaceCase):
    def make_bmslog(self) -> Path:
        path = self.folder / "run.bmslog"
        database = sqlite3.connect(path)
        database.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE frames (seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                direction TEXT NOT NULL, arbitration_id INTEGER NOT NULL, extended INTEGER NOT NULL,
                data BLOB NOT NULL);
            CREATE INDEX frames_id_timestamp_idx ON frames(arbitration_id, timestamp);
        """)
        database.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("format", "BITFSAE_BMS_LOG"), ("schema_version", "1"), ("bus_profile", "can1"), ("bitrate", "500000")])
        rows = []
        for index in range(300):
            timestamp = 1000.0 + index * 0.01
            rows.append((timestamp, "rx", 291, 0, struct.pack("<H", index * 10) + bytes([0x50, 3, 100, 0, 0, 0])))
            rows.append((timestamp, "rx", 512, 0, bytes(8)))
        database.executemany("INSERT INTO frames(timestamp, direction, arbitration_id, extended, data) VALUES (?,?,?,?,?)", rows)
        database.commit()
        database.close()
        return path

    def test_bmslog_summary_and_signal_series(self) -> None:
        self.open("can1")
        opened = self.workspace.open_trace(str(self.make_bmslog()))
        self.assertTrue(opened["ok"], opened)
        summary = opened["trace"]
        self.assertEqual((summary["frames"], summary["bus_profile"], len(summary["ids"])), (600, "can1", 2))
        self.assertAlmostEqual(summary["duration"], 2.99, places=2)
        spec = {"sid": "x", "id": 291, "extended": False, "signal": "Speed"}
        reply = self.workspace.trace_series(summary["id"], "can1", [spec], max_points=100)
        series = reply["series"]["x"]
        self.assertEqual(series["samples"], 300)
        self.assertLessEqual(len(series["t"]), 100)
        self.assertAlmostEqual(max(series["v"]), 29.9, places=3)             # 2990 * 0.01
        self.assertEqual(series["t"][0], 0.0)

    def test_csv_trace_loads_like_the_native_log(self) -> None:
        path = self.folder / "run.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["本地时间", "方向", "ID", "帧类型", "DLC", "数据", "说明"])
            for index in range(5):
                writer.writerow([f"2026-10-14T10:00:0{index}.000", "rx", "0x123", "标准", 8,
                                 "88 13 50 03 64 00 00 00", ""])
        trace = TraceFile(path)
        self.assertEqual((trace.count, trace.ids), (5, {(0x123, False): 5}))
        self.assertEqual(len(list(trace.frames({(0x123, False)}))), 5)

    def test_trace_audit_and_csv_export(self) -> None:
        self.open("can1")
        trace_id = self.workspace.open_trace(str(self.make_bmslog()))["trace"]["id"]
        audit = self.workspace.trace_audit(trace_id, "can1")["report"]
        self.assertEqual(audit["frames"], 600)
        self.assertEqual([c["name"] for c in audit["cycle"]], ["MotorolaMsg"])  # declared 20 ms, recorded at 10 ms
        target = self.folder / "out.csv"
        result = self.workspace.trace_export_csv(trace_id, "can1", str(target))
        self.assertTrue(result["ok"])
        with target.open(encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][:4], ["时间 s", "报文", "ID", "信号"])
        self.assertEqual(result["rows"], len(rows) - 1)
        self.assertTrue(any(row[1] == "EngineData" and row[3] == "Gear" and row[7] == "D" for row in rows))

    def test_bad_traces_are_reported(self) -> None:
        bad = self.folder / "bad.bmslog"
        bad.write_bytes(b"not a database")
        self.assertFalse(self.workspace.open_trace(str(bad))["ok"])
        self.assertFalse(self.workspace.trace_series("t999", "can1", [])["ok"])


if __name__ == "__main__":
    unittest.main()
